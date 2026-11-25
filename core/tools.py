from __future__ import annotations

import asyncio
from collections import OrderedDict

import jsonschema

from .dota import fit_stats_payload
from .dota_details import EFFECTS, fit_knowledge_payload
from .memory import fit_recall_payload, normalize_proposals
from .util import clean, dump, now, unsafe_secret


def prop(desc, kind="string", **kwargs):
    return {"type": kind, "description": desc, **kwargs}


def tool(name, desc, props, required=()):
    return {
        "name": name,
        "description": desc,
        "parameters": {
            "type": "object",
            "properties": props,
            "required": list(required),
            "additionalProperties": False,
        },
    }


QUERY = prop(
    "结合完整聊天上下文生成具体搜索词，不要用'帮我看看'", maxLength=240, minLength=1
)
HERO = prop("英雄名称、英文名或别名，如紫猫", maxLength=80, minLength=1)
RANK = prop(
    "Valve 对抗原始 rank=0..6，非总体胜率；省略使用配置默认对抗分组",
    "integer",
    minimum=0,
    maximum=6,
)
WEEK = prop(
    "Valve 原始周索引1..8；默认1（推定最近），起止日期未核实",
    "integer",
    minimum=1,
    maximum=8,
)
RATE_SCOPE = {
    "bracket": prop(
        "OpenDota 段位：1先锋、2卫士、3中军、4统帅、5传奇、6万古流芳、7超凡入圣、8冠绝一世（可能无样本）；与 rank_chunk 互斥",
        "integer", minimum=1, maximum=8,
    ),
    "rank_chunk": prop(
        "仅明确查询 Valve 周统计原始分组时填写0..6，段位映射未核实；与 bracket 互斥",
        "integer", minimum=0, maximum=6,
    ),
    "week": WEEK,
}
LIMIT = prop("最多返回条数", "integer", minimum=1, maximum=30)
DATE = prop("可选日快照日期 YYYY-MM-DD，省略为最新已校验快照", maxLength=10)
ORDER = prop("desc 从高到低；asc 从低到高", enum=["desc", "asc"])
PATCH = prop("具体补丁（如7.42a）；省略查询当前可用资料。历史详情要求对应完整快照。", maxLength=12)
VIEW = prop("full=完整，effects=效果及说明，numbers=数值，upgrades=升级", enum=["full","effects","numbers","upgrades"])
OFFSET = prop("从第几条继续（从0开始，使用上次返回的 next_offset）", "integer", minimum=0, maximum=10000)


