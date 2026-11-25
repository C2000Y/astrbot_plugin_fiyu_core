from __future__ import annotations

import asyncio
import json
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from astrbot_plugin_fiyu_core.core.config import ConfigStore, defaults
from astrbot_plugin_fiyu_core.core.dota import (
    DotaService, HERO_LIST_URL, PUBLIC_STATS_URL, STATS_URL, WEEKLY_STATS_URL,
)
from astrbot_plugin_fiyu_core.core.research_sources import HTTPFailure
from astrbot_plugin_fiyu_core.core.telemetry import DailyLog
from astrbot_plugin_fiyu_core.core.util import atomic_json, day_key, stamp
from test_core import LOGGER, fixture, public_fixture, weekly_fixture


class DotaSyncTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.clock = datetime.fromisoformat("2026-11-25T22:00:00+08:00").timestamp()
        clock = patch("astrbot_plugin_fiyu_core.core.util.time.time", lambda: self.clock)
        clock.start()
        self.addCleanup(clock.stop)
        self.config = ConfigStore(self.root)
        self.log = DailyLog(self.root / "logs", self.config, LOGGER)
        self.cfg = defaults()["dota"]
        self.matchups = fixture(day_key())
        roster = self.matchups["heroes"]
        self.responses = {
            HERO_LIST_URL: {"result": {"data": {"heroes": roster}}},
            STATS_URL: self.matchups,
            PUBLIC_STATS_URL: public_fixture(heroes=roster)["hero_stats"],
            WEEKLY_STATS_URL: {"heroes": weekly_fixture(heroes=roster)["hero_stats"]},
        }
        self.requests = []
        self.service = self.reopen()

    def reopen(self):
        service = DotaService(self.root / "dota", self.config, self.log)

        async def fetch(url, cfg):
            self.requests.append(url)
            data = self.responses[url]
            if isinstance(data, Exception):
                raise data
            return data

        service._fetch = fetch
        return service

    def assert_requests(self, *urls):
        self.assertCountEqual(self.requests, urls)
        self.requests.clear()

    async def test_partial_matchup_cools_across_restart_and_manual_recovery_is_independent(self):
        previous = (datetime.fromisoformat(day_key()) - timedelta(days=1)).date().isoformat()
        previous_path = self.service.snap_dir / f"{previous.replace('-', '')}.json"
        atomic_json(previous_path, fixture(previous))
        previous_bytes = previous_path.read_bytes()
        # Reproduce the live Valve failure shape without committing the live payload.
        # Both arrays end before a real higher hero ID in exactly one rank.
        row = self.matchups["ranked_hero_data"][0]["hero_data"][-2]
        rates = {key: row[key][:] for key in ("enemy_win_rate", "ally_win_rate")}
        for key in rates:
            row[key].pop()

        result = await self.service.sync(self.cfg)
        self.assert_requests(HERO_LIST_URL, PUBLIC_STATS_URL, WEEKLY_STATS_URL, STATS_URL)
        self.assertEqual(result["state"], "部分同步成功")
        failure = result["sources"]["matchups"]
        self.assertEqual(failure["date"], previous)
        self.assertTrue(failure["stale"])
        self.assertEqual(failure["failure_kind"], "invalid_structure")
        self.assertIn("rank 0 英雄 101", failure["reason"])
        self.assertIn("对抗 24、同队 24，应各有 25 项", failure["reason"])
        self.assertEqual(failure["next_attempt"], stamp(self.clock + 4 * 3600))
        self.assertEqual(previous_path.read_bytes(), previous_bytes)

        self.service = self.reopen()
        self.assertEqual((await self.service.status(self.cfg))["sources"]["matchups"], failure)
        self.assertFalse(await self.service.auto_sync_due(self.cfg))
        await self.service.sync(self.cfg)
        self.assert_requests()

        self.clock += 3 * 3600  # Other sources are due on the next day, before matchups cools down.
        self.assertTrue(await self.service.auto_sync_due(self.cfg))
        result = await self.service.sync(self.cfg)
        self.assert_requests(HERO_LIST_URL, PUBLIC_STATS_URL, WEEKLY_STATS_URL)
        self.assertEqual(result["state"], "部分同步成功")
        self.assertEqual(result["sources"]["matchups"]["date"], previous)
        self.assertEqual(previous_path.read_bytes(), previous_bytes)

        row.update(rates)
        result = await self.service.sync(self.cfg, manual=True)
        self.assert_requests(HERO_LIST_URL, STATS_URL)
        self.assertEqual(result["state"], "同步成功")
        current = (await self.reopen().status(self.cfg))["sources"]["matchups"]
        self.assertEqual(current["date"], day_key())
        self.assertEqual(current["success_date"], day_key())
        self.assertEqual(current["last_attempt"], stamp(self.clock))
        self.assertNotIn("failure_kind", current)
        self.assertNotIn("next_attempt", current)

    async def test_transient_and_invalid_json_failures_have_separate_retry_schedules(self):
        self.responses.update({
            PUBLIC_STATS_URL: TimeoutError("private connection detail"),
            WEEKLY_STATS_URL: json.JSONDecodeError("private payload", "private payload", 0),
            STATS_URL: HTTPFailure(503),
        })
        result = await self.service.sync(self.cfg)
        self.assert_requests(HERO_LIST_URL, PUBLIC_STATS_URL, WEEKLY_STATS_URL, STATS_URL)
        self.assertEqual(result["state"], "同步失败")
        states = result["sources"]
        self.assertEqual(states["public"]["failure_kind"], "timeout")
        self.assertEqual(states["weekly"]["failure_kind"], "invalid_json")
        self.assertEqual(states["matchups"]["failure_kind"], "http_error")
        self.assertEqual(states["public"]["next_attempt"], stamp(self.clock + 900))
        self.assertEqual(states["matchups"]["next_attempt"], stamp(self.clock + 900))
        self.assertEqual(states["weekly"]["next_attempt"], stamp(self.clock + 4 * 3600))
        self.assertNotIn("private", str(states) + str(self.log.recent))

        self.clock += 900
        result = await self.service.sync(self.cfg)
        self.assert_requests(HERO_LIST_URL, PUBLIC_STATS_URL, STATS_URL)
        self.assertEqual(result["sources"]["public"]["next_attempt"], stamp(self.clock + 1800))
        await self.service.sync(self.cfg, manual=True)
        self.assert_requests(HERO_LIST_URL, PUBLIC_STATS_URL, WEEKLY_STATS_URL, STATS_URL)
        recent = self.reopen()
        result = await recent.sync(self.cfg, manual=True)
        self.assert_requests()
        self.assertEqual(result["sources"]["weekly"]["next_manual_attempt"], stamp(self.clock + 60))

    async def test_cancellation_preserves_snapshots_and_a_short_restart_hold(self):
        entered = asyncio.Event()

        async def fetch(url, cfg):
            entered.set()
            await asyncio.Event().wait()

        self.service._fetch = fetch
        task = asyncio.create_task(self.service.sync(self.cfg))
        await asyncio.wait_for(entered.wait(), 2)
        self.assertEqual((await self.service.status(self.cfg))["state"], "同步中")
        self.assertFalse(await self.service.auto_sync_due(self.cfg))
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

        reopened = self.reopen()
        result = await reopened.sync(self.cfg)
        self.assert_requests()
        for state in result["sources"].values():
            self.assertEqual(state["failure_kind"], "interrupted")
            self.assertIsNone(state["date"])
            self.assertEqual(state["next_attempt"], stamp(self.clock + 900))
        self.clock += 900
        self.assertTrue(await reopened.auto_sync_due(self.cfg))
