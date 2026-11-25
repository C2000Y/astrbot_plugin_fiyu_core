"""Regression cases for model-directed QQ bubbles, length limits and old JSON."""
import importlib.util
import json
import random
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock, patch

from astrbot_plugin_fiyu_core.core.config import ConfigError, ConfigStore, defaults, validate
from astrbot_plugin_fiyu_core.core.telemetry import BalanceCheckError, ModelFailure
from astrbot_plugin_fiyu_core.core.util import limit_reply, plain_reply_text, split_reply


class ReplyFormatTests(unittest.TestCase):
    def test_short_model_breaks_and_literal_characters(self):
        for separator in ("\n", "\n\n", "\n  \n", "\r\n", "\r\n\r\n", "\r"):
            self.assertEqual(split_reply("甲"+separator+"乙", 300, 5), ["甲", "乙"])
        for single in ("原神，启动", r"甲\n乙", "一个比较长但仍然是一行的回复。" * 7):
            self.assertEqual(split_reply(single, 300, 5), [single])
        self.assertEqual(split_reply("甲\n乙", 300, 5, on_newline=False), ["甲\n乙"])
        self.assertEqual(split_reply("甲\n乙", 300, 1), ["甲\n乙"])
        self.assertEqual(split_reply(" \r\n\n", 300, 5), [])

    def test_rank_table_heading_and_quote_boundaries(self):
        rank = "今天的胜率\n8、甲 53.62%\n9、乙 53.48%\n10、丙 53.21%"
        table = "英雄 | 胜率\n:---|---:\n力丸 | 56.40%\n幽鬼 | 53.21%"
        self.assertEqual(split_reply(rank, 300, 5), [rank])
        self.assertEqual(split_reply("1、甲\n\n2、乙\n就这些", 300, 5), ["1、甲\n\n2、乙", "就这些"])
        self.assertEqual(split_reply(table, 12, 8), [table])
        for block in ("### 数据范围\n仅当日公开样本", "> 原文第一行\n> 原文第二行", rank, table):
            self.assertEqual(split_reply("查到了\n\n" + block + "\n\n就这些", 300, 5),
                             ["查到了", block, "就这些"])

    def test_prose_and_code_can_coexist_in_one_answer(self):
        for code in ("```python\n    print(1)\n\n```", "~~~~\n```\n  1. 保留\n~~~~",
                     "- ```js\n  let a = 1;\n  ```", "> ~~~\n> 引用内代码\n> ~~~"):
            self.assertEqual(split_reply("前面\n" + code + "\n后面", 10, 5),
                             ["前面", code, "后面"])
        self.assertEqual(split_reply("开头\n```\n没有闭合\n8. 原样", 9, 5),
                         ["开头", "```\n没有闭合\n8. 原样"])
        self.assertEqual(split_reply("    def a():\n        return 1", 6, 5),
                         ["    def a():\n        return 1"])

    def test_plain_length_and_bubble_cap(self):
        self.assertEqual([len(s) for s in split_reply("甲" * 1800, 150, 5)], [360] * 5)
        self.assertEqual([len(s) for s in split_reply("甲" * 160, 150, 5)], [80, 80])
        lines = [f"第{i:02d}句" for i in range(1, 13)]
        pieces = split_reply("\n".join(lines), 300, 5)
        self.assertEqual("\n".join(pieces).splitlines(), lines)
        self.assertEqual(len(pieces), 5)
        self.assertLessEqual(max(len(s.splitlines()) for s in pieces), 3)
        mixed = "第一句\n" + "甲" * 280 + "\n最后一句"
        parts = split_reply(mixed, 40, 4)
        self.assertEqual((parts[0], parts[-1]), ("第一句", "最后一句"))
        self.assertEqual("".join(parts[1:-1]), "甲" * 280)
        uneven = "一\n二\n三\n四\n五\n六\n" + "甲" * 1650
        parts = split_reply(uneven, 300, 5)
        self.assertLess(max(map(len, parts)), 500)
        self.assertEqual("".join(parts).replace("\n", ""), uneven.replace("\n", ""))

    def test_urls_and_inline_code_survive_fallback(self):
        for literal in ("https://example.test/"+ "a"*60, "`" + "abc def " * 9 + "`"):
            parts = split_reply("这是 " + literal + " 再说下一件事", 20, 8)
            self.assertTrue(any(literal in part for part in parts), parts)

    def test_truncation_at_total_limit(self):
        for fence in ("```", "~~~~"):
            txt = "看这里\n"+ fence + "\n" + "a" * 2000 + "\n" + fence
            limited = limit_reply(txt, 160)
            self.assertLessEqual(len(limited), 160)
            self.assertTrue(limited.endswith("\n"+fence+"\n…（可继续问）"))
            self.assertEqual(len(split_reply(limited, 40, 5)), 3)
        regular = "甲" * 180 + "\n结尾"
        self.assertEqual(len(limit_reply(regular, 160)), 160)
        self.assertEqual(limit_reply(regular, 300), regular)
        self.assertEqual(plain_reply_text("甲\r\n<@123>\n乙[CQ:at,qq=23]"), "甲\n\n乙")

    def test_generated_inputs_do_not_drop_reorder_or_duplicate_words(self):
        rand = random.Random(42)
        for _ in range(180):
            rows = ["段" + str(i) + "字" * rand.randint(0, 100) + "。" for i in range(rand.randint(1, 24))]
            body = "\n\n".join(rows)
            cap = rand.randint(1, 8)
            parts = split_reply(body, rand.randint(40, 500), cap)
            self.assertLessEqual(len(parts), cap)
            self.assertTrue(all(p.strip() for p in parts))
            self.assertEqual("".join(parts).replace("\n", ""), "".join(rows))


