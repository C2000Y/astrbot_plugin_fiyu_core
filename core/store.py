from __future__ import annotations

import asyncio
import hashlib
import json
import math
import sqlite3
import shutil
import tempfile
import threading
from contextlib import closing, contextmanager
from pathlib import Path
from typing import Any

from . import identity
from .util import MAX_REPLY_CHARS, clean, day_key, dump, entity_key, lexical, normalized, now, user_ref, parse_time
from .personality import legacy_title, legacy_reference, replace_legacy, familiarity

KINDS = ("core", "profile", "experience", "ordinary", "temporary", "habit")
STATES = ("active", "archived", "expired")
CLARITY_ORDER = {"vivid": 0, "clear": 1, "fuzzy": 2, "trace": 3}
SCHEMA_VERSION = 4

DDL = """
CREATE TABLE IF NOT EXISTS sessions (
 scope TEXT PRIMARY KEY, platform TEXT NOT NULL, platform_id TEXT NOT NULL,
 kind TEXT NOT NULL, location TEXT NOT NULL, persona TEXT NOT NULL, umo TEXT NOT NULL,
 last_seen REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS people (
 scope TEXT NOT NULL, uid TEXT NOT NULL, ref TEXT NOT NULL, name TEXT NOT NULL,
 display_name TEXT NOT NULL DEFAULT '', note TEXT NOT NULL DEFAULT '',
 role TEXT NOT NULL DEFAULT 'member', affinity INTEGER NOT NULL DEFAULT 50,
 blocked INTEGER NOT NULL DEFAULT 0, deny_keywords TEXT NOT NULL DEFAULT '[]',
 deny_reply TEXT NOT NULL DEFAULT '这个我就不回答啦。', last_seen REAL NOT NULL,
 PRIMARY KEY(scope,uid), UNIQUE(scope,ref), FOREIGN KEY(scope) REFERENCES sessions(scope)
);
CREATE TABLE IF NOT EXISTS person_aliases (
 scope TEXT NOT NULL, person_ref TEXT NOT NULL, alias TEXT NOT NULL,
 kind TEXT NOT NULL CHECK(kind IN ('observed','display','imported','manual')),
 first_seen REAL NOT NULL, last_seen REAL NOT NULL,
 PRIMARY KEY(scope,person_ref,alias,kind),
 FOREIGN KEY(scope,person_ref) REFERENCES people(scope,ref) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS alias_scope_name ON person_aliases(scope,alias);
CREATE TABLE IF NOT EXISTS messages (
 seq INTEGER PRIMARY KEY AUTOINCREMENT, scope TEXT NOT NULL, ext_id TEXT NOT NULL,
 uid TEXT NOT NULL, ref TEXT NOT NULL, name TEXT NOT NULL, text TEXT NOT NULL,
 ts REAL NOT NULL, directed INTEGER NOT NULL DEFAULT 0, bot INTEGER NOT NULL DEFAULT 0,
 image_count INTEGER NOT NULL DEFAULT 0, explicit INTEGER NOT NULL DEFAULT 0,
 memory_done INTEGER NOT NULL DEFAULT 0, style_done INTEGER NOT NULL DEFAULT 0,
 identity_refs TEXT NOT NULL DEFAULT '{}',
 UNIQUE(scope,ext_id), FOREIGN KEY(scope) REFERENCES sessions(scope)
);
CREATE INDEX IF NOT EXISTS msg_scope_seq ON messages(scope,seq);
CREATE INDEX IF NOT EXISTS msg_pending_mem ON messages(memory_done,scope,seq);
CREATE INDEX IF NOT EXISTS msg_pending_style ON messages(style_done,scope,seq);
CREATE TABLE IF NOT EXISTS memories (
 id INTEGER PRIMARY KEY AUTOINCREMENT, scope TEXT NOT NULL, subject TEXT NOT NULL,
 kind TEXT NOT NULL, topic_key TEXT NOT NULL, title TEXT NOT NULL,
 full_text TEXT NOT NULL, gist TEXT NOT NULL, trace TEXT NOT NULL,
 importance INTEGER NOT NULL, confidence REAL NOT NULL,
 clarity TEXT NOT NULL DEFAULT 'vivid', state TEXT NOT NULL DEFAULT 'active',
 pinned INTEGER NOT NULL DEFAULT 0, created REAL NOT NULL, updated REAL NOT NULL,
 last_recalled REAL, recall_count INTEGER NOT NULL DEFAULT 0, valid_until REAL,
 source_ids TEXT NOT NULL DEFAULT '[]', evidence TEXT NOT NULL DEFAULT '[]',
 revision INTEGER NOT NULL DEFAULT 1, archived_at REAL,
 FOREIGN KEY(scope) REFERENCES sessions(scope)
);
CREATE INDEX IF NOT EXISTS mem_scope ON memories(scope,state,subject,kind,updated);
CREATE UNIQUE INDEX IF NOT EXISTS mem_unique ON memories(scope,subject,kind,topic_key);
CREATE UNIQUE INDEX IF NOT EXISTS mem_scoped_id ON memories(id,scope);
CREATE TABLE IF NOT EXISTS memory_people (
 memory_id INTEGER NOT NULL, scope TEXT NOT NULL, person_ref TEXT NOT NULL,
 PRIMARY KEY(memory_id,person_ref),
 FOREIGN KEY(memory_id,scope) REFERENCES memories(id,scope) ON DELETE CASCADE,
 FOREIGN KEY(scope,person_ref) REFERENCES people(scope,ref) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS mem_people_scope_ref ON memory_people(scope,person_ref,memory_id);
CREATE TABLE IF NOT EXISTS memory_versions (
 version_id INTEGER PRIMARY KEY AUTOINCREMENT, memory_id INTEGER NOT NULL,
 saved REAL NOT NULL, reason TEXT NOT NULL, snapshot TEXT NOT NULL,
 FOREIGN KEY(memory_id) REFERENCES memories(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS vectors (
 memory_id INTEGER PRIMARY KEY, fingerprint TEXT NOT NULL, revision INTEGER NOT NULL,
 vector TEXT NOT NULL, FOREIGN KEY(memory_id) REFERENCES memories(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS styles (
 id INTEGER PRIMARY KEY AUTOINCREMENT, scope TEXT NOT NULL, kind TEXT NOT NULL,
 pattern TEXT NOT NULL, norm TEXT NOT NULL, meaning TEXT NOT NULL, example TEXT NOT NULL,
 confidence REAL NOT NULL, status TEXT NOT NULL, count INTEGER NOT NULL DEFAULT 1,
 created REAL NOT NULL, updated REAL NOT NULL, revision INTEGER NOT NULL DEFAULT 1,
 UNIQUE(scope,kind,norm), FOREIGN KEY(scope) REFERENCES sessions(scope)
);
CREATE INDEX IF NOT EXISTS style_scope ON styles(scope,status,kind);
CREATE TABLE IF NOT EXISTS style_versions (
 version_id INTEGER PRIMARY KEY AUTOINCREMENT, style_id INTEGER NOT NULL,
 saved REAL NOT NULL, snapshot TEXT NOT NULL,
 FOREIGN KEY(style_id) REFERENCES styles(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS daily_events (
 day TEXT NOT NULL, scope TEXT NOT NULL, kind TEXT NOT NULL, n INTEGER NOT NULL,
 PRIMARY KEY(day,scope,kind)
);
CREATE TABLE IF NOT EXISTS usage (
 day TEXT NOT NULL, module TEXT NOT NULL, provider TEXT NOT NULL,
 calls INTEGER NOT NULL DEFAULT 0, failures INTEGER NOT NULL DEFAULT 0,
 input_other INTEGER NOT NULL DEFAULT 0, input_cached INTEGER NOT NULL DEFAULT 0,
 output INTEGER NOT NULL DEFAULT 0, estimated INTEGER NOT NULL DEFAULT 0,
 elapsed_ms INTEGER NOT NULL DEFAULT 0,
 PRIMARY KEY(day,module,provider)
);
CREATE TABLE IF NOT EXISTS image_cache (
 cache_key TEXT PRIMARY KEY, description TEXT NOT NULL, ts REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS relationship_days (
 scope TEXT NOT NULL, person_ref TEXT NOT NULL, day TEXT NOT NULL, turns INTEGER NOT NULL DEFAULT 1,
 PRIMARY KEY(scope,person_ref,day),
 FOREIGN KEY(scope,person_ref) REFERENCES people(scope,ref) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS habit_observations (
 scope TEXT NOT NULL, person_ref TEXT NOT NULL, topic_key TEXT NOT NULL,
 activity TEXT NOT NULL, category TEXT NOT NULL, period TEXT NOT NULL,
 day TEXT NOT NULL, source_seq INTEGER NOT NULL, signal TEXT NOT NULL, confidence REAL NOT NULL,
 PRIMARY KEY(scope,person_ref,topic_key,day),
 FOREIGN KEY(scope,person_ref) REFERENCES people(scope,ref) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS habit_days ON habit_observations(day);
CREATE TABLE IF NOT EXISTS research_runs (
 id INTEGER PRIMARY KEY AUTOINCREMENT, persona TEXT NOT NULL, day TEXT NOT NULL,
 started REAL NOT NULL, finished REAL, state TEXT NOT NULL, source TEXT NOT NULL DEFAULT '',
 topic TEXT NOT NULL DEFAULT '', reason TEXT NOT NULL DEFAULT '', tokens INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS research_day ON research_runs(day,started);
CREATE TABLE IF NOT EXISTS research_notes (
 id INTEGER PRIMARY KEY AUTOINCREMENT, run_id INTEGER NOT NULL UNIQUE, persona TEXT NOT NULL,
 source TEXT NOT NULL, topic TEXT NOT NULL, topic_key TEXT NOT NULL, evidence_key TEXT NOT NULL,
 facts TEXT NOT NULL, opinion TEXT NOT NULL, sources TEXT NOT NULL,
 created REAL NOT NULL, expires REAL NOT NULL, state TEXT NOT NULL DEFAULT 'active',
 FOREIGN KEY(run_id) REFERENCES research_runs(id)
);
CREATE INDEX IF NOT EXISTS research_persona ON research_notes(persona,state,created);
CREATE TABLE IF NOT EXISTS relay_jobs (
 id INTEGER PRIMARY KEY AUTOINCREMENT, scope TEXT NOT NULL, uid TEXT NOT NULL,
 request_seq INTEGER NOT NULL, day TEXT NOT NULL, alias TEXT NOT NULL, target_umo TEXT NOT NULL,
 target_scope TEXT NOT NULL, body TEXT NOT NULL, status TEXT NOT NULL,
 created REAL NOT NULL, expires REAL NOT NULL, finished REAL,
 UNIQUE(scope,request_seq), FOREIGN KEY(scope) REFERENCES sessions(scope)
);
CREATE INDEX IF NOT EXISTS relay_scope_day ON relay_jobs(scope,uid,day);
"""


