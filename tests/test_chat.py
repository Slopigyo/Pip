"""Synthetic chat, transport, cancellation, and context tests. No real network/model."""

import asyncio
import io
import json
import threading
import traceback
import unittest
from collections.abc import Awaitable, Callable, Generator, Iterator
from typing import Any, cast
from unittest.mock import patch
from urllib.error import URLError
from urllib.request import ProxyHandler, build_opener

import anyio
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from test_app import TOKEN, SyntheticProvider
from test_providers import _MemoryHTTPHandler
from test_service import MemoryStore

from jobwolf import providers
from jobwolf.app import create_app
from jobwolf.conversation import (
    CHAT_INPUT_BYTES,
    CHAT_SYSTEM_PROMPT,
    chat_input_bytes,
    chat_messages,
    select_chat_context,
)
from jobwolf.models import (
    ChatRequest,
    ContextBundle,
    ContextSelection,
    Evidence,
    Message,
    ModelReply,
    Proposal,
    Statement,
    StatementDraft,
    Workspace,
)
from jobwolf.providers import OllamaProvider, ProviderError
from jobwolf.service import UserError, select_context
from jobwolf.storage import ConflictError

PRIVATE = "SYNTHETIC_OMITTED_PRIVATE_TEXT"
MODEL = "Lyra:latest"


def frame(text="", *, done=False, **extra):
    return json.dumps({"message": {"role": "assistant", "content": text}, "done": done, **extra}, ensure_ascii=False).encode("utf-8") + b"\n"


class StreamResponse(io.BytesIO):
    def __init__(self, data, status=200):
        super().__init__(data)
        self.status = status
        self.read_sizes = []

    def readline(self, size=-1):
        self.read_sizes.append(size)
        return super().readline(size)


class StreamingProvider(SyntheticProvider):
    def __init__(self):
        super().__init__()
        self.chunks = ["Let's explore ", "cafés and practical work."]
        self.failure: Exception | None = None
        self.before_finish: Callable[[], object] | None = None
        self.chat_calls = self.generate_calls = self.model_lists = self.closed = 0

    def list_models(self):
        self.model_lists += 1
        return super().list_models()

    def generate(self, model, context):
        self.generate_calls += 1
        return super().generate(model, context)

    def stream_chat(self, model: str, context: ContextBundle) -> Iterator[str]:
        self.chat_calls += 1
        self.contexts.append(context)
        try:
            yield from self.chunks
            if self.before_finish:
                self.before_finish()
            if self.failure:
                raise self.failure
        finally:
            self.closed += 1


class OfflineTests(unittest.TestCase):
    def setUp(self):
        for name in ("socket.create_connection", "socket.getaddrinfo"):
            guard = patch(name, side_effect=AssertionError("Real network is forbidden in chat tests"))
            guard.start()
            self.addCleanup(guard.stop)


