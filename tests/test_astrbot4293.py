"""Offline host integration smoke tests for the real AstrBot 4.29.x SDK."""

import json
import wave
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

try:
    from astrbot.api.message_components import At, AtAll, Image, Plain, Poke, Record, Reply, Video
    from astrbot.api.star import StarTools
    from astrbot.core.platform.astr_message_event import AstrMessageEvent
    from astrbot.core.platform.astrbot_message import AstrBotMessage, MessageMember
    from astrbot.core.platform.message_type import MessageType
    from astrbot.core.platform.platform_metadata import PlatformMetadata
    from astrbot.core.provider.entities import LLMResponse, TokenUsage
    from astrbot.core.star.star import StarMetadata
    from astrbot.core.star.star_handler import EventType, star_handlers_registry
    from astrbot_plugin_fiyu_core.host import AstrHost
    from astrbot_plugin_fiyu_core.main import FiyuCore

    HAS_ASTRBOT = True
except ImportError:
    HAS_ASTRBOT = False


@unittest.skipUnless(HAS_ASTRBOT, "requires real AstrBot SDK 4.29.x")
class NativeTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def event(components, platform="qq_official", typ=None, raw=None):
        m = AstrBotMessage()
        m.type = typ or MessageType.GROUP_MESSAGE
        m.self_id = "bot-test"
        m.session_id = "group-test"
        m.group_id = "group-test"
        m.message_id = "msg-test"
        m.sender = MessageMember("alice", "Alice")
        m.message = components
        m.message_str = "".join(x.text for x in components if isinstance(x, Plain))
        m.raw_message = raw or {}
        return AstrMessageEvent(
            m.message_str,
            m,
            PlatformMetadata(name=platform, description="test", id="adapter-test"),
            m.session_id,
        )

    async def test_real_event_components_and_handlers(self):
        host = AstrHost(None)
        ordinary = self.event([Plain("你好")])
        self.assertFalse(host.info(ordinary)["directed"])
        at = self.event([At(qq="bot-test"), Plain("你好")])
        self.assertTrue(host.info(at)["directed"])
        atall = self.event([AtAll(), Plain("群友们")])
        atall.is_at_or_wake_command = True
        self.assertFalse(host.info(atall)["directed"])
        quote = self.event(
            [
                Reply(id="123", sender_id="bot-test", message_str="来自肥鱼的消息"),
                Plain("真的吗"),
            ]
        )
        self.assertEqual(host.info(quote)["direct_reason"], "quote")
        unverified = self.event(
            [Reply(id="123", message_str="来自肥鱼的消息"), Plain("真的吗")]
        )
        self.assertEqual(host.info(unverified)["unverified_quotes"], ["来自肥鱼的消息"])
        links = self.event([At(qq="other-user"), At(qq="bot-test"), AtAll(), Reply(id="9", sender_id="other-user", message_str="旧消息"), Plain("看看")])
        self.assertEqual(host.info(links)["mentioned_uids"], ["other-user"])
        self.assertEqual(host.info(links)["quoted_uids"], ["other-user"])
        poke = self.event(
            [Poke(id="bot-test")],
            platform="aiocqhttp",
            typ=MessageType.OTHER_MESSAGE,
            raw={
                "post_type": "notice",
                "notice_type": "notify",
                "sub_type": "poke",
                "user_id": "alice",
                "target_id": "bot-test",
                "self_id": "bot-test",
                "group_id": "group-test",
            },
        )
        self.assertTrue(host.info(poke)["poke"])
        host.consume(at)
        self.assertTrue(at.is_stopped())
        self.assertTrue(at.call_llm)
        handlers = [
            h
            for h in star_handlers_registry.get_handlers_by_event_type(
                EventType.AdapterMessageEvent
            )
            if h.handler_module_path == FiyuCore.__module__
        ]
        self.assertTrue(
            any(
                h.handler_name == "receive" and h.extras_configs.get("priority") == 90
                for h in handlers
            )
        )

    async def test_actual_function_call_message_objects(self):
        calls = []

        class C:
            async def llm_generate(self, **kwargs):
                calls.append(kwargs)
                return LLMResponse(
                    role="tool",
                    tools_call_name=["query"],
                    tools_call_args=[{"hero": "紫猫"}],
                    tools_call_ids=["abc"],
                    reasoning_content="native reasoning",
                    usage=TokenUsage(12, 3, 5),
                )

        host = AstrHost(C())
        spec = {
            "name": "query",
            "description": "test",
            "parameters": {
                "type": "object",
                "properties": {"hero": {"type": "string"}},
                "required": ["hero"],
            },
        }
        res = await host.generate("provider", "question", "system", tools=[spec])
        self.assertEqual(res.usage.total, 20)
        actual = calls[0]["tools"].get_func_desc_openai_style()
        self.assertEqual(actual[0]["function"]["name"], "query")
        tcr = host.tool_result(res, [("abc", '{"answer":"test"}')])
        messages = tcr.to_openai_messages()
        self.assertEqual(messages[0]["role"], "assistant")
        self.assertEqual(messages[0]["tool_calls"][0]["id"], "abc")
        self.assertIn("native reasoning", str(messages[0]))
        self.assertEqual(messages[1]["tool_call_id"], "abc")
        await host.generate("provider", "question", "system", tool_results=[tcr])
        self.assertIs(calls[1]["tool_calls_result"][0], tcr)

    async def test_native_media_components_and_outgoing_chain(self):
        from PIL import Image as PillowImage

        host = AstrHost(None)
        with TemporaryDirectory() as path:
            audio = Path(path) / "audio.wav"
            with wave.open(str(audio), "wb") as stream:
                stream.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
                stream.writeframes(b"\0\0" * 160)
            video = Path(path) / "video.mp4"
            video.write_bytes(b"local media path fixture")
            image = Path(path) / "image.png"
            PillowImage.new("RGB", (1, 1)).save(image)
            record, movie = Record.fromFileSystem(audio), Video.fromFileSystem(video)
            event = self.event([record, movie], platform="aiocqhttp")
            self.assertEqual(host.info(event)["media"], [("audio", record), ("video", movie)])
            self.assertTrue(Path(await host.media_file(record)).is_file())
            self.assertEqual(Path(await host.media_file(movie)), video)
            event.send = AsyncMock()
            self.assertTrue(await host.send_media(event, "audio", audio))
            self.assertIsInstance(event.send.await_args.args[0].chain[0], Record)
            self.assertTrue(await host.send_media(event, "image", image))
            self.assertIsInstance(event.send.await_args.args[0].chain[0], Image)

    async def test_start_dashboard_routes_and_terminate(self):
        class C:
            def __init__(self):
                self.routes = {}

            def register_web_api(self, path, fn, methods, description):
                self.routes[path] = (fn, methods)

            def get_all_providers(self):
                return []

            def get_all_embedding_providers(self):
                return []

        c = C()
        plugin = FiyuCore(c)
        with TemporaryDirectory() as d:
            with patch.object(StarTools, "get_data_dir", return_value=Path(d)):
                await plugin.initialize()
                try:
                    self.assertTrue(plugin.engine.running)
                    for route, method in (
                        ("overview", "GET"), ("config", "GET"), ("config/save", "POST"),
                        ("usage", "GET"), ("action", "POST"), ("personas/merge_options", "GET"),
                        ("personas/merge_preview", "POST"), ("personas/merge_apply", "POST"),
                    ):
                        handler, methods = c.routes[f"/astrbot_plugin_fiyu_core/{route}"]
                        self.assertTrue(callable(handler))
                        self.assertEqual(methods, [method])
                    overview = json.loads((await plugin.api_overview()).body)
                    self.assertEqual(overview["mode"], "off")
                    self.assertEqual(overview["embedding"]["state"], "disabled")
                    value = json.loads((await plugin.api_config()).body)
                    self.assertTrue(value["schema"])
                    self.assertEqual(value["config"]["logs"]["retention_days"], 15)
                finally:
                    await plugin.terminate()
                self.assertFalse(plugin.engine.running)

    async def test_native_views_discovery(self):
        from astrbot.dashboard.services.plugin_page_service import PluginPageService

        plugin_root = Path(__file__).resolve().parents[1]
        manager = SimpleNamespace(plugin_store_path=str(plugin_root.parent))
        service = PluginPageService(manager)
        metadata = StarMetadata(name=plugin_root.name, root_dir_name=plugin_root.name)
        pages = await service.discover_plugin_pages(metadata)
        self.assertEqual([p.name for p in pages], ["dashboard"])


if __name__ == "__main__":
    unittest.main()
