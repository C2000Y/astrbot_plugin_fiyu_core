"""Offline v0.2.7 decision, routing, and diagnostic regressions."""
import asyncio
import json
import logging
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from astrbot_plugin_fiyu_core.core.engine import Engine
from astrbot_plugin_fiyu_core.core.telemetry import ModelFailure
from astrbot_plugin_fiyu_core.core.util import day_key
from test_core import FakeHost, event


class DecisionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.host = FakeHost()
        self.engine = Engine(Path(self.temp.name), self.host, logging.getLogger("fiyu.v027.decision"))
        self.addAsyncCleanup(self.engine.stop)
        cfg = self.engine.config.snapshot()
        cfg["access"].update(mode="active", allowed_groups=["g1"],
                             private_enabled=True, private_users=["alice"])
        cfg["decision"].update(ordinary_check_probability=1, merge_wait_seconds=0.05,
                               merge_max_wait_seconds=0.12, unsolicited_gap_seconds=0)
        cfg["models"].update(chat_provider="chat", decision_provider="judge")
        self.engine.config.save(cfg, self.engine.config.revision())
        self.engine.running = True

    def configure(self, section, **changes):
        cfg = self.engine.config.snapshot()
        cfg[section].update(changes)
        self.engine.config.save(cfg, self.engine.config.revision())

    async def process(self, *events):
        for message in events:
            await self.engine.on_event(message)
        workers = [state.worker for state in self.engine.states.values() if state.worker]
        if workers:
            await asyncio.wait_for(asyncio.gather(*workers), timeout=3)

    def decision_logs(self):
        return self.engine.log.list(module="DECISION")["items"]

    async def test_open_game_discussion_can_get_one_reply_without_addressing_bot(self):
        message = event("game", "这局最后一波还能翻盘")

        def judge(prompt, system, kwargs):
            self.assertIn("没点名、没问号都不是拒绝理由", system)
            self.assertIn("图片占位符不代表已看见", system)
            payload = json.loads(prompt)
            self.assertNotIn("image_descriptions", payload)
            self.assertFalse(payload["chat"][-1].get("directed_to_bot", False))
            return json.dumps({"reply": True, "confidence": 0.85,
                               "target": payload["new_ids"][-1]})

        self.host.responses.extend([judge, "那波切后排确实有机会。"])
        await self.process(message)
        self.assertEqual([call["provider"] for call in self.host.calls], ["judge", "chat"])
        self.assertEqual(self.host.sent, [(message, "那波切后排确实有机会。")])
        self.assertEqual(self.decision_logs()[0]["status"], "model_reply")

    async def test_sampling_permissions_and_direct_routes(self):
        self.configure("decision", ordinary_check_probability=0)
        await self.process(event("skipped", "我们等会开一局"))
        self.assertFalse(self.host.calls)
        self.assertEqual(self.decision_logs()[0]["reason"], "ordinary_sample_skipped")
        self.assertEqual(self.decision_logs()[0]["status"], "local_skip")

        forbidden = event("forbidden", "请帮我", location="other-group", direct=True)
        await self.process(forbidden)
        self.assertFalse(forbidden.consumed)
        self.assertFalse(self.host.calls)

        self.host.responses.extend(["在呀", "收到"])
        real_at = event("real-at", "在吗", direct=True)
        private = event("private", "早啊", kind="private", location="alice")
        await self.process(real_at)
        await self.process(private)
        self.assertEqual([call["provider"] for call in self.host.calls], ["chat", "chat"])
        self.assertEqual(self.host.sent, [(real_at, "在呀"), (private, "收到")])

    async def test_smart_name_silence_retains_no_provider_reason_text(self):
        self.configure("decision", ordinary_check_probability=0)
        private_marker = "不应写入日志的模拟隐私文本"
        self.host.responses.append(json.dumps({"reply": False, "confidence": 0.95,
                                               "target": None, "reason": private_marker}))
        await self.process(event("mention", "听说肥鱼昨天也打了两局"))
        self.assertEqual([call["provider"] for call in self.host.calls], ["judge"])
        self.assertFalse(self.host.sent)
        records = self.decision_logs()
        self.assertEqual({row["status"] for row in records}, {"model_silent"})
        self.assertNotIn(private_marker, json.dumps(records, ensure_ascii=False))
        self.assertFalse(self.engine.log.list(q=private_marker)["items"])

    async def test_other_mentions_are_not_targets_even_in_a_mixed_batch(self):
        addressed = event("addressed", "@小周 把上一条发我", uid="bob")
        addressed.info["at_others"] = True
        await self.process(addressed)
        self.assertEqual(self.decision_logs()[0]["reason"], "talking_to_others")
        self.assertFalse(self.host.calls)

        def choose_other(prompt, system, kwargs):
            payload = json.loads(prompt)
            addressed_id = payload["chat"][-1]["n"]
            self.assertNotIn(addressed_id, payload["new_ids"])
            return json.dumps({"reply": True, "confidence": 0.99, "target": addressed_id})

        second = event("second-addressed", "@小周 别忘了上次的", uid="bob")
        second.info["at_others"] = True
        self.host.responses.append(choose_other)
        await self.process(event("open", "刚才那波挺有意思"), second)
        self.assertEqual(len(self.host.calls), 1)
        self.assertFalse(self.host.sent)
        error = next(row for row in self.decision_logs() if row["code"] == "model_error_silent")
        self.assertEqual(error["error_kind"], "invalid_response")

    async def test_budget_rejection_and_provider_failure_are_separate_from_silence(self):
        self.host.responses.extend([
            ModelFailure("content_rejected", http_status=400),
            "这不是有效的 JSON",
        ])
        await self.process(event("provider-refusal", "大家来聊聊这次比赛"))
        await self.process(event("bad-shape", "这张地图我还挺喜欢"))
        self.configure("budget", module_call_limits={"decision": 2})
        await self.process(event("budget", "后天还有一场比赛"))
        self.assertFalse(self.host.sent)
        self.assertEqual(len(self.host.calls), 2)
        records = self.decision_logs()
        self.assertEqual(records[0]["status"], "decision_budget")
        self.assertEqual({row["error_kind"] for row in records if row["code"] == "model_error_silent"},
                         {"content_rejected", "invalid_response"})
        self.assertEqual((await self.engine.store.call("usage_today", day_key()))["modules"],
                         {"decision": 2})

    async def test_only_bound_image_descriptions_enter_model_and_vision_error_falls_back(self):
        async def describe(*args, visible_ids):
            return [{"message": min(visible_ids), "description": "可见图片"},
                    {"message": -1, "description": "超出决策上下文"}]

        def with_image(prompt, system, kwargs):
            payload = json.loads(prompt)
            self.assertEqual(payload["image_descriptions"], [
                {"message": payload["chat"][-1]["n"], "description": "可见图片"},
            ])
            return '{"reply":false,"confidence":0.85,"target":null}'

        self.host.responses.append(with_image)
        with patch.object(self.engine, "_decision_vision", side_effect=describe):
            await self.process(event("with-image", "图里这是什么"))

        def without_image(prompt, system, kwargs):
            self.assertNotIn("image_descriptions", json.loads(prompt))
            return '{"reply":false,"confidence":0.85,"target":null}'

        self.host.responses.append(without_image)
        with patch.object(self.engine, "_decision_vision", side_effect=ModelFailure("timeout")):
            await self.process(event("vision-failure", "这个图是怎么回事"))
        self.assertEqual(len(self.host.calls), 2)
        failure = self.engine.log.list(module="VISION")["items"][0]
        self.assertEqual((failure["code"], failure["stage"], failure["error_kind"]),
                         ("analysis_failed", "pre-decision", "timeout"))
        self.assertEqual(self.decision_logs()[0]["status"], "model_silent")
