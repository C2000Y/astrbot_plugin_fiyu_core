"""Consent-based, coarse impressions of self-disclosed interests and chat habits."""
from __future__ import annotations

import re
from collections import Counter
from datetime import date, timedelta

from .util import clean, day_key, entity_key, now, number, parse_time, unsafe_secret

HABIT_PROMPT = """
额外输出 habits 数组（最多8条）仅针对资料中 habit_eligible 列出的本人。只从本人主动分享的非敏感活动提取候选，不保存地址、通勤、工作/上下班、到家时间、健康等可用于推断现实行踪的细节，不根据发消息时刻判断他实际做了什么。
格式：{"habits":[{"subject":"U...","category":"game|food|hobby|chat","activity":"本人明确提到的具体游戏/食物/爱好，或 chat 时固定为日常分享","period":"any|morning|afternoon|evening|night|weekend","signal":"support|stop","confidence":0.9,"source_ids":[123]}]}。
game/food/hobby 的活动名称必须出现在本人原话；period 仅本人明确说的休闲时段，没说则 any。chat 只表示在群内分享日常的聊天行为，不能当成真实到家/下班时刻；程序用粗略消息时段。stop 仅本人明确说不再有该习惯。不要从转述、玩笑、推测提取。程序合并不同日期；不需要本批直接断言已经形成习惯。
"""
CATEGORIES = {"game", "food", "hobby", "chat"}
PERIODS = {"any": "", "morning": "早间", "afternoon": "下午", "evening": "晚间", "night": "深夜", "weekend": "周末"}
SENSITIVE_ROUTINES = re.compile(
    r"住址|地址|门牌|车牌|定位|路线|通勤|下班|上班|到家|回家|出门|离家|到公司|到校|"
    r"公司|学校|小区|宿舍|医院|酒店|地铁|车站|班车|(?:凌晨|早上|晚上|下午)?\d{1,2}(?:[:：]\d{2}|点|时)|"
    r"宗教|政治|疾病|病史|住院|怀孕|性取向|密码|银行卡|身份证|手机号码|坐标",
    re.I,
)
ALIASES = {
    "dota2": ("dota2", "dota", "刀塔"),
    "cs2": ("cs2", "csgo", "反恐精英"),
}


def safe_activity(value):
    return (
        isinstance(value, str) and bool(value.strip()) and len(value) <= 32
        and not unsafe_secret(value) and not SENSITIVE_ROUTINES.search(value)
        and not re.search(r"https?://|[@<>/\\\n\r]", value)
        and len(entity_key(value)) >= 1
    )


def coarse_period(ts):
    from datetime import datetime
    from .util import CN_TZ
    h = datetime.fromtimestamp(ts, CN_TZ).hour
    return "morning" if 6 <= h < 12 else "afternoon" if 12 <= h < 18 else "evening" if 18 <= h < 24 else "night"


def normalize_habits(data, rows, people, cfg):
    if not isinstance(data, dict) or not isinstance(data.get("habits"), list) or not cfg["habits"]["enabled"]:
        return []
    settings = cfg["habits"]
    eligible = {p["ref"]: p for p in people if p.get("habit_opt_in") and not p["blocked"]
                and p["uid"] not in cfg["access"]["blocked_users"]}
    actual = {r["seq"]: r for r in rows if not r["bot"]}
    out = []
    for item in data["habits"][:settings["max_per_batch"]]:
        if not isinstance(item, dict):
            continue
        ref, category = item.get("subject"), item.get("category")
        activity, period, signal = item.get("activity"), item.get("period"), item.get("signal")
        ids = item.get("source_ids")
        if (not isinstance(ref, str) or ref not in eligible or not isinstance(category,str) or category not in CATEGORIES
            or not isinstance(period,str) or period not in PERIODS or signal not in ("support", "stop")
            or not safe_activity(activity) or not isinstance(ids,list) or not ids or len(ids)>6
            or any(type(i) is not int or i not in actual or actual[i]["ref"] != ref for i in ids)):
            continue
        confidence = number(item.get("confidence"), 0, 0, 1)
        if confidence < max(0.80, cfg["memory"]["minimum_confidence"]):
            continue
        activity = clean(activity, 32)
        key = entity_key(activity)
        if category == "chat":
            activity, key = "日常分享", "日常分享"
        elif category == "game":
            for canon, names in ALIASES.items():
                if key in names:
                    key = canon
                    activity = "Dota 2" if canon == "dota2" else "CS2"
                    break
        for seq in dict.fromkeys(ids):
            row = actual[seq]
            if row["ts"] < eligible[ref]["habit_since"]:
                continue
            text = row["text"]
            # No extrapolation from absence, third-party reports or forwarded data.
            if re.search(r"听说|据说|转发|他说|她说|别人说|假设|要是|如果", text):
                continue
            if category == "chat":
                # This is a chat pattern, not an assertion about physical activity.
                if signal != "support" or not re.search(r"今天|刚刚|刚|昨天|这两天|最近|日常", text):
                    continue
                effective_period = coarse_period(row["ts"])
            else:
                if unsafe_secret(text) or SENSITIVE_ROUTINES.search(text):
                    continue
                candidates = ALIASES.get(key, (key,))
                if not any(n in entity_key(text) for n in candidates):
                    continue
                if signal == "stop" and not re.search(r"不再|不(?:玩|吃|看|打).{0,25}(?:了|啦)|戒掉|不喜欢|以后不", text):
                    continue
                # Only periods explicitly supported by the message, never derived from timestamps.
                phrases = {
                    "morning": r"早上|上午|早晨",
                    "afternoon": r"下午",
                    "evening": r"晚上|傍晚|晚饭后|晚间",
                    "night": r"深夜|半夜|凌晨",
                    "weekend": r"周末|星期六|星期天|周六|周日",
                }
                effective_period = period if period in phrases and re.search(phrases[period], text) else "any"
                if category == "food":
                    effective_period = "any"
            out.append(dict(ref=ref, topic_key=category+":"+key, category=category,
                            activity=activity, period=effective_period, signal=signal,
                            source_seq=seq, day=day_key(row["ts"]), confidence=confidence))
    return out[:settings["max_per_batch"]*3]


