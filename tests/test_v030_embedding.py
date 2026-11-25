"""Offline checks for physical embedding batches and durable indexing."""

import asyncio
import json
import logging
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from astrbot_plugin_fiyu_core.core.config import ConfigError, defaults, validate
from astrbot_plugin_fiyu_core.core.embedding import (
    EmbeddingProtocolError, EmbeddingProviderError, EmbeddingResult, embedding_profile, ordered_vectors,
)
from astrbot_plugin_fiyu_core.core.memory import MemoryService
from astrbot_plugin_fiyu_core.core.store import Store
from astrbot_plugin_fiyu_core.core.telemetry import DailyLog, ModelFailure, ModelGateway
from astrbot_plugin_fiyu_core.core.util import day_key
from test_core import add_message, metadata


class TestEmbeddingCapabilities(unittest.TestCase):
    def test_known_limit_registration_and_overrides(self):
        known = {"model": "text-embedding-v4", "identity": "fixture"}
        overrides = {"fixture": {"max_texts": 30, "max_text_tokens": 16000}}
        profile = embedding_profile("fixture", known, overrides)
        self.assertEqual((profile.max_texts, profile.max_text_tokens), (10, 8192))
        self.assertIn(1024, profile.dimensions)
        unknown = embedding_profile("fixture", dict(known, model="custom"), overrides)
        self.assertEqual(unknown.max_texts, 30)
        self.assertEqual(validate({"models": {"embedding_limits": overrides}})["models"]["embedding_limits"], overrides)
        with self.assertRaises(ConfigError):
            validate({"models": {"embedding_limits": {"fixture": {"max_texts": 0}}}})

    def test_ordered_payload_requires_an_exact_permutation(self):
        self.assertEqual(ordered_vectors([(1, [0, 1]), (0, [1, 0])], 2), [[1, 0], [0, 1]])
        for rows in ([(1, [0]), (1, [1])], [(0, [0])], [(0, [0]), (2, [1])]):
            with self.assertRaises(EmbeddingProtocolError):
                ordered_vectors(rows, 2)