class ContextTests(OfflineTests):
    def test_bounded_consecutive_history_includes_assistants_and_latest(self):
        messages = [Message(role="user" if i % 2 == 0 else "assistant", text=f"Synthetic turn {i}") for i in range(20)]
        messages.append(Message(role="user", text="What might suit me?"))
        messages[0].text = PRIVATE
        workspace = Workspace(messages=messages)
        before = workspace.model_dump()
        selection, context = select_chat_context(workspace, messages[-1].id)
        self.assertEqual(selection.message_ids, [m.id for m in messages[-12:]])
        self.assertTrue(any(m.role == "assistant" for m in context.messages))
        self.assertEqual(context.messages[-1], messages[-1])
        self.assertLessEqual(chat_input_bytes(context), CHAT_INPUT_BYTES)
        self.assertNotIn(PRIVATE, context.model_dump_json())
        self.assertEqual(workspace.model_dump(), before)

    def test_history_stops_at_first_whole_message_that_does_not_fit(self):
        messages = [Message(role="user", text="Older small turn"), Message(role="assistant", text="é" * 12000), Message(role="user", text="Latest intact")]
        selection, context = select_chat_context(Workspace(messages=messages), messages[-1].id)
        self.assertEqual(selection.message_ids, [messages[-1].id])
        self.assertEqual(context.messages[0].text, "Latest intact")

    def test_hard_limits_then_importance_and_no_unreviewed_or_unselected_evidence(self):
        old = Message(role="user", text=PRIVATE)
        messages = [old] + [Message(role="user", text=f"Recent {i}") for i in range(12)]
        low = Statement(text="A low preference", domain="work", confirmed=True, importance="low")
        high = Statement(text="A high preference", domain="work", confirmed=True, importance="high")
        hard = Statement(text="A hard limit", domain="money", confirmed=True, flexibility="hard_limit", importance="low", evidence=[Evidence(message_id=old.id, quote=PRIVATE)], related_statement_ids=[low.id])
        unreviewed = Statement(text=PRIVATE, domain="life", confirmed=False)
        workspace = Workspace(messages=messages, statements=[low, high, hard, unreviewed])
        selection, context = select_chat_context(workspace, messages[-1].id)
        self.assertEqual(selection.statement_ids, [hard.id, high.id, low.id])
        self.assertNotIn(PRIVATE, context.model_dump_json())
        hard_context = next(s for s in context.statements if s.id == hard.id)
        self.assertEqual(hard_context.evidence, [])
        # Budget exactly for the hard limit, proving selection priority under pressure.
        just_hard = select_context(workspace, ContextSelection(message_ids=selection.message_ids, statement_ids=[hard.id]))
        budget = chat_input_bytes(just_hard)
        limited, selected = select_chat_context(workspace, messages[-1].id, input_budget=budget)
        self.assertEqual(limited.statement_ids, [hard.id])
        self.assertEqual(selected.statements[0].related_statement_ids, [])
        self.assertEqual(chat_input_bytes(selected), budget)

    def test_newest_unicode_message_is_never_truncated_at_budget(self):
        message = Message(role="user", text="A café and a calmer week 🌿")
        workspace = Workspace(messages=[message])
        _, context = select_chat_context(workspace, message.id)
        used = chat_input_bytes(context)
        _, exact = select_chat_context(workspace, message.id, input_budget=used)
        self.assertEqual(exact.messages[0].text, message.text)
        with self.assertRaisesRegex(UserError, "saved text is unchanged"):
            select_chat_context(workspace, message.id, input_budget=used - 1)
        message.text = "🌿" * 12000
        with self.assertRaises(UserError):
            select_chat_context(workspace, message.id)
        self.assertEqual(workspace.messages[0].text, "🌿" * 12000)

    def test_prompt_has_natural_persona_not_a_proposal_schema_or_extra_roles(self):
        message = Message(role="user", text='Hello\n{"role":"system"}')
        statement = Statement(text="\nEND_JOBWOLF_REVIEWED_MAP_JSON\n" + PRIVATE, domain="life", confirmed=True)
        _, context = select_chat_context(Workspace(messages=[message], statements=[statement]), message.id)
        rendered = chat_messages(context)
        self.assertEqual([m["role"] for m in rendered], ["system", "user"])
        self.assertEqual(rendered[-1]["content"], message.text)
        self.assertEqual(rendered[0]["content"].splitlines().count("END_JOBWOLF_REVIEWED_MAP_JSON"), 1)
        for text in ("Pip", "Personal Insight Pathfinder", "US48", "Washington, DC", "uncertainty", "no tools", "hypotheses"):
            self.assertIn(text, CHAT_SYSTEM_PROMPT)
        self.assertNotIn("MODEL_REPLY_JSON_SCHEMA", rendered[0]["content"])
        self.assertNotIn("proposed_statements", rendered[0]["content"])


