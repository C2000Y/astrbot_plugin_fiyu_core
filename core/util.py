from __future__ import annotations

import hashlib
import html
import json
import os
import re
import tempfile
import time
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

CN_TZ = timezone(timedelta(hours=8), "Asia/Shanghai")
MAX_REPLY_CHARS = 4000


def now() -> float:
    return time.time()


def day_key(ts: float | None = None) -> str:
    return datetime.fromtimestamp(now() if ts is None else ts, CN_TZ).strftime(
        "%Y-%m-%d"
    )


def stamp(ts: float | None = None) -> str:
    return datetime.fromtimestamp(now() if ts is None else ts, CN_TZ).isoformat(
        timespec="seconds"
    )


def dump(value: Any, *, pretty: bool = False) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        indent=2 if pretty else None,
        separators=None if pretty else (",", ":"),
    )


def read_json(path: Path) -> Any:
    with path.open(encoding="utf-8-sig") as f:
        return json.load(f)


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".fiyu-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(dump(value, pretty=True) + "\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def ident(value: str, length: int = 12) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:length]


def normalized(value: Any) -> str:
    return " ".join(unicodedata.normalize("NFKC", str(value or "")).casefold().split())


def entity_key(value: Any) -> str:
    return re.sub(r"[\W_]+", "", normalized(value), flags=re.UNICODE)


def scope_key(platform: str, kind: str, location: str, persona: str) -> str:
    return ident(dump([platform, kind, location, persona]), 24)


def user_ref(scope: str, user_id: str) -> str:
    return "U" + ident(scope + "\0" + user_id, 12)


SECRET_PATTERNS = (
    re.compile(r"(?i)\b(?:sk|abk|ghp|xoxb|AKIA)[-_][A-Za-z0-9_\-]{12,}\b"),
    re.compile(
        r"(?i)(?:bearer\s+|(?:api[ _-]?key|access[ _-]?token|secret|password|passwd|密码|密钥|令牌)\s*[=:：]\s*)[^\s,;，；]+"
    ),
    re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"),
    re.compile(r"(?<![0-9])\d{17}[0-9Xx](?![0-9])"),
    re.compile(r"(?i)<qqbot-at-user\s+id=[\"'][^\"']+[\"']\s*/?>"),
    re.compile(r"(?i)<@!?[\w-]+>|\[CQ:at,[^\]]+\]"),
    re.compile(r"(?i)\b[A-F0-9]{32,}\b"),
    re.compile(r"(?i)[A-Za-z0-9_.+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}"),
)


def clean(value: Any, limit: int = 2000, *, urls: bool = False) -> str:
    text = str(value or "").replace("\x00", "")
    text = "".join(c for c in text if c in "\n\t" or (ord(c) >= 32 and ord(c) != 127))
    for pattern in SECRET_PATTERNS:
        text = pattern.sub("[已隐藏]", text)
    if not urls:
        text = re.sub(r"https?://[^\s<>\"']+", "[链接]", text, flags=re.I)
    return text.strip()[:limit]


def unsafe_secret(value: Any) -> bool:
    text = str(value or "")
    return any(pattern.search(text) for pattern in SECRET_PATTERNS)


def html_text(value: Any) -> str:
    text = re.sub(r"(?i)</h[1-6]>|<br\s*/?>|</(?:p|li)>", "\n", str(value or ""))
    return html.unescape(re.sub(r"<[^>]*>", "", text)).strip()


def json_value(text: str) -> Any:
    """Accept a JSON object/array or a single fenced JSON block, never eval."""
    text = str(text or "").strip()
    if text.startswith("```"):
        match = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", text, re.I | re.S)
        if match:
            text = match.group(1)
    return json.loads(
        text,
        parse_constant=lambda val: (_ for _ in ()).throw(
            ValueError(f"非法数值: {val}")
        ),
    )


def number(value: Any, default: float, low: float, high: float) -> float:
    if isinstance(value, bool):
        return default
    try:
        val = float(value)
    except (ValueError, TypeError, OverflowError):
        return default
    if not low <= val <= high:
        return default
    return val


def parse_time(value: Any) -> float | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=CN_TZ)
        return parsed.timestamp()
    except (ValueError, OverflowError, OSError):
        return None


def terms(value: str) -> set[str]:
    text = normalized(value)
    out = set(re.findall(r"[a-z0-9_]+", text))
    for segment in re.findall(r"[\u3400-\u9fff]+", text):
        if len(segment) == 1:
            out.add(segment)
        else:
            out.update(segment[i : i + 2] for i in range(len(segment) - 1))
    return out


def lexical(query: str, text: str) -> float:
    q = normalized(query)
    hay = normalized(text)
    if not q or not hay:
        return 0.0
    if q in hay:
        return 1.0
    qt, ht = terms(q), terms(hay)
    return len(qt & ht) / len(qt) if qt else 0.0


