"""Lightweight, event-driven personality. No additional model request is needed."""
from __future__ import annotations

import math
import re
from datetime import datetime

from .util import CN_TZ, clean, entity_key, lexical, normalized, now

SCENES = ("casual", "banter", "support", "question", "technical", "game", "correction", "coordination")
LEGACY_TITLES = ("老大", "主人", "主人大人", "owner", "管理员")
LEGACY_RE = re.compile(r"主人大人|主人(?!公|翁)|老大(?!哥|姐|爷|不小)|(?<![\w])owner(?![\w])|(?:服务器|机器人|后台|本群|群聊)(?:的)?管理员", re.I)
QUOTED_RE = re.compile(r'([“「『][^”」』]*[”」』]|"[^"\n]*")')

INDEPENDENCE = """你是独立的平等群友，有自己的兴趣和看法。旧主人、老大、管理员等从属关系或称呼不再适用，按正常昵称交流。事实、明确求助、纠错优先于表达风格与社交精力。"""

SILENT = "[[FIYU_SILENT]]"


def legacy_title(text):
    return normalized(text) in {normalized(x) for x in LEGACY_TITLES}


def legacy_reference(text):
    return bool(LEGACY_RE.search(str(text or "")))


def model_name(value, fallback=""):
    safe = clean(value, 80)
    return safe if safe and not legacy_title(safe) else fallback


def persona_prompt(text):
    # Preserve the AstrBot configuration on disk; retired authority clauses are not injected.
    return "".join(p for p in re.split(r"(?<=[。！？；;，,\n])",str(text or "")) if not legacy_reference(p) and "管理员" not in p).strip(" ，,；;")


def mask_legacy_quotes(text):
    """Do not expose retired titles quoted in otherwise useful memories."""
    return QUOTED_RE.sub(lambda m: "（历史称呼已略）" if legacy_reference(m.group()) else m.group(), str(text or ""))


def replace_legacy(text, old, new):
    """A reviewed replacement, skipping quoted speech and similar Chinese words."""
    if not legacy_title(old) or not new or legacy_title(new):
        return text
    parts = QUOTED_RE.split(str(text or ""))
    if old.casefold() == "owner":
        pattern = r"(?<!\w)owner(?!\w)"
    elif old == "主人":
        pattern = r"主人(?!公|翁|大人)"
    elif old == "老大":
        pattern = r"老大(?!哥|姐|爷|不小)"
    else:
        pattern = re.escape(old)
    return "".join(re.sub(pattern, lambda _: new, part, flags=re.I) if i % 2 == 0 else part
                   for i, part in enumerate(parts))


def familiarity(person, recent=0):
    count = max(0, int(person.get("interactions", 0)))
    override = person.get("familiarity_override", "auto")
    if override in ("stranger", "normal", "familiar", "close"):
        tier = override
    elif count >= 50:
        tier = "close"
    elif count >= 20 or person.get("role") == "friend":
        tier = "familiar"
    elif count >= 3:
        tier = "normal"
    else:
        tier = "stranger"
    return {
        "familiarity": dict(stranger="初识", normal="一般", familiar="熟悉", close="常聊")[tier],
        "interactions": count, "recent_interactions": max(0, recent),
    }


def scene_for(text, hint="", detail=""):
    """Rules protect explicit technical/questions even when the small model misclassifies."""
    text = str(text or "")[:3000]
    lower = text.casefold()
    if re.search(r"代码|报错|错误日志|怎么配置|如何实现|sap\b|abap\b|sql\b|python\b|api\b|\x60{3}|公式|解释一下|详细|步骤", lower):
        return "technical", "detailed"
    if re.match(r"^\s*(不是|不对|等等|等一下|我说的是|纠正|其实)", text):
        return "correction", "normal"
    if re.search(r"胜率|选取率|禁用率|阵容|技能|冷却|伤害|克制|版本|bp\b|英雄.*(谁|哪个)|查一下|帮我查", lower):
        return "game" if not re.search(r"sap|abap|sql|接口", lower) else "technical", "normal"
    if hint in SCENES:
        return hint, detail if detail in ("short", "normal", "detailed") else ("short" if hint in ("casual", "banter") else "normal")
    if re.search(r"为什么|怎么|如何|什么|多少|哪个|吗[？?]?\s*$|[？?]", text):
        return "question", "normal"
    if re.search(r"(难受|好累|伤心|心情很差|撑不住|难过)", text):
        return "support", "normal"
    if re.search(r"(今晚|明天|几点).*(开黑|一起|记得|约)", text):
        return "coordination", "short"
    return ("banter", "short") if re.search(r"哈哈|笑死|乐了|绷不住", text) else ("casual", "short")


