"""Administrator-confirmed copies between two personas in the same authorized chat."""
from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import tempfile
from contextlib import closing
from pathlib import Path

from . import identity
from .habits import SENSITIVE_ROUTINES
from .memory import allowed
from .personality import legacy_title
from .store import evidence_refs, identity_name
from .util import clean, dump, normalized, now, unsafe_secret, user_ref


SECTIONS = ("people", "memories", "relationships", "styles", "research")
DEFAULT_SECTIONS = SECTIONS[:3]
PLAN_LIMIT = 1200
MEMORY_FIELDS = (
    "scope", "subject", "kind", "topic_key", "title", "full_text", "gist", "trace",
    "importance", "confidence", "clarity", "state", "pinned", "created", "updated",
    "last_recalled", "recall_count", "valid_until", "source_ids", "evidence", "revision", "archived_at",
)
TEXT_LIMITS = {"title": 140, "full_text": 1200, "gist": 500, "trace": 200}
RELATION_FIELDS = ("role", "affinity", "familiarity_override")


def _rows(db, sql, params=()):
    return [dict(row) for row in db.execute(sql, params)]


def _first(db, sql, params=()):
    row = db.execute(sql, params).fetchone()
    return dict(row) if row else None


def _hash(value):
    return hashlib.sha256(dump(value).encode("utf-8")).hexdigest()


def _stable_preview(value):
    """Passive observation and recall must not invalidate an open preview."""
    if isinstance(value, dict):
        return {k: _stable_preview(v) for k, v in value.items()
                if k not in ("last_seen", "last_recalled", "recall_count")}
    if isinstance(value, (tuple, list)):
        return [_stable_preview(v) for v in value]
    return value


def _chat(session):
    return tuple(session[key] for key in ("platform", "platform_id", "kind", "location"))


def _available_person(person, config):
    return bool(person and not person["blocked"] and person["uid"] not in config["access"]["blocked_users"])


def _identity_matches(db, session, person):
    actual = identity.affiliation(db, session["scope"], person["ref"])
    if not actual:
        return False
    return actual["identity_id"] == identity.global_id(session["platform"], session["platform_id"], person["uid"])


def _put_record(db, table, values, row_id=None):
    # Table and column names are selected exclusively by this module.
    if row_id is not None:
        db.execute(f"UPDATE {table} SET " + ",".join(f"{field}=?" for field in values) + " WHERE id=?",
                   (*values.values(), row_id))
        return row_id
    fields = ",".join(values)
    marks = ",".join("?" for _ in values)
    return int(db.execute(f"INSERT INTO {table}({fields}) VALUES({marks})", tuple(values.values())).lastrowid)


def _snapshot_origin(row, session):
    return {"persona": session["persona"], "scope": session["scope"],
            "memory_id": row["id"], "revision": row["revision"]}


def _origin_evidence(row, session, refs):
    origin = _snapshot_origin(row, session)
    evidence = [{"origin": {**origin, "message_numbers": json.loads(row["source_ids"])}}]
    for previous in json.loads(row["evidence"]):
        if not isinstance(previous, dict):
            continue
        speaker = previous.get("speaker")
        source = previous.get("origin")
        if not isinstance(source, dict):
            source = {**origin, "message_number": previous.get("n"), "speaker": speaker}
        saved = {"origin": source}
        if speaker in refs:
            saved["speaker"] = refs[speaker]["target_ref"]
        elif speaker in ("group", "BOT"):
            saved["speaker"] = speaker
        if isinstance(previous.get("text"), str):
            saved["text"] = previous["text"]
        evidence.append(saved)
    return evidence


def _usable_memory(row, refs, config):
    participants = set(row["participant_refs"]) | set(evidence_refs(row))
    if row["subject"] != "group":
        participants.add(row["subject"])
    if not participants.issubset(refs):
        return False
    if row["kind"] == "habit":
        person = refs.get(row["subject"])
        return bool(config["habits"]["enabled"] and person and person["source"]["habit_opt_in"]
                    and person["target"] and person["target"]["habit_opt_in"])
    return True