def conservative_tokens(text: str) -> int:
    # Provider usage is preferred; this is only a labeled estimate/reservation.
    cjk = len(re.findall(r"[\u2e80-\u9fff]", text))
    return max(16, cjk * 2 + (len(text) - cjk + 2) // 3 + 24)


_ORDERED_LIST_MARKER = re.compile(r"^( {0,3})([0-9]{1,9})[.)][ \t]+(?=\S)")
_MARKDOWN_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})([^\r\n]*)")
_NESTED_FENCE = re.compile(
    r"^[ \t]*(?:[-+*]|[0-9]{1,9}[.)])[ \t]+(?:`{3,}|~{3,})"
)


def qq_literal_ordered_lists(text: str) -> str:
    """Keep explicit list numbers in QQ's per-message Markdown renderer.

    Only rewrite plain ordered-list prefixes, before message splitting. Never
    calculate new numbers or modify the content inside fenced/indented code.
    """
    output = []
    fence = None
    for line in text.splitlines(keepends=True):
        marker = _MARKDOWN_FENCE.match(line)
        if fence is not None:
            if (
                marker
                and marker[1][0] == fence[0]
                and len(marker[1]) >= fence[1]
                and not marker[2].strip()
            ):
                fence = None
        elif marker and (marker[1][0] == "~" or "`" not in marker[2]):
            fence = (marker[1][0], len(marker[1]))
        elif _NESTED_FENCE.match(line):
            # A list can itself contain a code block. Keep this complex
            # Markdown intact instead of removing its containing list marker.
            return text
        else:
            line = _ORDERED_LIST_MARKER.sub(r"\g<1>\g<2>、", line, count=1)
        output.append(line)
    return "".join(output)


_ADAPTER_MARKUP = re.compile(r"(?i)<qqbot-at-user\b[^>]*>|<@!?[\w-]+>|\[CQ:[^\]]+\]")
_DSML_MARKUP = re.compile(r"<\s*/?\s*(?:\|\s*)+dsml\b", re.IGNORECASE)
_FORMAT_ROW = re.compile(r"^\s*(?:[-+*]\s+|\d{1,9}(?:[.)]\s+|、)|>|[|｜])")
_HEADING = re.compile(r"^ {0,3}#{1,6}\s")
_FENCE_PREFIX = re.compile(r"^(?:\s*>\s*)*(?:[ \t]*(?:[-+*]|\d{1,9}[.)])\s+)?[ \t]*")
_SENTENCE_END = re.compile(r"""(?:[。！？!?；;…]+|(?<!\d)\.(?=\s|$))["'”’）】」』]*""")
_WORD_BREAK = re.compile(r"[\s，,、]+")
_INLINE_LITERAL = re.compile(r"https?://[^\s<>()\u3000-\u303f，。！？]+|(`+)[^`\n]+\1")
_TABLE_SEPARATOR = re.compile(r"^\s*\|?\s*:?-+:?\s*(?:\|\s*:?-+:?\s*)+\|?\s*$")


def _trim_blank_lines(text: str) -> str:
    """Keep indentation and internal line breaks in the visible part."""
    first = len(text) - len(text.lstrip())
    if first == len(text):
        return ""
    start = text.rfind("\n", 0, first) + 1
    end = text.find("\n", len(text.rstrip()))
    return text[start:end if end >= 0 else len(text)]


def plain_reply_text(text: str) -> str:
    value = _ADAPTER_MARKUP.sub("", str(text or "")).replace("\r\n", "\n").replace("\r", "\n")
    return _trim_blank_lines(value)


def contains_dsml(text: str) -> bool:
    """Detect raw DeepSeek control tags, including spaced/full-width or incomplete tags."""
    return bool(_DSML_MARKUP.search(unicodedata.normalize("NFKC", str(text or ""))))


def _reply_units(text: str):
    """Yield original text ranges. A fenced block is a single indivisible unit."""
    fence = None
    pos = 0
    lines = text.split("\n")
    previous_table = False
    for index, line in enumerate(lines):
        start, end = pos, pos + len(line)
        pos = end + 1
        in_table = "|" in line and (previous_table or line.lstrip().startswith("|")
                    or (index + 1 < len(lines) and _TABLE_SEPARATOR.fullmatch(lines[index + 1])))
        previous_table = False
        prefix = _FENCE_PREFIX.match(line)[0]
        marker = _MARKDOWN_FENCE.match(line[len(prefix):])
        if fence:
            if (marker and marker[1][0] == fence[0] and len(marker[1]) >= fence[1]
                    and not marker[2].strip() and not prefix.replace(">", "").strip()
                    and prefix.count(">") == fence[3]):
                yield fence[2], end, "code"
                fence = None
            continue
        if not line.strip():
            continue
        if marker and (marker[1][0] == "~" or "`" not in marker[2]):
            fence = (marker[1][0], len(marker[1]), start, prefix.count(">"))
        elif _HEADING.match(line):
            yield start, end, "heading"
        elif line.startswith(("    ", "\t")):
            yield start, end, "indent"
        elif in_table:
            previous_table = True
            yield start, end, "table"
        elif _FORMAT_ROW.match(line):
            yield start, end, "row"
        else:
            yield start, end, "prose"
    if fence:
        yield fence[2], len(text), "code"


