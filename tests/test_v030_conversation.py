"""Offline checks for opt-in routing and per-session activity sampling."""

import asyncio
import json
import logging
import random
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from astrbot_plugin_fiyu_core.core.config import ConfigError, defaults, validate
from astrbot_plugin_fiyu_core.core.decision import DecisionTraffic
from astrbot_plugin_fiyu_core.core.engine import Engine
from astrbot_plugin_fiyu_core.core.session_config import PRESETS
from astrbot_plugin_fiyu_core.core.telemetry import ModelFailure
from astrbot_plugin_fiyu_core.core.util import day_key
from test_core import FakeHost, event


class TrafficTests(unittest.TestCase):
    def setUp(self):
        self.cfg = defaults()["decision"]

    def replay(self, interval, participants):
        activity = DecisionTraffic()
        rng = random.Random(45)
        sampled = 0
        for i in range(50):
            at = i * interval
            activity.record(at, f"speaker-{i % participants}")
            observed = activity.sample(self.cfg, 0.35, at, set())
            sampled += rng.random() < observed.probability
        return activity, sampled

    def test_quiet_normal_burst_and_smooth_recovery(self):
        quiet, quiet_calls = self.replay(45, 1)
        ordinary, ordinary_calls = self.replay(3, 3)
        burst, burst_calls = self.replay(0.3, 8)
        self.assertEqual([s.latest.activity for s in (quiet, ordinary, burst)],
                         ["quiet", "normal", "burst"])
        self.assertGreater(quiet_calls, ordinary_calls)
        self.assertGreater(ordinary_calls, burst_calls)
        at_burst = burst.latest.probability
        burst.record(105, "same-scope")
        first = burst.sample(self.cfg, 0.35, 105, set())
        self.assertGreater(first.probability, at_burst)
        self.assertLess(first.probability, quiet.latest.probability)
        next_sample = burst.sample(self.cfg, 0.35, 180, set())
        self.assertGreater(next_sample.probability, first.probability)
        self.assertEqual(burst.sample(self.cfg, 0, 180, set()).probability, 0)

    def test_excluded_users_bounded_state_and_config_shape(self):
        state = DecisionTraffic()
        for i in range(600):
            state.record(i / 100, "blocked" if i % 2 else "authorized")
        self.assertEqual(len(state.messages), 512)
        visible = state.sample(self.cfg, 1, 6, {"blocked"})
        self.assertEqual(visible.speakers, 1)
        self.assertLess(visible.probability, 1)
        self.assertEqual(visible.short_rate, 256 * 60 / self.cfg["dynamic_short_seconds"])
        with self.assertRaises(ConfigError):
            validate({"decision": {"dynamic_short_seconds": 50, "dynamic_window_seconds": 20}})


class ConversationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.host = FakeHost()
        self.engine = Engine(Path(temp.name), self.host, logging.getLogger("fiyu.v030.conversation"))
        self.addAsyncCleanup(self.engine.stop)
        cfg = self.engine.config.snapshot()
        cfg["access"].update(mode="active", allowed_groups=["g1", "g2"])
        cfg["decision"].update(merge_wait_seconds=0.03, merge_max_wait_seconds=0.12,
                               ordinary_check_probability=0, unsolicited_gap_seconds=0)
        cfg["models"].update(chat_provider="chat", decision_provider="judge")
        cfg["dota"]["auto_sync"] = False
        self.engine.config.save(cfg, self.engine.config.revision())
        self.engine.running = True

    def update(self, section, **values):
        cfg = self.engine.config.snapshot()
        cfg[section].update(values)
        self.engine.config.save(cfg, self.engine.config.revision())

    async def process(self, original):
        await self.engine.on_event(original)
        running = [state.worker for state in self.engine.states.values() if state.worker]
        if running:
            await asyncio.wait_for(asyncio.gather(*running), timeout=5)

    async def test_role_model_is_opt_in_and_tool_context_routes_to_primary(self):
        self.host.responses.append("早上好呀")
        await self.process(event("legacy", "早上好呀", uid="first", direct=True))
        self.assertEqual(self.host.calls[0]["provider"], "chat")

        self.update("models", casual_provider="rp", casual_context_chars=800)
        self.host.responses.extend(["也歇一会儿吧", "可以继续查资料", "先核对出处"])
        await self.process(event("rp", "终于忙完啦", uid="first", direct=True))
        await self.process(event("lookup", "紫猫的胜率是多少？", uid="first", direct=True))
        await self.process(event("follow", "那它呢", uid="first", direct=True))
        self.assertEqual([call["provider"] for call in self.host.calls], ["chat", "rp", "chat", "chat"])
        self.assertFalse(self.host.calls[1]["tools"])
        self.assertIn("你是肥鱼", self.host.calls[1]["system"])
        self.assertIn("related_memories", json.loads(self.host.calls[1]["prompt"]))
        self.assertIn("get_hero_stats", {s["name"] for s in self.host.calls[2]["tools"]})

    async def test_deep_route_daily_budget_total_reply_cap_and_no_vendor_fallback(self):
        self.update("models", casual_provider="rp", deep_provider="deep", deep_daily_calls=1)
        self.update("budget", module_call_limits={"reply": 2})
        self.host.responses.extend(["可以按基例和归纳两步证明", ModelFailure("quota_exhausted")])
        for identifier, text in (
            ("deep", "请严格证明有限集合上的归纳原理"),
            ("deep-cap", "请严格证明三角不等式"),
            ("rp-rejection", "今天玩得真开心"),
            ("aggregate-cap", "现在查一下刀塔胜率"),
        ):
            await self.process(event(identifier, text, uid="speaker", direct=True))
        self.assertEqual([call["provider"] for call in self.host.calls], ["deep", "rp"])
        self.assertFalse(self.host.calls[0]["tools"])
        self.assertEqual((await self.engine.store.call("usage_today", day_key()))["modules"],
                         {"reply_deep": 1, "reply_casual": 1})

    async def test_ingress_ignores_duplicate_blocked_and_bot_and_keeps_scopes_separate(self):
        self.update("decision", dynamic_enabled=True)
        self.update("access", blocked_users=["blocked"])
        seed = event("seed", "给这群留一条模拟消息", uid="first")
        await self.process(seed)
        await self.process(seed)
        await self.process(event("blocked", "不应记录", uid="blocked"))
        bot = event("self", "不应记录机器人", uid="self")
        bot.info["self_message"] = True
        await self.process(bot)
        await self.process(event("second-scope", "给另一个群留消息", uid="second", location="g2"))
        self.assertFalse(self.host.calls)
        states = list(self.engine.states.values())
        self.assertEqual([{uid for _, uid in state.traffic.messages} for state in states],
                         [{"first"}, {"second"}])
        self.assertEqual([len(state.traffic.messages) for state in states], [1, 1])
        self.assertEqual([state.traffic.latest.probability for state in states], [0, 0])
        overview = await self.engine.overview()
        self.assertEqual(len(overview["decision_activity"]), 2)
        self.assertNotIn("first", json.dumps(overview["decision_activity"]))

        self.host.responses.extend(["在呀", '{"reply":false,"confidence":0.95,"target":null}'])
        await self.process(event("direct", "在吗", uid="first", direct=True))
        await self.process(event("continued", "我还有件事", uid="first"))
        self.assertEqual([call["provider"] for call in self.host.calls], ["chat", "judge"])

    async def test_preset_changes_only_selected_scope_and_restores_inheritance(self):
        self.update("decision", ordinary_check_probability=0.35)
        self.update("social", energy_enabled=False)
        original = event("ordinary", "群里能聊点轻松的", location="g1")
        scoped = event("scoped", "群里能聊点轻松的", location="g2")
        self.update("sessions", overrides={scoped.info["scope"]: PRESETS["quiet"]})
        self.host.responses.append('{"reply":false,"confidence":0.95,"target":null}')
        with patch.object(self.engine.rng, "random", return_value=0.4):
            await self.process(original)
            await self.process(scoped)
            self.update("sessions", overrides={})
            await self.process(event("after-reset", "回到全局设置", location="g2"))
        self.assertEqual([call["provider"] for call in self.host.calls], ["judge"])
        self.assertTrue(all(state.traffic.latest is None for state in self.engine.states.values()))
