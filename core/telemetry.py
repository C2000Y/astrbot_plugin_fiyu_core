from __future__ import annotations

import asyncio
import json
import logging
import re
import threading
import time
from collections import defaultdict, deque
from datetime import date, timedelta
from pathlib import Path

from aiohttp import ClientConnectionError

from .util import MAX_REPLY_CHARS, clean, conservative_tokens, day_key, dump, stamp


class BalanceCheckError(ValueError):
    """Only these public diagnostics may appear in the dashboard or log."""
    REASONS = {
        "provider": "找不到支持余额查询的 Provider",
        "endpoint": "Provider 的 API Base 与官方 DeepSeek 接口不匹配",
        "key": "所选 Provider 缺少可用密钥",
        "http": "余额接口返回 HTTP 错误",
        "payload": "余额响应格式或数值无效",
        "currency": "响应中缺少所选币种",
    }

    def __init__(self, code, http_status=None):
        reason = self.REASONS[code]
        if code == "http" and type(http_status) is int:
            reason += f"（{http_status}）"
        super().__init__(reason)


_MODEL_ERROR_REASONS = {
    "provider_unavailable": "模型服务当前不可用",
    "timeout": "模型服务响应超时",
    "rate_limit": "模型服务限制了调用频率",
    "quota_exhausted": "模型服务额度不足",
    "content_rejected": "模型服务拒绝处理这条请求",
    "unsupported_modality": "模型服务不支持该媒体类型",
    "invalid_response": "模型返回的格式无效",
    "unknown": "没有可识别的模型故障详情",
}

# Keep only documented machine-readable values. Messages and raw completion data
# may contain private prompts, URL query strings or credentials.
_PROVIDER_ERROR_KINDS = {
    "DataInspectionFailed": "content_rejected",
    "data_inspection_failed": "content_rejected",
    "content_policy_violation": "content_rejected",
    "content_filter": "content_rejected",
    "image_content_policy_violation": "content_rejected",
    "unsupported_image_media_type": "unsupported_modality",
    "insufficient_quota": "quota_exhausted",
    "Throttling.AllocationQuota": "quota_exhausted",
    "Arrearage": "quota_exhausted",
    "isv.OUT_OF_SERVICE": "quota_exhausted",
    "Throttling.RateQuota": "rate_limit",
    "rate_limit_exceeded": "rate_limit",
    "InvalidApiKey": "provider_unavailable",
    "invalid_api_key": "provider_unavailable",
    "AccessDenied": "provider_unavailable",
    "access_denied": "provider_unavailable",
    "ModelNotFound": "provider_unavailable",
    "model_not_found": "provider_unavailable",
    "ServiceUnavailable": "provider_unavailable",
}


def _http_error_status(value):
    return value if type(value) is int and 400 <= value <= 599 else None


def _provider_error_code(value):
    return value if type(value) is str and value in _PROVIDER_ERROR_KINDS else None


class ModelFailure(RuntimeError):
    """A model failure with only safe fields for the plugin log and dashboard."""

    def __init__(self, category, *, http_status=None, provider_code=None):
        self.category = (
            category if type(category) is str and category in _MODEL_ERROR_REASONS else "unknown"
        )
        self.http_status = _http_error_status(http_status)
        self.provider_code = _provider_error_code(provider_code)
        super().__init__(_MODEL_ERROR_REASONS[self.category])

    def log_fields(self):
        fields = {"error_kind": self.category}
        if self.http_status is not None:
            fields["http_status"] = self.http_status
        if self.provider_code is not None:
            fields["provider_code"] = self.provider_code
        return fields


def _model_failure(source):
    if isinstance(source, ModelFailure):
        return source
    # OpenAI-compatible SDKs expose the code directly or as body.error.code.
    body = getattr(source, "body", None) if isinstance(source, Exception) else None
    error = body.get("error") if isinstance(body, dict) else None
    samples = (
        getattr(source, "error_code", None),
        getattr(source, "code", None),
        error.get("code") if isinstance(error, dict) else None,
        body.get("code") if isinstance(body, dict) else None,
    )
    code = next((code for value in samples if (code := _provider_error_code(value))), None)
    statuses = (
        getattr(source, "status_code", None),
        getattr(source, "status", None),
        getattr(getattr(source, "response", None), "status_code", None),
    )
    status = next((status for v in statuses if (status := _http_error_status(v))), None)
    if code:
        kind = _PROVIDER_ERROR_KINDS[code]
    elif isinstance(source, (TimeoutError, asyncio.TimeoutError)) or status in (408, 504):
        kind = "timeout"
    elif status == 429:
        kind = "rate_limit"
    elif status == 402:
        kind = "quota_exhausted"
    elif (
        isinstance(source, (ConnectionError, ClientConnectionError))
        or status in (401, 403, 404)
        or (status is not None and status >= 500)
    ):
        kind = "provider_unavailable"
    else:
        kind = "unknown"
    return ModelFailure(kind, http_status=status, provider_code=code)