BASE_EXAMPLES = (
    ("casual", "今天终于忙完了", "终于下班啦"),
    ("casual", "刚吃完饭", "吃的啥"),
    ("banter", "这把我无敌了", "别立flag啊"),
    ("banter", "又白给了", "又来？"),
    ("support", "今天有点累", "早点歇着吧"),
    ("question", "这个说法是真的吗", "我不太确定，得看看具体依据"),
    ("technical", "能解释一下这个报错吗", "先确认是哪一步失败了，再看错误信息"),
    ("game", "这个英雄是不是很强", "得看版本和位置，不能光看一个数字"),
    ("correction", "我说的是之前那个版本", "哦，刚才理解错版本了"),
    ("coordination", "晚上有人来开黑吗", "看看还有几个人有空"),
)


def select_examples(settings, scene, text, native, cooldown=()):
    """Select complete user/assistant pairs, capped by one shared character budget."""
    if not settings["enabled"]:
        out=[]
        for i in range(0,len(native)-1,2):
            pair=native[i:i+2]
            if not any(legacy_reference(x.get("content","")) for x in pair):
                out.extend(pair)
        return out
    pool = [(s, a, b, 0) for s, a, b in BASE_EXAMPLES]
    pool.extend((x["scene"], x["user"], x["assistant"], 2) for x in settings["examples"])
    for i in range(0, len(native) - 1, 2):
        a, b = native[i:i+2]
        if a.get("role") == "user" and b.get("role") == "assistant":
            label, _ = scene_for(a["content"])
            pool.append((label, a["content"], b["content"], 1))
    ranked = sorted(pool, key=lambda e: (e[0] == scene, lexical(text, e[1]), e[3]), reverse=True)
    out, used, seen = [], 0, set()
    for label, user, assistant, _ in ranked:
        if label != scene or len(out) >= 2 * settings["examples_count"]:
            continue
        if legacy_reference(user+" "+assistant) or any(p.casefold() in assistant.casefold() for p in cooldown):
            continue
        user, assistant = clean(user, 350), clean(assistant, 350)
        # Configured scene examples are explicit style guidance. Built-in and
        # native examples need topic overlap so they do not become catchphrases.
        if _ != 2 and lexical(user, text) < 0.15:
            continue
        if not user or not assistant or assistant in seen or used + len(user) + len(assistant) + 24 > settings["examples_chars"]:
            continue
        seen.add(assistant)
        used += len(user) + len(assistant) + 24
        out.extend(({"role": "user", "content": user}, {"role": "assistant", "content": assistant}))
    return out


def scene_examples(settings, scene, text, native, cooldown=()):
    """Select positive pairs and labeled counterexamples under one budget."""
    positives = select_examples(settings, scene, text, native, cooldown)
    if not settings["enabled"]:
        return positives, []
    remaining = settings["examples_chars"] - sum(
        len(x["content"]) + 12 for x in positives
    )
    negatives = []
    candidates = sorted(
        (x for x in settings["negative_examples"] if x["scene"] == scene),
        key=lambda x: lexical(x["user"], text), reverse=True,
    )
    for x in candidates[:settings["negative_examples_count"]]:
        item = {"situation": clean(x["user"], 250),
                "avoid": clean(x["avoid"], 250), "prefer": clean(x["prefer"], 250)}
        if any(legacy_reference(value) for value in item.values()):
            continue
        if any(p.casefold() in item["prefer"].casefold() for p in cooldown):
            continue
        size = sum(len(v) for v in item.values()) + 60
        if size <= remaining:
            negatives.append(item)
            remaining -= size
    return positives, negatives


