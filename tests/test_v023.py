"""v0.2.3 regression tests, modeled on the Valve datafeed response schema."""
from __future__ import annotations

import asyncio
import copy
import json
import logging
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs, urlsplit

from astrbot_plugin_fiyu_core.core.config import ConfigError, defaults, validate
from astrbot_plugin_fiyu_core.core.dota import DotaService
from astrbot_plugin_fiyu_core.core.dota_details import (
    effect_evidence, filter_mechanic, fit_knowledge_payload, hero_result,
    render_numbers, resolve_skill, slot_map, talents_of,
)
from astrbot_plugin_fiyu_core.core.dota_knowledge import (
    latest_patch, parse_patch_notes, parse_constant_notes, validate_snapshot,
)
from astrbot_plugin_fiyu_core.core.engine import Engine
from astrbot_plugin_fiyu_core.core.personality import SILENT, scene_examples
from astrbot_plugin_fiyu_core.core.routing import ToolPlan, domain_rules, focus_text
from astrbot_plugin_fiyu_core.core.util import atomic_json, dump, now
from test_core import FakeHost, event, metadata, fixture


LOGGER=logging.getLogger("fiyu.v023.test")
LOGGER.addHandler(logging.NullHandler())


def ability(code, identity, name, **fields):
    return {
        "id":identity,"name":code,"name_loc":name,"type":0,"max_level":4,
        "behavior":"8","target_team":2,"damage":2,"immunity":4,"dispellable":2,
        "desc_loc":"造成测试魔法伤害。","notes_loc":[],
        "cooldowns":[12,10,8,6],"cast_ranges":[350,400,450,500],
        "mana_costs":[80,90,100,110],"special_values":[],
        **fields,
    }


def am_fixture():
    first=ability("antimage_mana_break",5003,"法力损毁",behavior="2",
        damage=1,desc_loc="燃烧魔法值，附加%percent_damage_per_burn%%%的伤害。",
        notes_loc=["幻象系数为%illusion_percentage%%%。"],cooldowns=[0],
        special_values=[
            {"name":"percent_damage_per_burn","values_float":[65],"heading_loc":"测试伤害系数",
             "bonuses":[{"name":"special_bonus_am_damage","value":10,"operation":0}]},
            {"name":"mana_per_hit_pct","values_float":[1.8,2.7,3.6,4.5],
             "heading_loc":"燃烧最大法力","values_scepter":[1.5]},
            {"name":"illusion_percentage","values_float":[25]},
        ])
    second=ability("antimage_blink",5004,"闪烁",behavior="274878431248",
        damage=0,desc_loc="测试传送。",ability_has_scepter=True,
        scepter_loc="施放后持续%buff_time%秒，数值%unknown_upgrade%。",
        special_values=[{"name":"buff_time","values_float":[0],"values_scepter":[5]},
                        {"name":"AbilityCooldown","values_float":[12,10,8,6],"bonuses":[
                            {"name":"special_bonus_am_cd","value":1,"operation":2}]}])
    third=ability("antimage_counterspell",5005,"法术反制",damage=0,desc_loc="抵挡技能。")
    innate=ability("antimage_persectur",10001,"绝人之路",max_level=1,ability_is_innate=True)
    granted=ability("antimage_extra",10002,"测试魔晶技能",ability_is_granted_by_shard=True)
    ultimate=ability("antimage_mana_void",5006,"法力虚空",type=1,max_level=3,immunity=3)
    talents=[{"id":6000+i,"name":"special_bonus_am_"+str(i),"name_loc":"测试天赋 "+str(i)} for i in range(8)]
    talents[0].update(name="special_bonus_am_damage",name_loc="+{s:bonus_percent_damage_per_burn}% 测试")
    talents[1].update(name="special_bonus_am_cd",name_loc="-{s:bonus_AbilityCooldown}秒 测试")
    talents[2].update(name_loc="+{s:bonus_unknown} 未知")
    return {"id":1,"name":"npc_dota_hero_antimage","name_loc":"敌法师",
            "damage_min":54,"damage_max":58,"str_base":21,"attack_range":150,
            "abilities":[first,second,third,innate,granted,ultimate],
            "talents":talents}


