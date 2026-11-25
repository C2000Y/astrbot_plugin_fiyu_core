"""v0.2.0 regression tests. All model/network/QQ calls are simulated."""
from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import unittest
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory

from astrbot_plugin_fiyu_core.core.config import ConfigError, defaults, validate
from astrbot_plugin_fiyu_core.core.engine import Engine, SessionState
from astrbot_plugin_fiyu_core.core.habits import normalize_habits
from astrbot_plugin_fiyu_core.core.memory import model_memory_rows, normalize_proposals
from astrbot_plugin_fiyu_core.core.personality import (
    cooling_phrases, energy_value, familiarity, light_filter, scene_for, select_examples,
)
from astrbot_plugin_fiyu_core.core.relay import parse_request
from astrbot_plugin_fiyu_core.core.research import PRO_LIST, PRO_MATCH, STEAM_NEWS
from astrbot_plugin_fiyu_core.core.store import Store
from astrbot_plugin_fiyu_core.core.util import CN_TZ, day_key, now, atomic_json
from test_core import FakeHost, metadata, add_message, event, fixture, public_fixture

LOGGER=logging.getLogger("fiyu.v020.test")
LOGGER.addHandler(logging.NullHandler())


class SettingsAndPersonalityTests(unittest.TestCase):
    def test_old_config_merges_new_fields_and_validates_options(self):
        old=defaults()
        for key in ("social","habits","research","relay","interrupt"):
            old.pop(key)
        old["models"].pop("research_provider")
        old["budget"]["module_call_limits"].pop("research")
        old["access"]["allowed_groups"]=["g1"]
        cfg=validate(old)
        self.assertEqual(cfg["access"]["allowed_groups"],["g1"])
        self.assertFalse(cfg["relay"]["enabled"])
        self.assertFalse(cfg["research"]["enabled"])
        self.assertEqual(cfg["research"]["daily_topics"],1)
        self.assertEqual(cfg["research"]["daily_tokens"],10000)
        for path,value in ((("research","sources"),["arbitrary_url"]),
                           (("relay","group_aliases"),{"群":"not-a-umo"}),
                           (("habits","min_days"),1),
                           (("social","examples"),[{"scene":"unknown","user":"a","assistant":"b"}])):
            bad=defaults()
            bad[path[0]][path[1]]=value
            with self.assertRaises(ConfigError):
                validate(bad)

    def test_scene_examples_cooldown_energy_and_technical_protection(self):
        s=defaults()["social"]
        # Test recovery within one time-of-day band, independent of wall time.
        t=datetime(2026,11,20,13,tzinfo=CN_TZ).timestamp()
        rows=[{"bot":True,"text":"确实，这波","ts":t-10},{"bot":True,"text":"确实","ts":t-20}]
        self.assertIn("确实",cooling_phrases(s,rows,t))
        self.assertEqual(cooling_phrases(s,rows,t+3600),[])
        s["examples"]=[{"scene":"casual","user":"吃饭","assistant":"确实"},{"scene":"casual","user":"吃饭","assistant":"老大好"}]
        native=[{"role":"user","content":"SAP代码"},{"role":"assistant","content":"分析调用顺序"}]
        chosen=select_examples(s,"casual","吃饭",native,["确实"])
        self.assertLessEqual(len(chosen),6)
        self.assertTrue(all("老大" not in x["content"] and "确实" not in x["content"] and "SAP" not in x["content"] for x in chosen))
        self.assertEqual(scene_for("请详细解释这个SAP报错","banter","short"),("technical","detailed"))
        text="好的，我来帮你：可以呀"
        self.assertEqual(light_filter(text,s,"casual"),("可以呀",True))
        self.assertEqual(light_filter(text,s,"technical"),(text,False))
        self.assertEqual(light_filter("笑死，这波有点意思",s,"banter",["确实","笑死"]),("这波有点意思",True))
        self.assertEqual(light_filter("确实！",s,"casual",["确实"]),("确实！",False))
        self.assertEqual(light_filter("确实，这个参数是 10",s,"technical",["确实"]),("确实，这个参数是 10",False))
        st=SessionState()
        first=energy_value(st,s,t)
        low=energy_value(st,s,t,spent=3)
        recovered=energy_value(st,s,t+3600)
        self.assertLess(low,first)
        self.assertGreater(recovered,low)
        self.assertEqual(familiarity({"interactions":60,"affinity":2},0)["familiarity"],"常聊")
        self.assertEqual(familiarity({"interactions":1,"affinity":99},0)["familiarity"],"初识")

    def test_relay_parser_only_explicit_request(self):
        cfg=defaults()["relay"]
        cfg["group_aliases"]={"刀塔群":"test:GroupMessage:g1","群":"test:GroupMessage:g2"}
        for text in ("如果我让你去刀塔群发：你好，行吗？","A说去刀塔群发：你好","引用「去刀塔群发：你好」"):
            self.assertIsNone(parse_request(text,cfg,["肥鱼"]))
        a=parse_request("肥鱼，帮我去刀塔群说一下，今晚九点开黑",cfg,["肥鱼"])
        self.assertEqual(a,{"action":"request","alias":"刀塔群","body":"今晚九点开黑","rewrite":False})
        self.assertTrue(parse_request("去刀塔群组织语言发：今晚开黑",cfg)["rewrite"])
        self.assertEqual(parse_request("去不存在的群发：hi",cfg)["action"],"clarify")


class DevelopmentTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=TemporaryDirectory()
        self.root=Path(self.tmp.name)
        self.host=FakeHost()
        self.e=Engine(self.root,self.host,LOGGER)
        cfg=self.e.config.snapshot()
        cfg["access"].update(mode="active",allowed_groups=["g1","g2"],private_enabled=True,private_users=["alice","bob"])
        cfg["decision"].update(merge_wait_seconds=0.03,merge_max_wait_seconds=0.12,ordinary_check_probability=0,unsolicited_gap_seconds=0)
        cfg["reply"].update(gap_min=0,gap_max=0,split_chars=80)
        cfg["dota"]["auto_sync"]=False
        cfg["models"].update(chat_provider="chat",decision_provider="decision",memory_provider="memory",research_provider="research")
        self.e.config.save(cfg,self.e.config.revision())
        self.e.running=True
        self.group=metadata("g1")
        self.private=metadata("alice","private",uid="alice")

    async def asyncTearDown(self):
        await self.e.stop()
        self.tmp.cleanup()

    def update(self,section,**values):
        cfg=self.e.config.snapshot()
        cfg[section].update(values)
        self.e.config.save(cfg,self.e.config.revision())

    async def idle(self):
        for _ in range(400):
            if all(not s.pending and (not s.worker or s.worker.done()) for s in self.e.states.values()):
                return
            await asyncio.sleep(0.01)
        self.fail("worker did not finish")

    def ingest(self,meta,uid,text,key,**opts):
        return add_message(self.e.store,meta,uid,text,key,**opts)

    def authorize_habit(self,meta,uid,at):
        self.e.store.set_habit_consent(meta["scope"],uid,True)
        # Test historical samples after a simulated prior consent; never touch real data.
        with self.e.store.connection() as db:
            db.execute("UPDATE people SET habit_since=? WHERE scope=? AND uid=?",(at,meta["scope"],uid))

    def candidate(self,row,activity="Dota 2",category="game",period="evening",signal="support"):
        return {"habits":[{"subject":row["ref"],"category":category,"activity":activity,"period":period,"signal":signal,
                    "confidence":0.95,"source_ids":[row["seq"]]}]}

    def commit_habit(self,meta,row,**kwargs):
        cfg=self.e.config.snapshot()
        obs=normalize_habits(self.candidate(row,**kwargs),[row],self.e.store.scope_people(meta["scope"]),cfg)
        return self.e.store.commit_memory([],[],[row["seq"]],obs,cfg["habits"])

    async def test_independent_persona_hides_old_role_and_counts_one_interaction(self):
        a=self.ingest(self.group,"alice","hi","a",name="正常昵称")
        self.e.store.edit_person(self.group["scope"],"alice",{"role":"friend","affinity":88,
                 "note":"服务器管理员是PrivateAdminMarker"})
        with self.e.store.connection() as db:
            self.e.store._record_alias(db,self.group["scope"],a["ref"],"老大","imported",now())
        async def persona(event,cfg):
            return {"id":"fish","system":"你是大肥鱼，主人是PrivateAdminMarker。喜欢白饭。",
                    "examples":[{"role":"user","content":"你好老大"},{"role":"assistant","content":"主人好"}]}
        self.host.persona=persona
        def answer(prompt,system,kwargs):
            data=json.loads(prompt)
            self.assertNotIn("PrivateAdminMarker",system)
            self.assertNotIn("PrivateAdminMarker",prompt)
            p=next(x for x in data["participants"] if x["speaker"]==a["ref"])
            self.assertNotIn("owner",str(p))
            self.assertNotIn("老大",str(p))
            self.assertEqual(p["affinity"],88)
            self.assertEqual(p["familiarity"],"熟悉")
            self.assertIn("独立的平等群友",system)
            self.assertIn("喜欢白饭",system)
            self.assertNotIn("主人好",str(kwargs["contexts"]))
            return "这是测试内容。" * 40
        self.host.responses.append(answer)
        await self.e.on_event(event("q1","你好肥鱼",direct=True))
        await self.idle()
        self.assertGreater(len(self.host.sent),1)
        person=self.e.store.person(self.group["scope"],"alice")
        self.assertEqual(person["interactions"],1)
        self.assertEqual(person["affinity"],88)
        self.assertEqual(self.e.store.recent_interactions(self.group["scope"])[a["ref"]],1)
        self.assertEqual(len(self.host.calls),1)

    async def test_legacy_preview_scope_revision_and_original_evidence(self):
        a=self.ingest(self.group,"alice","我的生日","a",name="小蓝")
        other=metadata("g2")
        b=self.ingest(other,"alice","我的生日","b",name="小蓝")
        def insert(meta,row):
            return self.e.store.manual_memory(dict(scope=meta["scope"],subject=row["ref"],kind="core",topic_key="birthday",
                title="老大的生日",full_text="老大生日在七月；他说「老大加油」。",gist="老大生日在七月",
                trace="老大生日",importance=90,confidence=.95,evidence=[{"n":row["seq"],"speaker":row["ref"],"text":"原话老大"}],
                source_ids=[row["seq"]],participant_refs=[row["ref"]]))
        mid=insert(self.group,a)
        mid2=insert(other,b)
        before=self.e.store.list_memories(self.group["scope"])[0]
        plan=self.e.store.legacy_preview(self.group["scope"],a["ref"],"老大","小蓝")
        self.assertEqual([r["id"] for r in plan["rows"]],[mid])
        with self.assertRaises(ValueError):
            self.e.store.legacy_apply(self.group["scope"],a["ref"],"老大","小蓝",plan["revision"],[mid2])
        self.e.store.legacy_apply(self.group["scope"],a["ref"],"老大","小蓝",plan["revision"],[mid],True)
        after=self.e.store.list_memories(self.group["scope"])[0]
        self.assertIn("小蓝生日",after["full_text"])
        self.assertIn("「老大加油」",after["full_text"])
        for key in ("id","subject","evidence","source_ids","updated","created","state"):
            self.assertEqual(before[key],after[key])
        self.assertIn("老大",self.e.store.list_memories(other["scope"])[0]["title"])
        self.assertEqual(len(self.e.store.memory_versions(mid)),1)
        view=model_memory_rows([after],self.e.config.snapshot(),set())
        self.assertEqual(len(view),1)
        self.assertIn("小蓝生日",view[0]["full_text"])
        self.assertNotIn("老大",view[0]["full_text"])
        self.assertIn("老大",after["full_text"])
        self.update("models",embedding_provider="embedding")
        sent=[]
        async def embed(pid,texts):
            sent.extend(texts)
            return [[1.0,0.0] for _ in texts]
        self.host.embed=embed
        self.assertTrue(await self.e.memory.embed_once(self.e.config.snapshot()))
        self.assertEqual(len(sent),1)
        self.assertNotIn("老大",sent[0])
        with self.assertRaises(ValueError):
            self.e.store.legacy_apply(self.group["scope"],a["ref"],"老大","小蓝",plan["revision"],[mid])

    async def test_normal_memory_rejects_inferred_physical_schedule(self):
        row=self.ingest(self.group,"alice","刚到家","arrival")
        base=dict(subject=row["ref"],key="routine",kind="profile",title="通勤印象",
                  full="她平时大概七点下班",gist="通常七点下班",trace="下班规律",
                  source_ids=[row["seq"]],importance=85,confidence=.98)
        for kind in ("core","profile","experience","ordinary"):
            data={"memories":[{**base,"kind":kind}]}
            accepted,_=normalize_proposals(data,self.group["scope"],[row],self.e.config.snapshot()["memory"])
            self.assertEqual(accepted,[])

    async def test_habits_cross_days_dedup_opt_out_and_no_physical_tracking(self):
        t=now()
        a=self.ingest(self.group,"alice","晚上打Dota","a",ts=t-5*86400)
        self.assertEqual(self.commit_habit(self.group,a),[])
        self.authorize_habit(self.group,"alice",t-6*86400)
        for i in range(4):
            r=self.ingest(self.group,"alice","晚上玩Dota","same"+str(i),ts=t-4*86400+i)
            self.assertEqual(self.commit_habit(self.group,r),[])
        with self.e.store.connection() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM habit_observations").fetchone()[0],1)
        for i in (3,2):
            r=self.ingest(self.group,"alice","晚上玩Dota","day"+str(i),ts=t-i*86400)
            self.commit_habit(self.group,r)
        habits=self.e.store.list_memories(self.group["scope"],kind="habit")
        self.assertEqual(len(habits),1)
        h=habits[0]
        self.assertIn("3个不同日期",h["full_text"])
        self.assertIn("晚间",h["full_text"])
        self.assertNotIn("到家",h["full_text"])
        self.assertEqual(h["subject"],a["ref"])
        self.e.store.mark_recalled([h["id"]],True)
        self.assertEqual(self.e.store.list_memories(self.group["scope"],kind="habit")[0]["valid_until"],h["valid_until"])
        unsafe=self.ingest(self.group,"alice","我每天19:30到家","unsafe",ts=t)
        self.assertEqual(self.commit_habit(self.group,unsafe,category="hobby",activity="19:30到家"),[])
        bad=self.ingest(self.group,"bob","alice 喜欢Dota","bad")
        payload=self.candidate(a)
        payload["habits"][0]["source_ids"]=[bad["seq"]]
        self.assertEqual(normalize_habits(payload,[a,bad],self.e.store.scope_people(self.group["scope"]),self.e.config.snapshot()),[])
        # Explicit correction retires the impression; it is not a permanent factual profile.
        r=self.ingest(self.group,"alice","我不再玩Dota了","stop")
        self.commit_habit(self.group,r,signal="stop")
        self.assertEqual(self.e.store.list_memories(self.group["scope"],kind="habit")[0]["state"],"expired")
        self.e.store.set_habit_consent(self.group["scope"],"alice",False)
        self.assertEqual(self.e.store.list_memories(self.group["scope"],kind="habit"),[])
        with self.e.store.connection() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM habit_observations").fetchone()[0],0)

    async def test_habit_learning_reuses_existing_batch_and_consent(self):
        t=now()
        a=self.ingest(self.group,"alice","晚上玩Dota","a",ts=t-4*86400)
        self.authorize_habit(self.group,"alice",t-5*86400)
        b=self.ingest(self.group,"alice","晚上玩Dota","b",ts=t-3*86400)
        c=self.ingest(self.group,"alice","晚上玩Dota","c",ts=t-2*86400)
        def output(prompt,sys,kwargs):
            self.assertIn("habit_eligible",prompt)
            self.assertIn("额外输出 habits",sys)
            return json.dumps({"memories":[],"affinity":[],"habits":[{"subject":a["ref"],"category":"game","activity":"Dota","period":"evening",
                      "signal":"support","confidence":.95,"source_ids":[a["seq"],b["seq"],c["seq"]]}]})
        self.host.responses.append(output)
        self.assertTrue(await self.e.memory.learn_once(self.e.config.snapshot(),scope=self.group["scope"],force=True))
        self.assertEqual(len(self.host.calls),1)
        self.assertEqual(len(self.e.store.list_memories(self.group["scope"],kind="habit")),1)

    def relay_setup(self):
        self.ingest(self.group,"bob","hi","observe")
        self.update("relay",enabled=True,allowed_users=["alice"],group_aliases={"刀塔群":self.group["umo"]},daily_limit=5)

    async def test_relay_original_confirm_rewrite_and_no_private_context_leak(self):
        self.relay_setup()
        self.ingest(self.private,"alice","不应转达的私聊内容","secret")
        request=event("relay1","肥鱼，帮我去刀塔群说一下，今晚九点开黑",kind="private",location="alice")
        await self.e.on_event(request)
        await self.idle()
        self.assertEqual(self.host.notify_calls,[(self.group["umo"],"今晚九点开黑","")])
        self.assertEqual(len(self.host.calls),0)
        await self.e.on_event(request)
        await self.idle()
        self.assertEqual(len(self.host.notify_calls),1)
        group_rows=self.e.store.context(self.group["scope"],20)
        self.assertNotIn("不应转达",str(group_rows))
        self.assertIn("今晚九点开黑",str(group_rows))
        def rewrite(prompt,sys,kwargs):
            self.assertEqual(prompt,"明天玩吧")
            self.assertNotIn("不应转达",sys)
            return "明天一起玩吧！"
        self.host.responses.append(rewrite)
        await self.e.on_event(event("relay2","去刀塔群组织语言发：明天玩吧",kind="private",location="alice"))
        await self.idle()
        self.assertEqual(len(self.host.notify_calls),1)
        pending=self.e.store.pending_relay(self.private["scope"],"alice")
        self.assertIsNotNone(pending)
        await self.e.on_event(event("bad-confirm",f"确认发送 {pending['id']}",uid="bob",kind="private",location="bob"))
        await self.idle()
        self.assertEqual(len(self.host.notify_calls),1)
        await self.e.on_event(event("confirm",f"确认发送 {pending['id']}",kind="private",location="alice"))
        await self.idle()
        self.assertEqual(self.host.notify_calls[-1],(self.group["umo"],"明天一起玩吧！",""))
        await self.e.on_event(event("confirm-again",f"确认发送 {pending['id']}",kind="private",location="alice"))
        await self.idle()
        self.assertEqual(len(self.host.notify_calls),2)
        self.assertTrue(all("body" not in x for x in self.e.store.relay_history()))

    async def test_literal_relay_keeps_original_ordered_list(self):
        self.relay_setup()
        body = "8. 测试甲\n9. 测试乙\n10. 测试丙"
        await self.e.on_event(event("relay-list", "去刀塔群发：" + body,
                                    kind="private", location="alice"))
        await self.idle()
        self.assertEqual(self.host.notify_calls, [(self.group["umo"], body, "")])
        self.assertEqual(self.host.calls, [])

    async def test_relay_denied_changed_destination_and_unknown_no_retry(self):
        self.relay_setup()
        await self.e.on_event(event("bad1","去刀塔群发：hello",uid="bob",kind="private",location="bob"))
        await self.idle()
        self.assertFalse(self.host.notify_calls)
        self.host.responses.append("明天见！")
        await self.e.on_event(event("draft","去刀塔群组织语言发：明天见",kind="private",location="alice"))
        await self.idle()
        draft=self.e.store.pending_relay(self.private["scope"],"alice")
        self.update("access",allowed_groups=["g2"])
        await self.e.on_event(event("changed",f"确认发送 {draft['id']}",kind="private",location="alice"))
        await self.idle()
        self.assertFalse(self.host.notify_calls)
        self.update("access",allowed_groups=["g1","g2"])
        async def unknown(*args):
            self.host.notify_calls.append(args)
            raise TimeoutError("test uncertain adapter")
        self.host.notify=unknown
        req=event("timeout","去刀塔群发：晚安",kind="private",location="alice")
        await self.e.on_event(req)
        await self.idle()
        self.assertEqual(self.e.store.relay_history()[0]["status"],"unknown")
        await self.e.on_event(req)
        await self.idle()
        self.assertEqual(len(self.host.notify_calls),1)

    async def test_restart_retires_uncertain_relay_and_inflight_research(self):
        self.relay_setup()
        row=self.ingest(self.private,"alice","请求转达","r")
        job,_=self.e.store.create_relay(self.private,row["seq"],"刀塔群",self.group,"测试正文",False,5,now()+300)
        self.assertEqual(job["status"],"sending")
        run=self.e.store.claim_research("fish",1,60)
        self.assertIsNotNone(run)
        restarted=Store(self.e.store.path)
        self.assertEqual(restarted.relay_history()[0]["status"],"unknown")
        self.assertEqual(restarted.research_runs()[0]["state"],"interrupted")
        self.assertIsNone(restarted.claim_research("fish",1,60))
        self.assertEqual(restarted.create_relay(self.private,row["seq"],"刀塔群",self.group,"测试正文",False,5,now()+300)[0],None)
        with restarted.connection() as db:
            self.assertEqual(db.execute("SELECT body FROM relay_jobs WHERE id=?",(job["id"],)).fetchone()[0],"")
        self.assertEqual(self.host.notify_calls,[])

    async def test_stop_between_segments_and_ignore_other_speakers(self):
        self.update("reply",gap_min=.4,gap_max=.4)
        sent=asyncio.Event()
        async def send(ev,part):
            self.host.sent.append((ev,part))
            sent.set()
            return True
        self.host.send=send
        self.host.responses.append("第一段说明。" * 60)
        await self.e.on_event(event("long","肥鱼，详细说说",direct=True))
        await asyncio.wait_for(sent.wait(),2)
        await self.e.on_event(event("unrelated","别说了",uid="bob"))
        self.assertFalse(self.e.states[self.group["scope"]].turn.event.is_set())
        await self.e.on_event(event("stop","别说了",uid="alice"))
        await self.idle()
        self.assertEqual(len(self.host.sent),1)
        self.assertEqual(len(self.host.calls),1)
        rows=self.e.store.context(self.group["scope"],20)
        self.assertEqual(len([r for r in rows if r["bot"]]),1)

    async def test_supplement_during_generation_replans_once(self):
        started,release=asyncio.Event(),asyncio.Event()
        orig=self.host.generate
        async def generate(provider,prompt,system,**kw):
            if not started.is_set():
                started.set()
                await release.wait()
            return await orig(provider,prompt,system,**kw)
        self.host.generate=generate
        self.host.responses.append("旧版本回答，不应发送")
        def newer(prompt,sys,kw):
            data=json.loads(prompt)
            self.assertIn("老版本",data["target_text"])
            self.assertIn("前一轮未发送",data["turn_notice"])
            return "那按老版本讨论吧"
        self.host.responses.append(newer)
        await self.e.on_event(event("old","肥鱼，现在版本怎么样",direct=True))
        await asyncio.wait_for(started.wait(),2)
        await self.e.on_event(event("new","等等，我说的是老版本"))
        release.set()
        await self.idle()
        self.assertEqual([t for _,t in self.host.sent],["那按老版本讨论吧"])
        self.assertEqual(len(self.host.calls),2)

    def research_setup(self,source="dota_news"):
        self.ingest(self.group,"alice","今天聊刀塔，私有主题 SecretGroupMarker","g")
        # This legacy regression deliberately covers the original JSON endpoint.
        self.update("research",enabled=True,sources=[source],daily_topics=1,recall_chars=1800,news_transport="api_first")

    async def test_research_public_news_daily_persistence_and_source_validation(self):
        self.research_setup()
        calls=[]
        async def fetch(url,params,cfg):
            calls.append((url,params))
            return {"appnews":{"appid":570,"newsitems":[
                {"gid":"123456","date":int(now()),"feedname":"steam_community_announcements","title":"测试版本说明","contents":"<p>测试：某项技能改动。</p>"},
                {"gid":"999999","date":int(now()),"feedname":"third_party","title":"不明来源","contents":"不应读取"}]}}
        self.e.research._fetch=fetch
        def select(prompt,sys,kw):
            self.assertNotIn("SecretGroupMarker",prompt)
            self.assertNotIn("alice",prompt)
            return json.dumps({"source":"dota_news","topic":"测试版本","skip":False})
        self.host.responses.extend([select,json.dumps({"facts":[{"text":"测试：某项技能改动。","refs":["s1"]},{"text":"假引用","refs":["missing"]}],
                       "opinion":{"text":"我想等更多比赛再评价。","refs":["s1"]}})])
        status=await self.e.research.run_once(self.e.config.snapshot())
        self.assertEqual(status["state"],"completed")
        self.assertEqual(len(calls),1)
        self.assertEqual(calls[0][0],STEAM_NEWS)
        notes=self.e.store.research_notes("fish")
        self.assertEqual(len(notes),1)
        self.assertEqual(len(notes[0]["facts"]),1)
        self.assertIn("store.steampowered.com/news/app/570/view/123456",notes[0]["sources"][0]["url"])
        self.assertEqual(self.e.store.usage_today(day_key())["modules"]["research"],2)
        again=await self.e.research.run_once(self.e.config.snapshot())
        self.assertIn("限制",again["state"])
        self.assertEqual(len(self.host.calls),2)
        self.assertIsNone(Store(self.e.store.path).claim_research("fish",1,60))
        result=await self.e.research.recall(self.group,self.e.config.snapshot(),"测试版本")
        self.assertTrue(result["notes"])
        self.assertEqual((await self.e.research.recall({**self.group,"persona":"other"},self.e.config.snapshot(),recent=True))["notes"],[])
        with self.e.store.connection() as db:
            db.execute("UPDATE research_notes SET expires=?", (now()-1,))
        self.assertEqual((await self.e.research.recall(self.group,self.e.config.snapshot(),recent=True))["notes"],[])

    async def test_research_pro_samples_request_limit_and_no_player_ids(self):
        self.research_setup("dota_pro")
        ts=int(now())
        match_rows=[dict(match_id=10+i,start_time=ts-i,radiant_win=True,radiant_name="Team A",dire_name="Team B",league_name="Test League") for i in range(3)]
        calls=[]
        async def fetch(url,params,cfg):
            calls.append(url)
            if url==PRO_LIST:
                return match_rows
            return dict(match_id=int(url.split("/")[-1]),radiant_win=True,players=[
                dict(player_slot=i if i<5 else 128+i-5,hero_id=i+1,account_id="PRIVATE_TEST_ID") for i in range(10)])
        self.e.research._fetch=fetch
        data=await self.e.research.evidence("dota_pro",self.e.config.snapshot(),"Team A")
        self.assertEqual(len(calls),3)
        self.assertEqual(len(data),2)
        self.assertIn("天辉出场英雄",data[0]["text"])
        self.assertNotIn("PRIVATE_TEST_ID",str(data))
        self.update("research",data_requests=1)
        calls.clear()
        one=await self.e.research.evidence("dota_pro",self.e.config.snapshot())
        self.assertEqual(len(calls),1)
        self.assertNotIn("出场英雄",one[0]["text"])

    async def test_research_deduplicates_topics_and_evidence_even_after_restart(self):
        self.research_setup()
        self.update("research",daily_topics=3,interval_minutes=15)
        calls=[]
        async def fetch(url,params,cfg):
            calls.append(url)
            return {"appnews":{"appid":570,"newsitems":[
                {"gid":"123","date":int(now()),"feedname":"steam_community_announcements","title":"官方测试","contents":"测试补丁摘要"}]}}
        self.e.research._fetch=fetch
        choice=lambda topic:json.dumps({"source":"dota_news","topic":topic,"skip":False})
        self.host.responses.extend([choice("补丁研究"),json.dumps({"facts":[{"text":"存在测试补丁摘要","refs":["s1"]}]}),
                                    choice("补丁研究"),choice("测试公告更新")])
        self.assertEqual((await self.e.research.run_once(self.e.config.snapshot()))["state"],"completed")
        for i in range(2):
            with self.e.store.connection() as db:
                db.execute("UPDATE research_runs SET started=?",(now()-3600,))
            self.assertEqual((await self.e.research.run_once(self.e.config.snapshot()))["state"],"skipped")
            self.assertEqual(len(calls),1+i)
        self.assertEqual(len(self.host.calls),4)
        self.assertEqual(len(Store(self.e.store.path).research_notes("fish")),1)
        self.assertIn("限制",(await self.e.research.run_once(self.e.config.snapshot()))["state"])

    async def test_research_budget_limit_and_reject_model_supplied_source(self):
        self.research_setup()
        self.host.responses.append(json.dumps({"source":["dota_news"],"topic":"测试","skip":False}))
        async def fetch(*args):
            self.fail("invalid source must never fetch")
        self.e.research._fetch=fetch
        result=await self.e.research.run_once(self.e.config.snapshot())
        self.assertEqual(result["state"],"failed")
        self.assertFalse(self.e.store.research_notes())
        self.assertEqual(self.e.store.research_overview()["today_runs"],1)
        # Model reservation respects the new independent daily cap.
        self.update("research",daily_tokens=1000)
        from astrbot_plugin_fiyu_core.core.telemetry import BudgetExceeded
        with self.assertRaises(BudgetExceeded):
            await self.e.gateway.generate("research",self.group["umo"],"测试"*2000,"test",background=True)
        self.assertEqual(self.e.gateway.running,0)
        self.assertEqual(self.e.gateway.reserved_module_tokens[(day_key(),"research")],0)


if __name__=="__main__":
    unittest.main()