class ReplyConfigTests(unittest.TestCase):
    def test_import_and_reload_preserve_custom_values(self):
        self.assertEqual(defaults()["reply"]["split_chars"], 300)
        for old, expected in ((150, 300), (200, 200), (500, 500)):
            raw = {"reply": {"split_chars": old, "split_enabled": False}}
            with TemporaryDirectory() as d:
                path = Path(d) / "config.json"
                original = json.dumps(raw).encode()
                path.write_bytes(original)
                store = ConfigStore(Path(d))
                draft = store.snapshot()
                self.assertEqual(path.read_bytes(), original)
                self.assertFalse(draft["reply"]["split_enabled"])
                self.assertEqual(draft["reply"]["split_chars"], expected)
                draft["reply"].update(split_chars=150, split_on_newline=False)
                store.save(draft, store.revision())
                self.assertEqual(ConfigStore(Path(d)).snapshot()["reply"], draft["reply"])
        with self.assertRaises(ConfigError):
            validate({"reply": {"split_on_newline": "false"}})


class AdapterBoundaryTests(unittest.IsolatedAsyncioTestCase):
    """Exercise the shipped host methods with inert SDK types and an offline HTTP response."""
    @classmethod
    def setUpClass(cls):
        imports = ("astrbot.api.event", "astrbot.api.message_components", "astrbot.core.agent.message",
                   "astrbot.core.agent.tool", "astrbot.core.provider.entities")
        sdk = {name: Mock() for name in imports}
        connection_error = type("APIConnectionError", (RuntimeError,), {})
        sdk["astrbot.core.exceptions"] = SimpleNamespace(
            EmptyModelOutputError=type("EmptyModelOutputError", (RuntimeError,), {}),
            ProviderNotFoundError=type("ProviderNotFoundError", (RuntimeError,), {}),
        )
        sdk["openai"] = SimpleNamespace(
            APIConnectionError=connection_error,
            APITimeoutError=type("APITimeoutError", (connection_error,), {}),
        )
        path = Path(__file__).resolve().parents[1] / "host.py"
        spec = importlib.util.spec_from_file_location("astrbot_plugin_fiyu_core.offline_host", path)
        cls.module = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, sdk):
            spec.loader.exec_module(cls.module)

    async def test_sdk_failure_types_remain_distinguishable(self):
        context = SimpleNamespace(llm_generate=AsyncMock(), get_current_chat_provider_id=AsyncMock())
        host = self.module.AstrHost(context)
        context.get_current_chat_provider_id.side_effect = self.module.ProviderNotFoundError("private")
        with self.assertRaises(ModelFailure) as missing:
            await host.chat_provider("fixture-session")
        self.assertEqual(missing.exception.category, "provider_unavailable")
        for exception_type, expected in (
            (self.module.ProviderNotFoundError, "provider_unavailable"),
            (self.module.APIConnectionError, "provider_unavailable"),
            (self.module.APITimeoutError, "timeout"),
            (self.module.EmptyModelOutputError, "invalid_response"),
        ):
            with self.subTest(exception_type=exception_type):
                context.llm_generate.side_effect = exception_type("private_failure_details")
                with self.assertRaises(ModelFailure) as error:
                    await host.generate("fixture-provider", "prompt", "system")
                self.assertEqual(error.exception.category, expected)
                self.assertNotIn("private_failure_details", str(error.exception))
                self.assertEqual(context.llm_generate.await_args.kwargs["request_max_retries"], 1)

    async def test_each_bubble_submits_its_own_plain_message_chain(self):
        class Chain:
            def message(self, text):
                self.text = text
                return self
        event = SimpleNamespace(send=AsyncMock())
        with patch.object(self.module, "MessageChain", Chain):
            for part in split_reply("第一条\n第二条", 300, 5):
                self.assertTrue(await self.module.AstrHost.send(event, part))
            self.assertFalse(await self.module.AstrHost.send(event, "<@123>"))
        chains = [call.args[0] for call in event.send.await_args_list]
        self.assertIsNot(chains[0], chains[1])
        self.assertEqual([chain.text for chain in chains], ["第一条", "第二条"])

    async def test_native_tool_history_omits_only_contaminated_assistant_text(self):
        calls = [{"id": "native-call", "type": "function", "function": {"name": "query"}}]
        response = SimpleNamespace(
            completion_text="<||DSML||calls>",
            reasoning_content="native reasoning retained",
            reasoning_signature="native signature",
            to_openai_tool_calls_model=lambda: calls,
        )
        with patch.object(self.module, "ThinkPart", SimpleNamespace), \
                patch.object(self.module, "TextPart", SimpleNamespace), \
                patch.object(self.module, "AssistantMessageSegment", SimpleNamespace), \
                patch.object(self.module, "ToolCallMessageSegment", SimpleNamespace), \
                patch.object(self.module, "ToolCallsResult", SimpleNamespace):
            result = self.module.AstrHost.tool_result(response, [("native-call", "真实测试结果")])
            info = result.tool_calls_info
            self.assertEqual(info.tool_calls, calls)
            self.assertEqual(len(info.content), 1)
            self.assertEqual(info.content[0].think, "native reasoning retained")
            self.assertEqual(info.content[0].encrypted, "native signature")
            self.assertEqual(result.tool_calls_result[0].content[0].text, "真实测试结果")
            response.completion_text = "普通的查询说明"
            ordinary = self.module.AstrHost.tool_result(response, [("native-call", "结果")])
            self.assertEqual(ordinary.tool_calls_info.content[1].text, "普通的查询说明")

    async def test_balance_failure_categories_and_normal_response(self):
        provider = Mock()
        provider.provider_config = {"api_base": "https://api.deepseek.com"}
        host = self.module.AstrHost(SimpleNamespace(get_provider_by_id=lambda pid: provider))
        provider.get_keys.return_value = []
        with self.assertRaisesRegex(BalanceCheckError, "缺少可用密钥"):
            await host.balance("test", "CNY")
        provider.get_keys.return_value = ["dummy"]
        with patch.object(self.module.aiohttp, "ClientSession") as session:
            client = MagicMock()
            session.return_value.__aenter__.return_value = client
            response = client.get.return_value.__aenter__.return_value
            response.status = 401
            with self.assertRaisesRegex(BalanceCheckError, "401"):
                await host.balance("test", "CNY")
            response.status = 200
            response.json = AsyncMock(return_value={"balance_infos": [{"currency":"USD", "total_balance":"5.4"}]})
            with self.assertRaisesRegex(BalanceCheckError, "所选币种"):
                await host.balance("test", "CNY")
            self.assertEqual((await host.balance("test", "USD"))["amount"], 5.4)
            response.json.return_value = {"balance_infos":[{"currency":"CNY","total_balance":"sensitive detail"}]}
            with self.assertRaisesRegex(BalanceCheckError, "^余额响应格式或数值无效$"):
                await host.balance("test", "CNY")
