"""Resumable, patch-bound Valve knowledge snapshots; no LLM or embedding calls."""
from __future__ import annotations

import asyncio
import os
import re
import threading
from datetime import datetime, timezone
from pathlib import Path

from .dota_details import CODE, PATCH, named_abilities
from .util import atomic_json, day_key, dump, html_text, now, read_json, stamp

BASE = "https://www.dota2.com/datafeed/"
KIND = {
    "hero": ("herolist", "heroes", "herodata", "hero_id", "heroes", "npc_dota_hero_", 100),
    "item": ("itemlist", "itemabilities", "itemdata", "item_id", "items", "item_", 200),
}


def patch_key(value):
    match = re.fullmatch(r"(\d+)\.(\d+)([a-z]*)", value)
    return (int(match[1]), int(match[2]), match[3]) if match else (0,0,"")


def latest_patch(payload):
    if not isinstance(payload, dict) or ("success" in payload and payload["success"] not in (True,1)):
        raise ValueError("版本目录请求失败")
    patches = payload.get("patches")
    if not isinstance(patches, list) or not patches:
        raise ValueError("版本目录为空")
    valid = [
        p for p in patches if isinstance(p, dict)
        and isinstance(p.get("patch_number"), str) and PATCH.fullmatch(p["patch_number"])
        and type(p.get("patch_timestamp")) is int
    ]
    if not valid:
        raise ValueError("没有可验证补丁")
    newest = max(valid, key=lambda p: (p["patch_timestamp"], patch_key(p["patch_number"])))
    return newest["patch_number"]


def api_rows(payload, key):
    if not isinstance(payload, dict):
        raise ValueError("官方响应格式异常")
    result = payload.get("result", {})
    if result.get("status", 1) != 1:
        raise ValueError("官方详情状态异常")
    rows = result.get("data", {}).get(key)
    if not isinstance(rows, list):
        raise ValueError(key+" 数据缺失")
    return rows


def validate_roster(rows, kind):
    prefix, minimum = KIND[kind][-2:]
    if not isinstance(rows, list) or not minimum <= len(rows) <= 5000:
        raise ValueError("官方目录数量异常")
    seen_ids, seen_codes = set(), set()
    for r in rows:
        if (not isinstance(r, dict) or type(r.get("id")) is not int or not 0 < r["id"] < 1000000
            or not isinstance(r.get("name"), str) or not r["name"].startswith(prefix)
            or not CODE.fullmatch(r["name"]) or r["id"] in seen_ids or r["name"] in seen_codes):
            raise ValueError("官方目录 ID/代码不唯一")
        seen_ids.add(r["id"])
        seen_codes.add(r["name"])
    return rows


def validate_detail(data, row, kind, strict=True):
    if not isinstance(data, dict) or data.get("id") != row["id"] or data.get("name") != row["name"]:
        raise ValueError("官方详情与目录 ID/代码不匹配")
    if kind == "hero":
        abilities = data.get("abilities")
        if not isinstance(abilities, list) or not named_abilities(data):
            raise ValueError("英雄技能数据为空")
        if strict and (not isinstance(data.get("talents"), list)
                       or not any(isinstance(a.get("desc_loc"), str) and a["desc_loc"].strip()
                                  for a in named_abilities(data))):
            raise ValueError("英雄描述/天赋数据缺失")
        if strict:
            codes=[a["name"] for a in named_abilities(data)]
            if len(codes)!=len(set(codes)):
                raise ValueError("英雄技能代码重复")
    elif strict and not any(k in data for k in ("desc_loc", "special_values", "item_cost")):
        raise ValueError("物品详情字段缺失")
    return data


