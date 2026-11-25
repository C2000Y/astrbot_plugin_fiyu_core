from __future__ import annotations

import asyncio
import json
import math
import os
import re
from datetime import date, timedelta
from pathlib import Path

import aiohttp

from .dota_details import (PATCH, ATTRIBUTES, NUM_FIELDS, ability_type, filter_mechanic,
                           hero_result, named_abilities, render_numbers, specials_of,
                           talents_of, with_english_names)
from .dota_knowledge import (KnowledgeStore, api_rows, latest_patch, patch_key,
                             patch_history, validate_detail, validate_roster)
from .dota_matches import DotaMatches, PATCHES
from .research_sources import HTTPFailure, PRO_LIST
from .util import (
    atomic_json,
    clean,
    day_key,
    dump,
    entity_key,
    html_text,
    now,
    read_json,
    stamp,
)

BASE = "https://www.dota2.com/datafeed/"
STATS_URL = "https://www.dota2.com/webapi/IDOTA2Plus/GetPlusHeroAllyAndEnemyData/v001"
PUBLIC_STATS_URL = "https://api.opendota.com/api/heroStats"
WEEKLY_STATS_URL = "https://www.dota2.com/webapi/IDOTA2Plus/GetPlusStatsData/v001"
HERO_LIST_URL = BASE + "herolist?language=schinese"
SOURCES = {"public": PUBLIC_STATS_URL, "weekly": WEEKLY_STATS_URL, "matchups": STATS_URL}
STATS_SYNC_INTERVAL = 900
STATS_MANUAL_INTERVAL = 60
STATS_FAILURES = {
    "timeout", "network_error", "http_error", "invalid_json", "invalid_structure",
    "roster_unavailable", "storage_error", "interrupted", "unknown",
}
BRACKETS = {
    1: "先锋 Herald",
    2: "卫士 Guardian",
    3: "中军 Crusader",
    4: "统帅 Archon",
    5: "传奇 Legend",
    6: "万古流芳 Ancient",
    7: "超凡入圣 Divine",
    8: "冠绝一世 Immortal",
}
MAX_BALANCE_ERROR_PP = 0.5
PUBLIC_SEGMENTS = ("pub", *(str(i) for i in BRACKETS))
ALIAS_FILE = Path(__file__).resolve().parents[1] / "assets" / "dota-aliases.json"
SNAP_RE = re.compile(r"^\d{8}\.json$")


class InvalidStatsFormat(ValueError):
    """Structural failure with a diagnostic containing only validated metadata."""


async def write_stats_json(path, value):
    # A cancelled task must not release its sync lock while the disk write is active.
    worker = asyncio.create_task(asyncio.to_thread(atomic_json, path, value))
    try:
        await asyncio.shield(worker)
    except asyncio.CancelledError:
        await asyncio.gather(worker, return_exceptions=True)
        raise


def require_int(value, low, high):
    if type(value) is not int or not low <= value <= high:
        raise ValueError("统计返回了无效整数")
    return value


def rows_at(payload, key):
    rows = payload.get("result", {}).get("data", {}).get(key)
    if not isinstance(rows, list):
        raise ValueError(f"Valve 缺少 {key} 数据")
    return rows


def key_of(row, hero=True):
    prefix = "npc_dota_hero_" if hero else "item_"
    name = str(row.get("name") or "")
    return name.removeprefix(prefix)


class AliasIndex:
    def __init__(self, rows: list[dict], aliases: dict, hero=True):
        self.by_id = {}
        self.by_key = {}
        self.lookup = {}
        for row in rows:
            if type(row.get("id")) is not int or row["id"] <= 0:
                continue
            key = key_of(row, hero)
            local = html_text(row.get("name_loc") or row.get("name_english_loc") or key)
            english = html_text(row.get("name_english_loc") or key.replace("_", " "))
            item = {**row, "display": local, "english": english, "key": key}
            self.by_id[row["id"]] = item
            self.by_key[key] = item
            names = [
                local,
                english,
                key,
                row.get("name"),
                str(row["id"]),
                *aliases.get(key, []),
            ]
            if not hero and key.startswith("enhancement_"):
                names.extend([local + "附魔", english + " Enchantment"])
            for val in names:
                n = entity_key(val)
                if n:
                    self.lookup.setdefault(n, set()).add(row["id"])
        if not hero:
            for key, item in self.by_key.items():
                if not key.startswith("recipe_"):
                    continue
                product = self.by_key.get(key.removeprefix("recipe_"))
                if not product:
                    continue
                for name in [
                    product["display"],
                    product["english"],
                    *aliases.get(product["key"], []),
                ]:
                    for suffix in ("图纸", " recipe"):
                        self.lookup.setdefault(entity_key(name + suffix), set()).add(
                            item["id"]
                        )

    def resolve(self, name):
        normalized = entity_key(name)
        ids = self.lookup.get(normalized, set())
        if len(ids) == 1:
            return self.by_id[next(iter(ids))], None
        if not ids and len(normalized) >= 2:
            ids = {
                i
                for k, values in self.lookup.items()
                if normalized in k
                for i in values
            }
            if len(ids) == 1:
                return self.by_id[next(iter(ids))], None
        options = [self.label(i) for i in sorted(ids)[:12]]
        return None, {
            "error": "名称不明确，请指定完整名称" if options else "未找到该英雄/物品",
            "query": str(name)[:80],
            "candidates": options,
        }

    def label(self, id):
        r = self.by_id.get(id, {})
        return {
            "id": id,
            "name": r.get("display", str(id)),
            "english": r.get("english", ""),
        }


def hero_index(rows, aliases, *, minimum=100):
    # Match the full current hero roster by ID, never by display name or position.
    if not isinstance(rows, list) or len(rows) < minimum:
        raise ValueError("英雄名单不完整")
    ids, names = set(), set()
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("无效英雄记录")
        hid = require_int(row.get("id"), 1, 2000)
        name = row.get("name")
        if (
            hid in ids
            or not isinstance(name, str)
            or not re.fullmatch(r"npc_dota_hero_[a-z0-9_]+", name)
            or name in names
        ):
            raise ValueError("英雄名单 ID 或代码异常")
        ids.add(hid)
        names.add(name)
    return AliasIndex(rows, aliases)


def external_path(value):
    # AstrBot's Windows file picker can return /C:/... rather than C:/....
    if os.name == "nt" and re.match(r"^/[A-Za-z]:[/\\]", value):
        value = value[1:]
    return Path(value)


def snapshot_header(payload, index, source):
    return {
        "snapshot_date": date.fromisoformat(payload["observed_date"]).isoformat(),
        "observed_at": str(payload.get("observed_at_china") or payload["observed_date"]),
        "source": source,
        "covered_heroes": len(index.by_id),
        "stale": payload["observed_date"] != day_key(),
    }


