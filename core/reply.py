"""Reply generation and authorized, bounded native tool rounds."""

from __future__ import annotations

from dataclasses import dataclass

from .memory import allowed
from .personality import SILENT
from .routing import ToolPlan, domain_rules
from .telemetry import BudgetExceeded
from .util import contains_dsml, dump, now


FINAL_REPLY_RULES = """当前请求没有可执行工具。现在结束查询，根据已获得的资料和上下文直接回答；无法核实就说明。不要继续请求或模拟工具，不输出调用标记或参数，不声称完成未执行的操作。"""

TEXT_REPLY_RULES = """本轮以自然语言回应。不能调用外部工具，也不要模拟调用或声称完成外部操作。"""

TOOL_RETRY_RULES = """上一份响应的工具调用格式异常，异常部分未执行。请重新生成，使用已经返回的资料，避免重复查询。本轮有工具时只通过原生工具调用接口提交请求；没有工具时直接结束查询并回答。不要复述内部协议或参数。"""

TOOL_PROTOCOL_FALLBACK = "刚才没能整理出可靠的答案，先不乱说了。"


def valid_tool_call_fields(names, args, call_ids):
    return (
        all(isinstance(values, (list, tuple)) for values in (names, args, call_ids))
        and len(names) == len(args) == len(call_ids) <= 24
        and all(isinstance(name, str) and name for name in names)
        and all(isinstance(cid, str) and cid for cid in call_ids)
        and len(set(call_ids)) == len(call_ids)
    )

@dataclass(frozen=True)
class ReplyTurn:
    meta: dict
    config: dict
    task: str
    prompt: str
    system: str
    examples: list
    plan: ToolPlan
    explicit: bool
    direct: bool
    can_silent: bool
    turn_id: int
    event: object
    current: str
    batch_rows: list[dict]


