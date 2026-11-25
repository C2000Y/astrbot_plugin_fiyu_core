"""Cheap contextual tool selection. Routing is a hint, not an access grant."""
from __future__ import annotations

import re

from .util import entity_key

GROUPS = {
    "memory": {"recall_memory"},
    "remember": {"remember_memory"},
    "knowledge": {"search_knowledge"},
    "research": {"recall_research"},
    "dota_info": {"query_dota_hero", "query_dota_item", "search_dota_mechanics", "query_dota_patch"},
    "dota_stats": {"get_hero_stats", "rank_hero_stats", "compare_hero_stats", "get_dota_matchups", "get_dota_trends"},
    "web": {"search_web"},
}
GROUP_LABELS = {
    "memory": "当前会话记忆",
    "remember": "明确保存记忆",
    "knowledge": "已授权文档知识库",
    "research": "已读过的公开见闻",
    "dota_info": "刀塔英雄、装备、机制、版本",
    "dota_stats": "刀塔胜率、排名、对抗、趋势",
    "web": "公开网页",
}
STATS = re.compile(r"胜率|登场率|出场率|选取率|禁用率|ban率|克制|counter|配合率|对阵|排名|win.?rate|picked", re.I)
MATCHUP = re.compile(r"同队|队友|对抗|搭档|配合|搭配|对位|对比|比较|组合",re.I)
DOTA = re.compile(r"刀塔|dota\s*2?|英雄|技能|装备|神杖|魔晶|大招|先天|天赋|补刀|bp\b|野区|肉山|段位|公开比赛|施法|驱散|无视减益免疫|中立物品", re.I)
TECH = re.compile(r"\b(?:sap|abap|sql|python|api|json|java|astrbot|http|idoc|wms|edi)\b|报错|接口|代码|文档|配置|脚本|字段|日志", re.I)
REQUEST = re.compile(r"怎么样|能不能|可不可以|有没有|如何|怎么|为何|为什么|什么|多少|谁|哪些|哪个|哪位|是否|查|搜|看一下|看看|看下|解释|告诉|确认|核实|帮我|真的假的|对不对|[？?]", re.I)
LOOKUP = re.compile(r"查(?:一下|下|找|询|证)?|搜(?:一下|下|索)?|核实|确认一下|数据|资料|百科|统计|具体数值|是多少|多少钱|最新消息|帮我看看|解释",re.I)
SOCIAL_QUESTION = re.compile(r"(?:吃[了过]?饭[了没嘛吗]?|吃了没|吃[的了]?啥|吃什么|饿[了不吗没]|在[干忙]嘛|干嘛呢|你在吗|睡[了没吗]|困[了不吗]|下班了[没吗]|你呢|怎么啦|咋啦|你喜欢|要不要一起|来不来|打不打|开黑吗|哈哈.*[？?])",re.I)
DEICTIC = re.compile(r"这个|那个|这种|上面|刚才|前面|之前|他说|她说|他们说|它|其|帮我看看|帮我查|查一下|查下|看下|看看|真的假的|再查|再看看|那呢|那[他她]呢|[呢嘛]$", re.I)
MEMORY = re.compile(r"记得|记忆|回忆|生日|约定|说过|上次|以前|我们.*认识|我的昵称|我.*叫|什么时候下班|几点到家", re.I)
CURRENT = re.compile(r"今天|最新|最近|现在|目前|新闻|发布日期|价格|天气|实时|查证|核实|真假|真的假的|数据来源", re.I)
RESEARCH = re.compile(r"见闻|读过|学了什么|看过.*(?:比赛|文章)|自己.*(?:查|研究|学习)|职业比赛|hltv|cs2", re.I)