class ToolRouter:
    def __init__(self, host, store, memory, dota, log, config, research=None):
        self.host, self.store, self.memory, self.dota, self.log, self.config = (
            host,
            store,
            memory,
            dota,
            log,
            config,
        )
        self.web_cache = OrderedDict()
        self.research = research

    def bindings(self, meta, cfg):
        out = {}
        if not cfg["tools"]["kb_enabled"]:
            return out
        for b in cfg["tools"]["kb_bindings"]:
            if (meta["kind"] == "private" and b["allow_private"]) or (
                meta["kind"] == "group"
                and (
                    meta["location"] in b["allowed_groups"]
                    or meta["umo"] in b["allowed_groups"]
                )
            ):
                out[b["alias"]] = b
        return out

    def specs(self, meta, cfg, explicit=False):
        specs = []
        if cfg["memory"]["enabled"]:
            specs.append(
                tool(
                    "recall_memory",
                    "主动回忆此会话内群友的事实、共同经历、约定，按稳定人物代号关联个人及多人记忆。普通搜索只返回仍有效、可见的活跃记忆；精确旧细节才用 deep，历史变化才用 historical。",
                    {
                        "query": prop("要回忆的关键词或明确问题", maxLength=240),
                        "who": prop(
                            "current=发言者，group=本群公共记忆；优先使用 speaker 代号，也可用当前或历史昵称/别名；空=本会话。重名需要确定代号",
                            maxLength=80,
                        ),
                        "kind": prop(
                            "记忆类型",
                            enum=[
                                "core",
                                "profile",
                                "experience",
                                "ordinary",
                                "temporary",
                                "habit",
                            ],
                        ),
                        "deep": prop("必要时查深层存档及完整细节", "boolean"),
                        "historical": prop(
                            "仅历史问题使用，可能返回已失效记录", "boolean"
                        ),
                        "from_time": prop("ISO 起始日期", maxLength=32),
                        "to_time": prop("ISO 结束日期", maxLength=32),
                        "limit": prop("结果条数", "integer", minimum=1, maximum=20),
                    },
                )
            )
            if explicit and (
                meta["kind"] != "private" or cfg["memory"]["capture_private"]
            ):
                specs.append(
                    tool(
                        "remember_memory",
                        "仅本轮明确要求记住的本人稳定事实或临时约定。必须有本轮本人实际消息支持，不保存敏感标识/指令。成功后才能说已记住。",
                        {
                            "key": prop(
                                "稳定主题键，如 birthday", maxLength=80, minLength=1
                            ),
                            "kind": prop(
                                "记忆类型", enum=["core", "profile", "temporary"]
                            ),
                            "title": prop("标题", maxLength=100, minLength=1),
                            "full": prop(
                                "本人明确给出的事实", maxLength=450, minLength=1
                            ),
                            "gist": prop(
                                "语义要旨（日期可只保留月份）",
                                maxLength=300,
                                minLength=1,
                            ),
                            "trace": prop("最短印象", maxLength=120, minLength=1),
                            "valid_until": prop(
                                "临时约定截止 ISO 日期时间", maxLength=32
                            ),
                        },
                        ("key", "kind", "title", "full", "gist", "trace"),
                    )
                )
        if self.research and cfg["research"]["enabled"]:
            specs.append(tool(
                "recall_research",
                "回忆自身主动阅读过的公开游戏资料和主观看法。仅当前人格，非个人聊天记忆；不能自行检索外部网页。recent 可查询近期实际阅读内容。",
                {"query":prop("相关公开游戏主题",maxLength=200),"recent":prop("查询近期阅读记录","boolean")}
            ))
        bindings = self.bindings(meta, cfg)
        if bindings:
            description = "检索指定的文档知识库而不是混合全部库。" + "；".join(
                f"{k}: {clean(v['description'], 160) or v['name']}"
                for k, v in bindings.items()
            )
            specs.append(
                tool(
                    "search_knowledge",
                    description,
                    {
                        "source": prop("选择正确的知识库", enum=list(bindings)),
                        "query": QUERY,
                    },
                    ("source", "query"),
                )
            )
        if cfg["dota"]["enabled"]:
            specs.extend(
                [
                    tool(
                        "query_dota_hero",
                        "Dota官方英雄、技能、天赋、神杖/魔晶。支持俗称、Q/W/E/R、大招及一至四技能。历史补丁需对应快照。按需要用 view 缩小结果。",
                        {
                            "hero": HERO,
                            "skill": prop(
                                "技能名、别名、按键、天赋/10级天赋、神杖、魔晶、先天；空则按 section 查询", maxLength=100
                            ),
                            "section":prop("查询栏目", enum=["summary","abilities","talents","innate","scepter","shard"]),
                            "view":VIEW,
                            "patch":PATCH,
                            "offset":OFFSET,
                            "limit":LIMIT,
                        },
                        ("hero",),
                    ),
                    tool(
                        "query_dota_item",
                        "查 Dota 2 装备、消耗品、中立物品、附魔、图纸及数值，支持俗称。",
                        {"item": prop("物品名或俗称", minLength=1, maxLength=100), "patch":PATCH, "view":VIEW},
                        ("item",),
                    ),
                    tool(
                        "search_dota_mechanics",
                        "从同一已验收补丁的完整目录筛选所有匹配技能/物品。条件为 AND；结构化字段可精确筛选，效果文字只给有依据的候选。至少一项条件。",
                        {
                            "kind":prop("英雄技能（默认）/物品/两类",enum=["hero_ability","item","any"]),
                            "hero":HERO,
                            "patch":PATCH,
                            "damage_type":prop("官方伤害归类",enum=["physical","magical","pure","hp_removal"]),
                            "ability_type":prop("技能类别",enum=["basic","ultimate","innate","scepter_granted","shard_granted"]),
                            "behavior":prop("技能行为",enum=["passive","no_target","unit_target","point","aoe","channelled"]),
                            "pierces_debuff_immunity":prop("官方对敌免疫标记：true穿透，false不穿透；未知不匹配","boolean"),
                            "dispellability":prop("效果能否被驱散，非技能执行驱散",enum=["strong_only","basic","not_dispellable"]),
                            "target_team":prop("官方目标队伍",enum=["ally","enemy","both"]),
                            "cooldown_max":prop("最高提供基础等级冷却不超过此秒数", "number",minimum=0,maximum=10000),
                            "cast_range_min":prop("最高提供基础等级施法距离不少于此值", "number",minimum=0,maximum=10000),
                            "effect":prop("描述/注释中的机制候选（非完整机制证明）",enum=list(EFFECTS)),
                            "include_upgrades":prop("效果文字匹配是否含神杖/魔晶（默认不含）","boolean"),
                            "limit":LIMIT,
                            "offset":OFFSET,
                        }
                    ),
                    tool(
                        "query_dota_patch",
                        "查指定补丁官方公告（英雄、物品或通用）；省略 patch 为最新已核实版本。from_patch 仅在两份完整快照存在时按字段比较同一英雄/物品，勿猜历史。",
                        {"patch":PATCH,"hero":HERO,"item":prop("物品名/别名",maxLength=100),"from_patch":PATCH,"offset":OFFSET,"limit":LIMIT},
                    ),
                    tool(
                        "get_hero_stats",
                        "查英雄自身胜率。未指定范围时用 OpenDota 公开比赛总体样本；指定段位用 bracket，明确 Valve 原始周分组才用 rank_chunk。切勿用对抗统计替代。",
                        {"hero": HERO, **RATE_SCOPE, "observed_date": DATE},
                        ("hero",),
                    ),
                    tool(
                        "rank_hero_stats",
                        "按已校验的英雄自身胜率精确排序：最高/最低/前N名。默认 OpenDota 公开总体样本；明确段位才传 bracket。不是对抗数据 rank=0。",
                        {
                            **RATE_SCOPE,
                            "limit": LIMIT,
                            "order": ORDER,
                            "observed_date": DATE,
                        },
                    ),
                    tool(
                        "compare_hero_stats",
                        "比较两个英雄同口径的自身胜率。默认 OpenDota 公开总体样本。明确需要对阵/配合时可 include_matchup；对抗结果来源和分组单列，不能与自身胜率混同。",
                        {
                            "hero": HERO,
                            "other": HERO,
                            **RATE_SCOPE,
                            "include_matchup": prop("是否额外返回对阵、同队胜率（独立 Valve 数据）", "boolean"),
                            "matchup_rank": RANK,
                            "observed_date": DATE,
                        },
                        ("hero", "other"),
                    ),
                    tool(
                        "get_dota_matchups",
                        "独立 Valve 对抗工具。查克制/被克制/配合：counters=该英雄对阵最低，favored=最高，allies=同队最高；other 指定某个对手/队友。不是自身胜率，不自动映射段位。",
                        {
                            "hero": HERO,
                            "other": prop("可选第二英雄，指定时查询具体对阵和同队胜率", maxLength=80, minLength=1),
                            "mode": prop(
                                "关系排序方式", enum=["counters", "favored", "allies"]
                            ),
                            "rank": RANK,
                            "limit": LIMIT,
                            "observed_date": DATE,
                        },
                        ("hero",),
                    ),
                    tool(
                        "get_dota_trends",
                        "查胜率变化（百分点）。OpenDota：比较同口径两份日快照，用 days；Valve：明确 rank_chunk，用 week、previous_week 对比，同快照单英雄另返回8周序列，不传 days。周日期与段位映射未核实。",
                        {
                            "hero": prop("可选英雄；空表示所有英雄", maxLength=80),
                            "days": prop("仅 OpenDota 日快照：回看天数（默认7）", "integer", minimum=1, maximum=180),
                            **RATE_SCOPE,
                            "previous_week": prop("仅 Valve：对比周索引1..8，默认 week+1", "integer", minimum=1, maximum=8),
                            "limit": LIMIT,
                            "order": ORDER,
                            "observed_date": DATE,
                        },
                    ),
                ]
            )
        if cfg["tools"]["web_enabled"]:
            specs.append(
                tool(
                    "search_web",
                    "按需搜索公开网页或近期信息。禁止向公开搜索服务提交聊天隐私、用户 ID、密钥、非公开业务信息。",
                    {"query": QUERY},
                    ("query",),
                )
            )
        return specs

    async def _call(self, name, args, meta, cfg, event, current, batch):
        if name=="recall_research" and self.research:
            return await self.research.recall(meta,cfg,args.get("query",""),args.get("recent",False),explicit=True)
        if name == "recall_memory":
            if not args.get("query") and not args.get("who"):
                return {"error": "请提供查询词或人物"}
            return await self.memory.recall(meta, cfg, current, **args)
        if name == "remember_memory":
            evidence = [
                r
                for r in batch
                if r["explicit"] and r["ref"] == current and not r["bot"]
            ]
            if (
                not evidence
                or not cfg["memory"]["explicit_immediate"]
                or not cfg["memory"]["enabled"]
                or (meta["kind"] == "private" and not cfg["memory"]["capture_private"])
            ):
                return {
                    "error": "本轮不允许保存持久记忆，或没有可验证的本人明确记住请求"
                }
            item = {
                **args,
                "subject": current,
                "importance": 85,
                "confidence": 0.96,
                "source_ids": [r["seq"] for r in evidence],
                "confirmed_update": True,
            }
            proposals, _ = normalize_proposals(
                {"memories": [item]}, meta["scope"], evidence, cfg["memory"]
            )
            if not proposals:
                return {"error": "记忆没有通过证据及隐私校验"}
            result = await self.store.call("remember_explicit", proposals[0])
            if not result["saved"]:
                return {
                    "saved": False,
                    "error": "未保存新事实：现有记忆受保护或已失效，请由管理员核对。",
                }
            return {
                "saved": True,
                "kind": args["kind"],
                "note": "该事实已进入本会话记忆。",
            }
        if name == "search_knowledge":
            b = self.bindings(meta, cfg).get(args["source"])
            if not b:
                return {"error": "当前会话不可访问这个知识库"}
            return await self.host.kb(
                b["name"], args["query"], cfg["tools"]["kb_top_k"]
            )
        if name == "query_dota_hero":
            options={k:v for k,v in args.items() if k in ("patch","section","view","offset","limit")}
            return await self.dota.hero(
                cfg["dota"], args["hero"], args.get("skill", ""), **options
            )
        if name == "query_dota_item":
            return await self.dota.item(cfg["dota"], args["item"], **{k:v for k,v in args.items() if k in ("patch","view")})
        if name == "search_dota_mechanics":
            return await self.dota.mechanics(cfg["dota"], **args)
        if name == "query_dota_patch":
            return await self.dota.patch_changes(cfg["dota"], **args)
        actions = {
            "get_hero_stats": "hero",
            "rank_hero_stats": "ranking",
            "compare_hero_stats": "compare",
            "get_dota_matchups": "matchups",
            "get_dota_trends": "trends",
        }
        if name in actions:
            return await self.dota.stats(cfg["dota"], actions[name], **args)
        if name == "search_web":
            query = args["query"].strip()
            if unsafe_secret(query):
                return {"error": "拒绝将敏感标识提供给搜索服务"}
            key = (meta["scope"], query.casefold())
            ttl = cfg["tools"]["web_cache_minutes"] * 60
            old = self.web_cache.get(key)
            if ttl and old and now() - old[0] < ttl:
                return old[1]
            data = await self.host.web_search(event, query)
            if ttl:
                self.web_cache[key] = (now(), data)
                while len(self.web_cache) > 100:
                    self.web_cache.popitem(last=False)
            return data
        return {"error": "该工具不可用"}

    async def expand(self, plan, ready, args, meta, current, batch):
        spec = next((s for s in ready if s["name"] == "open_tools"), None)
        live = self.config.snapshot()
        from .memory import allowed
        if not spec or live["access"]["mode"] != "active" or not allowed(meta, live):
            return dump({"error": "该工具未授权"})
        try:
            jsonschema.validate(args, spec["parameters"])
        except jsonschema.ValidationError:
            return dump({"error": "请选择本轮允许展开的类别"})
        person = await self.store.call("person", meta["scope"], meta["uid"])
        if person and person["blocked"]:
            return dump({"error": "该用户已被屏蔽"})
        latest = self.specs(meta, live, any(r["explicit"] and r["ref"] == current for r in batch))
        result = plan.expand(args["groups"], latest)
        self.log.emit("TOOL", "expanded", scope=meta["scope"], groups=result["expanded"])
        return dump(result)

    async def execute(self, specs, name, args, meta, cfg, event, current, batch):
        spec = next((s for s in specs if s["name"] == name), None)
        # Re-check currently configured access before running any tool in a long conversation.
        live = self.config.snapshot()
        latest_specs = self.specs(
            meta, live, any(r["explicit"] and r["ref"] == current for r in batch)
        )
        if not spec or not any(s["name"] == name for s in latest_specs):
            return dump({"error": "该工具未授权"})
        if not isinstance(args, dict):
            return dump({"error": "参数必须为 JSON 对象"})
        try:
            jsonschema.validate(args, spec["parameters"])
        except jsonschema.ValidationError:
            return dump({"error": "参数与工具定义不符，请检查工具字段与类型"})
        if live["access"]["mode"] != "active":
            return dump({"error": "插件非正式运行模式"})
        from .memory import allowed

        if not allowed(meta, live):
            return dump({"error": "该会话已被停用"})
        person = await self.store.call("person", meta["scope"], meta["uid"])
        if person and person["blocked"]:
            return dump({"error": "该用户已被屏蔽"})
        try:
            # No arbitrary functions, filesystem paths or network endpoints are accepted from the model.
            data = await asyncio.wait_for(
                self._call(name, args, meta, live, event, current, batch),
                timeout=4 * live["dota"]["network_timeout"] + 8 if name == "query_dota_patch"
                else 2 * live["dota"]["network_timeout"] + 8
                if name in ("query_dota_hero", "query_dota_item", "search_dota_mechanics")
                else 32,
            )
            maximum = cfg["reply"]["tool_result_chars"]
            if name == "recall_memory" and isinstance(data, dict):
                # Identity refs have already been validated. A generic digit/redaction regex
                # must never mutate a short ref or cut through a participant's JSON object.
                result = dump(fit_recall_payload(data, maximum))
            elif name=="recall_research" and isinstance(data,dict):
                result=dump(data)
                if len(result)>maximum:
                    result=dump({"notes":[],"note":"见闻结果预算不足，请提高工具结果字符预算。"})
            elif name in ("get_hero_stats", "rank_hero_stats", "compare_hero_stats", "get_dota_matchups", "get_dota_trends") and isinstance(data, dict):
                result = dump(fit_stats_payload(data, maximum))
            elif name in ("query_dota_hero","query_dota_item","search_dota_mechanics","query_dota_patch") and isinstance(data,dict):
                # Scrub strings separately; preserve actual numeric objects and complete JSON.
                def scrub(value, field=""):
                    if isinstance(value,str):
                        if field == "behavior" and value.isascii() and value.isdecimal() and len(value)<=20:
                            return value
                        return clean(value, len(value), urls=True)
                    if isinstance(value,list):
                        return [scrub(v) for v in value]
                    if isinstance(value,dict):
                        return {k:scrub(v,k) for k,v in value.items()}
                    return value
                result = dump(fit_knowledge_payload(scrub(data),maximum))
            else:
                result = data if isinstance(data, str) else dump(data)
                # Redact sensitive values without stripping reference URLs; tool output remains untrusted data.
                result = clean(result, maximum + 1, urls=True)
                if len(result) > maximum:
                    result = result[:maximum] + "\n[工具结果已裁剪，可缩小查询范围重试]"
            self.log.emit(
                "TOOL",
                "completed",
                scope=meta["scope"],
                tool=name,
                result_chars=len(result),
            )
            return result or "无结果"
        except Exception as e:
            self.log.emit(
                "TOOL",
                "failed",
                "WARNING",
                scope=meta["scope"],
                tool=name,
                exception=type(e).__name__,
            )
            return dump({"error": "工具暂时不可用，请勿假称查到数据。"})