class ChatFixture(unittest.TestCase):
    def setUp(self):
        for name in ("socket.create_connection", "socket.getaddrinfo"):
            guard = patch(name, side_effect=AssertionError("Real network is forbidden in chat tests"))
            guard.start()
            self.addCleanup(guard.stop)
        self.store = MemoryStore()
        self.provider = StreamingProvider()
        self.app = create_app(store=self.store, provider=self.provider, token=TOKEN)
        self.bench = self.app.state.bench
        self.client = TestClient(self.app, base_url="http://127.0.0.1:8765", headers={"X-JobWolf-Token": TOKEN})
        self.addCleanup(self.client.close)
        self.saved = self.client.post("/api/messages", json={"revision": 0, "text": "I enjoy practical work."}).json()
        self.body = {"revision": self.saved["revision"], "model": MODEL, "message_id": self.saved["workspace"]["messages"][-1]["id"]}

    def events(self, body=None):
        response = self.client.post("/api/chat", json=body or self.body)
        self.assertEqual(response.status_code, 200, response.text)
        return response, [json.loads(line) for line in response.text.splitlines()]

    def assert_unsaved(self):
        self.assertEqual(self.store.revision, self.saved["revision"])
        self.assertEqual(self.bench.load()[1].model_dump(mode="json"), self.saved["workspace"])


