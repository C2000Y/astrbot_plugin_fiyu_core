from __future__ import annotations

import unittest
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from astrbot_plugin_fiyu_core.core.config import ConfigStore, defaults
from astrbot_plugin_fiyu_core.core.dota import (
    ALIAS_FILE, DotaService, HERO_LIST_URL, PUBLIC_STATS_URL, STATS_URL, WEEKLY_STATS_URL,
    StatsSnapshot,
)
from astrbot_plugin_fiyu_core.core.telemetry import DailyLog
from astrbot_plugin_fiyu_core.core.util import atomic_json, day_key, read_json
from test_core import LOGGER, public_fixture, weekly_fixture


# §7.2's roster IDs plus a synthetic matrix. Every non-hero slot is zero.
HERO_IDS = (*range(1, 24), *range(25, 115), 119, 120, 121, 123, 126, 128,
            129, 131, 135, 136, 137, 138, 145, 155)
NAMED_HEROES = {
    107: ("earth_spirit", "大地之灵"),
    145: ("kez", "凯"),
    155: ("largo", "朗戈"),
}
RATE_KEYS = ("enemy_win_rate", "ally_win_rate")
TAIL_UNKNOWN = {
    "first_id": 107, "second_id": 155, "enemy_unknown": True, "ally_unknown": True,
    "reason": "omitted_tail",
}


def matchup_fixture(day="2026-11-25", *, partial=False):
    heroes = []
    for hero_id in HERO_IDS:
        name, display = NAMED_HEROES.get(hero_id, (f"fixture_{hero_id}", f"测试英雄{hero_id}"))
        heroes.append({"id": hero_id, "name": "npc_dota_hero_" + name, "name_loc": display,
                       "name_english_loc": name.replace("_", " ")})
    groups = []
    for rank in range(7):
        rows = []
        for pos, hero_id in enumerate(HERO_IDS):
            width = HERO_IDS[-1] - hero_id
            row = {"hero_id": hero_id, "first_other_hero_id": hero_id + 1}
            if width:
                enemy, ally = [0] * width, [0] * width
                for other in HERO_IDS[pos + 1:]:
                    enemy[other - hero_id - 1] = ally[other - hero_id - 1] = 5000
                if hero_id == 107 and rank == 2:
                    enemy[155 - 108] = 5179
                if hero_id == 107 and rank == 0:
                    enemy[155 - 108], ally[155 - 108] = 5310, 5070
                    if partial:
                        enemy, ally = enemy[:-10], ally[:-10]
                row.update(enemy_win_rate=enemy, ally_win_rate=ally)
            rows.append(row)
        groups.append({"rank": rank, "hero_data": rows})
    return {"schema": 1, "source": STATS_URL, "heroes": heroes, "observed_date": day,
            "observed_at_china": day + "T03:00:00+08:00", "ranked_hero_data": groups}


def hero_row(payload, hero_id, rank=0):
    return next(row for row in payload["ranked_hero_data"][rank]["hero_data"]
                if row["hero_id"] == hero_id)


class PartialMatchupSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.aliases = read_json(ALIAS_FILE)["heroes"]

    def test_exact_verified_tail_shape_and_bidirectional_queries(self):
        payload = matchup_fixture(partial=True)
        snapshot = StatsSnapshot(payload, self.aliases)
        self.assertEqual(snapshot.integrity(), {
            "partial": True, "expected_pairs": 56007, "valid_pairs": 56006,
            "missing_pairs": 1, "coverage_pct": 99.9982,
        })
        self.assertEqual(snapshot.coverage[0]["valid_pairs"], 8000)
        self.assertEqual(snapshot.coverage[0]["expected_pairs"], 8001)
        self.assertEqual(snapshot.coverage[0]["tail_missing_pairs"], 1)
        self.assertEqual(snapshot.unknown_pairs[0], [TAIL_UNKNOWN])
        self.assertTrue(all(snapshot.coverage[rank]["valid_pairs"] == 8001 for rank in range(1, 7)))
        self.assertEqual(snapshot.pair(107, 155, 0), (None, None))
        self.assertEqual(snapshot.pair(155, 107, 0), (None, None))
        self.assertEqual(snapshot.pair(107, 145, 0), (50, 50))
        self.assertEqual(snapshot.pair(145, 107, 0), (50, 50))
        self.assertEqual(snapshot.pair(107, 155, 2), (51.79, 50))
        self.assertEqual(snapshot.pair(155, 107, 2), (48.21, 50))
        for hero_id, unknown_id in ((107, 155), (155, 107)):
            self.assertEqual(snapshot.hero_coverage[0][hero_id]["valid_pairs"], 125)
            for mode in ("counters", "favored", "allies"):
                ranked = snapshot.related(hero_id, 0, mode, 127)
                self.assertEqual(len(ranked), 125)
                self.assertNotIn(unknown_id, [row["id"] for row in ranked])
        self.assertEqual([len(hero_row(payload, 107)[key]) for key in RATE_KEYS], [38, 38])

    def test_complete_formats_and_single_zero_metric(self):
        for day in ("2026-11-23", "2026-11-25"):
            payload = matchup_fixture(day)
            hero_row(payload, 155, 0).update(enemy_win_rate=[], ally_win_rate=[])
            snapshot = StatsSnapshot(payload, self.aliases)
            self.assertFalse(snapshot.partial)
            self.assertEqual(snapshot.integrity()["valid_pairs"], 56007)
            self.assertEqual(snapshot.pair(107, 155, 0), (53.1, 50.7))
        row = hero_row(payload, 107)
        row["enemy_win_rate"][-1] = 0
        snapshot = StatsSnapshot(payload, self.aliases)
        self.assertTrue(snapshot.partial)
        self.assertEqual(snapshot.pair(107, 155, 0), (None, 50.7))
        self.assertEqual(snapshot.pair(155, 107, 0), (None, 50.7))
        self.assertEqual(snapshot.coverage[0]["zero_unknown_pairs"], 1)
        self.assertEqual(len(snapshot.related(107, 0, "allies", 127)), 126)
        self.assertEqual(len(snapshot.related(107, 0, "counters", 127)), 125)

    def test_invalid_shapes_are_rejected(self):
        def internal_shift(data):
            row = hero_row(data, 1)
            for key in RATE_KEYS:
                row[key].pop(24 - row["first_other_hero_id"])
                row[key].append(0)

        for name, corrupt, error in (
            ("mismatched widths", lambda d: hero_row(d, 107)[RATE_KEYS[1]].pop(), "数组异常"),
            ("first ID shifted", lambda d: hero_row(d, 107).update(first_other_hero_id=109), "数组异常"),
            ("internal shift over ID gap", internal_shift, "非英雄 ID 槽"),
            ("nonhero slot nonzero", lambda d: hero_row(d, 107)[RATE_KEYS[0]].__setitem__(115 - 108, 100), "非英雄 ID 槽"),
            ("severe tail truncation", lambda d: hero_row(d, 107).update(enemy_win_rate=[5000], ally_win_rate=[5000]), "有效配对不足"),
            ("missing rank", lambda d: d["ranked_hero_data"].pop(), "分组不完整"),
            ("duplicate rank", lambda d: d["ranked_hero_data"][1].update(rank=0), "重复"),
            ("out-of-range", lambda d: hero_row(d, 107)[RATE_KEYS[0]].__setitem__(0, 10001), "数组值无效"),
            ("wrong type", lambda d: hero_row(d, 107)[RATE_KEYS[0]].__setitem__(0, True), "数组值无效"),
            ("unknown row ID", lambda d: hero_row(d, 107).update(hero_id=115), "不在名单中"),
            ("duplicate roster ID", lambda d: d["heroes"][0].update(id=2), "英雄名单 ID"),
            ("nonfinal fields missing", lambda d: hero_row(d, 107).pop(RATE_KEYS[0]), "数组异常"),
        ):
            with self.subTest(name=name):
                payload = matchup_fixture()
                corrupt(payload)
                with self.assertRaisesRegex(ValueError, error):
                    StatsSnapshot(payload, self.aliases)


class PartialMatchupSyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_partial_snapshot_survives_restart_without_replacing_last_complete(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        ts = datetime.fromisoformat("2026-11-25T12:00:00+08:00").timestamp()
        clock = patch("astrbot_plugin_fiyu_core.core.util.time.time", lambda: ts)
        clock.start()
        self.addCleanup(clock.stop)
        config = ConfigStore(root)
        log = DailyLog(root / "logs", config, LOGGER)
        cfg = defaults()["dota"]
        cfg["snapshot_retention_days"] = 30
        previous_day = "2026-10-20"
        current_day = day_key()
        payload = matchup_fixture(current_day, partial=True)
        heroes = payload["heroes"]
        data = {
            HERO_LIST_URL: {"result": {"data": {"heroes": heroes}}},
            STATS_URL: payload,
            PUBLIC_STATS_URL: public_fixture(heroes=heroes)["hero_stats"],
            WEEKLY_STATS_URL: {"heroes": weekly_fixture(heroes=heroes)["hero_stats"]},
        }
        calls = []

        async def fetch(url, config):
            calls.append(url)
            return data[url]

        def reopen():
            service = DotaService(root / "dota", config, log)
            service._fetch = fetch
            return service

        service = reopen()
        previous_path = service.snap_dir / (previous_day.replace("-", "") + ".json")
        atomic_json(previous_path, matchup_fixture(previous_day))
        before = previous_path.read_bytes()
        status = await service.sync(cfg)
        self.assertCountEqual(calls, data)
        matchup = status["sources"]["matchups"]
        self.assertEqual(matchup["state"], "今日已同步，部分配对缺失")
        self.assertTrue(matchup["partial"])
        self.assertFalse(matchup["stale"])
        self.assertEqual(matchup["date"], current_day)
        self.assertEqual(matchup["last_complete_date"], previous_day)
        self.assertEqual(matchup["valid_pairs"], 56006)
        self.assertEqual(status["sources"]["public"]["state"], "同步成功")
        self.assertEqual(status["sources"]["weekly"]["state"], "同步成功")
        self.assertEqual(previous_path.read_bytes(), before)
        filename = current_day.replace("-", "") + ".json"
        saved = read_json(service.partial_matchup_dir / filename)
        self.assertEqual(saved["matchup_quality"]["unknown_pairs"]["0"], [TAIL_UNKNOWN])
        self.assertEqual(saved["ranked_hero_data"], payload["ranked_hero_data"])
        self.assertFalse((service.snap_dir / filename).exists())

        calls.clear()
        service = reopen()
        self.assertEqual((await service.status(cfg))["sources"]["matchups"], matchup)
        self.assertFalse(await service.auto_sync_due(cfg))
        await service.sync(cfg)
        self.assertEqual(calls, [])
        query = {"hero": "大地之灵", "other": "朗戈", "rank": 0}
        current = await service.stats(cfg, "matchups", **query)
        self.assertEqual(current["snapshot_date"], current_day)
        self.assertTrue(current["partial"])
        self.assertEqual(current["pair_status"], "partial")
        self.assertIsNone(current["first_vs_second_pct"])
        self.assertIsNone(current["same_team_pct"])
        prior = await service.stats(cfg, "matchups", observed_date=previous_day, **query)
        self.assertEqual(prior["snapshot_date"], previous_day)
        self.assertTrue(prior["stale"])
        self.assertFalse(prior["partial"])
        self.assertEqual(prior["first_vs_second_pct"], 53.1)
        ranked = await service.stats(cfg, "matchups", hero="大地之灵", rank=0, mode="counters")
        self.assertEqual(ranked["available_pairs"], 125)
        self.assertNotIn(155, [row["id"] for row in ranked["rows"]])

        data[STATS_URL] = matchup_fixture(current_day)
        final = await service.sync(cfg, manual=True)
        self.assertCountEqual(calls, [HERO_LIST_URL, STATS_URL])
        self.assertFalse(final["sources"]["matchups"]["partial"])
        self.assertEqual(final["sources"]["matchups"]["last_complete_date"], current_day)
        complete_path = service.snap_dir / filename
        complete_bytes = complete_path.read_bytes()
        self.assertFalse(previous_path.exists())

        data[STATS_URL] = payload
        await service.sync(cfg, force=True)
        self.assertEqual(complete_path.read_bytes(), complete_bytes)
        updated = await service.stats(cfg, "matchups", **query)
        self.assertEqual(updated["pair_status"], "complete")
        self.assertEqual(updated["first_vs_second_pct"], 53.1)
