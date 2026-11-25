"""Normalize official Dota descriptions without inventing hidden game rules."""
from __future__ import annotations

import math
import re

from .util import dump, entity_key, html_text


PATCH = re.compile(r"^[0-9]{1,2}\.[0-9]{1,3}[a-z]{0,2}$")
CODE = re.compile(r"^[a-zA-Z0-9_]{1,120}$")
NUM_FIELDS = {
    "cooldowns": ("冷却秒", "AbilityCooldown"),
    "mana_costs": ("魔法消耗", "AbilityManaCost"),
    "health_costs": ("生命消耗", "AbilityHealthCost"),
    "gold_costs": ("金币消耗", "AbilityGoldCost"),
    "cast_ranges": ("施法距离", "AbilityCastRange"),
    "cast_points": ("施法前摇秒", "AbilityCastPoint"),
    "channel_times": ("持续施法秒", "AbilityChannelTime"),
    "damages": ("伤害", "AbilityDamage"),
    "durations": ("持续秒", "AbilityDuration"),
}
NUM_UNITS = {
    "cooldowns": "秒", "cast_points": "秒", "channel_times": "秒", "durations": "秒",
    "mana_costs": "魔法值", "health_costs": "生命值", "gold_costs": "金币",
    "cast_ranges": "Dota 距离单位", "damages": "伤害点",
}
DAMAGE = {1: "physical", 2: "magical", 4: "pure", 8: "hp_removal"}
IMMUNITY = {1: "ally_yes", 2: "ally_no", 3: "enemy_yes", 4: "enemy_no", 5: "ally_yes_enemy_no"}
DISPELLABLE = {1: "strong_only", 2: "basic", 3: "not_dispellable"}
TEAMS = {1: "ally", 2: "enemy", 3: "both", 4: "custom"}
BEHAVIOR = {
    1: "hidden", 2: "passive", 4: "no_target", 8: "unit_target",
    16: "point", 32: "aoe", 64: "not_learnable", 128: "channelled",
}
# Code-keyed, scoped to the specific hero's actual returned abilities.
BUILTIN_ALIASES = {
    "antimage_mana_break": ["烧蓝", "削蓝", "消魔"],
    "antimage_blink": ["blink", "跳", "闪现"],
    "antimage_counterspell": ["反弹", "法术盾", "反制"],
    "antimage_mana_void": ["炸蓝", "法力炸弹"],
    "axe_berserkers_call": ["吼", "嘲讽", "斧王吼"],
    "axe_counter_helix": ["转", "反击螺旋"],
    "axe_culling_blade": ["斩杀", "淘汰之刃"],
    "pudge_meat_hook": ["钩", "钩子", "肉钩"],
    "pudge_rot": ["毒", "腐烂"],
    "pudge_dismember": ["咬", "肢解"],
    "lion_impale": ["穿刺", "插"],
    "lion_voodoo": ["羊", "羊人", "变羊"],
    "lion_finger_of_death": ["一指", "死亡一指", "大"],
    "void_spirit_aether_remnant": ["残阴", "拉", "魂"],
    "void_spirit_dissimilate": ["异化", "遁入虚无"],
    "void_spirit_resonant_pulse": ["盾", "脉冲"],
    "void_spirit_astral_step": ["太虚之径", "大招位移"],
    "faceless_void_time_walk": ["跳", "时间漫游"],
    "faceless_void_chronosphere": ["罩子", "大罩", "时间结界"],
    "legion_commander_duel": ["决斗"],
    "legion_commander_press_the_attack": ["强攻"],
    "phantom_assassin_stifling_dagger": ["飞镖", "匕首"],
    "phantom_assassin_coup_de_grace": ["暴击", "恩赐解脱"],
    "magnataur_reverse_polarity": ["猛犸大", "两极反转"],
    "enigma_black_hole": ["黑洞"],
    "earthshaker_echo_slam": ["回音击", "跳大"],
    "silencer_global_silence": ["全场沉默", "全局沉默"],
    "abaddon_aphotic_shield": ["盾", "无光之盾"],
    "oracle_false_promise": ["大招保人", "虚妄之诺"],
}