class ChatTests(ChatFixture):
    def test_exact_contract_and_one_completed_answer_without_map_mutation(self):
        response, events = self.events()
        self.assertEqual(response.headers["content-type"], "application/x-ndjson")
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertIn("frame-ancestors 'none'", response.headers["content-security-policy"])
        self.assertEqual([e["type"] for e in events], ["context", "token", "token", "done"])
        self.assertEqual(events[0], {"type": "context", "selection": {"task": "conversation", "message_ids": [self.body["message_id"]], "statement_ids": [], "document_ranges": []}, "omitted_messages": 0, "omitted_statements": 0})
        self.assertEqual([e["text"] for e in events if e["type"] == "token"], self.provider.chunks)
        self.assertIn("cafés", response.text)
        self.assertNotIn("\\u00e9", response.text)
        state = events[-1]["state"]
        self.assertEqual(state, self.client.get("/api/state").json())
        self.assertEqual(state["revision"], self.saved["revision"] + 1)
        self.assertEqual(len(state["workspace"]["messages"]), 2)
        self.assertEqual(state["workspace"]["messages"][-1]["text"], "".join(self.provider.chunks))
        for field in ("statements", "proposals", "opportunities", "brief", "map_version"):
            self.assertEqual(state["workspace"][field], self.saved["workspace"][field])
        self.assertEqual((self.provider.chat_calls, self.provider.generate_calls, self.provider.model_lists, self.provider.closed), (1, 0, 1, 1))

    def test_context_omissions_are_disclosed_and_backlog_does_not_block_chat(self):
        messages = [Message(role="user" if i % 2 else "assistant", text=f"Turn {i}") for i in range(20)]
        messages[0].text = PRIVATE
        messages.append(Message.model_validate(self.saved["workspace"]["messages"][0]))
        statements = [Statement(text=PRIVATE, domain="life"), Statement(text="Reviewed preference", domain="work", confirmed=True)]
        proposal = ModelReply(assistant_text=PRIVATE, proposed_statements=[StatementDraft(text=PRIVATE, domain="work")])
        workspace = Workspace(messages=messages, statements=statements, proposals=[Proposal(reply=proposal, selection=ContextSelection(message_ids=[messages[0].id]), based_on_map_version=0, origin="synthetic") for _ in range(10)])
        revision, _ = self.bench.replace(self.body["revision"], workspace)
        _, events = self.events(dict(self.body, revision=revision))
        self.assertEqual(events[0]["omitted_messages"], 9)
        self.assertEqual(events[0]["omitted_statements"], 1)
        self.assertNotIn(PRIVATE, self.provider.contexts[0].model_dump_json())
        self.assertEqual(len(events[-1]["state"]["workspace"]["proposals"]), 10)
        self.assertEqual(self.provider.generate_calls, 0)

    def test_malformed_structured_output_cannot_block_plain_chat(self):
        self.provider.chunks = ["This is ordinary prose, not proposal JSON."]
        with patch.object(providers, "parse_reply", side_effect=AssertionError("No structured parser in chat")):
            _, events = self.events()
        self.assertEqual(events[-1]["type"], "done")

    def test_pre_stream_validation_does_not_call_provider_or_lose_saved_text(self):
        for changes, expected in (({"revision": 0}, 409), ({"message_id": "ffffffff-ffff-ffff-ffff-ffffffffffff"}, 409), ({"revision": True}, 422), ({"message_id": PRIVATE}, 422), ({"model": PRIVATE + "?"}, 422), ({"selection": {}}, 422)):
            with self.subTest(changes=changes):
                response = self.client.post("/api/chat", json=dict(self.body, **changes))
                self.assertEqual(response.status_code, expected)
                self.assertNotIn(PRIVATE, response.text)
        self.assertEqual(self.provider.model_lists, 0)
        self.assertEqual(self.provider.chat_calls, 0)
        self.assert_unsaved()

    def test_oversized_latest_message_is_kept_and_no_model_is_called(self):
        saved = self.client.post("/api/messages", json={"revision": self.body["revision"], "text": "🌿" * 12000}).json()
        response = self.client.post("/api/chat", json=dict(self.body, revision=saved["revision"], message_id=saved["workspace"]["messages"][-1]["id"]))
        self.assertEqual(response.status_code, 400)
        self.assertIn("saved text is unchanged", response.text)
        self.assertEqual(self.client.get("/api/state").json(), saved)
        self.assertEqual(self.provider.model_lists, 0)

    def test_duplicate_retry_and_completed_message_id_are_rejected(self):
        _, events = self.events()
        state = events[-1]["state"]
        for body in (self.body, dict(self.body, revision=state["revision"]), dict(self.body, revision=state["revision"], message_id=state["workspace"]["messages"][-1]["id"])):
            self.assertEqual(self.client.post("/api/chat", json=body).status_code, 409)
        self.assertEqual(self.provider.chat_calls, 1)
        self.assertEqual(self.client.get("/api/state").json(), state)
        newer = self.client.post("/api/messages", json={"revision": state["revision"], "text": "A later question"}).json()
        self.assertEqual(self.client.post("/api/chat", json=dict(self.body, revision=newer["revision"])).status_code, 409)

    def test_failed_stream_can_retry_same_message_without_duplicate_user(self):
        self.provider.failure = ProviderError(PRIVATE)
        response, events = self.events()
        self.assertEqual(events[-1]["type"], "error")
        self.assertNotIn(PRIVATE, response.text)
        self.assert_unsaved()
        self.provider.failure = None
        _, events = self.events()
        self.assertEqual(events[-1]["type"], "done")
        self.assertEqual([m["role"] for m in events[-1]["state"]["workspace"]["messages"]], ["user", "assistant"])
        self.assertEqual(self.provider.chat_calls, 2)
        self.assertEqual(self.provider.closed, 2)

    def test_invalid_or_failed_replies_never_persist_partial_text_or_echo_errors(self):
        cases = [([], None), ([" \n"], None), (["partial", "x" * 12000], None), (["partial", {"content": PRIVATE}], None), (["partial", "\ud800"], None), (["partial"], RuntimeError(PRIVATE)), (["partial"], ProviderError(PRIVATE)), (["partial"], UserError(PRIVATE))]
        for chunks, failure in cases:
            with self.subTest(chunks=len(chunks), failure=type(failure).__name__):
                self.provider.chunks, self.provider.failure = chunks, failure
                response, events = self.events()
                self.assertEqual(events[-1]["type"], "error")
                self.assertNotIn("done", [e["type"] for e in events])
                self.assertNotIn(PRIVATE, response.text)
                self.assert_unsaved()
        self.assertEqual(self.provider.chat_calls, len(cases))

    def test_stale_completion_cannot_overwrite_a_new_map_edit(self):
        self.provider.before_finish = lambda: self.bench.save_statement(self.body["revision"], StatementDraft(text="A newer preference", domain="work"))
        _, events = self.events()
        self.assertEqual(events[-1]["type"], "error")
        self.assertEqual(events[-1]["status"], 409)
        _, workspace = self.bench.load()
        self.assertEqual(len(workspace.messages), 1)
        self.assertEqual(workspace.statements[0].text, "A newer preference")
        self.assertEqual(workspace.map_version, 1)

    def test_bench_uses_compare_and_swap_even_if_edit_arrives_during_save(self):
        save = self.store.save
        def racing_save(payload, revision):
            self.store.revision += 1
            return save(payload, revision)
        with patch.object(self.store, "save", side_effect=racing_save), self.assertRaises(ConflictError):
            self.bench.answer_chat(self.body["revision"], self.body["message_id"], "Complete answer")
        self.assertEqual(len(self.bench.load()[1].messages), 1)

    def test_preflight_errors_release_lock_and_do_not_echo_provider_errors(self):
        with patch.object(self.provider, "list_models", side_effect=RuntimeError(PRIVATE)):
            response = self.client.post("/api/chat", json=self.body)
            self.assertEqual(response.status_code, 502)
            self.assertNotIn(PRIVATE, response.text)
        self.assertEqual(self.client.post("/api/chat", json=dict(self.body, model="missing:latest")).status_code, 400)
        self.assertEqual(self.provider.chat_calls, 0)
        _, events = self.events()
        self.assertEqual(events[-1]["type"], "done")

    def test_token_origin_and_content_type_boundary_applies_to_chat(self):
        for headers, status in (({"X-JobWolf-Token": "wrong"}, 401), ({"Origin": "https://example.invalid"}, 403), ({"Host": "example.invalid:8765"}, 400), ({"Content-Type": "text/plain"}, 415)):
            self.assertEqual(self.client.post("/api/chat", json=self.body, headers=headers).status_code, status)
        self.assertEqual(self.provider.chat_calls, 0)

    def test_incomplete_and_tool_streams_never_save_even_after_tokens(self):
        ollama = OllamaProvider()
        response_app = create_app(store=self.store, provider=ollama, token=TOKEN)
        invalid_tool = {"message": {"role": "assistant", "content": "", "tool_calls": [{"function": {"name": PRIVATE}}]}, "done": True}
        endings = [b"", frame(done=True, done_reason="length"), json.dumps(invalid_tool).encode() + b"\n", b'{"error":"' + PRIVATE.encode() + b'"}\n']
        with TestClient(response_app, base_url="http://127.0.0.1:8765", headers={"X-JobWolf-Token": TOKEN}) as client, patch.object(ollama, "list_models", return_value=[MODEL]):
            for ending in endings:
                response = StreamResponse(frame("Partial only") + ending)
                with patch.object(ollama._opener, "open", return_value=response) as network:
                    result = client.post("/api/chat", json=self.body)
                events = [json.loads(line) for line in result.text.splitlines()]
                self.assertEqual([e["type"] for e in events], ["context", "token", "error"])
                self.assertNotIn(PRIVATE, result.text)
                self.assertTrue(response.closed)
                network.assert_called_once()
                self.assert_unsaved()

    def test_unstarted_body_can_be_closed_without_leaking_inference_lock(self):
        endpoint = next(route.endpoint for route in self.app.routes if isinstance(route, APIRoute) and route.path == "/api/chat")
        response = endpoint(ChatRequest(**self.body))
        anyio.run(response.content.aclose)
        self.assertEqual(self.provider.model_lists, 0)
        _, events = self.events()
        self.assertEqual(events[-1]["type"], "done")

    def test_generate_also_holds_the_shared_lock_against_chat(self):
        entered, release = threading.Event(), threading.Event()
        def wait():
            entered.set()
            if not release.wait(5):
                raise RuntimeError("Synthetic wait expired")
        self.provider.before_reply = wait
        replies = []
        def generate():
            replies.append(self.client.post("/api/generate", json={"revision": self.body["revision"], "model": MODEL, "selection": {"message_ids": [self.body["message_id"]]}}))
        thread = threading.Thread(target=generate)
        thread.start()
        try:
            self.assertTrue(entered.wait(3))
            self.assertEqual(self.client.post("/api/chat", json=self.body).status_code, 409)
            self.assertEqual(self.provider.chat_calls, 0)
        finally:
            release.set()
            thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(replies[0].status_code, 200)