def enough_items(details, roster):
    # The official roster contains obsolete/special-mode entries too. Require
    # a substantial valid detail set, but don't invent availability of these
    # entries or assume an arbitrary 80% of them are current shop items.
    return len(details) >= min(200,len(roster)//2)


def validate_snapshot(data, kind, patch, published=True):
    if (not isinstance(data, dict) or data.get("schema") != 1 or data.get("kind") != kind
        or data.get("patch") != patch or not PATCH.fullmatch(patch)):
        raise ValueError("资料快照标识错误")
    roster = validate_roster(data.get("roster"), kind)
    by_id = {str(r["id"]): r for r in roster}
    details, unavailable = data.get("details"), data.get("unavailable")
    if not isinstance(details, dict) or not isinstance(unavailable, list) or any(not isinstance(x,str) for x in unavailable):
        raise ValueError("资料快照记录异常")
    ids = set(details) | set(unavailable)
    if (not ids <= set(by_id) or set(details) & set(unavailable)
        or len(unavailable) != len(set(unavailable)) or (kind == "hero" and unavailable)):
        raise ValueError("资料快照覆盖关系有误")
    if published and (ids != set(by_id) or not data.get("complete")
                      or (kind == "item" and not enough_items(details,roster))):
        raise ValueError("资料快照未完成")
    for key, value in details.items():
        validate_detail(value, by_id[key], kind)
    english = data.get("ability_names_en", {})
    if not isinstance(english,dict) or len(english)>10000 or any(
        not isinstance(key,str) or not key.isdecimal() or len(key)>8
        or not isinstance(value,dict) or not isinstance(value.get("code"),str)
        or not CODE.fullmatch(value["code"]) or not isinstance(value.get("name"),str)
        or not 0<len(value["name"])<=240
        for key,value in english.items()
    ):
        raise ValueError("英文技能索引格式异常")
    return data


class KnowledgeStore:
    def __init__(self, service, root: Path, log):
        self.service, self.root, self.log = service, root, log
        self.snapshots = root / "snapshots"
        self.staging = root / "staging"
        self.announcements = root / "announcements"
        self.lock = asyncio.Lock()
        self.notes_lock = asyncio.Lock()
        self.cache = {}
        self.cache_lock = threading.Lock()
        self.checked_patch = ""
        self.checked_at = 0
        self.next_attempt = 0
        self.next_kind = "hero"
        self.progress = {"state": "未同步"}
        self.notes_retry = {}
        for d in (self.snapshots, self.staging, self.announcements):
            d.mkdir(parents=True, exist_ok=True)

    def path(self, kind, patch, stage=False):
        if kind not in KIND or not isinstance(patch,str) or not PATCH.fullmatch(patch):
            raise ValueError("无效补丁标识")
        return (self.staging if stage else self.snapshots) / (kind+"-"+patch+".json")

    def versions(self, kind):
        found = []
        for p in self.snapshots.glob(kind+"-*.json"):
            patch = p.stem.removeprefix(kind+"-")
            if PATCH.fullmatch(patch):
                found.append(patch)
        return sorted(found, key=patch_key, reverse=True)

    def _read(self, kind, patch, stage=False):
        p = self.path(kind, patch, stage)
        try:
            stat = p.stat()
            if stat.st_size > 64*1024*1024:
                raise ValueError("资料快照超限")
            signature = (stat.st_mtime_ns, stat.st_size)
            with self.cache_lock:
                old = self.cache.get(p)
            if old and old[0] == signature:
                return old[1]
            data = validate_snapshot(read_json(p), kind, patch, not stage)
            if not stage:
                with self.cache_lock:
                    self.cache[p] = (signature, data)
                    while len(self.cache) > 4:
                        self.cache.pop(next(iter(self.cache)))
            return data
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            return None

    async def get(self, kind, patch=""):
        if patch:
            if not PATCH.fullmatch(patch):
                return None
            return await asyncio.to_thread(self._read, kind, patch)
        for version in await asyncio.to_thread(self.versions, kind):
            data = await asyncio.to_thread(self._read, kind, version)
            if data:
                return data
        return None

    def header(self, snapshot, current, verified_at):
        patch = snapshot["patch"]
        return {
            "patch": patch, "catalog_patch": patch,
            "current_patch": current or None,
            "stale": not current or current != patch or now() - verified_at > 3600,
            "source": "Valve 官方 "+("英雄" if snapshot["kind"]=="hero" else "装备")+"版本快照",
            "snapshot_checked_at": snapshot.get("checked_at"),
            "coverage": {"details": len(snapshot["details"]), "roster": len(snapshot["roster"]),
                         "metadata_only":len(snapshot["unavailable"])},
            "note": "补丁在分批抓取前后核对。无公告的临时热修不保证逐次捕获。",
        }

    async def check_patch(self, cfg):
        patch = latest_patch(await self.service._fetch(BASE+"patchnoteslist?language=english", cfg))
        self.checked_patch, self.checked_at = patch, now()
        # On-demand detail queries must not reuse another version's catalog.
        old = self.service.catalog_cache
        if isinstance(old,dict) and old.get("patch") != patch:
            self.service.catalog_retry_after = 0
        return patch

    async def _new_stage(self, cfg, kind, patch):
        listing, key, *_ = KIND[kind]
        rows = validate_roster(api_rows(await self.service._fetch(BASE+listing+"?language=schinese",cfg),key), kind)
        stage = {"schema":1, "kind":kind, "patch":patch, "started_at":stamp(),
                "roster":rows, "details":{}, "unavailable":[], "cursor":0, "complete":False}
        if kind=="hero":
            try:
                # Optional single bulk localization feed; official English names
                # may differ from the old internal ability code.
                data=await self.service._fetch(BASE+"abilitylist?language=english",cfg)
                arr=data.get("result",{}).get("data",{})
                vals=api_rows(data,"itemabilities" if "itemabilities" in arr else "abilities")
                index={}
                for a in vals:
                    identity=a.get("id")
                    code=a.get("name")
                    name=html_text(a.get("name_english_loc") or a.get("name_loc"))
                    if (type(identity) is not int or not 0<identity<100000000
                        or not isinstance(code,str) or not CODE.fullmatch(code)
                        or not name or len(name)>240):
                        continue
                    if str(identity) in index:
                        raise ValueError("英文技能 ID 重复")
                    index[str(identity)]={"code":code,"name":name}
                if index:
                    stage["ability_names_en"]=index
            except Exception:
                # Chinese details remain usable; the alias index is supplementary.
                pass
        return stage

    async def _one(self, cfg, kind, row):
        _, _, endpoint, parameter, key, *_ = KIND[kind]
        try:
            payload = await self.service._fetch(BASE+f"{endpoint}?{parameter}={row['id']}&language=schinese",cfg)
            res=payload.get("result",{})
            if kind=="item" and res.get("status")==1 and res.get("data")=={}:
                values=[]
            else:
                values = api_rows(payload, key)
            if not values and kind == "item" and payload["result"].get("status") == 1:
                # Confirmed empty result, not a timeout or transport error.
                if any(row.get(k) is True for k in ("is_pregame_suggested","is_earlygame_suggested","is_lategame_suggested")):
                    return str(row["id"]),None,"recommended_item_missing"
                return str(row["id"]), None, "empty"
            if len(values) != 1:
                raise ValueError("官方详情不是唯一对象")
            return str(row["id"]), validate_detail(values[0],row,kind), "ok"
        except Exception as e:
            return str(row["id"]), None, type(e).__name__

    def _counts(self, data, published=False):
        if not data:
            return {"done":0,"total":0,"available":0,"metadata_only":0,"published":False}
        return {"done":len(data["details"])+len(data["unavailable"]), "total":len(data["roster"]),
                "available":len(data["details"]), "metadata_only":len(data["unavailable"]),
                "published":published, "patch":data["patch"]}

    def _status(self):
        out = dict(self.progress)
        checked, checked_at = self.checked_patch, self.checked_at
        if checked:
            out.update(patch=checked, patch_checked_at=stamp(checked_at))
        # Status always shows the last usable snapshot separately from staging.
        for kind in KIND:
            versions = self.versions(kind)
            last_good = next((v for v in versions if self._read(kind,v)),None)
            published = (self._read(kind,checked) if checked else None)
            stage = self._read(kind,checked,True) if checked and not published else None
            if not published and not stage and last_good and not checked:
                published = self._read(kind,last_good)
            out[kind] = self._counts(published or stage, bool(published))
            out[kind]["latest_published"] = last_good
            out[kind]["versions"] = len(versions)
        return out

    async def status(self):
        return await asyncio.to_thread(self._status)

    async def sync_step(self, cfg, force=False):
        if not cfg["enabled"]:
            return {"state":"已停用"}
        async with self.lock:
            if not force and now() < self.next_attempt:
                return await self.status()
            self.progress = {"state":"检查版本"}
            kind = None
            try:
                patch = await self.check_patch(cfg)
                order=(self.next_kind,"item" if self.next_kind=="hero" else "hero")
                for k in order:
                    if not await self.get(k,patch):
                        kind = k
                        break
                if kind is None:
                    self.progress = {"state":"已同步"}
                    self.next_attempt = now() + 3600
                    return await self.status()
                stage = await asyncio.to_thread(self._read,kind,patch,True)
                if stage is None:
                    stage = await self._new_stage(cfg,kind,patch)
                # Copy data loaded from disk; only commit after the end-of-batch
                # patch check succeeds. The public snapshot is never modified.
                stage = {**stage, "details":dict(stage["details"]), "unavailable":list(stage["unavailable"])}
                self.progress = {"state":"同步英雄资料" if kind=="hero" else "同步装备资料",
                                 "batch":self._counts(stage)}
                pending = [r for r in stage["roster"]
                           if str(r["id"]) not in stage["details"]
                           and str(r["id"]) not in stage["unavailable"]]
                # Correctly retry records instead of permanently blocking later IDs.
                if pending:
                    rotate = int(stage.get("cursor",0)) % len(pending)
                    pending = pending[rotate:] + pending[:rotate]
                selected = pending[:cfg["knowledge_batch_size"]]
                failures, consecutive, completed = 0, 0, 0
                for pos in range(0,len(selected),2):
                    results = await asyncio.gather(*(self._one(cfg,kind,r) for r in selected[pos:pos+2]))
                    for identity, data, state in results:
                        if state == "ok":
                            stage["details"][identity] = data
                            completed += 1
                            consecutive = 0
                        elif state == "empty":
                            stage["unavailable"].append(identity)
                            completed += 1
                            consecutive = 0
                        else:
                            failures += 1
                            consecutive += 1
                    self.progress["batch"] = self._counts(stage)
                    if consecutive >= 4:
                        break
                    if pos+2 < len(selected):
                        await asyncio.sleep(0.18)
                stage["cursor"] = int(stage.get("cursor",0))+max(1,failures)
                end = await self.check_patch(cfg)
                if end != patch:
                    self.progress = {"state":"补丁变化，本批未发布", "previous_patch":patch}
                    self.next_attempt = now()+30
                    return await self.status()
                stage["checked_at"] = stamp(self.checked_at)
                count = len(stage["details"])+len(stage["unavailable"])
                if count == len(stage["roster"]) and kind == "item" and not enough_items(stage["details"],stage["roster"]):
                    # A broadly broken detail endpoint cannot become the canonical
                    # catalog merely by claiming every absent item is an empty result.
                    stage["unavailable"] = []
                    failures += 1
                stage["complete"] = len(stage["details"])+len(stage["unavailable"])==len(stage["roster"])
                out = self.path(kind,patch,not stage["complete"])
                if stage["complete"]:
                    validate_snapshot(stage,kind,patch)
                await asyncio.to_thread(atomic_json,out,stage)
                # Preserve a previous complete version; only this version's
                # now-obsolete checkpoint may be removed after publication.
                if stage["complete"]:
                    checkpoint = self.path(kind,patch,True)
                    try:
                        await asyncio.to_thread(checkpoint.unlink, missing_ok=True)
                    except OSError:
                        pass
                self.progress = {"state":"本批完成" if not failures else "部分请求失败，等待重试",
                                 "batch":self._counts(stage,stage["complete"]), "failures":failures}
                self.next_attempt = now()+(180 if failures and not completed else 30 if failures else 0)
                self.next_kind = ("item" if kind=="hero" else "hero") if failures else "hero"
                self.log.emit("DOTA","knowledge_batch",patch=patch,kind=kind,
                              fetched=completed,failed=failures,published=stage["complete"])
                if stage["complete"]:
                    other = "item" if kind == "hero" else "hero"
                    if await self.get(other,patch):
                        self.progress["state"] = "已同步"
                        self.next_attempt = now()+3600
                return await self.status()
            except Exception as e:
                if kind:
                    self.next_kind = "item" if kind=="hero" else "hero"
                self.progress = {"state":"请求失败，保留已有版本","reason":type(e).__name__}
                self.next_attempt = now()+300
                self.log.emit("DOTA","knowledge_failed","WARNING",exception=type(e).__name__)
                return await self.status()

    async def build(self, config, force=False):
        """Manual action processes multiple batches until complete or retry."""
        first = True
        while True:
            cfg = config.snapshot()["dota"]
            result = await self.sync_step(cfg, force=force and first)
            first = False
            if result["state"] != "本批完成":
                return result
            await asyncio.sleep(0.35)

    @staticmethod
    def local_path(value):
        if os.name == "nt" and re.match(r"^/[A-Za-z]:[/\\]",value):
            value = value[1:]
        return Path(value)

    async def notes(self, cfg, patch):
        if not isinstance(patch,str) or not PATCH.fullmatch(patch):
            return None
        async with self.notes_lock:
            path = self.announcements / (patch+".json")
            try:
                prior = await asyncio.to_thread(read_json,path)
                normalized = parse_patch_notes(prior["data"],patch)
                return {**normalized,"source":prior["source"]}
            except (OSError, ValueError, KeyError, TypeError, AttributeError):
                pass
            if now() >= self.notes_retry.get(patch,0):
                for language in ("schinese","english"):
                    url = BASE+f"patchnotes?version={patch}&language={language}"
                    try:
                        data = await self.service._fetch(url,cfg)
                        result = parse_patch_notes(data,patch)
                        await asyncio.to_thread(atomic_json,path,{"data":data,"source":url})
                        return {**result,"source":url}
                    except Exception:
                        continue
                self.notes_retry[patch] = now()+300
                if len(self.notes_retry)>100:
                    self.notes_retry.pop(next(iter(self.notes_retry)))
            if cfg.get("patchnotes_json"):
                def read_local():
                    p=self.local_path(cfg["patchnotes_json"])
                    if not p.is_file() or p.stat().st_size > 32*1024*1024:
                        return None
                    raw = read_json(p)
                    if isinstance(raw,dict) and raw.get("patch_number")==patch:
                        return {**parse_patch_notes(raw,patch),"source":"本地原版公告 JSON（未在线核实）"}
                    return {**parse_constant_notes(raw,patch),"source":"本地 dotaconstants 公告 JSON（未在线核实）"}
                try:
                    return await asyncio.to_thread(read_local)
                except (OSError, KeyError, ValueError, TypeError, AttributeError):
                    pass
            return None


def note_lines(value):
    if isinstance(value,str):
        text=html_text(value)
        return [text] if text else []
    if isinstance(value,list):
        return [x for item in value for x in note_lines(item)]
    if isinstance(value,dict):
        # Valve uses notes, postfix lines, nested generic notes and talent notes.
        known = ("title","note","info","ability_notes","hero_notes","talent_notes","postfix_lines","generic","notes")
        found = [k for k in known if k in value]
        if not found and value:
            raise ValueError("公告包含未知文本结构")
        return [x for k in found for x in note_lines(value[k])]
    if value is None:
        return []
    raise ValueError("公告文本类型无效")


def parse_patch_notes(payload, patch):
    """Known Valve patchnotes layout. Never interpret a failed response as no changes."""
    if (not isinstance(payload,dict) or payload.get("success") not in (True,1)
        or payload.get("patch_number") != patch
        or not any(k in payload for k in ("heroes","items","neutral_items","general_notes"))):
        raise ValueError("公告版本或请求状态异常")
    result = {"patch":patch,"hero_notes":{},"item_notes":{},"general":[],
              "sections":{"heroes":"heroes" in payload,
                          "items":"items" in payload or "neutral_items" in payload,
                          "general":"general_notes" in payload}}
    timestamp=payload.get("patch_timestamp")
    if type(timestamp) is int and timestamp>0:
        result["published_date"]=datetime.fromtimestamp(timestamp,timezone.utc).date().isoformat()
    for section in ("heroes","items","neutral_items","general_notes"):
        if section in payload and not isinstance(payload[section],list):
            raise ValueError("公告栏目结构异常")
    seen=set()
    for h in payload.get("heroes",[]):
        identity=h["hero_id"]
        if type(identity) is not int or identity<=0 or identity in seen:
            raise ValueError("公告英雄标识异常")
        seen.add(identity)
        lines=[{"text":s} for s in note_lines(h.get("hero_notes"))]
        abilities=h.get("abilities",[])
        if not isinstance(abilities,list):
            raise ValueError("公告技能结构异常")
        for a in abilities:
            aid=a.get("ability_id")
            if type(aid) is not int or aid<=0:
                raise ValueError("公告技能标识异常")
            lines.extend({"ability_id":aid,"text":s}
                         for s in note_lines(a.get("ability_notes"))+note_lines(a.get("postfix_lines")))
        lines.extend({"section":"talent","text":s} for s in note_lines(h.get("talent_notes")))
        result["hero_notes"][str(identity)]=lines
    for section in ("items","neutral_items"):
        for item in payload.get(section,[]):
            identity=item["ability_id"]
            if type(identity) is not int or identity<=0:
                raise ValueError("公告物品标识异常")
            result["item_notes"].setdefault(str(identity),[]).extend(
                {"text":s,"section":section} for s in
                note_lines(item.get("ability_notes"))+note_lines(item.get("postfix_lines")))
    result["general"]=[{"text":s} for s in note_lines(payload.get("general_notes"))]
    return result


def parse_constant_notes(payload, patch):
    """Optional local, code-keyed export; only the explicitly named patch."""
    if not isinstance(payload,dict):
        raise ValueError("本地公告格式异常")
    raw=payload.get(patch.replace(".","_"),payload.get(patch))
    if not isinstance(raw,dict) or not any(k in raw for k in ("heroes","items","general")):
        raise ValueError("本地公告没有指定版本")
    result={"patch":patch,"hero_notes":{},"item_notes":{},"general":[],
            "sections":{k:k in raw for k in ("heroes","items","general")}}
    for source,target in (("heroes","hero_notes"),("items","item_notes")):
        groups=raw.get(source,{})
        if not isinstance(groups,dict):
            raise ValueError("本地公告分组异常")
        for code,value in groups.items():
            if not CODE.fullmatch(code):
                continue
            lines=[]
            if isinstance(value,dict):
                for sub,notes in value.items():
                    lines.extend({"section":sub,"text":s} for s in note_lines(notes))
            else:
                lines.extend({"text":s} for s in note_lines(value))
            result[target][code]=lines
    result["general"]=[{"text":s} for s in note_lines(raw.get("general"))]
    return result
