"""Global administrative identity and persona-specific relationships.

The legacy (scope, ref) pairs remain the evidence and access-control keys.
Neither this module nor its search helpers grant access to another chat.
"""
from __future__ import annotations

import json
import re

from .personality import familiarity, legacy_reference, legacy_title
from .util import clean, day_key, dump, ident, normalized, now


DDL = """
CREATE TABLE IF NOT EXISTS person_identities (
 identity_id TEXT PRIMARY KEY, platform TEXT NOT NULL, platform_id TEXT NOT NULL,
 uid TEXT NOT NULL, display_name TEXT NOT NULL DEFAULT '',
 custom_aliases TEXT NOT NULL DEFAULT '[]', display_history TEXT NOT NULL DEFAULT '[]',
 revision INTEGER NOT NULL DEFAULT 1,
 created REAL NOT NULL, updated REAL NOT NULL,
 UNIQUE(platform,platform_id,uid)
);
CREATE TABLE IF NOT EXISTS identity_memberships (
 scope TEXT NOT NULL, person_ref TEXT NOT NULL, identity_id TEXT NOT NULL,
 PRIMARY KEY(scope,person_ref),
 FOREIGN KEY(scope,person_ref) REFERENCES people(scope,ref) ON DELETE CASCADE,
 FOREIGN KEY(identity_id) REFERENCES person_identities(identity_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS identity_member_index ON identity_memberships(identity_id,scope,person_ref);
CREATE TABLE IF NOT EXISTS identity_relationships (
 identity_id TEXT NOT NULL, persona TEXT NOT NULL,
 role TEXT NOT NULL DEFAULT 'member', affinity INTEGER NOT NULL DEFAULT 50,
 familiarity_override TEXT NOT NULL DEFAULT 'auto', revision INTEGER NOT NULL DEFAULT 1,
 PRIMARY KEY(identity_id,persona),
 FOREIGN KEY(identity_id) REFERENCES person_identities(identity_id) ON DELETE CASCADE
);
"""

FAMILIARITY = ("auto", "stranger", "normal", "familiar", "close")


def global_id(platform, platform_id, uid):
    # An adapter's raw ID is opaque and case-sensitive. Do not normalize it or
    # merge across robots, platforms, different adapter instances, or names.
    return "G" + ident(dump([platform, platform_id, uid]), 24)


def canonical_global_id(value):
    return "G"+value[1:].lower() if isinstance(value,str) and re.fullmatch(r"G[0-9a-f]{24}",value,re.I) else ""


def _rows(db, sql, params=()):
    return [dict(r) for r in db.execute(sql, params)]


def _chunks(values, count=250):
    values = list(values)
    for start in range(0, len(values), count):
        yield values[start:start+count]


def _marks(values):
    return ",".join("?" for _ in values)


def affiliation(db, scope, ref):
    return db.execute("""SELECT g.*,s.persona FROM identity_memberships im
        JOIN person_identities g ON g.identity_id=im.identity_id
        JOIN sessions s ON s.scope=im.scope
        WHERE im.scope=? AND im.person_ref=?""", (scope,ref)).fetchone()


def ensure_member(db, scope, ref):
    """Called inside the same transaction that inserts the scoped person."""
    known = affiliation(db,scope,ref)
    if known:
        return known
    row = db.execute("""SELECT p.uid,p.last_seen,s.platform,s.platform_id,s.persona
        FROM people p JOIN sessions s ON s.scope=p.scope
        WHERE p.scope=? AND p.ref=?""", (scope,ref)).fetchone()
    if not row:
        raise ValueError("会话人物不存在")
    gid = global_id(row["platform"],row["platform_id"],row["uid"])
    db.execute("""INSERT INTO person_identities(identity_id,platform,platform_id,uid,created,updated)
        VALUES(?,?,?,?,?,?) ON CONFLICT(platform,platform_id,uid) DO NOTHING""",
        (gid,row["platform"],row["platform_id"],row["uid"],row["last_seen"],now()))
    db.execute("INSERT INTO identity_memberships(scope,person_ref,identity_id) VALUES(?,?,?)", (scope,ref,gid))
    db.execute("""INSERT INTO identity_relationships(identity_id,persona) VALUES(?,?)
        ON CONFLICT(identity_id,persona) DO NOTHING""", (gid,row["persona"]))
    return affiliation(db,scope,ref)