class DetailTests(unittest.TestCase):
    def setUp(self):
        self.cfg=defaults()["dota"]
        self.hero=am_fixture()

    def test_base_skill_order_innate_granted_and_talents(self):
        for query,code in (("一技能","antimage_mana_break"),("Q","antimage_mana_break"),
                           ("烧蓝","antimage_mana_break"),("二号技能","antimage_blink"),
                           ("3","antimage_counterspell"),("大招","antimage_mana_void")):
            self.assertEqual(hero_result(self.hero,self.cfg,query)["ability"]["code"],code)
        self.assertEqual(hero_result(self.hero,self.cfg,"先天")["abilities"][0]["code"],"antimage_persectur")
        self.assertEqual(hero_result(self.hero,self.cfg,"魔晶")["abilities"][0]["code"],"antimage_extra")
        self.assertEqual(hero_result(self.hero,self.cfg)["attributes"]["damage_min"],54)
        talents=hero_result(self.hero,self.cfg,"10级天赋")["talents"]
        self.assertEqual(talents,hero_result(self.hero,self.cfg,"十级天赋")["talents"])
        self.assertEqual(talents[0]["name"],"+10% 测试")
        self.assertEqual(talents[1]["name"],"-1秒 测试")
        self.assertIn("unresolved",talents_of(self.hero)[2])
        self.assertEqual([t["level"] for t in talents_of(self.hero)], [10,10,15,15,20,20,25,25])

    def test_ambiguous_and_special_hero_slots(self):
        complex_hero={**self.hero,"name":"npc_dota_hero_kez"}
        self.assertIn("error",hero_result(complex_hero,self.cfg,"Q"))
        self.cfg["skill_slots"]={"kez":{"Q":"antimage_blink","W":"nonexistent"}}
        self.assertEqual(hero_result(complex_hero,self.cfg,"Q")["ability"]["code"],"antimage_blink")
        self.assertIn("error",hero_result(complex_hero,self.cfg,"W"))
        self.cfg["skill_aliases"]={"antimage_blink":["位移"],"antimage_counterspell":["位移"]}
        self.assertEqual(len(hero_result(self.hero,self.cfg,"位移")["abilities"]),2)
        complex_hero["abilities"]=self.hero["abilities"]+[ability("test_ult",8000,"第二个大招",type=1)]
        self.assertEqual(len(resolve_skill(complex_hero,"R",self.cfg)[0]),2)
        atypical={**self.hero,"abilities":self.hero["abilities"]+[ability("extra_basic",8888,"额外技能")]}
        self.assertNotIn("Q",slot_map(atypical,{"skill_slots":{}}))

    def test_values_descriptions_variants_and_no_fake_units(self):
        result=hero_result(self.hero,self.cfg,"烧蓝")["ability"]
        self.assertIn("65%",result["description"])
        self.assertIn("25%",result["notes"][0])
        bykey={s["key"]:s for s in result["special_values"]}
        self.assertTrue(bykey["mana_per_hit_pct"]["percent"])
        self.assertEqual(bykey["mana_per_hit_pct"]["scepter_values"],[1.5])
        self.assertEqual(bykey["percent_damage_per_burn"]["talent_modifiers"][0]["value"],10)
        upgrade=hero_result(self.hero,self.cfg,"W",view="upgrades")["ability"]
        self.assertIn("5（升级原始字段）",upgrade["神杖"])
        self.assertIn("数值未提供",upgrade["神杖"])
        self.assertEqual(upgrade["special_values"][0]["values"],[0])
        self.assertEqual(upgrade["special_values"][0]["scepter_values"],[5])
        missing=render_numbers(ability("test",111,"测试",desc_loc=""))
        self.assertIn("description_notice",missing)
        self.assertNotIn("description",hero_result(self.hero,self.cfg,"Q",view="numbers")["ability"])

    def test_conditions_distinguish_dispels_and_unknown(self):
        row=ability("shield",800,"测试护盾",desc_loc="保护队友。\n驱散类型：强驱散",dispellable=2)
        self.assertIsNotNone(effect_evidence(row,"strong_dispel"))
        self.assertEqual(filter_mechanic(row,{"dispellability":"basic"})["dispellability"],"basic")
        self.assertIsNone(filter_mechanic(row,{"dispellability":"strong_only"}))
        strong_only=ability("fake",900,"测试限制",desc_loc="只能被强驱散移除。",dispellable=1)
        self.assertIsNone(effect_evidence(strong_only,"strong_dispel"))
        self.assertIsNotNone(filter_mechanic(strong_only,{"dispellability":"strong_only"}))
        self.assertIsNotNone(effect_evidence({**row,"desc_loc":"DISPEL TYPE: Strong Dispel"},"strong_dispel"))
        self.assertIsNone(effect_evidence({**row,"desc_loc":"无法造成沉默。"},"silence"))
        extra={**row,"desc_loc":"测试。","shard_loc":"造成眩晕。"}
        self.assertIsNone(effect_evidence(extra,"stun"))
        self.assertEqual(effect_evidence(extra,"stun",True)["origin"],"shard")
        self.assertIsNotNone(filter_mechanic({**row,"immunity":3},{"pierces_debuff_immunity":True,"cooldown_max":6,"cast_range_min":500}))
        for immunity in (0,1,2,6,99):
            self.assertIsNone(filter_mechanic({**row,"immunity":immunity},{"pierces_debuff_immunity":False}))
        self.assertIsNotNone(filter_mechanic({**row,"immunity":5},{"pierces_debuff_immunity":False}))
        self.assertIsNone(filter_mechanic({**row,"cooldowns":[6,8,10,12]}, {"cooldown_max":6}))
        self.assertIsNone(filter_mechanic({**row,"behavior":"2","cooldowns":[0]}, {"cooldown_max":0}))

    def test_budget_pagination_does_not_skip_or_mutate_numbers(self):
        rows=[{"text":"测试值"*35,"value":55.267321,"picks":13810000000} for _ in range(15)]
        for key in ("matches","changes","abilities","talents"):
            out=fit_knowledge_payload({"patch":"7.42","offset":3,"next_offset":18,"total":30,key:rows},600)
            self.assertLessEqual(len(dump(out)),600)
            self.assertTrue(out["truncated"])
            self.assertEqual(out["next_offset"],3+len(out[key]))
            self.assertEqual(out[key],rows[:len(out[key])])
        result=hero_result(self.hero,self.cfg,section="abilities",limit=2,offset=1,view="effects")
        self.assertEqual(result["next_offset"],3)
        self.assertEqual(result["total"],6)


class KnowledgeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=TemporaryDirectory()
        self.root=Path(self.tmp.name)
        self.config=defaults()
        self.cfg=self.config["dota"]
        self.cfg["knowledge_batch_size"]=60
        self.patch="7.42"
        self.heroes=fixture()["heroes"]
        self.items=[{"id":i,"name":"item_fixture_"+str(i),"name_loc":"物品"+str(i)} for i in range(1,204)]
        self.items[0].update(name="item_blink",name_loc="闪烁匕首",name_english_loc="Blink Dagger")
        self.items[1].update(name="item_recipe_blink",name_loc="图纸",recipes=[{"items":[1]}])
        self.requests=[]
        self.failure=set()
        self.empty={203}
        self.patch_checks=0
        self.flip_at=0
        self.offline=False
        self.delay=patch("astrbot_plugin_fiyu_core.core.dota_knowledge.asyncio.sleep",new=AsyncMock())
        self.delay.start()
        self.svc=self.make_service()

    async def asyncTearDown(self):
        self.delay.stop()
        self.tmp.cleanup()

    def make_service(self):
        svc=DotaService(self.root, None, SimpleLog())
        svc._fetch=self.fetch
        return svc

    def detail(self,kind,row):
        if kind=="hero":
            if row["id"]==1:
                result=copy.deepcopy(am_fixture())
                if self.patch=="7.42a":
                    result["abilities"][0]["special_values"][0]["values_float"]=[70]
                return result
            return {**row,"abilities":[ability("test_"+str(row["id"]),row["id"]+20000,"测试技能")],"talents":[]}
        return {**row,"item_cost":2250 if row["id"]==1 else 50,"desc_loc":"测试物品。","is_item":True}

    async def fetch(self,url,cfg):
        parts=urlsplit(url)
        self.assertEqual(parts.hostname,"www.dota2.com")
        endpoint=parts.path.split("/")[-1]
        params=parse_qs(parts.query)
        self.requests.append((endpoint,params))
        if self.offline:
            raise OSError("offline")
        if endpoint=="patchnoteslist":
            self.patch_checks+=1
            if self.flip_at and self.patch_checks==self.flip_at:
                self.patch="7.42a"
            return {"success":True,"patches":[{"patch_number":"7.41f","patch_timestamp":1},
                      {"patch_number":self.patch,"patch_timestamp":2}]}
        if endpoint=="herolist":
            return {"result":{"status":1,"data":{"heroes":self.heroes}}}
        if endpoint=="itemlist":
            return {"result":{"status":1,"data":{"itemabilities":self.items}}}
        if endpoint=="abilitylist":
            return {"result":{"status":1,"data":{"itemabilities":[
                {"id":5003,"name":"antimage_mana_break","name_loc":"Fixture Burn"},
                {"id":5004,"name":"not_the_same_code","name_loc":"Wrong Skill"},
            ]}}}
        if endpoint in ("herodata","itemdata"):
            kind="hero" if endpoint=="herodata" else "item"
            identity=int(params[kind+"_id"][0])
            if (kind,identity) in self.failure:
                raise TimeoutError("simulated failure")
            key="heroes" if kind=="hero" else "items"
            row=next(x for x in (self.heroes if kind=="hero" else self.items) if x["id"]==identity)
            return {"result":{"status":1,"data":{key:[] if kind=="item" and identity in self.empty else [self.detail(kind,row)]}}}
        if endpoint=="patchnotes":
            if params["version"] != ["7.42"]:
                raise OSError("unsupported fixture")
            return {"success":True,"patch_number":"7.42","patch_timestamp":1794988800,
                    "general_notes":[{"title":"测试通用","generic":[{"note":"测试规则修改"}]}],
                    "items":[{"ability_id":1,"ability_notes":["测试价格修改"]}],
                    "neutral_items":[],
                    "heroes":[{"hero_id":1,"hero_notes":["测试基础攻击力修改"],
                               "abilities":[{"ability_id":5003,"ability_notes":["测试魔法值伤害系数修改"]}],
                               "talent_notes":["测试天赋修改"]}]}
        raise AssertionError(endpoint)

    async def finish(self):
        for _ in range(30):
            result=await self.svc.knowledge.sync_step(self.cfg,force=True)
            if result["state"]=="已同步":
                return result
        self.fail("static sync did not finish")

    async def test_complete_publication_resume_and_offline_history(self):
        await self.svc.knowledge.sync_step(self.cfg,force=True)
        self.assertIsNone(await self.svc.knowledge.get("hero","7.42"))
        stage=self.svc.knowledge._read("hero","7.42",True)
        self.assertEqual(len(stage["details"]),60)
        # Restart before the first hero snapshot is complete.
        self.svc=self.make_service()
        status=await self.finish()
        self.assertEqual(status["hero"]["available"],len(self.heroes))
        self.assertTrue(status["hero"]["published"])
        self.assertEqual(status["item"]["available"],202)
        self.assertEqual(status["item"]["metadata_only"],1)
        history=[int(x["hero_id"][0]) for endpoint,x in self.requests if endpoint=="herodata"]
        self.assertEqual(len(history),len(set(history)))
        before=len(self.requests)
        hero=await self.svc.hero(self.cfg,"敌法","大招")
        self.assertEqual(hero["ability"]["code"],"antimage_mana_void")
        self.assertFalse(hero["stale"])
        translated=await self.svc.hero(self.cfg,"敌法","Fixture Burn")
        self.assertEqual(translated["ability"]["english"],"Fixture Burn")
        self.assertIn("error",await self.svc.hero(self.cfg,"敌法","Wrong Skill"))
        item=await self.svc.item(self.cfg,"跳刀")
        self.assertEqual(item["cost_gold"],2250)
        self.assertEqual(item["recipe_cost_gold"],50)
        missing=await self.svc.item(self.cfg,"203")
        self.assertFalse(missing["detail_available"])
        self.assertIsNone(missing["cost_gold"])
        self.assertEqual(len(self.requests),before)
        self.assertIn("error",await self.svc.hero(self.cfg,"敌法",patch="7.41f"))
        self.patch="7.42a"
        await self.finish()
        old=await self.svc.hero(self.cfg,"敌法","Q",patch="7.42")
        new=await self.svc.hero(self.cfg,"敌法","Q")
        self.assertTrue(old["stale"])
        self.assertEqual(old["ability"]["special_values"][0]["values"],[65])
        self.assertEqual(new["ability"]["special_values"][0]["values"],[70])
        self.offline=True
        self.svc=self.make_service()
        fallback=await self.svc.hero(self.cfg,"敌法","Q")
        self.assertEqual(fallback["patch"],"7.42a")
        self.assertTrue(fallback["stale"])

    async def test_hero_error_does_not_publish_incomplete_or_block_items(self):
        self.failure={("hero",1)}
        for _ in range(15):
            await self.svc.knowledge.sync_step(self.cfg,force=True)
        self.assertIsNone(await self.svc.knowledge.get("hero","7.42"))
        self.assertIsNotNone(await self.svc.knowledge.get("item","7.42"))
        result=await self.svc.mechanics(self.cfg,damage_type="magical")
        self.assertIn("error",result)
        self.failure.clear()
        await self.finish()
        self.assertTrue((await self.svc.knowledge.status())["hero"]["published"])

    async def test_patch_switch_discards_new_batch(self):
        await self.svc.knowledge.sync_step(self.cfg,force=True)
        old=self.svc.knowledge._read("hero","7.42",True)
        # During the next batch the patch changes after fetches have started.
        self.flip_at=self.patch_checks+2
        result=await self.svc.knowledge.sync_step(self.cfg,force=True)
        self.assertEqual(result["state"],"补丁变化，本批未发布")
        self.assertEqual(self.svc.knowledge._read("hero","7.42",True),old)
        self.assertIsNone(await self.svc.knowledge.get("hero","7.42"))
        await self.finish()
        self.assertIsNotNone(await self.svc.knowledge.get("hero","7.42a"))
        self.assertIsNone(await self.svc.knowledge.get("hero","7.42"))

    async def test_exact_current_detail_available_before_full_sync(self):
        result=await self.svc.hero(self.cfg,"敌法","Q",patch="7.42")
        self.assertEqual(result["catalog_patch"],"7.42")
        self.assertEqual(result["ability"]["code"],"antimage_mana_break")
        self.assertFalse(result["stale"])
        self.assertIsNone(await self.svc.knowledge.get("hero","7.42"))
        self.assertIn("error",await self.svc.hero(self.cfg,"敌法","Q",patch="7.41f"))
        self.failure={("hero",2)}
        self.assertIn("error",await self.svc.hero(self.cfg,"斧王",patch="7.42"))

    async def test_previous_version_detail_cache_still_readable_offline(self):
        atomic_json(self.svc.catalog_path,{"date":"2026-11-23","patch":"7.42",
                    "heroes":self.heroes,"items":self.items})
        atomic_json(self.svc.detail_dir/"hero-1-7.42.json",{"fetched":now()-60,"data":am_fixture()})
        self.offline=True
        old=await self.svc.hero(self.cfg,"敌法","Q")
        self.assertTrue(old["stale"])
        self.assertEqual(old["catalog_patch"],"unknown")
        self.assertEqual(old["ability"]["code"],"antimage_mana_break")
        self.assertIn("error",await self.svc.hero(self.cfg,"敌法","Q",patch="7.42"))

    async def test_mechanics_full_fields_and_pagination(self):
        await self.finish()
        ult=await self.svc.mechanics(self.cfg,ability_type="ultimate",pierces_debuff_immunity=True)
        self.assertEqual(ult["matched"],1)
        self.assertEqual(ult["matches"][0]["code"],"antimage_mana_void")
        page=await self.svc.mechanics(self.cfg,damage_type="magical",limit=2,offset=1)
        self.assertEqual(page["next_offset"],3)
        self.assertEqual(page["coverage"]["heroes"],len(self.heroes))
        both=await self.svc.mechanics(self.cfg,kind="any",damage_type="magical",hero="")
        self.assertEqual(both["quality"],"structured_fields")
        self.assertEqual(both["coverage"]["item_metadata_only"],1)
        candidate=await self.svc.mechanics(self.cfg,effect="teleport",hero="敌法")
        self.assertEqual(candidate["quality"],"description_candidates")
        self.assertEqual(candidate["matches"][0]["evidence"]["origin"],"base")
        self.assertIn("error",await self.svc.mechanics(self.cfg))
        self.assertIn("error",await self.svc.mechanics(self.cfg,damage_type="pure",patch="7.41f"))

    async def test_patch_notes_and_exact_diff(self):
        await self.finish()
        result=await self.svc.patch_changes(self.cfg,hero="敌法",patch="7.42")
        self.assertEqual(result["total"],3)
        self.assertEqual(result["changes"][1]["ability"],"法力损毁")
        self.assertIn("datafeed/patchnotes",result["source"])
        general=await self.svc.patch_changes(self.cfg,patch="7.42")
        self.assertEqual(general["affected_heroes"],1)
        self.assertEqual((await self.svc.patch_changes(self.cfg,patch="7.42",item="跳刀"))["total"],1)
        self.assertEqual((await self.svc.patch_changes(self.cfg,patch="7.42",hero="斧王"))["total"],0)
        self.assertIn("error",await self.svc.patch_changes(self.cfg,hero="敌法",patch="7.42",from_patch="7.41f"))
        self.patch="7.42a"
        await self.finish()
        diff=await self.svc.patch_changes(self.cfg,hero="敌法",patch="7.42a",from_patch="7.42")
        row=next(r for r in diff["changes"] if r["field"]=="special_values.percent_damage_per_burn")
        self.assertEqual(row["before"]["values_float"],[65])
        self.assertEqual(row["after"]["values_float"],[70])
        local=self.root/"patch.json"
        atomic_json(local,{"7_41f":{"heroes":{"antimage":{"antimage_blink":["测试历史公告"]}},"items":{},"general":[]}})
        self.cfg["patchnotes_json"]=str(local)
        local_result=await self.svc.patch_changes(self.cfg,hero="敌法",patch="7.41f")
        self.assertIn("未在线核实",local_result["source"])
        self.assertEqual(local_result["changes"][0]["text"],"测试历史公告")
        self.assertIn("error",await self.svc.patch_changes(self.cfg,patch="7.41e"))

    def test_invalid_patch_notes_and_roster_are_not_empty_success(self):
        for bad in ({}, {"success":False,"patch_number":"7.42","heroes":[]},
                    {"success":True,"patch_number":"7.42a","heroes":[]},
                    {"success":True,"patch_number":"7.42","heroes":[{"hero_id":1,"abilities":[{"ability_notes":["x"]}]}]}):
            with self.assertRaises((ValueError,KeyError)):
                parse_patch_notes(bad,"7.42")
        with self.assertRaises(ValueError):
            latest_patch({"success":0,"patches":[{"patch_number":"7.42","patch_timestamp":2}]})
        with self.assertRaises(ValueError):
            parse_constant_notes({"7_41":{"heroes":{}}},"7.42")
        with self.assertRaises(ValueError):
            validate_snapshot({"schema":1,"kind":"hero","patch":"7.42","roster":self.heroes,
                               "details":{},"unavailable":[],"complete":True},"hero","7.42")