SLOTS = {}
for letter, n, cn in zip("QWER", "1234", "一二三四"):
    for alias in (letter, n, cn, n+"技能", cn+"技能", "第"+cn+"个技能",
                  "第"+n+"个技能", "技能"+n, "技能"+cn, cn+"技", n+"号技能", cn+"号技能"):
        SLOTS[entity_key(alias)] = letter
for alias in ("大招", "终极技能", "ult", "ultimate", "大"):
    SLOTS[entity_key(alias)] = "R"
SLOTS.update(d="D", f="F")

# Ordinary Q/W/E order inference is explicitly not used for these multi-form,
# spell-swapping, summon-control or multi-button heroes.
COMPLEX_SLOTS = {
    "invoker", "kez", "largo", "rubick", "morphling", "lone_druid",
    "troll_warlord", "meepo", "chen", "doom_bringer", "techies",
    "tinker", "brewmaster", "earth_spirit", "phoenix", "visage",
    "elder_titan", "monkey_king", "spectre", "lycan", "beastmaster",
}


def numeric_list(value):
    if isinstance(value, list) and all(
        type(x) in (float, int) and math.isfinite(x) for x in value
    ):
        return value
    return []


def show_values(values):
    return " / ".join(str(x) if type(x) is int else format(x, ".12g") for x in values)


def enum(row, key, mapping):
    value = row.get(key)
    return mapping.get(value) if type(value) is int else None


def flags(row):
    value = row.get("behavior")
    try:
        mask = int(value) if type(value) in (str, int) else 0
        if not 0 <= mask < (1 << 64):
            return []
        return [v for k, v in BEHAVIOR.items() if mask & k]
    except (ValueError, TypeError):
        return []


def ability_type(row):
    if row.get("ability_is_innate") is True:
        return "innate"
    if row.get("ability_is_granted_by_scepter") is True:
        return "scepter_granted"
    if row.get("ability_is_granted_by_shard") is True:
        return "shard_granted"
    return {0: "basic", 1: "ultimate", 2: "attribute", 3: "hidden"}.get(row.get("type"), "unknown")


def named_abilities(hero):
    # The provider's base ability array is the supported phase-one slot
    # catalogue. Hidden/empty internal definitions remain in raw snapshots.
    return [
        a for a in hero.get("abilities", []) if isinstance(a, dict)
        and a.get("name_loc") and isinstance(a.get("name"), str)
        and CODE.fullmatch(a["name"]) and not a["name"].startswith(("dota_empty", "generic_hidden"))
    ]


def with_english_names(hero, index):
    if not index:
        return hero
    enriched=[]
    for a in hero.get("abilities",[]):
        entry=index.get(str(a.get("id"))) if isinstance(a,dict) else None
        if entry and entry["code"]==a.get("name"):
            enriched.append({**a,"name_english_loc":entry["name"]})
        else:
            enriched.append(a)
    return {**hero,"abilities":enriched}


def slot_map(hero, cfg):
    all_abilities = named_abilities(hero)
    code = str(hero.get("name", ""))
    bare = code.removeprefix("npc_dota_hero_")
    base = [
        a for a in all_abilities if ability_type(a) in ("basic", "ultimate")
        and not {"hidden", "not_learnable"} & set(flags(a))
        and a.get("max_level") != 0
    ]
    basics = [a["name"] for a in base if ability_type(a) == "basic"]
    ults = [a["name"] for a in base if ability_type(a) == "ultimate"]
    out = {}
    if len(ults) == 1:
        out["R"] = (ults[0], "官方唯一终极技能类型")
    if len(basics) == 3 and len(ults) == 1 and bare not in COMPLEX_SLOTS:
        out.update({k: (v, "按官方普通技能顺序推定") for k, v in zip("QWE", basics)})
    codes = {a["name"] for a in all_abilities}
    configured = cfg.get("skill_slots", {}).get(code, cfg.get("skill_slots", {}).get(bare, {}))
    out.update({k: (v, "管理员核实") for k, v in configured.items() if v in codes})
    return out