def backfill(db):
    """One-time migration: do not rewrite scoped people, messages or memories."""
    rows = _rows(db,"""SELECT p.*,s.platform,s.platform_id,s.persona FROM people p
        JOIN sessions s ON s.scope=p.scope ORDER BY p.last_seen,p.scope,p.ref""")
    groups={}
    for row in rows:
        person=ensure_member(db,row["scope"],row["ref"])
        groups.setdefault((person["identity_id"],row["persona"]),[]).append(row)
    for (gid,persona), members in groups.items():
        # No exact time of the historical affinity edit is available. Seed once
        # from the most recently interacted non-default record (then last seen).
        # Defaults from a newly encountered chat must not erase an older value.
        # Old individual scores remain untouched for administrator review.
        ranked=sorted(members,key=lambda p:(p.get("last_interaction",0),p["last_seen"],p["scope"]),reverse=True)
        affinity=next((p["affinity"] for p in ranked if type(p["affinity"]) is int and 0<=p["affinity"]<=100 and p["affinity"]!=50),50)
        role="friend" if any(p["role"]=="friend" for p in ranked) else "member"
        override=next((p["familiarity_override"] for p in ranked if p["familiarity_override"] in FAMILIARITY[1:]),"auto")
        db.execute("""UPDATE identity_relationships SET role=?,affinity=?,familiarity_override=?
            WHERE identity_id=? AND persona=?""", (role,affinity,override,gid,persona))
    counts=db.execute("SELECT (SELECT COUNT(*) FROM people),(SELECT COUNT(*) FROM identity_memberships)").fetchone()
    if counts[0]!=counts[1]:
        raise ValueError("全局身份映射不完整")
    if db.execute("""SELECT 1 FROM identity_memberships im
        JOIN person_identities g ON g.identity_id=im.identity_id
        JOIN people p ON p.scope=im.scope AND p.ref=im.person_ref
        JOIN sessions s ON s.scope=im.scope
        LEFT JOIN identity_relationships r ON r.identity_id=g.identity_id AND r.persona=s.persona
        WHERE p.uid!=g.uid OR s.platform!=g.platform OR s.platform_id!=g.platform_id OR r.identity_id IS NULL LIMIT 1""").fetchone():
        raise ValueError("全局身份与原会话关联校验失败")


