"""Offline regressions for provider diagnostics and proactive visual accounting."""

import asyncio
import logging
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import AsyncMock

from astrbot_plugin_fiyu_core.core.config import defaults
from astrbot_plugin_fiyu_core.core.store import Store
from astrbot_plugin_fiyu_core.core.telemetry import (
    BudgetExceeded,
    DailyLog,
    ModelFailure,
    ModelGateway,
)
from astrbot_plugin_fiyu_core.core.util import day_key


class APIError(RuntimeError):
    def __init__(self, status_code=None, code=None):
        super().__init__("raw_failure_body https://provider.example.test/key-text")
        self.status_code = status_code
        self.body = {"error": {"code": code, "message": "private_response_marker"}}


class ModelGatewayTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        folder = TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        path = Path(folder.name)
        self.settings = defaults()
        self.config = SimpleNamespace(snapshot=lambda: self.settings)
        self.store = Store(path / "fiyu.sqlite3")
        self.log = DailyLog(path / "logs", self.config, logging.getLogger(__name__))
        self.host = SimpleNamespace(
            generate=AsyncMock(return_value=SimpleNamespace(completion_text='{"reply":false}')),
            chat_provider=AsyncMock(return_value="configured-provider"),
        )
        self.gateway = ModelGateway(self.host, self.store, self.config, self.log)

    async def call(self, module="decision", **kwargs):
        return await self.gateway.generate(
            module, "fixture-session", "private_prompt_marker", "fixture-system", **kwargs
        )

    async def test_provider_fields_are_classified_and_body_is_discarded(self):
        cases = (
            (403, "DataInspectionFailed", "content_rejected"),
            (429, "insufficient_quota", "quota_exhausted"),
            (429, None, "rate_limit"),
            (403, None, "provider_unavailable"),
            (400, "unsupported_image_media_type", "unsupported_modality"),
            (400, "secret_code_marker", "unknown"),
        )
        for status, code, expected in cases:
            with self.subTest(status=status, code=code):
                self.host.generate.side_effect = APIError(status, code)
                with self.assertRaises(ModelFailure) as error:
                    await self.call()
                event = self.log.recent[-1]
                self.assertEqual(event["code"], "model_call")
                self.assertTrue(event["failed"])
                self.assertEqual(event["error_kind"], expected)
                self.assertEqual(event["http_status"], status)
                self.assertEqual(event.get("provider_code"), code if expected != "unknown" else None)
                self.assertEqual(error.exception.category, expected)
                self.assertTrue(error.exception.__suppress_context__)
                self.assertNotIn("raw_failure_body", str(error.exception))

        rows = await self.store.call("usage_rows")
        self.assertEqual(rows[0]["calls"], len(cases))
        self.assertEqual(rows[0]["failures"], len(cases))
        self.assertEqual(self.host.generate.await_count, len(cases))
        stored_log = "".join(p.read_text(encoding="utf-8") for p in self.log.path.iterdir())
        for marker in ("raw_failure_body", "provider.example", "private_response_marker",
                       "private_prompt_marker", "secret_code_marker"):
            self.assertNotIn(marker, stored_log)

    async def test_unstructured_provider_error_distinct_from_silent_decision(self):
        self.host.generate.return_value = SimpleNamespace(
            role="err", completion_text="DataInspectionFailed in an unstructured response"
        )
        with self.assertRaises(ModelFailure) as error:
            await self.call()
        self.assertEqual(error.exception.category, "unknown")
        self.assertNotIn("provider_code", self.log.recent[-1])

        self.host.generate.return_value = SimpleNamespace(completion_text='{"reply":false}')
        await self.call()
        self.assertFalse(self.log.recent[-1]["failed"])
        self.assertNotIn("error_kind", self.log.recent[-1])

        self.host.generate.return_value = None
        with self.assertRaises(ModelFailure) as error:
            await self.call()
        self.assertEqual(error.exception.category, "invalid_response")

    async def test_timeout_releases_slot_and_budget_does_not_call_provider(self):
        async def slow_provider(*args, **kwargs):
            await asyncio.Event().wait()

        self.host.generate.side_effect = slow_provider
        with self.assertRaises(ModelFailure) as error:
            await self.call(timeout=0.02)
        self.assertEqual(error.exception.category, "timeout")
        self.assertEqual(self.gateway.running, 0)
        self.assertEqual(self.gateway.reserved[day_key()], 0)

        self.settings["budget"]["daily_tokens"] = 1
        with self.assertRaises(BudgetExceeded):
            await self.call()
        self.assertEqual(self.host.generate.await_count, 1)
        self.assertEqual(self.gateway.running, 0)

    async def test_missing_provider_does_not_reserve_or_charge(self):
        self.host.chat_provider.return_value = ""
        with self.assertRaises(ModelFailure) as error:
            await self.call()
        self.assertEqual(error.exception.category, "provider_unavailable")
        self.assertEqual(self.log.recent[-1]["code"], "model_unavailable")
        self.assertEqual(await self.store.call("usage_rows"), [])
        self.host.generate.assert_not_awaited()

    async def test_full_observation_has_persisted_cap_and_shares_vision_limit(self):
        self.settings["models"]["vision_provider"] = "image-provider"
        self.settings["reply"].update(
            image_enabled=True, vision_strategy="full", vision_observe_daily_calls=1
        )
        self.settings["budget"]["module_call_limits"]["vision"] = 2
        with self.assertRaises(BudgetExceeded):
            await self.call("vision_observe", background=True)
        self.settings["access"]["mode"] = "active"
        await self.call("vision_observe", background=True)
        self.assertEqual(self.host.generate.await_args.args[0], "image-provider")
        self.gateway = ModelGateway(self.host, self.store, self.config, self.log)
        with self.assertRaises(BudgetExceeded):
            await self.call("vision_observe", background=True)
        await self.call("vision")
        with self.assertRaises(BudgetExceeded):
            await self.call("vision")
        usage = await self.store.call("usage_today", day_key())
        self.assertEqual(usage["modules"], {"vision": 1, "vision_observe": 1})

    async def test_full_observation_reservation_counts_before_provider_returns(self):
        self.settings["models"].update(vision_provider="image-provider", max_parallel_requests=2)
        self.settings["access"]["mode"] = "active"
        self.settings["reply"].update(
            image_enabled=True, vision_strategy="full", vision_observe_daily_calls=2
        )
        self.settings["budget"]["module_call_limits"]["vision"] = 1
        started, finished = asyncio.Event(), asyncio.Event()

        async def slow_vision(*args, **kwargs):
            started.set()
            await finished.wait()
            return SimpleNamespace(completion_text="fixture image description")

        self.host.generate.side_effect = slow_vision
        task = asyncio.create_task(self.call("vision_observe", background=True))
        try:
            await asyncio.wait_for(started.wait(), timeout=1)
            with self.assertRaises(BudgetExceeded):
                await self.call("vision")
        finally:
            finished.set()
            await task
        self.host.generate.assert_awaited_once()
