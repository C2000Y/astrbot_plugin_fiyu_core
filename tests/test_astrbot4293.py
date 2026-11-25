"""Host integration smoke tests, run when real AstrBot 4.29.3 is available."""

import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

try:
    from astrbot.api.message_components import At, AtAll, Plain, Poke, Reply
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


@unittest.skipUnless(HAS_ASTRBOT, "requires real AstrBot SDK 4.29.3")
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
                self.assertTrue(plugin.engine.running)
                self.assertEqual(len(c.routes), 24)
                r = await plugin.api_overview()
                self.assertEqual(json.loads(r.body)["mode"], "off")
                value = json.loads((await plugin.api_config()).body)
                self.assertTrue(value["schema"])
                self.assertEqual(value["config"]["logs"]["retention_days"], 15)
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