def style_rules(settings, scene, detail):
    if not settings["enabled"]:
        return ""
    common = persona_prompt(settings["style_prompt"])
    if scene in ("casual", "banter") and detail != "detailed":
        specific = "这是日常聊天，像群友即时回应，可半句、附和、带个人偏好，不用客套开场、文学旁白、总结或强行抛问题。"
    elif scene in ("game", "question", "technical", "correction"):
        specific = "本轮认真回答实际问题，直接讲结论与必要依据，准确使用单位和术语；如果我错了可以承认，不自圆其说，也不为文风故意省掉关键信息。"
    else:
        specific = "自然接话，按对方真实情绪与请求回应，不做戏剧化表演，不擅自许诺。"
    return "\n".join(x for x in (common, specific) if x)


def cooling_phrases(settings, rows, at=None):
    if not settings["enabled"] or not settings["phrase_control"]:
        return []
    at = now() if at is None else at
    texts = [str(x["text"]).casefold() for x in rows if x["bot"] and at - x["ts"] <= settings["phrase_cooldown_minutes"] * 60]
    texts = texts[-settings["phrase_window"]:]
    return [p for p in settings["phrases"] if sum(p.casefold() in t for t in texts) >= settings["phrase_limit"]][:8]


def baseline(at):
    h = datetime.fromtimestamp(at, CN_TZ).hour
    return 0.38 if h < 6 else 0.56 if h < 12 else 0.64 if h < 18 else 0.76


def energy_value(state, settings, at=None, *, spent=0):
    at = now() if at is None else at
    if not settings["enabled"] or not settings["energy_enabled"]:
        return 0.65
    base = baseline(at)
    elapsed = max(0, at - state.energy_at)
    prior = state.energy if state.energy_at else base
    value = min(0.95, max(0.15, base + (prior - base) * math.exp(-elapsed / 1800) - min(0.24, spent * 0.06)))
    state.energy, state.energy_at = value, at
    return value


def interested(settings, text):
    key = entity_key(text)
    return any((n := entity_key(x)) and len(n) >= 2 and n in key for x in settings["interests"])


def behavior(settings, scene, detail, energy, interest, cooldown):
    if not settings["enabled"]:
        return {}
    if scene == "technical" or detail == "detailed":
        length = "按问题完整解释，不硬压缩"
    elif detail == "short" or scene in ("casual", "banter"):
        length = "自然短反应，通常1至30字；无需强行追问" if energy >= 0.35 else "简短即可，可以少说"
    else:
        length = "先回答重点，通常数句；复杂问题按需详细"
    out = {"scene": scene, "length_hint": length, "topic_interest": "关注" if interest else "一般"}
    if settings["energy_enabled"]:
        out["social_energy"] = "低" if energy < 0.4 else "高" if energy > 0.72 else "正常"
    if cooldown:
        out["avoid_repetitive_fillers"] = cooldown
    return out


def light_filter(text, settings, scene, cooldown=()):
    """Only strip redundant assistant boilerplate in low-stakes casual replies."""
    if not settings["enabled"] or not settings["light_filter"] or scene not in ("casual", "banter"):
        return text, False
    if any(x in text for x in (chr(96) * 3, '「', '“', '"', '\n', 'http')):
        return text, False
    original = text.strip()
    value = re.sub(r"^(?:好的|当然|没问题)[，,！! ]*(?:我来帮你|让我帮你)[：:，, ]*", "", original)
    value = re.sub(r"[。！!\s]*希望(?:这|以上)?(?:对你|能对你)有帮助[。！!\s]*$", "", value)
    value = re.sub(r"^大肥鱼[：:]\s*", "", value)
    for filler in ("确实", "笑死", "啊这", "嗯嗯", "好的"):
        if filler in cooldown:
            shortened = re.sub(r"^"+re.escape(filler)+r"[，,！!]\s*", "", value, count=1)
            if shortened != value:
                value = shortened
                break
    # Never turn an intended reply into silence just because it was formulaic.
    return (value, True) if value.strip() and value != original else (original, False)
