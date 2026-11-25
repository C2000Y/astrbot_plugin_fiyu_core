"""Offline deterministic tests: no credentials, QQ sends or live model requests."""

from __future__ import annotations

import asyncio
import json
import logging
from collections import deque
from datetime import date, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from astrbot_plugin_fiyu_core.core.config import (
    ConfigError,
    ConfigStore,
    defaults,
    validate,
)
from astrbot_plugin_fiyu_core.core.dota import (
    AliasIndex,
    DotaService,
    HERO_LIST_URL,
    PUBLIC_STATS_URL,
    StatsSnapshot,
    STATS_URL,
    WEEKLY_STATS_URL,
    WinRateSnapshot,
    fit_stats_payload,
)
from astrbot_plugin_fiyu_core.core.engine import Engine, FINAL_REPLY_RULES, TOOL_PROTOCOL_FALLBACK, TOOL_RETRY_RULES
from astrbot_plugin_fiyu_core.core.memory import (
    normalize_proposals,
    manual_proposal,
    learning_transcript,
)
from astrbot_plugin_fiyu_core.core.store import Store, rank_memories
from astrbot_plugin_fiyu_core.core.telemetry import BudgetExceeded, DailyLog
from astrbot_plugin_fiyu_core.core.util import (
    atomic_json,
    contains_dsml,
    day_key,
    now,
    qq_literal_ordered_lists,
    scope_key,
    split_reply,
)


LOGGER = logging.getLogger("fiyu.test")
LOGGER.addHandler(logging.NullHandler())

RAW_DSML = (
    '<｜｜DSML｜｜calls>\n'
    '<｜｜DSML｜｜invoke name="search_web">\n'
    '<｜｜DSML｜｜parameter name="query" string="true">未执行的测试查询</｜｜DSML｜｜parameter>\n'
    '</｜｜DSML｜｜invoke>\n'
    '</｜｜DSML｜｜calls>'
)


def tool_response(prefix, *calls, content=""):
    return {
        "completion_text": content,
        "tools_call_name": [name for name, _ in calls],
        "tools_call_args": [args for _, args in calls],
        "tools_call_ids": [f"{prefix}-{i}" for i in range(len(calls))],
    }


def metadata(group="g1", kind="group", persona="fish", uid="alice"):
    return {
        "scope": scope_key("qq-test", kind, group, persona),
        "platform": "qq_official",
        "platform_id": "qq-test",
        "kind": kind,
        "location": group,
        "persona": persona,
        "umo": f"qq-test:{'GroupMessage' if kind == 'group' else 'FriendMessage'}:{group}",
        "uid": uid,
    }


def add_message(store, meta, uid, text, ext_id, **kwargs):
    return store.ingest(
        meta,
        {
            "uid": uid,
            "name": uid,
            "text": text,
            "ext_id": ext_id,
            "ts": now(),
            **kwargs,
        },
    )


def proposal(
    row,
    scope,
    *,
    kind="core",
    key="生日",
    full="Alice 的生日是 7 月 22 日",
    gist="Alice 的生日在 7 月",
    trace="Alice 的生日",
    confirmed=False,
    importance=90,
):
    return {
        "subject": row["ref"],
        "kind": kind,
        "key": key,
        "title": "生日" if kind == "core" else key,
        "full": full,
        "gist": gist,
        "trace": trace,
        "importance": importance,
        "confidence": 0.97,
        "source_ids": [row["seq"]],
        "confirmed_update": confirmed,
        "valid_until": None,
    }


def fixture(day="2026-11-20", shift=0):
    """Same upper-triangle, placeholder and rank format as the existing PowerShell script."""
    ids = [x for x in range(1, 102) if x != 17] + [126]
    heroes = [
        {
            "id": i,
            "name": f"npc_dota_hero_{'antimage' if i == 1 else 'axe' if i == 2 else 'void_spirit' if i == 126 else 'fixture_' + str(i)}",
            "name_loc": {1: "敌法师", 2: "斧王", 126: "虚无之灵"}.get(
                i, f"测试英雄{i}"
            ),
            "name_english_loc": {1: "Anti-Mage", 2: "Axe", 126: "Void Spirit"}.get(
                i, f"Fixture {i}"
            ),
        }
        for i in ids
    ]
    data = []
    for rank in range(7):
        rows = []
        for i in ids:
            enemy = [0] * (126 - i)
            ally = [0] * (126 - i)
            for j in ids:
                if j > i:
                    enemy[j - i - 1] = 4200 if i == 1 and j == 126 else 5000
                    ally[j - i - 1] = 6000 if i == 1 and j == 126 else 5100
            if i == 1:
                enemy[2] = 0
                ally[2] = 0  # pair 1/4 missing, not 0% and not 100%
            rows.append(
                {
                    "hero_id": i,
                    "win_rate": (5900 + shift if i == 126 else 4000 + i) + rank * 5,
                    "first_other_hero_id": i + 1,
                    "enemy_win_rate": enemy,
                    "ally_win_rate": ally,
                }
            )
        data.append({"rank": rank, "hero_data": rows})
    return {
        "schema": 1,
        "source": STATS_URL,
        "observed_date": day,
        "observed_at_china": day + " 03:00:00",
        "heroes": heroes,
        "ranked_hero_data": data,
    }