def _memory_values(row, source, target, refs, *, target_id=None, text=None):
    values = {field: row[field] for field in MEMORY_FIELDS}
    values["scope"] = target["scope"]
    values["subject"] = "group" if row["subject"] == "group" else refs[row["subject"]]["target_ref"]
    values["source_ids"] = "[]"
    values["evidence"] = dump(_origin_evidence(row, source, refs))
    if target_id is not None:
        values["id"] = target_id
    if text:
        values.update(text)
    return values


def _remapped_refs(row, refs):
    keys = set(row["participant_refs"]) | set(evidence_refs(row))
    if row["subject"] != "group":
        keys.add(row["subject"])
    return sorted({refs[ref]["target_ref"] for ref in keys})


class PersonaMerge:
    def __init__(self, store, config):
        self.store = store
        self.config = config

    def options(self):
        cfg = self.config.snapshot()
        with self.store.connection() as db:
            sessions = _rows(db, "SELECT * FROM sessions ORDER BY persona,kind,location,scope")
        personas = {}
        for row in sessions:
            if allowed(row, cfg):
                personas.setdefault(row["persona"], []).append(
                    {key: row[key] for key in ("scope", "platform", "platform_id", "kind", "location")}
                )
        return {"personas": [{"id": persona, "sessions": chats} for persona, chats in personas.items()]}

    @staticmethod
    def _request(spec):
        source, target = spec.get("source"), spec.get("target")
        scope = spec.get("scope", "")
        sections = spec.get("sections", list(DEFAULT_SECTIONS))
        if (not all(type(x) is str and x.strip() and len(x) <= 400 for x in (source, target))
                or source == target):
            raise ValueError("请选择不同的来源和目标人格")
        if type(scope) is not str or len(scope) > 80:
            raise ValueError("会话标识无效")
        if (type(sections) is not list or not sections or len(sections) > len(SECTIONS)
                or any(type(x) is not str or x not in SECTIONS for x in sections)):
            raise ValueError("请选择预览项目")
        return source, target, scope, tuple(key for key in SECTIONS if key in sections)

    def _pairs(self, db, cfg, source, target, scope):
        rows = _rows(db, "SELECT * FROM sessions WHERE persona IN (?,?) ORDER BY scope", (source, target))
        destinations = {_chat(row): row for row in rows if row["persona"] == target and allowed(row, cfg)}
        return [
            (row, destinations[_chat(row)])
            for row in rows
            if row["persona"] == source and (not scope or row["scope"] == scope)
            and allowed(row, cfg) and _chat(row) in destinations
        ]

    def _people(self, db, cfg, source, target):
        new = {p["uid"]: p for p in _rows(db, "SELECT * FROM people WHERE scope=?", (target["scope"],))}
        source_rows = _rows(db, "SELECT * FROM people WHERE scope=? ORDER BY uid", (source["scope"],))
        refs = {}
        for person in source_rows:
            prior = new.get(person["uid"])
            if not _available_person(person, cfg) or not _identity_matches(db, source, person):
                continue
            if prior and (not _available_person(prior, cfg) or not _identity_matches(db, target, prior)):
                continue
            refs[person["ref"]] = {
                "source": person, "target": prior,
                "target_ref": prior["ref"] if prior else user_ref(target["scope"], person["uid"]),
            }
        return refs

    def _plan(self, db, cfg, spec):
        source_id, target_id, only_scope, sections = self._request(spec)
        pairs = self._pairs(db, cfg, source_id, target_id, only_scope)
        if not pairs:
            raise ValueError("没有平台、实例、会话类型和位置相同的已授权现有会话")
        entries = {}
        contexts = {}
        skipped = {"memory_permission": 0}
        seen_relations = set()

        def add(kind, key, source, target, label, data, status="copy"):
            marker = "persona_merge:" + _hash([kind, key, target["scope"] if target else target_id,
                                             data["origin_revision"]])
            if db.execute("SELECT 1 FROM kv WHERE key=?", (marker,)).fetchone():
                status = "imported"
            entry = {"id": kind + ":" + key, "kind": kind, "status": status, "title": label,
                     "scope": source["scope"] if source else "", "data": data, "marker": marker}
            entries[entry["id"]] = entry
            if len(entries) > PLAN_LIMIT:
                raise ValueError("预览项目过多，请只选择一个匹配会话或减少数据类别")

        for source, target in pairs:
            refs = self._people(db, cfg, source, target)
            contexts[source["scope"]] = (source, target, refs)
            if "people" in sections:
                aliases = _rows(db, "SELECT * FROM person_aliases WHERE scope=? ORDER BY person_ref,alias,kind",
                                (source["scope"],))
                for ref, member in refs.items():
                    member_aliases = [a for a in aliases if a["person_ref"] == ref]
                    label = identity_name(member["source"]["name"], member["source"]["uid"], ref) or ref
                    data = {"ref": ref, "aliases": member_aliases,
                            "origin_revision": _hash(_stable_preview([label, member_aliases]))}
                    add("people", source["scope"] + ":" + ref, source, target, label, data)
            if "memories" in sections:
                for row in _rows(db, "SELECT * FROM memories WHERE scope=? ORDER BY id", (source["scope"],)):
                    row["participant_refs"] = self.store._memory_refs(db, row["id"])
                    if not _usable_memory(row, refs, cfg):
                        skipped["memory_permission"] += 1
                        continue
                    subject = "group" if row["subject"] == "group" else refs[row["subject"]]["target_ref"]
                    existing = _first(db, "SELECT * FROM memories WHERE scope=? AND subject=? AND kind=? AND topic_key=?",
                                      (target["scope"], subject, row["kind"], row["topic_key"]))
                    if existing:
                        existing["participant_refs"] = self.store._memory_refs(db, existing["id"])
                        if self._restricted_target(db, target, existing, cfg):
                            skipped["memory_permission"] += 1
                            continue
                    history = _rows(db, "SELECT * FROM memory_versions WHERE memory_id=? ORDER BY version_id",
                                    (row["id"],))
                    data = {"source": row, "target": existing, "history": history,
                            "origin_revision": _hash([row["revision"], row["participant_refs"], row["state"],
                                                      row["clarity"], row["pinned"], row["valid_until"]])}
                    identical = existing and all(row[k] == existing[k] for k in (
                        *TEXT_LIMITS, "kind", "importance", "confidence", "state", "clarity", "pinned", "valid_until"
                    )) and set(_remapped_refs(row, refs)) == set(existing["participant_refs"])
                    add("memories", str(row["id"]), source, target, row["title"], data,
                        "identical" if identical else "conflict" if existing else "copy")
            if "relationships" in sections:
                for ref, member in refs.items():
                    uid = member["source"]["uid"]
                    gid = identity.global_id(source["platform"], source["platform_id"], uid)
                    local_days = _rows(db, """SELECT day,turns FROM relationship_days
                        WHERE scope=? AND person_ref=? ORDER BY day""", (source["scope"], ref))
                    target_days = _rows(db, """SELECT day,turns FROM relationship_days
                        WHERE scope=? AND person_ref=? ORDER BY day""", (target["scope"], member["target_ref"]))
                    if member["source"]["interactions"] or local_days:
                        data = {"ref": ref, "days": local_days, "target_days": target_days,
                                "origin_revision": _hash([local_days, member["source"]["interactions"],
                                                          member["source"]["last_interaction"]])}
                        add("interactions", source["scope"] + ":" + ref, source, target,
                            member["source"]["name"] or ref, data)
                    if gid in seen_relations:
                        continue
                    seen_relations.add(gid)
                    old = _first(db, "SELECT * FROM identity_relationships WHERE identity_id=? AND persona=?",
                                 (gid, source_id))
                    current = _first(db, "SELECT * FROM identity_relationships WHERE identity_id=? AND persona=?",
                                     (gid, target_id))
                    if not old or current and all(old[k] == current[k] for k in RELATION_FIELDS):
                        continue
                    data = {"ref": ref, "source": old, "target": current, "gid": gid,
                            "origin_revision": old["revision"]}
                    add("relationships", gid, source, target, member["source"]["name"] or ref, data,
                        "conflict" if current else "copy")
            if "styles" in sections:
                for row in _rows(db, "SELECT * FROM styles WHERE scope=? AND status='approved' ORDER BY id",
                                 (source["scope"],)):
                    prior = _first(db, "SELECT * FROM styles WHERE scope=? AND kind=? AND norm=?",
                                   (target["scope"], row["kind"], row["norm"]))
                    history = _rows(db, "SELECT * FROM style_versions WHERE style_id=? ORDER BY version_id",
                                    (row["id"],))
                    data = {"source": row, "target": prior, "history": history, "origin_revision": row["revision"]}
                    add("styles", str(row["id"]), source, target, row["pattern"], data, "identical" if prior else "copy")
        if "research" in sections:
            topic_keys, evidence_keys = set(), set()
            for row in _rows(db, """SELECT * FROM research_notes WHERE persona=?
                                     ORDER BY created DESC,id DESC""", (source_id,)):
                existing = _first(db, """SELECT * FROM research_notes WHERE persona=? AND
                    (topic_key=? OR (evidence_key=? AND evidence_key!='')) ORDER BY id LIMIT 1""",
                                  (target_id, row["topic_key"], row["evidence_key"]))
                duplicate = (existing or row["topic_key"] in topic_keys
                             or row["evidence_key"] in evidence_keys)
                topic_keys.add(row["topic_key"])
                if row["evidence_key"]:
                    evidence_keys.add(row["evidence_key"])
                data = {"source": row, "target": existing, "origin_revision": _hash(row)}
                add("research", str(row["id"]), None, None, row["topic"], data, "identical" if duplicate else "copy")
        revision = _hash(_stable_preview(
            [source_id, target_id, only_scope, sections, contexts, entries, skipped, cfg["access"]]
        ))
        return source_id, target_id, contexts, entries, skipped, revision

    @staticmethod
    def _restricted_target(db, target, row, cfg):
        relevant = set(row["participant_refs"]) | set(evidence_refs(row))
        if row["subject"] != "group":
            relevant.add(row["subject"])
        for ref in relevant:
            person = _first(db, "SELECT * FROM people WHERE scope=? AND ref=?", (target["scope"], ref))
            if not _available_person(person, cfg):
                return True
        return False

    def preview(self, spec):
        with self.config.lock, self.store.connection() as db:
            db.execute("BEGIN")
            source_id, target_id, contexts, entries, skipped, revision = self._plan(db, self.config.snapshot(), spec)
        items = []
        for entry in entries.values():
            item = {key: entry[key] for key in ("id", "kind", "status", "title", "scope")}
            if entry["status"] == "conflict":
                fields = TEXT_LIMITS if entry["kind"] == "memories" else RELATION_FIELDS
                for side in ("source", "target"):
                    item[side] = {key: entry["data"][side][key] for key in fields}
            items.append(item)
        return {
            "source": source_id, "target": target_id, "revision": revision, "items": items, "skipped": skipped,
            "sessions": [{"scope": source["scope"], "target_scope": target["scope"],
                          "kind": source["kind"], "platform": source["platform"],
                          "platform_id": source["platform_id"], "location": source["location"]}
                         for source, target, _ in contexts.values()],
        }

    def _backup(self):
        parent = self.store.path.parent / "persona_merge_backups"
        folder = None
        try:
            parent.mkdir(exist_ok=True)
            folder = Path(tempfile.mkdtemp(prefix="merge-", dir=parent))
            with (closing(sqlite3.connect(self.store.path, timeout=10)) as source,
                  closing(sqlite3.connect(folder / "fiyu-before-merge.sqlite3")) as dest):
                source.backup(dest)
                if (dest.execute("PRAGMA quick_check").fetchone()[0] != "ok"
                        or dest.execute("PRAGMA foreign_key_check").fetchone() is not None):
                    raise ValueError("人格合并备份校验失败")
            if self.config.path.is_file():
                shutil.copy2(self.config.path, folder / "config-before-merge.json")
        except (OSError, sqlite3.Error, ValueError):
            if folder:
                shutil.rmtree(folder, ignore_errors=True)
            raise ValueError("未能创建或验证备份，本次人格合并未执行") from None
        return folder.name

    @staticmethod
    def _decisions(spec, entries):
        if spec.get("confirm") is not True:
            raise ValueError("请确认人格合并预览")
        actions = spec.get("actions")
        if not isinstance(actions, list) or not actions or len(actions) > PLAN_LIMIT:
            raise ValueError("请选择预览中的项目")
        result = []
        selected = set()
        for action in actions:
            if not isinstance(action, dict) or type(action.get("id")) is not str:
                raise ValueError("选择项格式无效")
            key = action["id"]
            entry = entries.get(key)
            if not entry or key in selected or entry["status"] not in ("copy", "conflict"):
                raise ValueError("选择项已变化或不属于当前预览")
            choice = action.get("choice")
            if entry["status"] == "copy" and choice != "copy":
                raise ValueError("非冲突项目仅可确认复制")
            if entry["status"] == "conflict" and choice not in ("source", "manual"):
                raise ValueError("请选择冲突处理方式")
            values = None
            if choice == "manual":
                values = PersonaMerge._manual(entry["kind"], action.get("values"))
                if (entry["kind"] == "memories" and entry["data"]["source"]["kind"] == "habit"
                        and any(SENSITIVE_ROUTINES.search(value) for value in values.values())):
                    raise ValueError("习惯印象不可记录敏感或可追踪的现实行程")
            selected.add(key)
            result.append((entry, choice, values))
        return result

    @staticmethod
    def _manual(kind, values):
        if kind == "memories":
            if (not isinstance(values, dict) or set(values) != set(TEXT_LIMITS)
                    or any(type(values[key]) is not str or len(values[key]) > limit or unsafe_secret(values[key])
                           or not clean(values[key], limit) for key, limit in TEXT_LIMITS.items())):
                raise ValueError("请填写有效的手工合并文本，避免凭证与个人敏感标识")
            return {name: clean(values[name], limit) for name, limit in TEXT_LIMITS.items()}
        if (not isinstance(values, dict) or set(values) != set(RELATION_FIELDS)
                or values["role"] not in ("friend", "member") or type(values["affinity"]) is not int
                or not 0 <= values["affinity"] <= 100 or values["familiarity_override"] not in identity.FAMILIARITY):
            raise ValueError("角色、好感度或熟悉度无效")
        return values

    @staticmethod
    def _person(db, target, member):
        prior = member["source"]
        scope, ref, uid = target["scope"], member["target_ref"], prior["uid"]
        # No membership policy, consent, per-chat display name or note is copied.
        db.execute("""INSERT INTO people(scope,uid,ref,name,last_seen) VALUES(?,?,?,?,?)
                      ON CONFLICT(scope,uid) DO NOTHING""",
                   (scope, uid, ref, identity_name(prior["name"], uid, prior["ref"]) or ref, prior["last_seen"]))
        identity.ensure_member(db, scope, ref)
        return ref

    @staticmethod
    def _alias_owners(db, scope):
        owners = {}
        for text, ref in db.execute("""SELECT name,ref FROM people WHERE scope=?
            UNION SELECT display_name,ref FROM people WHERE scope=?
            UNION SELECT alias,person_ref FROM person_aliases WHERE scope=?""", (scope, scope, scope)):
            key = normalized(text)
            if key:
                owners.setdefault(key, set()).add(ref)
        return owners

    def _copy_people(self, db, source, target, refs, data, counts):
        member = refs[data["ref"]]
        ref = self._person(db, target, member)
        original_owners = self._alias_owners(db, source["scope"])
        target_owners = self._alias_owners(db, target["scope"])
        for r in data["aliases"]:
            alias = identity_name(r["alias"], member["source"]["uid"], data["ref"])
            if not alias or legacy_title(alias):
                continue
            key = normalized(alias)
            if original_owners[key] != {data["ref"]} or target_owners.get(key, {ref}) != {ref}:
                continue
            if db.execute("""INSERT INTO person_aliases(scope,person_ref,alias,kind,first_seen,last_seen)
                             VALUES(?,?,?,?,?,?) ON CONFLICT DO NOTHING""",
                          (target["scope"], ref, alias, "imported", r["first_seen"], r["last_seen"])).rowcount:
                counts["aliases"] += 1
        return ref

    def _copy_memory(self, db, source, target, refs, data, choice, text, cfg, counts):
        row = data["source"]
        prior = data["target"]
        target_id = prior["id"] if prior else None
        versions = []
        for rec in data["history"]:
            snapshot = json.loads(rec["snapshot"])
            if (not isinstance(snapshot, dict) or snapshot.get("scope") != source["scope"]
                    or snapshot.get("id") != row["id"]):
                counts["histories_skipped"] += 1
                continue
            historical = {**row, **snapshot}
            # Older snapshots did not carry participant_refs. Use their own
            # evidence; do not add present-day associations retroactively.
            if "participant_refs" not in snapshot:
                historical["participant_refs"] = evidence_refs(historical)
            if not _usable_memory(historical, refs, cfg):
                counts["histories_skipped"] += 1
                continue
            versions.append((rec, historical))
        allowed_refs = set(_remapped_refs(row, refs))
        all_refs = allowed_refs | {ref for _, historical in versions for ref in _remapped_refs(historical, refs)}
        for member in refs.values():
            if member["target_ref"] in all_refs:
                self._person(db, target, member)
        values = _memory_values(row, source, target, refs)
        evidence = json.loads(values["evidence"])
        evidence[0]["target_seq_cutoff"] = db.execute(
            "SELECT COALESCE(MAX(seq),0) FROM messages WHERE scope=?", (target["scope"],)
        ).fetchone()[0]
        values["evidence"] = dump(evidence)
        if prior:
            if choice == "manual":
                # Keep the target's lifecycle and original target-local evidence.
                evidence = [*json.loads(prior["evidence"]), *evidence]
                values = {**text, "evidence": dump(evidence)}
                allowed_refs.update(prior["participant_refs"])
            values["revision"] = prior["revision"] + 1
        mid = _put_record(db, "memories", values, target_id)
        if prior:
            db.execute("DELETE FROM vectors WHERE memory_id=?", (mid,))
            db.execute("DELETE FROM memory_people WHERE memory_id=?", (mid,))
        self.store._add_memory_refs(db, mid, target["scope"], sorted(allowed_refs))
        for rec, historical in versions:
            snapshot = _memory_values(historical, source, target, refs, target_id=mid)
            snapshot["participant_refs"] = _remapped_refs(historical, refs)
            snapshot["persona_merge_origin"] = {**_snapshot_origin(historical, source),
                                               "version_id": rec["version_id"], "reason": rec["reason"]}
            db.execute("INSERT INTO memory_versions(memory_id,saved,reason,snapshot) VALUES(?,?,?,?)",
                       (mid, rec["saved"], "persona_merge_source", dump(snapshot)))
            counts["histories"] += 1
        if prior:
            db.execute("INSERT INTO memory_versions(memory_id,saved,reason,snapshot) VALUES(?,?,?,?)",
                       (mid, now(), "persona_merge_target", dump(prior)))
        return mid

    def _copy_interactions(self, db, target, member, data):
        ref = self._person(db, target, member)
        for day in data["days"]:
            db.execute("""INSERT INTO relationship_days(scope,person_ref,day,turns) VALUES(?,?,?,?)
                          ON CONFLICT(scope,person_ref,day) DO UPDATE SET turns=MAX(turns,excluded.turns)""",
                       (target["scope"], ref, day["day"], day["turns"]))
        total = db.execute("SELECT COALESCE(SUM(turns),0) FROM relationship_days WHERE scope=? AND person_ref=?",
                           (target["scope"], ref)).fetchone()[0]
        db.execute("""UPDATE people SET interactions=MAX(interactions,?,?),
                      last_interaction=MAX(last_interaction,?) WHERE scope=? AND ref=?""",
                   (member["source"]["interactions"], total, member["source"]["last_interaction"], target["scope"], ref))
        return ref

    @staticmethod
    def _copy_style(db, target, data):
        row = {k: v for k, v in data["source"].items() if k != "id"}
        row["scope"] = target["scope"]
        sid = _put_record(db, "styles", row)
        for version in data["history"]:
            prior = json.loads(version["snapshot"])
            if isinstance(prior, dict) and prior.get("id") == data["source"]["id"]:
                prior.update(id=sid, scope=target["scope"])
                db.execute("INSERT INTO style_versions(style_id,saved,snapshot) VALUES(?,?,?)",
                           (sid, version["saved"], dump(prior)))
        return sid

    @staticmethod
    def _copy_research(db, target_id, data):
        row = {k: v for k, v in data["source"].items() if k not in ("id", "run_id")}
        row["persona"] = target_id
        # The FK requires a unique parent. This provenance marker is excluded
        # from task history, daily counters and scheduling, with zero tokens.
        from .util import day_key
        run_id = _put_record(db, "research_runs", {
            "persona": target_id, "day": day_key(row["created"]), "started": row["created"],
            "finished": row["created"], "state": "migrated", "source": row["source"], "topic": row["topic"],
        })
        row["run_id"] = run_id
        return _put_record(db, "research_notes", row)

    def apply(self, spec):
        with self.config.lock, self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            cfg = self.config.snapshot()
            source_id, target_id, contexts, entries, skipped, revision = self._plan(db, cfg, spec)
            if type(spec.get("revision")) is not str or spec["revision"] != revision:
                raise ValueError("记录或会话权限已经变化，请重新预览")
            actions = self._decisions(spec, entries)
            backup = self._backup()
            counts = {kind: 0 for kind in (*SECTIONS, "interactions", "histories", "histories_skipped", "aliases")}
            order = ("people", "memories", "interactions", "relationships", "styles", "research")
            for entry, choice, text in sorted(actions, key=lambda item: order.index(item[0]["kind"])):
                kind, data = entry["kind"], entry["data"]
                source, target, refs = contexts.get(entry["scope"], (None, None, {}))
                if kind == "people":
                    result_id = self._copy_people(db, source, target, refs, data, counts)
                elif kind == "memories":
                    result_id = self._copy_memory(db, source, target, refs, data, choice, text, cfg, counts)
                elif kind == "interactions":
                    result_id = self._copy_interactions(db, target, refs[data["ref"]], data)
                elif kind == "relationships":
                    self._person(db, target, refs[data["ref"]])
                    values = text if choice == "manual" else data["source"]
                    db.execute("""UPDATE identity_relationships SET role=?,affinity=?,familiarity_override=?,
                                  revision=revision+1 WHERE identity_id=? AND persona=?""",
                               (*(values[k] for k in RELATION_FIELDS), data["gid"], target_id))
                    result_id = data["gid"]
                elif kind == "styles":
                    result_id = self._copy_style(db, target, data)
                elif kind == "research":
                    result_id = self._copy_research(db, target_id, data)
                db.execute("INSERT INTO kv(key,value) VALUES(?,?)", (entry["marker"], dump({
                    "source": source_id, "target": target_id, "source_scope": entry["scope"],
                    "target_scope": target["scope"] if target else "", "item": entry["id"],
                    "origin_revision": data["origin_revision"], "target_id": result_id, "choice": choice,
                    "saved": now(),
                })))
                counts[kind] += 1
            if db.execute("PRAGMA foreign_key_check").fetchone() is not None:
                raise ValueError("关联完整性校验失败，本次写入已撤销")
            return {"backup": backup, "counts": counts, "skipped": {**skipped, "not_selected": len(entries) - len(actions)},
                    "conflicts_resolved": sum(entry["status"] == "conflict" for entry, _, _ in actions)}
