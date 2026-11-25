"""Exercise exact prior releases, not a synthetic new-code schema.

Run from the plugin's parent directory:
  python astrbot_plugin_fiyu_core/tests/verify_upgrade_v012.py /path/to/Fiyu-Core-v0.1.2.zip
  python astrbot_plugin_fiyu_core/tests/verify_upgrade_v012.py /path/to/Fiyu-Core-v0.2.0.zip
  python astrbot_plugin_fiyu_core/tests/verify_upgrade_v012.py /path/to/Fiyu-Core-v0.2.2.zip
  python astrbot_plugin_fiyu_core/tests/verify_upgrade_v012.py /path/to/Fiyu-Core-v0.2.3.zip
  python astrbot_plugin_fiyu_core/tests/verify_upgrade_v012.py /path/to/Fiyu-Core-v0.2.4.zip
  python astrbot_plugin_fiyu_core/tests/verify_upgrade_v012.py /path/to/Fiyu-Core-v0.2.5.zip
  python astrbot_plugin_fiyu_core/tests/verify_upgrade_v012.py /path/to/Fiyu-Core-v0.2.6.zip

All versions operate solely on a fresh temporary fixture. No real bot data/API.
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory


OLD_FIXTURE = r"""
from pathlib import Path
from astrbot_plugin_fiyu_core import __version__
from astrbot_plugin_fiyu_core.core.config import ConfigStore
from astrbot_plugin_fiyu_core.core.store import Store
from astrbot_plugin_fiyu_core.core.util import scope_key, now, day_key
assert __version__ in ("0.1.2","0.2.0","0.2.1","0.2.2","0.2.3","0.2.4","0.2.5","0.2.6")
v3=__version__!="0.1.2"
v4=__version__ not in ("0.1.2","0.2.0")
root=Path("fixture")
root.mkdir()
conf=ConfigStore(root)
cfg=conf.snapshot()
cfg["access"].update(mode="observe", allowed_groups=["test-group-a","test-group-b"],
                     private_enabled=True, private_users=["test-person-0"])
cfg["persona"].update(persona_id="test-fish", context_messages=50)
cfg["models"].update(chat_provider="test-chat",decision_provider="test-judge",
                     memory_provider="test-memory",embedding_provider="test-vector")
cfg["memory"].update(core_blur_after_days=0,auto_recall_count=3)
cfg["budget"].update(daily_tokens=2000000)
cfg["dota"].update(existing_snapshots_dir="C:/test/snapshots",auto_sync=False)
if v4:
    cfg["research"].update(sources=["cs2_news","dota_meta"],daily_topics=2)
if __version__ in ("0.2.5", "0.2.6"):
    cfg["reply"].update(split_chars=420,split_on_newline=False,max_tool_rounds=2,max_tool_calls=4)