def public_fixture(day="2026-11-20", shift=0, heroes=None):
    """Counts are balanced by picks; the public leader differs from the old matchup baseline."""
    heroes = heroes or fixture()["heroes"]
    rows = []
    for h in heroes:
        hid = h["id"]
        picks = 3000 if hid == 1 else 1000
        adjustment = -(60 + shift) if hid == 1 else 60 + shift if hid == 2 else 0
        row = {"id": hid, "name": h["name"], "localized_name": h["name_english_loc"], "pub_pick": picks, "pub_win": picks // 2 + adjustment}
        for rank in range(1, 8):
            shift_rank = -(70 + rank) if hid == 1 else 70 + rank if hid == 126 else 0
            row[f"{rank}_pick"] = picks * rank
            row[f"{rank}_win"] = (picks // 2 + shift_rank) * rank
        row.update({"8_pick": 0, "8_win": 0})
        rows.append(row)
    return {
        "schema": 1,
        "source": PUBLIC_STATS_URL,
        "observed_date": day,
        "observed_at_china": day + "T03:00:00+08:00",
        "heroes": heroes,
        "hero_stats": rows,
    }


def weekly_fixture(day="2026-11-20", shift=0.0, heroes=None):
    heroes = heroes or fixture()["heroes"]
    rows = []
    for h in heroes:
        hid = h["id"]
        sign = 1 if hid == 126 else -1 if hid == 1 else 0
        chunks = [
            {
                "rank_chunk": rank,
                "week_data": [
                    {
                        "week": week,
                        "win_percent": 0.5 + sign * (0.05 + rank * 0.001 + (8 - week) * 0.01 + shift),
                        "pick_percent": 10 / len(heroes),
                        "ban_percent": 0.025,
                    }
                    for week in range(8, 0, -1)
                ],
            }
            for rank in range(7)
        ]
        rows.append({"hero_id": hid, "hero_data_per_chunk": chunks})
    return {
        "schema": 1,
        "source": WEEKLY_STATS_URL,
        "observed_date": day,
        "observed_at_china": day + "T03:00:00+08:00",
        "heroes": heroes,
        "hero_stats": rows,
    }


class FakeHost:
    def __init__(self):
        self.calls = []
        self.responses = deque()
        self.sent = []
        self.kb_calls = []
        self.web_calls = []
        self.notify_calls = []

    async def chat_provider(self, umo):
        return "chat"

    def info(self, event):
        return dict(event.info)

    def is_command(self, event, info, prefixes):
        return any(info["original"].startswith(x) for x in prefixes)

    def ai_enabled(self, event):
        return True

    def consume(self, event):
        event.consumed = True

    async def persona(self, event, cfg):
        return {"id": event.persona, "system": "你是肥鱼。", "examples": []}

    async def generate(self, provider, prompt, system, **kwargs):
        self.calls.append(
            {"provider": provider, "prompt": prompt, "system": system, **kwargs}
        )
        if not self.responses:
            raise AssertionError("unexpected model request")
        value = self.responses.popleft()
        if callable(value):
            value = value(prompt, system, kwargs)
        if isinstance(value, Exception):
            raise value
        if isinstance(value, str):
            value = {"completion_text": value}
        return SimpleNamespace(
            role=value.get("role", "assistant"),
            completion_text=value.get("completion_text", ""),
            tools_call_name=value.get("tools_call_name", []),
            tools_call_args=value.get("tools_call_args", []),
            tools_call_ids=value.get("tools_call_ids", []),
            usage=SimpleNamespace(input_other=100, input_cached=20, output=20),
        )

    def tool_result(self, resp, results):
        return {"names": resp.tools_call_name, "results": results}

    async def send(self, event, text):
        self.sent.append((event, text))
        return True

    def embedding_fingerprint(self, pid):
        return "fp" if pid else ""

    async def embed(self, pid, texts):
        return [[1.0, 0.0] for _ in texts]

    async def kb(self, name, query, k):
        self.kb_calls.append((name, query, k))
        return {"results": [{"source": "test.pdf", "content": "SAP测试文档"}]}

    async def web_search(self, event, query):
        self.web_calls.append(query)
        return "公开测试资料"

    async def balance(self, pid, currency):
        return {"amount": 2.1, "currency": currency, "available": True}

    async def notify(self, umo, text, mention):
        self.notify_calls.append((umo, text, mention))
        return True


def event(
    ext_id,
    text,
    uid="alice",
    location="g1",
    direct=False,
    kind="group",
    persona="fish",
    poke=False,
    quotes=(),
):
    return SimpleNamespace(
        consumed=False,
        persona=persona,
        info={
            **metadata(location, kind, persona, uid),
            "group": location if kind == "group" else "",
            "name": uid,
            "text": text,
            "original": text,
            "directed": direct or kind == "private",
            "direct_reason": "at"
            if direct
            else "private"
            if kind == "private"
            else "wake",
            "self_message": False,
            "ext_id": ext_id,
            "poke": poke,
            "other_poke": False,
            "at_others": False,
            "images": [],
            "unverified_quotes": list(quotes),
        },
    )


class ToolProtocolTests(unittest.TestCase):
    def test_dsml_control_markers_and_normal_technical_text(self):
        for value in (
            RAW_DSML, "<||DSML||tool_calls>", "<|DSML|function_calls>",
            "</ | | DSML | | invoke>", "<｜DSML｜parameter>",
            "查询中\n<||DSML", "＜｜｜dsml｜｜calls＞",
        ):
            with self.subTest(value=value):
                self.assertTrue(contains_dsml(value))
        for value in (
            "DSML 是一种工具调用协议。", '<tool_call>普通 XML 示例</tool_call>',
            '{"tool_calls": [{"name": "example"}]}', "| DSML | calls |",
            "正常聊天\n第二条", "if a < b: print(a)",
        ):
            with self.subTest(value=value):
                self.assertFalse(contains_dsml(value))


class QQOrderedListTests(unittest.TestCase):
    def test_explicit_numbers_are_never_recalculated(self):
        original = "8. 测试甲\r\n9) 测试乙\r\n10. 测试丙\n\n1. 另一组\n5. 保留跳号\n007. 保留前导零\n7、已有格式"
        expected = "8、测试甲\r\n9、测试乙\r\n10、测试丙\n\n1、另一组\n5、保留跳号\n007、保留前导零\n7、已有格式"
        self.assertEqual(qq_literal_ordered_lists(original), expected)
        self.assertEqual(qq_literal_ordered_lists(expected), expected)

    def test_only_plain_line_start_markers_are_changed(self):
        original = (
            "56.40%\n3.14159\n7.41f\n192.168.1.10\nv0.2.1\n2026.11.23\n"
            "胜率 55.27%，参见第 8. 项。\n"
            "6.表面上不是 Markdown 列表\n"
            "    8. 缩进代码\n\t9. 缩进代码\n"
            "> 10. 引用正文\n11\\. 转义序号\n"
            "`12. 行内代码`\n"
        )
        self.assertEqual(qq_literal_ordered_lists(original), original)
        self.assertEqual(
            qq_literal_ordered_lists("  8.  `version 0.2.2`\n9.\t55.27%"),
            "  8、`version 0.2.2`\n9、55.27%",
        )

    def test_fenced_code_keeps_exact_text(self):
        fenced = (
            "````text\r\n8. literal\r\n```\r\n9. literal\r\n"
            "~~~\r\n10) literal\r\n````\r\n"
            "~~~text\n3. literal\n```\n4) literal\n~~~~\n"
        )
        self.assertEqual(
            qq_literal_ordered_lists("7. 说明\r\n" + fenced + "11. 后续"),
            "7、说明\r\n" + fenced + "11、后续",
        )
        self.assertEqual(
            qq_literal_ordered_lists("7. 说明\n```text\n8. literal"),
            "7、说明\n```text\n8. literal",
        )

    def test_complex_list_code_is_left_alone(self):
        for prefix in ("8. ", "- "):
            original = "7. 说明\n" + prefix + "```text\n  9. literal\n  ```\n10. 后续"
            with self.subTest(prefix=prefix):
                self.assertEqual(qq_literal_ordered_lists(original), original)


class ConfigTests(unittest.TestCase):
    def test_config_save_validation_and_restore(self):
        with TemporaryDirectory() as d:
            cfg = ConfigStore(Path(d))
            initial = cfg.info()
            self.assertEqual(initial["config"]["access"]["mode"], "off")
            raw = {
                "access": {"mode": "active", "allowed_groups": ["g1", "g1"]},
                "memory": {"core_blur_after_days": 60},
            }
            saved = cfg.save(raw, initial["revision"])
            self.assertEqual(saved["config"]["access"]["allowed_groups"], ["g1"])
            self.assertEqual(saved["config"]["logs"]["retention_days"], 15)
            self.assertNotEqual(initial["revision"], saved["revision"])
            self.assertEqual(len(saved["backups"]), 1)
            with self.assertRaises(ConfigError):
                cfg.save({}, initial["revision"])
            with self.assertRaises(ConfigError):
                cfg.save({"access": {"allow_all_groups": 1}}, saved["revision"])
            with self.assertRaises(ConfigError):
                validate({"memory": {"experience_clear_days": 100}})
            with self.assertRaises(ConfigError):
                validate({"persona": {"unknown": False}})
            restored = cfg.restore(saved["backups"][0], saved["revision"])
            self.assertEqual(restored["config"]["access"]["mode"], "off")

    def test_corrupt_config_starts_safe(self):
        with TemporaryDirectory() as d:
            path = Path(d) / "config.json"
            path.write_text("{broken", encoding="utf-8")
            cfg = ConfigStore(Path(d))
            self.assertTrue(cfg.error)
            self.assertEqual(cfg.snapshot()["access"]["mode"], "off")
            self.assertEqual(path.read_text(), "{broken")
            fixed = cfg.save(defaults(), cfg.revision())
            self.assertFalse(fixed["error"])
            self.assertEqual(
                (Path(d) / "config_backups" / fixed["backups"][0]).read_text(),
                "{broken",
            )

    def test_logs_15_days(self):
        with TemporaryDirectory() as d:
            p = Path(d)
            cfg = ConfigStore(p)
            log = DailyLog(p / "logs", cfg, LOGGER)
            for name in (
                "fiyu-2026-11-08.jsonl",
                "fiyu-2026-11-08-1.jsonl",
                "fiyu-2026-11-09.jsonl",
                "other-2026-11-01.jsonl",
            ):
                (log.path / name).write_text("", encoding="utf-8")
            self.assertEqual(log.clean_old(15, "2026-11-23"), 2)
            self.assertTrue((log.path / "fiyu-2026-11-09.jsonl").is_file())
            self.assertTrue((log.path / "other-2026-11-01.jsonl").is_file())
            log.emit("TOKEN", "test", input=5, reason="password: secret_value")
            items = log.list()["items"]
            self.assertEqual(items[0]["input"], 5)
            self.assertNotIn("secret_value", items[0]["reason"])


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "db.sqlite3")
        self.cfg = defaults()
        self.meta = metadata()
        self.scope = self.meta["scope"]
        self.a = add_message(self.store, self.meta, "alice", "我的生日是7月22日", "m1")
        self.b = add_message(self.store, self.meta, "bob", "我叫Bob", "m2")

    def tearDown(self):
        self.tmp.cleanup()

    def put(self, items, rows=None):
        ps, _ = normalize_proposals(
            {"memories": items},
            self.scope,
            rows or [self.a, self.b],
            self.cfg["memory"],
        )
        return self.store.commit_memory(ps, [], [])

    def test_bot_history_and_its_counter_commit_together(self):
        from unittest.mock import patch
        record = dict(ext_id="test-submit-1", uid="BOT", name="肥鱼", text="第二条", ts=now(), bot=True)
        with patch.object(self.store, "_count_event", side_effect=OSError("synthetic")):
            with self.assertRaises(OSError):
                self.store.ingest(self.meta, record, count_submission=True)
        self.assertEqual(self.store.recent_bot_messages(self.scope), [])
        self.assertNotIn("sent_parts", self.store.totals()["today"])
        self.assertIsNotNone(self.store.ingest(self.meta, record, count_submission=True))
        self.assertIsNone(self.store.ingest(self.meta, record, count_submission=True))
        self.assertEqual(self.store.totals()["today"]["sent_parts"], 1)

    def test_dedup_and_scope_isolation(self):
        self.assertIsNone(add_message(self.store, self.meta, "alice", "重复", "m1"))
        other = metadata("g2")
        private = metadata("alice", kind="private")
        another_persona = metadata("g1", persona="other")
        for m in (other, private, another_persona):
            r = add_message(self.store, m, "alice", "别处消息", "m1")
            self.assertNotEqual(r["ref"], self.a["ref"])
        self.put([proposal(self.a, self.scope)])
        for m in (other, private, another_persona):
            self.assertEqual(
                self.store.recall_candidates(
                    m["scope"], None, None, True, True, None, None
                ),
                [],
            )
        self.assertEqual(len(self.store.context(self.scope, 10)), 2)

    def test_evidence_and_correction_history(self):
        p = proposal(self.a, self.scope)
        wrong = {**p, "source_ids": [self.b["seq"]]}
        nonexistent = {**p, "source_ids": [999999]}
        mixed = {**p, "source_ids": [self.a["seq"], self.b["seq"]]}
        self.assertEqual(self.put([wrong, nonexistent, mixed]), [])
        mid = self.put([p])[0]
        newer = add_message(
            self.store, self.meta, "alice", "生日刚才说错了，是8月9日", "m3"
        )
        correction = proposal(
            newer,
            self.scope,
            key="birthday",
            full="Alice 的生日是8月9日",
            gist="Alice 的生日在8月",
            confirmed=True,
        )
        self.assertEqual(self.put([correction], [newer]), [mid])
        self.assertEqual(len(self.store.memory_versions(mid)), 1)
        self.put(
            [{**p, "confirmed_update": True}], [self.a]
        )  # delayed batch cannot undo
        row = self.store.list_memories(self.scope)[0]
        self.assertIn("8月9日", row["full_text"])
        self.assertEqual(row["topic_key"], "birthday")
        hist = self.store.recall_candidates(
            self.scope, self.a["ref"], "core", True, True, None, None
        )
        self.assertTrue(
            any(
                x["state"] == "historical" and "7 月 22" in x["full_text"] for x in hist
            )
        )
        self.assertEqual(
            len(
                self.store.recall_candidates(
                    self.scope, self.b["ref"], None, True, True, None, None
                )
            ),
            0,
        )
        manual = manual_proposal({**row, "pinned": True}, self.cfg["memory"])
        self.store.manual_memory(manual, mid)
        n = add_message(self.store, self.meta, "alice", "新的生日是9月11日", "m4")
        ps, _ = normalize_proposals(
            {"memories": [proposal(n, self.scope, full="生日9月11日", confirmed=True)]},
            self.scope,
            [n],
            self.cfg["memory"],
        )
        self.assertFalse(self.store.remember_explicit(ps[0])["saved"])
        self.assertIn("8月9日", self.store.list_memories(self.scope)[0]["full_text"])

    def test_importance_survives_time_and_deep_recall(self):
        items = [
            proposal(self.a, self.scope),
            proposal(
                self.a,
                self.scope,
                kind="experience",
                key="群赛",
                full="11月群赛拿了冠军",
                gist="群赛冠军",
                trace="一起比赛",
                importance=95,
            ),
            proposal(
                self.a,
                self.scope,
                kind="ordinary",
                key="晚饭",
                full="周一吃火锅",
                gist="吃过火锅",
                trace="吃饭",
                importance=35,
            ),
            proposal(
                self.a,
                self.scope,
                kind="experience",
                key="普通赛",
                full="10月打了一次普通比赛",
                gist="打过比赛",
                trace="比赛",
                importance=40,
            ),
            proposal(
                self.a,
                self.scope,
                kind="temporary",
                key="约定",
                full="明天开黑",
                gist="开黑",
                trace="约定",
                importance=50,
            ),
        ]
        mids = self.put(items)
        self.assertEqual(len(mids), 5)
        self.store.maintenance(self.cfg, now() + 500 * 86400)
        rows = {r["title"]: r for r in self.store.list_memories(self.scope)}
        self.assertIn("7 月 22 日", rows["生日"]["visible_text"])
        self.assertEqual(rows["生日"]["importance"], 90)
        self.assertEqual(rows["群赛"]["state"], "active")
        self.assertEqual(rows["群赛"]["clarity"], "fuzzy")
        self.assertEqual(rows["群赛"]["visible_text"], "群赛冠军")
        self.assertEqual(rows["普通赛"]["state"], "archived")
        self.assertNotIn("晚饭", rows)
        self.assertEqual(rows["约定"]["state"], "expired")
        self.store.mark_recalled([rows["群赛"]["id"]])
        self.store.maintenance(self.cfg, now() + 1)
        now_rows = {r["title"]: r for r in self.store.list_memories(self.scope)}
        self.assertEqual(now_rows["群赛"]["clarity"], "fuzzy")
        self.store.mark_recalled([rows["群赛"]["id"]], True)
        self.assertEqual(
            next(
                r for r in self.store.list_memories(self.scope) if r["title"] == "群赛"
            )["clarity"],
            "vivid",
        )

    def test_optional_birthday_precision_and_lexical_recall(self):
        self.cfg["memory"]["core_blur_after_days"] = 30
        mid = self.put([proposal(self.a, self.scope)])[0]
        self.store.maintenance(self.cfg, now() + 365 * 86400)
        rows = self.store.recall_candidates(
            self.scope, None, None, False, False, None, None
        )
        result = rank_memories(rows, "Alice的生日是什么时候", None, self.a["ref"], 3)
        self.assertEqual(result[0]["text"], "Alice 的生日在 7 月")
        deep = rank_memories(rows, "生日", None, self.a["ref"], 3, deep=True)
        self.assertIn("22 日", deep[0]["text"])
        self.assertEqual(self.store.list_memories(self.scope)[0]["recall_count"], 0)
        self.store.mark_recalled([mid])
        self.store.maintenance(self.cfg, now() + 10)
        self.assertEqual(self.store.list_memories(self.scope)[0]["clarity"], "clear")

    def test_style_review_and_manual_rollback(self):
        self.store.commit_styles(
            self.scope,
            [
                {
                    "kind": "slang",
                    "pattern": "紫猫",
                    "meaning": "虚无之灵",
                    "example": "紫猫上高地",
                    "confidence": 0.95,
                }
            ],
            [self.a["seq"]],
            False,
        )
        s = self.store.list_styles(self.scope)[0]
        self.assertEqual(s["status"], "pending")
        self.store.edit_style(s["id"], {"status": "approved"})
        self.assertEqual(len(self.store.list_styles(self.scope, "", "approved")), 1)
        self.store.edit_style(s["id"], {}, "undo")
        self.assertEqual(self.store.list_styles(self.scope)[0]["status"], "pending")

    def test_learning_batch_contains_only_markable_source_ids(self):
        rows = [
            add_message(self.store, self.meta, "alice", '"\\' * 850, f"batch-{i}")
            for i in range(10)
        ]
        included, body = learning_transcript(rows, 6000)
        self.assertTrue(included)
        self.assertLess(len(included), len(rows))
        self.assertEqual(
            [r["seq"] for r in included], [r["n"] for r in json.loads(body)]
        )


class DotaTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = TemporaryDirectory()
        self.root = Path(self.tmp.name)
        cfg = ConfigStore(self.root)
        self.svc = DotaService(
            self.root / "dota", cfg, DailyLog(self.root / "logs", cfg, LOGGER)
        )
        self.cfg = defaults()["dota"]

    async def asyncTearDown(self):
        self.tmp.cleanup()

    async def test_exact_full_rank_compare_and_missing_pairs(self):
        s = StatsSnapshot(fixture(), self.svc.aliases["heroes"])
        self.assertEqual(s.index.resolve("紫猫")[0]["id"], 126)
        self.assertEqual(len(s.ranks), 7)
        self.assertFalse(hasattr(s, "rank_rows"))
        self.assertNotIn("win", s.ranks[0][126])
        self.assertEqual(s.pair(1, 126, 0), (42.0, 60.0))
        self.assertEqual(s.pair(126, 1, 0), (58.0, 60.0))
        self.assertEqual(s.pair(4, 1, 0), (None, None))
        bad = fixture()
        bad["ranked_hero_data"][0]["hero_data"][0]["enemy_win_rate"].pop()
        with self.assertRaises(ValueError):
            StatsSnapshot(bad, self.svc.aliases["heroes"])

    async def test_existing_snapshots_read_only_and_trends(self):
        p = self.root / "external"
        p.mkdir()
        self.cfg["existing_snapshots_dir"] = str(p)
        atomic_json(p / "20261120.json", fixture(shift=100))
        atomic_json(p / "20261113.json", fixture("2026-11-13"))
        (p / "20261121.json").write_text("{invalid")
        before = (p / "20261120.json").stat().st_mtime_ns
        # Legacy matchup data is still usable, but cannot provide ordinary rankings.
        self.assertIn("error", await self.svc.stats(self.cfg, "ranking", limit=1))
        self.assertIn("error", await self.svc.stats(self.cfg, "ranking", rank_chunk=0))
        r = await self.svc.stats(self.cfg, "matchups", hero="敌法", other="紫猫")
        self.assertEqual(r["snapshot_date"], "2026-11-20")
        self.assertEqual(r["first_vs_second_pct"], 42.0)
        self.assertEqual(r["same_team_pct"], 60.0)
        self.assertNotIn("win_rate_pct", r["first"])
        atomic_json(self.svc.source_dirs["public"] / "20261120.json", public_fixture(shift=10))
        atomic_json(self.svc.source_dirs["public"] / "20261113.json", public_fixture("2026-11-13"))
        r = await self.svc.stats(self.cfg, "ranking", limit=1)
        self.assertEqual(r["ranking"][0]["name"], "斧王")
        self.assertEqual(r["ranking"][0]["win_rate_pct"], 57.0)
        self.assertEqual(r["source"], PUBLIC_STATS_URL)
        self.assertEqual(r["stale"], day_key() != "2026-11-20")
        trends = await self.svc.stats(self.cfg, "trends", hero="斧王", days=7)
        self.assertEqual(trends["rows"][0]["delta_percentage_points"], 1)
        self.assertEqual(trends["previous_date"], "2026-11-13")
        self.assertIn("error", await self.svc.stats(self.cfg, "trends", days=3))
        self.assertEqual((p / "20261120.json").stat().st_mtime_ns, before)

    async def test_default_public_explicit_bracket_week_and_pair(self):
        self.cfg["default_rank"] = 6  # Only the matchup default, never the own-stat scope.
        self.cfg["rank_labels"] = {"6": "对抗组自定义标签"}
        atomic_json(self.svc.source_dirs["public"] / "20261120.json", public_fixture())
        atomic_json(self.svc.source_dirs["weekly"] / "20261120.json", weekly_fixture())
        atomic_json(self.svc.snap_dir / "20261120.json", fixture())
        overall = await self.svc.stats(self.cfg, "ranking", limit=1)
        self.assertEqual(overall["ranking"][0]["id"], 2)
        self.assertEqual(overall["ranking"][0]["win_rate_pct"], 56)
        self.assertEqual(overall["ranking"][0]["picks"], 1000)
        self.assertEqual(overall["quality"]["weighted_win_rate_pct"], 50)
        self.assertNotIn("rank", overall)
        bracket = await self.svc.stats(self.cfg, "ranking", bracket=1, limit=1)
        self.assertEqual(bracket["ranking"][0]["id"], 126)
        self.assertIn("先锋", bracket["scope"])
        missing = await self.svc.stats(self.cfg, "hero", hero="紫猫", bracket=8)
        self.assertIn("error", missing)
        self.assertNotIn("hero", missing)
        self.assertEqual(missing["quality"]["sampled_heroes"], 0)
        self.assertNotIn(8, missing["available_brackets"])
        weekly = await self.svc.stats(self.cfg, "ranking", rank_chunk=0, limit=1)
        self.assertEqual(weekly["source"], WEEKLY_STATS_URL)
        self.assertEqual(weekly["ranking"][0]["win_rate_pct"], 62)
        self.assertEqual(weekly["ranking"][0]["ban_rate_pct"], 2.5)
        self.assertEqual(weekly["week"], 1)
        self.assertNotIn("对抗组自定义标签", str(weekly))
        trends = await self.svc.stats(self.cfg, "trends", hero="紫猫", rank_chunk=0)
        self.assertEqual(len(trends["series"]), 8)
        self.assertEqual(trends["previous_week"], 2)
        self.assertEqual(trends["rows"][0]["delta_percentage_points"], 1)
        self.assertIn("error", await self.svc.stats(self.cfg, "ranking", bracket=1, rank_chunk=0))
        self.assertIn("error", await self.svc.stats(self.cfg, "hero", hero="紫猫", week=2))
        self.assertIn("error", await self.svc.stats(self.cfg, "trends", rank_chunk=0, days=7))
        pair = await self.svc.stats(self.cfg, "compare", hero="紫猫", other="敌法", include_matchup=True)
        self.assertEqual(pair["source"], PUBLIC_STATS_URL)
        self.assertEqual(pair["first"]["win_rate_pct"], 50)
        self.assertEqual(pair["matchup"]["source"], STATS_URL)
        self.assertEqual(pair["matchup"]["rank"], 6)
        self.assertEqual(pair["matchup"]["rank_label"], "对抗组自定义标签")
        self.assertEqual(pair["matchup"]["first_vs_second_pct"], 58)

    def test_public_validation_weighted_and_bad_inputs(self):
        aliases = self.svc.aliases["heroes"]
        good = public_fixture()
        count = len(good["hero_stats"]) - 1
        for row in good["hero_stats"]:
            # Unweighted hero average ~60%, yet the pick-weighted pool is exactly 50%.
            row.update(pub_pick=200 * count if row["id"] == 1 else 100, pub_win=90 * count if row["id"] == 1 else 60)
        s = WinRateSnapshot(good, aliases, source="public")
        self.assertEqual(s.quality["pub"]["weighted_win_rate_pct"], 50)
        self.assertGreater(sum(r["rate"] for r in s.groups["pub"].values()) / len(s.groups["pub"]), 0.59)
        for corrupt in (
            lambda rows: rows[0].update(pub_win=rows[0]["pub_pick"] + 1),
            lambda rows: rows[0].update(pub_pick=True),
            lambda rows: rows.pop(),
            lambda rows: rows.append(rows[0]),
            lambda rows: rows[0].update(name=rows[1]["name"]),
            lambda rows: [r.update(pub_win=int(r["pub_pick"] * 0.43)) for r in rows],
            lambda rows: rows[0].pop("7_win"),
            lambda rows: [r.update(pub_pick=0, pub_win=0) for r in rows[2:]],
        ):
            bad = public_fixture()
            corrupt(bad["hero_stats"])
            with self.assertRaises(ValueError):
                WinRateSnapshot(bad, aliases, source="public")
        no_picks = public_fixture()
        no_picks["hero_stats"][2].update(pub_pick=0, pub_win=0)
        snapshot = WinRateSnapshot(no_picks, aliases, source="public")
        hid = no_picks["hero_stats"][2]["id"]
        self.assertIsNone(snapshot.row(hid, "pub")["win_rate_pct"])
        self.assertNotIn(hid, [r["id"] for r in snapshot.ranked("pub", "asc", 30)])

    def test_weekly_validation_rejects_units_balance_and_coverage_errors(self):
        aliases = self.svc.aliases["heroes"]
        for corrupt in (
            lambda rows: rows[0]["hero_data_per_chunk"][0]["week_data"][-1].update(win_percent=57),
            lambda rows: rows[0]["hero_data_per_chunk"][0]["week_data"][-1].update(win_percent=float("nan")),
            lambda rows: [r["hero_data_per_chunk"][0]["week_data"][-1].update(win_percent=0.43) for r in rows],
            lambda rows: [r["hero_data_per_chunk"][0]["week_data"][-1].update(pick_percent=0.01) for r in rows],
            lambda rows: rows[0]["hero_data_per_chunk"][0]["week_data"].pop(),
            lambda rows: [r["hero_data_per_chunk"][0]["week_data"].pop() for r in rows],
            lambda rows: rows[0]["hero_data_per_chunk"].append(rows[0]["hero_data_per_chunk"][0]),
        ):
            bad = weekly_fixture()
            corrupt(bad["hero_stats"])
            with self.assertRaises(ValueError):
                WinRateSnapshot(bad, aliases, source="weekly")

    async def test_independent_sync_and_failed_refresh_retains_each_source(self):
        today = day_key()
        filename = today.replace("-", "") + ".json"
        external = self.root / "external"
        external.mkdir()
        prior = fixture(today)
        for group in prior["ranked_hero_data"]:
            group["hero_data"][0]["enemy_win_rate"][2] = 5000
            group["hero_data"][0]["ally_win_rate"][2] = 5000
        atomic_json(external / filename, prior)
        original_legacy = (external / filename).read_bytes()
        self.cfg["existing_snapshots_dir"] = str(external)
        public = public_fixture(today, heroes=prior["heroes"])
        weekly = weekly_fixture(today, heroes=prior["heroes"])
        responses = {
            HERO_LIST_URL: {"result": {"data": {"heroes": prior["heroes"]}}},
            PUBLIC_STATS_URL: public["hero_stats"],
            WEEKLY_STATS_URL: {"heroes": weekly["hero_stats"]},
        }
        calls = []

        async def fetch(url, cfg):
            calls.append(url)
            value = responses[url]
            if isinstance(value, Exception):
                raise value
            return value

        self.svc._fetch = fetch
        state = await self.svc.sync(self.cfg)
        self.assertEqual(state["state"], "同步成功")
        self.assertEqual(set(calls), set(responses))  # Today's legacy snapshot does not skip the two new sources.
        self.assertEqual(state["sources"]["matchups"]["state"], "今日快照已存在")
        self.assertEqual(state["sources"]["public"]["available_brackets"], list(range(1, 8)))
        public_path = self.svc.source_dirs["public"] / filename
        original = public_path.read_bytes()
        calls.clear()
        await self.svc.sync(self.cfg)
        self.assertEqual(calls, [])
        responses[PUBLIC_STATS_URL][0]["pub_win"] = responses[PUBLIC_STATS_URL][0]["pub_pick"] + 1
        responses[WEEKLY_STATS_URL] = {"heroes": weekly_fixture(today, shift=0.01, heroes=prior["heroes"])["hero_stats"]}
        responses[STATS_URL] = RuntimeError("offline")
        state = await self.svc.sync(self.cfg, force=True)
        self.assertEqual(state["state"], "部分同步成功")
        self.assertIn("保留旧数据", state["sources"]["public"]["state"])
        self.assertIn("保留旧数据", state["sources"]["matchups"]["state"])
        self.assertEqual(state["sources"]["weekly"]["state"], "同步成功")
        self.assertEqual(public_path.read_bytes(), original)
        self.assertEqual((external / filename).read_bytes(), original_legacy)
        self.assertEqual((await self.svc.stats(self.cfg, "ranking", limit=1))["ranking"][0]["win_rate_pct"], 56)
        self.assertEqual((await self.svc.stats(self.cfg, "ranking", rank_chunk=0, limit=1))["ranking"][0]["win_rate_pct"], 63)

    async def test_public_first_sync_without_valve_roster(self):
        public = public_fixture(day_key())

        async def fetch(url, cfg):
            if url == PUBLIC_STATS_URL:
                return public["hero_stats"]
            raise RuntimeError("Valve offline")

        self.svc._fetch = fetch
        result = await self.svc.sync(self.cfg)
        self.assertEqual(result["state"], "部分同步成功")
        self.assertEqual(result["sources"]["public"]["state"], "同步成功")
        r = await self.svc.stats(self.cfg, "ranking", limit=1)
        self.assertEqual(r["ranking"][0]["english"], "Axe")
        self.assertIn("roster_note", r)
        self.assertIn("error", await self.svc.stats(self.cfg, "ranking", rank_chunk=0))
        self.assertIn("error", await self.svc.stats(self.cfg, "matchups", hero="Axe"))

    async def test_successful_source_cleanup_never_prunes_failed_source_fallback(self):
        today = day_key()
        old = (date.fromisoformat(today) - timedelta(days=45)).isoformat()
        filename = old.replace("-", "") + ".json"
        self.cfg["snapshot_retention_days"] = 30
        public_path = self.svc.source_dirs["public"] / filename
        weekly_path = self.svc.source_dirs["weekly"] / filename
        atomic_json(public_path, public_fixture(old))
        atomic_json(weekly_path, weekly_fixture(old))
        weekly = weekly_fixture(today)

        async def fetch(url, cfg):
            if url == HERO_LIST_URL:
                return {"result": {"data": {"heroes": weekly["heroes"]}}}
            if url == WEEKLY_STATS_URL:
                return {"heroes": weekly["hero_stats"]}
            raise RuntimeError("source offline")

        self.svc._fetch = fetch
        result = await self.svc.sync(self.cfg)
        self.assertEqual(result["state"], "部分同步成功")
        self.assertTrue(public_path.exists())
        self.assertFalse(weekly_path.exists())
        self.assertTrue((await self.svc.stats(self.cfg, "ranking", limit=1))["stale"])
        reopened = DotaService(self.svc.root, self.svc.config, self.svc.log)
        status = await reopened.status(self.cfg)
        self.assertEqual(status["sources"]["public"]["date"], old)
        self.assertEqual(status["sources"]["weekly"]["date"], today)

    async def test_result_budget_preserves_exact_values_and_json(self):
        atomic_json(self.svc.source_dirs["weekly"] / "20261120.json", weekly_fixture())
        r = await self.svc.stats(self.cfg, "ranking", rank_chunk=0, limit=30)
        bounded = fit_stats_payload(r, 1250)
        encoded = json.dumps(bounded, ensure_ascii=False, separators=(",", ":"))
        self.assertLessEqual(len(encoded), 1250)
        self.assertTrue(bounded["truncated"])
        self.assertGreater(len(bounded["ranking"]), 0)
        self.assertLess(len(bounded["ranking"]), 30)
        self.assertEqual(bounded["ranking"], r["ranking"][:len(bounded["ranking"])])
        self.assertEqual(bounded["source"], WEEKLY_STATS_URL)
        self.assertEqual(bounded["quality"], r["quality"])
        self.assertEqual(fit_stats_payload({"picks": 13810000000}, 500)["picks"], 13810000000)

    async def test_hero_and_item_mock_official_endpoints(self):
        roster = fixture()["heroes"]
        items = [
            {
                "id": i,
                "name": f"item_fixture_{i}",
                "name_loc": f"物品{i}",
                "name_english_loc": f"Item {i}",
            }
            for i in range(1, 204)
        ]
        items[0].update(
            name="item_blink", name_loc="闪烁匕首", name_english_loc="Blink Dagger"
        )
        items[1].update(
            name="item_recipe_blink",
            name_loc="图纸",
            name_english_loc="Recipe",
            recipes=[{"items": [1]}],
        )
        requests = []

        async def fetch(url, cfg):
            requests.append(url)
            if "patchnoteslist" in url:
                return {"patches": [{"patch_number": "7.41f", "patch_timestamp": 1}]}
            if "herolist" in url:
                return {"result": {"data": {"heroes": roster}}}
            if "itemlist" in url:
                return {"result": {"data": {"itemabilities": items}}}
            if "herodata" in url:
                return {
                    "result": {
                        "data": {
                            "heroes": [
                                {
                                    **roster[-1],
                                    "abilities": [
                                        {
                                            "name": "void_spirit_test",
                                            "name_loc": "测试技能",
                                            "desc_loc": "造成 %damage% 点伤害。",
                                            "special_values": [
                                                {
                                                    "name": "damage",
                                                    "heading_loc": "伤害",
                                                    "values_float": [100, 200],
                                                }
                                            ],
                                            "cooldowns": [10, 8],
                                        }
                                    ],
                                }
                            ]
                        }
                    }
                }
            if "itemdata" in url:
                return {
                    "result": {
                        "data": {
                            "items": [
                                {**items[0], "item_cost": 2250, "desc_loc": "闪烁效果"}
                            ]
                        }
                    }
                }
            raise AssertionError(url)

        self.svc._fetch = fetch
        hero = await self.svc.hero(self.cfg, "紫猫", "测试技能")
        self.assertEqual(hero["hero"]["id"], 126)
        self.assertIn("100 / 200", hero["ability"]["description"])
        item = await self.svc.item(self.cfg, "跳刀")
        self.assertEqual(item["cost_gold"], 2250)
        self.assertIn("闪烁", item["effects"]["description"])
        self.assertEqual(len(requests), 5)
        await self.svc.hero(self.cfg, "紫猫", "测试技能")
        self.assertEqual(len(requests), 5)
        idx = AliasIndex(items, self.svc.aliases["items"], False)
        self.assertEqual(idx.resolve("跳刀图纸")[0]["id"], 2)

    async def test_failed_detail_uses_old_md(self):
        async def fail(url, cfg):
            raise RuntimeError("offline")

        self.svc._fetch = fail
        p = self.root / "Dota2-Heroes.md"
        p.write_text(
            "## Dota 2 7.41f · 虚无之灵（Void Spirit），又称紫猫 · 残阴\n- 基础伤害 100。\n\n## Dota 2 7.41f · 基础装备 · 闪烁匕首（Blink Dagger），又称跳刀\n物品 ID 1 · 代码 item_blink。\n- Valve 标价 2250 金币。",
            encoding="utf-8",
        )
        self.cfg["existing_catalog_md"] = str(self.root)  # Also accept the directory chosen in the file picker.
        result = await self.svc.hero(self.cfg, "紫猫", "残阴")
        self.assertIn("100", result["entries"][0])
        self.assertIn("未核实", result["note"])
        item = await self.svc.item(self.cfg, "跳刀")
        self.assertIn("2250", item["entries"][0])


class EngineTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = TemporaryDirectory()
        self.host = FakeHost()
        self.engine = Engine(Path(self.tmp.name), self.host, LOGGER)
        cfg = self.engine.config.snapshot()
        cfg["access"].update(
            mode="active",
            allowed_groups=["g1"],
            private_enabled=True,
            private_users=["alice"],
        )
        cfg["decision"].update(
            merge_wait_seconds=0.08,
            merge_max_wait_seconds=0.16,
            ordinary_check_probability=1.0,
            unsolicited_gap_seconds=0,
        )
        cfg["reply"].update(gap_min=0, gap_max=0, split_chars=80)
        cfg["dota"]["auto_sync"] = False
        cfg["models"].update(
            chat_provider="chat", decision_provider="fast", memory_provider="memory"
        )
        cfg["tools"]["kb_bindings"] = [
            {
                "alias": "sap",
                "name": "SAP Documents",
                "description": "SAP 技术文档",
                "allowed_groups": ["g1"],
                "allow_private": False,
            }
        ]
        self.engine.config.save(cfg, self.engine.config.revision())
        self.engine.running = True

    async def asyncTearDown(self):
        await self.engine.stop()
        self.tmp.cleanup()

    def update(self, section, **values):
        c = self.engine.config.snapshot()
        c[section].update(values)
        self.engine.config.save(c, self.engine.config.revision())

    async def idle(self):
        for _ in range(150):
            if all(
                not s.pending and (not s.worker or s.worker.done())
                for s in self.engine.states.values()
            ):
                return
            await asyncio.sleep(0.025)
        self.fail("chat worker did not drain")

    async def test_multi_speaker_context_with_tool_round(self):
        a = event("g-msg-1", "紫猫这个技能残阴很厉害，伤害很高。", "alice")
        b = event("g-msg-2", "真的假的，肥鱼帮我看看。", "bob")

        def decision(prompt, system, kwargs):
            x = json.loads(prompt)
            self.assertEqual(len(x["new_ids"]), 2)
            self.assertIn("残阴", prompt)
            return {
                "completion_text": json.dumps(
                    {
                        "reply": True,
                        "confidence": 0.99,
                        "target": x["new_ids"][-1],
                        "reason": "点名请教",
                    }
                )
            }

        def first(prompt, system, kwargs):
            x = json.loads(prompt)
            self.assertEqual(x["target_text"], b.info["text"])
            self.assertIn("残阴", prompt)
            self.assertTrue(
                any(t["name"] == "query_dota_hero" for t in kwargs["tools"])
            )
            return {
                "role": "tool",
                "tools_call_name": ["query_dota_hero"],
                "tools_call_args": [{"hero": "紫猫", "skill": "残阴"}],
                "tools_call_ids": ["call-1"],
            }

        async def detail(cfg, name, skill):
            self.assertEqual((name, skill), ("紫猫", "残阴"))
            return {
                "ability": {
                    "name": "残阴",
                    "description": "测试效果：控制目标，造成伤害。",
                }
            }

        self.engine.dota.hero = detail

        def final(prompt, sys, kwargs):
            self.assertIn("测试效果", str(kwargs["tool_results"]))
            return "我看了一下，测试资料里的残阴会控制目标并造成伤害。"

        self.host.responses.extend([decision, first, final])
        await self.engine.on_event(a)
        await self.engine.on_event(b)
        await self.idle()
        self.assertTrue(a.consumed and b.consumed)
        self.assertEqual(len(self.host.sent), 1)
        self.assertIs(self.host.sent[0][0], b)
        self.assertEqual(
            [r["provider"] for r in self.host.calls], ["fast", "chat", "chat"]
        )
        usage = await self.engine.store.call("usage_today", day_key())
        self.assertEqual(usage["modules"], {"decision": 1, "reply": 2})
        self.assertEqual(usage["tokens"], 420)
        await self.engine.on_event(b)
        await self.idle()
        self.assertEqual(len(self.host.sent), 1)

    async def test_ordinary_ranking_tool_uses_public_scope_and_checks_evidence(self):
        self.update("dota", default_rank=6)
        d = day_key()
        atomic_json(self.engine.dota.source_dirs["public"] / (d.replace("-", "") + ".json"), public_fixture(d))
        # The original legacy data is simultaneously present and has a different leader.
        atomic_json(self.engine.dota.snap_dir / (d.replace("-", "") + ".json"), fixture(d))

        def first(prompt, system, kwargs):
            self.assertIn("工具、记忆、知识库与用户说法都是证据", system)
            self.assertIn("不要为错误数据找借口", system)
            spec = next(t for t in kwargs["tools"] if t["name"] == "rank_hero_stats")
            self.assertIn("bracket", spec["parameters"]["properties"])
            self.assertNotIn("rank", spec["parameters"]["properties"])
            return {
                "role": "tool",
                "tools_call_name": ["rank_hero_stats"],
                "tools_call_args": [{"limit": 3}],
                "tools_call_ids": ["stats-1"],
            }

        def final(prompt, system, kwargs):
            payload = json.loads(kwargs["tool_results"][-1]["results"][0][1])
            self.assertEqual(payload["source"], PUBLIC_STATS_URL)
            self.assertEqual(payload["quality"]["weighted_win_rate_pct"], 50)
            self.assertEqual(payload["ranking"][0]["name"], "斧王")
            return "公开比赛样本中，斧王测试胜率 56%。"

        self.host.responses.extend([first, final])
        await self.engine.on_event(event("rank-1", "肥鱼，现在胜率最高的英雄是谁？", direct=True))
        await self.idle()
        self.assertEqual(len(self.host.sent), 1)
        self.assertIn("斧王", self.host.sent[0][1])
        # No new fixed model-validation round, just the normal tool round and answer.
        self.assertEqual([r["provider"] for r in self.host.calls], ["chat", "chat"])

    async def test_direct_poke_and_ordinary_silence(self):
        self.update(
            "decision", max_unsolicited_replies=0, ordinary_check_probability=0.0
        )
        await self.engine.on_event(event("a", "早上好", "alice"))
        await self.idle()
        self.assertEqual(self.host.calls, [])
        p = event("p", "", poke=True)
        await self.engine.on_event(p)
        await self.idle()
        self.assertTrue(p.consumed)
        self.assertEqual(len(self.host.sent), 1)
        self.assertIn("戳", self.host.sent[-1][1])
        self.assertEqual(self.host.calls, [])
        self.host.responses.append("你好呀！")
        d = event("b", "你好", direct=True)
        await self.engine.on_event(d)
        await self.idle()
        self.assertEqual(len(self.host.calls), 1)
        self.assertEqual(len(self.host.sent), 2)

    async def test_named_request_not_blocked_by_unsolicited_density(self):
        self.update(
            "decision", max_unsolicited_replies=0, ordinary_check_probability=0.0
        )
        self.host.responses.append(
            lambda p, s, k: json.dumps(
                {
                    "reply": True,
                    "confidence": 0.9,
                    "target": json.loads(p)["new_ids"][-1],
                }
            )
        )
        self.host.responses.append("好呀，肥鱼在。")
        await self.engine.on_event(event("named", "肥鱼，你在吗？", "bob"))
        await self.idle()
        self.assertEqual(len(self.host.sent), 1)

    async def test_observe_off_and_commands(self):
        self.update("access", mode="off")
        e = event("a", "你好", direct=True)
        await self.engine.on_event(e)
        self.assertFalse(e.consumed)
        self.update("access", mode="observe")
        e = event("b", "你好", direct=True)
        await self.engine.on_event(e)
        await self.idle()
        self.assertFalse(e.consumed)
        self.assertEqual(self.host.calls, [])
        self.assertEqual(self.host.sent, [])
        self.update("access", mode="active")
        cmd = event("c", "/sid", direct=True)
        await self.engine.on_event(cmd)
        self.assertFalse(cmd.consumed)
        outside = event("d", "你好", location="g2", direct=True)
        await self.engine.on_event(outside)
        self.assertFalse(outside.consumed)

    async def test_blocked_users_denials_and_private_scope(self):
        self.update("access", blocked_users=["bob"])
        blocked = event("b", "不该被保存", uid="bob", direct=True)
        await self.engine.on_event(blocked)
        self.assertTrue(blocked.consumed)
        self.assertEqual(self.host.calls, [])
        self.host.responses.append("允许的回复")
        e = event("a", "你好", direct=True)
        await self.engine.on_event(e)
        await self.idle()
        scope = metadata()["scope"]
        self.assertEqual(await self.engine.store.call("person", scope, "bob"), None)
        await self.engine.store.call(
            "edit_person",
            scope,
            "alice",
            {"deny_keywords": ["测试受限"], "deny_reply": "不回答"},
        )
        e = event("denied", "测试受限请求", direct=True)
        await self.engine.on_event(e)
        await self.idle()
        self.assertEqual(self.host.sent[-1][1], "不回答")
        self.assertEqual(len(self.host.calls), 1)
        self.host.responses.append("这是私聊")
        priv = event("p", "私聊消息", kind="private", location="alice")
        await self.engine.on_event(priv)
        await self.idle()
        self.assertNotIn("允许的回复", self.host.calls[-1]["prompt"])
        self.assertNotEqual(
            metadata()["scope"], metadata("alice", kind="private")["scope"]
        )

    async def test_knowledge_binding_and_explicit_remember(self):
        m = metadata()
        cfg = self.engine.config.snapshot()
        s = self.engine.store
        r = await s.call(
            "ingest",
            m,
            {
                "uid": "alice",
                "name": "Alice",
                "text": "肥鱼请记住我的生日是7月22日",
                "ext_id": "e",
                "ts": now(),
                "explicit": True,
            },
        )
        router = self.engine.tools
        specs = router.specs(m, cfg, True)
        value = json.loads(
            await router.execute(
                specs,
                "search_knowledge",
                {"source": "sap", "query": "SAP STO"},
                m,
                cfg,
                None,
                r["ref"],
                [r],
            )
        )
        self.assertIn("results", value)
        self.assertEqual(self.host.kb_calls[0][0], "SAP Documents")
        m2 = metadata("alice", kind="private")
        self.assertFalse(
            any(t["name"] == "search_knowledge" for t in router.specs(m2, cfg))
        )
        remember = {
            "key": "birthday",
            "kind": "core",
            "title": "生日",
            "full": "Alice 生日7月22日",
            "gist": "Alice 生日7月",
            "trace": "Alice的生日",
        }
        value = json.loads(
            await router.execute(
                specs, "remember_memory", remember, m, cfg, None, r["ref"], [r]
            )
        )
        self.assertTrue(value["saved"])
        result = json.loads(
            await router.execute(
                specs,
                "recall_memory",
                {"who": "current", "query": "生日"},
                m,
                cfg,
                None,
                r["ref"],
                [r],
            )
        )
        self.assertIn("7月22日", result["memories"][0]["text"])
        self.assertEqual(
            await s.call(
                "recall_candidates", m2["scope"], None, None, True, True, None, None
            ),
            [],
        )
        self.update("memory", capture_private=False)
        self.assertFalse(
            any(
                t["name"] == "remember_memory"
                for t in router.specs(m2, self.engine.config.snapshot(), True)
            )
        )

    async def test_background_batch_and_style_approval(self):
        self.update("decision", ordinary_check_probability=0.0)
        m = metadata()
        s = self.engine.store
        r = await s.call(
            "ingest",
            m,
            {
                "uid": "alice",
                "name": "Alice",
                "text": "我生日在7月22日",
                "ext_id": "learn",
                "ts": now(),
                "memory_done": False,
                "style_done": True,
            },
        )
        self.host.responses.append(
            json.dumps(
                {"memories": [proposal(r, m["scope"])], "affinity": []},
                ensure_ascii=False,
            )
        )
        self.assertTrue(
            await self.engine.memory.learn_once(
                self.engine.config.snapshot(), force=True
            )
        )
        self.assertEqual(len(await s.call("list_memories", m["scope"])), 1)
        self.assertFalse(
            await self.engine.memory.learn_once(
                self.engine.config.snapshot(), force=True
            )
        )
        self.assertEqual(len(self.host.calls), 1)

    async def test_token_hard_soft_limits_and_balance_no_llm(self):
        c = self.engine.config.snapshot()
        c["budget"].update(daily_tokens=1000, soft_ratio=0.8)
        self.engine.config.save(c, self.engine.config.revision())
        await self.engine.store.call(
            "add_usage", day_key(), "reply", "chat", False, 850, 0, 0, False, 1
        )
        with self.assertRaises(BudgetExceeded):
            await self.engine.gateway.generate(
                "style", "test", "hi", "s", background=True, output_reserve=10
            )
        self.host.responses.append("ok")
        await self.engine.gateway.generate(
            "reply", "test", "hi", "s", output_reserve=10
        )
        self.assertEqual(
            (await self.engine.store.call("usage_today", day_key()))["tokens"], 990
        )
        with self.assertRaises(BudgetExceeded):
            await self.engine.gateway.generate(
                "reply", "test", "hello", "s", output_reserve=10
            )
        self.update(
            "budget",
            balance_enabled=True,
            balance_provider="deepseek",
            balance_target_umo=metadata()["umo"],
            balance_mention_id="alice",
        )
        m = metadata()
        await self.engine.store.call(
            "ingest",
            m,
            {
                "uid": "alice",
                "name": "Alice",
                "text": "hello",
                "ext_id": "balance",
                "ts": now(),
            },
        )
        self.assertEqual((await self.engine.check_balance())["state"], "余额低")
        await self.engine.check_balance()
        self.assertEqual(len(self.host.notify_calls), 1)
        self.assertEqual(len(self.host.calls), 1)

    async def test_split_reply(self):
        self.assertEqual(
            split_reply("第一句。第二句。第三句。", 6, 3),
            ["第一句。", "第二句。", "第三句。"],
        )
        self.assertEqual(split_reply("```\nexample\n```", 4, 3), ["```\nexample\n```"])

    async def test_qq_ranking_retains_numbers_through_split_and_storage(self):
        ranks = [
            "力丸 56.40%", "冥魂大帝 55.27%", "米波 55.02%",
            "亚巴顿 54.68%", "巨魔战将 54.35%", "幻影刺客 54.07%",
            "维萨吉 53.75%", "全能骑士 53.62%", "戴泽 53.48%", "幽鬼 53.21%",
        ]
        title = "公开样本胜率 Top10：\n"
        original = title + "\n".join(f"{i}. {rank}" for i, rank in enumerate(ranks, 1))
        first = title + "\n".join(f"{i}、{rank}" for i, rank in enumerate(ranks[:7], 1))
        second = "\n".join(f"{i}、{rank}" for i, rank in enumerate(ranks[7:], 8))
        self.update("reply", split_chars=len(first) + 1, max_parts=5)
        self.host.responses.append(original)
        await self.engine.on_event(event("list-long", "肥鱼，列一下前十", direct=True))
        await self.idle()
        self.assertEqual([part for _, part in self.host.sent], [first, second])
        self.assertEqual(len(self.host.calls), 1)
        rows = self.engine.store.recent_bot_messages(metadata()["scope"])
        self.assertEqual([row["text"] for row in rows], [first, second])

    async def test_short_model_line_breaks_reach_separate_adapter_calls(self):
        first = "断崖之剑不是固拉多的技能吗，怎么拿来劈鱼了"
        second = "我一条鱼吃地面技能，你说呢"
        self.update("reply", split_chars=300)
        self.update("logs", level="DEBUG")
        self.host.responses.extend([first + "\n" + second, "原神，启动"])
        for i in range(2):
            await self.engine.on_event(event("chat-lines-" + str(i), "肥鱼，你说呢", direct=True))
            await self.idle()
        expected = [first, second, "原神，启动"]
        self.assertEqual([text for _, text in self.host.sent], expected)
        history = self.engine.store.recent_bot_messages(metadata()["scope"])
        self.assertEqual([row["text"] for row in history], expected)
        self.assertEqual(len(self.host.calls), 2)
        self.assertIn("想另发一条时换行", self.host.calls[0]["system"])
        logs = [r for r in self.engine.log.recent if r["module"] == "REPLY"]
        turns = [r for r in logs if r["code"] == "submitted_to_adapter"]
        self.assertEqual([(r["planned"], r["parts"], r["newlines"]) for r in turns], [(2,2,1),(1,1,0)])
        self.assertEqual(turns[0]["part_chars"], [len(first), len(second)])
        self.assertEqual([r["part"] for r in logs if r["code"] == "part_submitted"
                          and r["turn_id"] == turns[0]["turn_id"]], [1, 2])
        self.assertNotIn(first, str(logs))

    async def test_exhausted_six_tools_recover_final_answer_without_more_queries(self):
        # Matches the reported private-chat incident: 3 query rounds, 6 calls,
        # then a fourth response containing raw DeepSeek protocol.
        self.update("reply", max_tool_rounds=3, max_tool_calls=6, split_chars=300)
        self.update("tools", contextual_tools=False, web_enabled=True,
                    kb_bindings=[{"alias": "dota", "name": "Dota", "description": "",
                                  "allowed_groups": [], "allow_private": True}])
        self.update("logs", level="DEBUG")
        executed = []

        async def query(name, args, *rest):
            executed.append((name, args))
            return "合成测试资料，未核实野怪金币。"

        self.host.responses.extend([
            tool_response("one", ("search_knowledge", {"source": "dota", "query": "远古野"}),
                          ("search_dota_mechanics", {"kind": "hero_ability", "damage_type": "physical"})),
            tool_response("two", ("search_web", {"query": "test-a"}), ("search_web", {"query": "test-b"})),
            tool_response("three", ("search_web", {"query": "test-c"}), ("search_web", {"query": "test-d"})),
            RAW_DSML,
            "查了几种资料，暂时没核实到准确金币数。",
        ])
        with patch.object(self.engine.tools, "_call", side_effect=query):
            await self.engine.on_event(event("six-tools", "远古野的金钱是多少，大野点是多少",
                                             location="alice", kind="private"))
            await self.idle()
        self.assertEqual([name for name, _ in executed],
                         ["search_knowledge", "search_dota_mechanics"] + ["search_web"] * 4)
        self.assertEqual(len(self.host.calls), 5)
        self.assertTrue(all(c["tools"] for c in self.host.calls[:3]))
        self.assertEqual([c["tools"] for c in self.host.calls[3:]], [[], []])
        for call in self.host.calls[3:]:
            self.assertIn(FINAL_REPLY_RULES, call["system"])
            self.assertNotIn("Dota自身胜率/排名/比较使用", call["system"])
            self.assertIn("完整5v5样本", call["system"])
            self.assertEqual(len(call["tool_results"]), 3)
        self.assertNotIn(TOOL_RETRY_RULES, self.host.calls[3]["system"])
        self.assertIn(TOOL_RETRY_RULES, self.host.calls[4]["system"])
        self.assertEqual([text for _, text in self.host.sent], ["查了几种资料，暂时没核实到准确金币数。"])
        logs = list(self.engine.log.recent)
        failures = [r for r in logs if r["code"] == "tool_protocol_error"]
        self.assertEqual(len(failures), 1)
        self.assertEqual((failures[0]["tool_rounds"], failures[0]["tool_calls"], failures[0]["offered_tools"]), (3, 6, 0))
        self.assertTrue(failures[0]["retry"])
        self.assertTrue(any(r["code"] == "tool_protocol_recovered" for r in logs))
        self.assertNotIn("未执行的测试查询", str(logs))

    async def test_first_round_dsml_retry_executes_only_structured_call(self):
        self.update("tools", contextual_tools=False, web_enabled=True)
        self.host.responses.extend([
            RAW_DSML,
            tool_response("valid", ("search_web", {"query": "public fixture query"})),
            "查到了测试资料。",
        ])
        await self.engine.on_event(event("raw-first", "肥鱼帮我查资料", direct=True))
        await self.idle()
        self.assertEqual(self.host.web_calls, ["public fixture query"])
        self.assertEqual(len(self.host.calls), 3)
        self.assertEqual(self.host.calls[0]["tools"], self.host.calls[1]["tools"])
        self.assertIn(TOOL_RETRY_RULES, self.host.calls[1]["system"])
        self.assertEqual([text for _, text in self.host.sent], ["查到了测试资料。"])
        self.assertFalse(any(contains_dsml(r["text"]) for r in self.engine.store.recent_bot_messages(metadata()["scope"])))

    async def test_only_one_protocol_recovery_for_whole_turn(self):
        self.update("tools", contextual_tools=False, web_enabled=True)
        self.update("reply", max_tool_rounds=1, max_tool_calls=1)
        self.host.responses.extend([
            RAW_DSML,
            tool_response("valid", ("search_web", {"query": "public fixture query"})),
            RAW_DSML,
        ])
        await self.engine.on_event(event("raw-twice", "肥鱼帮我查资料", direct=True))
        await self.idle()
        self.assertEqual(self.host.web_calls, ["public fixture query"])
        self.assertEqual(len(self.host.calls), 3)
        self.assertEqual(self.host.calls[-1]["tools"], [])
        self.assertEqual([text for _, text in self.host.sent], [TOOL_PROTOCOL_FALLBACK])
        errors = [r for r in self.engine.log.recent if r["code"] == "tool_protocol_error"]
        self.assertEqual([r["retry"] for r in errors], [True, False])

    async def test_final_unavailable_structured_tool_call_is_not_executed(self):
        self.update("reply", max_tool_rounds=0)
        self.host.responses.extend([
            tool_response("not-available", ("search_web", {"query": "do not execute"}),
                          content="我再查一下"),
            "目前没有足够资料。",
        ])
        await self.engine.on_event(event("closed-tools", "肥鱼帮我查资料", direct=True))
        await self.idle()
        self.assertEqual([c["tools"] for c in self.host.calls], [[], []])
        self.assertEqual(self.host.web_calls, [])
        self.assertEqual([text for _, text in self.host.sent], ["目前没有足够资料。"])
        self.assertTrue(any(r.get("reason") == "tools_unavailable" for r in self.engine.log.recent))

    async def test_invalid_structured_metadata_is_recovered_before_execution(self):
        self.update("tools", contextual_tools=False, web_enabled=True)
        malformed = tool_response("bad", ("search_web", {"query": "do not execute"}),
                                  ("search_web", {"query": "do not execute either"}))
        malformed["tools_call_ids"] = ["duplicate", "duplicate"]
        self.host.responses.extend([malformed, "这次暂时查不到。"])
        await self.engine.on_event(event("bad-call-fields", "肥鱼帮我查资料", direct=True))
        await self.idle()
        self.assertEqual(len(self.host.calls), 2)
        self.assertEqual(self.host.web_calls, [])
        self.assertEqual([text for _, text in self.host.sent], ["这次暂时查不到。"])
        self.assertTrue(any(r.get("reason") == "invalid_tool_calls" for r in self.engine.log.recent))

    async def test_valid_structured_call_with_dsml_text_needs_no_extra_model_request(self):
        self.update("tools", contextual_tools=False, web_enabled=True)
        self.host.responses.extend([
            tool_response("valid", ("search_web", {"query": "actual structured request"}), content=RAW_DSML),
            "返回了测试资料。",
        ])
        await self.engine.on_event(event("mixed-protocol", "肥鱼帮我查资料", direct=True))
        await self.idle()
        self.assertEqual(len(self.host.calls), 2)
        self.assertEqual(self.host.web_calls, ["actual structured request"])
        self.assertEqual([text for _, text in self.host.sent], ["返回了测试资料。"])
        self.assertTrue(any(r["code"] == "tool_protocol_text_discarded" for r in self.engine.log.recent))

    async def test_protocol_recovery_obeys_module_budget(self):
        limits = self.engine.config.snapshot()["budget"]["module_call_limits"]
        limits["reply"] = 1
        self.update("budget", module_call_limits=limits)
        self.host.responses.append(RAW_DSML)
        await self.engine.on_event(event("protocol-budget", "肥鱼帮我查资料", direct=True))
        await self.idle()
        self.assertEqual(len(self.host.calls), 1)
        self.assertEqual(self.host.web_calls, [])
        self.assertEqual([text for _, text in self.host.sent], [TOOL_PROTOCOL_FALLBACK])
        failure = next(r for r in self.engine.log.recent if r["code"] == "tool_protocol_retry_failed")
        self.assertEqual(failure["reason"], "budget")

    async def test_protocol_recovery_checks_access_and_interruption(self):
        def revoke(prompt, system, kwargs):
            self.update("access", mode="observe")
            return RAW_DSML
        self.host.responses.append(revoke)
        await self.engine.on_event(event("protocol-revoked", "肥鱼帮我查资料", direct=True))
        await self.idle()
        self.assertEqual(len(self.host.calls), 1)
        self.assertEqual(self.host.sent, [])
        self.update("access", mode="active")

        def interrupt(prompt, system, kwargs):
            self.engine.states[metadata()["scope"]].turn.event.set()
            return RAW_DSML
        self.host.responses.append(interrupt)
        await self.engine.on_event(event("protocol-interrupted", "肥鱼帮我查资料", direct=True))
        await self.idle()
        self.assertEqual(len(self.host.calls), 2)
        self.assertEqual(self.host.sent, [])

    async def test_old_protocol_history_is_not_injected_or_learned(self):
        m = metadata()
        s = self.engine.store
        add_message(s, m, "BOT", RAW_DSML, "old-protocol", bot=True, memory_done=False, style_done=True)
        add_message(s, m, "BOT", "普通的历史回答", "old-answer", bot=True, memory_done=True, style_done=True)
        add_message(s, m, "alice", "我喜欢测试", "old-user", memory_done=False, style_done=True)

        def reply(prompt, system, kwargs):
            chat = json.loads(prompt)["chat"]
            self.assertFalse(any(contains_dsml(r["text"]) for r in chat))
            self.assertIn("普通的历史回答", [r["text"] for r in chat])
            return "继续聊吧"

        self.host.responses.append(reply)
        await self.engine.on_event(event("old-history", "肥鱼说说", direct=True))
        await self.idle()
        self.assertEqual([text for _, text in self.host.sent], ["继续聊吧"])

        def learn(prompt, system, kwargs):
            self.assertNotIn("DSML", prompt)
            return '{"memories":[],"affinity":[]}'

        self.host.responses.append(learn)
        self.assertTrue(await self.engine.memory.learn_once(self.engine.config.snapshot(), scope=m["scope"], force=True))
        rows = s.context(m["scope"], 10)
        old = next(r for r in rows if contains_dsml(r["text"]))
        self.assertEqual(old["text"], RAW_DSML)
        self.assertTrue(old["memory_done"])

    async def test_last_send_boundary_blocks_unfiltered_model_output(self):
        async def bad_reply(*args, **kwargs):
            return RAW_DSML
        with patch.object(self.engine, "_model_reply", side_effect=bad_reply):
            await self.engine.on_event(event("sender-guard", "肥鱼帮我查资料", direct=True))
            await self.idle()
        self.assertEqual([text for _, text in self.host.sent], [TOOL_PROTOCOL_FALLBACK])
        self.assertTrue(any(r["code"] == "tool_protocol_blocked_at_send" for r in self.engine.log.recent))

    async def test_model_split_controls_and_non_model_messages(self):
        answer = "第一句话\n第二句话"
        for i, settings in enumerate(({"split_enabled": False}, {"split_enabled": True, "max_parts": 1},
                                     {"max_parts": 5, "split_on_newline": False})):
            self.update("reply", **settings)
            self.host.responses.append(answer)
            await self.engine.on_event(event("split-opt-" + str(i), "肥鱼说说", direct=True))
            await self.idle()
            self.assertEqual(self.host.sent[-1][1], answer)
            self.assertNotIn("想另发一条时换行", self.host.calls[-1]["system"])
        self.update("reply", split_on_newline=True)
        self.update("decision", poke_reply_text="戳一戳\n预设两行")
        await self.engine.on_event(event("fixed-poke", "", poke=True))
        await self.idle()
        self.assertEqual([p for _, p in self.host.sent], [answer] * 3 + ["戳一戳\n预设两行"])
        self.assertEqual(len(self.host.calls), 3)

    async def test_balance_diagnostics_do_not_log_arbitrary_exception_messages(self):
        from astrbot_plugin_fiyu_core.core.telemetry import BalanceCheckError
        self.update("budget", balance_enabled=True, balance_provider="mock")
        for error, reason in ((BalanceCheckError("endpoint"), "Provider 的 API Base 与官方 DeepSeek 接口不匹配"),
                              (ValueError("private fixture text"), "ValueError")):
            async def failed(pid, currency):
                raise error
            self.host.balance = failed
            result = await self.engine.check_balance()
            self.assertEqual(result["reason"], reason)
            self.assertEqual(self.engine.log.recent[-1]["reason"], reason)
            self.assertNotIn("private fixture text", str(self.engine.log.recent[-1]))

    async def test_adapter_decline_stops_tail_and_reports_partial_submission(self):
        attempts = []
        async def send(ev, part):
            attempts.append(part)
            return len(attempts) == 1
        self.host.send = send
        self.host.responses.append("上句\n中句\n尾句")
        await self.engine.on_event(event("declined", "肥鱼说说", direct=True))
        await self.idle()
        self.assertEqual(attempts, ["上句", "中句"])
        history = self.engine.store.recent_bot_messages(metadata()["scope"])
        self.assertEqual([row["text"] for row in history], ["上句"])
        result = next(r for r in self.engine.log.recent if r["code"] == "submitted_to_adapter")
        self.assertEqual((result["planned"], result["parts"], result["status"]), (3, 1, "adapter_declined"))

    async def test_storage_error_after_submission_does_not_resend(self):
        original = self.engine.store.call
        async def fail_bot_history(action, *args, **kwargs):
            if action == "ingest" and args[-1].get("bot"):
                raise OSError("fixture")
            return await original(action, *args, **kwargs)
        self.engine.store.call = fail_bot_history
        self.host.responses.append("已经提交\n尚未提交")
        await self.engine.on_event(event("history-error", "肥鱼说说", direct=True))
        await self.idle()
        self.assertEqual([part for _, part in self.host.sent], ["已经提交"])
        self.assertFalse(any(r["code"] == "send_failed" for r in self.engine.log.recent))
        result = next(r for r in self.engine.log.recent if r["code"] == "submitted_to_adapter")
        self.assertEqual((result["planned"], result["parts"], result["status"]), (2, 1, "history_failed"))

    async def test_per_bubble_history_and_counter_follow_actual_submission(self):
        self.update("reply", split_enabled=False, max_reply_chars=4000)
        answer = "非常具体的测试内容。" * 250
        self.host.responses.append(answer)
        await self.engine.on_event(event("long-single", "肥鱼说说", direct=True))
        await self.idle()
        self.assertEqual(len(self.host.sent), 1)
        self.assertEqual(self.engine.store.recent_bot_messages(metadata()["scope"])[0]["text"], answer)
        self.assertEqual(self.engine.store.totals()["today"]["sent_parts"], 1)

    async def test_adapter_timeout_is_unconfirmed_and_never_retried(self):
        attempts = []
        async def send(ev, part):
            attempts.append(part)
            raise asyncio.TimeoutError()
        self.host.send = send
        self.host.responses.append("开头\n结尾")
        await self.engine.on_event(event("unconfirmed", "肥鱼说说", direct=True))
        await self.idle()
        self.assertEqual(attempts, ["开头"])
        self.assertEqual(self.engine.store.recent_bot_messages(metadata()["scope"]), [])
        result = next(r for r in self.engine.log.recent if r["code"] == "submission_stopped")
        self.assertEqual((result["planned"], result["parts"], result["status"]), (2, 0, "adapter_unconfirmed"))

    async def test_access_change_after_first_bubble_stops_second(self):
        async def send(ev, part):
            self.host.sent.append((ev, part))
            self.update("access", mode="observe")
            return True
        self.host.send = send
        self.host.responses.append("先发这条\n权限改变后这条应取消")
        await self.engine.on_event(event("access-change", "肥鱼说说", direct=True))
        await self.idle()
        self.assertEqual([p for _, p in self.host.sent], ["先发这条"])
        result = next(r for r in self.engine.log.recent if r["code"] == "submitted_to_adapter")
        self.assertEqual((result["planned"], result["parts"], result["status"]), (2, 1, "access_changed"))

    async def test_line_breaks_obey_an_interrupt_between_short_bubbles(self):
        submitted = asyncio.Event()
        async def send(ev, part):
            self.host.sent.append((ev, part))
            submitted.set()
            return True
        self.host.send = send
        self.update("reply", gap_min=0.4, gap_max=0.4)
        self.host.responses.append("前一句\n后一句")
        await self.engine.on_event(event("natural-stop", "肥鱼说说", direct=True))
        await asyncio.wait_for(submitted.wait(), timeout=2)
        await self.engine.on_event(event("stop-here", "别说了", uid="alice"))
        await self.idle()
        self.assertEqual([part for _, part in self.host.sent], ["前一句"])
        result = next(r for r in self.engine.log.recent if r["code"] == "submitted_to_adapter")
        self.assertEqual((result["planned"], result["parts"], result["status"]), (2, 1, "interrupted"))

    async def test_short_list_format_is_independent_of_splitting_and_platform(self):
        original = "8. 测试甲\n9. 测试乙\n10. 测试丙"
        expected = "8、测试甲\n9、测试乙\n10、测试丙"
        self.update("access", allowed_platforms=["qq_official", "qq_official_webhook", "aiocqhttp"])
        for platform, split_enabled, output in (
            ("qq_official", True, expected),
            ("qq_official_webhook", False, expected),
            ("aiocqhttp", True, original),
        ):
            with self.subTest(platform=platform):
                self.update("reply", split_enabled=split_enabled)
                e = event("list-short-" + platform, "肥鱼，复述测试列表", direct=True)
                e.info.update(platform=platform, platform_id=platform + "-test",
                              umo=platform + "-test:GroupMessage:g1")
                before = len(self.host.calls)
                self.host.responses.append(original)
                await self.engine.on_event(e)
                await self.idle()
                self.assertEqual(self.host.sent[-1], (e, output))
                self.assertEqual(len(self.host.calls), before + 1)
        self.assertEqual(len(self.host.sent), 3)


if __name__ == "__main__":
    unittest.main()
