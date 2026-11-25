"""Unified identity without cross-session access. Synthetic conversations only."""
from __future__ import annotations

import asyncio
import json
import logging
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from astrbot_plugin_fiyu_core.core.engine import Engine
from astrbot_plugin_fiyu_core.core.store import Store, compact_person
from astrbot_plugin_fiyu_core.core.util import now, scope_key
from test_core import FakeHost, add_message, event, metadata


class IdentityV021Tests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=TemporaryDirectory()
        self.root=Path(self.tmp.name)
        self.host=FakeHost()
        self.engine=Engine(self.root,self.host,logging.getLogger("fiyu.identity.test"))
        self.store=self.engine.store
        cfg=self.engine.config.snapshot()
        cfg["access"].update(mode="active",allowed_groups=["g1","g2","g3"],
                             private_enabled=True,private_users=["alice","bob"])
        cfg["decision"].update(merge_wait_seconds=.03,merge_max_wait_seconds=.12,
                               ordinary_check_probability=0)
        cfg["reply"].update(gap_min=0,gap_max=0)
        cfg["dota"]["auto_sync"]=False
        self.engine.config.save(cfg,self.engine.config.revision())
        self.engine.running=True
        self.g1=metadata("g1")
        self.g2=metadata("g2")
        self.pm=metadata("alice","private",uid="alice")

    async def asyncTearDown(self):
        await self.engine.stop()
        self.tmp.cleanup()

    def msg(self,meta,uid,text,ext,**opts):
        return add_message(self.store,meta,uid,text,ext,**opts)

    def put(self,meta,row,title,related=()):
        return self.store.manual_memory(dict(scope=meta["scope"],subject=row["ref"],kind="core",
            topic_key=title,title=title,full_text=title,gist=title,trace=title,importance=90,
            confidence=.96,participant_refs=[row["ref"],*related],source_ids=[row["seq"]],
            evidence=[{"n":row["seq"],"speaker":row["ref"],"text":row["text"]}]))

    def triple(self):
        return (self.msg(self.g1,"alice","群一证据","g1-a",name="蓝蓝"),
                self.msg(self.g2,"alice","群二证据","g2-a",name="蓝二"),
                self.msg(self.pm,"alice","私聊证据","pm-a",name="私聊专属名字"))

    def identity(self,uid="alice",scope=""):
        matches=[r for r in self.store.list_identities(scope,uid) if r["uid"]==uid]
        self.assertEqual(len(matches),1)
        return matches[0]

    async def test_one_uid_three_memberships_and_identity_pagination(self):
        a,b,c=self.triple()
        self.msg(self.g1,"bob","同样的昵称","g1-b",name="蓝蓝")
        self.assertEqual(self.store.totals()["people"],2)
        self.assertEqual(self.store.totals()["memberships"],4)
        p=self.identity()
        self.assertEqual(set(m["ref"] for m in p["memberships"]),{a["ref"],b["ref"],c["ref"]})
        self.assertEqual((p["group_count"],p["private_count"]),(2,1))
        self.assertIn("私聊专属名字",p["historical_aliases"]+[p["name"]])
        self.assertEqual(self.identity(scope=self.g2["scope"])["identity_id"],p["identity_id"])
        self.assertEqual(len(self.store.list_identities(q="蓝蓝")),2)
        self.assertEqual(len(self.store.list_identities(q=b["ref"])),1)
        # The old endpoint remains strictly scoped for the memory editor.
        self.assertEqual([x["ref"] for x in self.store.list_people(self.pm["scope"])],[c["ref"]])
        page1=self.store.list_identities(limit=1)
        page2=self.store.list_identities(offset=1,limit=1)
        self.assertNotEqual(page1[0]["identity_id"],page2[0]["identity_id"])
        self.assertEqual(self.store.list_identities(offset=2,limit=1),[])
        self.msg(self.g2,"alice","昵称变了","rename",name="新昵称")
        self.assertEqual(self.identity()["identity_id"],p["identity_id"])
        old=self.store.list_identities(q="蓝二")
        self.assertEqual([x["identity_id"] for x in old],[p["identity_id"]])
        # A different robot instance cannot be linked from the same opaque ID.
        foreign={**self.g1,"platform_id":"second-account"}
        foreign["scope"]=scope_key(foreign["platform_id"],foreign["kind"],foreign["location"],foreign["persona"])
        foreign["umo"]="second-account:GroupMessage:g1"
        self.msg(foreign,"alice","同样的ID但另一个机器人","foreign")
        matches=[r for r in self.store.list_identities(q="alice") if r["uid"]=="alice"]
        self.assertEqual(len(matches),2)
        self.assertEqual(len({x["identity_id"] for x in matches}),2)
        # New, genuinely distinct identities with the same name are not merged.
        self.assertEqual(self.store.totals()["people"],3)

    async def test_global_nickname_local_override_and_admin_search(self):
        a,b,c=self.triple()
        self.store.edit_person(self.g1["scope"],"alice",
            {"display_name":"群一称呼","custom_aliases":["群一别名"],"note":"群一备注"})
        p=self.identity()
        patch={"revision":p["revision"],"display_name":"蓝鲸",
               "custom_aliases":["小鲸","小鲸"],"relationships":[]}
        self.store.edit_identity(p["identity_id"],patch)
        p=self.identity()
        self.assertEqual(p["custom_aliases"],["小鲸"])
        for meta,expected in ((self.g1,"群一称呼"),(self.g2,"蓝鲸"),(self.pm,"蓝鲸")):
            row=self.store.scope_people(meta["scope"])[0]
            self.assertEqual(compact_person(row)["name"],expected)
            self.assertEqual(self.store.resolve_people(meta["scope"],"小鲸")[0]["ref"],row["ref"])
        self.assertEqual(self.store.resolve_people(self.g2["scope"],"群一别名"),[])
        self.assertEqual(self.store.resolve_people(self.g1["scope"],"私聊专属名字"),[])
        self.assertNotIn("私聊专属名字",str(self.store.scope_people(self.g1["scope"])))
        self.assertNotIn("群一备注",str(self.store.scope_people(self.g2["scope"])))
        self.assertEqual(self.store.resolve_people(metadata("g3")["scope"],p["identity_id"]),[])
        # Admin memory search may find all linked memories across scopes.
        mid1=self.put(self.g1,a,"测试记忆一")
        mid2=self.put(self.g2,b,"测试记忆二")
        mid3=self.put(self.pm,c,"测试记忆三")
        for q in (p["identity_id"],"小鲸","蓝鲸","alice"):
            self.assertEqual({m["id"] for m in self.store.list_memories(q=q)},{mid1,mid2,mid3})
        unrelated=self.msg(self.g1,"bob","另一个人的事实","unrelated",name="另一位")
        self.put(self.g1,unrelated,"仅在正文中提及"+p["identity_id"])
        self.assertEqual({m["id"] for m in self.store.list_memories(q=p["identity_id"])},{mid1,mid2,mid3})
        self.assertEqual({m["id"] for m in self.store.list_memories(self.g2["scope"],p["identity_id"])},{mid2})
        self.assertEqual(self.store.list_people(self.g2["scope"],"小鲸")[0]["ref"],b["ref"])
        self.store.edit_identity(p["identity_id"],{"revision":p["revision"],"display_name":"新鲸"})
        latest=self.identity()
        self.assertIn("蓝鲸",latest["historical_aliases"])
        self.assertEqual([x["identity_id"] for x in self.store.list_identities(q="蓝鲸")],[p["identity_id"]])
        self.assertEqual(len(self.store.list_memories(q="蓝鲸")),3)
        # Fresh scopes also get the shared profile when the same ID is observed.
        fresh=self.msg(metadata("g3"),"alice","新会话","fresh",name="群三名字")
        self.assertEqual(fresh["name"],"新鲸")
        self.assertEqual(self.identity()["membership_count"],4)
        restarted=Store(self.store.path)
        self.assertEqual(restarted.list_identities(q=p["identity_id"])[0]["display_name"],"新鲸")
        with self.assertRaises(ValueError):
            self.store.edit_identity(p["identity_id"],patch)  # stale prior revision
        for invalid in ("主人","服务器管理员","alice",a["ref"]):
            with self.assertRaises(ValueError):
                self.store.edit_identity(latest["identity_id"],{"revision":latest["revision"],"display_name":invalid})

    async def test_one_relation_per_persona_and_local_rules_unchanged(self):
        a,b,c=self.triple()
        other=metadata("g1",persona="another-persona")
        d=self.msg(other,"alice","另一个人格","other")
        p=self.identity()
        rels={x["persona"]:x for x in p["relationships"]}
        self.assertEqual(len(rels),2)
        self.store.edit_identity(p["identity_id"],{"revision":p["revision"],"relationships":[
            {"persona":"fish","revision":rels["fish"]["revision"],"affinity":83,"role":"friend",
             "familiarity_override":"close"}]})
        self.store.interaction(self.g1["scope"],"alice")
        self.store.interaction(self.g2["scope"],"alice")
        self.store.interaction(self.pm["scope"],"alice")
        self.store.interaction(other["scope"],"alice")
        # Learning in one scope updates just the common relationship.
        self.store.commit_memory([],[(self.g1["scope"],a["ref"],2)],[])
        for meta,row in ((self.g1,a),(self.g2,b),(self.pm,c)):
            current=self.store.person(meta["scope"],"alice")
            self.assertEqual(current["affinity"],85)
            self.assertEqual(current["role"],"friend")
            self.assertEqual(current["interactions"],3)
            self.assertEqual(current["scope_interactions"],1)
            self.assertEqual(self.store.recent_interactions(meta["scope"])[row["ref"]],3)
        self.assertEqual(self.store.person(other["scope"],"alice")["affinity"],50)
        self.assertEqual(self.store.recent_interactions(other["scope"])[d["ref"]],1)
        self.store.edit_person(self.g2["scope"],"alice",
            {"blocked":True,"deny_keywords":["专属关键词"],"note":"会话限定","habit_opt_in":True})
        self.assertTrue(self.store.person(self.g2["scope"],"alice")["blocked"])
        self.assertFalse(self.store.person(self.pm["scope"],"alice")["blocked"])
        self.assertFalse(self.store.person(self.pm["scope"],"alice")["habit_opt_in"])
        self.assertEqual(self.store.person(self.pm["scope"],"alice")["deny_keywords"],"[]")
        self.assertEqual(self.identity()["blocked_count"],1)
        self.store.commit_memory([],[(self.g2["scope"],b["ref"],2)],[])
        self.assertEqual(self.store.person(self.g1["scope"],"alice")["affinity"],85)
        # Editing just a scoped rule must not reset any shared settings.
        self.store.edit_person(self.g1["scope"],"alice",{"note":"新备注"})
        self.assertEqual(self.store.person(self.pm["scope"],"alice")["affinity"],85)
        # Backwards-compatible admin calls with affinity update the scoped persona.
        self.store.edit_person(self.pm["scope"],"alice",{"affinity":80})
        self.assertEqual(self.store.person(self.g1["scope"],"alice")["affinity"],80)
        self.assertEqual(self.store.person(other["scope"],"alice")["affinity"],50)
        before=self.identity()
        shared=next(r for r in before["relationships"] if r["persona"]=="fish")
        with self.assertRaises(ValueError):
            self.store.edit_identity(before["identity_id"],{"revision":before["revision"],"display_name":"不会保存",
                "relationships":[{"persona":"fish","revision":shared["revision"]-1,"affinity":10}]})
        self.assertEqual(self.identity()["display_name"],before["display_name"])
        self.assertEqual(self.store.person(self.g1["scope"],"alice")["affinity"],80)
        reopened=Store(self.store.path)
        self.assertEqual(reopened.person(self.pm["scope"],"alice")["affinity"],80)

    async def test_shared_profile_does_not_bridge_private_or_group_contexts(self):
        a=self.msg(self.g1,"alice","GroupOneOnlyMarker","g1-sec",name="群昵称")
        b=self.msg(self.g2,"alice","GroupTwoOnlyMarker","g2-sec",name="另一个群昵称")
        c=self.msg(self.pm,"alice","PrivateOnlyMarker","pm-sec",name="PrivateNicknameMarker")
        self.put(self.g1,a,"GroupOneMemoryMarker")
        self.put(self.g2,b,"GroupTwoMemoryMarker")
        self.put(self.pm,c,"PrivateMemoryMarker")
        self.store.edit_person(self.pm["scope"],"alice",{"note":"PrivateNoteMarker","custom_aliases":["PrivateAliasMarker"]})
        p=self.identity()
        self.store.edit_identity(p["identity_id"],{"revision":p["revision"],"display_name":"普通群友"})
        self.store.interaction(self.pm["scope"],"alice")
        def respond(prompt,system,kwargs):
            data=json.loads(prompt)
            self.assertIn("GroupOne",prompt)
            for value in ("PrivateOnlyMarker","PrivateMemoryMarker","PrivateNicknameMarker","PrivateNoteMarker",
                          "PrivateAliasMarker","GroupTwoOnlyMarker","GroupTwoMemoryMarker",p["identity_id"]):
                self.assertNotIn(value,prompt)
            member=next(x for x in data["participants"] if x["speaker"]==a["ref"])
            self.assertEqual(member["name"],"普通群友")
            self.assertEqual(member["interactions"],1)
            return "收到。"
        self.host.responses.append(respond)
        await self.engine.on_event(event("group-test","GroupOneMemoryMarker",direct=True))
        for _ in range(300):
            if all(not s.pending and (not s.worker or s.worker.done()) for s in self.engine.states.values()):
                break
            await asyncio.sleep(.01)
        self.assertEqual([t for _,t in self.host.sent],["收到。"])
        for meta,row,query in ((self.g1,a,"PrivateMemoryMarker"),(self.pm,c,"GroupOneMemoryMarker")):
            cfg=self.engine.config.snapshot()
            result=await self.engine.memory.recall(meta,cfg,row["ref"],query=query,who=p["identity_id"],deep=True,historical=True)
            self.assertNotIn(query,json.dumps(result,ensure_ascii=False))
            rows=self.store.recall_candidates(meta["scope"],row["ref"],None,True,True,None,None)
            self.assertTrue(all(r["scope"]==meta["scope"] for r in rows))
        # There is no new chat-reading tool, and cross-scope associations are still rejected.
        with self.assertRaises(ValueError):
            self.put(self.g1,a,"不可跨会话关联",[c["ref"]])


if __name__=="__main__":
    unittest.main()
