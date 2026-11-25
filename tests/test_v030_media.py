import asyncio
import io
import json
import logging
import wave
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, patch

from astrbot_plugin_fiyu_core.core.config import ConfigError, validate
from astrbot_plugin_fiyu_core.core.dashscope_media import DashScopeMedia, media_format, wav_seconds
from astrbot_plugin_fiyu_core.core.engine import Engine
from astrbot_plugin_fiyu_core.core.media import MeteredResult
from astrbot_plugin_fiyu_core.core.session_config import effective_config
from astrbot_plugin_fiyu_core.core.telemetry import BudgetExceeded, ModelFailure
from astrbot_plugin_fiyu_core.core.util import day_key
from test_core import FakeHost, event, metadata

PNG = bytes.fromhex("89504e470d0a1a0a0000000d4948445200000001000000010802000000907753de"
                    "0000000c49444154789c63606060000000040001f61738550000000049454e44ae426082")


def sound():
    buf = io.BytesIO()
    with wave.open(buf, "wb") as out:
        out.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
        out.writeframes(b"\0" * 32000)
    return buf.getvalue()


class MediaHost(FakeHost):
    media_supported = staticmethod(lambda _event, kind: kind in ("image", "audio"))

    def __init__(self):
        super().__init__()
        self.attachments = []
        self.synthesize = AsyncMock()
        self.transcribe = AsyncMock(return_value="合成语音测试")
        self.error = None

    async def image_file(self, component):
        return component

    async def media_file(self, component):
        return component

    async def send_media(self, event, kind, path):
        self.attachments.append((kind, Path(path).read_bytes(), path))
        if self.error:
            raise self.error
        return True