def dictrow(row):
    return dict(row) if row is not None else None


def identity_name(value, uid, ref):
    """Missing names and adapter ID placeholders are not real nicknames."""
    name = clean(value, 80)
    if not name or name in (uid, ref) or "[已隐藏]" in name or name == "[链接]":
        return ""
    return name


def message_identity_refs(row):
    try:
        links = row.get("identity_refs", {})
        if isinstance(links, str):
            links = json.loads(links)
        return {
            k: [v for v in links.get(k, []) if isinstance(v, str)]
            for k in ("mentioned", "quoted")
            if isinstance(links, dict) and isinstance(links.get(k), list) and links[k]
        }
    except (TypeError, ValueError):
        return {}


def evidence_refs(row):
    """Only recorded evidence speaker refs; never infer an ID from old prose."""
    evidence = row.get("evidence", [])
    try:
        if isinstance(evidence, str):
            evidence = json.loads(evidence)
    except (TypeError, ValueError):
        return []
    if not isinstance(evidence, list):
        return []
    return [e["speaker"] for e in evidence if isinstance(e, dict)
            and isinstance(e.get("speaker"), str) and e["speaker"] not in ("BOT", "group")]


def compact_person(person, *, alias_limit=3):
    """The model receives stable short refs, never the platform UID."""
    uid, ref = person.get("uid", ""), person["ref"]
    names = (identity_name(person.get(k), uid, ref) for k in ("display_name", "common_display_name", "name"))
    name = next((n for n in names if n and not legacy_title(n)), ref)
    out = {"ref": person["ref"], "name": name}
    aliases = list(dict.fromkeys(
        [person.get("name", ""), *person.get("custom_aliases", []), *person.get("aliases", [])]
    ))
    chosen, remaining = [], 120
    for a in aliases:
        safe = identity_name(a, uid, ref)
        if safe and not legacy_title(safe) and safe != name and len(safe) <= remaining and len(chosen) < alias_limit:
            chosen.append(safe)
            remaining -= len(safe) + 3
    if chosen:
        out["aliases"] = chosen
    return out


def visible_text(row: dict, deep=False) -> str:
    if deep:
        return row["full_text"]
    clarity = row["clarity"]
    if clarity == "vivid":
        return row["full_text"]
    if clarity in ("clear", "fuzzy"):
        return row["gist"]
    return row["trace"] or row["gist"]


def memory_target(row: dict, settings: dict, ts: float) -> tuple[str, str]:
    """Importance is a property of the fact/event, not a time-decaying score."""
    if row["state"] != "active":
        return row["clarity"], row["state"]
    if row["kind"] in ("temporary", "habit") and row["valid_until"] and ts >= row["valid_until"]:
        return row["clarity"], "expired"
    if row["pinned"]:
        return "vivid", "active"
    age = (
        max(0, ts - max(row["created"], row["updated"], row["last_recalled"] or 0))
        / 86400
    )
    kind = row["kind"]

    def keep_detail_limit(candidate):
        # Recalling a gist may keep it accessible, but cannot recreate missing details.
        return max((row["clarity"], candidate), key=lambda x: CLARITY_ORDER.get(x, 0))

    if kind in ("core", "profile"):
        n = settings["core_blur_after_days"] if kind == "core" else 0
        return (
            keep_detail_limit("clear" if age >= n else "vivid") if n else "vivid"
        ), "active"
    if kind in ("temporary", "habit"):
        return "vivid", "active"
    if kind == "ordinary":
        if age >= settings["ordinary_days"]:
            return "trace", "archived"
        return keep_detail_limit("fuzzy" if age >= 1 else "vivid"), "active"
    protected = row["importance"] >= settings["important_threshold"]
    if age >= settings["experience_archive_days"]:
        return (
            (keep_detail_limit("fuzzy"), "active")
            if protected
            else ("trace", "archived")
        )
    if age >= settings["experience_fuzzy_days"]:
        return keep_detail_limit("fuzzy"), "active"
    if age >= settings["experience_clear_days"]:
        return keep_detail_limit("clear"), "active"
    return keep_detail_limit("vivid"), "active"