def decorate(db, rows, *, relations=False):
    """Attach global metadata only to already authorized, scoped identity rows.

    Never union observed aliases, notes, rules or habit consent from other
    sessions into a model-facing row. Shared aliases/nickname are deliberately
    maintained by the administrator. The local ref does not change.
    """
    by_scope={}
    for row in rows:
        by_scope.setdefault(row["scope"],{}).setdefault(row["ref"],[]).append(row)
    for scope, by_ref in by_scope.items():
        for refs in _chunks(by_ref):
            sql="""SELECT im.person_ref,g.identity_id,g.display_name,g.custom_aliases"""
            if relations:
                sql+=""",r.role,r.affinity,r.familiarity_override,
                    (SELECT COALESCE(SUM(op.interactions),0) FROM identity_memberships om
                       JOIN sessions os ON os.scope=om.scope AND os.persona=s.persona
                       JOIN people op ON op.scope=om.scope AND op.ref=om.person_ref
                       WHERE om.identity_id=im.identity_id) total_interactions,
                    (SELECT COALESCE(MAX(op.last_interaction),0) FROM identity_memberships om
                       JOIN sessions os ON os.scope=om.scope AND os.persona=s.persona
                       JOIN people op ON op.scope=om.scope AND op.ref=om.person_ref
                       WHERE om.identity_id=im.identity_id) latest_interaction"""
            sql+=""" FROM identity_memberships im JOIN person_identities g ON g.identity_id=im.identity_id
                JOIN sessions s ON s.scope=im.scope"""
            if relations:
                sql+=""" JOIN identity_relationships r ON r.identity_id=im.identity_id AND r.persona=s.persona"""
            sql+=" WHERE im.scope=? AND im.person_ref IN ("+_marks(refs)+")"
            for data in db.execute(sql,(scope,*refs)):
                for row in by_ref[data["person_ref"]]:
                    row["identity_id"]=data["identity_id"]
                    row["common_display_name"]=data["display_name"]
                    row["common_aliases"]=json.loads(data["custom_aliases"])
                    row["effective_display_name"]=next((v for v in (row.get("display_name"),data["display_name"],row.get("name"))
                        if v and v not in (row.get("uid"),row["ref"]) and not legacy_title(v)
                        and "[已隐藏]" not in v and v!="[链接]"),row["ref"])
                    if "aliases" in row:
                        row["aliases"]=list(dict.fromkeys([
                            *row["aliases"], *([data["display_name"]] if data["display_name"] else []),
                            *row["common_aliases"],
                        ]))
                    if relations:
                        row["scope_interactions"]=row.get("interactions",0)
                        row["scope_last_interaction"]=row.get("last_interaction",0)
                        row.update(role=data["role"],affinity=data["affinity"],
                                   familiarity_override=data["familiarity_override"],
                                   interactions=data["total_interactions"],
                                   last_interaction=data["latest_interaction"])
    return rows


def recent_for_scope(db, scope):
    # A scoped ref maps to aggregate interaction counts with this persona only.
    return dict(db.execute("""SELECT mine.person_ref,SUM(d.turns)
        FROM identity_memberships mine JOIN sessions here ON here.scope=mine.scope
        JOIN identity_memberships other ON other.identity_id=mine.identity_id
        JOIN sessions there ON there.scope=other.scope AND there.persona=here.persona
        JOIN relationship_days d ON d.scope=other.scope AND d.person_ref=other.person_ref
        WHERE mine.scope=? AND d.day>=? GROUP BY mine.person_ref""",
        (scope,day_key(now()-6*86400))).fetchall())


def _lookup_predicate(alias="g"):
    # SQL fragment uses only hard-coded aliases from this module.
    return f"({alias}.identity_id LIKE ? OR {alias}.display_name LIKE ? OR {alias}.custom_aliases LIKE ? OR {alias}.display_history LIKE ?)"


def member_matches_sql(scope_expr, ref_expr):
    """For admin search only; not a model recall authority or scope selector."""
    return f"""EXISTS (SELECT 1 FROM identity_memberships im JOIN person_identities g ON g.identity_id=im.identity_id
        WHERE im.scope={scope_expr} AND im.person_ref={ref_expr} AND {_lookup_predicate()})"""


def candidates(db, scope, q, offset, limit):
    params=[]
    where=""
    if scope:
        where=" WHERE im.scope=?"
        params.append(scope)
    sql=f"""SELECT g.*,matched.last_seen FROM person_identities g JOIN
        (SELECT im.identity_id,MAX(p.last_seen) last_seen FROM identity_memberships im
         JOIN people p ON p.scope=im.scope AND p.ref=im.person_ref{where}
         GROUP BY im.identity_id) matched ON matched.identity_id=g.identity_id"""
    if q:
        sql+=""" WHERE (g.uid LIKE ? OR """+_lookup_predicate()+"""
            OR EXISTS (SELECT 1 FROM identity_memberships im
              JOIN people p ON p.scope=im.scope AND p.ref=im.person_ref
              WHERE im.identity_id=g.identity_id AND (p.name LIKE ? OR p.display_name LIKE ? OR p.ref LIKE ?
                OR EXISTS (SELECT 1 FROM person_aliases a WHERE a.scope=p.scope AND a.person_ref=p.ref AND a.alias LIKE ?))))"""
        params.extend([f"%{q}%"]*9)
    sql+=" ORDER BY matched.last_seen DESC,g.identity_id LIMIT ? OFFSET ?"
    return _rows(db,sql,(*params,limit,offset))