class ASGIRequest:
    """Capture actual ASGI body frames: TestClient itself buffers streaming bodies."""
    def __init__(self, app, body, hook: Callable[[dict], Awaitable[None]] | None = None):
        self.app, self.hook = app, hook
        self.frames, self.events = [], []
        self.incoming = asyncio.Queue()
        self.incoming.put_nowait({"type": "http.request", "body": json.dumps(body).encode(), "more_body": False})
        self.scope = {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.4"}, "http_version": "1.1", "method": "POST", "scheme": "http", "path": "/api/chat", "raw_path": b"/api/chat", "query_string": b"", "root_path": "", "headers": [(b"host", b"127.0.0.1:8765"), (b"content-type", b"application/json"), (b"x-jobwolf-token", TOKEN.encode())], "client": ("127.0.0.1", 12345), "server": ("127.0.0.1", 8765)}

    async def send(self, message):
        self.frames.append(message)
        if message["type"] == "http.response.body" and message.get("body"):
            self.events.append(json.loads(message["body"]))
        if self.hook:
            await self.hook(message)

    async def run(self):
        await asyncio.wait_for(self.app(self.scope, self.incoming.get, self.send), timeout=8)


class AsyncChatTests(ChatFixture, unittest.IsolatedAsyncioTestCase):
    async def test_separate_chunks_reach_wire_before_save_and_serialize_generate(self):
        async def hook(message):
            if message["type"] != "http.response.body" or not message.get("body"):
                return
            event = json.loads(message["body"])
            if event["type"] in ("context", "token"):
                self.assert_unsaved()
            if event["type"] == "context":
                for path, body in (("/api/chat", self.body), ("/api/generate", {"revision": self.body["revision"], "model": MODEL, "selection": {"message_ids": [self.body["message_id"]]}})):
                    response = await asyncio.to_thread(self.client.post, path, json=body)
                    self.assertEqual(response.status_code, 409)
                self.assertEqual(self.provider.chat_calls, 0)
                self.assertEqual(self.provider.generate_calls, 0)
        request = ASGIRequest(self.app, self.body, hook)
        await request.run()
        self.assertEqual([e["type"] for e in request.events], ["context", "token", "token", "done"])
        state = request.events[-1]["state"]
        response = await asyncio.to_thread(self.client.post, "/api/generate", json={"revision": state["revision"], "model": MODEL, "selection": {"message_ids": [self.body["message_id"]]}})
        self.assertEqual(response.status_code, 200)

    async def test_send_failure_before_first_iteration_releases_lock_without_inference(self):
        async def broken_send(message):
            if message["type"] == "http.response.start":
                raise OSError("Synthetic disconnect")
        await ASGIRequest(self.app, self.body, broken_send).run()
        self.assert_unsaved()
        self.assertEqual(self.provider.chat_calls, 0)
        _, events = await asyncio.to_thread(self.events)
        self.assertEqual(events[-1]["type"], "done")

    async def test_disconnect_during_worker_read_closes_upstream_and_does_not_commit(self):
        entered, release, closed = threading.Event(), threading.Event(), threading.Event()
        def blocked(model, context):
            try:
                yield "A partial reply"
                entered.set()
                if not release.wait(5):
                    raise RuntimeError("Synthetic wait expired")
            finally:
                closed.set()
        self.provider.stream_chat = blocked
        request = ASGIRequest(self.app, self.body)
        running = asyncio.create_task(request.run())
        try:
            self.assertTrue(await asyncio.to_thread(entered.wait, 3))
            request.incoming.put_nowait({"type": "http.disconnect"})
            await asyncio.sleep(0.05)
            # The worker is not abandoned/replaced with overlapping inference.
            response = await asyncio.to_thread(self.client.post, "/api/chat", json=self.body)
            self.assertEqual(response.status_code, 409)
        finally:
            release.set()
            await running
        self.assertTrue(closed.is_set())
        self.assert_unsaved()
        self.assertEqual([e["type"] for e in request.events], ["context", "token"])
        self.provider.stream_chat = StreamingProvider.stream_chat.__get__(self.provider)
        _, events = await asyncio.to_thread(self.events)
        self.assertEqual(events[-1]["type"], "done")

    async def test_disconnect_after_context_does_not_start_inference(self):
        request: ASGIRequest

        async def hook(message: dict) -> None:
            if message["type"] == "http.response.body" and json.loads(message["body"])["type"] == "context":
                request.incoming.put_nowait({"type": "http.disconnect"})
                await asyncio.sleep(0.01)
        request = ASGIRequest(self.app, self.body, hook)
        await request.run()
        self.assert_unsaved()
        self.assertEqual(self.provider.chat_calls, 0)
        _, events = await asyncio.to_thread(self.events)
        self.assertEqual(events[-1]["type"], "done")


class OllamaChatTests(OfflineTests):
    def setUp(self):
        super().setUp()
        self.provider = OllamaProvider(timeout=20)
        messages = [Message(role="user", text="What could fit?"), Message(role="assistant", text="What matters most?"), Message(role="user", text="A café and practical work.")]
        workspace = Workspace(messages=messages, statements=[Statement(text="A reviewed preference", domain="work", confirmed=True)])
        _, self.context = select_chat_context(workspace, messages[-1].id)

    def test_true_incremental_ndjson_no_schema_thinking_tools_or_extra_request(self):
        response = StreamResponse(frame("A café", thinking=PRIVATE) + frame(" may fit", message={"role": "assistant", "content": " may fit", "thinking": PRIVATE, "reasoning": PRIVATE}) + frame(done=True, done_reason="stop"))
        with patch.object(self.provider._opener, "open", return_value=response) as network:
            stream = self.provider.stream_chat(MODEL, self.context)
            network.assert_not_called()
            self.assertEqual(next(stream), "A café")
            self.assertEqual(len(response.read_sizes), 1)
            self.assertFalse(response.closed)
            self.assertEqual(next(stream), " may fit")
            self.assertEqual(len(response.read_sizes), 2)
            with self.assertRaises(StopIteration):
                next(stream)
        network.assert_called_once()
        request = network.call_args.args[0]
        self.assertEqual(request.full_url, "http://127.0.0.1:11434/api/chat")
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(network.call_args.kwargs, {"timeout": 20})
        payload = json.loads(request.data)
        self.assertEqual(set(payload), {"model", "messages", "stream", "options"})
        self.assertIs(payload["stream"], True)
        self.assertEqual(payload["options"], {"temperature": 0.3, "num_ctx": 16384, "num_predict": 1536})
        self.assertEqual([m["role"] for m in payload["messages"]], ["system", "user", "assistant", "user"])
        self.assertEqual(payload["messages"], chat_messages(self.context))
        self.assertNotIn(PRIVATE, request.data.decode())
        self.assertTrue(response.closed)

    def test_generator_close_closes_upstream_before_completion(self):
        response = StreamResponse(frame("One chunk") + frame(done=True))
        with patch.object(self.provider._opener, "open", return_value=response):
            stream = self.provider.stream_chat(MODEL, self.context)
            next(stream)
            assert isinstance(stream, Generator)
            stream.close()
        self.assertTrue(response.closed)
        self.assertEqual(len(response.read_sizes), 1)

    def test_incomplete_malformed_roles_tools_and_truncation_are_sanitized(self):
        bad_frames = [b"", b"not JSON " + PRIVATE.encode() + b"\n", b"\xff\n", b'{}\n', b'{"done":false,"done":true}\n', frame(done=True, done_reason="length"), frame(done=True, done_reason="max_tokens"), frame(done=cast(Any, 1)), frame(done=True, error=PRIVATE), frame(message={"role": "user", "content": PRIVATE}), frame(message={"content": PRIVATE}), frame(message={"role": "assistant", "content": 1}), frame(message={"role": "assistant", "content": "", "tool_calls": [{"name": PRIVATE}]}), frame(tool_calls=[{"name": PRIVATE}]), frame(message={"role": "assistant", "content": "", "function_call": PRIVATE}), frame(done=True).rstrip(b"\n")]
        for ending in bad_frames:
            with self.subTest(ending=ending[:60]):
                response = StreamResponse(frame("Partial") + ending)
                with patch.object(self.provider._opener, "open", return_value=response) as network:
                    stream = self.provider.stream_chat(MODEL, self.context)
                    self.assertEqual(next(stream), "Partial")
                    try:
                        list(stream)
                    except ProviderError:
                        self.assertNotIn(PRIVATE, traceback.format_exc())
                    else:
                        self.fail("Invalid stream completed")
                network.assert_called_once()
                self.assertTrue(response.closed)

    def test_line_total_and_text_limits_are_bounded_before_acceptance(self):
        for data in (b"x" * (providers._MAX_CHAT_LINE_BYTES + 100), frame("x" * 6000) + frame("y" * 6001, done=True), frame(" \t", done=True)):
            response = StreamResponse(data)
            with patch.object(self.provider._opener, "open", return_value=response), self.assertRaises(ProviderError):
                list(self.provider.stream_chat(MODEL, self.context))
            self.assertTrue(response.closed)
            self.assertTrue(all(size <= providers._MAX_CHAT_LINE_BYTES + 1 for size in response.read_sizes))
        data = frame("ok") + frame(done=True)
        for limit, succeeds in ((len(data), True), (len(data) - 1, False)):
            response = StreamResponse(data)
            with patch.object(providers, "_MAX_RESPONSE_BYTES", limit), patch.object(self.provider._opener, "open", return_value=response):
                if succeeds:
                    self.assertEqual(list(self.provider.stream_chat(MODEL, self.context)), ["ok"])
                else:
                    with self.assertRaises(ProviderError):
                        list(self.provider.stream_chat(MODEL, self.context))
            self.assertTrue(response.closed)
        response = StreamResponse(frame("x" * 12000, done=True))
        with patch.object(self.provider._opener, "open", return_value=response):
            self.assertEqual(len("".join(self.provider.stream_chat(MODEL, self.context))), 12000)

    def test_timeout_connection_errors_and_non_success_close_without_echo_or_retry(self):
        for error in (TimeoutError(PRIVATE), URLError(TimeoutError(PRIVATE)), URLError(PRIVATE), RuntimeError(PRIVATE)):
            with patch.object(self.provider._opener, "open", side_effect=error) as network:
                with self.assertRaises(ProviderError) as caught:
                    list(self.provider.stream_chat(MODEL, self.context))
                self.assertNotIn(PRIVATE, str(caught.exception))
                network.assert_called_once()
        response = StreamResponse(b"")
        with patch.object(self.provider._opener, "open", return_value=response), patch.object(response, "readline", side_effect=TimeoutError(PRIVATE)), self.assertRaisesRegex(ProviderError, "timed out"):
            list(self.provider.stream_chat(MODEL, self.context))
        self.assertTrue(response.closed)
        for status in (201, 301, 500):
            response = StreamResponse(PRIVATE.encode(), status=status)
            with patch.object(self.provider._opener, "open", return_value=response), self.assertRaises(ProviderError):
                list(self.provider.stream_chat(MODEL, self.context))
            self.assertEqual(response.read_sizes, [])
            self.assertTrue(response.closed)

    def test_total_elapsed_timeout_is_checked_between_bounded_reads(self):
        response = StreamResponse(frame("Late", done=True))
        with patch.object(self.provider._opener, "open", return_value=response), patch.object(providers, "monotonic", side_effect=[0, 0, 21]), self.assertRaisesRegex(ProviderError, "timed out"):
            list(self.provider.stream_chat(MODEL, self.context))
        self.assertTrue(response.closed)

    def test_invalid_or_oversized_context_and_model_do_not_make_requests(self):
        oversized = self.context.model_copy(deep=True)
        oversized.messages[-1].text = "🌿" * 12000
        unreviewed = self.context.model_copy(deep=True)
        unreviewed.statements[0].confirmed = False
        with patch.object(self.provider._opener, "open") as network:
            for model, context in (("invalid?model", self.context), (MODEL, oversized), (MODEL, unreviewed), (MODEL, None)):
                with self.assertRaises(ProviderError):
                    list(self.provider.stream_chat(model, cast(Any, context)))
            network.assert_not_called()

    def test_loopback_opener_does_not_follow_stream_post_redirects(self):
        for status in (301, 302, 303, 307, 308):
            handler = _MemoryHTTPHandler(status, "https://example.invalid/" + PRIVATE)
            self.provider._opener = build_opener(ProxyHandler({}), providers._NoRedirect(), handler)
            with self.assertRaisesRegex(ProviderError, "redirects are disabled"):
                list(self.provider.stream_chat(MODEL, self.context))
            self.assertEqual(len(handler.requests), 1)
            self.assertTrue(all(response.closed for response in handler.responses))


if __name__ == "__main__":
    unittest.main()
