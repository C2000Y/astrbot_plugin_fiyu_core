import json
import logging
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from astrbot_plugin_fiyu_core.core.engine import Engine
from astrbot_plugin_fiyu_core.core.research_sources import PRO_LIST, STEAM_RSS, RequestBudget
from astrbot_plugin_fiyu_core.core.util import dump, now, stamp
from test_core import FakeHost, add_message, metadata
from test_v024 import rss, summary


class ResearchSourceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.host = FakeHost()
        self.engine = Engine(Path(temp.name), self.host, logging.getLogger("test.research.sources"))
        self.research = self.engine.research
        self.cfg = self.engine.config.snapshot()
        self.cfg["access"].update(mode="active", allowed_groups=["g1"])
        self.cfg["models"].update(chat_provider="chat")
        self.cfg["research"].update(enabled=True, data_requests=1, news_transport="rss_only")
        add_message(self.engine.store, metadata(), "alice", "Dota 和 CS2", "test-1")
        self.urls = []

        async def fetch(url, params, cfg):
            self.urls.append(url)
            self.assertEqual(url, STEAM_RSS[730])
            return rss()

        self.research._fetch = fetch

    def save_sources(self, *sources, dota=True):
        self.cfg["dota"]["enabled"] = dota
        self.cfg["research"]["sources"] = list(sources)
        self.engine.config.save(self.cfg, self.engine.config.revision())

    async def test_disabled_dota_does_not_initialize_domain_or_supply_prompts(self):
        def forbidden():
            self.fail("Dota service was requested while the domain was disabled")

        self.research._dota = forbidden
        self.save_sources("dota_news", "dota_pro", "dota_pro_cache", "dota_meta", "cs2_news", dota=False)
        for source in ("dota_news", "dota_pro", "dota_pro_cache", "dota_meta"):
            self.assertEqual(await self.research.evidence(source, self.cfg), [])

        def choose(prompt, system, kwargs):
            data = json.loads(prompt)
            self.assertEqual(list(data["sources"]), ["cs2_news"])
            self.assertNotIn("dota", (prompt + system).casefold())
            return dump({"source": "cs2_news", "topic": "CS2 测试公告"})

        self.host.responses.extend([choose, summary()])
        result = await self.research.run_once(self.cfg)
        self.assertEqual(result["state"], "completed")
        self.assertEqual(self.urls, [STEAM_RSS[730]])
        self.urls.clear()
        status = await self.research.probe(self.cfg)
        self.assertEqual(self.urls, [STEAM_RSS[730]])
        self.assertEqual({row["key"] for row in status["sources"]}, {"steam_rss", "cs2_esports"})
        cs2 = status["sources"][-1]
        self.assertEqual(cs2["state"], "unavailable")
        self.assertIn("Steam", cs2["reason"])

    async def test_opt_in_local_source_uses_recent_validated_samples_without_network(self):
        observed = int(now()) - 20
        self.save_sources("dota_pro_cache")
        sample = {
            "source": PRO_LIST,
            "observed_at": stamp(observed),
            "matches": [
                {"match_id": 42, "match_date": stamp(observed - 30), "radiant_team": "Team A",
                 "dire_team": "Team B", "radiant_win": False, "league_name": "Test League"},
                {"match_id": 40, "start_time": observed - 60, "radiant_team": "Team C",
                 "dire_team": "Team D", "radiant_win": True, "league_name": "Test League"},
                {"match_id": 41, "start_time": observed - 31 * 86400, "radiant_team": "Team A",
                 "dire_team": "Old Team", "radiant_win": True, "league_name": "Test League"},
                {"match_id": 43, "start_time": observed - 60, "radiant_team": "Team A",
                 "dire_team": "Unknown Team", "radiant_win": 1, "league_name": "Test League"},
            ],
        }

        async def cached():
            return sample

        self.research._dota = SimpleNamespace(matches=SimpleNamespace(cached_pro_sample=cached))
        budget = RequestBudget(0)
        self.assertEqual(await self.research._eligible(self.cfg, budget), ["dota_pro_cache"])
        records = await self.research.evidence("dota_pro_cache", self.cfg, "Team A", budget)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["key"], "opendota:match:42")
        self.assertEqual(json.loads(records[0]["text"])["胜者"], "Team B")
        self.assertIn("2 局有效职业样本", records[0]["scope"])
        self.assertEqual(budget.used, 0)

        self.host.responses.extend([
            dump({"source": "dota_pro_cache", "topic": "Team A 职业样本", "focus": "Team A"}),
            summary(),
        ])
        result = await self.research.run_once(self.cfg)
        self.assertEqual(result["state"], "completed")
        self.assertEqual(result["requests"], 0)
        self.assertEqual(self.urls, [])
        status = await self.research.probe(self.cfg)
        self.assertEqual(status["requests"], 0)
        self.assertEqual(status["sources"][0]["state"], "ok")
        self.assertIn(stamp(observed), status["sources"][0]["reason"])

        sample["observed_at"] = stamp(observed - 31 * 86400)
        self.assertEqual(await self.research._eligible(self.cfg, budget), [])
        status = await self.research.source_status(self.cfg)
        self.assertEqual(status[0]["state"], "empty")