class StatsSnapshot:
    """Valve matchup data only; also reads the legacy PowerShell snapshots.

    The endpoint's raw `win_rate` is NOT a hero's ordinary public win rate.
    Deliberately do not expose it as a baseline, ranking, or hero statistic.
    """

    def __init__(self, payload: dict, aliases: dict, *, strict=True):
        self.raw = payload
        if payload.get("source") != STATS_URL:
            raise ValueError("来源标识不是 Valve 官方统计")
        self.day = date.fromisoformat(payload["observed_date"]).isoformat()
        self.observed = str(payload.get("observed_at_china") or self.day)
        heroes = payload.get("heroes", [])
        groups = payload.get("ranked_hero_data", [])
        if not isinstance(groups, list):
            raise InvalidStatsFormat("对抗分组缺失或不是列表")
        self.index = hero_index(heroes, aliases, minimum=100 if strict else 2)
        ids = sorted(self.index.by_id)
        roster = set(ids)
        self.ranks = {}
        self.coverage = {}
        self.hero_coverage = {}
        self.unknown_pairs = {}
        for group in groups:
            if not isinstance(group, dict):
                raise InvalidStatsFormat("对抗分组记录无效")
            rank = require_int(group.get("rank"), 0, 6)
            if rank in self.ranks:
                raise InvalidStatsFormat(f"对抗分组 rank {rank} 重复")
            raw_rows = group.get("hero_data", [])
            if not isinstance(raw_rows, list) or len(raw_rows) != len(ids):
                count = len(raw_rows) if isinstance(raw_rows, list) else "缺失或非列表"
                raise InvalidStatsFormat(f"对抗 rank {rank} 英雄列表异常（{count}，应为 {len(ids)}）")
            indexed = {}
            for r in raw_rows:
                if not isinstance(r, dict):
                    raise InvalidStatsFormat(f"对抗 rank {rank} 英雄记录无效")
                hid = require_int(r.get("hero_id"), 1, 2000)
                if hid not in roster or hid in indexed:
                    raise InvalidStatsFormat(f"对抗 rank {rank} 英雄 ID {hid} 不在名单中或重复")
                first = require_int(r.get("first_other_hero_id"), 1, 2001)
                width = ids[-1] - hid
                empty = None if width else []
                enemy = r.get("enemy_win_rate", empty)
                ally = r.get("ally_win_rate", empty)
                if (
                    first != hid + 1
                    or not isinstance(enemy, list)
                    or not isinstance(ally, list)
                    or len(enemy) != len(ally)
                    or len(enemy) > width
                    or (len(enemy) < width and (not enemy or hid + len(enemy) not in roster))
                ):
                    enemy_len = len(enemy) if isinstance(enemy, list) else "非列表"
                    ally_len = len(ally) if isinstance(ally, list) else "非列表"
                    raise InvalidStatsFormat(
                        f"对阵 rank {rank} 英雄 {hid} 数组异常（起始 {first}，应为 {hid + 1}；"
                        f"对抗 {enemy_len}、同队 {ally_len}，应各有 {width} 项）"
                    )
                if any(type(x) is not int or not 0 <= x <= 10000 for x in enemy + ally):
                    raise InvalidStatsFormat(f"对阵 rank {rank} 英雄 {hid} 数组值无效")
                if any(a or b for i, (a, b) in enumerate(zip(enemy, ally), first) if i not in roster):
                    raise InvalidStatsFormat(f"对阵 rank {rank} 英雄 {hid} 的非英雄 ID 槽存在非零值")
                indexed[hid] = {
                    "first": first,
                    "enemy": enemy,
                    "ally": ally,
                }
            self.ranks[rank] = indexed
            self._measure_coverage(rank, ids, strict)
        if strict and set(self.ranks) != set(range(7)):
            missing = ", ".join(str(i) for i in range(7) if i not in self.ranks)
            raise InvalidStatsFormat(f"对抗分组不完整（缺少 rank {missing}）")
        self.partial = any(group["missing_pairs"] for group in self.coverage.values())

    def _measure_coverage(self, rank, ids, strict):
        length = len(ids) - 1
        heroes = {
            hid: dict(expected_pairs=length, valid_pairs=0, valid_enemy_pairs=0, valid_ally_pairs=0)
            for hid in ids
        }
        totals = dict(expected_pairs=len(ids) * length // 2, valid_pairs=0,
                      valid_enemy_pairs=0, valid_ally_pairs=0, tail_missing_pairs=0,
                      zero_unknown_pairs=0)
        unknown = []
        for pos, a in enumerate(ids):
            row = self.ranks[rank][a]
            for b in ids[pos + 1:]:
                i = b - row["first"]
                omitted = i >= len(row["enemy"])
                enemy, ally = (False, False) if omitted else (bool(row["enemy"][i]), bool(row["ally"][i]))
                if not (enemy and ally):
                    totals["tail_missing_pairs" if omitted else "zero_unknown_pairs"] += 1
                    unknown.append({"first_id": a, "second_id": b, "enemy_unknown": not enemy,
                                    "ally_unknown": not ally, "reason": "omitted_tail" if omitted else "zero"})
                for counts in (totals, heroes[a], heroes[b]):
                    counts["valid_pairs"] += enemy and ally
                    counts["valid_enemy_pairs"] += enemy
                    counts["valid_ally_pairs"] += ally
        for counts in (totals, *heroes.values()):
            possible, valid = counts["expected_pairs"], counts["valid_pairs"]
            counts["missing_pairs"] = possible - valid
            counts["coverage_pct"] = round(100 * valid / possible, 4)
        for hid, counts in heroes.items():
            if strict and counts["valid_pairs"] * 100 < length * 95:
                raise InvalidStatsFormat(f"对抗 rank {rank} 英雄 {hid} 有效配对不足 95%"
                                         f"（{counts['valid_pairs']}/{length}）")
        if strict and totals["valid_pairs"] * 100 < totals["expected_pairs"] * 95:
            raise InvalidStatsFormat(f"对抗 rank {rank} 有效配对不足 95%")
        self.coverage[rank] = totals
        self.hero_coverage[rank] = heroes
        self.unknown_pairs[rank] = unknown

    def integrity(self):
        possible = sum(group["expected_pairs"] for group in self.coverage.values())
        valid = sum(group["valid_pairs"] for group in self.coverage.values())
        return dict(partial=self.partial, expected_pairs=possible, valid_pairs=valid,
                    missing_pairs=possible - valid, coverage_pct=round(100 * valid / possible, 4) if possible else 0)

    def pair(self, a, b, rank):
        if a == b:
            return None, None
        rows = self.ranks[rank]
        low = min(a, b)
        high = max(a, b)
        r = rows[low]
        i = high - r["first"]
        if i >= len(r["enemy"]):
            return None, None
        enemy = r["enemy"][i]
        ally = r["ally"][i]
        return (
            (None if enemy == 0 else (10000 - enemy if a > b else enemy) / 100),
            None if ally == 0 else ally / 100,
        )

    def meta(self, rank, labels):
        return {
            **snapshot_header(self.raw, self.index, STATS_URL),
            "scope": "Valve 对抗/同队统计（非总体胜率）",
            "rank": rank,
            "partial": self.partial,
            "rank_coverage": self.coverage[rank],
            "rank_label": labels.get(str(rank), f"rank={rank}（段位未核实）"),
            "note": "仅对抗/同队条件胜率；准确统计窗口、样本量及 rank 映射未核实。原始 win_rate 不作英雄自身胜率；统计关系不是因果克制。",
        }

    def related(self, a, rank, mode, limit):
        entries = []
        for b in self.ranks[rank]:
            if b == a:
                continue
            enemy, ally = self.pair(a, b, rank)
            val = ally if mode == "allies" else enemy
            if val is None:
                continue
            entries.append(
                {
                    **self.index.label(b),
                    "hero_win_vs_pct": enemy,
                    "ally_win_pct": ally,
                    "metric_pct": val,
                }
            )
        entries.sort(key=lambda r: r["metric_pct"], reverse=mode != "counters")
        return entries[:limit]


def indexed_rows(rows, id_key, expected):
    if not isinstance(rows, list):
        raise ValueError("统计英雄记录不是列表")
    result = {}
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("统计英雄记录无效")
        hid = require_int(row.get(id_key), 1, 2000)
        if hid in result:
            raise ValueError("统计英雄 ID 重复")
        result[hid] = row
    if set(result) != expected:
        raise ValueError("统计英雄名单与目录不一致")
    return result


def fraction(value):
    if type(value) not in (int, float) or not 0 <= value <= 1:
        raise ValueError("Valve 周统计百分比必须为 0..1 的比例")
    return float(value)


def check_balance(rate, scope):
    # In a complete ordinary 5v5 match pool, 5 of the 10 hero picks win.
    # Weight by picks, not the unweighted average of hero win rates.
    # Deliberately NOT applied to the matchup endpoint's conditional statistics.
    pct = rate * 100
    if abs(pct - 50) > MAX_BALANCE_ERROR_PP:
        raise ValueError(f"{scope} 加权胜率 {pct:.2f}% 偏离 50%，拒绝异常快照")
    return round(pct, 5)


class WinRateSnapshot:
    """A self-contained, validated public or weekly hero-performance snapshot."""

    def __init__(self, payload, aliases, *, source):
        if source not in ("public", "weekly") or payload.get("source") != SOURCES[source]:
            raise ValueError("英雄胜率来源标识不匹配")
        self.raw = payload
        self.source = source
        self.day = date.fromisoformat(payload["observed_date"]).isoformat()
        self.index = hero_index(payload.get("heroes"), aliases)
        self.groups = {}
        self.quality = {}
        if source == "public":
            self._parse_public(payload.get("hero_stats"))
        else:
            self._parse_weekly(payload.get("hero_stats"))

    def _parse_public(self, rows):
        indexed = indexed_rows(rows, "id", set(self.index.by_id))
        for hid, row in indexed.items():
            if row.get("name") != self.index.by_id[hid]["name"]:
                raise ValueError("OpenDota 英雄 ID 对应的内部名称与目录不一致")
        for segment in PUBLIC_SEGMENTS:
            pick_key, win_key = segment + "_pick", segment + "_win"
            if segment != "pub" and not any(
                pick_key in r or win_key in r for r in indexed.values()
            ):
                continue  # A completely absent bracket is unavailable, not 0%.
            records = {}
            for hid, row in indexed.items():
                picks = require_int(row.get(pick_key), 0, 10**12)
                wins = require_int(row.get(win_key), 0, picks)
                if picks:
                    records[hid] = {"rate": wins / picks, "picks": picks, "wins": wins}
            if not records:
                if segment == "pub":
                    raise ValueError("OpenDota 公开样本为空")
                continue  # In particular, 8_pick/8_win can be all zero.
            if segment == "pub" and len(records) < 0.95 * len(indexed):
                raise ValueError("OpenDota 公开样本的有效英雄覆盖不足95%")
            total = sum(r["picks"] for r in records.values())
            wins = sum(r["wins"] for r in records.values())
            weighted = check_balance(wins / total, f"OpenDota {segment}")
            self.groups[segment] = records
            self.quality[segment] = {
                "sampled_heroes": len(records),
                "total_picks": total,
                "total_wins": wins,
                "weighted_win_rate_pct": weighted,
            }

    def _parse_weekly(self, rows):
        expected = set(self.index.by_id)
        indexed = indexed_rows(rows, "hero_id", expected)
        buckets = {}
        for hid, row in indexed.items():
            chunks = row.get("hero_data_per_chunk")
            if not isinstance(chunks, list):
                raise ValueError("Valve 周统计分组缺失")
            ranks = set()
            for chunk in chunks:
                if not isinstance(chunk, dict):
                    raise ValueError("Valve 周统计分组无效")
                rank = require_int(chunk.get("rank_chunk"), 0, 6)
                if rank in ranks:
                    raise ValueError("重复 rank_chunk")
                ranks.add(rank)
                weeks = chunk.get("week_data")
                if not isinstance(weeks, list):
                    raise ValueError("Valve 周统计 week_data 缺失")
                seen = set()
                for row_w in weeks:
                    if not isinstance(row_w, dict):
                        raise ValueError("Valve 周统计记录无效")
                    week = require_int(row_w.get("week"), 1, 8)
                    if week in seen:
                        raise ValueError("重复 week")
                    seen.add(week)
                    win = fraction(row_w.get("win_percent"))
                    pick = fraction(row_w.get("pick_percent"))
                    ban = fraction(row_w.get("ban_percent"))
                    buckets.setdefault((rank, week), {})[hid] = {
                        "rate": win, "pick": pick, "ban": ban
                    }
        if set(buckets) != {(rank, week) for rank in range(7) for week in range(1, 9)}:
            raise ValueError("Valve 的 7 个分组、8 个周索引不完整")
        for key, records in buckets.items():
            if set(records) != expected:
                raise ValueError(f"Valve 周统计 {key} 英雄名单不完整")
            total = math.fsum(r["pick"] for r in records.values())
            if not total:
                continue
            if not 9.5 <= total <= 10.5:
                raise ValueError(f"Valve 周统计 {key} 的选取率总和异常（{total * 100:.2f}%）")
            weighted = check_balance(
                math.fsum(r["rate"] * r["pick"] for r in records.values()) / total,
                f"Valve 周统计 {key}",
            )
            self.groups[key] = {hid: r for hid, r in records.items() if r["pick"] > 0}
            self.quality[key] = {
                "sampled_heroes": len(self.groups[key]),
                "total_pick_rate_pct": round(total * 100, 3),
                "weighted_win_rate_pct": weighted,
            }
        if not self.groups:
            raise ValueError("Valve 没有有效周统计样本")

    def rate(self, hid, key):
        return self.groups.get(key, {}).get(hid, {}).get("rate")

    def row(self, hid, key):
        row = self.groups.get(key, {}).get(hid)
        if not row:
            return {**self.index.label(hid), "win_rate_pct": None, "note": "该英雄无有效样本"}
        out = {**self.index.label(hid), "win_rate_pct": round(row["rate"] * 100, 2)}
        if self.source == "public":
            out.update(picks=row["picks"], wins=row["wins"])
        else:
            out.update(
                pick_rate_pct=round(row["pick"] * 100, 2),
                ban_rate_pct=round(row["ban"] * 100, 2),
            )
        return out

    def ranked(self, key, order, limit):
        rows = self.groups[key]
        # Sort on the unrounded ratios, consistently resolving ties by ID.
        direction = -1 if order == "desc" else 1
        ids = sorted(rows, key=lambda hid: (direction * rows[hid]["rate"], hid))
        return [self.row(hid, key) for hid in ids[:limit]]

    def meta(self, key):
        info = snapshot_header(self.raw, self.index, SOURCES[self.source])
        if self.source == "public":
            info.update(
                scope=("OpenDota 公开比赛样本（总体，非全段位天梯全集）"
                       if key == "pub" else f"OpenDota 段位字段 {key}：{BRACKETS[int(key)]}"),
                note="按 wins/picks 计算百分比；total_picks 为英雄选取总次数，非比赛总场次。快照日期为抓取日，接口为滚动统计，并非当日比赛或严格当前补丁样本；不代表全部比赛，不与 Valve 分组混用。",
            )
            if key != "pub":
                info["bracket"] = int(key)
                if key in ("7", "8"):
                    info["bracket_note"] = "按接口原始字段统计；字段8无样本时无法确认独立冠绝一世胜率，也不保证字段7是纯超凡入圣样本。"
        else:
            info.update(
                scope="Valve 官方英雄周统计",
                rank_chunk=key[0],
                week=key[1],
                note="rank_chunk 段位映射、week 的准确起止日期未核实；默认 week=1 是推定最近索引。百分比由 0..1 的比例乘 100，不是对抗胜率；绝对场次未提供。",
            )
        info["quality"] = self.quality.get(key, {"sampled_heroes": 0})
        if self.raw.get("roster_source") == PUBLIC_STATS_URL:
            info["roster_note"] = "Valve 名单暂不可用；与 OpenDota 返回的英雄名单核对，英雄名可能为英文。"
        if key in self.groups and len(self.groups[key]) < len(self.index.by_id):
            info["coverage_note"] = "部分英雄无样本，排名只包含有有效样本的英雄。"
        return info


