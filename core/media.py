"""Metered capability dispatch and persistent media accounting in the existing KV store."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from dataclasses import dataclass

from .media_policy import MEDIA_UNITS
from .memory import allowed
from .telemetry import BudgetExceeded, ModelFailure, _model_failure
from .util import day_key, dump, now

LEDGER_PREFIX = "media_usage:"
MEDIA_LIMIT = 2000
UNCERTAIN = {"timeout", "invalid_response", "unknown"}


@dataclass(frozen=True)
class MeteredResult:
    value: object
    units: int
    estimated: bool = False


def media_permission(kind, meta, cfg):
    settings = cfg.get("media", {}).get(kind, {})
    return (
        cfg["access"]["mode"] == "active" and allowed(meta, cfg)
        and settings.get("enabled") and settings.get("provider")
        and (settings.get("allow_private") if meta["kind"] == "private"
             else meta["location"] in settings.get("allowed_groups", ())
             or meta["umo"] in settings.get("allowed_groups", ()))
    )


class MediaLedger:
    def __init__(self, store):
        self.store = store

    @staticmethod
    def _rows(db, day):
        row = db.execute("SELECT value FROM kv WHERE key=?", (LEDGER_PREFIX + day,)).fetchone()
        return json.loads(row[0]) if row else {}

    @staticmethod
    def _save(db, day, rows):
        db.execute("INSERT OR REPLACE INTO kv VALUES(?,?)", (LEDGER_PREFIX + day, dump(rows)))

    def reserve(self, day, kind, provider, scope, units, limits):
        key = dump([kind, provider, scope])
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            rows = self._rows(db, day)
            related = [r for r in rows.values() if r["kind"] == kind]
            total = sum(r["used"] + r["pending_units"] for r in related)
            scoped = sum(r["used"] + r["pending_units"] for r in related if r["scope"] == scope)
            if total + units > limits[0] or scoped + units > limits[1]:
                raise BudgetExceeded("媒体今日额度不足")
            if key not in rows and len(rows) >= MEDIA_LIMIT:
                raise BudgetExceeded("媒体账本当日记录已满")
            row = rows.setdefault(key, dict(kind=kind, provider=provider, scope=scope, used=0,
                                           pending_units=0, success=0, failed=0, unknown=0,
                                           cancelled=0, pending=0, estimated_units=0))
            row["pending"] += 1
            row["pending_units"] += units
            self._save(db, day, rows)
        return key

    def finish(self, day, key, reserve, outcome, actual, estimated):
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            rows = self._rows(db, day)
            row = rows[key]
            row["pending_units"] -= reserve
            row["pending"] -= 1
            row[outcome] += 1
            row["used"] += actual
            if estimated:
                row["estimated_units"] += actual
            self._save(db, day, rows)

    def recover(self):
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            for key, payload in db.execute("SELECT key,value FROM kv WHERE key LIKE ?", (LEDGER_PREFIX + "%",)):
                rows = json.loads(payload)
                modified = False
                for row in rows.values():
                    if row["pending"]:
                        units = row["pending_units"]
                        row["unknown"] += row["pending"]
                        row["used"] += units
                        row["estimated_units"] += units
                        row["pending"] = row["pending_units"] = 0
                        modified = True
                if modified:
                    self._save(db, key[len(LEDGER_PREFIX):], rows)
            db.execute("DELETE FROM kv WHERE key LIKE ? AND key < ?",
                       (LEDGER_PREFIX + "%", LEDGER_PREFIX + day_key(now() - 90 * 86400)))

    def overview(self, days):
        since = day_key(now() - (days - 1) * 86400)
        with self.store.connection() as db:
            records = db.execute("SELECT key,value FROM kv WHERE key LIKE ? AND key >= ? ORDER BY key DESC",
                                 (LEDGER_PREFIX + "%", LEDGER_PREFIX + since)).fetchall()
        # Dashboard aggregates per provider. Scope IDs are only needed to enforce quotas.
        aggregated = {}
        for key, payload in records:
            day = key[len(LEDGER_PREFIX):]
            for row in json.loads(payload).values():
                identity = (day, row["kind"], row["provider"])
                out = aggregated.setdefault(identity, dict(day=day, kind=row["kind"], provider=row["provider"],
                                                            unit=MEDIA_UNITS[row["kind"]]))
                for name in ("used", "pending_units", "pending", "success", "failed", "unknown",
                             "cancelled", "estimated_units"):
                    out[name] = out.get(name, 0) + row[name]
        return list(aggregated.values())


class MediaGateway:
    def __init__(self, gateway, store, config, log):
        self.gateway = gateway
        self.store = store
        self.config = config
        self.log = log
        self.ledger = MediaLedger(store)
        self.available = True

    async def start(self):
        try:
            await asyncio.to_thread(self.ledger.recover)
        except (ValueError, TypeError, KeyError, sqlite3.Error):
            # Keep the text path available and stop new spend if an old ledger cannot be reconciled.
            self.available = False
            self.log.emit("MEDIA", "ledger_unavailable", "WARNING", error_kind="unknown")

    def enabled(self, kind, meta, cfg):
        return self.available and bool(media_permission(kind, meta, cfg))

    async def authorized(self, kind, meta):
        person = await self.store.call("person", meta["scope"], meta["uid"])
        return not (person and person["blocked"]) and self.enabled(kind, meta, self.config.snapshot())

    async def run(self, kind, meta, reserve, operation):
        try:
            await asyncio.wait_for(self.gateway._acquire(False), timeout=15)
        except asyncio.TimeoutError:
            raise ModelFailure("timeout") from None
        key = None
        day = day_key()
        actual, estimated, outcome = 0, False, "failed"
        started = time.monotonic()
        try:
            if not await self.authorized(kind, meta):
                raise ModelFailure("provider_unavailable")
            cfg = self.config.snapshot()
            settings = cfg["media"][kind]
            if not self.enabled(kind, meta, cfg):
                raise ModelFailure("provider_unavailable")
            if type(reserve) is not int or not 0 < reserve <= settings["max_units"]:
                raise BudgetExceeded("超出媒体单次额度")
            limit = settings["daily_units"]
            if settings["free_only"]:
                limit = min(limit, settings["free_units_per_day"]) if settings["free_until"] >= day else 0
            provider = settings["provider"]
            reservation = asyncio.create_task(asyncio.to_thread(
                self.ledger.reserve, day, kind, provider, meta["scope"], reserve,
                (limit, settings["scope_daily_units"]),
            ))
            try:
                key = await asyncio.shield(reservation)
            except asyncio.CancelledError:
                key = await reservation
                outcome = "cancelled"
                raise
            try:
                if not await self.authorized(kind, meta) or settings != self.config.snapshot()["media"][kind]:
                    outcome = "cancelled"
                    raise ModelFailure("provider_unavailable")
                result = await asyncio.wait_for(operation(settings), timeout=settings["timeout_seconds"])
                if not isinstance(result, MeteredResult) or type(result.units) is not int or result.units < 0:
                    raise ModelFailure("invalid_response")
                actual, estimated, outcome = result.units, result.estimated, "success"
                return result.value
            except asyncio.CancelledError:
                actual, estimated, outcome = reserve, True, "unknown"
                raise
            except Exception as exc:
                failure = _model_failure(exc)
                if failure.category in UNCERTAIN:
                    actual, estimated, outcome = reserve, True, "unknown"
                self.log.emit("MEDIA", "operation_failed", "WARNING", scope=meta["scope"], task=kind,
                              provider=provider, units=actual, **failure.log_fields())
                raise failure from None
        finally:
            try:
                if key:
                    await asyncio.to_thread(self.ledger.finish, day, key, reserve, outcome, actual, estimated)
                    self.log.emit("MEDIA", "operation_finished", scope=meta["scope"], task=kind,
                                  status=outcome, provider=provider, units=actual,
                                  elapsed_ms=int((time.monotonic() - started) * 1000))
            finally:
                await self.gateway._release()

    async def overview(self, days=15):
        if not self.available:
            return []
        rows = await asyncio.to_thread(self.ledger.overview, days)
        settings = self.config.snapshot()["media"]
        for row in rows:
            price = settings[row["kind"]]["price_per_unit"]
            row["cost_cny_estimate"] = round(row["used"] * price, 6) if price else None
        return rows
