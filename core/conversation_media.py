"""Dispatch explicit authorized media requests without giving models path or URL access."""

from __future__ import annotations

import asyncio
import hashlib
import re
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory

from .dashscope_media import DashScopeMedia, duration_seconds, read_media
from .media import MeteredResult
from .telemetry import BudgetExceeded, ModelFailure, _model_failure
from .util import contains_dsml, now, plain_reply_text

DRAW_REQUEST = re.compile(r"^(?:请|帮我)?\s*(画图|画一张|生成图片|文生图|编辑图片|改图)(?:\s*[:：]\s*|[ \t]+)(.+)$", re.S)
TRANSCRIBE_REQUEST = re.compile(r"^(?:请|帮我)?\s*(?:转文字|语音转文字|转写(?:这条)?语音|转录)(?:\s|[:：。！!？?]|$)")
OMNI_REQUEST = re.compile(r"^(?:请|帮我)?\s*(?:分析视频|分析音频|理解视频|听一下|看看视频|分析这段视频|分析这段音频)(?:\s|[:：。！!？?]|$)")
VOICE_REQUEST = re.compile(r"^(?:请|帮我)?\s*(?:用语音(?:回答|回复|说)|语音回复)(?:\s|[:：。！!？?]|$)")
AUDIO = {"audio/wav", "audio/mpeg"}
VIDEO = {"video/mp4"}
IMAGE = {"image/png", "image/jpeg"}


@dataclass(frozen=True)
class RecentMedia:
    message: int
    sender: str
    timestamp: float
    kind: str
    component: object


@dataclass(frozen=True)
class Delivery:
    attempted: bool = False
    submitted: int = 0


def request_text(info, cfg):
    text = str(info.get("original") or "").strip()
    for name in sorted(cfg["persona"]["names"], key=len, reverse=True):
        if name and text.startswith(name):
            return text[len(name):].lstrip("，,：: ")
    return text


def explicit_request(info, cfg):
    text = request_text(info, cfg)
    if match := DRAW_REQUEST.match(text):
        prompt = match.group(2).strip().strip("：:").strip()
        if not prompt:
            return None
        count = 1
        if quantity := re.match(r"^(\d{1,3})\s*张\s*[:：]\s*(.+)$", prompt, re.S):
            count, prompt = int(quantity.group(1)), quantity.group(2).strip()
        return "image", prompt, match.group(1) in {"编辑图片", "改图"}, count
    if TRANSCRIBE_REQUEST.match(text):
        return "stt", text, False, 0
    if OMNI_REQUEST.match(text):
        return "omni", text, False, 0
    return None


async def interruptible(operation, interrupted):
    pending = asyncio.create_task(operation)
    signal = asyncio.create_task(interrupted.wait())
    try:
        await asyncio.wait((pending, signal), return_when=asyncio.FIRST_COMPLETED)
        return None if interrupted.is_set() else await pending
    finally:
        for task in (pending, signal):
            if not task.done():
                task.cancel()
        await asyncio.gather(pending, signal, return_exceptions=True)