class SimpleLog:
    def emit(self,*args,**kwargs):
        pass


class RoutingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=TemporaryDirectory()
        self.host=FakeHost()
        self.e=Engine(Path(self.tmp.name),self.host,LOGGER)
        cfg=self.e.config.snapshot()
        cfg["access"].update(mode="active",allowed_groups=["g1","g2"],private_enabled=True,private_users=["alice"])
        cfg["decision"].update(merge_wait_seconds=0.03,merge_max_wait_seconds=0.1,ordinary_check_probability=1.0,unsolicited_gap_seconds=0)
        cfg["reply"].update(gap_min=0,gap_max=0)
        cfg["dota"].update(auto_sync=False,knowledge_auto_sync=False)
        cfg["models"].update(chat_provider="chat",decision_provider="decision")
        cfg["tools"]["kb_bindings"]=[{"alias":"sap","name":"SAP Docs","description":"SAP技术文档","allowed_groups":["g1"],"allow_private":False}]
        self.e.config.save(cfg,self.e.config.revision())
        self.e.running=True

    async def asyncTearDown(self):
        await self.e.stop()
        self.tmp.cleanup()

    def update(self,section,**fields):
        cfg=self.e.config.snapshot()
        cfg[section].update(fields)
        self.e.config.save(cfg,self.e.config.revision())

    async def idle(self):
        for _ in range(300):
            if all(not s.pending and (not s.worker or s.worker.done()) for s in self.e.states.values()):
                return
            await asyncio.sleep(0.01)
        self.fail("worker did not drain")

    def plan(self,text,scene="casual",focus=None,cfg=None,group="g1",explicit=False):
        cfg=cfg or self.e.config.snapshot()
        return ToolPlan(self.e.tools.specs(metadata(group),cfg,explicit),cfg,focus or text,text,scene,True,self.e.dota.aliases)

    def test_routing_context_casual_authorized_and_low_budget(self):
        def names(plan):
            return {s["name"] for s in plan.ready()}
        casual=self.plan("早安啊")
        self.assertEqual(names(casual),{"open_tools"})
        self.assertEqual(names(self.plan("吃饭了吗？","question")),{"open_tools"})
        rows=[{"text":"紫猫残阴伤害很高","seq":1,"ts":now()-2},{"text":"真的假的，帮我看看","seq":2,"ts":now()}]
        focus=focus_text(rows[-1]["text"],rows,rows[-1],[1,2])
        found=self.plan(rows[-1]["text"],"question",focus)
        self.assertIn("query_dota_hero",names(found))
        self.assertNotIn("rank_hero_stats",names(found))
        stats=self.plan("现在胜率最高的是谁","game")
        self.assertIn("rank_hero_stats",names(stats))
        self.assertNotIn("query_dota_hero",names(stats))
        self.assertIn("get_dota_matchups",names(self.plan("敌法和谁配合最好","question")))
        self.assertIn("50%",domain_rules(stats.selected))
        self.assertNotIn("50%",domain_rules(casual.selected))
        self.assertIn("search_knowledge",names(self.plan("SAP怎么配置","technical")))
        g2=self.plan("SAP怎么配置","technical",group="g2")
        self.assertNotIn("search_knowledge",names(g2))
        self.assertNotIn("knowledge",g2.available)
        self.assertIn("remember_memory",names(self.plan("请记住我的生日",explicit=True)))
        cfg=self.e.config.snapshot()
        cfg["reply"]["max_tool_rounds"]=1
        self.assertIn("query_dota_hero",names(self.plan("查一下","question",cfg=cfg)))
        self.assertNotIn("open_tools",names(self.plan("查一下","question",cfg=cfg)))
        cfg["tools"]["contextual_tools"]=False
        self.assertIn("rank_hero_stats",names(self.plan("早安",cfg=cfg)))

    async def test_expand_only_on_demand_and_no_hidden_execution(self):
        async def item(cfg,name,**kwargs):
            self.assertEqual(name,"跳刀")
            return {"code":"item_blink","cost_gold":2250}
        self.e.dota.item=item
        def first(prompt,system,kw):
            self.assertEqual({t["name"] for t in kw["tools"]},{"open_tools"})
            self.assertNotIn("完整5v5",system)
            return {"tools_call_name":["open_tools"],"tools_call_args":[{"groups":["dota_info"]}],"tools_call_ids":["expand-1"]}
        def second(prompt,system,kw):
            self.assertIn("query_dota_item",{t["name"] for t in kw["tools"]})
            self.assertIn("Dota技能",system)
            return {"tools_call_name":["query_dota_item"],"tools_call_args":[{"item":"跳刀"}],"tools_call_ids":["item-1"]}
        def third(prompt,system,kw):
            self.assertIn("2250",str(kw["tool_results"]))
            return "测试：2250"
        self.host.responses.extend([first,second,third])
        await self.e.on_event(event("expand","早安",direct=True))
        await self.idle()
        self.assertEqual(len(self.host.calls),3)
        self.assertEqual(self.host.sent[-1][1],"测试：2250")
        # A model cannot execute an initially hidden group in the expansion turn.
        self.host.responses.extend([
            {"tools_call_name":["query_dota_item"],"tools_call_args":[{"item":"跳刀"}],"tools_call_ids":["hidden-1"]},
            lambda p,s,k: "拒绝" if "未授权" in str(k["tool_results"]) else self.fail("hidden tool executed"),
        ])
        await self.e.on_event(event("hidden","你好啊",uid="bob",location="g2",direct=True))
        await self.idle()
        self.assertEqual(self.host.sent[-1][1],"拒绝")
        self.assertEqual(self.host.kb_calls,[])

    async def test_access_revoked_before_expansion(self):
        def first(prompt,system,kw):
            self.update("dota",enabled=False)
            return {"tools_call_name":["open_tools"],"tools_call_args":[{"groups":["dota_info"]}],"tools_call_ids":["expand-revoked"]}
        def last(prompt,system,kw):
            self.assertNotIn("query_dota_item",{t["name"] for t in kw["tools"]})
            self.assertNotIn("Dota技能",system)
            result=json.loads(kw["tool_results"][0]["results"][0][1])
            self.assertEqual(result["expanded"],[])
            return "好"
        self.host.responses.extend([first,last])
        await self.e.on_event(event("revoke","早安",direct=True))
        await self.idle()
        self.assertEqual(len(self.host.calls),2)

    async def test_ordinary_silence_not_explicit_and_counterexamples(self):
        def decision(prompt,system,kw):
            return {"completion_text":dump({"reply":True,"confidence":0.99,"target":json.loads(prompt)["new_ids"][-1],"reason":"闲聊","scene":"casual"})}
        def chat(prompt,system,kw):
            self.assertIn(SILENT,system)
            counterexamples=json.loads(prompt)["style_counterexamples"]
            self.assertIn("avoid",counterexamples[0])
            self.assertNotIn(counterexamples[0]["avoid"],str(kw["contexts"]))
            self.assertIn("真实群友",system)
            return SILENT
        self.host.responses.extend([decision,chat])
        await self.e.on_event(event("quiet","我先睡了"))
        await self.idle()
        self.assertEqual(self.host.sent,[])
        self.assertEqual(len(self.host.calls),2)
        self.host.responses.append(SILENT)
        await self.e.on_event(event("direct-quiet","肥鱼，早安",direct=True))
        await self.idle()
        self.assertEqual(len(self.host.sent),1)
        self.assertNotIn(SILENT,self.host.sent[-1][1])
        self.assertIn("回答",self.host.sent[-1][1])

    async def test_learned_style_must_be_relevant(self):
        class StyleStore:
            async def call(self,*args):
                return [
                    {"kind":"expression","pattern":"超高频表达","meaning":"无关口头禅","example":"重复","count":999,"confidence":0.99},
                    {"kind":"slang","pattern":"紫猫","meaning":"虚无之灵","example":"紫猫","count":2,"confidence":0.9},
                ]
        old=self.e.memory.store
        self.e.memory.store=StyleStore()
        try:
            cfg=self.e.config.snapshot()
            self.assertEqual(await self.e.memory.style_context("x",cfg,"吃晚饭了"),"")
            s=await self.e.memory.style_context("x",cfg,"紫猫","",scene="game")
            self.assertIn("虚无之灵",s)
            self.assertNotIn("超高频表达",s)
        finally:
            self.e.memory.store=old

    async def test_manual_knowledge_action_does_not_call_models(self):
        self.e.dota.knowledge.build=AsyncMock(return_value={"state":"已同步"})
        self.assertTrue(self.e.start_action("dota_knowledge"))
        for _ in range(30):
            if self.e.actions["dota_knowledge"]["state"]=="done":
                break
            await asyncio.sleep(0.01)
        self.e.dota.knowledge.build.assert_awaited_once()
        self.assertEqual(self.e.actions["dota_knowledge"]["result"]["state"],"已同步")
        self.assertEqual(self.host.calls,[])

    def test_old_config_validation_and_shared_example_budget(self):
        cfg=defaults()
        for key in ("knowledge_auto_sync","knowledge_batch_size","skill_aliases","skill_slots","patchnotes_json"):
            cfg["dota"].pop(key)
        for key in ("style_prompt","negative_examples_count","negative_examples","casual_silence"):
            cfg["social"].pop(key)
        cfg["tools"].pop("contextual_tools")
        fixed=validate(cfg)
        self.assertTrue(fixed["tools"]["contextual_tools"])
        self.assertTrue(fixed["dota"]["knowledge_auto_sync"])
        for sec,key,value in (("dota","skill_aliases",{"test":123}),
                              ("dota","skill_slots",{"kez":{"S":"x"}}),
                              ("social","negative_examples",[{"scene":"invalid"}])):
            bad=copy.deepcopy(fixed)
            bad[sec][key]=value
            with self.assertRaises(ConfigError):
                validate(bad)
        s=fixed["social"]
        s["examples"]=[{"scene":"casual","user":"晚安","assistant":"嗯嗯"}]
        positive,negative=scene_examples(s,"casual","我要睡了晚安",[],[])
        self.assertLessEqual(sum(len(x["content"])+12 for x in positive)
                             +sum(sum(len(v) for v in x.values())+60 for x in negative),s["examples_chars"])
        self.assertTrue(negative)
