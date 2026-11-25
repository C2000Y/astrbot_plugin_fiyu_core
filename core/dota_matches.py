"""Bounded read-only OpenDota samples; account identifiers are never returned."""
from __future__ import annotations

import asyncio
from collections import Counter, OrderedDict

from .research_sources import PRO_LIST, PRO_MATCH
from .util import atomic_json, clean, day_key, entity_key, now, read_json, stamp

API = "https://api.opendota.com/api/"
ITEMS = API + "constants/items"
PATCHES = API + "constants/patch"
PHASES = {
    "start": ("start_game_items", "≤0:00，价格≤600"),
    "early": ("early_game_items", "0:00<时间<10:00，价格≥500"),
    "mid": ("mid_game_items", "10:00≤时间<25:00，价格≥1000"),
    "late": ("late_game_items", "时间≥25:00，价格≥2000"),
}
LIMITATIONS = "OpenDota 公开样本，非全部对局；样本外比赛、未解析记录及缺字段均不可推断。"
SAMPLE_SOURCES = ("opendota_pro", "opendota_match_detail", "opendota_item_popularity",
                  "opendota_item_names", "opendota_patch_names")


def int_between(value, low, high):
    return type(value) is int and low <= value <= high


def label(value):
    return clean(value, 100, urls=True) if isinstance(value, str) else ""


def normalize_list(data, hero_id=None, *, require_league=True):
    if not isinstance(data, list) or len(data) > 100:
        raise ValueError("OpenDota 样本列表类型或数量异常")
    rows = {}
    for item in data:
        if not isinstance(item, dict):
            continue
        mid, ts = item.get("match_id"), item.get("start_time")
        won, league = item.get("radiant_win"), item.get("leagueid")
        if not (int_between(mid, 1, 10**15) and int_between(ts, 1300000000, int(now()) + 300)
                and type(won) is bool and (not require_league or int_between(league, 1, 10**9))):
            continue
        row = dict(match_id=mid, match_date=day_key(ts), starts_at=stamp(ts), start_time=ts,
                   league_id=league, league_name=label(item.get("league_name")), radiant_win=won,
                   radiant_team=label(item.get("radiant_name")), dire_team=label(item.get("dire_name")))
        slot = item.get("player_slot")
        if hero_id and type(slot) is int and slot in (*range(5), *range(128, 133)):
            side = slot < 128
            row.update(hero_id=hero_id, hero_side="radiant" if side else "dire", hero_won=won == side)
        rows[mid] = row
    if data and not rows:
        raise ValueError("OpenDota 职业样本无有效记录")
    return sorted(rows.values(), key=lambda r: (r["start_time"], r["match_id"]), reverse=True)


def normalize_match(data, match_id):
    if not isinstance(data, dict) or data.get("match_id") != match_id:
        raise ValueError("比赛明细不是所查询的比赛")
    league = data.get("league")
    league = league if isinstance(league, dict) else {}
    result = normalize_list([{
        **data, "league_name": league.get("name"),
        **{
            side + "_name": data[side + "_team"].get("name")
            for side in ("radiant", "dire") if isinstance(data.get(side + "_team"), dict)
        },
    }], require_league=False)[0]
    result["league_tier"] = label(league.get("tier"))
    if int_between(data.get("patch"), 0, 10000):
        result["patch_id"] = data["patch"]
    lineup = {}
    for player in (data.get("players") if isinstance(data.get("players"), list) else []):
        if not isinstance(player, dict):
            continue
        hid, slot = player.get("hero_id"), player.get("player_slot")
        if not int_between(hid, 1, 2000) or type(slot) is not int or slot not in (*range(5), *range(128, 133)):
            continue
        purchase = player.get("purchase_log")
        purchases = None
        if isinstance(purchase, list) and len(purchase) <= 500:
            purchases = [
                {"item_key": item["key"], "time_seconds": item["time"]}
                for item in purchase if isinstance(item, dict)
                and int_between(item.get("time"), -3600, 14400)
                and isinstance(item.get("key"), str)
                and item["key"].isascii() and item["key"].replace("_", "").isalnum()
                and len(item["key"]) <= 64
            ]
        if slot in lineup:
            raise ValueError("阵容位置重复")
        lineup[slot] = {"hero_id": hid, "side": "radiant" if slot < 128 else "dire",
                        "purchases": purchases, "lane_role": player.get("lane_role")
                        if int_between(player.get("lane_role"), 1, 4) else None}
    result["lineup"] = [lineup[k] for k in sorted(lineup)]
    draft = {}
    for action in (data.get("picks_bans") if isinstance(data.get("picks_bans"), list) else []):
        if not isinstance(action, dict):
            continue
        hid, order, team, pick = (action.get(k) for k in ("hero_id", "order", "team", "is_pick"))
        if (int_between(hid, 1, 2000) and int_between(order, 0, 47)
                and int_between(team, 0, 1) and type(pick) is bool):
            if order in draft:
                raise ValueError("BP 顺序重复")
            draft[order] = {"hero_id": hid, "order": order, "is_pick": pick,
                            "side": "radiant" if team == 0 else "dire"}
    result["draft"] = [draft[k] for k in sorted(draft)]
    result["draft_full_lineup"] = len(lineup) == 10 and (
        {(p["hero_id"], p["side"]) for p in lineup.values()} ==
        {(p["hero_id"], p["side"]) for p in draft.values() if p["is_pick"]}
    )
    return result