class ReplyService:
    def __init__(self, host, store, config, gateway, tools, log):
        self.host, self.store, self.config = host, store, config
        self.gateway, self.tools, self.log = gateway, tools, log

    async def run(self, turn: ReplyTurn, is_open):
        cfg, meta, task, plan = turn.config, turn.meta, turn.task, turn.plan
        max_rounds = cfg["reply"]["max_tool_rounds"]
        max_calls = cfg["reply"]["max_tool_calls"]
        tool_results = []
        used = 0
        seen = {}
        rounds = 0
        recovery_used = False
        recovering = False
        scope, turn_id = meta["scope"], turn.turn_id
        if cfg["models"].get("casual_provider") or cfg["models"].get("deep_provider"):
            self.log.emit("REPLY", "model_route", "DEBUG", scope=scope, turn_id=turn_id, status=task)
        fallback = "" if turn.can_silent else TOOL_PROTOCOL_FALLBACK
        # Normal path: at most max_rounds tool rounds + one final answer.
        # One extra model request per turn may recover a malformed response.
        for _ in range(max_rounds + 2):
            live = self.config.snapshot()
            if (not is_open()
                    or live["access"]["mode"] != "active" or not allowed(meta, live)):
                return ""
            if recovering:
                person = await self.store.call("person", scope, meta["uid"])
                if not is_open() or (person and person["blocked"]):
                    return ""
                if not turn.direct and now() - turn.batch_rows[-1]["ts"] > live["decision"]["stale_seconds"]:
                    self.log.emit("REPLY", "tool_protocol_retry_skipped", scope=scope,
                                  turn_id=turn_id, reason="stale_topic")
                    return ""
            ready = []
            if task == "reply":
                plan.restrict(self.tools.specs(meta, live, turn.explicit))
                if rounds < max_rounds and used < max_calls:
                    ready = plan.ready(rounds + 1 < max_rounds and used + 1 < max_calls)
            system = turn.system
            rules = domain_rules(plan.selected, can_call=bool(ready))
            if rules:
                system += "\n\n" + rules
            if any(x["name"] == "open_tools" for x in ready):
                system += "\n若现有工具不足，可以调用 open_tools 展开已授权类别，再实际查询；展开本身不代表查到了资料。"
            if recovering:
                system += "\n\n" + TOOL_RETRY_RULES
            if not ready:
                system += "\n\n" + (FINAL_REPLY_RULES if task == "reply" else TEXT_REPLY_RULES)
                if tool_results:
                    self.log.emit("REPLY", "tool_final_summary", "DEBUG", scope=scope,
                                  turn_id=turn_id, tool_rounds=rounds, tool_calls=used,
                                  retry=recovering)
            try:
                resp = await self.gateway.generate(
                    task,
                    meta["umo"],
                    turn.prompt,
                    system,
                    scope=scope,
                    config=cfg,
                    tools=ready,
                    contexts=turn.examples,
                    tool_results=tool_results,
                    output_reserve=min(1500, cfg["reply"]["max_reply_chars"]),
                )
            except Exception as exc:
                if not recovering:
                    raise
                self.log.emit("REPLY", "tool_protocol_retry_failed", "WARNING", scope=scope,
                              turn_id=turn_id, tool_rounds=rounds, tool_calls=used,
                              reason="budget" if isinstance(exc, BudgetExceeded) else "model_error",
                              exception=type(exc).__name__)
                return fallback
            if not is_open():
                return ""
            last = str(resp.completion_text or "").strip()
            names = getattr(resp, "tools_call_name", []) or []
            args = getattr(resp, "tools_call_args", []) or []
            call_ids = getattr(resp, "tools_call_ids", []) or []
            raw_protocol = contains_dsml(last)
            problem = ""
            if names and not ready:
                problem = "tools_unavailable"
            elif raw_protocol and not names:
                problem = "dsml_in_answer"
            elif not valid_tool_call_fields(names, args, call_ids):
                problem = "invalid_tool_calls"
            elif recovering and not names and not last:
                problem = "empty_recovery"
            if problem:
                retry = not recovery_used
                self.log.emit("REPLY", "tool_protocol_error", "WARNING", scope=scope,
                              reason=problem, turn_id=turn_id, tool_rounds=rounds, tool_calls=used,
                              offered_tools=len(ready), chars=len(last), dsml=raw_protocol, retry=retry)
                if not retry:
                    return fallback
                recovery_used = recovering = True
                continue
            if recovering:
                self.log.emit("REPLY", "tool_protocol_recovered", scope=scope, turn_id=turn_id,
                              tool_rounds=rounds, tool_calls=used, status="tool_call" if names else "answer")
                recovering = False
            if not names:
                if SILENT in last:
                    if last == SILENT and turn.can_silent and not used:
                        self.log.emit("REPLY", "casual_silent", scope=meta["scope"])
                        return ""
                    return last.replace(SILENT, "").strip() or "刚刚没组织好回答，你可以再问一下。"
                return last
            if raw_protocol:
                # A valid structured call remains authoritative. The host also
                # omits the malformed text from its next assistant tool message.
                self.log.emit("REPLY", "tool_protocol_text_discarded", "WARNING",
                              scope=scope, turn_id=turn_id, tool_rounds=rounds,
                              tool_calls=used, chars=len(last))
            fallback = TOOL_PROTOCOL_FALLBACK
            results = []
            for name, arg, call_id in zip(names, args, call_ids):
                if not is_open():
                    return ""
                if used >= max_calls:
                    result = dump({"error": "本轮工具预算已用完，请依据已知结果回答。"})
                else:
                    used += 1
                    try:
                        key = dump([name, arg])
                        count = seen.get(key, 0)
                        seen[key] = count + 1
                    except Exception:
                        count = 1
                    if count:
                        result = dump(
                            {
                                "error": "相同工具参数已执行过，请使用现有结果或修改查询。"
                            }
                        )
                    elif name == "open_tools":
                        result = await self.tools.expand(plan, ready, arg, meta, turn.current, turn.batch_rows)
                    else:
                        result = await self.tools.execute(
                            ready,
                            name,
                            arg,
                            meta,
                            cfg,
                            turn.event,
                            turn.current,
                            turn.batch_rows,
                        )
                results.append((call_id, result))
            tool_results.append(self.host.tool_result(resp, results))
            rounds += 1
        return fallback
