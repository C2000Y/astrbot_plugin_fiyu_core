import asyncio
import json
import logging
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from astrbot_plugin_fiyu_core.core.engine import Engine
from test_core import FakeHost, event


class VisionHost(FakeHost):
    def __init__(self, path):
        super().__init__()
        self.path = path
        self.reads = []

    async def image_file(self, component):
        self.reads.append(component)
        return str(self.path)


class VisionStrategyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        path = Path(self.tmp.name)
        picture = path / "picture.bin"
        picture.write_bytes(b"synthetic image" * 14000)
        self.host = VisionHost(picture)
        self.engine = Engine(path, self.host, logging.getLogger("fiyu-vision-test"))
        self.addAsyncCleanup(self.engine.stop)
        self.cfg = self.engine.config.snapshot()
        self.cfg["access"].update(mode="active", allowed_groups=["g1", "g2"])
        self.cfg["models"].update(chat_provider="chat", decision_provider="judge", vision_provider="vision")
        self.cfg["decision"].update(ordinary_check_probability=1, merge_wait_seconds=0.02,
                                    merge_max_wait_seconds=0.1, unsolicited_gap_seconds=0)
        self.cfg["reply"].update(image_cache_entries=0, gap_min=0, gap_max=0)
        self.save()
        self.engine.running = True

    def save(self):
        self.engine.config.save(self.cfg, self.engine.config.revision())

    @staticmethod
    def pictured(ext_id, text="[图片×1]", **kwargs):
        message = event(ext_id, text, **kwargs)
        message.info["images"] = [ext_id]
        return message

    @staticmethod
    def judge(prompt, system, kwargs):
        target = json.loads(prompt)["new_ids"][-1]
        return json.dumps({"reply": True, "confidence": 0.95, "target": target})

    async def send(self, *events):
        for message in events:
            await self.engine.on_event(message)
        for state in self.engine.states.values():
            if state.worker:
                await asyncio.wait_for(state.worker, timeout=3)

    def providers(self):
        return [call["provider"] for call in self.host.calls]

    async def test_balanced_reuses_captions_for_duplicate_content_and_reply(self):
        self.cfg["reply"]["vision_strategy"] = "balanced"
        self.save()
        self.host.responses.extend(['["合成截图"]', self.judge, "截图里有测试内容。"])
        await self.send(self.pictured("one"), self.pictured("two"), event("text", "这是什么？"))
        self.assertEqual(self.providers(), ["vision", "judge", "chat"])
        self.assertEqual(len(self.host.calls[0]["images"]), 1)
        decision = json.loads(self.host.calls[1]["prompt"])["image_descriptions"]
        reply = json.loads(self.host.calls[2]["prompt"])["image_descriptions"]
        self.assertEqual(len({entry["message"] for entry in decision}), 2)
        self.assertEqual(decision, reply)
        self.assertEqual(self.host.reads, ["one", "two"])

    async def test_economy_and_balanced_skip_media_on_unsampled_batches(self):
        self.cfg["decision"]["ordinary_check_probability"] = 0
        for mode in ("economy", "balanced"):
            self.cfg["reply"]["vision_strategy"] = mode
            self.save()
            await self.send(self.pictured(mode))
        self.assertEqual(self.providers(), [])
        self.assertEqual(self.host.reads, [])
        self.host.responses.extend(['["直达图"]', "看到了。"])
        await self.send(self.pictured("direct", "看图 [图片×1]", direct=True))
        self.assertEqual(self.providers(), ["vision", "chat"])

    async def test_failed_predecision_falls_back_without_extra_paid_request(self):
        self.cfg["reply"]["vision_strategy"] = "balanced"
        self.save()
        self.host.responses.extend([TimeoutError("private provider details"), self.judge, "图片暂时看不到。"])
        await self.send(self.pictured("timeout"))
        self.assertEqual(self.providers(), ["vision", "judge", "chat"])
        self.assertNotIn("image_descriptions", json.loads(self.host.calls[1]["prompt"]))
        self.assertIn("勿假装看见", self.host.calls[2]["prompt"])

    async def test_blocked_quote_and_another_scope_never_fetch_media(self):
        self.cfg["reply"]["vision_strategy"] = "balanced"
        self.cfg["access"]["blocked_users"] = ["blocked-sender"]
        self.cfg["decision"]["ordinary_check_probability"] = 0
        self.save()
        await self.send(self.pictured("different-scope", location="g2"))
        reply = self.pictured("quoted", "看看引用图 [图片×1]", direct=True)
        reply.info["image_senders"] = ["blocked-sender"]
        self.host.responses.append("这边没看到图。")
        await self.send(reply)
        self.assertEqual(self.providers(), ["chat"])
        self.assertEqual(self.host.reads, [])

    async def test_full_mode_separate_interval_and_persisted_daily_cap(self):
        self.cfg["reply"].update(vision_strategy="full", vision_observe_daily_calls=1)
        self.cfg["decision"]["ordinary_check_probability"] = 0
        self.save()
        self.host.responses.append('["主动观察的合成图"]')
        await self.send(self.pictured("first"))
        await self.send(self.pictured("same-scope"))
        self.assertEqual(self.host.reads, ["first"])
        await self.engine.stop()
        self.engine = Engine(Path(self.tmp.name), self.host, logging.getLogger("fiyu-vision-test"))
        self.addAsyncCleanup(self.engine.stop)
        self.engine.running = True
        await self.send(self.pictured("after-restart", location="g2"))
        self.assertEqual(self.providers(), ["vision"])

    async def test_description_cache_reuses_content_in_later_turns(self):
        self.cfg["reply"]["image_cache_entries"] = 5
        self.save()
        self.host.responses.extend(['["合成图片"]', "收到图了。", "还是同一张。"])
        await self.send(self.pictured("first", direct=True))
        await self.send(self.pictured("second", direct=True))
        self.assertEqual(self.providers(), ["vision", "chat", "chat"])
        first, second = [json.loads(call["prompt"])["image_descriptions"] for call in self.host.calls[1:]]
        self.assertEqual(first[0]["description"], second[0]["description"])
        self.assertNotEqual(first[0]["message"], second[0]["message"])

    async def test_expired_media_is_not_fetched_on_a_new_request(self):
        self.cfg["decision"]["ordinary_check_probability"] = 0
        self.save()
        await self.send(self.pictured("old"))
        next(iter(self.engine.states.values())).images[0].timestamp -= 60
        self.host.responses.append("聊天里暂时看不到旧图。")
        await self.send(event("ask-older", "看看上面那张图", direct=True))
        self.assertEqual(self.host.reads, [])
        self.assertEqual(self.providers(), ["chat"])

    async def test_cancellation_during_file_load_never_calls_a_provider(self):
        self.cfg["reply"]["vision_strategy"] = "balanced"
        self.save()
        started = asyncio.Event()

        async def waiting(component):
            started.set()
            await asyncio.Future()

        self.host.image_file = waiting
        await self.engine.on_event(self.pictured("will-cancel"))
        await asyncio.wait_for(started.wait(), 3)
        await self.engine.stop()
        self.assertEqual(self.providers(), [])


if __name__ == "__main__":
    unittest.main()