def fit_stats_payload(data, maximum):
    # All numeric fields are validated public statistics, not text containing
    # personal IDs. Scrub string values only and truncate whole records, never
    # change a count or slice through a decimal/JSON object.
    def scrub(value):
        if isinstance(value, str):
            return clean(value, 500, urls=True)
        if isinstance(value, dict):
            return {key: scrub(item) for key, item in value.items()}
        if isinstance(value, list):
            return [scrub(item) for item in value]
        return value

    out = scrub(data)
    if len(dump(out)) <= maximum:
        return out
    out["truncated"] = True
    for key in ("series", "ranking", "rows", "hero_rows", "candidates"):
        rows = out.get(key)
        if isinstance(rows, list):
            while len(rows) > 1 and len(dump(out)) > maximum:
                rows.pop()
            if key == "rows" and "purchase_count" in out and "offset" in out:
                next_offset = out["offset"] + len(rows)
                out["next_offset"] = next_offset if next_offset < out["purchase_count"] else None
    if len(dump(out)) > maximum and "matchup" in out:
        out["matchup"] = {"error": "字符预算不足，请单独查询对抗统计"}
    if len(dump(out)) <= maximum:
        return out
    return {
        "source": out.get("source"),
        "snapshot_date": out.get("snapshot_date"),
        "error": "统计结果的完整记录和口径说明超过字符预算，请缩小查询范围或提高 tool_result_chars。",
    }