class DailyLog:
    PATTERN = re.compile(r"^fiyu-(\d{4}-\d{2}-\d{2})(?:-(\d+))?\.jsonl$")

    def __init__(self, path: Path, config, logger: logging.Logger):
        self.path, self.config, self.logger = path, config, logger
        self.path.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.recent = deque(maxlen=500)

    def emit(
        self, module: str, code: str, level="INFO", *, scope="", reason="", **metrics
    ):
        # Log site/module names and safe bounded metadata, never prompts or raw exceptions.
        cfg = self.config.snapshot()["logs"]
        if getattr(logging, level, logging.INFO) < getattr(
            logging, cfg["level"], logging.INFO
        ):
            return
        entry = {
            "time": stamp(),
            "level": level,
            "module": clean(module, 35),
            "code": clean(code, 70),
        }
        if scope:
            entry["scope"] = str(scope)[:12]
        if reason:
            entry["reason"] = clean(reason, 160)
        for k, v in metrics.items():
            if k == "error_kind":
                if type(v) is str and v in _MODEL_ERROR_REASONS:
                    entry[k] = v
            elif k == "provider_code":
                if code := _provider_error_code(v):
                    entry[k] = code
            elif k == "http_status":
                if status := _http_error_status(v):
                    entry[k] = status
            elif module == "VISION" and k == "stage":
                if v in ("observe", "pre-decision", "reply"):
                    entry[k] = v
            elif type(v) in (float, int, bool) or v is None:
                entry[k] = v
            elif module == "REPLY" and k == "part_chars" and isinstance(v, list):
                entry[k] = [n for n in v[:8] if type(n) is int and 0 <= n <= MAX_REPLY_CHARS]
            elif k in ("provider", "tool", "exception", "task", "status"):
                entry[k] = clean(v, 120)
        with self.lock:
            self.recent.append(entry)
            try:
                limit = cfg["max_file_mb"] * 1024 * 1024
                name = f"fiyu-{day_key()}"
                p = self.path / f"{name}.jsonl"
                idx = 0
                while p.exists() and p.stat().st_size >= limit:
                    idx += 1
                    p = self.path / f"{name}-{idx}.jsonl"
                with p.open("a", encoding="utf-8") as f:
                    f.write(dump(entry) + "\n")
            except OSError:
                self.logger.warning("[Fiyu Core] 独立日志写入失败（请检查磁盘）")
        if level in ("WARNING", "ERROR"):
            self.logger.log(
                getattr(logging, level),
                "[Fiyu Core][%s] %s %s",
                module,
                code,
                entry.get("exception", entry.get("reason", "")),
            )

    def clean_old(self, retention: int, today: str | None = None) -> int:
        cutoff = date.fromisoformat(today or day_key()) - timedelta(days=retention - 1)
        n = 0
        with self.lock:
            for path in self.path.iterdir():
                m = self.PATTERN.fullmatch(path.name)
                if not m or not path.is_file() or path.is_symlink():
                    continue
                try:
                    if date.fromisoformat(m.group(1)) < cutoff:
                        path.unlink()
                        n += 1
                except (OSError, ValueError):
                    continue
        return n

    def days(self) -> list[str]:
        with self.lock:
            return sorted(
                {
                    m.group(1)
                    for p in self.path.iterdir()
                    if (m := self.PATTERN.fullmatch(p.name))
                    and p.is_file()
                    and not p.is_symlink()
                },
                reverse=True,
            )

    def list(self, day="", level="", module="", q="", limit=300):
        day = day or day_key()
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
            raise ValueError("日期格式错误")
        result = deque(maxlen=min(2000, max(1, limit)))
        with self.lock:
            files = []
            for p in self.path.iterdir():
                m = self.PATTERN.fullmatch(p.name)
                if m and m.group(1) == day and not p.is_symlink() and p.is_file():
                    files.append((int(m.group(2) or 0), p))
        # A Dashboard search can read a large daily file without blocking message logging.
        for _, p in sorted(files):
            try:
                with p.open(encoding="utf-8", errors="replace") as f:
                    for line in f:
                        try:
                            item = json.loads(line)
                        except ValueError:
                            continue
                        if level and item.get("level") != level:
                            continue
                        if module and item.get("module") != module:
                            continue
                        if q and q.casefold() not in line.casefold():
                            continue
                        result.append(item)
            except OSError:
                continue
        return {"days": self.days(), "items": list(result)[::-1]}