def skill_names(row, cfg):
    code = row["name"]
    return [
        row.get("name_loc"), row.get("name_english_loc"), code,
        code.split("_", 1)[-1].replace("_", " "),
        *BUILTIN_ALIASES.get(code, []), *cfg.get("skill_aliases", {}).get(code, []),
    ]


def resolve_skill(hero, query, cfg):
    abilities = named_abilities(hero)
    key = entity_key(query)
    if key in SLOTS:
        slot = SLOTS[key]
        mapped = slot_map(hero, cfg).get(slot)
        if mapped:
            return [a for a in abilities if a["name"] == mapped[0]], mapped[1]
        # Multiple ultimate forms should be visible, not a made-up single R.
        if slot == "R":
            matches = [a for a in abilities if ability_type(a) == "ultimate"]
            if matches:
                return matches, "多个终极技能定义，请指定名称"
        return [], "槽位无法从官方目录唯一确定，请指定技能名称"
    exact = [a for a in abilities if key and key in {entity_key(x) for x in skill_names(a, cfg)}]
    if exact:
        return exact, "名称/别名"
    partial = [a for a in abilities if len(key) >= 2 and
        any(key in entity_key(x) for x in skill_names(a, cfg)
            if len(entity_key(x)) >= 2)]
    return partial, "名称片段"


def specials_of(row):
    return [s for s in row.get("special_values", []) or []
            if isinstance(s, dict) and isinstance(s.get("name"), str)
            and CODE.fullmatch(s["name"])]


def percent_hint(s, text):
    key = s["name"]
    if s.get("is_percentage") is True:
        return True, "官方百分比标记"
    if "%" in html_text(s.get("heading_loc")) or re.search(
        "%"+re.escape(key)+r"%{2,}", text, re.I
    ):
        return True, "官方显示文本"
    if re.search(r"(?:pct|percent|percentage)", key, re.I):
        return True, "字段名推定"
    return False, None


