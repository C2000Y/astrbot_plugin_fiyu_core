import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from astrbot_plugin_fiyu_core.core.config import ConfigStore
from astrbot_plugin_fiyu_core.core.persona_merge import PersonaMerge
from astrbot_plugin_fiyu_core.core.store import Store
from astrbot_plugin_fiyu_core.core.util import day_key, dump, now, scope_key, user_ref


class PersonaMergeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)
        self.config = ConfigStore(self.path)
        self.store = Store(self.path / "fiyu.sqlite3")
        self.merger = PersonaMerge(self.store, self.config)
        self.authorize(allowed_groups=["test-team", "test-other"], private_users=["test-alice"])
        self.old = self.session("original")
        self.new = self.session("current")
        self.old_alice = self.message(self.old, "test-alice", "旧昵称", "old-original")
        self.new_alice = self.message(self.new, "test-alice", "新昵称", "new-original")
        self.spec = {"source": "original", "target": "current"}

    def authorize(self, **fields):
        config = self.config.snapshot()
        config["access"].update(mode="active", private_enabled=True, allowed_platforms=["aiocqhttp"], **fields)
        self.config.save(config, self.config.revision())

    def session(self, persona, kind="group", location="test-team", platform_id="test-adapter"):
        meta = {"persona": persona, "platform": "aiocqhttp", "platform_id": platform_id,
                "kind": kind, "location": location, "umo": f"{platform_id}:{kind}:{location}",
                "scope": scope_key(platform_id, kind, location, persona)}
        self.store.session(meta)
        return meta

    def message(self, meta, uid, name, ext_id):
        return self.store.ingest(meta, {"uid": uid, "name": name, "ext_id": ext_id,
                                      "text": f"{name}的合成测试证据", "ts": now()})

    def memory(self, meta, person, text, key="fact", *, people=(), kind="core", **fields):
        row = dict(scope=meta["scope"], subject=person["ref"] if person else "group", kind=kind,
                   topic_key=key, title=text, full_text=text, gist=text, trace=text, importance=88,
                   confidence=.92, pinned=False, participant_refs=list(people), **fields)
        return self.store.manual_memory(row)

    def apply(self, plan, actions, spec=None):
        return self.merger.apply({**(spec or self.spec), "revision": plan["revision"],
                                  "confirm": True, "actions": actions})

    @staticmethod
    def item(plan, kind, item_id):
        return next(item for item in plan["items"] if item["id"] == f"{kind}:{item_id}")

    def target_memories(self):
        return self.store.list_memories(self.new["scope"])

    def test_three_scopes_are_matched_without_crossing_kind_instance_or_authority(self):
        other_old, other_new = [self.session(p, location="test-other") for p in ("original", "current")]
        dm_old, dm_new = [self.session(p, "private", "test-alice") for p in ("original", "current")]
        team_old = self.memory(self.old, None, "群一事实")
        self.memory(other_old, None, "群二事实")
        self.memory(dm_old, None, "私聊事实")
        self.session("original", location="test-not-in-config")
        self.session("current", location="test-not-in-config")
        self.session("original", platform_id="other-adapter")
        before = {meta["scope"]: self.store.context(meta["scope"], 20) for meta in
                  (self.old, self.new, other_old, other_new, dm_old, dm_new)}
        plan = self.merger.preview(self.spec)
        self.assertEqual({s["scope"] for s in plan["sessions"]},
                         {self.old["scope"], other_old["scope"], dm_old["scope"]})
        self.apply(plan, [{"id": f"memories:{team_old}", "choice": "copy"}])
        self.assertEqual([r["title"] for r in self.target_memories()], ["群一事实"])
        self.assertEqual(self.store.list_memories(other_new["scope"]), [])
        self.assertEqual(self.store.list_memories(dm_new["scope"]), [])
        after = {scope: self.store.context(scope, 20) for scope in before}
        self.assertEqual(after, before)

    def test_copy_real_identity_history_evidence_lifecycle_and_backup(self):
        same_name_old = self.message(self.old, "test-bob", "同名", "old-bob")
        same_name_new = self.message(self.new, "test-lee", "同名", "new-lee")
        mid = self.memory(self.old, self.old_alice, "旧事实", people=(same_name_old["ref"],),
                          source_ids=[self.old_alice["seq"], 999999],
                          evidence=[{"n": 999999, "speaker": same_name_old["ref"], "text": "非现存原序号证据"}])
        self.memory(self.old, self.old_alice, "修订事实", people=(same_name_old["ref"],),
                    source_ids=[self.old_alice["seq"], 999999],
                    evidence=[{"n": 999999, "speaker": same_name_old["ref"], "text": "非现存原序号证据"}])
        self.store.archive_memory(mid, False)
        with self.store.connection() as db:
            db.execute("UPDATE memories SET clarity='trace',pinned=1 WHERE id=?", (mid,))
        original = self.store.list_memories(self.old["scope"])
        self.store.save_vectors("source-embedding", [(mid, original[0]["revision"], [1, 2])])
        spec = {**self.spec, "sections": ["memories"]}
        plan = self.merger.preview(spec)
        self.assertEqual(self.target_memories(), [])
        self.assertFalse((self.path / "persona_merge_backups").exists())
        report = self.apply(plan, [{"id": f"memories:{mid}", "choice": "copy"}], spec)
        imported, = self.target_memories()
        self.assertEqual(imported["subject"], self.new_alice["ref"])
        bob_ref = user_ref(self.new["scope"], "test-bob")
        self.assertEqual(set(imported["participant_refs"]), {self.new_alice["ref"], bob_ref})
        self.assertNotIn(same_name_new["ref"], imported["participant_refs"])
        self.assertEqual((imported["state"], imported["pinned"], imported["clarity"]), ("archived", 1, "trace"))
        self.assertEqual(imported["source_ids"], [])
        self.assertEqual(imported["evidence"][1]["origin"]["message_number"], 999999)
        self.assertEqual(imported["evidence"][1]["origin"]["scope"], self.old["scope"])
        self.assertEqual(imported["evidence"][1]["speaker"], bob_ref)
        historical, = self.store.memory_versions(imported["id"])
        self.assertEqual(historical["snapshot"]["full_text"], "旧事实")
        self.assertEqual(historical["snapshot"]["scope"], self.new["scope"])
        self.assertEqual(historical["snapshot"]["subject"], self.new_alice["ref"])
        with self.store.connection() as db:
            self.assertEqual([r["memory_id"] for r in db.execute("SELECT memory_id FROM vectors")], [mid])
            self.assertIsNone(db.execute("PRAGMA foreign_key_check").fetchone())
        self.assertEqual(self.store.list_memories(self.old["scope"]), original)
        backup = self.path / "persona_merge_backups" / report["backup"]
        with closing(sqlite3.connect(backup / "fiyu-before-merge.sqlite3")) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM memories WHERE scope=?", (self.new["scope"],)).fetchone()[0], 0)
            self.assertEqual(db.execute("PRAGMA integrity_check").fetchone(), ("ok",))
        self.assertEqual((backup / "config-before-merge.json").read_bytes(), self.config.path.read_bytes())
        self.assertEqual(self.item(self.merger.preview(spec), "memories", mid)["status"], "imported")
        with self.assertRaises(ValueError):
            self.apply(plan, [{"id": f"memories:{mid}", "choice": "copy"}], spec)

    def test_conflicts_protect_target_by_default_and_allow_manual_text_with_history(self):
        shared = self.memory(self.old, self.old_alice, "来源事实", "conflict")
        kept = self.memory(self.old, self.old_alice, "来源另一个事实", "keep")
        dst = self.memory(self.new, self.new_alice, "目标待合并事实", "conflict")
        self.memory(self.new, self.new_alice, "测试期新增事实", "keep")
        self.store.save_vectors("target-embedding", [(dst, 1, [3, 4])])
        spec = {**self.spec, "sections": ["memories"]}
        plan = self.merger.preview(spec)
        self.assertEqual(self.item(plan, "memories", shared)["status"], "conflict")
        self.assertEqual(self.item(plan, "memories", kept)["target"]["full_text"], "测试期新增事实")
        values = {"title": "手工核对事实", "full_text": "合并正文", "gist": "合并摘要", "trace": "合并印象"}
        report = self.apply(plan, [{"id": f"memories:{shared}", "choice": "manual", "values": values}], spec)
        self.assertEqual(report["conflicts_resolved"], 1)
        target = {m["topic_key"]: m for m in self.target_memories()}
        self.assertEqual(target["keep"]["full_text"], "测试期新增事实")
        self.assertEqual(target["conflict"]["full_text"], "合并正文")
        self.assertEqual(target["conflict"]["id"], dst)
        self.assertEqual(self.store.memory_versions(dst)[0]["snapshot"]["full_text"], "目标待合并事实")
        with self.store.connection() as db:
            self.assertIsNone(db.execute("SELECT 1 FROM vectors WHERE memory_id=?", (dst,)).fetchone())

    def test_permission_change_after_preview_and_consent_is_not_copied(self):
        self.store.set_habit_consent(self.old["scope"], "test-alice", True)
        with self.store.connection() as db:
            habit = self.store._insert_memory(db, dict(scope=self.old["scope"], subject=self.old_alice["ref"],
                                                      kind="habit", topic_key="game", title="常分享棋类",
                                                      full_text="本人常分享棋类活动", gist="棋类活动", trace="棋类",
                                                      importance=35, confidence=.91, valid_until=now() + 86400))
        forbidden = self.merger.preview(self.spec)
        self.assertIsNone(next((r for r in forbidden["items"] if r["id"] == f"memories:{habit}"), None))
        self.assertEqual(forbidden["skipped"]["memory_permission"], 1)
        self.store.set_habit_consent(self.new["scope"], "test-alice", True)
        plan = self.merger.preview(self.spec)
        self.store.set_habit_consent(self.new["scope"], "test-alice", False)
        with self.assertRaisesRegex(ValueError, "重新预览"):
            self.apply(plan, [{"id": f"memories:{habit}", "choice": "copy"}])
        self.assertFalse((self.path / "persona_merge_backups").exists())
        self.store.set_habit_consent(self.new["scope"], "test-alice", True)
        plan = self.merger.preview(self.spec)
        self.authorize(allowed_groups=[])
        with self.assertRaisesRegex(ValueError, "已授权"):
            self.apply(plan, [{"id": f"memories:{habit}", "choice": "copy"}])
        self.assertEqual(self.target_memories(), [])

    def test_model_proposal_prepared_before_merge_cannot_overwrite_newly_merged_fact(self):
        mid = self.memory(self.old, self.old_alice, "合并后的事实")
        spec = {**self.spec, "sections": ["memories"]}
        plan = self.merger.preview(spec)
        self.apply(plan, [{"id": f"memories:{mid}", "choice": "copy"}], spec)
        proposed = {
            "scope": self.new["scope"], "subject": self.new_alice["ref"], "kind": "core", "topic_key": "fact",
            "title": "落后批次的事实", "full_text": "落后批次的事实", "gist": "落后摘要", "trace": "落后印象",
            "importance": 88, "confidence": 0.92, "confirmed_update": True,
            "source_ids": [self.new_alice["seq"]],
        }
        self.assertFalse(self.store.remember_explicit(proposed)["saved"])
        self.assertEqual(self.target_memories()[0]["title"], "合并后的事实")
        current = self.message(self.new, "test-alice", "新昵称", "authorized-correction")
        proposed.update(source_ids=[current["seq"]])
        self.assertTrue(self.store.remember_explicit(proposed)["saved"])
        self.assertEqual(self.target_memories()[0]["title"], "落后批次的事实")

    def test_revoking_target_consent_after_import_removes_only_target_habit(self):
        for meta in (self.old, self.new):
            self.store.set_habit_consent(meta["scope"], "test-alice", True)
        with self.store.connection() as db:
            habit = self.store._insert_memory(db, dict(scope=self.old["scope"], subject=self.old_alice["ref"],
                                                      kind="habit", topic_key="chess", title="常分享棋类",
                                                      full_text="本人常分享棋类活动", gist="棋类活动", trace="棋类",
                                                      importance=35, confidence=.91, valid_until=now() + 86400))
        plan = self.merger.preview(self.spec)
        self.apply(plan, [{"id": f"memories:{habit}", "choice": "copy"}])
        self.assertEqual(self.target_memories()[0]["kind"], "habit")
        self.store.set_habit_consent(self.new["scope"], "test-alice", False)
        self.assertEqual(self.target_memories(), [])
        self.assertTrue(self.store.person(self.old["scope"], "test-alice")["habit_opt_in"])
        self.assertEqual(self.store.list_memories(self.old["scope"])[0]["id"], habit)

    def test_relation_interactions_alias_and_approved_public_note_without_quota_usage(self):
        with self.store.connection() as db:
            db.execute("UPDATE identity_relationships SET affinity=80 WHERE persona='original'")
            db.execute("UPDATE identity_relationships SET affinity=70 WHERE persona='current'")
            for meta, person, turns in ((self.old, self.old_alice, 3), (self.new, self.new_alice, 2)):
                db.execute("UPDATE people SET interactions=?,last_interaction=? WHERE scope=?",
                           (turns, now(), meta["scope"]))
                db.execute("INSERT INTO relationship_days VALUES(?,?,?,?)",
                           (meta["scope"], person["ref"], day_key(), turns))
            self.store._record_alias(db, self.old["scope"], self.old_alice["ref"], "旧别名", "manual", now())
            db.execute("UPDATE people SET deny_keywords=?,display_name='目标称呼' WHERE scope=?",
                       (dump(["target"],), self.new["scope"]))
        self.store.commit_styles(self.old["scope"], [
            {"kind": "slang", "meaning": "合成义项", "example": "合成匿名", "pattern": "已批准", "confidence": .99}
        ], [], auto=True)
        self.store.commit_styles(self.old["scope"], [
            {"kind": "slang", "meaning": "未审义项", "example": "合成匿名", "pattern": "待批准", "confidence": .9}
        ], [])
        run_id = self.store.claim_research("original", 1, 0)
        self.store.finish_research(run_id, "completed", "dota_news", "合成公开主题", note={
            "topic_key": "test-public", "evidence_key": "test-evidence", "facts": [],
            "opinion": {}, "sources": [], "expires": now() + 3600,
        })
        spec = {**self.spec, "sections": ["people", "relationships", "styles", "research"]}
        plan = self.merger.preview(spec)
        actions = [{"id": i["id"], "choice": "copy"} for i in plan["items"] if i["status"] == "copy"]
        relation = next(i for i in plan["items"] if i["kind"] == "relationships")
        actions.append({"id": relation["id"], "choice": "manual",
                        "values": {"role": "friend", "affinity": 75, "familiarity_override": "close"}})
        self.apply(plan, actions, spec)
        target = self.store.person(self.new["scope"], "test-alice")
        self.assertEqual((target["affinity"], target["interactions"], target["display_name"]),
                         (75, 3, "目标称呼"))
        self.assertEqual(json.loads(target["deny_keywords"]), ["target"])
        self.assertIn("旧别名", self.store.list_people(self.new["scope"])[0]["historical_aliases"])
        self.assertEqual([row["pattern"] for row in self.store.list_styles(self.new["scope"])], ["已批准"])
        notes = self.store.research_notes("current")
        self.assertEqual([(note["source"], note["topic"]) for note in notes], [("dota_news", "合成公开主题")])
        self.assertEqual(self.store.research_overview()["today_runs"], 1)
        self.assertEqual(len(self.store.research_runs()), 1)
        self.assertIsNotNone(self.store.claim_research("current", 2, 0))

    def test_stale_revision_backup_failure_and_partial_write_rollback(self):
        mid = self.memory(self.old, self.old_alice, "待复制事实")
        plan = self.merger.preview(self.spec)
        self.memory(self.new, self.new_alice, "新事实")
        actions = [{"id": f"memories:{mid}", "choice": "copy"}]
        with self.assertRaisesRegex(ValueError, "重新预览"):
            self.apply(plan, actions)
        # Reset a conflicting target and prepare an actual insertion.
        self.store.delete_memory(self.target_memories()[0]["id"])
        self.store.commit_styles(self.old["scope"], [
            {"kind": "slang", "meaning": "解释", "example": "例子", "pattern": "表达", "confidence": .99}
        ], [], auto=True)
        spec = {**self.spec, "sections": ["memories", "styles"]}
        plan = self.merger.preview(spec)
        actions += [{"id": next(i["id"] for i in plan["items"] if i["kind"] == "styles"), "choice": "copy"}]
        with patch.object(self.merger, "_backup", side_effect=ValueError("模拟备份失败")):
            with self.assertRaisesRegex(ValueError, "备份失败"):
                self.apply(plan, actions, spec)
        self.assertEqual(self.target_memories(), [])
        with patch.object(self.merger, "_copy_style", side_effect=sqlite3.OperationalError("模拟写入失败")):
            with self.assertRaises(sqlite3.OperationalError):
                self.apply(plan, actions, spec)
        self.assertEqual(self.target_memories(), [])
        self.assertEqual(self.merger.preview(spec)["revision"], plan["revision"])