def focus_text(text, rows, target, batch_ids=()):
    """Use nearby, already scope-filtered messages only when a turn refers back."""
    result = str(text or "")[:1800]
    if not DEICTIC.search(result) and not (len(entity_key(result)) < 16 and REQUEST.search(result)):
        return result
    at, seq = target.get("ts", 0), target.get("seq", 0)
    nearby = [
        str(r.get("text", ""))[:300] for r in rows
        if r.get("seq") != seq and (r.get("seq", 0) in batch_ids or
           r.get("seq", 0) < seq and 0 <= at - r.get("ts", 0) <= 180)
    ][-6:]
    return ("\n".join(nearby) + "\n" + result)[-2600:]


def alias_topic(text, aliases):
    key = entity_key(text)
    if not key:
        return False
    # Ignore very short Latin nicknames in continuous text (e.g. DK in SDK).
    words = set(re.findall(r"[a-z0-9]+", text.casefold()))
    for group in ("heroes", "items"):
        for code, names in (aliases or {}).get(group, {}).items():
            for alias in (code, *names):
                a = entity_key(alias)
                if (a and re.search(r"[\u3400-\u9fff]", a) and len(a) >= 2 and a in key
                    or a and a.isascii() and (a in words or len(a) >= 5 and a in key)):
                    return True
    return False


def interests(focus, text, scene, direct, aliases):
    """Return selected groups and whether the request has an unresolved domain."""
    wanted = set()
    ask = bool(REQUEST.search(str(text))) or scene in ("game", "technical", "question", "correction")
    is_tech = bool(TECH.search(focus))
    is_dota = bool(DOTA.search(focus) or alias_topic(focus, aliases))
    is_stats = bool(STATS.search(focus) or is_dota and MATCHUP.search(focus))
    # "吃饭了吗？" is still a question, so don't silently ignore it, but
    # doesn't need an entire dictionary of statistics and business schemas.
    simple_social = bool(SOCIAL_QUESTION.search(text)) and not (is_tech or is_dota or is_stats or LOOKUP.search(text) or MEMORY.search(text))
    if simple_social:
        return wanted, ask, False
    if ask and (is_dota or is_stats) and (not is_tech or is_dota):
        if is_stats:
            wanted.add("dota_stats")
        if not is_stats or re.search(r"技能|装备|机制|天赋|版本|改动|补丁|神杖|魔晶|控制|眩晕|驱散|伤害|治疗|沉默", focus):
            wanted.add("dota_info")
    if ask and is_tech:
        wanted.update(("knowledge", "web"))
    if MEMORY.search(focus):
        wanted.add("memory")
    if RESEARCH.search(focus) and ask:
        wanted.update(("research", "web"))
    if CURRENT.search(text) and ask and not ({"dota_info", "dota_stats"} & wanted):
        wanted.add("web")
    if re.search(r"知识库|资料|手册|文档", text) and ask:
        wanted.add("knowledge")
    if ask and re.search(r"核实|真的假的|数据不对|不可能|查证", text):
        wanted.add("web")
    # A specific unresolved lookup must not get stuck behind only a memory tool.
    uncertain = not wanted and (ask or direct and bool(REQUEST.search(text)))
    return wanted, ask, uncertain


