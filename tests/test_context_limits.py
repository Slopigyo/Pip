"""Focused context-window tests; synthetic data, no model or real network calls."""

import io
import json
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from pydantic import ValidationError
from test_app import TOKEN, SyntheticProvider
from test_service import MemoryStore

from jobwolf.app import create_app
from jobwolf.context_limits import (
    CONTEXT_CHOICES,
    DEFAULT_CONTEXT_TOKENS,
    local_input_bytes,
)
from jobwolf.models import (
    ChatRequest,
    ContextBundle,
    ContextRequest,
    GenerateRequest,
    Message,
    ModelReply,
)
from jobwolf.providers import (
    OllamaProvider,
    ProviderInputError,
    structured_input_size,
)


class SyntheticResponse(io.BytesIO):
    status = 200


class RecordingProvider(SyntheticProvider):
    def generate(self, model, context):
        self.contexts.append(context)
        return ModelReply(assistant_text="Synthetic response; no map changes.")


class ContextWindowTests(unittest.TestCase):
    def setUp(self):
        for name in ("socket.create_connection", "socket.getaddrinfo"):
            guard = patch(name, side_effect=AssertionError("No real network in context-window tests"))
            guard.start()
            self.addCleanup(guard.stop)

    def test_missing_setting_defaults_without_allowing_unknown_fields(self):
        message = Message(role="user", text="Synthetic message")
        cases = (
            (ContextRequest, {"revision": 0, "selection": {"message_ids": [message.id]}}),
            (GenerateRequest, {"revision": 0, "selection": {"message_ids": [message.id]}, "model": "Lyra:latest"}),
            (ChatRequest, {"revision": 0, "message_id": message.id, "model": "Lyra:latest"}),
            (ContextBundle, {"task": "conversation", "messages": [message], "statements": []}),
        )
        for model, fields in cases:
            with self.subTest(model=model.__name__):
                self.assertEqual(model.model_validate(fields).context_tokens, 128000)
                with self.assertRaises(ValidationError):
                    model.model_validate({**fields, "unexpected_setting": True})

    def test_default_and_lower_windows_reach_both_ollama_request_types(self):
        self.assertEqual(DEFAULT_CONTEXT_TOKENS, 128000)
        self.assertEqual(local_input_bytes(), 123904)
        provider = OllamaProvider()
        for tokens in CONTEXT_CHOICES:
            with self.subTest(tokens=tokens):
                context = ContextBundle(task="conversation", messages=[Message(role="user", text="Synthetic hello")], statements=[], context_tokens=tokens)
                self.assertNotIn("context_tokens", context.model_dump())
                with patch.object(provider, "_request_json", return_value={"message": {"content": ModelReply(assistant_text="Synthetic answer").model_dump_json()}, "done": True}) as request:
                    provider.generate("Lyra:latest", context)
                self.assertEqual(request.call_args.args[1]["options"]["num_ctx"], tokens)
                response = SyntheticResponse((json.dumps({"message": {"role": "assistant", "content": "Synthetic answer"}, "done": True}) + "\n").encode())
                with patch.object(provider._opener, "open", return_value=response) as request:
                    self.assertEqual(list(provider.stream_chat("Lyra:latest", context)), ["Synthetic answer"])
                self.assertEqual(json.loads(request.call_args.args[0].data)["options"]["num_ctx"], tokens)

    def test_default_large_budget_exact_boundary_without_trimming(self):
        context = ContextBundle(task="conversation", messages=[Message(role="user", text="x" * 10000) for _ in range(11)] + [Message(role="user", text="x")], statements=[])
        size = structured_input_size(context)
        context.messages[-1].text += "x" * (size.input_budget - size.input_bytes)
        self.assertLessEqual(len(context.messages[-1].text), 12000)
        self.assertEqual(structured_input_size(context).input_bytes, 123904)
        structured_input_size(context).require_fits()
        context.messages[-1].text += "x"
        before = context.model_dump()
        provider = OllamaProvider()
        with patch.object(provider._opener, "open") as network, self.assertRaises(ProviderInputError):
            provider.generate("Lyra:latest", context)
        network.assert_not_called()
        self.assertEqual(context.model_dump(), before)

    def client(self):
        self.store = MemoryStore()
        self.provider = RecordingProvider()
        app = create_app(store=self.store, provider=self.provider, token=TOKEN)
        client = TestClient(app, base_url="http://127.0.0.1:8765", headers={"X-JobWolf-Token": TOKEN})
        self.addCleanup(client.close)
        return client

    def test_preflight_and_generation_use_the_same_requested_window(self):
        client = self.client()
        saved = client.post("/api/messages", json={"revision": 0, "text": "Synthetic selected text. " * 250}).json()
        request = {"revision": saved["revision"], "selection": {"message_ids": [saved["workspace"]["messages"][0]["id"]]}}
        before = self.store.load()
        small = client.post("/api/context/size", json={**request, "context_tokens": 16384}).json()
        large = client.post("/api/context/size", json=request).json()
        self.assertFalse(small["fits"])
        self.assertTrue(large["fits"])
        self.assertEqual((small["context_tokens"], small["input_budget"]), (16384, 12288))
        self.assertEqual((large["context_tokens"], large["input_budget"]), (128000, 123904))
        self.assertEqual(small["input_bytes"], large["input_bytes"])
        self.assertEqual(self.store.load(), before)
        rejected = client.post("/api/generate", json={**request, "model": "Lyra:latest", "context_tokens": 16384})
        self.assertEqual(rejected.status_code, 400)
        self.assertEqual(self.provider.contexts, [])
        accepted = client.post("/api/generate", json={**request, "model": "Lyra:latest"})
        self.assertEqual(accepted.status_code, 200, accepted.text)
        self.assertEqual(self.provider.contexts[-1].context_tokens, 128000)

    def test_chat_selection_and_provider_keep_the_lower_window_after_adding_history_and_map(self):
        client = self.client()
        saved = client.post("/api/messages", json={"revision": 0, "text": "Earlier synthetic turn"}).json()
        saved = client.post("/api/statements", json={"revision": saved["revision"], "statement": {"text": "Synthetic preference", "domain": "work"}}).json()
        saved = client.post("/api/messages", json={"revision": saved["revision"], "text": "Current synthetic turn"}).json()
        response = client.post("/api/chat", json={"revision": saved["revision"], "message_id": saved["workspace"]["messages"][-1]["id"], "model": "Lyra:latest", "context_tokens": 32768})
        self.assertEqual(response.status_code, 200, response.text)
        events = [json.loads(line) for line in response.text.splitlines()]
        self.assertEqual(events[0]["context_tokens"], 32768)
        self.assertEqual(events[-1]["type"], "done")
        context = self.provider.contexts[-1]
        self.assertEqual(context.context_tokens, 32768)
        self.assertEqual(len(context.messages), 2)
        self.assertEqual(len(context.statements), 1)

    def test_invalid_window_is_rejected_before_any_inference_or_write(self):
        client = self.client()
        saved = client.post("/api/messages", json={"revision": 0, "text": "Synthetic message"}).json()
        before = self.store.load()
        message_id = saved["workspace"]["messages"][0]["id"]
        selected = {"revision": saved["revision"], "selection": {"message_ids": [message_id]}}
        requests = (
            ("/api/context/size", selected),
            ("/api/generate", {**selected, "model": "Lyra:latest"}),
            ("/api/chat", {"revision": saved["revision"], "message_id": message_id, "model": "Lyra:latest"}),
        )
        for path, body in requests:
            for value in (0, 16000, 256000, "128000", True, None, 128000.5):
                with self.subTest(path=path, value=value):
                    response = client.post(path, json={**body, "context_tokens": value})
                    self.assertEqual(response.status_code, 422)
        self.assertEqual(self.store.load(), before)
        self.assertEqual(self.provider.contexts, [])


if __name__ == "__main__":
    unittest.main()