def rollup(store, db, observations, settings):
    """One vote per person/topic/day; expiry reflects evidence, never recall."""
    if not observations or not settings or not settings["enabled"]:
        return []
    cutoff = (date.fromisoformat(day_key())-timedelta(days=settings["window_days"]-1)).isoformat()
    touched = set()
    for o in observations:
        row = db.execute("""SELECT m.scope,m.ref,m.ts,p.habit_opt_in,p.habit_since,p.blocked,s.kind
           FROM messages m JOIN people p ON p.scope=m.scope AND p.ref=m.ref
           JOIN sessions s ON s.scope=m.scope WHERE m.seq=? AND m.bot=0""", (o["source_seq"],)).fetchone()
        if (not row or row["kind"]!="group" or row["ref"]!=o["ref"] or not row["habit_opt_in"] or row["blocked"]
            or row["ts"]<row["habit_since"] or o["day"]!=day_key(row["ts"]) or o["day"]<cutoff):
            continue
        scope, ref, topic = row["scope"], row["ref"], o["topic_key"]
        # Bound storage per person; only learned candidate keys, not arbitrary activity timelines.
        existing = db.execute("SELECT COUNT(DISTINCT topic_key) FROM habit_observations WHERE scope=? AND person_ref=?", (scope,ref)).fetchone()[0]
        if existing >= 80 and not db.execute("SELECT 1 FROM habit_observations WHERE scope=? AND person_ref=? AND topic_key=?", (scope,ref,topic)).fetchone():
            continue
        changed = db.execute("""INSERT INTO habit_observations(scope,person_ref,topic_key,activity,category,period,day,source_seq,signal,confidence)
          VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(scope,person_ref,topic_key,day) DO UPDATE SET
          activity=excluded.activity,period=excluded.period,source_seq=excluded.source_seq,signal=excluded.signal,confidence=excluded.confidence
          WHERE excluded.source_seq>habit_observations.source_seq""",
          (scope,ref,topic,o["activity"],o["category"],o["period"],o["day"],o["source_seq"],o["signal"],o["confidence"])).rowcount
        if changed:
            touched.add((scope,ref,topic))
    saved = []
    for scope, ref, key in touched:
        rows = [dict(r) for r in db.execute("""SELECT * FROM habit_observations
            WHERE scope=? AND person_ref=? AND topic_key=? AND day>=? ORDER BY day,source_seq""", (scope,ref,key,cutoff))]
        prior = db.execute("SELECT id,state,pinned FROM memories WHERE scope=? AND subject=? AND kind='habit' AND topic_key=?", (scope,ref,key)).fetchone()
        if not rows:
            continue
        last_stop = max((r["day"] for r in rows if r["signal"]=="stop"), default="")
        recent = [r for r in rows if r["signal"]=="support" and r["day"]>last_stop]
        if last_stop and len(recent)<settings["min_days"]:
            if prior:
                db.execute("UPDATE memories SET state='expired',valid_until=?,revision=revision+1 WHERE id=?", (now(),prior["id"]))
                db.execute("DELETE FROM vectors WHERE memory_id=?", (prior["id"],))
            continue
        if len(recent)<settings["min_days"]:
            continue
        last = recent[-1]
        until = (parse_time(last["day"]) or now())+(settings["stale_days"]+1)*86400
        if until<=now():
            continue
        counts = Counter(r["period"] for r in recent if r["period"]!="any")
        common, count = counts.most_common(1)[0] if counts else ("any",0)
        # A general interest can be stable even if the time of leisure varies.
        period = PERIODS[common] if count>=settings["min_days"] and count/len(recent)>=0.6 else ""
        activity, category = last["activity"], last["category"]
        if category=="chat":
            impression = f"常在{period}分享日常" if period else "经常在群里分享日常"
        elif category=="food":
            impression = f"似乎经常提及吃{activity}"
        else:
            impression = f"似乎常{'在'+period if period else ''}参与{activity}相关的休闲活动"
        title = "习惯印象："+activity
        full = f"据本人近期在本会话多次分享（{len(recent)}个不同日期），{impression}。仅为以往聊天印象，不代表当前情况。"
        p = dict(scope=scope,subject=ref,kind="habit",topic_key=key,title=title,full_text=full,
                 gist=f"{impression}（多日观察，非确定事实）",trace=title,importance=55,
                 confidence=min(0.95, sum(r["confidence"] for r in recent)/len(recent)),
                 participant_refs=[ref],valid_until=until,confirmed_update=True,created=parse_time(recent[0]["day"]) or now(),
                 source_ids=[r["source_seq"] for r in recent[-6:]],
                 evidence=[{"n":r["source_seq"],"speaker":ref,"text":"本人主动分享的非敏感活动（已归纳）"} for r in recent[-6:]])
        saved.append(store._insert_memory(db,p))
    return saved