class ConversationMedia:
    def __init__(self, host, store, meter, config, log):
        self.host, self.store, self.meter, self.config, self.log = host, store, meter, config, log
        self.cache = OrderedDict()

    def api(self):
        return DashScopeMedia(self.config.snapshot()["media"]["api_origin"])

    async def caption(self, kind, meta, target, item, question, interrupt):
        if not await self.meter.authorized(kind, meta):
            raise ModelFailure("provider_unavailable")
        path = await asyncio.wait_for(self.host.media_file(item.component), timeout=8)
        data, mime = await asyncio.to_thread(read_media, path, AUDIO if kind == "stt" else AUDIO | VIDEO)
        seconds = await asyncio.to_thread(duration_seconds, path, data, mime)
        question = question[:400]
        settings = self.config.snapshot()["media"][kind]
        if seconds > (min(120, settings["max_units"]) if kind == "stt" else 60):
            raise BudgetExceeded("媒体时长超限")
        provider = settings["provider"]
        digest = hashlib.sha256(data).hexdigest()
        key = (kind, meta["scope"], provider, digest, question if kind == "omni" else "")
        if (cached := self.cache.get(key)) and cached[0] > now() and await self.meter.authorized(kind, meta):
            self.cache.move_to_end(key)
            return cached[1]
        if interrupt.is_set():
            return None

        async def analyze(live):
            if live["provider"] != provider:
                raise ModelFailure("provider_unavailable")
            if kind == "omni":
                return await self.api().understand(provider, data, mime, question[:400], live["max_units"])
            if provider == "fun-asr-flash-2026-06-15":
                return await self.api().transcribe(provider, data, mime, seconds)
            text = plain_reply_text(await self.host.transcribe(provider, path))
            if not text:
                raise ModelFailure("invalid_response")
            return MeteredResult(text[:4000], seconds, True)

        reserve = seconds if kind == "stt" else settings["max_units"]
        text = await interruptible(self.meter.run(kind, meta, reserve, analyze), interrupt)
        if text and not contains_dsml(text) and await self.meter.authorized(kind, meta):
            self.cache[key] = (now() + 300, text)
            if len(self.cache) > 128:
                self.cache.popitem(last=False)
            return text
        return None

    @staticmethod
    def related_audio(state, target, kind):
        cutoff = now() - 45
        valid = [item for item in state.media
                 if item.sender == target.row["uid"] and item.timestamp >= cutoff
                 and item.message <= target.row["seq"]
                 and (kind == "omni" or item.kind == "audio")]
        if kind == "omni":
            request = target.info.get("original", "")
            if "视频" in request:
                valid = [item for item in valid if item.kind == "video"]
            elif "音频" in request:
                valid = [item for item in valid if item.kind == "audio"]
        return valid[-1] if valid else None

    async def references(self, meta, state, target, interruption):
        cutoff = now() - 45
        candidates = [image for image in state.images if image.sender == target.row["uid"]
                      and image.timestamp >= cutoff and image.message <= target.row["seq"]
                      and (image.source == image.sender or image.message == target.row["seq"])]
        profiles = await self.store.call("scope_people", meta["scope"])
        blocked = {person["uid"] for person in profiles if person["blocked"]}
        blocked.update(self.config.snapshot()["access"]["blocked_users"])
        result = []
        for image in candidates[-2:]:
            if image.source in blocked or interruption.is_set() or not await self.meter.authorized("image", meta):
                continue
            path = await asyncio.wait_for(self.host.image_file(image.component), timeout=8)
            result.append((image.source, await asyncio.to_thread(read_media, path, IMAGE)))
        return result

    async def draw(self, meta, state, target, prompt, edit, count, interruption):
        if not prompt or len(prompt) > 1000 or contains_dsml(prompt):
            raise ModelFailure("invalid_response")
        if not self.host.media_supported(target.event, "image") or not await self.meter.authorized("image", meta):
            raise ModelFailure("unsupported_modality")
        if not 1 <= count <= self.config.snapshot()["media"]["image"]["max_units"]:
            raise BudgetExceeded("图片张数超过单次额度")
        references = await self.references(meta, state, target, interruption) if edit else []
        if edit and not references:
            return None
        if interruption.is_set():
            return None

        async def generate(live):
            if references:
                profiles = await self.store.call("scope_people", meta["scope"])
                blocked = {person["uid"] for person in profiles if person["blocked"]}
                blocked.update(self.config.snapshot()["access"]["blocked_users"])
                if any(source in blocked for source, _ in references):
                    raise ModelFailure("provider_unavailable")
            if interruption.is_set():
                raise ModelFailure("provider_unavailable")
            return await self.api().draw(live["provider"], prompt, count, live["size"],
                                         [file for _, file in references])

        generated = await interruptible(self.meter.run("image", meta, count, generate), interruption)
        if not generated:
            return Delivery()
        return await self.deliver("image", meta, target, generated, interruption)

    async def speak(self, meta, target, text, interruption):
        if not self.host.media_supported(target.event, "audio") or contains_dsml(text):
            return Delivery()
        text = plain_reply_text(text)
        if not text:
            return Delivery()

        async def synthesize(live):
            source = await self.host.synthesize(live["provider"], text)
            try:
                data, mime = await asyncio.to_thread(read_media, source, AUDIO)
                duration = await asyncio.to_thread(duration_seconds, source, data, mime)
                if duration > 60:
                    raise ValueError
            except (OSError, TypeError, ValueError, ModelFailure):
                # Synthesis already ran. The result is unusable but may have incurred a charge.
                raise ModelFailure("invalid_response") from None
            return MeteredResult((data, mime), len(text), True)

        audio = await interruptible(self.meter.run("tts", meta, len(text), synthesize), interruption)
        if audio is None:
            return Delivery()
        return await self.deliver("tts", meta, target, [audio], interruption)

    async def deliver(self, kind, meta, target, media, interruption):
        if interruption.is_set() or not await self.meter.authorized(kind, meta):
            return Delivery()
        format_hint = "image" if kind == "image" else "audio"
        submitted = 0
        attempted = False
        with TemporaryDirectory(prefix="fiyu-media-") as directory:
            for index, item in enumerate(media):
                if interruption.is_set() or not await self.meter.authorized(kind, meta):
                    break
                data, ext = ((item, ".png") if kind == "image"
                             else (item[0], ".wav" if item[1] == "audio/wav" else ".mp3"))
                path = Path(directory) / (str(index) + ext)
                path.write_bytes(data)
                attempted = True
                try:
                    ok = await asyncio.wait_for(self.host.send_media(target.event, format_hint, str(path)), timeout=20)
                except Exception as exc:
                    self.log.emit("MEDIA", "submission_unknown", "WARNING", scope=meta["scope"],
                                  task=kind, **_model_failure(exc).log_fields())
                    break
                if not ok:
                    self.log.emit("MEDIA", "submission_unknown", "WARNING", scope=meta["scope"], task=kind)
                    break
                submitted += 1
                self.log.emit("MEDIA", "submitted_to_adapter", scope=meta["scope"], task=kind, count=1)
        return Delivery(attempted, submitted)