class Store:
    """Small SQLite store. Every operation runs in a worker thread from asyncio."""

    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.RLock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1, 2, 3, SCHEMA_VERSION):
                raise ValueError(f"不支持的 Fiyu 数据库版本：{version}")
            db.execute("PRAGMA journal_mode=WAL")
            if version in (1, 2, 3):
                # Snapshot committed pages, including WAL. Failure stops the upgrade.
                backup_dir = path.parent / "upgrade_backups"
                backup_dir.mkdir(exist_ok=True)
                prefix = {1:"v0.1.0-",2:"v0.1.2-",3:"v0.2.0-"}[version]
                folder = Path(tempfile.mkdtemp(prefix=prefix, dir=backup_dir))
                with closing(sqlite3.connect(folder / "fiyu-before-v0.2.1.sqlite3")) as backup:
                    db.backup(backup)
                    if backup.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                        raise ValueError("升级前数据库备份校验失败")
                if (path.parent/"config.json").is_file():
                    shutil.copy2(path.parent/"config.json",folder/"config-before-v0.2.1.json")
            # executescript commits an existing transaction, so BEGIN belongs inside it.
            db.executescript("BEGIN IMMEDIATE;\n" + DDL + identity.DDL)
            if version < 3:
                columns = {r[1] for r in db.execute("PRAGMA table_info(messages)")}
                if "identity_refs" not in columns:
                    db.execute("ALTER TABLE messages ADD COLUMN identity_refs TEXT NOT NULL DEFAULT '{}'")
                if version < 2:
                    self._backfill_identity(db)
                columns = {r[1] for r in db.execute("PRAGMA table_info(people)")}
                for col, definition in (
                    ("interactions", "INTEGER NOT NULL DEFAULT 0"),
                    ("last_interaction", "REAL NOT NULL DEFAULT 0"),
                    ("familiarity_override", "TEXT NOT NULL DEFAULT 'auto'"),
                    ("habit_opt_in", "INTEGER NOT NULL DEFAULT 0"),
                    ("habit_since", "REAL NOT NULL DEFAULT 0"),
                ):
                    if col not in columns:
                        db.execute(f"ALTER TABLE people ADD COLUMN {col} {definition}")
                # Only retire the role label; preserve affinity, IDs, links and all original prose.
                db.execute("UPDATE people SET role='friend' WHERE role='owner'")
                for p in db.execute("SELECT scope,uid,ref,name,display_name FROM people").fetchall():
                    if legacy_title(p["display_name"]):
                        self._record_alias(db,p["scope"],p["ref"],p["display_name"],"imported",now())
                        db.execute("UPDATE people SET display_name='' WHERE scope=? AND uid=?", (p["scope"], p["uid"]))
                for a in db.execute("SELECT * FROM person_aliases WHERE kind='manual'").fetchall():
                    if legacy_title(a["alias"]):
                        self._record_alias(db,a["scope"],a["person_ref"],a["alias"],"imported",a["first_seen"])
                        self._record_alias(db,a["scope"],a["person_ref"],a["alias"],"imported",a["last_seen"])
                        db.execute("DELETE FROM person_aliases WHERE scope=? AND person_ref=? AND alias=? AND kind='manual'",
                                   (a["scope"],a["person_ref"],a["alias"]))
            if version < SCHEMA_VERSION:
                identity.backfill(db)
                if db.execute("PRAGMA foreign_key_check").fetchone() is not None:
                    raise ValueError("数据库身份关联升级校验失败")
                db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            # Uncertain network submissions are never automatically retried after restart.
            db.execute("UPDATE relay_jobs SET status='unknown',body='',finished=? WHERE status='sending'", (now(),))
            db.execute("UPDATE relay_jobs SET status='cancelled',body='',finished=? WHERE status='drafting'", (now(),))
            db.execute("UPDATE research_runs SET state='interrupted',finished=?,reason='实例重启' WHERE state='running'", (now(),))

    @staticmethod
    def _record_alias(db, scope, ref, alias, kind, seen):
        if alias:
            db.execute(
                """INSERT INTO person_aliases(scope,person_ref,alias,kind,first_seen,last_seen)
                VALUES(?,?,?,?,?,?) ON CONFLICT(scope,person_ref,alias,kind) DO UPDATE SET
                first_seen=MIN(person_aliases.first_seen,excluded.first_seen),
                last_seen=MAX(person_aliases.last_seen,excluded.last_seen)""",
                (scope, ref, alias, kind, seen, seen),
            )

    def _backfill_identity(self, db):
        for p in db.execute("SELECT scope,uid,ref,name,display_name,last_seen FROM people"):
            for col, kind in (("name", "observed"), ("display_name", "display")):
                self._record_alias(db, p["scope"], p["ref"], identity_name(p[col], p["uid"], p["ref"]), kind, p["last_seen"])
        for p in db.execute("""SELECT m.scope,m.uid,m.ref,m.name,MIN(m.ts) first_ts,MAX(m.ts) last_ts
                FROM messages m JOIN people p ON p.scope=m.scope AND p.uid=m.uid AND p.ref=m.ref
                WHERE m.bot=0 GROUP BY m.scope,m.uid,m.ref,m.name"""):
            alias = identity_name(p["name"], p["uid"], p["ref"])
            self._record_alias(db, p["scope"], p["ref"], alias, "observed", p["first_ts"])
            self._record_alias(db, p["scope"], p["ref"], alias, "observed", p["last_ts"])
        for m in db.execute("SELECT id,scope,subject,evidence FROM memories"):
            refs = self._valid_refs(db, m["scope"], [m["subject"], *evidence_refs(dict(m))])
            self._add_memory_refs(db, m["id"], m["scope"], refs)

    @staticmethod
    def _valid_refs(db, scope, refs):
        return list(dict.fromkeys(ref for ref in refs if isinstance(ref, str)
            and ref not in ("group", "BOT") and db.execute(
                "SELECT 1 FROM people WHERE scope=? AND ref=?", (scope, ref)
            ).fetchone()))

    @staticmethod
    def _add_memory_refs(db, mid, scope, refs):
        db.executemany("INSERT OR IGNORE INTO memory_people(memory_id,scope,person_ref) VALUES(?,?,?)",
                       ((mid, scope, ref) for ref in refs))

    @staticmethod
    def _memory_refs(db, mid):
        return [r[0] for r in db.execute("SELECT person_ref FROM memory_people WHERE memory_id=? ORDER BY person_ref", (mid,))]

    @contextmanager
    def connection(self):
        with self.lock:
            db = sqlite3.connect(self.path, timeout=10)
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("PRAGMA busy_timeout=10000")
            try:
                yield db
                db.commit()
            except BaseException:
                db.rollback()
                raise
            finally:
                db.close()

    async def call(self, method: str, *args, **kwargs):
        return await asyncio.to_thread(getattr(self, method), *args, **kwargs)

    def session(self, meta: dict) -> None:
        with self.connection() as db:
            db.execute(
                """INSERT INTO sessions(scope,platform,platform_id,kind,location,persona,umo,last_seen)
                VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(scope) DO UPDATE SET umo=excluded.umo,last_seen=excluded.last_seen""",
                (
                    meta["scope"],
                    meta["platform"],
                    meta["platform_id"],
                    meta["kind"],
                    meta["location"],
                    meta["persona"],
                    meta["umo"],
                    now(),
                ),
            )

    def sessions(self) -> list[dict]:
        with self.connection() as db:
            return [
                dict(r)
                for r in db.execute(
                    "SELECT * FROM sessions ORDER BY last_seen DESC LIMIT 1000"
                )
            ]

    def get_session(self, scope: str) -> dict | None:
        with self.connection() as db:
            return dictrow(
                db.execute("SELECT * FROM sessions WHERE scope=?", (scope,)).fetchone()
            )

    def get_session_by_umo(self, umo: str) -> dict | None:
        with self.connection() as db:
            return dictrow(
                db.execute(
                    "SELECT * FROM sessions WHERE umo=? ORDER BY last_seen DESC LIMIT 1",
                    (umo,),
                ).fetchone()
            )

    def person(self, scope: str, uid: str) -> dict | None:
        with self.connection() as db:
            row=dictrow(
                db.execute(
                    "SELECT * FROM people WHERE scope=? AND uid=?", (scope, uid)
                ).fetchone()
            )
            return identity.decorate(db,[row],relations=True)[0] if row else None

    @staticmethod
    def _with_aliases(db, rows):
        """Fetch aliases in bounded batches. Order recent observations first."""
        by_identity = {(r["scope"], r["ref"]): r for r in rows}
        for row in rows:
            row["aliases"] = []
            row["custom_aliases"] = []
            row["historical_aliases"] = []
        scopes = {}
        for scope, ref in by_identity:
            scopes.setdefault(scope, []).append(ref)
        for scope, refs in scopes.items():
            for start in range(0, len(refs), 300):
                batch = refs[start:start + 300]
                for a in db.execute(
                    "SELECT * FROM person_aliases WHERE scope=? AND person_ref IN ("
                    + ",".join("?" for _ in batch) + ") ORDER BY last_seen DESC,alias",
                    (scope, *batch),
                ):
                    p = by_identity[(scope, a["person_ref"])]
                    alias = a["alias"]
                    bucket = p["custom_aliases"] if a["kind"] == "manual" else p["historical_aliases"]
                    if alias not in bucket:
                        bucket.append(alias)
                    if alias not in p["aliases"]:
                        p["aliases"].append(alias)
        for p in rows:
            p["historical_aliases"] = [a for a in p["historical_aliases"] if a not in (p["name"], p["display_name"])]
        return rows

    def resolve_people(self, scope, who):
        """An exact canonical UID/ref wins; otherwise all matching names/aliases must agree."""
        with self.connection() as db:
            rows = [dict(r) for r in db.execute(
                "SELECT scope,uid,ref,name,display_name,blocked FROM people WHERE scope=?", (scope,)
            )]
            identity.decorate(db,self._with_aliases(db,rows))
            exact = [p for p in rows if who == p["uid"] or who.casefold() == p["ref"].casefold()
                     or (p.get("identity_id") and who.casefold() == p["identity_id"].casefold())]
            if exact:
                return exact
        key = normalized(who)
        if not key:
            return []
        def names(p):
            return (p["name"], p["display_name"], *p["aliases"])
        exact = [p for p in rows if any(normalized(n) == key for n in names(p) if n)]
        if exact:
            return exact
        fuzzy = entity_key(who)
        return [p for p in rows if fuzzy and any(entity_key(n) == fuzzy for n in names(p) if n)]

    def scope_people(self, scope):
        """Full scope for ambiguity/block checks; not paginated like the admin list."""
        with self.connection() as db:
            return identity.decorate(db,self._with_aliases(db, [dict(r) for r in db.execute(
                "SELECT * FROM people WHERE scope=? ORDER BY last_seen DESC", (scope,)
            )]),relations=True)

    def list_people(self, scope="", q="", offset=0, limit=100) -> list[dict]:
        params = []
        parts = []
        if scope:
            parts.append("p.scope=?")
            params.append(scope)
        if q:
            parts.append("(p.uid LIKE ? OR p.ref LIKE ? OR p.name LIKE ? OR p.display_name LIKE ? OR EXISTS (SELECT 1 FROM person_aliases a WHERE a.scope=p.scope AND a.person_ref=p.ref AND a.alias LIKE ?) OR "+
                         identity.member_matches_sql("p.scope","p.ref")+")")
            params.extend([f"%{q}%"] * 9)
        where = " WHERE " + " AND ".join(parts) if parts else ""
        with self.connection() as db:
            rows = [
                dict(r)
                for r in db.execute(
                    "SELECT p.* FROM people p"
                    + where
                    + " ORDER BY last_seen DESC LIMIT ? OFFSET ?",
                    (*params, limit, offset),
                )
            ]
            identity.decorate(db,self._with_aliases(db, rows),relations=True)
            recent={sc:identity.recent_for_scope(db,sc) for sc in {r["scope"] for r in rows}}
            for r in rows:
                r.update(familiarity(r, recent[r["scope"]].get(r["ref"],0)))
        for r in rows:
            r["deny_keywords"] = json.loads(r["deny_keywords"])
            r["habit_opt_in"] = bool(r["habit_opt_in"])
        return rows

    def list_identities(self, scope="", q="", offset=0, limit=100):
        """Admin list: group before filtering/pagination, not after returning rows."""
        with self.connection() as db:
            rows=identity.candidates(db,scope,q,offset,limit)
            members,relations=identity.members_and_relations(db,[p["identity_id"] for p in rows])
            self._with_aliases(db,members)
            for m in members:
                m["deny_keywords"]=json.loads(m["deny_keywords"])
                m["habit_opt_in"]=bool(m["habit_opt_in"])
            return identity.assemble(rows,members,relations)

    def edit_identity(self, gid, data):
        with self.connection() as db:
            identity.edit_profile(db,gid,data)

    def edit_person(self, scope: str, uid: str, data: dict) -> None:
        # Old clients may submit role/affinity; if explicitly present, these
        # now edit the corresponding persona's shared relationship, not a
        # scope-specific copy. The new local editor never submits them.
        identity.validate_relation(data)
        aliases = data.get("custom_aliases")
        if legacy_title(data.get("display_name")) or (aliases is not None and isinstance(aliases, list) and any(legacy_title(a) for a in aliases)):
            raise ValueError("请使用正常昵称，旧主人称呼只保留为历史别名")
        opt_in = data.get("habit_opt_in")
        if opt_in is not None and type(opt_in) is not bool:
            raise ValueError("习惯记忆授权需要布尔值")
        if aliases is not None and (
            not isinstance(aliases, list) or len(aliases) > 30
            or any(not isinstance(a, str) or not a.strip() or len(a) > 80 for a in aliases)
        ):
            raise ValueError("自定义别名需要不超过30个非空字符串，每个最多80字")
        with self.connection() as db:
            prior = db.execute("SELECT * FROM people WHERE scope=? AND uid=?", (scope, uid)).fetchone()
            if not prior:
                raise ValueError("用户不存在")
            ref = prior["ref"]
            fields = dict(
                display_name=clean(data.get("display_name",prior["display_name"]),80),
                note=clean(data.get("note",prior["note"]),500),
                blocked=data.get("blocked",bool(prior["blocked"])),
                deny_keywords=data.get("deny_keywords",json.loads(prior["deny_keywords"])),
                deny_reply=clean(data.get("deny_reply",prior["deny_reply"]),300),
            )
            kws=fields["deny_keywords"]
            if type(fields["blocked"]) is not bool:
                raise ValueError("会话屏蔽状态无效")
            if (not isinstance(kws,list) or len(kws)>40
                or any(not isinstance(x,str) or not x.strip() or len(x)>80 for x in kws)):
                raise ValueError("拒绝关键词需要非空字符串数组")
            if aliases is not None:
                aliases = list(dict.fromkeys(identity_name(a, uid, ref) for a in aliases))
                if "" in aliases:
                    raise ValueError("自定义别名不能包含用户ID、敏感资料或无效名称")
            changed = db.execute(
                "UPDATE people SET display_name=?,note=?,blocked=?,deny_keywords=?,deny_reply=? WHERE scope=? AND uid=?",
                (
                    fields["display_name"],
                    fields["note"],
                    int(fields["blocked"]),
                    dump(kws),
                    fields["deny_reply"],
                    scope,
                    uid,
                ),
            ).rowcount
            if not changed:
                raise ValueError("用户不存在")
            seen = now()
            if set(data)&{"role","affinity","familiarity_override"}:
                shared=identity.ensure_member(db,scope,ref)
                identity.edit_relation(db,shared["identity_id"],shared["persona"],data)
            if opt_in is not None:
                self._habit_consent(db, scope, ref, opt_in)
            self._record_alias(db, scope, ref, identity_name(prior["display_name"], uid, ref), "display", prior["last_seen"])
            self._record_alias(db, scope, ref, identity_name(fields["display_name"], uid, ref), "display", seen)
            if aliases is not None:
                db.execute("DELETE FROM person_aliases WHERE scope=? AND person_ref=? AND kind='manual'", (scope, ref))
                for name in aliases:
                    self._record_alias(db, scope, ref, name, "manual", seen)

    def interaction(self, scope, uid, at=None):
        """Count a completed/submitted reply once, not every bot message fragment."""
        at = now() if at is None else at
        with self.connection() as db:
            p = db.execute("SELECT ref,blocked FROM people WHERE scope=? AND uid=?", (scope, uid)).fetchone()
            if not p or p["blocked"]:
                return
            db.execute("UPDATE people SET interactions=interactions+1,last_interaction=MAX(last_interaction,?) WHERE scope=? AND uid=?",
                       (at, scope, uid))
            db.execute("""INSERT INTO relationship_days(scope,person_ref,day,turns) VALUES(?,?,?,1)
                       ON CONFLICT(scope,person_ref,day) DO UPDATE SET turns=turns+1""",
                       (scope, p["ref"], day_key(at)))

    def recent_interactions(self, scope):
        with self.connection() as db:
            return identity.recent_for_scope(db,scope)

    def recent_bot_messages(self, scope, limit=20):
        with self.connection() as db:
            return [dict(r) for r in reversed(db.execute(
                "SELECT text,ts,bot FROM messages WHERE scope=? AND bot=1 ORDER BY seq DESC LIMIT ?",
                (scope,limit)).fetchall())]

    @staticmethod
    def _habit_consent(db, scope, ref, consent):
        p = db.execute("SELECT habit_opt_in FROM people WHERE scope=? AND ref=?", (scope, ref)).fetchone()
        if not p:
            raise ValueError("人物不存在")
        if bool(p["habit_opt_in"]) != consent:
            db.execute("UPDATE people SET habit_opt_in=?,habit_since=? WHERE scope=? AND ref=?",
                       (int(consent), now() if consent else 0, scope, ref))
        if not consent:
            db.execute("DELETE FROM habit_observations WHERE scope=? AND person_ref=?", (scope, ref))
            db.execute("DELETE FROM memories WHERE scope=? AND subject=? AND kind='habit'", (scope, ref))

    def set_habit_consent(self, scope, uid, consent):
        with self.connection() as db:
            p = db.execute("SELECT ref,blocked FROM people WHERE scope=? AND uid=?", (scope, uid)).fetchone()
            if not p or p["blocked"]:
                raise ValueError("请先在该授权会话正常发言")
            self._habit_consent(db, scope, p["ref"], bool(consent))

    def _legacy_plan(self, db, scope, ref, old, replacement):
        p = db.execute("SELECT * FROM people WHERE scope=? AND ref=?", (scope, ref)).fetchone()
        if not p or not legacy_title(old):
            raise ValueError("请选择会话中的身份与旧称呼")
        p = dict(p)
        name = identity_name(replacement, p["uid"], p["ref"])
        if not name or legacy_title(name) or legacy_reference(name):
            raise ValueError("请确认正常的新昵称，不使用旧身份称呼")
        rows = []
        for item in db.execute("""SELECT m.* FROM memories m
          JOIN memory_people mp ON mp.memory_id=m.id AND mp.scope=m.scope
          WHERE m.scope=? AND mp.person_ref=? ORDER BY m.id""", (scope, ref)):
            if item["kind"] == "habit":
                continue
            changes = {}
            for key in ("title", "full_text", "gist", "trace"):
                value = replace_legacy(item[key], old, name)
                if value != item[key]:
                    changes[key] = {"before": item[key], "after": value}
            if changes:
                rows.append({"id": item["id"], "revision": item["revision"], "changes": changes})
                if len(rows) > 200:
                    raise ValueError("命中超过200条，请按单条记忆核对")
        plan = {"scope": scope, "ref": ref, "uid": p["uid"], "old": old, "name": name,
                "current_display": p["display_name"], "rows": rows}
        plan["revision"] = hashlib.sha256(dump(plan).encode()).hexdigest()
        return plan

    def legacy_preview(self, scope, ref, old, replacement):
        with self.connection() as db:
            return self._legacy_plan(db, scope, ref, old, replacement)

    def legacy_apply(self, scope, ref, old, replacement, revision, selected, rename_display=False):
        if not isinstance(selected, list) or any(type(x) is not int for x in selected):
            raise ValueError("请选择预览中的记忆")
        with self.connection() as db:
            plan = self._legacy_plan(db, scope, ref, old, replacement)
            if revision != plan["revision"]:
                raise ValueError("相关记录已变化，请重新预览")
            if set(selected) - {r["id"] for r in plan["rows"]}:
                raise ValueError("选中的记忆不属于预览")
            for row in plan["rows"]:
                if row["id"] not in selected:
                    continue
                prior = dict(db.execute("SELECT * FROM memories WHERE id=?", (row["id"],)).fetchone())
                snapshot = {**prior, "participant_refs": self._memory_refs(db, row["id"])}
                db.execute("INSERT INTO memory_versions(memory_id,saved,reason,snapshot) VALUES(?,?,?,?)",
                           (row["id"], now(), "nickname_cleanup", dump(snapshot)))
                values = {k: v["after"] for k, v in row["changes"].items()}
                db.execute("UPDATE memories SET "+",".join(k+"=?" for k in values)+",revision=revision+1 WHERE id=?",
                           (*values.values(), row["id"]))
                db.execute("DELETE FROM vectors WHERE memory_id=?", (row["id"],))
            if rename_display:
                db.execute("UPDATE people SET display_name=? WHERE scope=? AND ref=?", (plan["name"], scope, ref))
                self._record_alias(db, scope, ref, plan["name"], "display", now())
            return {"changed": len(set(selected)), "display_changed": bool(rename_display)}

    def ingest(self, meta: dict, msg: dict, *, count_submission=False) -> dict | None:
        with self.connection() as db:
            db.execute(
                """INSERT INTO sessions(scope,platform,platform_id,kind,location,persona,umo,last_seen)
                VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(scope) DO UPDATE SET umo=excluded.umo,last_seen=excluded.last_seen""",
                (
                    meta["scope"],
                    meta["platform"],
                    meta["platform_id"],
                    meta["kind"],
                    meta["location"],
                    meta["persona"],
                    meta["umo"],
                    now(),
                ),
            )
            scope, uid = meta["scope"], msg["uid"]
            ref = "BOT" if msg.get("bot") else user_ref(scope, uid)
            if db.execute("SELECT 1 FROM messages WHERE scope=? AND ext_id=?", (scope, msg["ext_id"])).fetchone():
                return None
            name = clean(msg.get("name"), 80) or "肥鱼"
            refs = {}
            if not msg.get("bot"):
                # Check per-scope administrative block before persisting anything about the sender.
                old = db.execute(
                    "SELECT * FROM people WHERE scope=? AND uid=?", (scope, uid)
                ).fetchone()
                if old and old["blocked"]:
                    return {"blocked": True}
                incoming = identity_name(msg.get("name"), uid, ref)
                seen = msg["ts"]
                # Do not replace an observed nickname with a missing-name placeholder or a late message.
                old_name = identity_name(old["name"], uid, ref) if old else ""
                name = (incoming if incoming and (not old or seen >= old["last_seen"] or not old_name) else "") or old_name or ref
                db.execute(
                    """INSERT INTO people(scope,uid,ref,name,last_seen) VALUES(?,?,?,?,?)
                    ON CONFLICT(scope,uid) DO UPDATE SET name=excluded.name,last_seen=MAX(people.last_seen,excluded.last_seen)""",
                    (scope, uid, ref, name, seen),
                )
                shared=identity.ensure_member(db,scope,ref)
                self._record_alias(db, scope, ref, incoming, "observed", seen)
                for kind, key in (("mentioned", "mentioned_uids"), ("quoted", "quoted_uids")):
                    # Ground mentions and quotes in real adapter IDs, only for known people in this scope.
                    trusted = []
                    for target in list(dict.fromkeys(msg.get(key, [])))[:12]:
                        p = db.execute("SELECT ref FROM people WHERE scope=? AND uid=? AND blocked=0", (scope, target)).fetchone()
                        if p and p["ref"] not in trusted:
                            trusted.append(p["ref"])
                    if trusted:
                        refs[kind] = trusted
                if old and identity_name(old["display_name"], uid, ref) and not legacy_title(old["display_name"]):
                    name = old["display_name"]
                elif shared["display_name"]:
                    name = identity_name(shared["display_name"],uid,ref) or name
            else:
                old = None
            cur = db.execute(
                """INSERT OR IGNORE INTO messages(scope,ext_id,uid,ref,name,text,ts,directed,bot,image_count,explicit,memory_done,style_done,identity_refs)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    scope,
                    msg["ext_id"],
                    uid,
                    ref,
                    name,
                    clean(msg.get("text"), MAX_REPLY_CHARS if count_submission else 1800),
                    msg["ts"],
                    int(msg.get("directed", False)),
                    int(msg.get("bot", False)),
                    msg.get("image_count", 0),
                    int(msg.get("explicit", False)),
                    int(msg.get("memory_done", False)),
                    int(msg.get("style_done", False)),
                    dump(refs),
                ),
            )
            if not cur.rowcount:
                return None
            if count_submission:
                self._count_event(db, scope, "sent_parts", 1)
            row = dict(
                db.execute(
                    "SELECT * FROM messages WHERE seq=?", (cur.lastrowid,)
                ).fetchone()
            )
            row["user_rule"] = dict(old) if old else {}
            return row

    def context(self, scope: str, limit: int, max_seq: int | None = None) -> list[dict]:
        with self.connection() as db:
            if max_seq is None:
                rows = db.execute(
                    "SELECT * FROM messages WHERE scope=? ORDER BY seq DESC LIMIT ?",
                    (scope, limit),
                )
            else:
                rows = db.execute(
                    "SELECT * FROM messages WHERE scope=? AND seq<=? ORDER BY seq DESC LIMIT ?",
                    (scope, max_seq, limit),
                )
            return [dict(r) for r in rows][::-1]

    def pending(
        self, module: str, count: int, max_age: int, min_count=1, urgent=False
    ) -> list[dict]:
        col = {"memory": "memory_done", "style": "style_done"}[module]
        with self.connection() as db:
            where = f"m.{col}=0" + (
                " AND m.bot=0 AND s.kind='group'" if module == "style" else ""
            )
            rows = db.execute(
                f"""SELECT m.scope, COUNT(*) n, MIN(m.ts) oldest, MAX(m.explicit) urgent
                FROM messages m JOIN sessions s ON s.scope=m.scope WHERE {where}
                GROUP BY m.scope HAVING COUNT(*)>=? OR (COUNT(*)>=? AND MIN(m.ts)<=?) OR (? AND MAX(m.explicit)=1)
                ORDER BY urgent DESC, oldest ASC LIMIT 200""",
                (count, min_count, now() - max_age, int(urgent)),
            )
            return [dict(r) for r in rows]

    def batch(self, scope: str, module: str, count: int, max_chars: int) -> list[dict]:
        col = {"memory": "memory_done", "style": "style_done"}[module]
        with self.connection() as db:
            extra = " AND bot=0" if module == "style" else ""
            rows = db.execute(
                f"SELECT * FROM messages WHERE scope=? AND {col}=0{extra} ORDER BY seq LIMIT ?",
                (scope, count),
            )
            out = []
            size = 0
            for row in rows:
                n = len(row["text"]) + len(row["name"]) + 100
                if out and size + n > max_chars:
                    break
                out.append(dict(row))
                size += n
            return out

    def mark_done(self, db, module: str, ids: list[int]) -> None:
        if ids:
            col = {"memory": "memory_done", "style": "style_done"}[module]
            db.executemany(
                f"UPDATE messages SET {col}=1 WHERE seq=?", ((n,) for n in ids)
            )

    def _insert_memory(
        self, db, p: dict, *, manual=False, identity: int | None = None
    ) -> int:
        old = None
        if identity:
            old = db.execute(
                "SELECT * FROM memories WHERE id=?", (identity,)
            ).fetchone()
            if not old:
                raise ValueError("记忆不存在")
            if old["scope"] != p["scope"]:
                raise ValueError("不可改变记忆作用域")
        else:
            old = db.execute(
                "SELECT * FROM memories WHERE scope=? AND subject=? AND kind=? AND topic_key=?",
                (p["scope"], p["subject"], p["kind"], p["topic_key"]),
            ).fetchone()
        requested = p.get("participant_refs", [])
        if not isinstance(requested, list) or any(not isinstance(ref, str) for ref in requested):
            raise ValueError("记忆关联人物必须为身份代号数组")
        wanted = list(dict.fromkeys(([p["subject"]] if p["subject"] != "group" else []) + requested))
        refs = self._valid_refs(db, p["scope"], wanted)
        if len(refs) != len(wanted):
            raise ValueError("记忆关联人物不属于当前会话")
        if old:
            if not manual and old["pinned"]:
                return old["id"]
            if not manual:
                prev_ids = json.loads(old["source_ids"])
                if p.get("source_ids"):
                    evidence = json.loads(old["evidence"])
                    merged_at_seq = max(
                        (item["target_seq_cutoff"] for item in evidence if isinstance(item, dict)
                         and type(item.get("target_seq_cutoff")) is int), default=0
                    )
                    if max(p["source_ids"]) <= merged_at_seq:
                        # A learning batch prepared before the merge may have
                        # spent time awaiting its model. Preserve the merge.
                        return old["id"]
                if (
                    prev_ids
                    and p.get("source_ids")
                    and max(prev_ids) > max(p["source_ids"])
                ):
                    # A delayed background batch cannot undo a newer explicit correction.
                    return old["id"]
            if not manual and p["kind"]!="habit" and old["full_text"] == p["full_text"]:
                # Explicit new evidence strengthens accessibility, not inherent importance.
                db.execute(
                    "UPDATE memories SET last_recalled=?,clarity='vivid',state='active',archived_at=NULL,valid_until=? WHERE id=?",
                    (now(), p.get("valid_until") or old["valid_until"], old["id"]),
                )
                self._add_memory_refs(db, old["id"], p["scope"], refs)
                return old["id"]
            if (
                not manual
                and old["kind"] in ("core", "profile")
                and not p.get("confirmed_update", False)
            ):
                # Do not replace a durable fact because of a vague mention.
                return old["id"]
            previous_refs = self._memory_refs(db, old["id"])
            snapshot = {**dict(old), "participant_refs": previous_refs}
            if manual and all(old[key] == p.get(key) for key in (
                "kind", "topic_key", "title", "full_text", "gist", "trace", "importance", "valid_until"
            )) and bool(old["pinned"]) == bool(p.get("pinned", False)):
                # Editing identities alone is not evidence of a renewed experience.
                # Preserve clarity, archive state, age, confidence and embedding revision.
                new_refs = refs if "participant_refs" in p else list(dict.fromkeys(
                    [r for r in previous_refs if r != old["subject"] or old["subject"] == p["subject"]] + refs
                ))
                if old["subject"] != p["subject"] or set(new_refs) != set(previous_refs):
                    db.execute("INSERT INTO memory_versions(memory_id,saved,reason,snapshot) VALUES(?,?,?,?)",
                               (old["id"], now(), "identity", dump(snapshot)))
                    db.execute("UPDATE memories SET subject=? WHERE id=?", (p["subject"], old["id"]))
                    db.execute("DELETE FROM memory_people WHERE memory_id=?", (old["id"],))
                    self._add_memory_refs(db, old["id"], p["scope"], new_refs)
                return old["id"]
            db.execute(
                "INSERT INTO memory_versions(memory_id,saved,reason,snapshot) VALUES(?,?,?,?)",
                (
                    old["id"],
                    now(),
                    "manual" if manual else "new_evidence",
                    dump(snapshot),
                ),
            )
            source_ids = p.get("source_ids", json.loads(old["source_ids"]))
            evidence = p.get("evidence", json.loads(old["evidence"]))
            db.execute(
                """UPDATE memories SET subject=?,kind=?,topic_key=?,title=?,full_text=?,gist=?,trace=?,importance=?,confidence=?,clarity='vivid',state='active',pinned=?,updated=?,last_recalled=NULL,valid_until=?,source_ids=?,evidence=?,revision=revision+1,archived_at=NULL WHERE id=?""",
                (
                    p["subject"],
                    p["kind"],
                    p["topic_key"],
                    p["title"],
                    p["full_text"],
                    p["gist"],
                    p["trace"],
                    p["importance"],
                    p["confidence"],
                    int(p.get("pinned", False) if manual else old["pinned"]),
                    now(),
                    p.get("valid_until"),
                    dump(source_ids),
                    dump(evidence),
                    old["id"],
                ),
            )
            db.execute("DELETE FROM vectors WHERE memory_id=?", (old["id"],))
            if (manual and "participant_refs" in p) or (not manual and p.get("confirmed_update", False) and old["kind"] in ("core", "profile", "temporary")):
                db.execute("DELETE FROM memory_people WHERE memory_id=?", (old["id"],))
            elif manual and old["subject"] != p["subject"]:
                db.execute("DELETE FROM memory_people WHERE memory_id=? AND person_ref=?", (old["id"], old["subject"]))
            # Partial later accounts are additive; explicit durable-fact corrections replace links.
            self._add_memory_refs(db, old["id"], p["scope"], refs)
            return old["id"]
        cur = db.execute(
            """INSERT INTO memories(scope,subject,kind,topic_key,title,full_text,gist,trace,importance,confidence,pinned,created,updated,valid_until,source_ids,evidence)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                p["scope"],
                p["subject"],
                p["kind"],
                p["topic_key"],
                p["title"],
                p["full_text"],
                p["gist"],
                p["trace"],
                p["importance"],
                p["confidence"],
                int(p.get("pinned", False) if manual else False),
                p.get("created", now()),
                now(),
                p.get("valid_until"),
                dump(p.get("source_ids", [])),
                dump(p.get("evidence", [])),
            ),
        )
        mid = int(cur.lastrowid)
        self._add_memory_refs(db, mid, p["scope"], refs)
        return mid

    def commit_memory(
        self, proposals: list[dict], affinity_changes: list[tuple], ids: list[int],
        observations=(), habit_settings=None,
    ) -> list[int]:
        with self.connection() as db:
            result = [self._insert_memory(db, p) for p in proposals]
            for scope, ref, delta in affinity_changes:
                identity.change_affinity(db,scope,ref,delta)
            if observations and habit_settings:
                from .habits import rollup
                allowed_ids=set(ids)
                result.extend(rollup(self, db, [o for o in observations if o["source_seq"] in allowed_ids], habit_settings))
            self.mark_done(db, "memory", ids)
            return result

    def remember_explicit(self, p: dict) -> dict:
        """Acknowledge the fact actually committed, not merely an existing pinned ID."""
        with self.connection() as db:
            mid = self._insert_memory(db, p)
            saved = db.execute(
                "SELECT full_text,state,pinned FROM memories WHERE id=?", (mid,)
            ).fetchone()
            return {
                "id": mid,
                "saved": bool(
                    saved
                    and saved["full_text"] == p["full_text"]
                    and saved["state"] == "active"
                ),
                "pinned": bool(saved and saved["pinned"]),
            }

    def manual_memory(self, p: dict, identity: int | None = None) -> int:
        with self.connection() as db:
            if not db.execute(
                "SELECT scope FROM sessions WHERE scope=?", (p["scope"],)
            ).fetchone():
                raise ValueError("会话不存在")
            if (
                p["subject"] != "group"
                and not db.execute(
                    "SELECT uid FROM people WHERE scope=? AND ref=?",
                    (p["scope"], p["subject"]),
                ).fetchone()
            ):
                raise ValueError("记忆对象不属于此会话")
            prior = db.execute("SELECT * FROM memories WHERE id=?", (identity,)).fetchone() if identity else None
            if p["kind"]=="habit" or (prior and prior["kind"]=="habit"):
                from .habits import SENSITIVE_ROUTINES
                from .util import unsafe_secret
                if not prior or prior["kind"]!="habit" or p["kind"]!="habit" or prior["scope"]!=p["scope"] or prior["subject"]!=p["subject"]:
                    raise ValueError("习惯由自愿的多日证据形成，仅允许编辑本条归纳文本")
                if not db.execute("SELECT 1 FROM people WHERE scope=? AND ref=? AND habit_opt_in=1", (p["scope"],p["subject"])).fetchone():
                    raise ValueError("该人物未授权习惯记忆")
                if any(unsafe_secret(p[k]) or SENSITIVE_ROUTINES.search(p[k]) for k in ("title","full_text","gist","trace")):
                    raise ValueError("习惯印象不可记录敏感或可追踪的现实行程")
                snapshot={**dict(prior),"participant_refs":self._memory_refs(db,identity)}
                db.execute("INSERT INTO memory_versions(memory_id,saved,reason,snapshot) VALUES(?,?,?,?)",
                           (identity,now(),"habit_edit",dump(snapshot)))
                db.execute("UPDATE memories SET title=?,full_text=?,gist=?,trace=?,importance=?,revision=revision+1 WHERE id=?",
                           (p["title"],p["full_text"],p["gist"],p["trace"],p["importance"],identity))
                db.execute("DELETE FROM vectors WHERE memory_id=?", (identity,))
                return identity
            return self._insert_memory(db, p, manual=True, identity=identity)

    def list_memories(
        self, scope="", q="", kind="", state="", offset=0, limit=80
    ) -> list[dict]:
        clauses = []
        params = []
        for col, val in (("scope", scope), ("kind", kind), ("state", state)):
            if val:
                clauses.append(f"m.{col}=?")
                params.append(val)
        gid=identity.canonical_global_id(q)
        if gid:
            # The "关联记忆" button uses the exact canonical ID, not a text
            # occurrence in another person's prose. Scope filters still apply.
            clauses.append("""(EXISTS (SELECT 1 FROM identity_memberships im
                WHERE im.identity_id=? AND im.scope=m.scope AND im.person_ref=m.subject)
                OR EXISTS (SELECT 1 FROM memory_people mp JOIN identity_memberships im
                  ON im.scope=mp.scope AND im.person_ref=mp.person_ref
                  WHERE mp.memory_id=m.id AND im.identity_id=?))""")
            params.extend((gid,gid))
        elif q:
            clauses.append(
                """(m.title LIKE ? OR m.full_text LIKE ? OR m.topic_key LIKE ?
                OR m.subject LIKE ? OR p.name LIKE ? OR p.display_name LIKE ? OR p.uid LIKE ?
                OR """+identity.member_matches_sql("p.scope","p.ref")+"""
                OR EXISTS (SELECT 1 FROM memory_people mp JOIN people linked
                   ON linked.scope=mp.scope AND linked.ref=mp.person_ref
                   WHERE mp.memory_id=m.id AND (linked.uid LIKE ? OR linked.ref LIKE ? OR linked.name LIKE ? OR linked.display_name LIKE ?
                   OR EXISTS (SELECT 1 FROM person_aliases a WHERE a.scope=mp.scope AND a.person_ref=mp.person_ref AND a.alias LIKE ?)
                   OR """+identity.member_matches_sql("linked.scope","linked.ref") + """)))"""
            )
            params.extend([f"%{q}%"] * 20)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self.connection() as db:
            result = [
                dict(r)
                for r in db.execute(
                    "SELECT m.*, COALESCE(NULLIF(p.display_name,''),p.name,m.subject) subject_name FROM memories m LEFT JOIN people p ON p.scope=m.scope AND p.ref=m.subject"
                    + where
                    + " ORDER BY m.pinned DESC,m.updated DESC LIMIT ? OFFSET ?",
                    (*params, limit, offset),
                )
            ]
            self._attach_memory_people(db, result)
        for r in result:
            r["visible_text"] = visible_text(r)
            r["source_ids"] = json.loads(r["source_ids"])
            r["evidence"] = json.loads(r["evidence"])
        return result

    def _attach_memory_people(self, db, rows):
        """Attach current identities and history to the returned rows, not stored prose."""
        if not rows:
            return rows
        memberships = {}
        ids = list(dict.fromkeys(r["id"] for r in rows))
        for start in range(0, len(ids), 300):
            batch = ids[start:start+300]
            for x in db.execute("SELECT memory_id,person_ref FROM memory_people WHERE memory_id IN ("
                                + ",".join("?" for _ in batch) + ") ORDER BY person_ref", batch):
                memberships.setdefault(x["memory_id"], []).append(x["person_ref"])
        people = {}
        for scope in set(r["scope"] for r in rows):
            persons = identity.decorate(db,self._with_aliases(db, [dict(p) for p in db.execute(
                "SELECT scope,uid,ref,name,display_name,blocked FROM people WHERE scope=?", (scope,))]))
            people.update({(scope, p["ref"]): p for p in persons})
        for r in rows:
            if r.get("state") == "historical":
                linked = r.get("participant_refs", evidence_refs(r))
                if not isinstance(linked, list):
                    linked = []
            else:
                linked = memberships.get(r["id"], [])
            refs = list(dict.fromkeys([r["subject"], *(v for v in linked if isinstance(v, str))]))
            participants = [people[(r["scope"], ref)] for ref in refs if (r["scope"], ref) in people]
            r["participant_refs"] = [p["ref"] for p in participants]
            r["participants"] = participants
            subject = people.get((r["scope"], r["subject"]))
            if subject:
                r["subject_name"] = compact_person(subject)["name"]
        return rows

    def memory_versions(self, mid: int) -> list[dict]:
        with self.connection() as db:
            return [
                {**dict(r), "snapshot": json.loads(r["snapshot"])}
                for r in db.execute(
                    "SELECT * FROM memory_versions WHERE memory_id=? ORDER BY version_id DESC LIMIT 50",
                    (mid,),
                )
            ]

    def delete_memory(self, mid: int) -> bool:
        with self.connection() as db:
            row=db.execute("SELECT scope,subject,topic_key,kind FROM memories WHERE id=?", (mid,)).fetchone()
            if row and row["kind"]=="habit":
                db.execute("DELETE FROM habit_observations WHERE scope=? AND person_ref=? AND topic_key=?",
                           (row["scope"],row["subject"],row["topic_key"]))
            return bool(db.execute("DELETE FROM memories WHERE id=?", (mid,)).rowcount)

    def archive_memory(self, mid: int, restore: bool) -> bool:
        with self.connection() as db:
            if restore:
                return bool(
                    db.execute(
                        "UPDATE memories SET state='active',clarity='vivid',archived_at=NULL,last_recalled=? WHERE id=? AND (valid_until IS NULL OR valid_until>?)",
                        (now(), mid, now()),
                    ).rowcount
                )
            return bool(
                db.execute(
                    "UPDATE memories SET state='archived',archived_at=? WHERE id=? AND pinned=0",
                    (now(), mid),
                ).rowcount
            )

    def recall_candidates(
        self,
        scope: str,
        subject: str | None,
        kind: str | None,
        deep: bool,
        historical: bool,
        since: float | None,
        until: float | None,
        fingerprint="",
    ) -> list[dict]:
        # The scope predicate is constructed only from the trusted runtime session.
        args = [scope]
        filt = ["m.scope=?", "COALESCE(p.blocked,0)=0",
                "(m.kind!='habit' OR COALESCE(p.habit_opt_in,0)=1)",
                "NOT EXISTS (SELECT 1 FROM memory_people mp JOIN people b ON b.scope=mp.scope AND b.ref=mp.person_ref WHERE mp.memory_id=m.id AND b.blocked=1)"]
        states = (
            ["active"]
            + (["archived"] if deep else [])
            + (["expired"] if historical else [])
        )
        filt.append("m.state IN (" + ",".join("?" for _ in states) + ")")
        args.extend(states)
        if not historical:
            filt.append("(m.valid_until IS NULL OR m.valid_until>?)")
            args.append(now())
        if subject:
            if subject == "group":
                filt.append("m.subject=?")
                args.append(subject)
            else:
                filt.append("(m.subject=? OR EXISTS (SELECT 1 FROM memory_people mp WHERE mp.memory_id=m.id AND mp.scope=m.scope AND mp.person_ref=?))")
                args.extend((subject, subject))
        if kind in KINDS:
            filt.append("m.kind=?")
            args.append(kind)
        if since is not None:
            filt.append("m.created>=?")
            args.append(since)
        if until is not None:
            filt.append("m.created<=?")
            args.append(until)
        sql = "SELECT m.*, COALESCE(NULLIF(p.display_name,''),p.name,m.subject) subject_name"
        if fingerprint:
            sql += ",CASE WHEN v.fingerprint=? AND v.revision=m.revision THEN v.vector END embedding"
            args.insert(0, fingerprint)
        sql += (
            " FROM memories m LEFT JOIN people p ON p.scope=m.scope AND p.ref=m.subject"
        )
        if fingerprint:
            sql += " LEFT JOIN vectors v ON v.memory_id=m.id"
        sql += (
            " WHERE "
            + " AND ".join(filt)
            + " ORDER BY m.pinned DESC,m.importance DESC,m.updated DESC LIMIT 10000"
        )
        with self.connection() as db:
            rows = [dict(r) for r in db.execute(sql, args)]
            if historical and deep:
                # Previous versions are deliberately marked historical and only available for a deliberate deep search.
                ids = [
                    r["id"]
                    for r in rows
                    if r["kind"] in ("core", "profile", "temporary")
                ]
                for start in range(0, min(len(ids), 2000), 300):
                    batch = ids[start : start + 300]
                    if not batch:
                        continue
                    records = db.execute(
                        "SELECT * FROM memory_versions WHERE memory_id IN ("
                        + ",".join("?" for _ in batch)
                        + ") ORDER BY saved DESC LIMIT 500",
                        batch,
                    )
                    for rec in records:
                        old = json.loads(rec["snapshot"])
                        old["state"] = "historical"
                        old["version_id"] = rec["version_id"]
                        prior_refs = old.get("participant_refs", evidence_refs(old))
                        if not isinstance(prior_refs, list):
                            prior_refs = []
                        if (
                            old["scope"] != scope
                            or subject
                            and old["subject"] != subject
                            and (subject == "group" or subject not in prior_refs)
                            or kind in KINDS
                            and old["kind"] != kind
                        ):
                            continue
                        if (
                            since is not None
                            and old["created"] < since
                            or until is not None
                            and old["created"] > until
                        ):
                            continue
                        if db.execute(
                            "SELECT 1 FROM people WHERE scope=? AND ref=? AND blocked=1",
                            (scope, old["subject"]),
                        ).fetchone():
                            continue
                        old["subject_name"] = next(
                            (x["subject_name"] for x in rows if x["id"] == old["id"]),
                            old["subject"],
                        )
                        rows.append(old)
            self._attach_memory_people(db, rows)
            return [r for r in rows if not any(p["blocked"] for p in r["participants"])]

    def mark_recalled(self, ids: list[int], deep=False):
        with self.connection() as db:
            db.executemany(
                "UPDATE memories SET last_recalled=?,recall_count=recall_count+1,clarity=CASE WHEN ? THEN 'vivid' ELSE clarity END WHERE id=? AND state='active'",
                ((now(), int(deep), mid) for mid in set(ids)),
            )

    @staticmethod
    def _embedding_filter(blocked_refs):
        where = (
            "m.scope=? AND NOT EXISTS (SELECT 1 FROM people p WHERE p.scope=m.scope AND p.ref=m.subject AND p.blocked=1)"
            " AND (m.kind!='habit' OR (? AND EXISTS (SELECT 1 FROM people p WHERE p.scope=m.scope AND p.ref=m.subject AND p.habit_opt_in=1)))"
            " AND NOT EXISTS (SELECT 1 FROM memory_people mp JOIN people b ON b.scope=mp.scope AND b.ref=mp.person_ref WHERE mp.memory_id=m.id AND b.blocked=1)"
        )
        if blocked_refs:
            marks = ",".join("?" for _ in blocked_refs)
            where += (" AND m.subject NOT IN (" + marks + ") AND NOT EXISTS (SELECT 1 FROM memory_people mp "
                      "WHERE mp.memory_id=m.id AND mp.person_ref IN (" + marks + "))")
        return where

    def unembedded(
        self, scope: str, fingerprint: str, limit: int = 16, blocked_refs=(),
        after=0, include_habits=True,
    ) -> list[dict]:
        with self.connection() as db:
            rows = [
                dict(r)
                for r in db.execute(
                    "SELECT m.* FROM memories m LEFT JOIN vectors v ON m.id=v.memory_id WHERE "
                    + self._embedding_filter(blocked_refs)
                    + " AND (v.memory_id IS NULL OR v.fingerprint<>? OR v.revision<>m.revision)"
                    + " AND m.id>? ORDER BY m.id LIMIT ?",
                    (scope, int(include_habits), *blocked_refs, *blocked_refs, fingerprint, after, limit),
                )
            ]
            return self._attach_memory_people(db, rows)

    def embedding_counts(self, scope, fingerprint, blocked_refs=(), include_habits=True):
        with self.connection() as db:
            total, indexed = db.execute(
                "SELECT COUNT(*), COALESCE(SUM(CASE WHEN v.fingerprint=? AND v.revision=m.revision "
                "THEN 1 ELSE 0 END),0) FROM memories m LEFT JOIN vectors v ON m.id=v.memory_id WHERE "
                + self._embedding_filter(blocked_refs),
                (fingerprint, scope, int(include_habits), *blocked_refs, *blocked_refs),
            ).fetchone()
        return {"indexed": indexed, "pending": total - indexed}

    @staticmethod
    def _save_vectors(db, fingerprint, records):
        saved = 0
        for mid, rev, vec in records:
            if db.execute(
                "SELECT id FROM memories WHERE id=? AND revision=?", (mid, rev)
            ).fetchone():
                db.execute(
                    "INSERT INTO vectors VALUES(?,?,?,?) ON CONFLICT(memory_id) DO UPDATE SET fingerprint=excluded.fingerprint,revision=excluded.revision,vector=excluded.vector",
                    (mid, fingerprint, rev, dump(vec)),
                )
                saved += 1
        return saved

    def save_vectors(
        self, fingerprint: str, records: list[tuple[int, int, list[float]]]
    ):
        with self.connection() as db:
            return self._save_vectors(db, fingerprint, records)

    def save_embedding_batch(self, key, previous_dimension, dimension, fingerprint, records):
        """Commit a validated sub-batch and its current dimension in the same transaction."""
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
            current = json.loads(row["value"]) if row else 0
            if current not in (previous_dimension, dimension):
                return None
            saved = self._save_vectors(db, fingerprint, records)
            db.execute("INSERT OR REPLACE INTO kv VALUES(?,?)", (key, dump(dimension)))
            return saved

    def commit_styles(self, scope: str, styles: list[dict], ids: list[int], auto=False):
        with self.connection() as db:
            for s in styles:
                status = "approved" if auto and s["confidence"] >= 0.92 else "pending"
                db.execute(
                    """INSERT INTO styles(scope,kind,pattern,norm,meaning,example,confidence,status,created,updated)
                    VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(scope,kind,norm) DO UPDATE SET count=count+1,updated=excluded.updated""",
                    (
                        scope,
                        s["kind"],
                        s["pattern"],
                        entity_key(s["pattern"]),
                        s["meaning"],
                        s["example"],
                        s["confidence"],
                        status,
                        now(),
                        now(),
                    ),
                )
            self.mark_done(db, "style", ids)

    def list_styles(self, scope="", q="", status="", offset=0, limit=100) -> list[dict]:
        filt = []
        args = []
        if scope:
            filt.append("scope=?")
            args.append(scope)
        if status:
            filt.append("status=?")
            args.append(status)
        if q:
            filt.append("(pattern LIKE ? OR meaning LIKE ?)")
            args.extend([f"%{q}%"] * 2)
        where = " WHERE " + " AND ".join(filt) if filt else ""
        with self.connection() as db:
            return [
                dict(r)
                for r in db.execute(
                    "SELECT * FROM styles"
                    + where
                    + " ORDER BY updated DESC LIMIT ? OFFSET ?",
                    (*args, limit, offset),
                )
            ]

    def edit_style(self, sid: int, data: dict, action="save"):
        with self.connection() as db:
            old = db.execute("SELECT * FROM styles WHERE id=?", (sid,)).fetchone()
            if old is None:
                raise ValueError("表达模式不存在")
            if action == "delete":
                db.execute("DELETE FROM styles WHERE id=?", (sid,))
                return
            snapshot = dict(old)
            if action == "undo":
                ver = db.execute(
                    "SELECT * FROM style_versions WHERE style_id=? ORDER BY version_id DESC LIMIT 1",
                    (sid,),
                ).fetchone()
                if not ver:
                    raise ValueError("没有可回滚的版本")
                data = json.loads(ver["snapshot"])
                db.execute(
                    "DELETE FROM style_versions WHERE version_id=?",
                    (ver["version_id"],),
                )
            else:
                db.execute(
                    "INSERT INTO style_versions(style_id,saved,snapshot) VALUES(?,?,?)",
                    (sid, now(), dump(snapshot)),
                )
            status = data.get("status", old["status"])
            if status not in ("pending", "approved", "rejected"):
                raise ValueError("审核状态无效")
            pattern = clean(data.get("pattern", old["pattern"]), 100)
            meaning = clean(data.get("meaning", old["meaning"]), 300)
            example = clean(data.get("example", old["example"]), 240)
            if not entity_key(pattern) or not meaning:
                raise ValueError("表达及含义不能为空")
            db.execute(
                "UPDATE styles SET pattern=?,norm=?,meaning=?,example=?,status=?,revision=revision+1,updated=? WHERE id=?",
                (pattern, entity_key(pattern), meaning, example, status, now(), sid),
            )

    def image_get(self, key: str) -> str | None:
        with self.connection() as db:
            r = db.execute(
                "SELECT description FROM image_cache WHERE cache_key=?", (key,)
            ).fetchone()
            return r[0] if r else None

    def image_set(self, key: str, text: str, max_entries: int):
        if max_entries <= 0:
            return
        with self.connection() as db:
            db.execute(
                "INSERT OR REPLACE INTO image_cache VALUES(?,?,?)",
                (key, clean(text, 2000), now()),
            )
            db.execute(
                "DELETE FROM image_cache WHERE cache_key IN (SELECT cache_key FROM image_cache ORDER BY ts DESC LIMIT -1 OFFSET ?)",
                (max_entries,),
            )

    def event_count(self, scope: str, kind: str, n=1):
        with self.connection() as db:
            self._count_event(db, scope, kind, n)

    @staticmethod
    def _count_event(db, scope, kind, n):
        db.execute(
            "INSERT INTO daily_events VALUES(?,?,?,?) ON CONFLICT(day,scope,kind) DO UPDATE SET n=n+excluded.n",
            (day_key(), scope, kind, n),
        )

    def add_usage(
        self,
        day: str,
        module: str,
        provider: str,
        failed: bool,
        other: int,
        cached: int,
        output: int,
        estimated: bool,
        ms: int,
    ):
        with self.connection() as db:
            db.execute(
                """INSERT INTO usage(day,module,provider,calls,failures,input_other,input_cached,output,estimated,elapsed_ms) VALUES(?,?,?,1,?,?,?,?,?,?)
              ON CONFLICT(day,module,provider) DO UPDATE SET calls=calls+1,failures=failures+excluded.failures,input_other=input_other+excluded.input_other,input_cached=input_cached+excluded.input_cached,output=output+excluded.output,estimated=estimated+excluded.estimated,elapsed_ms=elapsed_ms+excluded.elapsed_ms""",
                (
                    day,
                    module,
                    provider,
                    int(failed),
                    other,
                    cached,
                    output,
                    int(estimated),
                    ms,
                ),
            )

    def usage_today(self, day: str) -> dict:
        with self.connection() as db:
            rows = [
                dict(r)
                for r in db.execute(
                    "SELECT module,SUM(calls) calls,SUM(input_other+input_cached+output) tokens FROM usage WHERE day=? GROUP BY module",
                    (day,),
                )
            ]
            return {
                "tokens": sum(r["tokens"] for r in rows),
                "modules": {r["module"]: r["calls"] for r in rows},
                "module_tokens": {r["module"]: r["tokens"] for r in rows},
            }

    def usage_rows(self, days=15) -> list[dict]:
        with self.connection() as db:
            return [
                dict(r)
                for r in db.execute(
                    "SELECT * FROM usage WHERE day>=? ORDER BY day DESC,module,provider",
                    (day_key(now() - (days - 1) * 86400),),
                )
            ]

    def totals(self) -> dict:
        with self.connection() as db:
            return {
                "sessions": db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0],
                "people": db.execute("SELECT COUNT(DISTINCT identity_id) FROM identity_memberships").fetchone()[0],
                "memberships": db.execute("SELECT COUNT(*) FROM people").fetchone()[0],
                "memories": {
                    r[0]: r[1]
                    for r in db.execute(
                        "SELECT state,COUNT(*) FROM memories GROUP BY state"
                    )
                },
                "styles": {
                    r[0]: r[1]
                    for r in db.execute(
                        "SELECT status,COUNT(*) FROM styles GROUP BY status"
                    )
                },
                "today": {
                    r[0]: r[1]
                    for r in db.execute(
                        "SELECT kind,SUM(n) FROM daily_events WHERE day=? GROUP BY kind",
                        (day_key(),),
                    )
                },
                "pending_memory": db.execute(
                    "SELECT COUNT(*) FROM messages WHERE memory_done=0"
                ).fetchone()[0],
                "pending_style": db.execute(
                    "SELECT COUNT(*) FROM messages WHERE style_done=0 AND bot=0"
                ).fetchone()[0],
            }

    def get_kv(self, key: str, default=None):
        with self.connection() as db:
            r = db.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
            return json.loads(r[0]) if r else default

    def claim_research(self, persona, daily_limit, interval_minutes, at=None):
        at=now() if at is None else at
        day=day_key(at)
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            count,last=db.execute("SELECT COUNT(*),MAX(started) FROM research_runs WHERE day=? AND state<>'migrated'", (day,)).fetchone()
            if count>=daily_limit or (last and at-last<interval_minutes*60):
                return None
            run=db.execute("INSERT INTO research_runs(persona,day,started,state) VALUES(?,?,?,'running')", (persona,day,at))
            return int(run.lastrowid)

    def research_duplicate(self, persona, topic_key, evidence_key, cutoff):
        with self.connection() as db:
            return bool(db.execute("""SELECT 1 FROM research_notes WHERE persona=? AND created>=?
                  AND (topic_key=? OR (evidence_key=? AND evidence_key!='')) LIMIT 1""",
                  (persona,cutoff,topic_key,evidence_key)).fetchone())

    def finish_research(self, run_id, state, source, topic, reason="", tokens=0, note=None, dedupe_days=7):
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            run=db.execute("SELECT * FROM research_runs WHERE id=? AND state='running'", (run_id,)).fetchone()
            if not run:
                return False
            at=now()
            if note is not None:
                prior=db.execute("""SELECT 1 FROM research_notes WHERE persona=? AND created>=?
                  AND (topic_key=? OR evidence_key=?) LIMIT 1""",
                  (run["persona"],at-dedupe_days*86400,note["topic_key"],note["evidence_key"])).fetchone()
                if prior:
                    state,reason="skipped","重复主题或证据"
                else:
                    db.execute("""INSERT INTO research_notes(run_id,persona,source,topic,topic_key,evidence_key,facts,opinion,sources,created,expires)
                         VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                         (run_id,run["persona"],source,topic,note["topic_key"],note["evidence_key"],
                          dump(note["facts"]),dump(note["opinion"]),dump(note["sources"]),at,note["expires"]))
                    state="completed"
            db.execute("UPDATE research_runs SET state=?,source=?,topic=?,reason=?,tokens=?,finished=? WHERE id=?",
                       (state,source,topic,clean(reason,120),max(0,int(tokens)),at,run_id))
            return state=="completed"

    @staticmethod
    def _research_notes(db, persona="", recent_only=False, limit=80, offset=0, q=""):
        clauses,params=[],[]
        if persona:
            clauses.append("persona=?")
            params.append(persona)
        if recent_only:
            clauses.extend(("state='active'","expires>?"))
            params.append(now())
        if q:
            clauses.append("(topic LIKE ? OR facts LIKE ? OR opinion LIKE ?)")
            params.extend([f"%{q}%"]*3)
        rows=[dict(r) for r in db.execute("SELECT * FROM research_notes"
             + (" WHERE "+" AND ".join(clauses) if clauses else "")
             + " ORDER BY created DESC LIMIT ? OFFSET ?", (*params,limit,offset))]
        for r in rows:
            for key in ("facts","opinion","sources"):
                r[key]=json.loads(r[key])
            r["stale"]=r["expires"]<=now()
        return rows

    def research_notes(self, persona="", recent_only=False, limit=80, offset=0, q=""):
        with self.connection() as db:
            return self._research_notes(db,persona,recent_only,limit,offset,q)

    def research_overview(self):
        with self.connection() as db:
            return {"today_runs":db.execute("SELECT COUNT(*) FROM research_runs WHERE day=? AND state<>'migrated'", (day_key(),)).fetchone()[0],
                    "notes":db.execute("SELECT COUNT(*) FROM research_notes").fetchone()[0],
                    "last":dictrow(db.execute("SELECT * FROM research_runs WHERE state<>'migrated' ORDER BY id DESC LIMIT 1").fetchone())}

    def research_runs(self, limit=60):
        with self.connection() as db:
            return [dict(x) for x in db.execute("SELECT * FROM research_runs WHERE state<>'migrated' ORDER BY id DESC LIMIT ?", (limit,))]

    def research_note_action(self, note_id, action):
        with self.connection() as db:
            if action=="delete":
                return bool(db.execute("DELETE FROM research_notes WHERE id=?", (note_id,)).rowcount)
            if action in ("archive","restore"):
                return bool(db.execute("UPDATE research_notes SET state=? WHERE id=?",
                              ("archived" if action=="archive" else "active", note_id)).rowcount)
            raise ValueError("未知笔记操作")

    def create_relay(self, meta, request_seq, alias, target, body, rewrite, limit, expires):
        at=now()
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT 1 FROM relay_jobs WHERE scope=? AND request_seq=?", (meta["scope"],request_seq)).fetchone():
                return None,"本次请求已经处理过"
            n=db.execute("SELECT COUNT(*) FROM relay_jobs WHERE scope=? AND uid=? AND day=?",
                         (meta["scope"],meta["uid"],day_key(at))).fetchone()[0]
            if n>=limit:
                return None,"今天的转达次数已达上限"
            db.execute("UPDATE relay_jobs SET status='cancelled',body='',finished=? WHERE scope=? AND uid=? AND status='pending'", (at,meta["scope"],meta["uid"]))
            cur=db.execute("""INSERT INTO relay_jobs(scope,uid,request_seq,day,alias,target_umo,target_scope,body,status,created,expires)
                           VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                           (meta["scope"],meta["uid"],request_seq,day_key(at),alias,target["umo"],target["scope"],body,
                            "drafting" if rewrite else "sending",at,expires))
            return dict(db.execute("SELECT * FROM relay_jobs WHERE id=?", (cur.lastrowid,)).fetchone()),""

    def confirm_relay(self, scope, uid, job_id):
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            r=db.execute("SELECT * FROM relay_jobs WHERE id=? AND scope=? AND uid=? AND status='pending'", (job_id,scope,uid)).fetchone()
            if not r or r["expires"]<=now():
                if r:
                    db.execute("UPDATE relay_jobs SET status='expired',body='',finished=? WHERE id=?", (now(),job_id))
                return None
            db.execute("UPDATE relay_jobs SET status='sending' WHERE id=?", (job_id,))
            return dict(r)

    def set_relay_draft(self, job_id, body):
        with self.connection() as db:
            return bool(db.execute("UPDATE relay_jobs SET body=?,status='pending' WHERE id=? AND status='drafting' AND expires>?",
                                   (body,job_id,now())).rowcount)

    def pending_relay(self, scope, uid):
        with self.connection() as db:
            return dictrow(db.execute("SELECT * FROM relay_jobs WHERE scope=? AND uid=? AND status='pending' AND expires>? ORDER BY id DESC LIMIT 1",
                           (scope,uid,now())).fetchone())

    def finish_relay(self, job_id, status):
        with self.connection() as db:
            db.execute("UPDATE relay_jobs SET status=?,body='',finished=? WHERE id=? AND status IN ('sending','pending','drafting')",
                       (status,now(),job_id))

    def cancel_relay(self, scope, uid):
        with self.connection() as db:
            return bool(db.execute("UPDATE relay_jobs SET status='cancelled',body='',finished=? WHERE scope=? AND uid=? AND status='pending'",
                                   (now(),scope,uid)).rowcount)

    def relay_history(self, limit=100):
        with self.connection() as db:
            return [dict(x) for x in db.execute("""SELECT id,scope,alias,status,created,finished
                          FROM relay_jobs ORDER BY id DESC LIMIT ?""", (limit,))]

    def set_kv(self, key: str, value: Any):
        with self.connection() as db:
            db.execute("INSERT OR REPLACE INTO kv VALUES(?,?)", (key, dump(value)))

    def maintenance(self, cfg: dict, at: float | None = None) -> dict:
        ts = now() if at is None else at
        mem = cfg["memory"]
        personal = cfg["persona"]
        changed = 0
        expired = 0
        purged = 0
        raw = 0
        with self.connection() as db:
            rows = db.execute("SELECT * FROM memories WHERE state='active'").fetchall()
            for r in rows:
                clarity, state = memory_target(dict(r), mem, ts)
                if (clarity, state) != (r["clarity"], r["state"]):
                    db.execute(
                        "UPDATE memories SET clarity=?,state=?,archived_at=? WHERE id=?",
                        (clarity, state, None if state == "active" else ts, r["id"]),
                    )
                    changed += 1
                    expired += int(state == "expired")
            # Short-lived ordinary facts are deliberately not kept forever, including their revisions.
            for r in db.execute(
                "SELECT id,created,archived_at FROM memories WHERE state='archived' AND kind='ordinary' AND pinned=0"
            ).fetchall():
                if ts - r["created"] >= mem["ordinary_archive_days"] * 86400:
                    purged += db.execute(
                        "DELETE FROM memories WHERE id=?", (r["id"],)
                    ).rowcount
            raw += db.execute(
                "DELETE FROM messages WHERE ts<?",
                (ts - personal["raw_retention_days"] * 86400,),
            ).rowcount
            for (scope,) in db.execute("SELECT scope FROM sessions").fetchall():
                raw += db.execute(
                    "DELETE FROM messages WHERE scope=? AND seq IN (SELECT seq FROM messages WHERE scope=? ORDER BY seq DESC LIMIT -1 OFFSET ?)",
                    (scope, scope, personal["raw_max_per_scope"]),
                ).rowcount
            db.execute(
                "DELETE FROM daily_events WHERE day<?", (day_key(ts - 365 * 86400),)
            )
            db.execute("DELETE FROM usage WHERE day<?", (day_key(ts - 365 * 86400),))
            db.execute("DELETE FROM relationship_days WHERE day<?", (day_key(ts-30*86400),))
            db.execute("DELETE FROM habit_observations WHERE day<?", (day_key(ts-(cfg["habits"]["window_days"]-1)*86400),))
            db.execute("UPDATE relay_jobs SET status='expired',body='',finished=? WHERE status IN ('pending','drafting') AND expires<=?", (ts,ts))
            db.execute("DELETE FROM relay_jobs WHERE status NOT IN ('pending','sending') AND created<?", (ts-86400*cfg["logs"]["retention_days"],))
            db.execute("DELETE FROM research_runs WHERE started<? AND state!='running' AND NOT EXISTS (SELECT 1 FROM research_notes n WHERE n.run_id=research_runs.id)", (ts-90*86400,))
        return {
            "changed": changed,
            "expired": expired,
            "ordinary_removed": purged,
            "raw_removed": raw,
        }


