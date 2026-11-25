"""Offline v0.2.4 source fallback, budgets, health and failure-stage tests."""
from __future__ import annotations

import asyncio
import json
import logging
import unittest
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta
from email.utils import format_datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from xml.sax.saxutils import escape

from astrbot_plugin_fiyu_core.core.config import defaults, validate, ConfigError
from astrbot_plugin_fiyu_core.core.engine import Engine
from astrbot_plugin_fiyu_core.core.research import ResearchService
from astrbot_plugin_fiyu_core.core.research_sources import (
    ENDPOINTS, HEALTH_KEY, PRO_LIST, PRO_MATCH, STEAM_NEWS, STEAM_RSS,
    HTTPFailure, RequestBudget, SourceUnavailable, parse_news_json, parse_news_rss,
)
from astrbot_plugin_fiyu_core.core.util import atomic_json, day_key, dump, now
from test_core import FakeHost, metadata, add_message, public_fixture

LOGGER=logging.getLogger("fiyu.v024.test")
LOGGER.addHandler(logging.NullHandler())


def rss(app=730,gid="1849827110444052",ts=None,body="<p>测试公告</p><ul><li>测试改动</li></ul>",
        channel=None,items=None):
    ts=int(now()) if ts is None else ts
    date=format_datetime(datetime.fromtimestamp(ts,timezone(timedelta(hours=-8))))
    url=f"https://store.steampowered.com/news/app/{app}/view/{gid}"
    if items is None:
        items=f"""<item><title>官方测试更新</title>
        <description>{escape(body)}</description>
        <link><![CDATA[{url}]]></link>
        <guid isPermaLink="true">{url}</guid><pubDate>{date}</pubDate></item>"""
    return (f'<?xml version="1.0" encoding="UTF-8"?>'
            f'<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom"><channel>'
            f'<title>{app} RSS Feed</title><link><![CDATA[{channel or f"https://store.steampowered.com/news/app/{app}/"}]]></link>'
            f'{items}</channel></rss>').encode()


def json_news(app=730,gid="1849827110444052"):
    return {"appnews":{"appid":app,"newsitems":[
        {"gid":gid,"date":int(now()),"feedname":"steam_community_announcements",
         "title":"官方测试更新","contents":"<p>测试公告</p><li>测试改动</li>"}]}}


def select(source="cs2_news",topic="CS2 测试版本",backup=None):
    out={"source":source,"topic":topic,"focus":"","skip":False}
    if backup:
        out["backup"]=backup
    return dump(out)


def summary():
    return dump({"facts":[{"text":"根据提供资料的测试事实","refs":["s1"]}],
                 "opinion":{"text":"","refs":[]}})


class ParserTests(unittest.TestCase):
    def test_rss_xml_html_timestamp_and_canonical_identity(self):
        one=parse_news_rss(rss(),730)
        two=parse_news_json(json_news(),730)
        self.assertEqual(len(one),1)
        self.assertEqual(one[0]["text"],"测试公告 测试改动")
        self.assertEqual(one[0]["key"],two[0]["key"])
        self.assertEqual(one[0]["url"],two[0]["url"])
        self.assertIn("RSS",one[0]["scope"])
        self.assertTrue(one[0]["date"].endswith("+08:00"))
        self.assertEqual(parse_news_rss(rss(app=570),570)[0]["url"],
                         "https://store.steampowered.com/news/app/570/view/1849827110444052")
        self.assertEqual(parse_news_rss(rss(ts=int(now()-91*86400)),730),[])
        self.assertEqual(parse_news_rss(rss(ts=int(now()+3600)),730),[])

    def test_bad_feed_does_not_validate_as_success(self):
        for data in (
            b"<html>unavailable</html>",
            rss(app=570),
            rss(channel="https://example.invalid/news/app/730/"),
            rss(items="<item><title>unknown article</title><link>https://example.invalid</link></item>"),
            b'<!DOCTYPE rss [<!ENTITY test "placeholder">]><rss/>',
            b"\0"*32,
            b"x"*(2*1024*1024+1),
        ):
            with self.subTest(data=data[:100]):
                with self.assertRaises((ValueError,ET.ParseError,UnicodeError)):
                    parse_news_rss(data,730)
        bad=json_news()
        bad["appnews"]["appid"]=570
        with self.assertRaises(ValueError):
            parse_news_json(bad,730)

    def test_old_config_merge_and_validation(self):
        base=defaults()
        for key in ("news_transport","network_timeout","model_timeout_seconds","network_retries",
                    "source_cooldown_minutes","source_failover"):
            base["research"].pop(key)
        base["models"]["timeout_seconds"]=50
        base["dota"]["network_timeout"]=18
        base["research"]["sources"]=["cs2_news","dota_meta"]
        cfg=validate(base)
        self.assertEqual(cfg["research"]["news_transport"],"rss_first")
        self.assertEqual(cfg["research"]["network_timeout"],30)
        self.assertEqual(cfg["research"]["model_timeout_seconds"],90)
        self.assertEqual(cfg["models"]["timeout_seconds"],50)
        self.assertEqual(cfg["dota"]["network_timeout"],18)
        self.assertEqual(cfg["research"]["sources"],["cs2_news","dota_meta"])
        for key,value in (("news_transport","arbitrary"),("network_retries",8),
                          ("source_cooldown_minutes",0),("model_timeout_seconds",1000)):
            bad=defaults()
            bad["research"][key]=value
            with self.assertRaises(ConfigError):
                validate(bad)


class ResearchTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=TemporaryDirectory()
        self.root=Path(self.tmp.name)
        self.host=FakeHost()
        self.e=Engine(self.root,self.host,LOGGER)
        cfg=self.e.config.snapshot()
        cfg["access"].update(mode="active",allowed_groups=["g1"])
        cfg["models"].update(chat_provider="chat",research_provider="study",timeout_seconds=50)
        cfg["research"].update(enabled=True,sources=["cs2_news"],daily_topics=3,
                               interval_minutes=15,network_timeout=30,model_timeout_seconds=77)
        cfg["dota"].update(auto_sync=False,knowledge_auto_sync=False)
        self.e.config.save(cfg,self.e.config.revision())
        self.group=metadata("g1")
        add_message(self.e.store,self.group,"alice","CS2 PrivateChatSample","r1")
        self.requests=[]
        self.model_timeouts=[]
        original=self.e.gateway.generate
        async def generate(*args,**kwargs):
            if args[0]=="research":
                self.model_timeouts.append(kwargs.get("timeout"))
            return await original(*args,**kwargs)
        self.e.gateway.generate=generate

    async def asyncTearDown(self):
        await self.e.stop()
        self.tmp.cleanup()

    def update(self,section,**fields):
        cfg=self.e.config.snapshot()
        cfg[section].update(fields)
        self.e.config.save(cfg,self.e.config.revision())

    def next_run(self):
        with self.e.store.connection() as db:
            db.execute("UPDATE research_runs SET started=?",(now()-3600,))

    def fetch(self,handler):
        async def fake(url,params,cfg):
            self.requests.append((url,params))
            if params:
                self.assertEqual(set(params),{"appid","count","maxlength","feeds","format"})
            self.assertEqual(cfg["research"]["network_timeout"],30)
            return handler(url,params)
        self.e.research._fetch=fake

    async def run_study(self):
        return await self.e.research.run_once(self.e.config.snapshot())

    async def health(self):
        return {x["key"]:x for x in await self.e.research.source_status(self.e.config.snapshot())}

    def use_local_meta(self):
        day=day_key()
        atomic_json(self.e.dota.source_dirs["public"]/(day.replace("-","")+".json"),public_fixture(day))

    async def test_default_rss_no_extra_model_and_relevant_context(self):
        self.fetch(lambda url,p:rss() if url==STEAM_RSS[730] else self.fail("JSON fallback was not needed"))
        def choose(prompt,system,kwargs):
            self.assertNotIn("PrivateChatSample",prompt)
            self.assertNotIn("alice",prompt)
            self.assertEqual(set(json.loads(prompt)["sources"]),{"cs2_news"})
            return select()
        self.host.responses.extend([choose,summary()])
        out=await self.run_study()
        self.assertEqual(out["state"],"completed")
        self.assertEqual(out["requests"],1)
        self.assertEqual(len(self.host.calls),2)
        self.assertEqual(self.model_timeouts,[77,77])
        self.assertEqual(self.e.store.research_overview()["today_runs"],1)
        note=self.e.store.research_notes()[0]
        self.assertIn("RSS",note["sources"][0]["scope"])
        self.assertEqual((await self.health())["steam_rss"]["state"],"ok")
        self.assertEqual((await self.health())["steam_api"]["state"],"untested")

    async def test_transport_switch_persistence_and_same_article_dedup(self):
        def handler(url,params):
            if url==STEAM_RSS[730]:
                raise ConnectionResetError("simulated")
            self.assertEqual(url,STEAM_NEWS)
            return json_news()
        self.fetch(handler)
        self.host.responses.extend([select(),summary()])
        out=await self.run_study()
        self.assertEqual(out["state"],"completed")
        self.assertEqual(out["requests"],2)
        self.assertEqual(len(self.host.calls),2)
        self.assertTrue((await self.health())["steam_rss"]["cooling"])
        self.assertIn("ConnectionResetError",(await self.health())["steam_rss"]["reason"])
        self.assertEqual(self.e.store.get_kv(HEALTH_KEY,{})["steam_rss"]["state"],"failed")
        restarted=ResearchService(self.e.store,self.e.config,self.e.log,self.e.gateway,self.e.dota)
        self.e.research=restarted
        self.requests.clear()
        self.fetch(handler)
        self.next_run()
        self.host.responses.append(select(topic="另一个更新问题"))
        out=await self.run_study()
        self.assertEqual(out["state"],"skipped")
        self.assertEqual([x[0] for x in self.requests],[STEAM_NEWS])
        self.assertEqual(len(self.host.calls),3)
        # After a successful health probe, the same article on RSS retains
        # its canonical evidence key and is still not learned again.
        self.e.research._health["steam_rss"]["retry_after"]=0
        self.fetch(lambda url,p:rss())
        self.next_run()
        self.host.responses.append(select(topic="第三个公告问题"))
        self.assertEqual((await self.run_study())["state"],"skipped")
        self.assertEqual(len(self.e.store.research_notes()),1)

    async def test_same_endpoint_retry_and_no_retry_for_http_403(self):
        self.update("research",news_transport="rss_only")
        def handler(url,p):
            if len(self.requests)==1:
                raise asyncio.TimeoutError()
            return rss()
        self.fetch(handler)
        self.host.responses.extend([select(),summary()])
        out=await self.run_study()
        self.assertEqual(out["state"],"completed")
        self.assertEqual(out["requests"],2)
        self.assertEqual((await self.health())["steam_rss"]["state"],"ok")
        self.next_run()
        self.requests.clear()
        def forbidden(url,p):
            raise HTTPFailure(403)
        self.fetch(forbidden)
        self.host.responses.append(select(topic="新主题"))
        out=await self.run_study()
        self.assertEqual(out["state"],"failed")
        self.assertEqual(out["requests"],1)
        self.assertIn("HTTP 403",out["reason"])
        self.assertIn("资料获取",out["reason"])

    async def test_backup_topic_one_run_and_shared_request_cap(self):
        self.update("research",sources=["cs2_news","dota_pro"],data_requests=3)
        def handler(url,p):
            if url in (STEAM_RSS[730],STEAM_NEWS):
                raise TimeoutError()
            self.assertEqual(url,PRO_LIST)
            return [{"match_id":123,"start_time":int(now()),"radiant_win":True,
                     "radiant_name":"Test A","dire_name":"Test B","league_name":"Test Cup"}]
        self.fetch(handler)
        def after(prompt,system,kw):
            data=json.loads(prompt)
            self.assertEqual(data["topic"],"最近Dota职业样本")
            self.assertIn("OpenDota",data["evidence"][0]["scope"])
            return summary()
        self.host.responses.extend([select(backup={"source":"dota_pro","topic":"最近Dota职业样本"}),after])
        out=await self.run_study()
        self.assertEqual(out["state"],"completed")
        self.assertEqual(out["source"],"dota_pro")
        self.assertEqual(out["requests"],3)
        self.assertEqual(len(self.host.calls),2)
        self.assertEqual(self.e.store.research_overview()["today_runs"],1)
        self.assertEqual(self.e.store.research_notes()[0]["topic"],"最近Dota职业样本")
        self.assertIn("备用来源",out["reason"])

    async def test_backup_reselection_only_when_needed_and_local_after_network_budget(self):
        self.update("research",sources=["cs2_news","dota_meta"],data_requests=1)
        self.use_local_meta()
        def handler(url,p):
            raise ConnectionResetError("simulated")
        self.fetch(handler)
        def reselect(prompt,system,kw):
            data=json.loads(prompt)
            self.assertEqual(set(data["sources"]),{"dota_meta"})
            self.assertEqual(data["previous_source_unavailable"],"cs2_news")
            return select("dota_meta","最近公开比赛胜率")
        self.host.responses.extend([select(),reselect,summary()])
        result=await self.run_study()
        self.assertEqual(result["state"],"completed")
        self.assertEqual(result["requests"],1)
        self.assertEqual(len(self.host.calls),3)
        self.assertEqual(self.e.store.research_notes()[0]["source"],"dota_meta")

    async def test_cooling_shared_news_skips_selection_and_no_new_claim(self):
        self.update("research",sources=["cs2_news","dota_news"],data_requests=1,news_transport="rss_only")
        self.fetch(lambda u,p: (_ for _ in ()).throw(TimeoutError()))
        self.host.responses.append(select())
        out=await self.run_study()
        self.assertEqual(out["state"],"failed")
        self.assertEqual(out["requests"],1)
        self.next_run()
        out=await self.run_study()
        self.assertEqual(out["state"],"暂无可用数据源")
        self.assertEqual(len(self.requests),1)
        self.assertEqual(len(self.host.calls),1)
        self.assertEqual(self.e.store.research_overview()["today_runs"],1)
        self.e.research=ResearchService(self.e.store,self.e.config,self.e.log,self.e.gateway,self.e.dota)
        result=await self.run_study()
        self.assertEqual(result["state"],"暂无可用数据源")

    async def test_probe_no_token_or_topic_and_health_recovery(self):
        self.update("research",sources=["cs2_news","dota_news","dota_pro","dota_meta"])
        self.use_local_meta()
        def handler(url,p):
            if url==STEAM_RSS[730]:
                return rss()
            if url==STEAM_NEWS:
                raise TimeoutError()
            if url==PRO_LIST:
                return [{"match_id":123,"start_time":int(now()),"radiant_win":True}]
            self.fail(url)
        self.fetch(handler)
        out=await self.e.research.probe(self.e.config.snapshot())
        self.assertEqual(out["requests"],3)
        self.assertEqual(len(self.host.calls),0)
        self.assertEqual(self.e.store.research_overview()["today_runs"],0)
        self.assertEqual(self.e.store.usage_today(day_key())["tokens"],0)
        status=await self.health()
        self.assertEqual(status["steam_rss"]["state"],"ok")
        self.assertEqual(status["steam_api"]["state"],"failed")
        self.assertEqual(status["local_meta"]["state"],"ok")
        self.assertEqual(status["steam_api"]["last_source"],"cs2_news")
        self.assertIn("间隔",(await self.e.research.probe(self.e.config.snapshot()))["state"])

    async def test_model_timeouts_identify_stage_and_never_repeat_paid_call(self):
        self.host.responses.append(TimeoutError())
        out=await self.run_study()
        self.assertIn("选题模型",out["reason"])
        self.assertIn("TimeoutError",out["reason"])
        self.assertEqual(out["requests"],0)
        self.assertEqual(len(self.host.calls),1)
        self.next_run()
        self.fetch(lambda u,p:rss())
        self.host.responses.extend([select(),TimeoutError()])
        out=await self.run_study()
        self.assertIn("整理模型",out["reason"])
        self.assertIn("TimeoutError",out["reason"])
        self.assertEqual(out["requests"],1)
        self.assertEqual(len(self.host.calls),3)
        self.assertEqual(self.e.store.research_notes(),[])

    async def test_revoke_source_before_summary_and_invalid_alternative(self):
        def revoke(url,p):
            self.update("research",sources=["dota_pro"])
            return rss()
        self.fetch(revoke)
        self.host.responses.append(select(backup={"source":"dota_pro","topic":"not initially permitted"}))
        out=await self.run_study()
        self.assertEqual(out["state"],"budget")
        self.assertEqual(len(self.host.calls),1)
        self.assertEqual(self.e.store.research_notes(),[])

    async def test_revoke_source_before_transport_retry(self):
        def revoke(url,p):
            self.update("research",sources=["dota_pro"])
            raise TimeoutError()
        self.fetch(revoke)
        self.host.responses.append(select())
        out=await self.run_study()
        self.assertEqual(out["state"],"budget")
        self.assertEqual(out["requests"],1)
        self.assertEqual(len(self.host.calls),1)
        self.assertEqual(self.e.store.research_notes(),[])

    async def test_fixed_endpoint_fetch_timeout_and_no_external_redirect(self):
        cfg=self.e.config.snapshot()
        captured={}
        class Response:
            status=200
            def __init__(self):
                self.content=self
            async def iter_chunked(self,n):
                yield rss()
            async def __aenter__(self):
                return self
            async def __aexit__(self,*args):
                pass
        class Session:
            def __init__(self,**kwargs):
                captured["timeout"]=kwargs["timeout"].total
            async def __aenter__(self):
                return self
            async def __aexit__(self,*args):
                pass
            def get(self,url,**kwargs):
                captured["url"]=url
                captured["redirects"]=kwargs["allow_redirects"]
                return Response()
        with patch("astrbot_plugin_fiyu_core.core.research.aiohttp.ClientSession",Session):
            raw=await self.e.research._fetch(STEAM_RSS[730],None,cfg)
            self.assertIsInstance(raw,bytes)
            self.assertEqual(captured,{"timeout":30,"url":STEAM_RSS[730],"redirects":False})
            with self.assertRaises(ValueError):
                await self.e.research._fetch("https://example.invalid/",None,cfg)


if __name__=="__main__":
    unittest.main()
