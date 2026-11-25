from __future__ import annotations

import asyncio
import logging
import sqlite3
from pathlib import Path

from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools
from astrbot.api.web import json_response, request

from .core.config import ConfigError, defaults
from .core.engine import Engine
from .core.memory import manual_proposal
from .core.util import day_key
from .host import AstrHost

PLUGIN_NAME = "astrbot_plugin_fiyu_core"


def fail(message, status=400):
    return json_response({"ok": False, "error": str(message)}, status_code=status)


class FiyuCore(Star):
    """Standalone chat engine with an authenticated AstrBot Plugin View."""

    def __init__(self, context: Context):
        super().__init__(context)
        self.engine: Engine | None = None
        self._initializing = asyncio.Lock()

    async def initialize(self):
        async with self._initializing:
            if self.engine and self.engine.running:
                return
            root = StarTools.get_data_dir(PLUGIN_NAME)
            self.engine = await asyncio.to_thread(
                Engine,
                Path(root),
                AstrHost(self.context),
                getattr(self, "logger", logging.getLogger("astrbot")),
            )
            await self.engine.start()
            self._register_routes()

    async def terminate(self):
        if self.engine:
            await self.engine.stop()

    @filter.platform_adapter_type(
        filter.PlatformAdapterType.QQOFFICIAL
        | filter.PlatformAdapterType.QQOFFICIAL_WEBHOOK
        | filter.PlatformAdapterType.AIOCQHTTP
    )
    @filter.event_message_type(filter.EventMessageType.ALL, priority=90)
    async def receive(self, event: AstrMessageEvent):
        if self.engine and self.engine.running:
            try:
                await self.engine.on_event(event)
            except Exception as e:
                self.engine.log.emit(
                    "CHAT", "ingest_failed", "ERROR", exception=type(e).__name__
                )

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("fiyu_id", priority=100)
    async def show_id(self, event: AstrMessageEvent):
        """查看配置所需的真实平台 ID、UMO 和当前发送者 ID。"""
        yield event.plain_result(
            f"平台：{event.get_platform_name()} / {event.get_platform_id()}\n群 OpenID：{event.get_group_id() or '私聊'}\n发送者 ID：{event.get_sender_id()}\nUMO：{event.unified_msg_origin}"
        )
        event.stop_event()

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("fiyu_status", priority=100)
    async def show_status(self, event: AstrMessageEvent):
        """查看聊天引擎状态。"""
        if not self.engine:
            yield event.plain_result("Fiyu Core 未就绪。")
        else:
            s = await self.engine.overview()
            yield event.plain_result(
                f"Fiyu Core v{s['version']} · {s['mode']}\n今日消息：{s['totals']['today'].get('received', 0)}，回复：{s['totals']['today'].get('replied', 0)}\nToken：{s['usage']['tokens']}，等待：{s['queue']}\n请从 AstrBot 插件详情 → 可视化视图 → dashboard 配置。"
            )
        event.stop_event()

    @filter.command("fiyu_habit_on", priority=100)
    async def enable_own_habits(self,event: AstrMessageEvent):
        """同意在当前群归纳自己公开分享的非敏感习惯。"""
        message=await self.engine.habit_consent(event,True) if self.ready() else "Fiyu Core 未就绪。"
        yield event.plain_result(message)
        event.stop_event()

    @filter.command("fiyu_habit_off", priority=100)
    async def disable_own_habits(self,event: AstrMessageEvent):
        """撤回本人的习惯归纳授权并清除相关派生记录。"""
        message=await self.engine.habit_consent(event,False) if self.ready() else "Fiyu Core 未就绪。"
        yield event.plain_result(message)
        event.stop_event()

    def _register_routes(self):
        endpoints = {
            "overview": (self.api_overview, "GET"),
            "config": (self.api_config, "GET"),
            "config/defaults": (self.api_defaults, "GET"),
            "config/save": (self.api_save_config, "POST"),
            "config/restore": (self.api_restore_config, "POST"),
            "sessions": (self.api_sessions, "GET"),
            "users": (self.api_users, "GET"),
            "users/save": (self.api_save_user, "POST"),
            "users/identities": (self.api_identities, "GET"),
            "users/identity/save": (self.api_save_identity, "POST"),
            "users/legacy_preview": (self.api_legacy_preview, "POST"),
            "users/legacy_apply": (self.api_legacy_apply, "POST"),
            "memories": (self.api_memories, "GET"),
            "memories/versions": (self.api_versions, "GET"),
            "memories/save": (self.api_save_memory, "POST"),
            "memories/action": (self.api_memory_action, "POST"),
            "styles": (self.api_styles, "GET"),
            "styles/action": (self.api_style_action, "POST"),
            "research": (self.api_research, "GET"),
            "research/action": (self.api_research_action, "POST"),
            "relay": (self.api_relay, "GET"),
            "usage": (self.api_usage, "GET"),
            "logs": (self.api_logs, "GET"),
            "action": (self.api_action, "POST"),
        }
        for path, (fn, method) in endpoints.items():
            self.context.register_web_api(
                f"/{PLUGIN_NAME}/{path}", fn, [method], f"Fiyu Core {path}"
            )

    @staticmethod
    async def body():
        obj = await request.json(default=None)
        if not isinstance(obj, dict):
            raise ValueError("需要 JSON 对象")
        return obj

    @staticmethod
    def q(key, default="", limit=240):
        return str(request.query.get(key, default) or "")[:limit]

    @staticmethod
    def qint(key, default, low, high):
        try:
            return max(low, min(high, int(request.query.get(key, default))))
        except (TypeError, ValueError):
            return default

    def ready(self):
        # Framework bridge routes already require authenticated Dashboard access.
        return self.engine and self.engine.running

    async def api_overview(self):
        if not self.ready():
            return fail("插件未就绪", 503)
        return json_response(await self.engine.overview())

    async def api_config(self):
        if not self.ready():
            return fail("插件未就绪", 503)
        result = await asyncio.to_thread(self.engine.config.info)
        try:
            result["options"] = await self.engine.host.options()
        except Exception:
            result["options"] = {}
        return json_response(result)

    async def api_defaults(self):
        return json_response({"config": defaults()})

    async def api_save_config(self):
        if not self.ready():
            return fail("插件未就绪", 503)
        try:
            obj = await self.body()
            result = await asyncio.to_thread(
                self.engine.config.save, obj.get("config"), str(obj.get("revision", ""))
            )
            self.engine.log.emit("CONFIG", "saved")
            return json_response({"ok": True, **result})
        except (ConfigError, ValueError) as e:
            return fail(e)
        except OSError:
            return fail("配置写入失败，请检查磁盘和权限", 500)

    async def api_restore_config(self):
        if not self.ready():
            return fail("插件未就绪", 503)
        try:
            obj = await self.body()
            result = await asyncio.to_thread(
                self.engine.config.restore,
                str(obj.get("backup", "")),
                str(obj.get("revision", "")),
            )
            self.engine.log.emit("CONFIG", "restored")
            return json_response({"ok": True, **result})
        except (ValueError, OSError) as e:
            return fail("备份不可用" if isinstance(e, OSError) else e)

    async def api_sessions(self):
        if not self.ready():
            return fail("插件未就绪", 503)
        return json_response({"items": await self.engine.store.call("sessions")})

    async def api_users(self):
        if not self.ready():
            return fail("插件未就绪", 503)
        items = await self.engine.store.call(
            "list_people",
            self.q("scope"),
            self.q("q"),
            self.qint("offset", 0, 0, 1000000),
            self.qint("limit", 80, 1, 200),
        )
        return json_response({"items": items})

    async def api_identities(self):
        if not self.ready():
            return fail("插件未就绪",503)
        items=await self.engine.store.call("list_identities",self.q("scope"),self.q("q"),
            self.qint("offset",0,0,1000000),self.qint("limit",80,1,200))
        return json_response({"items":items})

    async def api_save_identity(self):
        if not self.ready():
            return fail("插件未就绪",503)
        try:
            obj=await self.body()
            await self.engine.store.call("edit_identity",str(obj.get("identity_id","")),obj)
            self.engine.log.emit("USER","identity_edited")
            return json_response({"ok":True})
        except ValueError as e:
            return fail(e)

    async def api_save_user(self):
        if not self.ready():
            return fail("插件未就绪", 503)
        try:
            obj = await self.body()
            await self.engine.store.call(
                "edit_person", str(obj.get("scope", "")), str(obj.get("uid", "")), obj
            )
            self.engine.log.emit("USER", "edited")
            return json_response({"ok": True})
        except ValueError as e:
            return fail(e)

    async def api_memories(self):
        if not self.ready():
            return fail("插件未就绪", 503)
        items = await self.engine.store.call(
            "list_memories",
            self.q("scope"),
            self.q("q"),
            self.q("kind"),
            self.q("state"),
            self.qint("offset", 0, 0, 1000000),
            self.qint("limit", 60, 1, 150),
        )
        return json_response({"items": items})

    async def api_legacy_preview(self):
        if not self.ready():
            return fail("插件未就绪",503)
        try:
            p=await self.body()
            result=await self.engine.store.call("legacy_preview",str(p.get("scope","")),str(p.get("ref","")),str(p.get("old","")),str(p.get("name","")))
            return json_response({"ok":True,**result})
        except ValueError as e:
            return fail(e)

    async def api_legacy_apply(self):
        if not self.ready():
            return fail("插件未就绪",503)
        try:
            p=await self.body()
            if p.get("confirm") is not True or type(p.get("rename_display",False)) is not bool:
                raise ValueError("需要确认预览结果")
            result=await self.engine.store.call("legacy_apply",str(p.get("scope","")),str(p.get("ref","")),str(p.get("old","")),str(p.get("name","")),str(p.get("revision","")),p.get("selected"),p.get("rename_display",False))
            self.engine.log.emit("USER","legacy_titles_migrated",count=result["changed"])
            return json_response({"ok":True,**result})
        except ValueError as e:
            return fail(e)

    async def api_research(self):
        if not self.ready():
            return fail("插件未就绪",503)
        config=self.engine.config.snapshot()
        rows=await self.engine.store.call("research_notes",self.q("persona"),False,self.qint("limit",60,1,100),self.qint("offset",0,0,1000000),self.q("q"))
        return json_response({"items":rows,"runs":await self.engine.store.call("research_runs",40),
            "overview":await self.engine.store.call("research_overview"),"settings":config["research"],
            "tokens":(await self.engine.store.call("usage_today",day_key()))["module_tokens"].get("research",0),
            "source_status":await self.engine.research.source_status(config),
            "actions":{k:v for k,v in self.engine.actions.items() if k in ("research_once","research_probe")}})

    async def api_research_action(self):
        if not self.ready():
            return fail("插件未就绪",503)
        try:
            p=await self.body()
            nid,action=p.get("id"),p.get("action")
            if type(nid) is not int or nid<=0 or action not in ("archive","restore","delete"):
                raise ValueError("无效笔记操作")
            if action=="delete" and p.get("confirm") is not True:
                raise ValueError("删除需要明确确认")
            ok=await self.engine.store.call("research_note_action",nid,action)
            return json_response({"ok":ok})
        except ValueError as e:
            return fail(e)

    async def api_relay(self):
        if not self.ready():
            return fail("插件未就绪",503)
        return json_response({"items":await self.engine.store.call("relay_history",100)})

    async def api_versions(self):
        if not self.ready():
            return fail("插件未就绪", 503)
        return json_response(
            {
                "items": await self.engine.store.call(
                    "memory_versions", self.qint("id", 0, 0, 2**53 - 1)
                )
            }
        )

    async def api_save_memory(self):
        if not self.ready():
            return fail("插件未就绪", 503)
        try:
            obj = await self.body()
            p = manual_proposal(obj, self.engine.config.snapshot()["memory"])
            identity = obj.get("id")
            if identity is not None and (type(identity) is not int or identity <= 0):
                raise ValueError("无效记忆ID")
            mid = await self.engine.store.call("manual_memory", p, identity)
            self.engine.log.emit("MEMORY", "manual_saved")
            return json_response({"ok": True, "id": mid})
        except sqlite3.IntegrityError:
            return fail("此会话存在相同人物/类型/主题的记忆，请编辑原记录")
        except ValueError as e:
            return fail(e)

    async def api_memory_action(self):
        if not self.ready():
            return fail("插件未就绪", 503)
        try:
            obj = await self.body()
            mid = obj.get("id")
            action = obj.get("action")
            if type(mid) is not int or mid <= 0:
                raise ValueError("无效记忆ID")
            if action == "delete":
                if obj.get("confirm") is not True:
                    raise ValueError("删除需要明确确认")
                ok = await self.engine.store.call("delete_memory", mid)
            elif action in ("archive", "restore"):
                ok = await self.engine.store.call(
                    "archive_memory", mid, action == "restore"
                )
            else:
                raise ValueError("无效操作")
            if not ok:
                return fail("记录不存在，已过期，或置顶记录不能归档")
            self.engine.log.emit("MEMORY", str(action))
            return json_response({"ok": True})
        except ValueError as e:
            return fail(e)

    async def api_styles(self):
        if not self.ready():
            return fail("插件未就绪", 503)
        rows = await self.engine.store.call(
            "list_styles",
            self.q("scope"),
            self.q("q"),
            self.q("status"),
            self.qint("offset", 0, 0, 1000000),
            self.qint("limit", 80, 1, 200),
        )
        return json_response({"items": rows})

    async def api_style_action(self):
        if not self.ready():
            return fail("插件未就绪", 503)
        try:
            obj = await self.body()
            sid = obj.get("id")
            action = obj.get("action", "save")
            if (
                type(sid) is not int
                or sid <= 0
                or action not in ("save", "delete", "undo")
            ):
                raise ValueError("无效表达操作")
            if action == "delete" and obj.get("confirm") is not True:
                raise ValueError("删除需要确认")
            await self.engine.store.call("edit_style", sid, obj, action)
            self.engine.log.emit("LEARNING", "manual_" + action)
            return json_response({"ok": True})
        except sqlite3.IntegrityError:
            return fail("表达模式重复")
        except ValueError as e:
            return fail(e)

    async def api_usage(self):
        if not self.ready():
            return fail("插件未就绪", 503)
        rows = await self.engine.store.call("usage_rows", self.qint("days", 15, 1, 90))
        prices = self.engine.config.snapshot()["budget"]["pricing"]
        for row in rows:
            rates = prices.get(row["provider"])
            row["cost_cny_estimate"] = (
                round(
                    (
                        row["input_other"] * rates["input"]
                        + row["input_cached"] * rates["cached"]
                        + row["output"] * rates["output"]
                    )
                    / 1e6,
                    6,
                )
                if rates
                else None
            )
        return json_response(
            {
                "items": rows,
                "note": "用量由 Provider 返回；缺失/embedding 为估算，价格为手动配置的估算，非账单。",
            }
        )

    async def api_logs(self):
        if not self.ready():
            return fail("插件未就绪", 503)
        try:
            return json_response(
                await asyncio.to_thread(
                    self.engine.log.list,
                    self.q("day"),
                    self.q("level"),
                    self.q("module"),
                    self.q("q"),
                    self.qint("limit", 300, 1, 2000),
                )
            )
        except ValueError as e:
            return fail(e)

    async def api_action(self):
        if not self.ready():
            return fail("插件未就绪", 503)
        try:
            obj = await self.body()
            name = obj.get("name")
            scope = obj.get("scope") or None
            if scope and not await self.engine.store.call("get_session", scope):
                raise ValueError("会话不存在")
            return json_response({"ok": self.engine.start_action(name, scope)})
        except ValueError as e:
            return fail(e)
