"""Regression tests for scoped user identity, aliases and multi-person memory."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest

from astrbot_plugin_fiyu_core.core.config import defaults
from astrbot_plugin_fiyu_core.core.memory import (
    MemoryService,
    fit_recall_payload,
    manual_proposal,
    mark_named_candidates,
    normalize_proposals,
    transcript,
)
from astrbot_plugin_fiyu_core.core.store import Store, rank_memories
from astrbot_plugin_fiyu_core.core.util import now, scope_key


ALICE = "A" * 32
BOB = "B" * 32
CAROL = "C" * 32


class IdentityTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.path = Path(self.tmp.name) / "fiyu.sqlite3"
        self.store = Store(self.path)
        self.cfg = defaults()
        self.meta = self.session("test-group")
        self.scope = self.meta["scope"]
        self.counter = 0
        self.service = MemoryService(
            self.store, None, SimpleNamespace(embedding_fingerprint=lambda _: ""),
            None, SimpleNamespace(emit=lambda *a, **kw: None),
        )

    def tearDown(self):
        self.tmp.cleanup()

    @staticmethod
    def session(location, *, kind="group", persona="fish"):
        return dict(scope=scope_key("qq-test", kind, location, persona), platform="qq_official",
                    platform_id="qq-test", kind=kind, location=location, persona=persona,
                    umo=f"qq-test:{'GroupMessage' if kind == 'group' else 'FriendMessage'}:{location}")

    def message(self, uid, name, text="聊天", meta=None, **kwargs):
        self.counter += 1
        return self.store.ingest(meta or self.meta, dict(
            uid=uid, name=name, text=text, ts=now(), ext_id=f"m-{self.counter}",
        ) | kwargs)

    def put(self, row, subject=None, refs=None, *, kind="core", title="生日", full="小蓝的生日是7月22日", topic="birthday"):
        p = dict(scope=row["scope"], subject=subject if subject is not None else row["ref"],
                 kind=kind, topic_key=topic, title=title, full_text=full, gist=full,
                 trace=title, importance=90, confidence=1.0, source_ids=[row["seq"]],
                 evidence=[{"n": row["seq"], "speaker": row["ref"], "text": row["text"]}])
        if refs is not None:
            p["participant_refs"] = refs
        return self.store.manual_memory(p)

    async def test_rename_duplicate_missing_and_manual_aliases(self):
        a = self.message(ALICE, "小蓝", "我的生日是7月22日")
        mid = self.put(a)
        b = self.message(ALICE, "大蓝")
        self.assertEqual(a["ref"], b["ref"])
        self.assertIsNone(self.message(ALICE, "过期改名", ext_id=a["ext_id"]))
        no_name = self.message(ALICE, "")
        self.assertEqual(no_name["name"], "大蓝")
        # QQ adapter can also supply the raw opaque ID in place of a nickname.
        self.assertEqual(self.message(ALICE, ALICE)["name"], "大蓝")
        self.message(ALICE, "更旧的昵称", ts=a["ts"] - 100)
        self.assertEqual(self.store.person(self.scope, ALICE)["name"], "大蓝")
        p = self.store.list_people(self.scope, "小蓝")[0]
        self.assertIn("小蓝", p["historical_aliases"])
        self.assertIn("更旧的昵称", p["aliases"])
        self.assertNotIn("过期改名", p["aliases"])
        for q in (ALICE, a["ref"], "小蓝", "大蓝"):
            self.assertEqual([r["id"] for r in self.store.list_memories(self.scope, q)], [mid])
            self.assertEqual(await self.service.resolve_person(self.scope, q, a["ref"]), (a["ref"], None))
        self.store.edit_person(self.scope, ALICE, {**p, "blocked": False, "display_name": "小鲸", "custom_aliases": ["蓝蓝"]})
        self.assertEqual(self.message(ALICE, "第三个昵称")["name"], "小鲸")
        revised = self.store.list_people(self.scope, ALICE)[0]
        self.assertEqual(revised["name"], "第三个昵称")
        self.assertEqual(await self.service.resolve_person(self.scope, "蓝蓝", a["ref"]), (a["ref"], None))
        self.store.edit_person(self.scope, ALICE, {**revised, "blocked": False, "display_name": "", "custom_aliases": []})
        self.assertEqual(self.message(ALICE, "")["name"], "第三个昵称")
        self.assertIsNotNone((await self.service.resolve_person(self.scope, "蓝蓝", a["ref"]))[1])
        self.assertEqual(self.store.list_memories(self.scope, "小鲸")[0]["id"], mid)

    async def test_ambiguous_alias_never_merges_people(self):
        a = self.message(ALICE, "同名")
        self.put(a)
        self.message(ALICE, "新名字")
        b = self.message(BOB, "同名")
        self.put(b, title="爱好", full="喜欢看书", topic="hobby")
        ref, error = await self.service.resolve_person(self.scope, "同名", a["ref"])
        self.assertIsNone(ref)
        self.assertEqual({p["ref"] for p in error["candidates"]}, {a["ref"], b["ref"]})
        self.assertEqual(await self.service.resolve_person(self.scope, "current", a["ref"]), (a["ref"], None))
        for uid, ref in ((ALICE, a["ref"]), (BOB, b["ref"])):
            self.assertEqual(await self.service.resolve_person(self.scope, uid, a["ref"]), (ref, None))

    async def test_multi_person_recall_and_blocked_participants(self):
        b = self.message(BOB, "小白")
        a = self.message(ALICE, "小蓝", "昨天我们开黑拿了冠军", mentioned_uids=[BOB], quoted_uids=[BOB])
        other = self.message(BOB, "异群昵称", meta=self.session("other-group"))
        item = {"subject": "group", "participants": [a["ref"], b["ref"], other["ref"], "Uinvented"],
                "kind": "experience", "key": "群赛", "title": "群赛冠军", "full": "小蓝和小白一起拿了冠军",
                "gist": "一起拿冠军", "trace": "比赛", "importance": 95, "confidence": 0.97,
                "source_ids": [a["seq"]]}
        proposals, _ = normalize_proposals({"memories": [item]}, self.scope, [a], self.cfg["memory"])
        self.assertEqual(set(proposals[0]["participant_refs"]), {a["ref"], b["ref"]})
        mid = self.store.commit_memory(proposals, [], [a["seq"]])[0]
        self.message(BOB, "新小白")
        row = self.store.list_memories(self.scope, BOB)[0]
        self.assertEqual(row["id"], mid)
        self.assertEqual({p["uid"] for p in row["participants"]}, {ALICE, BOB})
        raw = json.loads(transcript([a]))[0]
        self.assertEqual(raw["mentioned"], [b["ref"]])
        self.assertEqual(raw["quoted"], [b["ref"]])
        for name in ("小白", "新小白", BOB, b["ref"]):
            result = await self.service.recall(self.meta, self.cfg, a["ref"], who=name, query="冠军")
            self.assertEqual(result["memories"][0]["id"], mid)
            self.assertNotIn(BOB, json.dumps(result))
        passive = await self.service.auto_context(self.meta, self.cfg, a["ref"], "群赛冠军")
        data = json.loads(passive)
        self.assertEqual(data["subject"], "group")
        self.assertEqual({p["ref"] for p in data["participants"]}, {a["ref"], b["ref"]})
        self.assertNotIn(ALICE, passive)
        self.assertNotIn(BOB, passive)
        self.assertLessEqual(len(passive), self.cfg["memory"]["auto_recall_chars"])
        self.assertEqual(self.store.list_memories(other["scope"], BOB), [])
        with self.assertRaises(ValueError):
            self.store.manual_memory(manual_proposal({**row, "pinned": False, "participant_refs": [other["ref"]]}, self.cfg["memory"]), mid)
        with self.store.connection() as db:
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute("INSERT INTO memory_people VALUES(?,?,?)", (mid, other["scope"], other["ref"]))
        # A block of *any* linked person must prevent recall/indexing, not just the primary subject.
        self.cfg["access"]["blocked_users"] = [BOB]
        self.assertEqual(await self.service.auto_context(self.meta, self.cfg, a["ref"], "群赛冠军"), "")
        self.assertEqual((await self.service.recall(self.meta, self.cfg, a["ref"], query="冠军"))["memories"], [])
        self.assertEqual(self.store.unembedded(self.scope, "fp", blocked_refs=[b["ref"]]), [])
        with self.store.connection() as db:
            db.execute("UPDATE people SET blocked=1 WHERE scope=? AND uid=?", (self.scope, BOB))
        self.assertEqual(self.store.recall_candidates(self.scope, None, None, True, True, None, None), [])
        self.assertEqual(self.store.unembedded(self.scope, "fp"), [])
        self.store.delete_memory(mid)
        with self.store.connection() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM memory_people").fetchone()[0], 0)

    async def test_plain_names_can_be_evidence_candidates_but_not_guesses(self):
        b = self.message(BOB, "小白")
        self.message(BOB, "大白")
        a = self.message(ALICE, "小蓝", "小白与我一起比赛")
        rows = mark_named_candidates([a], self.store.scope_people(self.scope), set())
        self.assertIn(b["ref"], [p["ref"] for p in rows[0]["named_candidates"]])
        item = dict(subject="group", participants=[b["ref"]], kind="experience", key="match", title="比赛",
                    full="小蓝和小白一起比赛", gist="一起比赛", trace="比赛", importance=85, confidence=0.9,
                    source_ids=[a["seq"]])
        proposals, _ = normalize_proposals({"memories": [item]}, self.scope, rows, self.cfg["memory"])
        self.assertEqual(proposals[0]["participant_refs"], [b["ref"]])
        self.message(CAROL, "小白")
        rows = mark_named_candidates([a], self.store.scope_people(self.scope), set())
        self.assertEqual(rows[0]["named_candidates"], [])
        proposals, _ = normalize_proposals({"memories": [item]}, self.scope, rows, self.cfg["memory"])
        self.assertEqual(proposals[0]["participant_refs"], [])

    async def test_v1_schema_migration_preserves_names_memory_and_evidence(self):
        a = self.message(ALICE, "旧名", "以前的证据")
        self.message(ALICE, "新名")
        b = self.message(BOB, "小白")
        mid = self.put(a)
        group_mid = self.put(b, subject="group", kind="experience", title="比赛", full="小白获胜", topic="oldmatch:2026-11-01")
        # Reproduce schema v1 with test-only data. The released v1 zip is tested separately.
        with self.store.connection() as db:
            before = [tuple(r) for r in db.execute("SELECT * FROM memories ORDER BY id")]
            db.execute("DROP TABLE identity_relationships")
            db.execute("DROP TABLE identity_memberships")
            db.execute("DROP TABLE person_identities")
            db.execute("DROP TABLE memory_people")
            db.execute("DROP TABLE person_aliases")
            db.execute("DROP INDEX mem_scoped_id")
            db.execute("ALTER TABLE messages DROP COLUMN identity_refs")
            db.execute("PRAGMA user_version=1")
        upgraded = Store(self.path)
        with upgraded.connection() as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 4)
            self.assertEqual([tuple(r) for r in db.execute("SELECT * FROM memories ORDER BY id")], before)
            self.assertIsNone(db.execute("PRAGMA foreign_key_check").fetchone())
        rows = {r["id"]: r for r in upgraded.list_memories(self.scope)}
        self.assertEqual(rows[mid]["participant_refs"], [a["ref"]])
        self.assertEqual(rows[group_mid]["participant_refs"], [b["ref"]])
        self.assertEqual(upgraded.list_people(self.scope, "旧名")[0]["ref"], a["ref"])
        backups = list(self.path.parent.glob("upgrade_backups/*/*.sqlite3"))
        self.assertEqual(len(backups), 1)
        with closing(sqlite3.connect(backups[0])) as backup:
            self.assertEqual(backup.execute("PRAGMA user_version").fetchone()[0], 1)
        Store(self.path)
        self.assertEqual(len(list(self.path.parent.glob("upgrade_backups/*/*.sqlite3"))), 1)

    async def test_manual_link_edit_preserves_history_and_prior_subject(self):
        a = self.message(ALICE, "小蓝")
        b = self.message(BOB, "小白")
        mid = self.put(a, refs=[b["ref"]])
        self.store.archive_memory(mid, False)
        row = self.store.list_memories(self.scope)[0]
        self.store.manual_memory(manual_proposal({**row, "participant_refs": [], "pinned": False}, self.cfg["memory"]), mid)
        after = self.store.list_memories(self.scope)[0]
        self.assertEqual(after["participant_refs"], [a["ref"]])
        for field in ("clarity", "state", "updated", "importance", "confidence", "revision"):
            self.assertEqual(after[field], row[field])
        history = self.store.memory_versions(mid)
        self.assertEqual(set(history[0]["snapshot"]["participant_refs"]), {a["ref"], b["ref"]})
        ranked = rank_memories(self.store.recall_candidates(self.scope, None, None, True, True, None, None), "生日", None, a["ref"], 5, deep=True)
        self.assertTrue(any(r["state"] == "historical" and b["ref"] in {p["ref"] for p in r["participants"]} for r in ranked))

    async def test_recall_budget_never_splits_identity(self):
        ref = "U139123456789"
        data = {"memories": [{"subject": ref, "participants": [{"ref": ref, "name": "群友", "aliases": ["旧称" * 30]}], "text": "核心记忆" * 37} for _ in range(4)], "note": "检索结果"}
        fitted = fit_recall_payload(data, 500)
        self.assertTrue(fitted["memories"])
        self.assertLessEqual(len(json.dumps(fitted, ensure_ascii=False, separators=(",", ":"))), 500)
        self.assertEqual(fitted["memories"][0]["subject"], ref)
        self.assertEqual(fitted["memories"][0]["participants"][0]["ref"], ref)


if __name__ == "__main__":
    unittest.main()