class TestHostEmbedding(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        from test_v025 import AdapterBoundaryTests
        AdapterBoundaryTests.setUpClass()
        cls.host_class = AdapterBoundaryTests.module.AstrHost

    def provider(self, module, cls):
        adapter = type(cls, (), {"__module__": f"astrbot.core.provider.sources.{module}"})()
        adapter.get_embeddings = AsyncMock()
        adapter.get_model = lambda: ""
        adapter.provider_config = {}
        host = self.host_class(SimpleNamespace(get_provider_by_id=lambda _: adapter))
        return host, adapter

    async def test_openai_keeps_real_model_verified_order_and_actual_usage(self):
        host, adapter = self.provider("openai_embedding_source", "OpenAIEmbeddingProvider")
        adapter.model = "text-embedding-3-small"
        adapter._embedding_kwargs = lambda: {"dimensions": 2}
        create = AsyncMock(return_value=SimpleNamespace(
            data=[SimpleNamespace(index=1, embedding=[0, 1]), SimpleNamespace(index=0, embedding=[1, 0])],
            usage=SimpleNamespace(prompt_tokens=8),
        ))
        adapter.client = SimpleNamespace(base_url="https://example.test/v1", with_options=Mock(
            return_value=SimpleNamespace(embeddings=SimpleNamespace(create=create))
        ))
        self.assertEqual(await host.embed("known", ["a", "b"]), EmbeddingResult([[1, 0], [0, 1]], 8))
        adapter.client.with_options.assert_called_once_with(max_retries=0)
        create.assert_awaited_once_with(input=["a", "b"], model=adapter.model, dimensions=2)
        self.assertEqual(host.embedding_metadata("known")["model"], adapter.model)
        adapter.get_embeddings.assert_not_called()

    async def test_dashscope_keeps_raw_status_and_validates_native_text_indices(self):
        host, adapter = self.provider("dashscope_embedding_source", "DashScopeEmbeddingProvider")
        adapter.model = "text-embedding-v4"
        adapter.base_url = "https://example.test/api/v1"
        adapter.request_headers = {}
        adapter.api_key = "fixture-placeholder"
        adapter.get_dim = lambda: 128
        sdk = Mock()
        sdk.call.return_value = SimpleNamespace(
            status_code=200, usage={"total_tokens": 4}, output={"embeddings": [
                {"text_index": 1, "embedding": [0, 1]}, {"text_index": 0, "embedding": [1, 0]},
            ]},
        )
        with patch.dict(sys.modules, {"dashscope": SimpleNamespace(TextEmbedding=sdk, MultiModalEmbedding=Mock())}):
            self.assertEqual(await host.embed("known", ["a", "b"]), EmbeddingResult([[1, 0], [0, 1]], 4))
            self.assertEqual(sdk.call.call_args.kwargs["input"], ["a", "b"])
            sdk.call.return_value = SimpleNamespace(status_code=429, code="Throttling.AllocationQuota")
            with self.assertRaises(EmbeddingProviderError) as failed:
                await host.embed("known", ["a"])
            self.assertEqual((failed.exception.status_code, failed.exception.code), (429, "Throttling.AllocationQuota"))


class TestEmbeddingIndex(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        folder = TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)
        self.settings = defaults()
        self.settings["access"].update(mode="active", allowed_groups=["g1", "g2"])
        self.settings["models"]["embedding_provider"] = "fixture"
        self.config = SimpleNamespace(snapshot=lambda: self.settings)
        self.log = DailyLog(self.folder / "logs", self.config, logging.getLogger("embedding-test"))
        self.calls = []
        self.model = "first-space"
        self.size = 2
        self.on_call = None
        self.host = SimpleNamespace(embedding_metadata=self.describe, embed=self.fake_embed)
        self.restart()

    def restart(self):
        self.store = Store(self.folder / "store.db")
        self.gateway = ModelGateway(self.host, self.store, self.config, self.log)
        self.memory = MemoryService(self.store, self.gateway, self.host, self.config, self.log)

    def describe(self, provider):
        return {"identity": f"{provider}:{self.model}", "model": self.model}

    async def fake_embed(self, provider, texts):
        self.calls.append((provider, list(texts)))
        if self.on_call:
            await self.on_call(texts)
        vectors = []
        for text in texts:
            first = text.split("\n", 1)[0]
            number = int(first.rsplit(" ", 1)[-1]) if first.startswith("Fixture record ") else 1
            vectors.append([float(number), *([1.0] * (self.size - 1))])
        return EmbeddingResult(vectors, 7 * len(texts))

    def populate(self, start, count, *, group="g1", full_text="每条原始记忆须完整保存", gist="简短概要"):
        meta = metadata(group)
        evidence = add_message(self.store, meta, "alice", "用于回归测试的合成证据", f"source-{group}-{start}")
        ids = []
        for i in range(start, start + count):
            ids.append(self.store.manual_memory({
                "scope": meta["scope"], "subject": evidence["ref"], "kind": "core",
                "topic_key": f"fixture-{i}", "title": f"Fixture record {i}",
                "full_text": full_text, "gist": gist, "trace": "测试摘要", "importance": 80,
                "confidence": .9, "evidence": [], "participant_refs": [evidence["ref"]],
            }))
        return meta, ids

    def vectors(self):
        with self.store.connection() as db:
            rows = db.execute("SELECT fingerprint,vector FROM vectors ORDER BY memory_id").fetchall()
        return [(r["fingerprint"], json.loads(r["vector"])) for r in rows]

    async def test_second_subbatch_failure_restart_age_and_healthy_scope(self):
        self.populate(1, 12)

        async def fail_last_two(texts):
            if len(texts) == 2:
                raise ModelFailure("timeout")

        self.on_call = fail_last_two
        with patch("astrbot_plugin_fiyu_core.core.memory.now", return_value=100):
            self.assertTrue(await self.memory.embed_once(self.settings))
            self.assertEqual([len(texts) for _, texts in self.calls], [10, 2])
            self.assertEqual([v[0] for _, v in self.vectors()], list(range(1, 11)))
            usage = self.store.usage_rows()[0]
            self.assertEqual((usage["calls"], usage["failures"], usage["estimated"]), (2, 1, 1))
            self.assertGreaterEqual(usage["input_other"], 70)
            self.restart()
            self.assertFalse(await self.memory.embed_once(self.settings))
            status = await self.memory.embedding_status(self.settings)
            self.assertEqual((status["indexed"], status["pending"]), (10, 2))
            self.assertEqual(status["scopes"][0]["retry_at"], 220)
            self.populate(101, 4, group="g2")
            self.assertTrue(await self.memory.embed_once(self.settings))
        self.assertEqual([len(texts) for _, texts in self.calls], [10, 2, 4])
        self.on_call = None
        with patch("astrbot_plugin_fiyu_core.core.memory.now", return_value=220):
            self.assertTrue(await self.memory.embed_once(self.settings))
            status = await self.memory.embedding_status(self.settings)
        self.assertEqual([len(texts) for _, texts in self.calls], [10, 2, 4, 2])
        self.assertEqual([v[0] for _, v in self.vectors()], [*range(1, 13), *range(101, 105)])
        self.assertEqual((status["indexed"], status["pending"]), (16, 0))

    async def test_readonly_long_text_selection_and_budget_limit(self):
        text = "原始" * 400
        self.populate(1, 1, full_text=text)
        self.settings["models"]["embedding_limits"] = {"fixture": {"max_text_tokens": 90}}
        self.settings["budget"]["daily_tokens"] = 1
        self.assertFalse(await self.memory.embed_once(self.settings))
        self.assertEqual(self.calls, [])
        self.settings["budget"]["daily_tokens"] = 0
        self.assertTrue(await self.memory.embed_once(self.settings))
        self.assertEqual(self.calls[0][1], ["Fixture record 1\n简短概要"])
        self.assertEqual(self.store.list_memories()[0]["full_text"], text)
        status = await self.memory.embedding_status(self.settings)
        self.assertEqual(status["scopes"][0]["summary"], 1)
        self.assertEqual(await self.gateway.embed("unused", []), [])

    async def test_invalid_count_dimension_and_nonfinite_fail_safely(self):
        self.populate(1, 2)
        for index, vectors in enumerate(([[1.0, 2.0]], [[1.0], [1.0, 2.0]], [[1.0], [float("nan")]])):
            async def broken(provider, texts):
                return vectors
            self.host.embed = broken
            with patch("astrbot_plugin_fiyu_core.core.memory.now", return_value=100 + index * 15000):
                self.assertFalse(await self.memory.embed_once(self.settings))
                status = await self.memory.embedding_status(self.settings)
            self.assertEqual(status["scopes"][0]["error_kind"], "invalid_response")
            self.assertEqual(self.vectors(), [])
        self.assertEqual(len(self.store.list_memories()), 2)

    async def test_cancellation_does_not_rollback_committed_subbatch(self):
        self.populate(1, 12)
        second_started = asyncio.Event()

        async def pause_last_two(texts):
            if len(texts) == 2:
                second_started.set()
                await asyncio.Event().wait()

        self.on_call = pause_last_two
        task = asyncio.create_task(self.memory.embed_once(self.settings))
        await asyncio.wait_for(second_started.wait(), 2)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(len(self.vectors()), 10)
        self.assertEqual(self.gateway.running, 0)
        self.assertEqual(self.gateway.reserved[day_key()], 0)
        self.restart()
        self.on_call = None
        self.assertTrue(await self.memory.embed_once(self.settings))
        self.assertEqual([len(texts) for _, texts in self.calls], [10, 2, 2])

    async def test_changed_provider_model_and_dimension_never_reuse_old_space(self):
        meta, _ = self.populate(1, 2)
        self.assertTrue(await self.memory.embed_once(self.settings))
        old = self.vectors()[0][0]
        self.model = "different-model"
        self.settings["models"]["embedding_provider"] = "replacement"
        self.assertEqual((await self.memory.embedding_status(self.settings))["indexed"], 0)
        await self.memory.recall(meta, self.settings, "alice", query="完全无关的问题")
        self.assertEqual(len(self.calls), 1)
        self.assertTrue(await self.memory.embed_once(self.settings))
        second = self.vectors()[0][0]
        self.assertNotEqual(old, second)
        self.size = 3
        result = await self.memory.recall(meta, self.settings, "alice", query="另一个无关的问题")
        self.assertEqual(result["memories"], [])
        self.assertEqual((await self.memory.embedding_status(self.settings))["indexed"], 0)
        await self.memory.embed_once(self.settings)  # exhaust the old scan cursor if present
        if (await self.memory.embedding_status(self.settings))["indexed"] == 0:
            self.assertTrue(await self.memory.embed_once(self.settings))
        self.assertTrue(all(fp != second and len(v) == 3 for fp, v in self.vectors()))