class DotaService:
    def __init__(self, root: Path, config, log):
        self.root = root
        self.config = config
        self.log = log
        self.snap_dir = root / "snapshots"
        self.partial_matchup_dir = root / "partial_matchups"
        self.last_complete_matchup_date = None
        self.source_dirs = {
            "matchups": self.snap_dir,
            "public": root / "public_stats",
            "weekly": root / "weekly_stats",
        }
        self.detail_dir = root / "detail"
        self.catalog_path = root / "catalog.json"
        for directory in self.source_dirs.values():
            directory.mkdir(parents=True, exist_ok=True)
        self.partial_matchup_dir.mkdir(parents=True, exist_ok=True)
        self.detail_dir.mkdir(parents=True, exist_ok=True)
        self.aliases = read_json(ALIAS_FILE)
        self.stats_lock = asyncio.Lock()
        self.sync_lock = asyncio.Lock()
        self.catalog_lock = asyncio.Lock()
        self.detail_lock = asyncio.Lock()
        self.cached_stats = {}
        self.catalog_cache = None
        self.md_cache = None
        self.catalog_retry_after = 0
        self.detail_retry = {}
        self.last_status = {"state": "未同步", "sources": {}}
        self.last_sync_attempt = 0
        self.sync_state_path = root / "stats_sync_state.json"
        self.sync_state = self._load_sync_state()
        self.knowledge = KnowledgeStore(self, root / "knowledge", log)
        self.matches = DotaMatches(root, lambda url, cfg: self._fetch(url, cfg), log)

    def _load_sync_state(self):
        try:
            saved = read_json(self.sync_state_path)
        except FileNotFoundError:
            return {}
        except (OSError, ValueError):
            self.log.emit("DOTA", "sync_state_read_failed", "WARNING")
            return {}
        if not isinstance(saved, dict):
            return {}
        state = {}
        for source in SOURCES:
            entry = saved.get(source)
            if not isinstance(entry, dict):
                continue
            times = {
                key: value for key in ("last_attempt", "last_manual", "retry_after")
                if type(value := entry.get(key)) in (int, float) and 0 <= value <= 253402214399
            }
            kind = entry.get("failure_kind")
            if isinstance(kind, str) and kind in STATS_FAILURES:
                times.update(failure_kind=kind, reason=clean(entry.get("reason"), 160, urls=True))
                failures = entry.get("failures")
                times["failures"] = failures if type(failures) is int and 1 <= failures <= 8 else 1
            success = entry.get("success_date")
            if isinstance(success, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", success):
                times["success_date"] = success
            state[source] = times
        return state

    async def _save_sync_state(self):
        try:
            await write_stats_json(self.sync_state_path, self.sync_state)
        except OSError:
            self.log.emit("DOTA", "sync_state_write_failed", "WARNING")

    def _pending_sources(self, existing, at, *, force=False, manual=False):
        pending = []
        today = day_key(at)
        for source, snapshot in existing.items():
            health = self.sync_state.get(source, {})
            verified_today = snapshot and snapshot.day == today
            if force:
                pending.append(source)
            elif manual:
                last_manual = health.get("last_manual")
                if ((not verified_today or health.get("failure_kind") or
                     isinstance(snapshot, StatsSnapshot) and snapshot.partial)
                    and (last_manual is None or at >= last_manual + STATS_MANUAL_INTERVAL)):
                    pending.append(source)
            elif not verified_today and at >= health.get("retry_after", 0):
                pending.append(source)
        return pending

    async def auto_sync_due(self, cfg):
        if self.sync_lock.locked():
            return False
        return bool(self._pending_sources(await self._existing(cfg), now()))

    def _paths(self, cfg, day=None, source="matchups", complete=False):
        roots = [self.source_dirs[source]]
        if source == "matchups" and not complete:
            roots.append(self.partial_matchup_dir)
        if source == "matchups" and cfg["existing_snapshots_dir"]:
            roots.append(external_path(cfg["existing_snapshots_dir"]))
        paths = []
        for root in roots:
            try:
                if not root.is_dir():
                    continue
                candidates = (
                    [root / (day.replace("-", "") + ".json")]
                    if day
                    else [p for p in root.glob("*.json") if SNAP_RE.fullmatch(p.name)]
                )
                for path in candidates:
                    try:
                        stat = path.stat()
                        if path.is_file() and stat.st_size <= 32 * 1024 * 1024:
                            paths.append((path.name, stat.st_mtime, path))
                    except OSError:
                        continue
            except OSError:
                continue
        return [
            p for _, __, p in sorted(
                paths, key=lambda x: (x[0], x[2].parent != self.partial_matchup_dir, x[1]), reverse=True
            )
        ]

    def _load_stats(self, cfg, day=None, source="matchups", complete=False):
        candidates = self._paths(cfg, day, source, complete)
        if not complete:
            candidates = candidates[: (8 if day is None else 4)]
        best = None
        for path in candidates:
            if best and path.stem != best.day.replace("-", ""):
                break
            try:
                signature = (
                    source,
                    str(path.resolve()),
                    path.stat().st_mtime_ns,
                    path.stat().st_size,
                )
                if signature in self.cached_stats:
                    obj = self.cached_stats[signature]
                else:
                    payload = read_json(path)
                    obj = self._validate_snapshot(source, payload)
                    if path.stem != obj.day.replace("-", "") or obj.day > day_key():
                        continue
                    self.cached_stats[signature] = obj
                    while len(self.cached_stats) > 15:
                        self.cached_stats.pop(next(iter(self.cached_stats)))
                if source != "matchups" or not obj.partial:
                    return obj
                if not complete and (not best or obj.integrity()["valid_pairs"] > best.integrity()["valid_pairs"]):
                    best = obj
            except (OSError, ValueError, TypeError, KeyError, AttributeError, OverflowError):
                continue
        return best

    def _validate_snapshot(self, source, payload):
        if source == "matchups":
            return StatsSnapshot(payload, self.aliases["heroes"])
        return WinRateSnapshot(payload, self.aliases["heroes"], source=source)

    async def latest(self, cfg, day=None, source="matchups"):
        async with self.stats_lock:
            return await asyncio.to_thread(self._load_stats, cfg, day, source)

    async def _existing(self, cfg):
        def load():
            snapshots = {source: self._load_stats(cfg, source=source) for source in SOURCES}
            matchup = snapshots["matchups"]
            complete = self._load_stats(cfg, complete=True) if not matchup or matchup.partial else matchup
            self.last_complete_matchup_date = complete.day if complete else None
            return snapshots

        async with self.stats_lock:
            return await asyncio.to_thread(load)

    def _source_state(self, source, snapshot, state=None):
        health = self.sync_state.get(source, {})
        failure_kind = health.get("failure_kind")
        if not state:
            if failure_kind:
                state = "同步失败，保留旧数据" if snapshot else "同步失败，无已校验数据"
            else:
                state = "已读取快照" if snapshot else "无已校验数据"
        info = {
            "state": state,
            "date": snapshot.day if snapshot else None,
            "success_date": snapshot.day if snapshot else health.get("success_date"),
        }
        if snapshot:
            info.update(heroes=len(snapshot.index.by_id), stale=snapshot.day != day_key())
            if isinstance(snapshot, StatsSnapshot):
                info.update(**snapshot.integrity(), last_complete_date=self.last_complete_matchup_date)
                if snapshot.partial and state != "同步中":
                    if snapshot.day == day_key():
                        info["state"] = "今日已同步，部分配对缺失"
                    elif not failure_kind:
                        info["state"] = "已读取快照，部分配对缺失"
            if isinstance(snapshot, WinRateSnapshot):
                info["partitions"] = len(snapshot.groups)
                if snapshot.source == "public":
                    info["available_brackets"] = [i for i in BRACKETS if str(i) in snapshot.groups]
                    info["weighted_win_rate_pct"] = snapshot.quality["pub"]["weighted_win_rate_pct"]
        if health.get("last_attempt"):
            info["last_attempt"] = stamp(health["last_attempt"])
        if failure_kind:
            info.update(failure_kind=failure_kind, reason=health.get("reason", ""))
        at = now()
        retry_after = health.get("retry_after", 0)
        if retry_after > at:
            info["next_attempt"] = stamp(retry_after)
        if "last_manual" in health and health["last_manual"] + STATS_MANUAL_INTERVAL > at:
            info["next_manual_attempt"] = stamp(health["last_manual"] + STATS_MANUAL_INTERVAL)
        return info

    @staticmethod
    def _sync_overview(states, updated=()):
        if any(s.get("failure_kind") for s in states.values()):
            available = any(s["date"] and not s.get("failure_kind") for s in states.values())
            return "部分同步成功" if updated or available else "同步失败"
        if any(s.get("partial") for s in states.values()):
            return "部分配对缺失"
        if updated:
            return "同步成功"
        if all(s["date"] == day_key() for s in states.values()):
            return "今日快照已存在"
        return "已读取快照" if any(s["date"] for s in states.values()) else "未同步"

    async def status(self, cfg):
        # The management page can still be opened during a slow refresh.
        if self.sync_lock.locked():
            return self.last_status
        existing = await self._existing(cfg)
        if self.sync_lock.locked():
            return self.last_status
        states = {k: self._source_state(k, s) for k, s in existing.items()}
        return {"state": self._sync_overview(states), "sources": states}

    async def _fetch(self, url, cfg):
        # Only fixed Valve/OpenDota endpoints, never a model-supplied URL or credential.
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=cfg["network_timeout"])
        ) as client:
            async with client.get(
                url,
                allow_redirects=False,
                headers={"User-Agent": "FiyuCore/0.1 (Dota data reader)"},
            ) as response:
                if response.status != 200:
                    raise HTTPFailure(response.status)
                data = bytearray()
                async for chunk in response.content.iter_chunked(65536):
                    data.extend(chunk)
                    if len(data) > 32 * 1024 * 1024:
                        raise InvalidStatsFormat("接口数据过大")
                result = json.loads(data)
                list_endpoint = url in (PUBLIC_STATS_URL, PRO_LIST, PATCHES) or bool(re.fullmatch(
                    r"https://api\.opendota\.com/api/heroes/[1-9]\d{0,3}/matches", url
                ))
                if not isinstance(result, list if list_endpoint else dict):
                    raise InvalidStatsFormat("统计接口 JSON 根节点类型错误")
                return result

    def _choose_roster(self, roster_data, existing, fetched):
        try:
            heroes = rows_at(roster_data, "heroes")
            hero_index(heroes, self.aliases["heroes"])
            return heroes, HERO_LIST_URL
        except (ValueError, TypeError, KeyError, AttributeError):
            pass
        # Keep independent sources usable when the Valve roster endpoint is down.
        # Prefer a previously verified Valve roster, then OpenDota's own roster.
        candidates = sorted(
            (s for s in existing.values() if s),
            key=lambda s: (s.raw.get("roster_source") != PUBLIC_STATS_URL, s.day),
            reverse=True,
        )
        for s in candidates:
            if s.raw.get("roster_source") != PUBLIC_STATS_URL:
                return s.raw["heroes"], s.raw.get("roster_source", HERO_LIST_URL)
        try:
            catalog = self.catalog_cache or read_json(self.catalog_path)
            heroes = catalog["heroes"]
            hero_index(heroes, self.aliases["heroes"])
            return heroes, HERO_LIST_URL
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            pass
        public = fetched.get("public")
        if isinstance(public, list):
            try:
                heroes = [
                    {
                        "id": r["id"], "name": r["name"],
                        "name_loc": r["localized_name"],
                        "name_english_loc": r["localized_name"],
                    }
                    for r in public
                ]
                hero_index(heroes, self.aliases["heroes"])
                return heroes, PUBLIC_STATS_URL
            except (ValueError, KeyError, TypeError, AttributeError):
                pass
        if candidates:
            return candidates[0].raw["heroes"], PUBLIC_STATS_URL
        raise ValueError("没有可用于校验统计的完整英雄名单")

    def _new_snapshot(self, source, data, heroes, roster_source, day, observed):
        payload = {
            "schema": 1,
            "source": SOURCES[source],
            "observed_date": day,
            "observed_at_china": observed,
            "heroes": heroes,
            "roster_source": roster_source,
        }
        if source == "matchups":
            payload["ranked_hero_data"] = data.get("ranked_hero_data")
        elif source == "weekly":
            payload["hero_stats"] = data.get("heroes")
        else:
            if not isinstance(data, list):
                raise ValueError("OpenDota 必须返回英雄数组")
            keys = {
                "id", "name",
                *(f"{segment}_{field}" for segment in PUBLIC_SEGMENTS for field in ("pick", "win")),
            }
            # No need to persist unrelated hero attributes or duplicate trend arrays.
            payload["hero_stats"] = [{k: v for k, v in r.items() if k in keys} for r in data]
        snapshot = self._validate_snapshot(source, payload)
        if isinstance(snapshot, StatsSnapshot):
            payload["matchup_quality"] = {
                **snapshot.integrity(), "ranks": snapshot.coverage, "unknown_pairs": snapshot.unknown_pairs,
            }
        return snapshot

    @staticmethod
    def _sync_failure(error, stage):
        if stage == "roster":
            return "roster_unavailable", "没有可用于校验统计的完整英雄名单", True
        if stage == "storage":
            return "storage_error", "写入快照失败", False
        if isinstance(error, InvalidStatsFormat):
            return "invalid_structure", str(error), True
        if stage == "validation":
            # Validator messages are generated locally, without response fields.
            reason = clean(str(error), 160, urls=True) if isinstance(error, ValueError) else "统计结构或记录不完整"
            return "invalid_structure", reason, True
        if isinstance(error, HTTPFailure):
            transient = error.status in (408, 429) or 500 <= error.status <= 599
            return "http_error", f"统计接口 HTTP {error.status}", not transient
        if isinstance(error, (json.JSONDecodeError, UnicodeError)):
            return "invalid_json", "统计接口返回无效 JSON", True
        if isinstance(error, (asyncio.TimeoutError, TimeoutError)):
            return "timeout", "统计接口请求超时", False
        if isinstance(error, (aiohttp.ClientError, OSError)):
            return "network_error", "统计接口连接或传输失败", False
        return "unknown", "统计接口请求失败", False

    def _mark_sync_failed(self, source, kind, reason, structural):
        health = self.sync_state[source]
        failures = min(8, health.get("failures", 0) + 1) if health.get("failure_kind") == kind else 1
        base, limit = (4 * 3600, 24 * 3600) if structural else (STATS_SYNC_INTERVAL, 2 * 3600)
        health.update(failure_kind=kind, reason=reason, failures=failures,
                      retry_after=now() + min(limit, base * 2 ** (failures - 1)))

    async def sync(self, cfg, force=False, *, manual=False):
        async with self.sync_lock:
            started = self.last_sync_attempt = now()
            today = day_key(started)
            existing = await self._existing(cfg)
            pending = self._pending_sources(existing, started, force=force, manual=manual)
            for source in pending:
                health = self.sync_state.setdefault(source, {})
                health["last_attempt"] = started
                # Record a short hold before starting network I/O, in case the process stops.
                health["retry_after"] = max(health.get("retry_after", 0), started + STATS_SYNC_INTERVAL)
                if manual:
                    health["last_manual"] = started
            states = {
                k: self._source_state(k, s, "同步中" if k in pending else
                                      "今日快照已存在" if s and s.day == today and
                                      not self.sync_state.get(k, {}).get("failure_kind") else None)
                for k, s in existing.items()
            }
            if not pending:
                self.last_status = {"state": self._sync_overview(states), "sources": states}
                return self.last_status
            self.last_status = {"state": "同步中", "sources": states}
            updated = []
            remaining = set(pending)
            try:
                await self._save_sync_state()
                results = await asyncio.gather(
                    self._fetch(HERO_LIST_URL, cfg),
                    *(self._fetch(SOURCES[k], cfg) for k in pending),
                    return_exceptions=True,
                )
                fetched = dict(zip(pending, results[1:]))
                roster_error = None
                try:
                    heroes, roster_source = await asyncio.to_thread(
                        self._choose_roster, results[0], existing, fetched
                    )
                except ValueError as e:
                    roster_error = e
                observed = stamp()
                observed_day = observed[:10]
                for source in pending:
                    stage = "fetch"
                    try:
                        data = fetched[source]
                        if isinstance(data, BaseException):
                            raise data
                        stage = "roster"
                        if roster_error:
                            raise roster_error
                        stage = "validation"
                        validated = await asyncio.to_thread(
                            self._new_snapshot, source, data, heroes, roster_source, observed_day, observed
                        )
                        stage = "storage"
                        # Only a validated payload replaces this source's snapshot.
                        async with self.stats_lock:
                            partial = isinstance(validated, StatsSnapshot) and validated.partial
                            directory = self.partial_matchup_dir if partial else self.source_dirs[source]
                            await write_stats_json(
                                directory / (validated.day.replace("-", "") + ".json"),
                                validated.raw,
                            )
                            for key in list(self.cached_stats):
                                if key[0] == source:
                                    self.cached_stats.pop(key)
                    except Exception as e:
                        kind, reason, structural = self._sync_failure(e, stage)
                        self._mark_sync_failed(source, kind, reason, structural)
                        states[source] = self._source_state(source, existing[source])
                        self.log.emit(
                            "DOTA", "sync_failed", "WARNING", task=source, status=kind,
                            reason=reason, exception=type(e).__name__,
                            retry_after=int(self.sync_state[source]["retry_after"]),
                        )
                    else:
                        health = self.sync_state[source]
                        for key in ("failure_kind", "reason", "retry_after", "failures"):
                            health.pop(key, None)
                        health["success_date"] = validated.day
                        previous = existing[source]
                        if source == "matchups" and not partial:
                            self.last_complete_matchup_date = validated.day
                        if partial and previous and previous.day == validated.day and not previous.partial:
                            states[source] = self._source_state(source, previous, "今日快照已存在")
                        else:
                            states[source] = self._source_state(source, validated, "同步成功")
                        updated.append(source)
                        self.log.emit("DOTA", "sync_partial" if partial else "sync_ok",
                                      task=source, heroes=len(validated.index.by_id))
                    remaining.discard(source)
            except asyncio.CancelledError:
                for source in remaining:
                    health = self.sync_state[source]
                    if not health.get("failure_kind"):
                        health.update(failure_kind="interrupted", reason="统计同步中断", failures=1)
                    states[source] = self._source_state(source, existing[source])
                raise
            finally:
                self.last_status = {"state": self._sync_overview(states, updated), "sources": states}
                await self._save_sync_state()
            if updated:
                try:
                    await asyncio.to_thread(self._prune, cfg, updated)
                except OSError:
                    self.log.emit("DOTA", "snapshot_cleanup_failed", "WARNING")
            return self.last_status

    def _prune(self, cfg, sources):
        cutoff = (
            date.fromisoformat(day_key())
            - timedelta(days=cfg["snapshot_retention_days"] - 1)
        ).strftime("%Y%m%d")
        # A successful refresh of another source must not delete the only
        # usable fallback snapshot of a source that is still failing.
        directories = [self.source_dirs[source] for source in sources if source != "matchups"]
        if "matchups" in sources:
            directories.append(self.partial_matchup_dir)
            if not self.last_status["sources"]["matchups"].get("partial"):
                # A same-day full snapshot may have been retained from before
                # the refresh. Prune older complete data only with full data available.
                directories.append(self.snap_dir)
        for directory in directories:
            for p in directory.iterdir():
                if (
                    SNAP_RE.fullmatch(p.name)
                    and p.stem < cutoff
                    and p.is_file()
                    and not p.is_symlink()
                ):
                    p.unlink(missing_ok=True)

    async def stats(
        self,
        cfg,
        action,
        *,
        hero="",
        other="",
        rank=None,
        bracket=None,
        rank_chunk=None,
        week=None,
        previous_week=None,
        include_matchup=False,
        matchup_rank=None,
        limit=10,
        order="desc",
        mode="counters",
        days=None,
        observed_date="",
    ):
        limit = max(1, min(30, int(limit)))
        if order not in ("desc", "asc"):
            return {"error": "order 必须为 desc 或 asc"}
        if observed_date:
            try:
                if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", observed_date):
                    raise ValueError("日期格式错误")
                observed_date = date.fromisoformat(observed_date).isoformat()
            except ValueError:
                return {"error": "日期必须为 YYYY-MM-DD"}
        if action == "matchups":
            if bracket is not None or rank_chunk is not None or week is not None:
                return {"error": "对抗数据使用独立 Valve rank=0..6；不能自动映射 OpenDota 段位或周统计分组。"}
            return await self._matchups(cfg, hero, other, rank, mode, limit, observed_date)
        # `rank` used to be a raw Valve index in these internal tools. Preserve
        # explicitly supplied legacy arguments, but never apply default_rank to
        # ordinary hero win rates. The new tools expose rank_chunk separately.
        if rank is not None:
            if rank_chunk is not None and rank_chunk != rank:
                return {"error": "rank 与 rank_chunk 冲突"}
            rank_chunk = rank
        if bracket is not None and (type(bracket) is not int or bracket not in BRACKETS):
            return {"error": "OpenDota bracket 必须为 1..8"}
        if rank_chunk is not None and (type(rank_chunk) is not int or rank_chunk not in range(7)):
            return {"error": "Valve rank_chunk 必须为 0..6"}
        if bracket is not None and rank_chunk is not None:
            return {"error": "只能选择一种统计口径：OpenDota bracket 或 Valve rank_chunk"}
        if rank_chunk is None and (week is not None or previous_week is not None):
            return {"error": "week 和 previous_week 仅用于指定 Valve rank_chunk 的周统计"}
        if week is not None and (type(week) is not int or not 1 <= week <= 8):
            return {"error": "Valve week 必须为 1..8"}
        if rank_chunk is None:
            source = "public"
            key = str(bracket) if bracket is not None else "pub"
        else:
            source = "weekly"
            key = (rank_chunk, week if week is not None else 1)
        s = await self.latest(cfg, observed_date or None, source)
        if not s:
            reply = {
                "source": SOURCES[source],
                "error": "无该来源的已校验统计快照。请在管理页同步 Dota 统计。旧 Valve 对抗快照不是英雄总体胜率。",
            }
            if observed_date:
                reply["requested_snapshot_date"] = observed_date
            if action == "compare" and (include_matchup or matchup_rank is not None):
                reply["matchup"] = await self._matchups(cfg, hero, other, matchup_rank, mode, limit, observed_date)
            return reply
        meta = s.meta(key)
        if key not in s.groups:
            if source == "weekly":
                meta["available_weeks"] = sorted(w for r, w in s.groups if r == rank_chunk)
            else:
                meta["available_brackets"] = [i for i in BRACKETS if str(i) in s.groups]
            reply = {**meta, "error": "所选统计口径无有效样本，不是 0% 胜率。不能自动换成其他段位或数据来源。"}
            if action == "compare" and (include_matchup or matchup_rank is not None):
                reply["matchup"] = await self._matchups(cfg, hero, other, matchup_rank, mode, limit, observed_date)
            return reply
        if action == "ranking":
            return {**meta, "ranking": s.ranked(key, order, limit)}
        if action == "trends":
            if source == "weekly":
                if days is not None:
                    return {**meta, "error": "Valve 周统计请用 week/previous_week 原始周索引，不传 days。"}
                previous_week = key[1] + 1 if previous_week is None else previous_week
                if type(previous_week) is not int or not 1 <= previous_week <= 8 or previous_week == key[1]:
                    return {**meta, "error": "previous_week 需为 1..8 且不同于 week"}
                previous_key = (rank_chunk, previous_week)
                old = s
                comparison = {
                    "previous_week": previous_week,
                    "comparison_note": "同一抓取快照的两个原始周索引之差（百分点）；具体日期范围及版本未核实。",
                }
            else:
                days = 7 if days is None else days
                if type(days) is not int or not 1 <= days <= 180:
                    return {**meta, "error": "历史间隔需为 1..180 天"}
                past = (date.fromisoformat(s.day) - timedelta(days=days)).isoformat()
                old = await self.latest(cfg, past, source)
                previous_key = key
                comparison = {
                    "previous_date": past,
                    "comparison_note": "同一来源、同一口径的两个日快照之差（百分点），不是该时间段实际比赛统计；可能跨版本。",
                }
            if not old or previous_key not in old.groups:
                return {**meta, **comparison, "error": "缺少同口径的历史样本，不能比较；不拿旧对抗数据补充。"}
            result = self._compare_periods(s, key, old, previous_key, hero, limit, order)
            if source == "weekly" and hero and "error" not in result:
                a, _ = s.index.resolve(hero)
                result["series"] = []
                for w in range(1, 9):
                    row = s.row(a["id"], (rank_chunk, w))
                    result["series"].append({
                        "week": w,
                        **{k: v for k, v in row.items() if k not in ("id", "name", "english")},
                    })
            return {**meta, **comparison, "previous_quality": old.quality[previous_key], **result}
        a, err = s.index.resolve(hero)
        if err:
            return err
        aid = a["id"]
        if action == "hero":
            return {**meta, "hero": s.row(aid, key)}
        if action == "compare":
            b, err = s.index.resolve(other)
            if err:
                return err
            ar, br = s.rate(aid, key), s.rate(b["id"], key)
            result = {
                **meta,
                "first": s.row(aid, key),
                "second": s.row(b["id"], key),
                "difference_percentage_points": round((ar - br) * 100, 2) if ar is not None and br is not None else None,
            }
            if include_matchup or matchup_rank is not None:
                # Provenance stays nested; own win rates and conditional pair
                # statistics may represent different samples or rank groups.
                result["matchup"] = await self._matchups(cfg, hero, other, matchup_rank, mode, limit, observed_date)
            return result
        return {"error": "未知统计操作"}

    @staticmethod
    def _compare_periods(s, key, old, previous_key, hero, limit, order):
        ids = set(s.groups[key]) & set(old.groups[previous_key])
        if hero:
            a, err = s.index.resolve(hero)
            if err:
                return err
            ids &= {a["id"]}
        if not ids:
            return {"error": "两个时期没有共同的有效英雄样本"}
        delta = {hid: (s.rate(hid, key) - old.rate(hid, previous_key)) * 100 for hid in ids}
        direction = -1 if order == "desc" else 1
        ordered = sorted(ids, key=lambda hid: (direction * delta[hid], hid))
        rows = []
        for hid in ordered[:limit]:
            previous = old.row(hid, previous_key)
            row = {
                **s.row(hid, key),
                "previous_pct": previous["win_rate_pct"],
                "delta_percentage_points": round(delta[hid], 2),
            }
            if "picks" in previous:
                row["previous_picks"] = previous["picks"]
            rows.append(row)
        return {"compared_heroes": len(ids), "rows": rows}

    async def _matchups(self, cfg, hero, other, rank, mode, limit, observed_date):
        rank = cfg["default_rank"] if rank is None else rank
        if type(rank) is not int or rank not in range(7):
            return {"error": "Valve 对抗 rank 必须为 0..6"}
        if mode not in ("counters", "favored", "allies"):
            return {"error": "无效对抗排序方式"}
        s = await self.latest(cfg, observed_date or None, "matchups")
        if not s:
            return {"source": STATS_URL, "error": "无已校验的对抗快照。请同步或配置旧对抗快照目录。"}
        if rank not in s.ranks:
            return {"source": STATS_URL, "error": "该对抗 rank 无数据"}
        meta = s.meta(rank, cfg["rank_labels"])
        a, err = s.index.resolve(hero)
        if err:
            return {**meta, **err}
        aid = a["id"]
        if other:
            b, err = s.index.resolve(other)
            if err:
                return {**meta, **err}
            enemy, ally = s.pair(aid, b["id"], rank)
            return {
                **meta,
                "first": s.index.label(aid),
                "second": s.index.label(b["id"]),
                "first_vs_second_pct": enemy,
                "same_team_pct": ally,
                "pair_status": "partial" if enemy is None or ally is None else "complete",
                **({"data_note": "本快照、该 rank 的缺失指标无数据。可明确指定旧抓取日另查。"}
                   if enemy is None or ally is None else {}),
            }
        return {
            **meta,
            "hero": s.index.label(aid),
            "mode": mode,
            "rows": s.related(aid, rank, mode, limit),
            "hero_coverage": s.hero_coverage[rank][aid],
            "available_pairs": s.hero_coverage[rank][aid][
                "valid_ally_pairs" if mode == "allies" else "valid_enemy_pairs"
            ],
            "metric_explanation": "counters 按对阵胜率由低到高；favored 由高到低；allies 按同队胜率由高到低。",
        }

    async def match_hero(self, cfg, name):
        current = await self.latest(cfg, source="public") or await self.latest(cfg)
        if current:
            return current.index.resolve(name)
        catalog = await self.catalog(cfg)
        if not catalog or not catalog.get("heroes"):
            return None, {"error": "没有已校验的英雄名单，无法核对英雄"}
        return AliasIndex(catalog["heroes"], self.aliases["heroes"]).resolve(name)

    async def pro_matches(self, cfg, *, hero="", **options):
        if hero:
            row, error = await self.match_hero(cfg, hero)
            if error:
                return error
            options["hero_id"] = row["id"]
        return await self.matches.pro(cfg, **options)

    async def item_builds(self, cfg, hero, **options):
        row, error = await self.match_hero(cfg, hero)
        if error:
            return error
        return await self.matches.item_builds(cfg, {
            "id": row["id"], "name": row["display"], "english": row["english"],
        }, **options)

    async def catalog(self, cfg):
        async with self.catalog_lock:
            current = self.catalog_cache
            if not current and self.catalog_path.is_file():
                try:
                    current = await asyncio.to_thread(read_json, self.catalog_path)
                except Exception:
                    current = None
                self.catalog_cache = current
            today = day_key()
            current = dict(current) if isinstance(current, dict) else {}
            if current.get("date"):
                for k in ("patch", "heroes", "items"):
                    if current.get(k):
                        current.setdefault(k + "_date", current["date"])
            if all(
                current.get(k + "_date", current.get("date")) == today
                and current.get(k)
                for k in ("patch", "heroes", "items")
            ) and now()-current.get("patch_checked_at",0) < 3600 and (
                not self.knowledge.checked_patch or current.get("patch")==self.knowledge.checked_patch
            ) and all(current.get(k+"_patch")==current.get("patch") for k in ("heroes","items")):
                return current
            if now() < self.catalog_retry_after:
                return current or None
            p, heroes, items = await asyncio.gather(
                self._fetch(BASE + "patchnoteslist?language=english", cfg),
                self._fetch(BASE + "herolist?language=schinese", cfg),
                self._fetch(BASE + "itemlist?language=schinese", cfg),
                return_exceptions=True,
            )
            fresh = 0
            patch = ""
            if isinstance(p, dict):
                try:
                    patch = latest_patch(p)
                    current.update(patch=patch, patch_date=today, patch_checked_at=now())
                    self.knowledge.checked_patch = patch
                    self.knowledge.checked_at = current["patch_checked_at"]
                    fresh += 1
                except (ValueError, KeyError, TypeError):
                    pass
            for k, payload, element, minimum in (
                ("heroes", heroes, "heroes", 100),
                ("items", items, "itemabilities", 200),
            ):
                if not isinstance(payload, dict):
                    continue
                try:
                    rows = validate_roster(api_rows(payload, element), "hero" if k=="heroes" else "item")
                    current[k] = rows
                    current[k + "_date"] = today
                    current[k + "_patch"] = patch
                    fresh += 1
                except (ValueError, KeyError, TypeError):
                    pass
            if fresh:
                # Each roster and the patch have their own observation date: partial refreshes are not mislabeled as current.
                current.pop("date", None)
                current.setdefault("patch", "unknown")
                self.catalog_cache = current
                try:
                    await asyncio.to_thread(atomic_json, self.catalog_path, current)
                except OSError:
                    self.log.emit("DOTA", "catalog_cache_write_failed", "WARNING")
            if fresh < 3:
                self.catalog_retry_after = now() + 300
            return current or None

    async def _detail(self, cfg, cat, which, row):
        identity = int(row["id"])
        patch = cat.get("patch", "unknown")
        patch_hint = str(patch)
        roster_key = "heroes" if which=="hero" else "items"
        if (not PATCH.fullmatch(str(patch)) or cat.get(roster_key+"_patch")!=patch
            or now()-cat.get("patch_checked_at",0)>3600
            or self.knowledge.checked_patch and self.knowledge.checked_patch!=patch):
            patch = "unknown"
        cache = self.detail_dir / f"{which}-{identity}-{patch}.json"
        async with self.detail_lock:
            prior = None
            candidates=[cache]
            if patch_hint!=patch and PATCH.fullmatch(patch_hint):
                candidates.append(self.detail_dir/f"{which}-{identity}-{patch_hint}.json")
            for candidate in candidates:
                if not candidate.is_file():
                    continue
                try:
                    saved = await asyncio.to_thread(read_json,candidate)
                    validate_detail(saved["data"],row,which,strict=False)
                    if type(saved.get("fetched")) not in (int,float):
                        raise ValueError("缓存时间无效")
                    if not prior or saved["fetched"]>prior["fetched"]:
                        prior=saved
                    if (candidate==cache and saved.get("observed_patch")==patch and
                        now() - saved["fetched"] < cfg["detail_ttl_hours"] * 3600):
                        return saved
                except Exception:
                    continue
            cache_key = (which, identity, patch)
            if self.detail_retry.get(cache_key, 0) > now():
                return prior
            try:
                key = "hero_id" if which == "hero" else "item_id"
                endpoint = "herodata" if which == "hero" else "itemdata"
                raw = await self._fetch(
                    BASE + f"{endpoint}?{key}={identity}&language=schinese", cfg
                )
                values = api_rows(raw, "heroes" if which == "hero" else "items")
                if len(values) != 1:
                    raise ValueError("官方详情与目录 ID 不匹配")
                wrapper = {"fetched": now(), "data": validate_detail(values[0],row,which,strict=False),
                           "observed_patch":patch}
                await asyncio.to_thread(atomic_json, cache, wrapper)
                self.detail_retry.pop(cache_key, None)
                return wrapper
            except Exception:
                self.detail_retry[cache_key] = now() + 300
                if len(self.detail_retry) > 256:
                    self.detail_retry.pop(next(iter(self.detail_retry)))
                return prior

    def _md_lookup(self, path, name, skill="", item=False):
        if not path:
            return None
        p = external_path(path)
        if p.is_dir():
            p = p / "Dota2-Heroes.md"
        if not p.is_file() or p.stat().st_size > 24 * 1024 * 1024:
            return None
        signature = (str(p), p.stat().st_mtime_ns)
        if not self.md_cache or self.md_cache[0] != signature:
            text = p.read_text(encoding="utf-8-sig")
            chunks = []
            for chunk in re.split(r"(?m)(?=^## Dota 2 )", text):
                first = chunk.splitlines()[0] if chunk else ""
                if not first.startswith("## Dota 2 "):
                    continue
                parts = first[3:].split(" · ")
                if len(parts) != 3:
                    continue
                is_item = "物品 ID " in chunk[:700]
                title = parts[2] if is_item else parts[1]
                names = re.split(r"，又称|[（）、]", title)
                keys = {entity_key(x) for x in names if entity_key(x)}
                chunks.append((is_item, keys, parts, chunk[:5000]))
            self.md_cache = (signature, chunks)
        results = [
            (parts, c)
            for is_item, keys, parts, c in self.md_cache[1]
            if is_item == item
            and entity_key(name) in keys
            and (not skill or entity_key(skill) in entity_key(parts[-1]))
        ]
        entities = {x[0][2 if item else 1] for x in results}
        if len(entities) > 1:
            return {
                "error": "旧资料中有多个匹配项",
                "candidates": sorted(entities)[:10],
            }
        if not results:
            return None
        out = {
            "source": "本地旧版 Dota2-Heroes.md",
            "patch": results[0][0][0],
            "note": "本地资料可能落后于当前版本，未核实实时状态；有的技能仅有数值，不能据此补造效果描述。",
            "entries": [c for _, c in results][:12],
        }
        if not item and not skill:
            out["abilities"] = [
                parts[-1]
                for parts, _ in results
                if parts[-1] not in ("基础资料", "天赋树")
            ]
            out["entries"] = [
                c[:1200] for parts, c in results if parts[-1] == "基础资料"
            ]
        return out

    def _known_version(self):
        current = self.knowledge.checked_patch
        if current:
            return current, self.knowledge.checked_at
        cat = self.catalog_cache or {}
        return cat.get("patch",""), cat.get("patch_checked_at",0)

    async def _current_version(self, cfg):
        patch, at = self._known_version()
        if not PATCH.fullmatch(str(patch)) or now()-at>3600:
            await self.catalog(cfg)
            patch, at = self._known_version()
        return (patch if PATCH.fullmatch(str(patch)) else ""), at

    def _snapshot_index(self, snapshot):
        kind = snapshot["kind"]
        return AliasIndex(snapshot["roster"], self.aliases["heroes" if kind=="hero" else "items"], hero=kind=="hero")

    def _item_result(self, idx, row, detail, view="full", all_details=None):
        r = detail or {}
        result = {**idx.label(row["id"]), "code":row["name"]}
        tier = row.get("neutral_item_tier")
        if row["key"].startswith("enhancement_"):
            result["category"] = "中立附魔（可用层级未核实）"
        elif row["key"].startswith("recipe_"):
            result["category"] = "合成图纸"
        elif type(tier) is int and 0 <= tier < 10:
            result["category"] = f"中立物品，第{tier + 1}级（Valve字段）"
        elif row["key"] in ("aegis","cheese","refresher_shard","roshans_banner"):
            result["category"] = "肉山掉落类目录（当前获取状态待核实）"
        elif row.get("is_innate"):
            result["category"] = "英雄专属物品"
        else:
            result["category"] = "Valve 目录物品（当前获取状态以版本为准）"
        result["cost_gold"] = r.get("item_cost")
        result["detail_available"] = bool(detail)
        if r:
            result["effects"] = render_numbers(r,view)
            for key in ("item_initial_charges","item_stock_max","item_stock_time","item_quality","item_neutral_tier"):
                if key in r:
                    result[key] = r[key]
        else:
            result["detail_note"] = "仅目录记录，官方未返回可用详情；不能推断价格、效果或可购买状态。"
        recipe = row if row["key"].startswith("recipe_") else idx.by_key.get("recipe_"+row["key"])
        if recipe:
            result["recipes"] = [
                [idx.label(h)["name"] if h in idx.by_id else "未知物品ID "+str(h)
                 for h in entry.get("items", [])]
                for entry in recipe.get("recipes", []) if isinstance(entry,dict)
            ]
            if all_details:
                price = all_details.get(str(recipe["id"]), {}).get("item_cost")
                if price is not None:
                    result["recipe_cost_gold"] = price
        return result

    def _from_snapshot(self, snapshot, cfg, name, current, at, skill="", section="summary", view="full", offset=0, limit=15):
        idx = self._snapshot_index(snapshot)
        row, err = idx.resolve(name)
        header = self.knowledge.header(snapshot,current,at)
        if err:
            return {**header,**err}
        detail = snapshot["details"].get(str(row["id"]))
        if snapshot["kind"] == "hero":
            detail=with_english_names(detail,snapshot.get("ability_names_en",{}))
            return {**header,"hero":idx.label(row["id"]),**hero_result(detail,cfg,skill,section,view,offset,limit)}
        return {**header,**self._item_result(idx,row,detail,view,snapshot["details"])}

    async def _entity_detail(self, cfg, kind, name, patch="", skill="", section="summary", view="full", offset=0, limit=15):
        requested_patch = ""
        if patch:
            patch = str(patch).strip().lower()
            if not PATCH.fullmatch(patch):
                return {"error":"补丁格式应如 7.42 或 7.42a"}
            snapshot = await self.knowledge.get(kind,patch)
            current, at = self._known_version()
            if snapshot:
                return self._from_snapshot(snapshot,cfg,name,current,at,skill,section,view,offset,limit)
            current, at = await self._current_version(cfg)
            if patch != current or now()-at>3600:
                # Do not substitute today's detail for a requested historical patch.
                return {"error":"没有该补丁的已发布完整资料快照；历史详情不能靠当前接口重建。可查版本公告。",
                        "patch":patch,"available_versions":await asyncio.to_thread(self.knowledge.versions,kind)}
            requested_patch = patch
        current, at = await self._current_version(cfg)
        if current:
            snapshot = await self.knowledge.get(kind,current)
            if snapshot:
                return self._from_snapshot(snapshot,cfg,name,current,at,skill,section,view,offset,limit)
        if not requested_patch and (not current or now()-at>3600):
            # Failed patch check: a previously verified snapshot is a more
            # useful fallback than repeating slow unversioned requests.
            snapshot = await self.knowledge.get(kind)
            if snapshot:
                result=self._from_snapshot(snapshot,cfg,name,current,at,skill,section,view,offset,limit)
                if not result.get("error"):
                    return result
        cat = await self.catalog(cfg)
        roster_key = "heroes" if kind=="hero" else "items"
        if kind=="hero" and (not cat or not cat.get("heroes")):
            stats = await self.latest(cfg) or await self.latest(cfg,source="public") or await self.latest(cfg,source="weekly")
            if stats:
                cat = {**(cat or {}),"heroes_date":stats.day,"heroes":stats.raw["heroes"]}
        err = None
        if cat and cat.get(roster_key):
            current, at = self._known_version()
            idx = AliasIndex(cat[roster_key],self.aliases[roster_key],hero=kind=="hero")
            row, err = idx.resolve(name)
            if row:
                detail = await self._detail(cfg,cat,kind,row)
                if detail or kind=="item" and row["key"].startswith("recipe_"):
                    fetched = detail.get("fetched") if detail else None
                    observed_patch = detail.get("observed_patch","unknown") if detail else cat.get("patch","unknown")
                    header = {
                        "catalog_patch":observed_patch, "current_patch":current or None,
                        "catalog_patch_date":cat.get("patch_date"),
                        "catalog_date":cat.get(roster_key+"_date"),
                        "detail_fetched":stamp(fetched) if fetched else None,
                        "stale":not current or observed_patch!=current or now()-at>3600
                            or not fetched or now()-fetched>cfg["detail_ttl_hours"]*3600,
                        "source":BASE+("herodata?hero_id=" if kind=="hero" else "itemdata?item_id=")+str(row["id"])+"&language=schinese",
                        "note":"按需详情，补丁为抓取附近的目录观察值（非已核验完整版本快照）；升级原始字段不自动相加。",
                    }
                    if requested_patch and (observed_patch!=requested_patch or header["stale"]):
                        return {"error":"未取得该精确补丁的可核实详情；请等待完整资料同步。",
                                "patch":requested_patch}
                    r=detail["data"] if detail else None
                    if kind=="hero":
                        return {**header,"hero":idx.label(row["id"]),**hero_result(r,cfg,skill,section,view,offset,limit)}
                    return {**header,**self._item_result(idx,row,r,view)}
            elif err and err.get("candidates"):
                return err  # An ambiguous real ID must not become a legacy text guess.
        if requested_patch:
            return err or {"error":"该补丁的详情暂时未取得，不能使用其他版本冒充。","patch":requested_patch}
        old = await self.knowledge.get(kind)
        if old:
            result = self._from_snapshot(old,cfg,name,current,at,skill,section,view,offset,limit)
            if not result.get("error"):
                result["note"] += " 当前详情未取得，使用最近已发布版本。"
                result["stale"] = True
                return result
        fallback = await asyncio.to_thread(self._md_lookup,cfg["existing_catalog_md"],name,skill,kind=="item")
        return fallback or err or {"error":"无法获取官方详情。稍后重试，或配置旧本地目录。"}

    async def hero(self, cfg, name, skill="", *, patch="", section="summary", view="full", offset=0, limit=15):
        return await self._entity_detail(cfg,"hero",name,patch,skill,section,view,offset,limit)

    async def item(self, cfg, name, *, patch="", view="full"):
        return await self._entity_detail(cfg,"item",name,patch,view=view)

    async def mechanics(self, cfg, *, kind="hero_ability", hero="", patch="", limit=15, offset=0, **filters):
        if kind not in ("hero_ability","item","any") or hero and kind!="hero_ability":
            return {"error":"请明确查询英雄技能或物品；hero 筛选仅用于英雄技能。"}
        meaningful = set(filters)-{"include_upgrades"}
        if not meaningful and not hero:
            return {"error":"至少需要一项机制、属性、数值条件或英雄。"}
        kinds = ("hero","item") if kind=="any" else (("hero",) if kind=="hero_ability" else ("item",))
        current, at = self._known_version() if patch else await self._current_version(cfg)
        if patch:
            versions = [str(patch).lower()]
            if not PATCH.fullmatch(versions[0]):
                return {"error":"无效补丁版本"}
        else:
            available = await asyncio.gather(*(asyncio.to_thread(self.knowledge.versions,k) for k in kinds))
            common = set(available[0]).intersection(*available[1:])
            versions = sorted(common,key=patch_key,reverse=True)
            if current in common:
                versions.remove(current)
                versions.insert(0,current)
        snapshots = []
        for version in versions:
            loaded = await asyncio.gather(*(self.knowledge.get(k,version) for k in kinds))
            if all(loaded):
                snapshots = loaded
                break
        if not snapshots:
            return {"error":"条件查询需要同一补丁已验收的完整目录。请在管理中心同步 Dota 资料，不能用部分数据冒充全英雄。",
                    "patch":patch or current,"sync":await self.knowledge.status()}
        header = self.knowledge.header(snapshots[0],current,at)
        # All rows considered by the filter belong to one exact published patch.
        def search():
            records, counts = [], {"heroes":0,"abilities":0,"items":0,"item_metadata_only":0}
            for s in snapshots:
                idx = self._snapshot_index(s)
                rows = list(idx.by_id.values())
                if s["kind"]=="hero" and hero:
                    row, err = idx.resolve(hero)
                    if err:
                        return err
                    rows = [row]
                counts["item_metadata_only"] += len(s["unavailable"])
                for row in sorted(rows,key=lambda r:r["id"]):
                    detail = s["details"].get(str(row["id"]))
                    if not detail:
                        continue
                    if s["kind"]=="hero":
                        counts["heroes"] += 1
                        abilities = [a for a in named_abilities(detail) if ability_type(a)!="hidden"]
                        counts["abilities"] += len(abilities)
                    else:
                        abilities = [detail]
                        counts["items"] += 1
                    for a in abilities:
                        if not a.get("name_loc") or a.get("name","").startswith("item_recipe_"):
                            continue
                        found=filter_mechanic(a,filters)
                        if found:
                            if s["kind"]=="hero":
                                found["hero"]=idx.label(row["id"])
                            else:
                                found["item"]=idx.label(row["id"])
                            records.append(found)
            selected=records[offset:offset+limit]
            return {"matches":selected,"matched":len(records),"offset":offset,
                    "next_offset":offset+len(selected) if offset+len(selected)<len(records) else None,
                    "coverage":counts,"filters":filters}
        result = await asyncio.to_thread(search)
        return {**header,**result,
                "quality":"description_candidates" if filters.get("effect") else "structured_fields",
                "mechanics_note":"全部条件 AND；冷却/施法距离按最高提供基础等级，不含天赋/升级。官方字段不代表全部子效果；文本标签是有原文依据的候选，可能漏判或误判，不保证穷尽实战机制。"}

    async def _patch_entity(self, cfg, kind, name, patch="", extra_snapshot=None):
        snapshot = await self.knowledge.get(kind,patch) if patch else None
        if snapshot:
            idx = self._snapshot_index(snapshot)
            row, err = idx.resolve(name)
            if row or err.get("candidates"):
                return row,idx,err
        if extra_snapshot:
            idx = self._snapshot_index(extra_snapshot)
            row, err = idx.resolve(name)
            if row or err.get("candidates"):
                return row,idx,err
        cat = await self.catalog(cfg)
        key = "heroes" if kind=="hero" else "items"
        if cat and cat.get(key):
            idx=AliasIndex(cat[key],self.aliases[key],hero=kind=="hero")
            row,err=idx.resolve(name)
            return row,idx,err
        return None,None,{"error":"缺少可用于解析名称的官方目录"}

    @staticmethod
    def _changes(before, after, kind):
        if not before or not after:
            return [{"field":"entity","before":"存在" if before else "无详情","after":"存在" if after else "无详情"}]
        fields = (*NUM_FIELDS,"desc_loc","notes_loc","scepter_loc","shard_loc","damage","immunity","dispellable",
                  "behavior","target_team","target_type","max_level","type","ability_is_innate",
                  "ability_has_scepter","ability_has_shard","ability_is_granted_by_scepter",
                  "ability_is_granted_by_shard","item_cost","item_neutral_tier")
        changes=[]
        def diff_row(b,a,code,keys):
            for field in keys:
                if b.get(field)!=a.get(field):
                    changes.append({"code":code,"field":field,"before":b.get(field),"after":a.get(field)})
            bs={s["name"]:s for s in specials_of(b)}
            aa={s["name"]:s for s in specials_of(a)}
            for key in sorted(set(bs)|set(aa)):
                if bs.get(key)!=aa.get(key):
                    changes.append({"code":code,"field":"special_values."+key,"before":bs.get(key),"after":aa.get(key)})
        if kind=="item":
            diff_row(before,after,after.get("name"),fields)
        else:
            diff_row(before,after,after.get("name"),ATTRIBUTES)
            a,b = ({x["name"]:x for x in named_abilities(obj)} for obj in (after,before))
            for code in sorted(set(a)|set(b)):
                if code not in a or code not in b:
                    changes.append({"code":code,"field":"ability",
                                    "before":html_text(b[code].get("name_loc")) if code in b else None,
                                    "after":html_text(a[code].get("name_loc")) if code in a else None})
                else:
                    diff_row(b[code],a[code],code,fields)
            x,y=talents_of(before),talents_of(after)
            if x!=y:
                changes.append({"code":after.get("name"),"field":"talents","before":x,"after":y})
        return changes

    async def patch_changes(self,cfg,*,patch="",hero="",item="",from_patch="",offset=0,limit=15):
        if hero and item:
            return {"error":"请分别查询单个英雄或物品"}
        if not patch or patch=="latest":
            patch, at=await self._current_version(cfg)
            if not patch or now()-at>3600:
                return {"error":"暂时无法核实最新补丁号，请指定版本。"}
        patch=str(patch).lower()
        if not PATCH.fullmatch(patch) or (from_patch and from_patch != "previous" and
                                        not PATCH.fullmatch(str(from_patch).lower())):
            return {"error":"补丁格式应如 7.42 或 7.42a"}
        kind="hero" if hero else "item"
        name=hero or item
        if from_patch == "previous" and name:
            try:
                official = patch_history(await self._fetch(BASE+"patchnoteslist?language=english", cfg))
                from_patch = official[official.index(patch) + 1]
            except (ValueError, IndexError, aiohttp.ClientError, OSError, asyncio.TimeoutError):
                return {"patch": patch, "error": "Valve 版本目录无法核实该补丁的前一版，可指定精确补丁。"}
        if from_patch:
            if not name:
                return {"error":"跨版数值对比请指定一个英雄或物品"}
            from_patch=from_patch.lower()
            before,after=await asyncio.gather(self.knowledge.get(kind,from_patch),self.knowledge.get(kind,patch))
            if not before or not after:
                return {"error":"需要两个精确补丁的完整快照；不能从当前资料反推旧数值。可分别查版本公告。",
                        "from_patch":from_patch,"patch":patch,
                        "available_versions":await asyncio.to_thread(self.knowledge.versions,kind)}
            row,idx,err=await self._patch_entity(cfg,kind,name,patch,before)
            if err:
                return err
            code=row["name"]
            def find(s):
                match=next((r for r in s["roster"] if r["name"]==code),None)
                return s["details"].get(str(match["id"])) if match else None
            changes=await asyncio.to_thread(self._changes,find(before),find(after),kind)
            return {"patch":patch,"from_patch":from_patch,"entity":idx.label(row["id"]),
                    "source":"两个已验收的 Valve 版本快照字段差异","offset":offset,"total":len(changes),
                    "changes":changes[offset:offset+limit],"next_offset":offset+limit if offset+limit<len(changes) else None,
                    "note":"字段差异并非官方改动清单；不保证包括所有运行时机制。"}
        notes=await self.knowledge.notes(cfg,patch)
        if not notes:
            return {"error":"未取得这个补丁的可解析官方公告（中文/英文及本地备用）。请稍后重试或检查本地 JSON。",
                    "patch":patch}
        header={"patch":patch,"source":notes["source"],"published_date":notes.get("published_date"),
                "note":"指定补丁的公告，不等于完整历史数据。未列出的细节不能据此推断。"}
        if name:
            key="heroes" if kind=="hero" else "items"
            if not notes["sections"].get(key):
                return {**header,"error":"已获得公告，但对应栏目未提供，不能认定没有改动。"}
            row,idx,err=await self._patch_entity(cfg,kind,name,patch)
            if err:
                return {**header,**err}
            groups=notes["hero_notes" if kind=="hero" else "item_notes"]
            keys=(str(row["id"]),row["name"],row["key"])
            lines=next((groups[k] for k in keys if k in groups),[])
            if kind=="hero" and lines:
                snapshot=await self.knowledge.get("hero",patch)
                if snapshot:
                    ability_names={a.get("id"):html_text(a.get("name_loc"))
                        for a in named_abilities(snapshot["details"].get(str(row["id"]),{}))}
                    lines=[{**x,**({"ability":ability_names[x["ability_id"]]} if x.get("ability_id") in ability_names else {})} for x in lines]
            header["entity"]=idx.label(row["id"])
        else:
            if not notes["sections"].get("general"):
                return {**header,"error":"这份公告未提供通用改动栏目。可指定英雄或物品。"}
            lines=[*notes["general"]]
            header.update(affected_heroes=len(notes["hero_notes"]),affected_items=len(notes["item_notes"]))
            header["note"]+=" 全局查询只返回通用公告；逐英雄、物品查询改动明细。"
        return {**header,"changes":lines[offset:offset+limit],"offset":offset,"total":len(lines),
                "next_offset":offset+limit if offset+limit<len(lines) else None}