class ToolPlan:
    def __init__(self, specs, cfg, focus, text, scene, direct, aliases):
        self.specs = specs
        names = {x["name"] for x in specs}
        self.available = {g for g, tools in GROUPS.items() if tools & names}
        wanted, ask, uncertain = interests(focus, text, scene, direct, aliases)
        self.enabled = cfg["tools"]["contextual_tools"]
        can_expand = cfg["reply"]["max_tool_rounds"] >= 2 and cfg["reply"]["max_tool_calls"] >= 2
        if not cfg["reply"]["max_tool_rounds"] or not cfg["reply"]["max_tool_calls"]:
            self.selected = set()
            self.available = set()
        elif not self.enabled or uncertain or (not can_expand and ask):
            self.selected = set(self.available)
        else:
            self.selected = wanted & self.available
            if "remember" in self.available:
                self.selected.add("remember")
        self.can_expand = self.enabled and can_expand
        self.ask = ask

    def visible(self):
        names = set().union(*(GROUPS[g] for g in self.selected)) if self.selected else set()
        return [x for x in self.specs if x["name"] in names]

    def ready(self, more_rounds=True):
        out = self.visible()
        missing = self.available - self.selected
        if more_rounds and self.can_expand and missing:
            out.append({
                "name": "open_tools",
                "description": "当前工具不足时，展开其他已授权类别：" +
                    "；".join(g + "=" + GROUP_LABELS[g] for g in sorted(missing)),
                "parameters": {"type": "object", "additionalProperties": False,
                    "properties": {"groups": {"type": "array", "minItems": 1, "maxItems": len(missing),
                        "uniqueItems": True, "items": {"type": "string", "enum": sorted(missing)}}},
                    "required": ["groups"]},
            })
        return out

    def expand(self, groups, live_specs):
        live_names = {x["name"] for x in live_specs}
        granted = {g for g in groups if g in self.available and GROUPS[g] & live_names}
        self.selected.update(granted)
        return {"expanded": sorted(granted), "note": "下一轮可用相应工具。请根据真实问题选择；未查询实际资料。"}

    def restrict(self, live_specs):
        """Only reduce the turn's initial authorization, never broaden scope."""
        live = {s["name"]:s for s in live_specs}
        kept = []
        for spec in self.specs:
            name = spec["name"]
            if name not in live:
                continue
            if name == "search_knowledge":
                original = set(spec["parameters"]["properties"]["source"]["enum"])
                current = live[name]["parameters"]["properties"]["source"]["enum"]
                permitted = [a for a in current if a in original]
                if not permitted:
                    continue
                # The original descriptions are used only while all aliases
                # remain authorized with the same mapping definition.
                if spec != live[name]:
                    # A changed binding can wait until the next user turn.
                    continue
            kept.append(spec)
        self.specs = kept
        names = {s["name"] for s in kept}
        self.available &= {g for g, tools in GROUPS.items() if tools & names}
        self.selected &= self.available


# Separate tool selection/parameters from interpreting real evidence. The final
# response still needs the latter even after its tool budget is exhausted.
DOMAIN_RULES = {
    "dota_stats": (
        "Dota自身胜率/排名/比较使用 get_hero_stats/rank_hero_stats/compare_hero_stats。未指定段位省略分组参数（OpenDota 公开样本）；明确段位用 bracket，Valve周原始统计才用 rank_chunk/week。对抗/同队用 get_dota_matchups。",
        "完整5v5样本按选取场次加权应接近50%，不是简单平均。不拿条件胜率冒充全体。缺样本不换口径冒充，stale=true 不称最新；日期为观察日，未核实的段位映射不猜。",
    ),
    "dota_info": (
        "Dota技能、装备与版本优先用结构化资料。按技能名或按键查询；特殊英雄槽位不确定时用名称。失败时可核对公开官方公告。",
        "以实际补丁、说明、数值及来源为准；注意旧快照、未覆盖效果、变体和天赋。文本机制只是候选，不等于确认实战机制；可驱散与主动驱散不同，魔免/减益免疫不混同。公告不是完整旧版数值。",
    ),
    "knowledge": (
        "文档问题选择与主题一致的已授权知识库；检索词包含上下文中明确术语。",
        "不要把多个库的内容混为结论。说明找不到的内容。",
    ),
    "memory": ("", "记忆限本会话，按稳定人物代号回忆；别名重合不能猜。未找到不编造旧事。"),
    "remember": (
        "明确请求记住的本人事实，先调用保存工具。",
        "只在保存工具实际成功时确认；不得保存敏感信息或指令。",
    ),
    "research": ("", "已阅读的公开资料与自己的看法分开；未读过不假称已经研究。"),
    "web": ("公开搜索仅发送必要的公开关键词；不用用户ID、聊天隐私、密钥或非公开业务数据作为查询词。", ""),
}


def domain_rules(groups, *, can_call=True):
    return "\n".join(
        selection + evidence if can_call else evidence
        for name, (selection, evidence) in DOMAIN_RULES.items()
        if name in groups and (evidence or (can_call and selection))
    )