def members_and_relations(db, ids):
    members=[]
    relations=[]
    for batch in _chunks(ids):
        members.extend(_rows(db,"""SELECT p.*,im.identity_id,s.kind,s.location,s.persona,s.umo,s.platform,s.platform_id,
            COALESCE((SELECT SUM(d.turns) FROM relationship_days d WHERE d.scope=p.scope AND d.person_ref=p.ref AND d.day>=?),0) local_recent
            FROM identity_memberships im JOIN people p ON p.scope=im.scope AND p.ref=im.person_ref
            JOIN sessions s ON s.scope=p.scope WHERE im.identity_id IN ("""+_marks(batch)+") ORDER BY p.last_seen DESC,p.scope",
            (day_key(now()-6*86400),*batch)))
        relations.extend(_rows(db,"SELECT * FROM identity_relationships WHERE identity_id IN ("+_marks(batch)+") ORDER BY persona",batch))
    return members,relations


def assemble(rows, members, relations):
    """One admin row per opaque UID; members remain individually editable."""
    people={}
    for p in rows:
        people[p["identity_id"]]=p
        p["custom_aliases"]=json.loads(p["custom_aliases"])
        p["display_history"]=json.loads(p["display_history"])
        p["memberships"]=[]
        p["relationships"]=[]
    for m in members:
        people[m["identity_id"]]["memberships"].append(m)
    for r in relations:
        related=[m for m in people[r["identity_id"]]["memberships"] if m["persona"]==r["persona"]]
        r["interactions"]=sum(m["interactions"] for m in related)
        r["last_interaction"]=max((m["last_interaction"] for m in related),default=0)
        r.update(familiarity(r,sum(m["local_recent"] for m in related)))
        r["membership_count"]=len(related)
        people[r["identity_id"]]["relationships"].append(r)
    for p in rows:
        members=p["memberships"]
        p["last_seen"]=max(m["last_seen"] for m in members)
        p["name"]=next((m["display_name"] or m["name"] for m in members
                       if (m["display_name"] or m["name"]) and not legacy_title(m["display_name"] or m["name"])
                       and (m["display_name"] or m["name"]) not in (m["ref"],m["uid"])), "") or p["identity_id"]
        aliases=[a for a in p["display_history"] if a not in (p["name"],p["display_name"])]
        for m in members:
            for name in (m["name"],m["display_name"],*m.get("aliases",[])):
                if name and name not in (m["ref"],m["uid"],p["name"],p["display_name"]) and name not in aliases:
                    aliases.append(name)
        p["historical_aliases"]=aliases
        p["membership_count"]=len(members)
        p["group_count"]=sum(m["kind"]=="group" for m in members)
        p["private_count"]=sum(m["kind"]=="private" for m in members)
        p["blocked_count"]=sum(bool(m["blocked"]) for m in members)
    return rows


def _validated_name(value, identity, allow_empty=False):
    if not isinstance(value,str) or len(value)>80:
        raise ValueError("昵称或别名必须为不超过80字的文本")
    value=value.strip()
    name=clean(value,80)
    if not name and allow_empty and not value:
        return ""
    if (not name or name!=value or "[已隐藏]" in name or name=="[链接]"
        or name in (identity["uid"],identity["identity_id"]) or re.fullmatch(r"(?:U[0-9a-f]{12}|G[0-9a-f]{24})",name,re.I)
        or legacy_title(name) or legacy_reference(name)):
        raise ValueError("使用正常、可共享的昵称；不能填写身份ID、敏感资料或旧主人称呼")
    return name