def normalize_popularity(data):
    if not isinstance(data, dict) or any(not isinstance(data.get(k), dict) for k, _ in PHASES.values()):
        raise ValueError("OpenDota 购买频次阶段数据不完整")
    values = {}
    for phase, (key, _) in PHASES.items():
        rows = []
        for iid, count in data[key].items():
            if not (isinstance(iid, str) and iid.isascii() and iid.isdecimal() and
                    len(iid) <= 6 and int_between(int(iid), 1, 999999)
                    and int_between(count, 1, 50000)):
                raise ValueError("OpenDota 购买频次的物品或次数无效")
            rows.append((int(iid), count))
        values[phase] = sorted(rows, key=lambda r: (-r[1], r[0]))
    return values


class DotaMatches:
    def __init__(self, root, fetch, log):
        self.fetch, self.log = fetch, log
        self.cache = OrderedDict()
        self.lock = asyncio.Lock()
        self.pro_path = root / "open_dota_pro_sample.json"
        self.health_path = root / "opendota_sample_health.json"
        self.failures = self._read_health()

    def _read_health(self):
        try:
            if self.health_path.stat().st_size > 8192:
                return {}
            saved = read_json(self.health_path)
        except (OSError, ValueError):
            return {}
        if not isinstance(saved, dict):
            return {}
        return {
            source: {"reason": label(record.get("reason")), "retry_after": record["retry_after"]}
            for source in SAMPLE_SOURCES if isinstance(record := saved.get(source), dict)
            and type(record.get("retry_after")) in (int, float) and 0 < record["retry_after"] <= now() + 86400
        }

    async def _save_health(self):
        try:
            await asyncio.to_thread(atomic_json, self.health_path, self.failures)
        except OSError:
            self.log.emit("DOTA", "sample_health_write_failed", "WARNING")

    async def _get(self, source, url, cfg, parser, ttl=900):
        # Source and URL are built only here from fixed providers and validated numeric IDs.
        async with self.lock:
            prior = self.cache.get(url)
            if prior is None and url == PRO_LIST:
                prior = await self._read_pro_cache()
                if prior:
                    self.cache[url] = prior
            failure = self.failures.get(source)
            if prior and now() - prior["checked_at"] < ttl:
                return prior
            if failure and failure["retry_after"] > now():
                return prior
            try:
                value = parser(await self.fetch(url, cfg))
            except Exception as exc:
                invalid = isinstance(exc, (ValueError, KeyError, TypeError, IndexError))
                # HTTPFailure is a ValueError with a known numeric status.
                status = getattr(exc, "status", None)
                if int_between(status, 100, 599):
                    reason, retry = f"OpenDota HTTP {status}", 900 if status == 429 or status >= 500 else 14400
                elif invalid:
                    reason, retry = "OpenDota 数据未通过校验", 14400
                else:
                    reason, retry = "OpenDota 连接或请求失败", 900
                self.failures[source] = {"reason": reason, "retry_after": now() + retry}
                await self._save_health()
                self.log.emit("DOTA", "sample_failed", "WARNING", task=source, reason=reason)
                return prior
            if self.failures.pop(source, None):
                await self._save_health()
            result = {"source": url, "observed_at": stamp(), "checked_at": now(), "ttl": ttl, "data": value}
            self.cache[url] = result
            self.cache.move_to_end(url)
            while len(self.cache) > 48:
                self.cache.popitem(last=False)
            if url == PRO_LIST:
                try:
                    await asyncio.to_thread(atomic_json, self.pro_path, result)
                except OSError:
                    self.log.emit("DOTA", "sample_cache_write_failed", "WARNING")
            return result

    async def _pro_sample(self, cfg, hero_id=None):
        source = API + f"heroes/{hero_id}/matches" if hero_id else PRO_LIST
        return await self._get("opendota_pro", source, cfg, lambda raw: normalize_list(raw, hero_id))

    async def _read_pro_cache(self):
        try:
            if self.pro_path.stat().st_size > 512 * 1024:
                return None
            result = await asyncio.to_thread(read_json, self.pro_path)
        except (ValueError, OSError):
            return None
        if (not isinstance(result, dict) or result.get("source") != PRO_LIST
                or not isinstance(result.get("observed_at"), str)
                or type(result.get("checked_at")) not in (int, float)
                or not 0 < result["checked_at"] <= now() + 300
                or not isinstance(result.get("data"), list) or len(result["data"]) > 100):
            return None
        try:
            rows = normalize_list([{
                "match_id": row["match_id"], "start_time": row["start_time"],
                "leagueid": row["league_id"], "league_name": row["league_name"],
                "radiant_win": row["radiant_win"], "radiant_name": row["radiant_team"],
                "dire_name": row["dire_team"],
            } for row in result["data"]])
        except (ValueError, TypeError, KeyError):
            return None
        return {"source": PRO_LIST, "observed_at": stamp(result["checked_at"]),
                "checked_at": result["checked_at"], "ttl": 900, "data": rows}

    async def cached_pro_sample(self):
        result = self.cache.get(PRO_LIST) or await self._read_pro_cache()
        if not result:
            return None
        return {**self._header(result, "opendota_pro"), "matches": result["data"]}

    def _header(self, entry, source):
        return {k: entry[k] for k in ("source", "observed_at")} | {
            "stale": now() - entry["checked_at"] >= entry.get("ttl", 900),
            **self.failures.get(source, {}),
        }

    async def status(self):
        if PRO_LIST not in self.cache:
            prior = await self._read_pro_cache()
            if prior:
                self.cache.setdefault(PRO_LIST, prior)
        rows = []
        for source, filter_url in (
            ("opendota_pro", "/proMatches"),
            ("opendota_match_detail", "/matches/"),
            ("opendota_item_popularity", "/itemPopularity"),
        ):
            entry = next((v for k, v in reversed(self.cache.items()) if filter_url in k), None)
            failure = self.failures.get(source)
            if failure:
                state = "冷却中" if now() < failure["retry_after"] else "可重试"
            else:
                state = "有已验证样本" if entry else "待按需查询"
            row = {"source": source, "state": state, **(failure or {})}
            if entry:
                row.update(observed_at=entry["observed_at"], stale=now() - entry["checked_at"] >= entry["ttl"])
                if source == "opendota_pro":
                    row["sample_count"] = len(entry["data"])
            rows.append(row)
        return rows

    async def _match(self, cfg, mid):
        return await self._get("opendota_match_detail", PRO_MATCH + str(mid), cfg,
                               lambda data: normalize_match(data, mid), ttl=86400)

    async def _patch_names(self, cfg):
        def parse(data):
            if not isinstance(data, list) or not data:
                raise ValueError("补丁映射为空")
            return {
                row["id"]: label(row["name"])
                for row in data if isinstance(row, dict) and int_between(row.get("id"), 0, 10000)
                and isinstance(row.get("name"), str)
            }
        result = await self._get("opendota_patch_names", PATCHES, cfg, parse, ttl=86400)
        return result["data"] if result else {}

    @staticmethod
    def _visible_match(row, patch_names):
        result = {key: value for key, value in row.items() if key not in ("lineup", "draft")}
        if "lineup" in row:
            result["lineup"] = [{"hero_id": r["hero_id"], "side": r["side"]} for r in row["lineup"]]
            result["draft"] = row["draft"]
            result["patch"] = patch_names.get(row.get("patch_id"))
        return result

    async def pro(self, cfg, *, hero_id=None, team="", league="", days=30, patch="", match_id=None,
                  include_draft=False, limit=5):
        if match_id:
            if not int_between(match_id, 1, 10**15):
                return {"error": "无效比赛 ID"}
            detail = await self._match(cfg, match_id)
            if not detail or detail["data"]["league_tier"] not in ("professional", "premium"):
                return {"error": "未能核实该 ID 为 OpenDota 职业联赛明细。", "source": PRO_MATCH + str(match_id)}
            names = await self._patch_names(cfg)
            row = detail["data"]
            if (team and entity_key(team) not in (entity_key(row["radiant_team"]), entity_key(row["dire_team"]))
                    or league and entity_key(league) not in entity_key(row["league_name"])
                    or hero_id and not any(p["hero_id"] == hero_id for p in row["lineup"])):
                return {"error": "比赛明细没有核实所指定的战队、联赛或英雄"}
            if patch and names.get(row.get("patch_id")) != patch:
                return {"error": "比赛补丁与指定补丁不一致或未核实"}
            return {**self._header(detail, "opendota_match_detail"), "sample_count": 1,
                    "rows": [self._visible_match(row, names)], "note": LIMITATIONS}

        sample = await self._pro_sample(cfg, hero_id if not team else None)
        if not sample:
            return {"source": PRO_LIST, "error": "OpenDota 职业样本暂不可用", **self.failures.get("opendota_pro", {})}

        def matches_filters(row):
            return (row["start_time"] >= now() - days * 86400
                    and (not league or entity_key(league) in entity_key(row["league_name"]))
                    and (not team or entity_key(team) in (entity_key(row["radiant_team"]),
                                                         entity_key(row["dire_team"]))))

        window = sample["data"]
        rows = [r for r in window if matches_filters(r)]
        inspected = []
        names = {}
        detail_scan = bool(patch or team and hero_id)
        if detail_scan or include_draft:
            details = await asyncio.gather(*(self._match(cfg, r["match_id"]) for r in rows[:3]))
            inspected = [x["data"] for x in details if x]
            names = await self._patch_names(cfg) if inspected else {}
        if detail_scan:
            rows = [
                r for r in inspected if (not patch or names.get(r.get("patch_id")) == patch)
                and (not hero_id or any(p["hero_id"] == hero_id for p in r["lineup"]))
            ]
        lineup_sample = [r for r in (rows if detail_scan else inspected) if len(r.get("lineup", ())) == 10]
        picked, won, banned = Counter(), Counter(), Counter()
        for match in lineup_sample:
            for hero in match["lineup"]:
                hid = hero["hero_id"]
                picked[hid] += 1
                if match["radiant_win"] == (hero["side"] == "radiant"):
                    won[hid] += 1
        draft_sample = [r for r in (rows if detail_scan else inspected) if r.get("draft")]
        for match in draft_sample:
            banned.update({r["hero_id"] for r in match["draft"] if not r["is_pick"]})
        hero_summary = [
            {"hero_id": hid, "picks": picked[hid], "wins": won[hid],
             "win_rate_pct": round(100 * won[hid] / picked[hid], 2) if picked[hid] else None,
             "bans": banned[hid]}
            for hid in sorted(picked.keys() | banned.keys(), key=lambda h: (-picked[h], -banned[h], h))
        ]
        display = {r["match_id"]: r for r in inspected}
        result = {
            **self._header(sample, "opendota_pro"),
            "source_sample_count": len(window), "sample_count": len(rows), "days": days,
            "earliest_available_date": window[-1]["match_date"] if window else None,
            "latest_available_date": window[0]["match_date"] if window else None,
            "rows": [self._visible_match({**r, **display.get(r["match_id"], {})}, names) for r in rows[:limit]],
            "note": LIMITATIONS,
        }
        if detail_scan or include_draft:
            result.update(checked_match_details=len(inspected), lineup_sample_count=len(lineup_sample),
                          draft_sample_count=len(draft_sample), hero_rows=hero_summary[:limit],
                          detail_scope="仅尝试读取命中基础条件的最近三份明细，禁用次数仅取已记录的 BP")
        if hero_id and not detail_scan:
            known = [r for r in rows if "hero_won" in r]
            wins = sum(r["hero_won"] for r in known)
            result["hero_record"] = {"id": hero_id, "known_games": len(known), "wins": wins,
                                     "win_rate_pct": round(100 * wins / len(known), 2) if known else None}
        if team:
            known = [r for r in rows if entity_key(team) in (entity_key(r["radiant_team"]), entity_key(r["dire_team"]))]
            wins = sum(r["radiant_win"] == (entity_key(team) == entity_key(r["radiant_team"])) for r in known)
            result["team_record"] = {"games": len(known), "wins": wins,
                                     "win_rate_pct": round(100 * wins / len(known), 2) if known else None}
        return result

    async def item_builds(self, cfg, hero, *, phase="all", match_id=None, position=None, bracket=None,
                          patch="", days=None, limit=8, offset=0):
        hid = hero["id"]
        if position is not None or bracket is not None or (not match_id and (patch or days is not None)):
            return {"error": "购买频次接口无法按位置、段位、补丁或日期交叉筛选。"
                    "可查英雄聚合购买频次，或指定真实比赛 ID 查询该局购买时间与顺序。"}
        if match_id:
            if not int_between(match_id, 1, 10**15):
                return {"error": "无效比赛 ID"}
            detail = await self._match(cfg, match_id)
            if not detail:
                return {"error": "未获得已校验比赛明细", "source": PRO_MATCH + str(match_id)}
            if days is not None and detail["data"]["start_time"] < now() - days * 86400:
                return {"error": "该比赛超出指定日期范围"}
            entries = [r for r in detail["data"]["lineup"] if r["hero_id"] == hid]
            if len(entries) != 1 or entries[0]["purchases"] is None:
                return {"error": "该比赛缺少唯一可核验的英雄购买记录", "source": detail["source"]}
            entry = entries[0]
            purchases = sorted(entry["purchases"], key=lambda item: item["time_seconds"])
            names = await self._patch_names(cfg)
            known_patch = names.get(detail["data"].get("patch_id"))
            if patch and known_patch != patch:
                return {"error": "比赛补丁与指定补丁不一致或未核实"}
            return {**self._header(detail, "opendota_match_detail"), "hero": hero, "match_id": match_id,
                    "match_date": detail["data"]["match_date"], "patch": known_patch,
                    "purchase_count": len(purchases), "offset": offset, "rows": purchases[offset:offset + limit],
                    "next_offset": offset + limit if offset + limit < len(purchases) else None,
                    "lane_role": entry["lane_role"], "lane_role_source": "OpenDota 解析的 lane_role 原始编号",
                    "note": "实际解析的购买事件按游戏秒数排序，同秒保留原顺序；时间小于零为开局前。"
                            "位置 1～5 和该场段位未核实，位置字段仅为原始 lane_role。"}
        url = API + f"heroes/{hid}/itemPopularity"
        entry = await self._get("opendota_item_popularity", url, cfg, normalize_popularity, ttl=3600)
        if not entry:
            return {"source": url, "error": "OpenDota 购买频次暂不可用",
                    **self.failures.get("opendota_item_popularity", {})}
        counts = entry["data"]
        constants = await self._get("opendota_item_names", ITEMS, cfg, normalize_items, ttl=86400)
        names = constants["data"] if constants else {}
        phases = {name: desc for name, (_, desc) in PHASES.items() if phase in ("all", name)}
        rows = [
            {"phase": name, "item_id": item_id, "name": names.get(item_id), "purchases": frequency}
            for name in phases for item_id, frequency in counts[name][:limit]
        ]
        return {**self._header(entry, "opendota_item_popularity"), "hero": hero, "phases": phases,
                "sample_matches_max": 100, "sample_matches": None, "rows": rows,
                "note": "OpenDota 最多读取该英雄最近 100 条已解析对局购买日志。计数是购买次数，"
                        "可重复购买，精确对局数、日期、位置、段位和补丁均未返回。"
                        "分阶段购买频次只描述样本，无法恢复单局购买顺序或推断推荐出装。"}

    async def probe(self, cfg):
        sample = await self._pro_sample(cfg)
        if sample and sample["data"]:
            await self._match(cfg, sample["data"][0]["match_id"])
        await self._get("opendota_item_popularity", API + "heroes/1/itemPopularity", cfg,
                        normalize_popularity, ttl=3600)
        return await self.status()


def normalize_items(data):
    if not isinstance(data, dict):
        raise ValueError("OpenDota 物品目录无效")
    return {
        item["id"]: label(item.get("dname")) for item in data.values()
        if isinstance(item, dict) and int_between(item.get("id"), 1, 999999)
    }