class MediaTests(IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.host = MediaHost()
        self.engine = Engine(Path(self.tmp.name), self.host, logging.getLogger("fiyu-media-test"))
        self.addAsyncCleanup(self.engine.stop)
        self.engine.running = True
        self.cfg = self.engine.config.snapshot()
        self.cfg["access"].update(mode="active", allowed_groups=["g1", "g2"])
        self.cfg["decision"].update(ordinary_check_probability=0.0, merge_wait_seconds=0.02,
                                    merge_max_wait_seconds=0.1, unsolicited_gap_seconds=0)
        self.cfg["reply"].update(gap_min=0, gap_max=0)
        for kind in ("image", "stt", "tts", "omni"):
            self.cfg["media"][kind].update(enabled=True, allowed_groups=["g1"])
        self.cfg["media"]["stt"]["provider"] = "native-stt"
        self.cfg["media"]["tts"]["provider"] = "native-tts"
        self.save()
        self.path = Path(self.tmp.name) / "test.wav"
        self.path.write_bytes(sound())
        self.host.synthesize.return_value = str(self.path)
        self.draw = AsyncMock(return_value=MeteredResult([PNG], 1))
        self.omni = AsyncMock(return_value=MeteredResult("实际合成录音", 512))
        self.engine.conversation_media.api = lambda: type("TestApi", (), {
            "draw": self.draw, "understand": self.omni,
        })()

    def save(self):
        self.engine.config.save(self.cfg, self.engine.config.revision())

    async def deliver(self, *messages):
        for message in messages:
            await self.engine.on_event(message)
        for state in self.engine.states.values():
            if state.worker:
                await asyncio.wait_for(state.worker, timeout=4)

    async def test_drawing_permission_dedup_and_cleanup(self):
        await self.deliver(event("passive", "她真会画图", location="g1"))
        await self.deliver(event("question", "画图很贵吗"))
        await self.deliver(event("unauthorized", "画图：竹林", direct=True, location="g2"))
        self.draw.assert_not_called()
        request = event("draw", "画图：一只小猫")
        await self.deliver(request)
        await self.deliver(request)
        self.draw.assert_awaited_once()
        self.assertEqual(self.host.attachments[0][:2], ("image", PNG))
        self.assertFalse(Path(self.host.attachments[0][2]).exists())
        self.assertEqual((await self.engine.media.overview())[0]["used"], 1)
        self.assertEqual(self.host.calls, [])

    async def test_drawing_count_is_checked_before_provider(self):
        await self.deliver(event("over-count", "画图 2 张：竹林"))
        self.draw.assert_not_called()
        self.assertIn("额度", self.host.sent[-1][1])
        self.cfg["media"]["image"]["max_units"] = 2
        self.save()
        self.draw.return_value = MeteredResult([PNG, PNG], 2)
        await self.deliver(event("allowed-count", "画图 2 张：竹林"))
        self.assertEqual(self.draw.await_args.args[2], 2)
        self.assertEqual(len(self.host.attachments), 2)

    async def test_edit_references_come_from_current_authorized_conversation(self):
        await self.deliver(event("edit", "改图：把墙刷成蓝色"))
        path = Path(self.tmp.name) / "input.png"
        path.write_bytes(PNG)
        source = event("source", "[图片×1]", uid="bob")
        source.info["images"] = [str(path)]
        await self.deliver(source)
        await self.deliver(event("other-sender", "改图：背景变绿"))
        self.draw.assert_not_called()
        await self.deliver(event("owner", "改图：背景变绿", uid="bob"))
        self.assertEqual(self.draw.await_args.args[-1], [(PNG, "image/png")])

    async def test_stt_and_omni_reuse_real_media_only(self):
        audio = event("audio", "[语音]")
        audio.info["media"] = [("audio", str(self.path))]
        await self.deliver(audio)
        await self.deliver(event("caption", "转文字"))
        await self.deliver(event("again", "转文字"))
        self.host.transcribe.assert_awaited_once_with("native-stt", str(self.path))
        self.assertEqual(self.host.sent[-1][1], "语音转写：合成语音测试")
        self.assertEqual((await self.engine.media.overview())[0]["used"], 1)
        await self.deliver(event("different-scope", "转文字", direct=True, location="g2"))
        self.host.transcribe.assert_awaited_once()
        await self.deliver(event("no-video", "分析视频"))
        self.assertIn("没有收到", self.host.sent[-1][1])
        self.omni.assert_not_called()
        await self.deliver(event("use-audio", "分析音频"))
        self.omni.assert_awaited_once()
        self.assertEqual(self.omni.await_args.args[1], sound())

    async def test_tts_with_uncertain_submission_never_retries(self):
        self.host.responses.append("你好，听得见吗？")
        await self.deliver(event("tts", "用语音回复：打个招呼"))
        self.assertEqual(self.host.attachments[0][:2], ("audio", sound()))
        self.host.error = TimeoutError("synthetic provider body")
        self.host.responses.append("第二条语音。")
        await self.deliver(event("tts-uncertain", "用语音回复：再说一句"))
        self.assertEqual(self.host.synthesize.await_count, 2)
        self.assertEqual(len(self.host.attachments), 2)
        self.assertEqual(self.host.sent, [])

    async def test_free_budget_concurrency_and_crash_recovery(self):
        self.cfg["media"]["image"].update(free_only=True, free_units_per_day=1, free_until=day_key())
        self.save()
        entered, finish = asyncio.Event(), asyncio.Event()

        async def operation(_cfg):
            entered.set()
            await finish.wait()
            return MeteredResult(object(), 1)

        meta = metadata("g1")
        running = asyncio.create_task(self.engine.media.run("image", meta, 1, operation))
        await asyncio.wait_for(entered.wait(), timeout=3)
        try:
            with self.assertRaises(BudgetExceeded):
                await self.engine.media.run("image", meta, 1, operation)
        finally:
            finish.set()
            await running
        ledger = self.engine.media.ledger
        ledger.reserve(day_key(), "tts", "native-tts", meta["scope"], 17, (50, 50))
        ledger.recover()
        tts = next(r for r in ledger.overview(1) if r["kind"] == "tts")
        self.assertEqual((tts["unknown"], tts["estimated_units"], tts["pending"]), (1, 17, 0))

    async def test_interruption_during_provider(self):
        entered = asyncio.Event()

        async def generating(*args):
            entered.set()
            await asyncio.Future()

        self.draw.side_effect = generating
        await self.engine.on_event(event("cancel-draw", "画图：山水"))
        await asyncio.wait_for(entered.wait(), 3)
        await self.deliver(event("cancel", "先别说", direct=True))
        self.assertEqual(self.host.attachments, [])
        row = (await self.engine.media.overview())[0]
        self.assertEqual((row["unknown"], row["used"], row["pending"]), (1, 1, 0))

    async def test_revocation_after_generation_prevents_submission(self):
        async def policy_changes(*_args):
            self.cfg["media"]["image"]["enabled"] = False
            self.save()
            return MeteredResult([PNG], 1)

        self.draw.side_effect = policy_changes
        await self.deliver(event("revoked", "画图：取消权限用的测试图"))
        self.draw.assert_awaited_once()
        self.assertEqual(self.host.attachments, [])
        self.assertEqual((await self.engine.media.overview())[0]["used"], 1)

    async def test_fun_asr_protocol_uses_explicit_audio_and_measured_seconds(self):
        api = DashScopeMedia("https://dashscope.aliyuncs.com", api_key="synthetic")
        api.json_request = AsyncMock(return_value={"output": {"text": "测试转写"}, "usage": {"duration": 2}})
        result = await api.transcribe("fun-asr-flash-2026-06-15", sound(), "audio/wav", 1)
        self.assertEqual(result, MeteredResult("测试转写", 2))
        request = api.json_request.await_args
        self.assertIn("multimodal-generation/generation", request.args[2])
        self.assertEqual(request.kwargs["extra"]["X-DashScope-SSE"], "disable")
        item = request.kwargs["body"]["input"]["messages"][0]["content"][0]
        self.assertTrue(item["input_audio"]["data"].startswith("data:audio/wav;base64,"))

    async def test_omni_sse_requires_terminal_event_and_records_usage(self):
        api = DashScopeMedia("https://dashscope.aliyuncs.com", api_key="synthetic")
        frames = [
            {"choices": [{"delta": {"content": "合成内容"}}]},
            {"choices": [], "usage": {"total_tokens": 51}},
        ]
        request = {}

        class Stream:
            status = 200

            @property
            def content(self):
                async def chunks():
                    for frame in frames:
                        yield b"data: " + json.dumps(frame).encode() + b"\n"
                    if request["finished"]:
                        yield b"data: [DONE]\n"

                return chunks()

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_):
                return False

        class Session(Stream):
            def post(self, url, **kwargs):
                request["url"], request["body"] = url, kwargs["json"]
                return Stream()

        api.session = Session
        request["finished"] = True
        result = await api.understand("qwen3.8-omni-flash", sound(), "audio/wav", "描述声音", 500)
        self.assertEqual(result, MeteredResult("合成内容", 51))
        self.assertEqual(request["body"]["modalities"], ["text"])
        self.assertTrue(request["body"]["messages"][0]["content"][0]["input_audio"]["data"].startswith("data:;base64,"))
        request["finished"] = False
        with self.assertRaises(ModelFailure):
            await api.understand("qwen3.8-omni-flash", sound(), "audio/wav", "描述声音", 500)

    async def test_wan_protocol_and_reject_untrusted_result_url(self):
        api = DashScopeMedia("https://dashscope.aliyuncs.com", api_key="synthetic")
        api.json_request = AsyncMock(side_effect=[
            {"output": {"task_id": "public-test"}},
            {"output": {"task_status": "SUCCEEDED", "results": [{"url":
             "https://dashscope-result-bj.oss-cn-beijing.aliyuncs.com/test.png"}]}},
        ])
        api.download_image = AsyncMock(return_value=PNG)
        with patch("astrbot_plugin_fiyu_core.core.dashscope_media.asyncio.sleep", new=AsyncMock()):
            result = await api.draw("wan2.7-image", "合成", 1, "1024*1024", [(PNG, "image/png")])
        self.assertEqual(result, MeteredResult([PNG], 1))
        posted = api.json_request.await_args_list[0]
        self.assertEqual(posted.kwargs["extra"]["X-DashScope-Async"], "enable")
        self.assertTrue(posted.kwargs["body"]["input"]["messages"][0]["content"][0]["image"].startswith("data:image/png;"))
        self.assertEqual(api.json_request.await_args.args[-1], "/api/v1/tasks/public-test")
        with self.assertRaises(ModelFailure):
            await DashScopeMedia("https://dashscope.aliyuncs.com", "synthetic").download_image(
                object(), "https://untrusted.example.test/image.png"
            )

    def test_config_cannot_expand_permissions_or_vision(self):
        scope = metadata("g1")["scope"]
        self.cfg["sessions"]["overrides"] = {scope: {"reply": {"vision_strategy": "full"}}}
        self.assertEqual(effective_config(validate(self.cfg), scope)["reply"]["vision_strategy"], "economy")
        self.cfg["sessions"]["overrides"] = {scope: {"access": {"allow_all_groups": True}}}
        with self.assertRaises(ConfigError):
            validate(self.cfg)
        self.cfg["sessions"]["overrides"] = {}
        self.cfg["media"]["api_origin"] = "https://dashscope.aliyuncs.com.evil.example"
        with self.assertRaises(ConfigError):
            validate(self.cfg)
        self.assertEqual(media_format(sound()), "audio/wav")
        self.assertEqual(wav_seconds(sound()), 1)