def validate_relation(data):
    if not isinstance(data,dict):
        raise ValueError("关系设置格式错误")
    if "role" in data and data["role"] not in ("member","friend"):
        raise ValueError("关系标签无效")
    if "affinity" in data and (type(data["affinity"]) is not int or not 0<=data["affinity"]<=100):
        raise ValueError("好感度应为0至100的整数")
    if "familiarity_override" in data and data["familiarity_override"] not in FAMILIARITY:
        raise ValueError("熟悉度设置无效")


def edit_relation(db, gid, persona, data, *, optimistic=False):
    validate_relation(data)
    old=db.execute("SELECT * FROM identity_relationships WHERE identity_id=? AND persona=?", (gid,persona)).fetchone()
    if not old:
        raise ValueError("该身份与人格没有已观察会话")
    if optimistic and (type(data.get("revision")) is not int or data["revision"]!=old["revision"]):
        raise ValueError("关系已更新，请刷新后重新编辑")
    values={k:data[k] for k in ("role","affinity","familiarity_override") if k in data and data[k]!=old[k]}
    if values:
        db.execute("UPDATE identity_relationships SET "+",".join(k+"=?" for k in values)+
                   ",revision=revision+1 WHERE identity_id=? AND persona=?", (*values.values(),gid,persona))


def edit_profile(db, gid, data):
    old=db.execute("SELECT * FROM person_identities WHERE identity_id=?", (gid,)).fetchone()
    if not old:
        raise ValueError("全局身份不存在")
    if type(data.get("revision")) is not int or data["revision"]!=old["revision"]:
        raise ValueError("用户档案已更新，请刷新后重新编辑")
    values={}
    if "display_name" in data:
        values["display_name"]=_validated_name(data["display_name"],old,allow_empty=True)
        if old["display_name"] and old["display_name"]!=values["display_name"]:
            history=list(dict.fromkeys([old["display_name"],*json.loads(old["display_history"])]))
            values["display_history"]=dump(history[:200])
    if "custom_aliases" in data:
        if not isinstance(data["custom_aliases"],list) or len(data["custom_aliases"])>30:
            raise ValueError("通用别名最多30个")
        aliases={}
        for v in data["custom_aliases"]:
            a=_validated_name(v,old)
            aliases.setdefault(normalized(a),a)
        values["custom_aliases"]=dump(list(aliases.values()))
    values={k:v for k,v in values.items() if v!=old[k]}
    changes=data.get("relationships",[])
    if not isinstance(changes,list) or len(changes)>100:
        raise ValueError("关系编辑格式错误")
    personas=set()
    for r in changes:
        if not isinstance(r,dict) or not isinstance(r.get("persona"),str) or r["persona"] in personas:
            raise ValueError("关系人格无效或重复")
        personas.add(r["persona"])
        edit_relation(db,gid,r["persona"],r,optimistic=True)
    if values:
        db.execute("UPDATE person_identities SET "+",".join(k+"=?" for k in values)+
                   ",revision=revision+1,updated=? WHERE identity_id=?", (*values.values(),now(),gid))


def change_affinity(db, scope, ref, delta):
    """Aggregate a validated local learning event exactly once for its persona."""
    p=db.execute("SELECT blocked FROM people WHERE scope=? AND ref=?", (scope,ref)).fetchone()
    if not p or p["blocked"]:
        return
    g=affiliation(db,scope,ref)
    if not g:
        return
    db.execute("""UPDATE identity_relationships
        SET affinity=MIN(100,MAX(0,affinity+?)),revision=revision+1
        WHERE identity_id=? AND persona=? AND affinity!=MIN(100,MAX(0,affinity+?))""",
        (delta,g["identity_id"],g["persona"],delta))