conf.save(cfg,conf.revision())
store=Store(root/"fiyu.sqlite3")
t=now()
for ix,(location,session_kind,count,npeople) in enumerate([
    ("test-group-a","group",25,7),("test-group-b","group",3,1),("test-person-0","private",9,1)
]):
    sc=scope_key("test-platform",session_kind,location,"test-fish")
    kind="GroupMessage" if session_kind=="group" else "FriendMessage"
    meta=dict(scope=sc,platform="qq_official",platform_id="test-platform",kind=session_kind,
              location=location,persona="test-fish",umo=f"test-platform:{kind}:{location}")
    refs=[]
    for j in range(npeople):
        uid=f"test-person-{j}"
        row=store.ingest(meta,dict(ext_id=f"member-{j}",uid=uid,name=f"测试昵称{j}",
              text="本人确认的测试事实",ts=t-1000,bot=False))
        refs.append(row["ref"])
    with store.connection() as db:
        if v3:
            db.execute("UPDATE people SET role=?,display_name=?,affinity=?,familiarity_override=? WHERE scope=? AND uid='test-person-0'",
                ("friend" if ix==0 else "member",f"原会话称呼{ix}",(89,62,50)[ix],("close","normal","auto")[ix],sc))
        else:
            db.execute("UPDATE people SET role='owner',display_name='老大',affinity=89 WHERE scope=? AND uid='test-person-0'",(sc,))
            store._record_alias(db,sc,refs[0],"老大","manual",t-100)
            store._record_alias(db,sc,refs[0],"老大","display",t-100)
        store._record_alias(db,sc,refs[0],"旧昵称","observed",t-200)
        store._record_alias(db,sc,refs[0],"正常别名","manual",t-100)
    for i in range(count):
        j=i%npeople
        row=store.ingest(meta,dict(ext_id=f"evidence-{i}",uid=f"test-person-{j}",name=f"测试昵称{j}",
                         text=f"第{i}条测试证据",ts=t-60-i,bot=False))
        kind=("core","profile","experience","ordinary","temporary")[i%5]
        subject=refs[j] if i%4 else "group"
        title="老大的测试事实" if j==0 else f"测试事实{i}"
        p=dict(scope=sc,subject=subject,kind=kind,topic_key=f"fixture-{i}",title=title,
               full_text=title+f"详细内容{i}",gist=title,trace="印象",importance=70+i%30,
               confidence=.92,pinned=i%7==0,participant_refs=[refs[j],refs[(j+1)%npeople]],
               source_ids=[row["seq"]],evidence=[{"n":row["seq"],"speaker":row["ref"],"text":row["text"]}],
               created=t-60-i,valid_until=t+86400 if kind=="temporary" else None)
        mid=store.manual_memory(p)
        if i==0:
            store.manual_memory({**p,"full_text":p["full_text"]+"（后续修正）"},mid)
            store.save_vectors("test-vector",[(mid,2,[1.0,0.0])])
        if i==1:
            with store.connection() as db:
                db.execute("UPDATE memories SET state='archived',archived_at=?,clarity='fuzzy' WHERE id=?",(t,mid))
        store.mark_recalled([mid],False)
    store.commit_styles(sc,[dict(kind="expression",pattern="测试用语",meaning="合成描述",example="合成例子",confidence=.95)],[],True)
    store.event_count(sc,"received",count)
    if v3:
        for n in range(ix+1):
            store.interaction(sc,"test-person-0",t-100+ix*20+n)
        if ix==0:
            store.set_habit_consent(sc,"test-person-0",True)
            row=store.ingest(meta,dict(ext_id="habit-source",uid="test-person-0",name="测试昵称0",
                text="通常周末玩游戏",ts=now(),bot=False))
            with store.connection() as db:
                db.execute("INSERT INTO habit_observations VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (sc,refs[0],"game:test","游戏","game","any",day_key(row["ts"]),row["seq"],"support",.9))
if v4:
    run=store.claim_research("test-fish",3,15,at=t-7200)
    assert store.finish_research(run,"completed","cs2_news","原有公开见闻","",100,
        note={"topic_key":"cs2_news:old","evidence_key":"old-evidence",
              "facts":[{"text":"原有公开事实","refs":["s1"]}],
              "opinion":{"text":"","refs":[]},"sources":[{
                "id":"s1","url":"https://store.steampowered.com/news/app/730/view/123",
                "title":"测试旧公告","date":day_key()}],"expires":t+86400})
    run=store.claim_research("test-fish",3,15,at=t-3600)
    store.finish_research(run,"failed","cs2_news","原有失败任务","TimeoutError",613)
    store.add_usage(day_key(),"research","test-chat",False,296,0,317,False,3265)
store.add_usage(day_key(),"reply","test-chat",False,123,45,67,False,10)
with store.connection() as db:
    assert db.execute("PRAGMA user_version").fetchone()[0]==(4 if v4 else 3 if v3 else 2)
    assert db.execute("SELECT COUNT(*) FROM memories").fetchone()[0]==37
"""


TABLES = ("sessions","messages","memories","memory_people","memory_versions","vectors",
          "styles","style_versions","daily_events","usage","image_cache","kv")


def snapshot(path,tables=TABLES):
    with closing(sqlite3.connect(path)) as db:
        assert db.execute("PRAGMA quick_check").fetchone()==("ok",)
        assert db.execute("PRAGMA foreign_key_check").fetchone() is None
        return {name:db.execute("SELECT * FROM "+name+" ORDER BY rowid").fetchall() for name in tables}


def contains_old_values(old,new):
    for key,value in old.items():
        if isinstance(value,dict):
            contains_old_values(value,new[key])
        else:
            assert new[key]==value,(key,value,new[key])


def verify(previous):
    root=Path(__file__).resolve().parents[2]
    sys.path.insert(0,str(root))
    from astrbot_plugin_fiyu_core import __version__
    from astrbot_plugin_fiyu_core.core.config import ConfigStore
    from astrbot_plugin_fiyu_core.core.store import Store
    assert __version__=="0.2.7"
    with TemporaryDirectory(prefix="fiyu-upgrade-fixture-") as d:
        subprocess.run([sys.executable,"-c",OLD_FIXTURE],cwd=d,check=True,
                       env={**os.environ,"PYTHONPATH":str(previous),"PYTHONDONTWRITEBYTECODE":"1"})
        data=Path(d)/"fixture"
        path=data/"fiyu.sqlite3"
        with closing(sqlite3.connect(path)) as db:
            schema=db.execute("PRAGMA user_version").fetchone()[0]
            all_tables=tuple(r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"))
        tables=TABLES+("people","person_aliases","relationship_days","habit_observations",
                     "research_runs","research_notes","relay_jobs") if schema==3 else TABLES
        if schema==4:
            tables=all_tables
        expected=snapshot(path,tables)
        assert len(expected["memories"])==37
        config_bytes=(data/"config.json").read_bytes()
        with closing(sqlite3.connect(path)) as db:
            old_people=db.execute("SELECT scope,uid,ref,name,display_name,role,affinity,blocked,note FROM people ORDER BY scope,uid").fetchall()
            old_aliases=db.execute("SELECT scope,person_ref,alias,kind FROM person_aliases").fetchall()
        config=ConfigStore(data)
        assert not config.error
        expected_config=json.loads(config_bytes)
        if "split_on_newline" not in expected_config["reply"]:
            assert expected_config["reply"]["split_chars"]==150
            expected_config["reply"]["split_chars"]=300
        contains_old_values(expected_config,config.snapshot())
        assert config.snapshot()["reply"]["vision_strategy"] == "economy"
        assert config.snapshot()["reply"]["split_on_newline"] is expected_config["reply"].get("split_on_newline", True)
        assert config.snapshot()["research"]["enabled"] is False
        upgraded=Store(path)
        assert snapshot(path,tables)==expected
        assert (data/"config.json").read_bytes()==config_bytes
        if schema==4:
            assert config.snapshot()["tools"]["contextual_tools"] is True
            assert config.snapshot()["dota"]["knowledge_auto_sync"] is True
            assert config.snapshot()["research"]["news_transport"]=="rss_first"
            assert config.snapshot()["research"]["network_timeout"]==30
            assert config.snapshot()["research"]["sources"]==["cs2_news","dota_meta"]
            assert len(upgraded.research_runs())==2
            assert len(upgraded.research_notes())==1
            assert not list((data/"upgrade_backups").glob("*/fiyu-before-v0.2.1.sqlite3"))
            with upgraded.connection() as db:
                assert db.execute("PRAGMA user_version").fetchone()[0]==4
            Store(path)
            assert snapshot(path,tables)==expected
            print(f"Actual {previous.name} -> v{__version__}: verified all {len(tables)} tables, 37 memories, effective settings and original config bytes.")
            return
        old_version="0.2.0" if schema==3 else "0.1.2"
        backups=list((data/"upgrade_backups").glob(f"v{old_version}-*/fiyu-before-v0.2.1.sqlite3"))
        assert len(backups)==1
        assert snapshot(backups[0],tables)==expected
        assert (backups[0].parent/"config-before-v0.2.1.json").read_bytes()==config_bytes
        with closing(sqlite3.connect(backups[0])) as db:
            assert db.execute("PRAGMA user_version").fetchone()==(schema,)
        with upgraded.connection() as db:
            assert db.execute("PRAGMA user_version").fetchone()[0]==4
            people=db.execute("SELECT scope,uid,ref,name,display_name,role,affinity,blocked,note FROM people ORDER BY scope,uid").fetchall()
            expected_people=[(*r[:4],"" if r[4]=="老大" else r[4],"friend" if r[5]=="owner" else r[5],*r[6:]) for r in old_people] if schema==2 else old_people
            assert [tuple(r) for r in people]==expected_people
            aliases={tuple(r) for r in db.execute("SELECT scope,person_ref,alias,kind FROM person_aliases")}
            for row in old_aliases:
                assert (*row[:3],"imported" if row[2]=="老大" and row[3]=="manual" else row[3]) in aliases
            if schema==2:
                assert db.execute("SELECT COUNT(*) FROM people WHERE interactions!=0 OR habit_opt_in!=0").fetchone()[0]==0
            assert db.execute("SELECT COUNT(*) FROM person_identities").fetchone()[0]==7
            assert db.execute("SELECT COUNT(*) FROM identity_memberships").fetchone()[0]==9
        person=upgraded.list_identities(q="test-person-0")[0]
        assert person["membership_count"]==3
        assert len(person["relationships"])==1
        rel=person["relationships"][0]
        assert rel["affinity"]==(62 if schema==3 else 89)
        assert rel["interactions"]==(6 if schema==3 else 0)
        assert len(upgraded.list_memories(q=person["identity_id"]))>0
        Store(path)
        assert snapshot(path,tables)==expected
        assert len(list((data/"upgrade_backups").glob("*/fiyu-before-v0.2.1.sqlite3")))==1
        print(f"Actual v{old_version} -> v{__version__}: 37 memories, 3 isolated scopes, 9 memberships -> 7 unique users, "+
              f"{len(expected['memory_people'])} identity links, revisions/vectors/styles/usage/config preserved; "+
              f"verified schema-{schema} backup, schema-4 migration, repeat start and SQLite integrity.")


if __name__=="__main__":
    if len(sys.argv)!=2 or not Path(sys.argv[1]).is_file():
        raise SystemExit("Provide a release ZIP from v0.1.2 or v0.2.0–v0.2.6 as the only argument.")
    verify(Path(sys.argv[1]).resolve())