class BudgetExceeded(RuntimeError):
    pass


class ModelGateway:
    """One place for timeouts, accounting, soft priorities and concurrency limits."""

    def __init__(self, host, store, cfg, log):
        self.host, self.store, self.cfg, self.log = host, store, cfg, log
        self.lock = asyncio.Lock()
        self.condition = asyncio.Condition()
        self.running = 0
        self.waiting_foreground = 0
        self.reserved = defaultdict(int)
        self.reserved_calls = defaultdict(int)
        self.reserved_module_tokens = defaultdict(int)

    async def provider(self, module: str, umo: str, config: dict | None = None) -> str:
        cfg = config or self.cfg.snapshot()
        m = cfg["models"]
        if module in ("vision", "vision_observe"):
            if not m["vision_provider"]:
                raise ModelFailure("provider_unavailable")
            return m["vision_provider"]
        if module == "embedding":
            return m["embedding_provider"]
        preferred = {
            "reply": m["chat_provider"],
            "decision": m["decision_provider"] or m["chat_provider"],
            "memory": m["memory_provider"] or m["chat_provider"],
            "style": m["style_provider"] or m["memory_provider"] or m["chat_provider"],
            "research": m["research_provider"] or m["memory_provider"] or m["chat_provider"],
        }.get(module, "")
        return preferred or await self.host.chat_provider(umo)

    async def _reserve(
        self, module: str, estimate: int, background: bool, priority: bool
    ):
        d = day_key()
        async with self.lock:
            usage = await self.store.call("usage_today", d)
            live = self.cfg.snapshot()
            settings = live["budget"]
            used = usage["tokens"] + self.reserved[d]
            limit = settings["daily_tokens"]
            if limit and (
                used + estimate > limit
                or (
                    background
                    and not priority
                    and used >= limit * settings["soft_ratio"]
                )
            ):
                raise BudgetExceeded("今日 Token 预算不足或已达到后台暂停线")

            def calls_used(task):
                return usage["modules"].get(task, 0) + self.reserved_calls[(d, task)]

            limit_module = "vision" if module == "vision_observe" else module
            module_limit = settings["module_call_limits"].get(limit_module, 0)
            counted_modules = (
                ("vision", "vision_observe") if limit_module == "vision" else (module,)
            )
            if module_limit and sum(calls_used(task) for task in counted_modules) >= module_limit:
                raise BudgetExceeded("该模块今日调用次数已达上限")
            if module == "vision_observe":
                reply = live["reply"]
                if (
                    live["access"]["mode"] != "active"
                    or not reply["image_enabled"]
                    or reply.get("vision_strategy") != "full"
                    or calls_used(module) >= reply.get("vision_observe_daily_calls", 0)
                ):
                    raise BudgetExceeded("独立图片观察已关闭或达到每日调用上限")
            if module == "research":
                research = live["research"]
                if (not research["enabled"] or live["access"]["mode"] != "active"
                    or usage["module_tokens"].get(module,0)+self.reserved_module_tokens[(d,module)]+estimate>research["daily_tokens"]):
                    raise BudgetExceeded("自主学习独立预算不足或已停用")
            self.reserved[d] += estimate
            self.reserved_calls[(d, module)] += 1
            self.reserved_module_tokens[(d,module)] += estimate
        return d

    async def _acquire(self, background: bool):
        async with self.condition:
            if not background:
                self.waiting_foreground += 1
            acquired = False
            try:
                await self.condition.wait_for(
                    lambda: (
                        self.running
                        < self.cfg.snapshot()["models"]["max_parallel_requests"]
                        and (not background or self.waiting_foreground == 0)
                    )
                )
                self.running += 1
                acquired = True
            finally:
                if not background:
                    self.waiting_foreground -= 1
                if not acquired:
                    self.condition.notify_all()

    async def _release(self):
        async with self.condition:
            self.running -= 1
            self.condition.notify_all()

    async def _record(
        self, day, module, provider, estimate, failed, usage, elapsed, scope, failure=None
    ):
        other, cached, output, estimated = usage
        async with self.lock:
            try:
                await self.store.call(
                    "add_usage",
                    day,
                    module,
                    provider,
                    failed,
                    other,
                    cached,
                    output,
                    estimated,
                    int(elapsed * 1000),
                )
            finally:
                self.reserved[day] = max(0, self.reserved[day] - estimate)
                self.reserved_calls[(day, module)] = max(
                    0, self.reserved_calls[(day, module)] - 1
                )
                self.reserved_module_tokens[(day,module)] = max(0,self.reserved_module_tokens[(day,module)]-estimate)
        self.log.emit(
            "TOKEN",
            "model_call",
            "INFO",
            scope=scope,
            provider=provider,
            task=module,
            input=other + cached,
            cached=cached,
            output=output,
            estimated=estimated,
            elapsed_ms=int(elapsed * 1000),
            failed=failed,
            **(failure.log_fields() if failure else {}),
        )

    async def generate(
        self,
        module: str,
        umo: str,
        prompt: str,
        system: str,
        *,
        scope="",
        timeout=None,
        background=False,
        priority=False,
        config=None,
        tools=None,
        contexts=None,
        tool_results=None,
        images=None,
        output_reserve=300,
    ):
        cfg = config or self.cfg.snapshot()
        try:
            provider = await self.provider(module, umo, cfg)
            if not provider:
                raise ModelFailure("provider_unavailable")
        except Exception as exc:
            failure = _model_failure(exc)
            self.log.emit(
                "TOKEN", "model_unavailable", scope=scope, task=module, **failure.log_fields()
            )
            raise failure from None
        # Account for repeated tool round input and persona examples. Only a conservative estimate is used for reservation.
        estimate_input = conservative_tokens(
            system + prompt + str(contexts or "") + str(tool_results or "")
        )
        estimate_input += int(900 * len(images or []))
        if tools:
            estimate_input += conservative_tokens(str(tools))
        reservation = estimate_input + output_reserve
        await self._acquire(background)
        d = None
        started = time.monotonic()
        resp = None
        failed = True
        failure = None
        try:
            d = await self._reserve(module, reservation, background, priority)
            try:
                resp = await asyncio.wait_for(
                    self.host.generate(
                        provider,
                        prompt,
                        system,
                        tools=tools,
                        contexts=contexts,
                        tool_results=tool_results,
                        images=images,
                    ),
                    timeout=timeout or cfg["models"]["timeout_seconds"],
                )
            except Exception as exc:
                failure = _model_failure(exc)
                raise failure from None
            if getattr(resp, "role", None) == "err":
                failure = _model_failure(resp)
                raise failure
            if not hasattr(resp, "completion_text"):
                failure = ModelFailure("invalid_response")
                raise failure
            failed = False
            return resp
        finally:
            try:
                if d:
                    usage = getattr(resp, "usage", None)
                    try:
                        other, cached, output = (
                            max(0, int(getattr(usage, field, 0) or 0))
                            for field in ("input_other", "input_cached", "output")
                        )
                    except (ValueError, TypeError, OverflowError):
                        other = cached = output = 0
                    estimated = not bool(other + cached + output)
                    if estimated:
                        other = estimate_input
                        output = (
                            conservative_tokens(
                                str(getattr(resp, "completion_text", "") or "")
                            )
                            if resp and not failed
                            else 0
                        )
                    await self._record(
                        d,
                        module,
                        provider,
                        reservation,
                        failed,
                        (other, cached, output, estimated),
                        time.monotonic() - started,
                        scope,
                        failure,
                    )
            finally:
                await self._release()

    async def embed(
        self, umo: str, texts: list[str], *, scope="", background=False, config=None
    ):
        cfg = config or self.cfg.snapshot()
        pid = cfg["models"]["embedding_provider"]
        if not pid or not texts:
            return []
        estimate = sum(conservative_tokens(t) for t in texts)
        await self._acquire(background)
        d = None
        started = time.monotonic()
        failed = True
        try:
            d = await self._reserve("embedding", estimate, background, False)
            vectors = await asyncio.wait_for(
                self.host.embed(pid, texts), timeout=cfg["models"]["timeout_seconds"]
            )
            if len(vectors) != len(texts) or any(
                not isinstance(v, list)
                or not v
                or len(v) > 8192
                or any(type(x) not in (int, float) or not -1e10 <= x <= 1e10 for x in v)
                for v in vectors
            ):
                raise ValueError("Embedding 响应无效")
            failed = False
            return vectors
        finally:
            try:
                if d:
                    await self._record(
                        d,
                        "embedding",
                        pid,
                        estimate,
                        failed,
                        (estimate, 0, 0, True),
                        time.monotonic() - started,
                        scope,
                    )
            finally:
                await self._release()
