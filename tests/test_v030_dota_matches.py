from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import IsolatedAsyncioTestCase, TestCase

from astrbot_plugin_fiyu_core.core.dota_details import hero_result
from astrbot_plugin_fiyu_core.core.dota_knowledge import patch_history
from astrbot_plugin_fiyu_core.core.dota_matches import (
    API, ITEMS, PATCHES, PHASES, PRO_LIST, PRO_MATCH, DotaMatches, normalize_list,
)
from astrbot_plugin_fiyu_core.core.engine import Engine
from astrbot_plugin_fiyu_core.core.util import now
from test_core import FakeHost, LOGGER, metadata


class Log:
    def emit(self, *args, **fields):
        pass


class SampleTests(IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.clock = int(now()) - 500
        self.mid = 127
        self.calls = []
        self.responses = {
            PRO_LIST: [self.summary(self.mid - i) for i in range(5)],
            PATCHES: [{"id": 61, "name": "7.42"}],
            ITEMS: {"boots": {"id": 29, "dname": "Boots of Speed"}},
        }
        for i in range(5):
            self.responses[PRO_MATCH + str(self.mid - i)] = self.detail(self.mid - i)
        self.matches = DotaMatches(self.root, self.fetch, Log())

    def summary(self, mid):
        return {"match_id": mid, "start_time": self.clock - (self.mid - mid) * 60,
                "radiant_win": True, "leagueid": 11, "league_name": "Known League",
                "radiant_name": "Team A", "dire_name": "Team B", "account_id": 123456789}

    def detail(self, mid):
        players = [
            {"player_slot": slot, "hero_id": hero,
             "lane_role": 1, "account_id": 123456789,
             "purchase_log": [{"key":"boots", "time": 130},
                              {"key":"branches", "time": -60},
                              {"key":"tango", "time": 130}]}
            for hero, slot in enumerate((*range(5), *range(128, 133)), 1)
        ]
        draft = [{"order": i, "hero_id": p["hero_id"], "team": int(p["player_slot"] >= 128), "is_pick": True}
                 for i, p in enumerate(players)]
        draft.append({"order": 10, "hero_id": 11, "team": 0, "is_pick": False})
        return {**self.summary(mid), "patch": 61, "league": {"name": "Known League", "tier": "professional"},
                "radiant_team": {"name": "Team A"}, "dire_team": {"name": "Team B"},
                "players": players, "picks_bans": draft}

    async def fetch(self, url, cfg):
        self.calls.append(url)
        data = self.responses[url]
        if isinstance(data, Exception):
            raise data
        return data

    async def test_pro_filters_bound_detail_scan_and_cache_safe_evidence(self):
        sample = await self.matches.pro({}, team="Team A", patch="7.42", include_draft=True)
        self.assertCountEqual(self.calls, [
            PRO_LIST, PATCHES, *(PRO_MATCH + str(mid) for mid in range(self.mid - 2, self.mid + 1))
        ])
        self.assertEqual(sample["source_sample_count"], 5)
        self.assertEqual(sample["sample_count"], 3)
        self.assertEqual(sample["team_record"]["win_rate_pct"], 100)
        self.assertEqual(sample["draft_sample_count"], 3)
        self.assertEqual(sample["hero_rows"][0]["picks"], 3)
        self.assertEqual(sample["rows"][0]["patch"], "7.42")
        self.assertTrue(sample["rows"][0]["draft_full_lineup"])
        self.assertNotIn("account_id", repr(sample))
        self.assertNotIn("purchases", repr(sample))
        reopened = DotaMatches(self.root, self.fetch, Log())
        self.calls.clear()
        cached = await reopened.cached_pro_sample()
        self.assertEqual(len(cached["matches"]), 5)
        self.assertNotIn("account_id", repr(cached))
        self.assertEqual(self.calls, [])

    async def test_item_counts_and_single_match_purchase_order(self):
        hero = {"id": 1, "name": "Anti-Mage"}
        popularity = API + "heroes/1/itemPopularity"
        self.responses[popularity] = {key: ({"29": 205} if phase == "early" else {})
                                     for phase, (key, _) in PHASES.items()}
        unavailable = await self.matches.item_builds({}, hero, bracket=8)
        self.assertIn("error", unavailable)
        self.assertEqual(self.calls, [])
        sample = await self.matches.item_builds({}, hero, phase="early")
        self.assertEqual(sample["sample_matches_max"], 100)
        self.assertIsNone(sample["sample_matches"])
        self.assertEqual(sample["rows"], [{"phase":"early", "item_id":29,
                                           "name":"Boots of Speed", "purchases":205}])
        # Public (unclassified league) matches may have parsed purchase rows.
        raw = self.responses[PRO_MATCH + str(self.mid)]
        raw.update(leagueid=0, league=None)
        first = await self.matches.item_builds({}, hero, match_id=self.mid, patch="7.42", limit=2)
        self.assertEqual([row["time_seconds"] for row in first["rows"]], [-60, 130])
        self.assertEqual(first["purchase_count"], 3)
        self.assertEqual(first["next_offset"], 2)
        next_page = await self.matches.item_builds({}, hero, match_id=self.mid, offset=2)
        self.assertEqual(next_page["rows"], [{"item_key":"tango", "time_seconds":130}])
        self.assertEqual(first["lane_role"], 1)
        self.assertIn("error", await self.matches.pro({}, match_id=self.mid))

    async def test_probe_independent_outage_retains_pro_sample(self):
        self.responses[API + "heroes/1/itemPopularity"] = RuntimeError("unsafe response detail")
        status = await self.matches.probe({})
        self.assertEqual({entry["source"]: entry["state"] for entry in status}, {
            "opendota_pro": "有已验证样本", "opendota_match_detail": "有已验证样本",
            "opendota_item_popularity": "冷却中",
        })
        self.assertNotIn("unsafe", repr(status))
        self.calls.clear()
        await self.matches.probe({})
        self.assertEqual(self.calls, [])
        self.assertEqual(len((await self.matches.cached_pro_sample())["matches"]), 5)
        reopened = DotaMatches(self.root, self.fetch, Log())
        state = {entry["source"]: entry for entry in await reopened.status()}
        self.assertEqual(state["opendota_item_popularity"]["state"], "冷却中")
        self.assertEqual(state["opendota_pro"]["sample_count"], 5)

    async def test_disabled_domain_has_no_directory_specs_or_tasks(self):
        engine = Engine(self.root / "plugin", FakeHost(), LOGGER)
        config = engine.config.snapshot()
        config["dota"]["enabled"] = False
        engine.config.save(config, engine.config.revision())
        self.assertFalse(engine.start_action("dota_sync"))
        self.assertFalse(engine.start_action("dota_sample_probe"))
        specs = engine.tools.specs(metadata(), config)
        self.assertFalse({"query_dota_pro_matches", "query_dota_item_builds", "get_dota_matchups"} &
                         {spec["name"] for spec in specs})
        self.assertEqual((await engine.overview())["dota_samples"], [])
        self.assertFalse((engine.root / "dota").exists())

    def test_hero_side_uses_slot_not_misnamed_radiant_field(self):
        raw = {**self.summary(self.mid), "player_slot": 128, "radiant": True}
        row = normalize_list([raw], hero_id=1)[0]
        self.assertEqual(row["hero_side"], "dire")
        self.assertFalse(row["hero_won"])


class KnowledgeTests(TestCase):
    def test_verified_ability_alias_units_and_patch_order(self):
        hero = {"name":"npc_dota_hero_antimage", "abilities":[
            {"name":"antimage_blink", "name_loc":"闪烁", "type":0, "id":1, "cooldowns":[10, 8],
             "cast_ranges":[750, 1000], "immunity": 4, "desc_loc":"位移至指定位置。"}
        ]}
        details = hero_result(hero, {}, "跳", view="numbers")["ability"]
        self.assertIn("闪现", details["aliases"])
        self.assertEqual(details["stats_units"], {"冷却秒":"秒", "施法距离":"Dota 距离单位"})
        self.assertEqual(details["raw_enums"]["immunity"], 4)
        self.assertEqual(patch_history({"patches":[
            {"patch_number":"7.42", "patch_timestamp":100},
            {"patch_number":"7.41z", "patch_timestamp":90},
            {"patch_number":"7.42b", "patch_timestamp":110},
            {"patch_number":"7.42", "patch_timestamp":95},
        ]}), ["7.42b", "7.42", "7.41z"])