def render_numbers(row, view="full"):
    """Keep level arrays and raw modifier values; never add bonuses by guess."""
    specials, replacements, stats, units = [], {}, {}, {}
    descriptions = " ".join(str(row.get(k) or "") for k in
                            ("desc_loc", "notes_loc", "shard_loc", "scepter_loc"))
    special_lookup = {}
    for s in specials_of(row):
        key = s["name"]
        vals = numeric_list(s.get("values_float")) or numeric_list(s.get("values_int"))
        label = html_text(s.get("heading_loc") or key)
        percent, hint = percent_hint(s, descriptions)
        entry = {"key": key, "label": label, "values": vals, "percent": percent}
        if hint and hint != "官方百分比标记":
            entry["percent_source"] = hint
        for raw, name in (("values_shard", "shard_values"), ("values_scepter", "scepter_values")):
            values = numeric_list(s.get(raw))
            if values:
                entry[name] = values
        bonus = [
            {"talent": b["name"], "value": b["value"], "operation": b.get("operation")}
            for b in s.get("bonuses", []) or []
            if isinstance(b, dict) and isinstance(b.get("name"), str)
            and type(b.get("value")) in (int, float) and math.isfinite(b["value"])
        ]
        if bonus:
            entry["talent_modifiers"] = bonus
        if s.get("required_facet"):
            entry["required_facet"] = s["required_facet"]
        facet = s.get("facet_bonus")
        if isinstance(facet, dict) and numeric_list(facet.get("values")):
            entry["facet_modifier"] = {k: facet[k] for k in ("name", "values", "operation") if k in facet}
        if vals or any(k in entry for k in ("shard_values", "scepter_values", "talent_modifiers", "facet_modifier")):
            specials.append(entry)
        if vals:
            replacements[key.casefold()] = show_values(vals)
        special_lookup[key.casefold()] = entry
    for field, (label, sub) in NUM_FIELDS.items():
        val = numeric_list(row.get(field))
        # Zero placeholders occur throughout the datafeed; preserve explicit
        # nonzero arrays, and cooldown/mana zero for the numeric view.
        if val and (any(val) or view == "numbers" and field in ("cooldowns", "mana_costs")):
            stats[label] = val
            units[label] = NUM_UNITS[field]
        if val:
            replacements.setdefault(sub.casefold(), show_values(val))

    def desc(text, upgrade=None):
        def replace(m):
            key = m[1].casefold()
            entry = special_lookup.get(key)
            if upgrade and entry and entry.get(upgrade+"_values"):
                value = show_values(entry[upgrade+"_values"])
                base = entry["values"]
                if base and any(base):
                    return "（基础"+show_values(base)+"；升级原始字段"+value+"）"
                return value+"（升级原始字段）"
            return replacements.get(key, "（数值未提供:"+m[1]+"）")
        # The Valve template uses %% for a literal percent sign.
        text = re.sub(r"%([A-Za-z0-9_]+)%", replace, str(text or ""))
        return html_text(text.replace("%%", "%"))

    out = {"name": html_text(row.get("name_loc"))}
    if row.get("name_english_loc"):
        out["english"]=html_text(row["name_english_loc"])
    if "id" in row:
        out["id"] = row["id"]
    if row.get("name"):
        out["code"] = row["name"]
    out.update(type=ability_type(row))
    types = {
        "damage_type": enum(row, "damage", DAMAGE),
        "debuff_immunity": enum(row, "immunity", IMMUNITY),
        "dispellability": enum(row, "dispellable", DISPELLABLE),
        "target_team": enum(row, "target_team", TEAMS),
    }
    out.update({k: v for k, v in types.items() if v})
    behavior = flags(row)
    if behavior:
        out["behavior"] = behavior
    if type(row.get("max_level")) is int:
        out["max_level"] = row["max_level"]
    if view in ("full", "effects"):
        out["description"] = desc(row.get("desc_loc"))
        if not out["description"]:
            out["description_notice"] = "官方没有提供效果正文，仅可依据其他已知字段，不补造技能机制。"
        notes = [desc(x) for x in row.get("notes_loc", []) or [] if isinstance(x, str) and x]
        if notes:
            out["notes"] = notes
    if view in ("full", "numbers"):
        out["stats"] = stats
        if units:
            out["stats_units"] = units
        out["special_values"] = specials
    if view == "numbers":
        enum_fields = ("damage", "immunity", "dispellable", "target_team", "type")
        out["raw_enums"] = {key: row[key] for key in enum_fields if type(row.get(key)) is int}
    if view in ("full", "effects", "upgrades"):
        for field, label, mode in (("shard_loc", "魔晶", "shard"), ("scepter_loc", "神杖", "scepter")):
            if row.get(field):
                out[label] = desc(row[field], mode)
        upgrades = {k: True for k in ("ability_has_scepter", "ability_has_shard",
                                      "ability_is_granted_by_scepter", "ability_is_granted_by_shard")
                    if row.get(k) is True}
        if upgrades:
            out["upgrades"] = upgrades
        if view == "upgrades":
            out["special_values"] = [
                s for s in specials if any(k in s for k in ("shard_values", "scepter_values", "talent_modifiers", "facet_modifier"))
            ]
    if any(any(k in s for k in ("shard_values", "scepter_values", "talent_modifiers", "facet_modifier")) for s in specials) and view != "effects":
        out["modifier_note"] = "升级/天赋字段为官方原值，未假设操作码或直接相加。"
    if view in ("full", "numbers") and any(row.get(k) for k in ("behavior", "target_type", "flags")):
        out["raw_flags"] = {k: row[k] for k in ("behavior", "target_type", "flags") if k in row}
    return out