def _prose_units(line: str, target: int):
    """Find readable length breaks and share the tail across remaining pieces."""
    offset = 0
    literals = list(_INLINE_LITERAL.finditer(line)) if len(line) > target else []
    while len(line) - offset > target:
        remaining = len(line) - offset
        count = (remaining + target - 1) // target
        aim = offset + (remaining + count - 1) // count
        low = offset + max(1, (aim - offset) // 2)
        high = offset + target
        def boundary(pattern):
            return min((m.end() for m in pattern.finditer(line, offset, high) if m.end() >= low),
                       key=lambda end: abs(end - aim), default=None)
        cut = boundary(_SENTENCE_END) or boundary(_WORD_BREAK) or aim
        for match in literals:
            if match.start() < cut < match.end():
                cut = match.start() if match.start() > offset else match.end()
                break
        yield offset, cut
        offset = cut
    if offset < len(line):
        yield offset, len(line)


def limit_reply(text: str, maximum: int) -> str:
    if len(text) <= maximum:
        return text
    suffix = "…（可继续问）"
    limit = maximum - len(suffix)
    for start, end, kind in _reply_units(text):
        if start < limit < end and kind == "code":
            opening = text[start:end].split("\n", 1)[0]
            prefix = _FENCE_PREFIX.match(opening)[0]
            marker = _MARKDOWN_FENCE.match(opening[len(prefix):])[1]
            closing = "\n" + re.sub(r"[^\s>]", " ", prefix) + marker + "\n"
            if limit - len(closing) <= start + len(opening):
                return text[:start].rstrip() + suffix
            return text[:limit - len(closing)].rstrip() + closing + suffix
        if start < limit < end and kind in ("row", "table") and start > 0:
            return text[:start].rstrip() + "\n" + suffix
    return text[:limit].rstrip() + suffix


def _limit_cuts(end: int, cuts: list[int], intent: set[int], slots: int) -> list[int]:
    if len(cuts) <= slots:
        return cuts
    # All model boundaries fit. Spend any remaining slots on the longest spans.
    kept = sorted(intent)
    while len(kept) < slots:
        edges = [0, *kept, end]
        for a, b in sorted(zip(edges, edges[1:]), key=lambda ab: ab[1] - ab[0], reverse=True):
            candidates = [c for c in cuts if a < c < b]
            if candidates:
                kept.append(min(candidates, key=lambda c: abs(2 * c - a - b)))
                kept.sort()
                break
    edges = [0, *kept, end]
    if len(kept) == slots and max(b - a for a, b in zip(edges, edges[1:])) <= 2 * end / (slots + 1):
        return kept
    # Too many paragraphs, or several tiny ones have left a disproportionately
    # large tail. Pick from all safe cuts, preferring nearby explicit breaks.
    choices = cuts
    kept = []
    start = 0
    for left in range(slots, 0, -1):
        share = (end - start) / (left + 1)
        aim = start + share
        index = min(range(len(choices) - left + 1),
                    key=lambda i: abs(choices[i] - aim) - (share / 4 if choices[i] in intent else 0))
        start = choices[index]
        kept.append(start)
        choices = choices[index + 1:]
    return kept


def split_reply(text: str, target: int, max_parts: int, *, on_newline: bool = True) -> list[str]:
    text = _trim_blank_lines(str(text or "").replace("\r\n", "\n").replace("\r", "\n"))
    if not text:
        return []
    if max_parts <= 1 or (len(text) <= target and (not on_newline or "\n" not in text)):
        return [text]
    target = max(1, target, (len(text) + max_parts - 1) // max_parts)
    cuts, intent = [], set()
    group_start, previous_end, previous = 0, 0, ""
    for start, end, kind in _reply_units(text):
        blank = text.count("\n", previous_end, start) > 1
        same_structure = previous == kind and kind in ("indent", "row", "table")
        new_bubble = previous and on_newline and (
            (blank and not same_structure)
            or (kind == "prose" and previous != "heading")
            or "code" in (kind, previous) or kind == "heading"
        )
        if new_bubble:
            intent.add(start)
        ranges = _prose_units(text[start:end], target) if kind == "prose" else [(0, end - start)]
        for lo, hi in ranges:
            a, b = start + lo, start + hi
            # Keep a heading or introductory line with the first row of a list.
            lead = lo == 0 and (previous == "heading" or (previous == "prose" and kind in ("row", "table")))
            keep_together = lead or (previous == kind and kind in ("indent", "table"))
            if a > group_start and (new_bubble or (b - group_start > target and not keep_together)):
                cuts.append(a)
                group_start = a
            new_bubble = False
        previous_end, previous = end, kind
    edges = [0, *_limit_cuts(len(text), cuts, intent, max_parts - 1), len(text)]
    return [part for a, b in zip(edges, edges[1:]) if (part := _trim_blank_lines(text[a:b]))]