def cosine(v1: list[float], v2: list[float]) -> float:
    if not v1 or len(v1) != len(v2):
        return 0.0
    a = math.sqrt(sum(x * x for x in v1))
    b = math.sqrt(sum(x * x for x in v2))
    return sum(x * y for x, y in zip(v1, v2)) / (a * b) if a and b else 0.0


def rank_memories(
    rows: list[dict],
    query: str,
    subject: str | None,
    current: str,
    top_k: int,
    *,
    deep=False,
    vector: list[float] | None = None,
    semantic_min=0.6,
    related_refs=(),
) -> list[dict]:
    out = []
    for r in rows:
        # Crucially, ordinary recall does not search hidden exact details.
        txt = visible_text(r, deep=deep)
        persons = r.get("participants", [])
        aliases = [n for p in persons for n in (p["ref"], p.get("name", ""), p.get("display_name", ""), *p.get("aliases", [])) if n]
        hay = " ".join([r["title"], r["topic_key"], r.get("subject_name", ""), txt, *aliases])
        match = lexical(query, hay)
        # In a natural-language question (e.g. "他的生日是什么时候"), a short topic
        # is often a much stronger signal than the fraction of all matching bigrams.
        topic = entity_key(r["title"])
        qkey = entity_key(query)
        if len(topic) >= 2 and topic in qkey:
            match = max(match, 0.85)
        sem = 0.0
        if vector and r.get("embedding") and (deep or r["clarity"] == "vivid"):
            try:
                sem = cosine(vector, json.loads(r["embedding"]))
            except (TypeError, ValueError, OverflowError):
                pass
        if query and match < 0.24 and sem < semantic_min:
            continue
        if not query and not subject:
            continue
        score = max(match, sem if sem >= semantic_min else 0.0)
        score += (
            0.15 * (r["subject"] == current or current in r.get("participant_refs", []))
            + 0.22 * bool(set(related_refs) & set(r.get("participant_refs", [])))
            + 0.08 * r["importance"] / 100
            + 0.03 * r["pinned"]
        )
        score += 0.04 * math.exp(
            -max(0, now() - (r["last_recalled"] or r["updated"])) / (60 * 86400)
        )
        out.append((score, r, txt))
    out.sort(key=lambda x: (x[0], x[1]["updated"]), reverse=True)
    return [
        {
            "id": r["id"],
            "subject": r["subject"],
            "person": next((compact_person(p)["name"] for p in r.get("participants", []) if p["ref"] == r["subject"]), r.get("subject_name", r["subject"])),
            "participants": [compact_person(p) for p in r.get("participants", [])],
            "kind": r["kind"],
            "title": r["title"],
            "text": txt,
            "clarity": r["clarity"],
            "state": r["state"],
            "confidence": r["confidence"],
            "created": r["created"],
            "valid_until": r["valid_until"],
            "historical_version": r.get("version_id"),
        }
        for _, r, txt in out[:top_k]
    ]
