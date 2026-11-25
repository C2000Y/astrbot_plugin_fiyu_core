"""AstrBot 4.29.x integration. Framework-specific APIs live here."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from pathlib import Path
from urllib.parse import urlparse

import aiohttp
from astrbot.api.event import MessageChain
from astrbot.api.message_components import At, AtAll, Image, Plain, Poke, Record, Reply, Video
from astrbot.core.agent.message import (
    AssistantMessageSegment,
    TextPart,
    ThinkPart,
    ToolCallMessageSegment,
)
from astrbot.core.agent.tool import FunctionTool, ToolSet
from astrbot.core.exceptions import EmptyModelOutputError, ProviderNotFoundError
from astrbot.core.provider.entities import ToolCallsResult
from openai import APIConnectionError, APITimeoutError

from .core.embedding import EmbeddingProtocolError, EmbeddingProviderError, EmbeddingResult, ordered_vectors
from .core.telemetry import BalanceCheckError, ModelFailure
from .core.util import clean, contains_dsml, dump, ident, now, plain_reply_text, unsafe_secret


SEARCH_TOOLS = {
    "tavily": "web_search_tavily",
    "bocha": "web_search_bocha",
    "brave": "web_search_brave",
    "firecrawl": "web_search_firecrawl",
    "baidu_ai_search": "web_search_baidu",
    "exa": "web_search_exa",
    "anysearch": "web_search_anysearch",
    "deepseek": "web_search_deepseek",
    "serpapi": "web_search_serpapi",
}


class AstrHost:
    def __init__(self, context):
        self.context = context

    async def chat_provider(self, umo):
        try:
            return await self.context.get_current_chat_provider_id(umo=umo)
        except ProviderNotFoundError:
            raise ModelFailure("provider_unavailable") from None

    def ai_enabled(self, event):
        return bool(
            self.context.get_config(umo=event.unified_msg_origin)
            .get("provider_settings", {})
            .get("enable", True)
        )

    async def persona(self, event, cfg):
        manager = self.context.persona_manager
        requested = cfg["persona"]["persona_id"]
        selected = None
        persona_id = requested
        try:
            if requested:
                selected = manager.get_persona_v3_by_id(requested)
            else:
                cm = self.context.conversation_manager
                cid = await cm.get_curr_conversation_id(event.unified_msg_origin)
                conv = (
                    await cm.get_conversation(event.unified_msg_origin, cid)
                    if cid
                    else None
                )
                persona_id, selected, _, _ = await manager.resolve_selected_persona(
                    umo=event.unified_msg_origin,
                    conversation_persona_id=getattr(conv, "persona_id", None),
                    platform_name=event.get_platform_name(),
                )
        except Exception:
            # Fail closed for persona scope: a transient resolver failure gets a separate fallback namespace.
            selected = None
            persona_id = "error:" + (requested or "session")
        if selected:
            examples = []
            for x in selected.get("_begin_dialogs_processed", [])[:16]:
                if x.get("role") in ("user", "assistant") and isinstance(
                    x.get("content"), str
                ):
                    examples.append({"role": x["role"], "content": x["content"][:1600]})
            if examples and len(examples) % 2:
                examples.pop()
            return {
                "id": str(persona_id or selected.get("name", "default")),
                "system": str(selected.get("prompt", ""))[:24000],
                "examples": examples,
            }
        return {
            "id": "fallback:" + str(persona_id or "default"),
            "system": cfg["persona"]["fallback_prompt"],
            "examples": [],
        }

    def info(self, event):
        msgs = list(event.get_messages() or [])
        raw = getattr(event.message_obj, "raw_message", None)
        platform = str(event.get_platform_name())
        uid = str(event.get_sender_id() or "")
        selfid = str(event.get_self_id() or "")
        group = str(event.get_group_id() or "")
        if (
            platform == "aiocqhttp"
            and isinstance(raw, dict)
            and raw.get("group_id")
            and not group
        ):
            group = str(raw["group_id"])
        private = event.is_private_chat() and not group
        if not group and not private:
            return None
        poke = False
        other_poke = False
        if (
            isinstance(raw, dict)
            and platform == "aiocqhttp"
            and raw.get("post_type") == "notice"
            and raw.get("notice_type") == "notify"
            and raw.get("sub_type") == "poke"
        ):
            uid = str(raw.get("user_id") or uid)
            target = str(raw.get("target_id") or "")
            poke = bool(target and target == str(raw.get("self_id") or selfid))
            other_poke = not poke
        for message in msgs:
            if isinstance(message, Poke):
                target = message.target_id()
                if target and selfid and target == selfid:
                    poke = True
                elif target:
                    other_poke = True
        at = any(
            isinstance(m, At) and not isinstance(m, AtAll) and str(m.qq) == selfid
            for m in msgs
        )
        at_others = any(
            isinstance(m, At) and (isinstance(m, AtAll) or str(m.qq) != selfid)
            for m in msgs
        )
        quotes = [m for m in msgs if isinstance(m, Reply)]
        mentioned_uids = list(dict.fromkeys(str(m.qq) for m in msgs
            if isinstance(m, At) and not isinstance(m, AtAll) and m.qq and str(m.qq) not in (selfid, "all")))[:12]
        quoted_uids = list(dict.fromkeys(str(q.sender_id) for q in quotes
            if q.sender_id and str(q.sender_id) != selfid))[:12]
        quote_bot = any(m.sender_id and str(m.sender_id) == selfid for m in quotes)
        unverified_quotes = [
            clean(q.message_str or q.text, 1800) for q in quotes if not q.sender_id
        ]
        direct = (
            private
            or at
            or quote_bot
            or (bool(getattr(event, "is_at_or_wake_command", False)) and not at_others)
        )
        original = "".join(m.text for m in msgs if isinstance(m, Plain)) or str(
            event.message_str or ""
        )
        text = str(event.message_str or "").strip()
        for q in quotes[:2]:
            qt = clean(getattr(q, "message_str", None) or getattr(q, "text", None), 500)
            if qt:
                text += f"\n[引用消息：{clean(q.sender_nickname, 50) or '群友'}：{qt}]"
        if at_others:
            text += " [提及群友或全体成员]"
        images = [m for m in msgs if isinstance(m, Image)]
        image_senders = [uid] * len(images)
        # QQ may expose quoted images as Reply.chain; use only real adapter components.
        for q in quotes[:2]:
            quoted_images = [m for m in (q.chain or []) if isinstance(m, Image)]
            images.extend(quoted_images)
            image_senders.extend([str(q.sender_id or uid)] * len(quoted_images))
        if images:
            text += f" [图片×{min(len(images), 20)}]"
        media = [("audio" if isinstance(m, Record) else "video", m)
                 for m in msgs if isinstance(m, (Record, Video))][:4]
        for kind, _ in media:
            text += " [语音]" if kind == "audio" else " [视频]"
        if poke:
            text = "[戳了一下肥鱼]"
        ext_id = str(getattr(event.message_obj, "message_id", "") or "")
        if not ext_id:
            ext_id = "event-" + ident(
                str(getattr(event, "created_at", now())) + uid + text, 24
            )
        return {
            "platform": platform,
            "platform_id": str(event.get_platform_id()),
            "group": group,
            "kind": "private" if private else "group",
            "location": uid if private else group,
            "umo": str(event.unified_msg_origin),
            "uid": uid,
            "name": str(event.get_sender_name() or ""),
            "self_message": bool(uid and selfid and uid == selfid),
            "ext_id": ext_id,
            "text": text.strip(),
            "original": original.strip(),
            "directed": direct,
            "direct_reason": "poke"
            if poke
            else "private"
            if private
            else "at"
            if at
            else "quote"
            if quote_bot
            else "wake",
            "poke": poke,
            "other_poke": other_poke,
            "at_others": at_others,
            "mentioned_uids": mentioned_uids,
            "quoted_uids": quoted_uids,
            "images": images[:20],
            "image_senders": image_senders[:20],
            "media": media,
            "unverified_quotes": unverified_quotes,
        }

    @staticmethod
    def is_command(event, info, prefixes):
        if any(info["original"].startswith(p) for p in prefixes if p):
            return True
        # Let real registered commands (including custom prefixes) pass to AstrBot.
        handlers = event.get_extra("activated_handlers", []) or []
        for h in handlers:
            if any(
                type(f).__name__
                in ("CommandFilter", "CommandGroupFilter", "RegexFilter")
                for f in getattr(h, "event_filters", [])
            ):
                return True
        return False

    @staticmethod
    def consume(event):
        event.should_call_llm(True)
        event.stop_event()

    async def generate(
        self,
        provider,
        prompt,
        system,
        *,
        tools=None,
        contexts=None,
        tool_results=None,
        images=None,
    ):
        schemas = (
            ToolSet(
                [
                    FunctionTool(
                        name=t["name"],
                        description=t["description"],
                        parameters=t["parameters"],
                    )
                    for t in tools
                ]
            )
            if tools
            else None
        )
        # Reduce hidden network retries where the native provider supports this option.
        kwargs = {"request_max_retries": 1}
        if tool_results:
            kwargs["tool_calls_result"] = tool_results
        try:
            return await self.context.llm_generate(
                chat_provider_id=provider,
                prompt=prompt,
                system_prompt=system,
                contexts=contexts or [],
                image_urls=images or [],
                tools=schemas,
                **kwargs,
            )
        except APITimeoutError:
            raise ModelFailure("timeout") from None
        except (ProviderNotFoundError, APIConnectionError):
            raise ModelFailure("provider_unavailable") from None
        except EmptyModelOutputError:
            raise ModelFailure("invalid_response") from None

    @staticmethod
    def tool_result(response, results: list[tuple[str, str]]):
        # Use the same message model as AstrBot's native runner; preserve DeepSeek reasoning within this turn only.
        parts = []
        if getattr(response, "reasoning_content", None) is not None or getattr(
            response, "reasoning_signature", None
        ):
            parts.append(
                ThinkPart(
                    think=response.reasoning_content or "",
                    encrypted=getattr(response, "reasoning_signature", None),
                )
            )
        if response.completion_text and not contains_dsml(response.completion_text):
            parts.append(TextPart(text=response.completion_text))
        return ToolCallsResult(
            tool_calls_info=AssistantMessageSegment(
                content=parts or None, tool_calls=response.to_openai_tool_calls_model()
            ),
            tool_calls_result=[
                ToolCallMessageSegment(tool_call_id=i, content=[TextPart(text=s)])
                for i, s in results
            ],
        )

    async def embed(self, pid, texts):
        if not texts:
            return []
        p = self.context.get_provider_by_id(pid)
        if not p or not hasattr(p, "get_embeddings"):
            raise ModelFailure("provider_unavailable")
        try:
            adapter = self._embedding_adapter(p)
            if adapter == "openai":
                # Reuse the initialized provider client and request settings. Its public
                # list-only helper discards indices and uses SDK retries by default.
                response = await p.client.with_options(max_retries=0).embeddings.create(
                    input=texts, model=p.model, **p._embedding_kwargs()
                )
                rows = getattr(response, "data", None)
                if not isinstance(rows, list):
                    raise EmbeddingProtocolError("Embedding 响应缺少向量列表")
                vectors = ordered_vectors(
                    ((getattr(row, "index", None), getattr(row, "embedding", None)) for row in rows), len(texts)
                )
                usage = getattr(response, "usage", None)
                return EmbeddingResult(vectors, getattr(usage, "prompt_tokens", None))
            if adapter == "dashscope":
                return await self._dashscope_embedding(p, texts)
            return await p.get_embeddings(texts)
        except APITimeoutError:
            raise ModelFailure("timeout") from None
        except (ProviderNotFoundError, APIConnectionError):
            raise ModelFailure("provider_unavailable") from None

    @staticmethod
    def _embedding_adapter(provider):
        cls = type(provider)
        if cls.__module__ == "astrbot.core.provider.sources.openai_embedding_source" and cls.__name__ == "OpenAIEmbeddingProvider":
            return "openai"
        if cls.__module__ == "astrbot.core.provider.sources.dashscope_embedding_source" and cls.__name__ == "DashScopeEmbeddingProvider":
            return "dashscope"
        return "native"

    @staticmethod
    def _dashscope_multimodal(model):
        return "vl-embedding" in model or model.startswith(("multimodal-embedding", "tongyi-embedding-vision"))

    async def _dashscope_embedding(self, provider, texts):
        from dashscope import MultiModalEmbedding, TextEmbedding

        multimodal = self._dashscope_multimodal(provider.model)
        sdk = MultiModalEmbedding if multimodal else TextEmbedding
        options = {"base_address": provider.base_url, "headers": provider.request_headers.copy()}
        dimension = provider.get_dim()
        if dimension > 0:
            options["dimension"] = dimension
        # Keep the synchronous SDK's network timeout bounded even if the coroutine is cancelled.
        timeout = provider.provider_config.get("timeout", 20)
        options["request_timeout"] = max(1, min(120, int(timeout))) if str(timeout).isdigit() else 20
        response = await asyncio.to_thread(
            sdk.call, model=provider.model, api_key=provider.api_key,
            input=[{"text": text} for text in texts] if multimodal else texts, **options,
        )
        status = getattr(response, "status_code", None)
        if status != 200:
            raise EmbeddingProviderError(status, getattr(response, "code", None))
        output = getattr(response, "output", None)
        rows = output.get("embeddings") if isinstance(output, Mapping) else None
        if not isinstance(rows, list):
            raise EmbeddingProtocolError("Embedding 响应缺少向量列表")
        indexed = []
        for row in rows:
            if not isinstance(row, Mapping):
                raise EmbeddingProtocolError("Embedding 响应条目异常")
            index = row.get("text_index", row.get("index"))
            if "index" in row and row["index"] != index:
                raise EmbeddingProtocolError("Embedding 响应位置冲突")
            indexed.append((index, row.get("embedding")))
        usage = getattr(response, "usage", None)
        tokens = usage.get("total_tokens") if isinstance(usage, Mapping) else None
        return EmbeddingResult(ordered_vectors(indexed, len(texts)), tokens)

    def embedding_fingerprint(self, pid):
        info = self.embedding_metadata(pid)
        return info["identity"] if info else ""

    def embedding_metadata(self, pid):
        if not pid:
            return None
        p = self.context.get_provider_by_id(pid)
        if not p or not hasattr(p, "get_embeddings"):
            return None
        cfg = getattr(p, "provider_config", {})
        model = str(getattr(p, "model", "") or p.get_model() or cfg.get("embedding_model", ""))
        adapter = self._embedding_adapter(p)
        endpoint = str(cfg.get("embedding_api_base", cfg.get("api_base", "")))
        dimension, input_format = 0, "texts"
        if adapter == "openai":
            endpoint = str(p.client.base_url)
            dimension = p._embedding_kwargs().get("dimensions", 0)
        elif adapter == "dashscope":
            endpoint = str(p.base_url)
            dimension = p.get_dim()
            if self._dashscope_multimodal(model):
                input_format = "text_objects"
        dimension = dimension if type(dimension) is int and 1 <= dimension <= 8192 else 0
        return {
            "identity": ident(dump([pid, adapter, type(p).__qualname__, model, endpoint, dimension]), 32),
            "model": model, "dimension": dimension, "input_format": input_format,
        }

    async def kb(self, name, query, top_k):
        manager = self.context.kb_manager
        helper = await manager.get_kb_by_name(name)
        if not helper or getattr(helper, "init_error", None):
            return {"error": "知识库不存在或未初始化"}
        result = await manager.retrieve(
            query=query,
            kb_names=[name],
            top_k_fusion=max(20, top_k * 3),
            top_m_final=top_k,
        )
        if not result or not result.get("results"):
            return {"results": [], "message": "未检索到相关资料，可调整查询词后重试。"}
        return {
            "results": [
                {
                    "source": x.get("doc_name", ""),
                    "content": str(x.get("content", ""))[:1800],
                    "chunk": x.get("chunk_index"),
                }
                for x in result["results"]
            ]
        }

    async def web_search(self, event, query):
        from astrbot.core.astr_agent_context import (
            AgentContextWrapper,
            AstrAgentContext,
        )
        from astrbot.core.tools.registry import get_builtin_tool_class

        p = self.context.get_config(umo=event.unified_msg_origin).get(
            "provider_settings", {}
        )
        if not p.get("web_search") or p.get("websearch_provider") not in SEARCH_TOOLS:
            return {"error": "请在该会话的 AstrBot 配置文件中启用并配置原生网页搜索。"}
        if unsafe_secret(query):
            return {"error": "搜索词包含敏感标识，请仅查询公开信息。"}
        cls = get_builtin_tool_class(SEARCH_TOOLS[p["websearch_provider"]])
        if not cls:
            return {"error": "该搜索服务未加载"}
        context = AgentContextWrapper(
            context=AstrAgentContext(context=self.context, event=event)
        )
        if p["websearch_provider"] == "deepseek":
            query = query[:128]
        result = await cls().call(context, query=query)
        if isinstance(result, str):
            return result
        return (
            "\n".join(
                str(getattr(x, "text", "")) for x in getattr(result, "content", [])
            )
            or "没有搜索结果"
        )

    async def image_file(self, component):
        path = Path(await component.convert_to_file_path())
        if not path.is_file() or path.stat().st_size > 12 * 1024 * 1024:
            raise ValueError("图片不可读或超过12MB")
        return str(path)

    async def media_file(self, component):
        if not isinstance(component, (Record, Video)):
            raise ModelFailure("unsupported_modality")
        path = Path(await component.convert_to_file_path())
        if not path.is_file() or not 0 < path.stat().st_size <= 7 * 1024 * 1024:
            raise ModelFailure("unsupported_modality")
        return str(path)

    async def synthesize(self, provider_id, text):
        provider = self.context.get_provider_by_id(provider_id)
        if not provider or not callable(getattr(provider, "get_audio", None)):
            raise ModelFailure("provider_unavailable")
        return await provider.get_audio(text)

    async def transcribe(self, provider_id, path):
        provider = self.context.get_provider_by_id(provider_id)
        if not provider or not callable(getattr(provider, "get_text", None)):
            raise ModelFailure("provider_unavailable")
        return await provider.get_text(path)

    @staticmethod
    def media_supported(event, kind):
        return kind in ("image", "audio") and event.get_platform_name() in {
            "aiocqhttp", "qq_official", "qq_official_webhook"
        }

    @staticmethod
    async def send_media(event, kind, path):
        if not AstrHost.media_supported(event, kind):
            return False
        component = Image.fromFileSystem(path) if kind == "image" else Record.fromFileSystem(path)
        await event.send(MessageChain([component]))
        return True

    @staticmethod
    async def send(event, text):
        # Only plain text from the model. Never interpret model-generated @/CQ syntax as commands.
        text = plain_reply_text(text)
        if not text:
            return False
        await event.send(MessageChain().message(text))
        # Adapter's event.send has no uniform delivery receipt; no claim of client delivery.
        return True

    async def notify(self, umo, text, mention=""):
        chain = MessageChain()
        if mention:
            chain.chain.append(At(qq=mention))
        chain.message(text)
        return await self.context.send_message(umo, chain)

    async def balance(self, pid, currency, timeout=20):
        p = self.context.get_provider_by_id(pid)
        if not p or not hasattr(p, "get_keys"):
            raise BalanceCheckError("provider")
        try:
            base = urlparse(str(getattr(p, "provider_config", {}).get("api_base") or ""))
            port = base.port
        except ValueError:
            raise BalanceCheckError("endpoint") from None
        if (
            base.scheme != "https"
            or base.hostname != "api.deepseek.com"
            or port not in (None, 443)
            or base.username
            or base.password
            or base.path.rstrip("/") not in ("", "/v1")
        ):
            raise BalanceCheckError("endpoint")
        keys = p.get_keys()
        if not keys or not keys[0]:
            raise BalanceCheckError("key")
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=timeout)
        ) as s:
            async with s.get(
                "https://api.deepseek.com/user/balance",
                headers={"Authorization": "Bearer " + keys[0]},
                allow_redirects=False,
            ) as response:
                if response.status != 200:
                    raise BalanceCheckError("http", response.status)
                try:
                    payload = await response.json()
                except (aiohttp.ContentTypeError, ValueError):
                    raise BalanceCheckError("payload") from None
        if not isinstance(payload, dict) or not isinstance(payload.get("balance_infos"), list):
            raise BalanceCheckError("payload")
        for b in payload["balance_infos"]:
            if not isinstance(b, dict):
                raise BalanceCheckError("payload")
            if b.get("currency") == currency:
                try:
                    value = float(b["total_balance"])
                except (KeyError, TypeError, ValueError, OverflowError):
                    raise BalanceCheckError("payload") from None
                if not -1e9 < value < 1e9:
                    raise BalanceCheckError("payload")
                return {
                    "amount": value,
                    "currency": currency,
                    "available": bool(payload.get("is_available")),
                }
        raise BalanceCheckError("currency")

    async def options(self):
        def providers(items):
            return [
                {"id": str(p.meta().id), "label": f"{p.meta().id} · {p.get_model()}"}
                for p in items
            ]

        chat = providers(self.context.get_all_providers())
        embedding = providers(self.context.get_all_embedding_providers())
        try:
            kbs = [
                {
                    "id": str(k.kb_id),
                    "name": str(k.kb_name),
                    "description": str(k.description or ""),
                }
                for k in await self.context.kb_manager.list_kbs()
            ]
        except Exception:
            kbs = []
        try:
            personas = [
                {"id": str(p.persona_id), "label": str(p.persona_id)}
                for p in await self.context.persona_manager.get_all_personas()
            ]
        except Exception:
            personas = []
        return {
            "chat_provider": chat,
            "embedding_provider": embedding,
            "tts_provider": providers(self.context.get_all_tts_providers())
                if hasattr(self.context, "get_all_tts_providers") else [],
            "stt_provider": providers(self.context.get_all_stt_providers())
                if hasattr(self.context, "get_all_stt_providers") else [],
            "persona": personas,
            "knowledge_bases": kbs,
        }