def talents_of(hero):
    """Resolve only uniquely supported talent placeholders from linked bonuses."""
    bonus = {}
    for ability in named_abilities(hero):
        for s in specials_of(ability):
            for b in s.get("bonuses", []) or []:
                if isinstance(b, dict) and isinstance(b.get("name"), str) and type(b.get("value")) in (int, float) and math.isfinite(b["value"]):
                    bonus.setdefault((b["name"], ("bonus_"+s["name"]).casefold()), set()).add(b["value"])
    talents = [t for t in hero.get("talents", []) or [] if isinstance(t, dict) and t.get("name_loc")]
    result = []
    for i, t in enumerate(talents):
        values = {s["name"].casefold(): show_values(numeric_list(s.get("values_float")) or numeric_list(s.get("values_int")))
                  for s in specials_of(t)}
        missing = []
        def replace(m):
            key = m[1].casefold()
            direct = values.get(key)
            linked = bonus.get((t.get("name"), key), set())
            if direct:
                return direct
            if len(linked) == 1:
                return show_values(list(linked))
            missing.append(m[1])
            return "（数值未核实）"
        title = re.sub(r"\{s:([A-Za-z0-9_]+)\}", replace, str(t["name_loc"]))
        entry = {"name": html_text(title), "code": t.get("name")}
        if "id" in t:
            entry["id"] = t["id"]
        if len(talents) == 8:
            entry["level"] = (10, 15, 20, 25)[i//2]
        if missing:
            entry["unresolved"] = missing
        result.append(entry)
    return result


ATTRIBUTES = (
    "str_base", "str_gain", "agi_base", "agi_gain", "int_base", "int_gain",
    "primary_attr", "complexity", "attack_capability", "damage_min", "damage_max",
    "attack_min", "attack_max", "attack_rate", "attack_range", "projectile_speed",
    "movement_speed", "turn_rate", "armor", "magic_resistance",
    "sight_range_day", "sight_range_night", "max_health", "health_regen",
    "max_mana", "mana_regen",
)


def ability_summary(hero, cfg):
    slots = slot_map(hero, cfg)
    entries = []
    for a in named_abilities(hero):
        entry = {"name": html_text(a["name_loc"]), "code": a["name"], "type": ability_type(a)}
        if a.get("name_english_loc"):
            entry["english"]=html_text(a["name_english_loc"])
        if "id" in a:
            entry["id"] = a["id"]
        for slot, (code, origin) in slots.items():
            if code == a["name"]:
                entry.update(slot=slot, slot_source=origin)
                break
        if a.get("ability_has_scepter"):
            entry["scepter"] = True
        if a.get("ability_has_shard"):
            entry["shard"] = True
        entries.append(entry)
    return entries


def hero_result(hero, cfg, skill="", section="summary", view="full", offset=0, limit=15):
    summary = ability_summary(hero, cfg)
    key = entity_key(skill)
    sections = {
        "天赋":"talents", "天赋树":"talents", "先天":"innate", "先天技能":"innate",
        "神杖":"scepter", "a杖":"scepter", "a杖技能":"scepter",
        "魔晶":"shard", "魔晶技能":"shard", "技能":"abilities", "全部技能":"abilities",
    }
    if key in sections:
        section, skill = sections[key], ""
    chinese_levels={"十级天赋":"10级天赋","十五级天赋":"15级天赋",
                    "二十级天赋":"20级天赋","二十五级天赋":"25级天赋"}
    level = re.fullmatch(r"(10|15|20|25)(?:级)?(?:天赋)?", chinese_levels.get(key,key))
    if level:
        section, skill = "talents", ""
    if skill:
        matches, how = resolve_skill(hero, skill, cfg)
        if len(matches) == 1:
            selected = matches[0]
            details = render_numbers(selected, view)
            declared = BUILTIN_ALIASES.get(selected["name"], []) + cfg.get("skill_aliases", {}).get(selected["name"], [])
            if declared:
                details["aliases"] = list(dict.fromkeys(declared))[:12]
            return {"ability": details, "match": how}
        candidates = summary if not matches else [a for a in summary if a["code"] in {m["name"] for m in matches}]
        return {"error": "技能名称/槽位不唯一或未找到", "match": how, "abilities": candidates}
    if section == "talents":
        result = talents_of(hero)
        if level:
            result = [t for t in result if t.get("level") == int(level[1])]
        return {"talents": result[offset:offset+limit], "offset":offset, "total":len(result),
                "next_offset":offset+limit if offset+limit<len(result) else None,
                "talent_note": "仅八项标准目录按官方顺序推定10/15/20/25级；缺少数值不猜测。"}
    if section in ("scepter", "shard", "innate", "abilities"):
        selected = named_abilities(hero)
        if section == "innate":
            selected = [a for a in selected if ability_type(a) == "innate"]
        elif section in ("scepter", "shard"):
            selected = [a for a in selected if a.get("ability_has_"+section)
                        or a.get("ability_is_granted_by_"+section) or a.get(section+"_loc")]
            view = "upgrades" if view == "full" else view
            # A granted ability's description is its actual effect.
        records = []
        for a in selected[offset:offset+limit]:
            used_view = "full" if section in ("scepter", "shard") and ability_type(a) == section+"_granted" else view
            r = render_numbers(a, used_view)
            records.append(r)
        return {"section": section, "abilities": records, "total": len(selected), "offset":offset,
                "next_offset":offset+limit if offset+limit<len(selected) else None}
    return {
        "attributes": {k: hero[k] for k in ATTRIBUTES if hero.get(k) is not None},
        "abilities": summary, "talents": talents_of(hero),
        "summary": html_text(hero.get("hype_loc")), "slot_note": "普通QWE按目录推定；复杂英雄请用技能名。"
    }


def fit_knowledge_payload(data, maximum):
    """Return valid bounded JSON, without slicing a numeric record or a note."""
    if len(dump(data)) <= maximum:
        return data
    out = dict(data, truncated=True)
    omitted = []
    # Remove optional complete sections before splitting any result rows.
    for key in ("summary", "attributes", "talents", "general", "slot_note"):
        if key in out and (key != "talents" or "abilities" in out) and len(dump(out)) > maximum:
            del out[key]
            omitted.append(key)
    for key in ("entries", "matches", "changes", "abilities", "talents", "items", "heroes", "candidates"):
        rows = out.get(key)
        if isinstance(rows, list):
            out[key] = list(rows)
            while len(out[key]) > 1 and len(dump(out)) + 140 > maximum:
                out[key].pop()
            if len(out[key]) != len(rows):
                omitted.append(key)
                if "offset" in out and key in ("matches","changes","abilities","talents"):
                    out["next_offset"] = out["offset"] + len(out[key])
    for key in ("ability", "effects"):
        if isinstance(out.get(key), dict) and len(dump(out)) + 160 > maximum:
            record = dict(out[key])
            for optional in ("raw_flags", "raw_enums", "aliases", "notes", "special_values", "stats_units", "stats", "modifier_note"):
                if optional in record and len(dump({**out, key:record})) + 160 > maximum:
                    del record[optional]
                    omitted.append(key+"."+optional)
            out[key] = record
    out["omitted"] = omitted
    out["budget_note"] = "结果预算限制，必要时按单个技能/物品或 view、section、offset 缩小查询；未显示不代表不存在。"
    if len(dump(out)) <= maximum:
        return out
    return {"patch": data.get("patch") or data.get("catalog_patch"), "source": data.get("source"),
            "error": "完整记录超过工具字符预算，请缩小查询或提高 tool_result_chars。"}


# Text matches are evidence candidates, not a complete rules engine. Do not
# confuse a debuff that can be dispelled with a spell that performs dispel.
EFFECTS = {
    "strong_dispel": r"强驱散|强力驱散|strong dispel",
    "basic_dispel": r"弱驱散|基础驱散|基本驱散|basic dispel",
    "stun": r"眩晕|击晕|\bstun(?:s|ned|ning)?\b",
    "silence": r"沉默|\bsilence(?:s|d)?\b",
    "root": r"缠绕|定身|\broot(?:s|ed)?\b",
    "heal": r"治疗|回复.{0,8}生命|恢复.{0,8}生命|\bheal(?:s|ing)?\b",
    "disarm": r"缴械|\bdisarm(?:s|ed)?\b",
    "break": r"破坏被动|禁用被动|施加破坏|造成破坏|\bappl(?:y|ies) break\b",
    "invisibility": r"隐身|隐形|\binvisib",
    "teleport": r"传送|闪烁|\bteleport|\bblink",
    "slow": r"减速|降低.{0,12}移动速度|\bslow(?:s|ed)?\b",
    "max_health_damage": r"(?:最大生命值|最大生命).{0,20}(?:伤害)|伤害.{0,20}最大生命值|max(?:imum)? health.{0,24}damage|damage.{0,24}max(?:imum)? health",
}


def effect_evidence(row, effect, include_upgrades=False):
    if effect not in EFFECTS:
        return None
    sources = [("base", row.get("desc_loc"))]
    sources.extend(("note", n) for n in row.get("notes_loc", []) or [] if isinstance(n, str))
    if include_upgrades:
        sources.extend([("shard", row.get("shard_loc")), ("scepter", row.get("scepter_loc"))])
    for origin, text in sources:
        for sentence in re.split(r"[\n。；！?！？]|(?<=\.)\s", html_text(text)):
            match = re.search(EFFECTS[effect], sentence, re.I)
            if not match:
                continue
            # Conservative exclusions: no, removal, prevention, immunity,
            # receiving/enduring an effect or listed dispellability.
            if effect.endswith("_dispel"):
                # The game includes an explicit footer marking spells that
                # perform dispel, separate from the dispellability enum.
                tagged = bool(re.search(r"(?:驱散类型|dispel type)\s*[:：]\s*(?:"+EFFECTS[effect]+r")",sentence,re.I))
                if not tagged and (re.search(r"无法|不能|不会|免疫|需要|只能被|被驱散|可驱散|可被|可用.*驱散|cannot|only|requires?|can be dispelled|immune", sentence,re.I)
                    or not re.search(r"施加|施放|获得|进行|执行|驱散.{0,8}(?:负面|正面|效果|状态|减益|增益)|appl(?:y|ies)|perform|provides?|receives?",sentence,re.I)):
                    continue
            elif re.search(r"不(?:会|能|再|受)|无法|免疫|防止|解除|移除|抵消|结束|取消|被驱散|可驱散|不能被|不可以|cannot|not |no longer|immune|prevent|remov(?:e|ed|es)|can be dispelled", sentence,re.I):
                continue
            lo = max(0, match.start()-65)
            return {"origin": origin, "text": sentence[lo:lo+180], "quality": "text_candidate"}
    return None


def filter_mechanic(row, filters):
    checks = {
        "damage_type": enum(row, "damage", DAMAGE),
        "ability_type": ability_type(row),
        "dispellability": enum(row, "dispellable", DISPELLABLE),
        "target_team": enum(row, "target_team", TEAMS),
    }
    for key, expected in checks.items():
        if key in filters and filters[key] != expected:
            return None
    if "behavior" in filters and filters["behavior"] not in flags(row):
        return None
    if "pierces_debuff_immunity" in filters:
        immunity = enum(row, "immunity", IMMUNITY)
        if immunity not in ("enemy_yes", "enemy_no", "ally_yes_enemy_no") or filters["pierces_debuff_immunity"] != (immunity == "enemy_yes"):
            return None
    numeric = {}
    for field, key, op in (("cooldowns","cooldown_max","max"), ("cast_ranges","cast_range_min","min")):
        if key not in filters:
            continue
        values = numeric_list(row.get(field))
        # Zero-filled passive/default fields aren't a valid numeric measurement.
        if not values or not any(values) or (field=="cooldowns" and "passive" in flags(row)):
            return None
        val = values[-1]  # final provided base level, not arbitrary min/max
        if (op == "min" and val < filters[key]) or (op == "max" and val > filters[key]):
            return None
        numeric[field] = values
    evidence = effect_evidence(row, filters["effect"], filters.get("include_upgrades", False)) if filters.get("effect") else None
    if filters.get("effect") and not evidence:
        return None
    result = {"name": html_text(row.get("name_loc")), "code": row.get("name"), "id": row.get("id"),
              "type": ability_type(row), **{k:v for k,v in checks.items() if v and k != "ability_type"}}
    if "hidden" in flags(row):
        result["catalog_hidden"] = True
    if "pierces_debuff_immunity" in filters:
        result["debuff_immunity"] = enum(row, "immunity", IMMUNITY)
    if "behavior" in filters:
        result["behavior"] = flags(row)
    if numeric:
        result["values"] = numeric
    if evidence:
        result["evidence"] = evidence
    return result
