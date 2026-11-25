import asyncio
import json
import logging
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from astrbot_plugin_fiyu_core.core.engine import Engine
from test_core import event
import test_v027_vision as vision_tests


class OCRTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        path = Path(temporary.name)
        image = path / "test-image.bin"
        image.write_bytes(b"synthetic screenshot")
        self.host = vision_tests.VisionHost(image)
        self.engine = Engine(path, self.host, logging.getLogger("fiyu-ocr-test"))
        self.addAsyncCleanup(self.engine.stop)
        self.cfg = self.engine.config.snapshot()
        self.cfg["access"].update(mode="active", allowed_groups=["g1", "g2"])
        self.cfg["models"].update(chat_provider="chat", decision_provider="judge",
                                  vision_provider="vision", ocr_provider="ocr")
        self.cfg["decision"].update(ordinary_check_probability=1, merge_wait_seconds=0.18,
                                    merge_max_wait_seconds=0.45, unsolicited_gap_seconds=0)
        self.cfg["reply"].update(image_cache_entries=10, gap_min=0, gap_max=0)
        self.save()
        self.engine.running = True

    def save(self):
        self.engine.config.save(self.cfg, self.engine.config.revision())

    async def send(self, *messages):
        for message in messages:
            await self.engine.on_event(message)
        for state in self.engine.states.values():
            if state.worker:
                await asyncio.wait_for(state.worker, timeout=3)

    def providers(self):
        return [call["provider"] for call in self.host.calls]

    async def test_balanced_ocr_uses_recent_image_and_reuses_result_for_reply(self):
        self.cfg["reply"]["vision_strategy"] = "balanced"
        self.save()
        self.host.responses.extend(['["合成文字\\n[无法辨认]"]', vision_tests.VisionStrategyTests.judge, "读到了合成文字。"])
        picture = vision_tests.VisionStrategyTests.pictured("photo")
        await self.send(picture, event("request", "请识别截图里的文字，有看不清的地方也请告诉我"))
        self.assertEqual(["ocr", "judge", "chat"], self.providers())
        self.assertEqual(["photo"], self.host.reads)
        ocr, decision, reply = self.host.calls
        self.assertIn("不补全、不猜测", ocr["prompt"])
        evidence = json.loads(decision["prompt"])["image_descriptions"]
        self.assertEqual("ocr", evidence[0]["kind"])
        self.assertEqual("合成文字\n[无法辨认]", evidence[0]["description"])
        self.assertEqual(evidence, json.loads(reply["prompt"])["image_descriptions"])

    async def test_ocr_cache_isolated_from_vision_and_other_scope(self):
        self.cfg["models"]["ocr_provider"] = "vision"
        self.save()
        self.host.responses.extend(['["画面描述"]', "看到了。", '["合成文字"]', "这是合成文字。",
                                    "还是合成文字。", '["另一个群的合成文字"]', "这是别的会话。"])
        picture = vision_tests.VisionStrategyTests.pictured
        await self.send(picture("picture", "看这张截图 [图片×1]", direct=True))
        await self.send(picture("ocr", "请提取图里的文字 [图片×1]", direct=True))
        await self.send(picture("repeat", "OCR一下 [图片×1]", direct=True))
        await self.send(picture("other", "OCR一下 [图片×1]", direct=True, location="g2"))
        self.assertEqual(["vision", "chat", "vision", "chat", "chat", "vision", "chat"], self.providers())
        evidence = [json.loads(call["prompt"])["image_descriptions"][0]
                    for call in self.host.calls if call["provider"] == "chat"]
        self.assertNotIn("kind", evidence[0])
        self.assertEqual(["画面描述", "合成文字", "合成文字", "另一个群的合成文字"],
                         [entry["description"] for entry in evidence])

    async def test_blocked_ocr_and_full_observation_do_not_send_images(self):
        self.cfg["reply"]["vision_strategy"] = "full"
        self.cfg["decision"]["ordinary_check_probability"] = 0
        self.cfg["access"]["blocked_users"] = ["blocked-sender"]
        self.save()
        picture = vision_tests.VisionStrategyTests.pictured
        ordinary = picture("ordinary", "OCR一下 [图片×1]")
        blocked = picture("blocked", "OCR一下 [图片×1]", direct=True, location="g2")
        blocked.info["image_senders"] = ["blocked-sender"]
        self.host.responses.append("这边看不到要识别的图片。")
        await self.send(ordinary)
        await self.send(blocked)
        self.assertEqual(["chat"], self.providers())
        self.assertEqual([], self.host.reads)

    async def test_ocr_error_has_no_second_provider_and_exposes_no_raw_message(self):
        self.cfg["reply"]["vision_strategy"] = "balanced"
        self.save()
        self.host.responses.extend([TimeoutError("private provider details"), vision_tests.VisionStrategyTests.judge,
                                    "暂时无法读取文字。"])
        await self.send(vision_tests.VisionStrategyTests.pictured("failed", "OCR一下 [图片×1]"))
        self.assertEqual(["ocr", "judge", "chat"], self.providers())
        self.assertNotIn("image_descriptions", json.loads(self.host.calls[1]["prompt"]))
        self.assertEqual([], json.loads(self.host.calls[2]["prompt"])["image_descriptions"])
        self.assertNotIn("private provider details", json.dumps(list(self.engine.log.recent)))

    async def test_ocr_own_quota_stops_new_requests_but_disabled_uses_vision(self):
        self.cfg["reply"]["image_cache_entries"] = 0
        self.cfg["budget"]["module_call_limits"]["ocr"] = 1
        self.save()
        self.host.responses.extend(['["合成文字"]', "这是合成文字。", "暂时无法读取文字。",
                                    '["画面和文字"]', "看到了。"])
        picture = vision_tests.VisionStrategyTests.pictured
        await self.send(picture("within-quota", "OCR一下 [图片×1]", direct=True))
        await self.send(picture("over-quota", "OCR一下 [图片×1]", direct=True))
        self.cfg["models"]["ocr_provider"] = ""
        self.save()
        await self.send(picture("disabled-ocr", "OCR一下 [图片×1]", direct=True))
        self.assertEqual(["ocr", "chat", "chat", "vision", "chat"], self.providers())
        self.assertTrue(any(entry["code"] == "analysis_budget" and entry.get("task") == "ocr"
                            for entry in self.engine.log.recent))
