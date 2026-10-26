"""HTTP boundary and end-to-end synthetic workflows; no real model calls."""

import json
import sys
import tempfile
import unittest
from collections.abc import Callable
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from jobwolf.app import create_app
from jobwolf.models import (
    BriefDraft,
    ContextBundle,
    Evidence,
    ModelReply,
    OpportunityDraft,
    StatementDraft,
)
from jobwolf.providers import ProviderError
from jobwolf.storage import SQLiteStore, StorageError
from test_service import MemoryStore

TOKEN = "synthetic-session-token-for-tests-only"
PRIVATE = "SYNTHETIC_PRIVATE_MARKER"


class SyntheticProvider:
    base_url = "http://127.0.0.1:11434"

    def __init__(self):
        self.contexts: list[ContextBundle] = []
        self.before_reply: Callable[[], object] | None = None

    def list_models(self):
        return ["Lyra:latest"]

    def stream_chat(self, model: str, context: ContextBundle):
        self.contexts.append(context)
        if self.before_reply:
            self.before_reply()
        yield "What would you like to explore?"

    def generate(self, model: str, context: ContextBundle) -> ModelReply:
        self.contexts.append(context)
        if self.before_reply:
            self.before_reply()
        user = next((m for m in context.messages if m.role == "user"), None)
        return ModelReply(
            assistant_text="Practical problem-solving may be a useful thread. What would you like to change?",
            proposed_statements=[StatementDraft(
                text="Enjoys practical repair", domain="work", source="explicit",
                evidence=[Evidence(message_id=user.id, quote=user.text)],
            )] if user else [],
            opportunities=[OpportunityDraft(title="Explore repair-oriented work", kind="career", rationale="A hypothesis based on the selected context", unknowns=["Day-to-day variety"], next_steps=["Talk with a technician"])],
            brief=BriefDraft(title="An exploratory plan", summary="Look for hands-on problem-solving, preserving uncertainty", directions=["Explore repair work"], search_queries=["equipment repair technician"], next_steps=["Interview someone doing the work"]),
        )


class AppTests(unittest.TestCase):
    def setUp(self):
        for name in ("socket.create_connection", "socket.getaddrinfo"):
            guard = patch(name, side_effect=AssertionError("Real network access is forbidden in app tests"))
            guard.start()
            self.addCleanup(guard.stop)
        self.store = MemoryStore()
        self.provider = SyntheticProvider()
        self.app = create_app(store=self.store, provider=self.provider, token=TOKEN)
        self.client = TestClient(self.app, base_url="http://127.0.0.1:8765", headers={"X-JobWolf-Token": TOKEN})
        self.addCleanup(self.client.close)

    def post(self, path, body, status=200):
        response = self.client.post(path, json=body)
        self.assertEqual(response.status_code, status, response.text)
        return response.json()

    def saved_message(self):
        return self.post("/api/messages", {"revision": 0, "text": "I enjoy repairing bicycles."})

    def test_full_review_feedback_brief_export_import_reset_loop(self):
        state = self.saved_message()
        message_id = state["workspace"]["messages"][0]["id"]
        selection = {"task": "brief", "message_ids": [message_id], "statement_ids": []}
        packet = self.post("/api/context", {"revision": state["revision"], "selection": selection})
        self.assertIn("MODEL_REPLY_JSON_SCHEMA", packet["prompt"])
        self.assertEqual(self.store.revision, state["revision"])
        state = self.post("/api/generate", {"revision": state["revision"], "selection": selection, "model": "Lyra:latest"})
        self.assertEqual(state["workspace"]["statements"], [])
        proposal = state["workspace"]["proposals"][0]
        state = self.post(f"/api/proposals/{proposal['id']}/accept", {
            "revision": state["revision"], "statement_indices": [0], "opportunity_indices": [0], "include_brief": True,
        })
        self.assertTrue(state["workspace"]["statements"][0]["confirmed"])
        self.assertEqual(state["workspace"]["opportunities"][0]["status"], "hypothesis")
        opportunity_id = state["workspace"]["opportunities"][0]["id"]
        state = self.post(f"/api/opportunities/{opportunity_id}/feedback", {"revision": state["revision"], "feedback": "worth_exploring", "note": "I want to learn more"})
        self.assertEqual(state["workspace"]["map_version"], 1)
        brief = self.client.get("/api/brief.md")
        self.assertEqual(brief.status_code, 200)
        self.assertIn("An exploratory plan", brief.text)
        self.assertIn("Current Person Map", brief.text)
        exported = self.client.get("/api/export")
        self.assertEqual(exported.json(), state["workspace"])
        self.assertNotIn(TOKEN, exported.text)
        blank = self.post("/api/reset", {"revision": state["revision"], "confirmation": "DELETE"})
        self.assertEqual(blank["workspace"]["messages"], [])
        restored = self.post("/api/import", {"revision": blank["revision"], "workspace": exported.json(), "confirmation": "REPLACE"})
        self.assertEqual(restored["workspace"], exported.json())

    def test_generate_after_five_messages_recovers_invalid_optional_links_without_retry(self):
        state = self.saved_message()
        for index in range(4):
            state = self.post("/api/messages", {"revision": state["revision"], "text": f"Synthetic follow-up {index}"})
        message_id = state["workspace"]["messages"][0]["id"]
        generate = self.provider.generate

        def with_bad_link(model: str, context: ContextBundle) -> ModelReply:
            reply = generate(model, context)
            reply.opportunities[0].statement_ids = [message_id]
            return reply

        self.provider.generate = with_bad_link
        selection = {"message_ids": [message_id], "statement_ids": []}
        state = self.post("/api/generate", {"revision": state["revision"], "selection": selection, "model": "Lyra:latest"})
        self.assertEqual(len(self.provider.contexts), 1)
        self.assertEqual(len(self.provider.contexts[0].messages), 1)
        self.assertEqual(len(state["workspace"]["messages"]), 6)
        self.assertEqual(state["workspace"]["statements"], [])
        proposal = state["workspace"]["proposals"][0]
        self.assertEqual(proposal["unverified_opportunity_indices"], [0])
        self.assertEqual(proposal["reply"]["opportunities"][0]["statement_ids"], [])
        state = self.post(f"/api/proposals/{proposal['id']}/accept", {
            "revision": state["revision"], "opportunity_indices": [0], "include_brief": True,
        })
        self.assertTrue(state["workspace"]["opportunities"][0]["unverified_links"])
        self.assertTrue(self.client.get("/api/export").json()["opportunities"][0]["unverified_links"])
        self.assertIn("unverified Person Map links were omitted", self.client.get("/api/brief.md").text)

    def test_context_size_is_exact_private_read_only_and_provider_free(self):
        selected_text = PRIVATE + ' caf\u00e9 \U0001f43a\n"quoted"\\'
        state = self.post("/api/messages", {"revision": 0, "text": selected_text})
        message_id = state["workspace"]["messages"][0]["id"]
        unselected_text = "SYNTHETIC_UNSELECTED_CONTEXT"
        state = self.post("/api/messages", {"revision": state["revision"], "text": unselected_text * 300})
        state = self.post("/api/statements", {"revision": state["revision"], "statement": {
            "text": "Synthetic selected preference", "domain": "work", "source": "explicit",
            "evidence": [{"message_id": state["workspace"]["messages"][1]["id"], "quote": unselected_text}],
        }})
        selection = {"message_ids": [message_id], "statement_ids": [state["workspace"]["statements"][0]["id"]]}
        body = {"revision": state["revision"], "selection": selection}
        before = self.store.load()
        with (
            patch.object(self.provider, "list_models", side_effect=AssertionError("Unexpected model discovery")) as models,
            patch.object(self.provider, "generate", side_effect=AssertionError("Unexpected inference")) as generate,
            patch.object(self.provider, "stream_chat", side_effect=AssertionError("Unexpected chat")) as chat,
            patch("jobwolf.providers.build_opener", side_effect=AssertionError("Unexpected client")) as client,
            patch.object(self.store, "save", side_effect=AssertionError("Unexpected mutation")) as save,
        ):
            size = self.post("/api/context/size", body)
            packet = self.post("/api/context", body)
            for operation in (models, generate, chat, client, save):
                operation.assert_not_called()
        self.assertEqual(size, {
            "revision": state["revision"], "selection": {"task": "conversation", **selection, "document_ranges": []},
            "input_bytes": len(packet["prompt"].encode("utf-8")), "input_budget": 12288, "fits": True,
        })
        self.assertNotIn(selected_text, json.dumps(size, ensure_ascii=False))
        self.assertNotIn(PRIVATE, json.dumps(size))
        self.assertNotIn("Synthetic selected preference", json.dumps(size))
        self.assertNotIn(unselected_text, packet["prompt"])
        self.assertEqual(self.store.load(), before)

    def test_eighteen_message_oversize_is_safe_before_provider_calls_and_can_retry(self):
        text = PRIVATE.ljust(300, "x")
        state = self.client.get("/api/state").json()
        for _ in range(18):
            state = self.post("/api/messages", {"revision": state["revision"], "text": text})
        selection = {"task": "conversation", "message_ids": [message["id"] for message in state["workspace"]["messages"]], "statement_ids": []}
        body = {"revision": state["revision"], "selection": selection}
        before = self.store.load()
        with (
            patch.object(self.provider, "list_models", side_effect=AssertionError("Unexpected model discovery")) as models,
            patch.object(self.provider, "generate", side_effect=AssertionError("Unexpected inference")) as generate,
            patch.object(self.store, "save", side_effect=AssertionError("Unexpected mutation")) as save,
        ):
            size = self.post("/api/context/size", body)
            self.assertEqual(set(size), {"revision", "selection", "input_bytes", "input_budget", "fits"})
            self.assertEqual(size["revision"], state["revision"])
            self.assertEqual(size["selection"], {**selection, "document_ranges": []})
            self.assertEqual(size["input_budget"], 12288)
            self.assertGreater(size["input_bytes"], size["input_budget"])
            self.assertIs(size["fits"], False)
            self.assertNotIn(PRIVATE, json.dumps(size))
            # The manual handoff retains its deliberately larger budget.
            packet = self.post("/api/context", body)
            self.assertEqual(size["input_bytes"], len(packet["prompt"].encode("utf-8")))
            error = self.post("/api/generate", {**body, "model": "Lyra:latest"}, status=400)
            self.assertIs(error["state_changed"], False)
            self.assertIn("12288-byte input budget", error["detail"])
            self.assertIn(str(size["input_bytes"]), error["detail"])
            self.assertNotIn(PRIVATE, json.dumps(error))
            for operation in (models, generate, save):
                operation.assert_not_called()
        self.assertEqual(self.store.load(), before)
        self.assertEqual(self.client.get("/api/state").json(), state)
        self.assertFalse(self.provider.contexts)

        # A smaller explicit selection at the unchanged revision can use the lease.
        small_selection = {**selection, "message_ids": selection["message_ids"][:1]}
        body["selection"] = small_selection
        self.assertIs(self.post("/api/context/size", body)["fits"], True)
        result = self.post("/api/generate", {**body, "model": "Lyra:latest"})
        self.assertEqual(result["revision"], state["revision"] + 1)
        self.assertEqual(len(result["workspace"]["proposals"]), 1)
        self.assertEqual(result["workspace"]["statements"], [])
        self.assertEqual(len(self.provider.contexts), 1)
        self.assertEqual([message.id for message in self.provider.contexts[0].messages], small_selection["message_ids"])

    def test_context_size_enforces_auth_revision_and_selection_checks(self):
        state = self.saved_message()
        selection = {"message_ids": [state["workspace"]["messages"][0]["id"]]}
        body = {"revision": state["revision"], "selection": selection}
        before = self.store.load()
        with (
            patch.object(self.provider, "list_models") as models,
            patch.object(self.provider, "generate") as generate,
            patch.object(self.store, "save") as save,
        ):
            for headers, status in (({"X-JobWolf-Token": "wrong"}, 401), ({"Origin": "https://example.invalid"}, 403)):
                response = self.client.post("/api/context/size", json=body, headers=headers)
                self.assertEqual(response.status_code, status)
            cases = (
                ({**body, "revision": 0}, 409),
                ({**body, "revision": "1"}, 422),
                ({"selection": selection}, 422),
                ({**body, "selection": {}}, 400),
                ({**body, "selection": {"message_ids": ["ffffffff-ffff-ffff-ffff-ffffffffffff"]}}, 400),
                ({**body, "selection": {"message_ids": selection["message_ids"] * 2}}, 422),
                ({**body, "selection": {"message_ids": [PRIVATE]}}, 422),
                ({**body, "selection": {**selection, "task": "invalid"}}, 422),
                ({**body, "extra": PRIVATE}, 422),
            )
            for invalid, status in cases:
                for path in ("/api/context/size", "/api/generate"):
                    with self.subTest(path=path, body=invalid):
                        request = {**invalid, "model": "Lyra:latest"} if path == "/api/generate" else invalid
                        error = self.post(path, request, status=status)
                        self.assertNotIn(PRIVATE, json.dumps(error))
            for operation in (models, generate, save):
                operation.assert_not_called()
        self.assertEqual(self.store.load(), before)

    def test_static_assets_have_csp_and_no_embedded_session_secret(self):
        for path in ("/", "/assets/app.js", "/assets/style.css"):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 200)
            self.assertNotIn(TOKEN, response.text)
            self.assertIn("frame-ancestors 'none'", response.headers["content-security-policy"])
            self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(self.client.get("/assets/models.py").status_code, 404)
        self.assertEqual(self.client.get("/openapi.json").status_code, 404)

    def test_api_requires_token_and_rejects_cross_site_origins(self):
        for path in ("/api/state", "/api/export", "/api/models", "/api/brief.md"):
            response = self.client.get(path, headers={"X-JobWolf-Token": "wrong"})
            self.assertEqual(response.status_code, 401)
        for headers in ({"Origin": "https://example.invalid"}, {"Origin": "null"}, {"Sec-Fetch-Site": "cross-site"}):
            self.assertEqual(self.client.get("/api/state", headers=headers).status_code, 403)
        self.assertEqual(self.client.get("/api/state", headers={"Host": "attacker.invalid:8765"}).status_code, 400)
        self.assertEqual(self.client.get("/api/state", headers={"Origin": "http://127.0.0.1:8765"}).status_code, 200)
        self.assertEqual(self.client.post("/api/messages", content="{}", headers={"Content-Type": "text/plain"}).status_code, 415)

    def test_invalid_fields_are_redacted_from_errors(self):
        result = self.post("/api/messages", {"revision": 0, "text": PRIVATE, "extra": PRIVATE}, status=422)
        self.assertNotIn(PRIVATE, json.dumps(result))
        self.assertEqual(self.store.revision, 0)
        self.assertEqual(self.client.post("/api/messages", content=PRIVATE, headers={"Content-Type": "application/json"}).status_code, 422)

    def test_request_limits_cover_declared_and_streamed_lengths(self):
        response = self.client.post("/api/messages", content="{}", headers={"Content-Type": "application/json", "Content-Length": "10000000"})
        self.assertEqual(response.status_code, 413)
        response = self.client.post("/api/messages", content=iter([b"x" * 5_000_000, b"x" * 5_000_000]), headers={"Content-Type": "application/json"})
        self.assertEqual(response.status_code, 413)

    def test_manual_handoff_never_calls_a_model_and_preserves_frozen_revision(self):
        state = self.saved_message()
        selection = {"message_ids": [state["workspace"]["messages"][0]["id"]]}
        packet = self.post("/api/context", {"revision": state["revision"], "selection": selection})
        reply = {"assistant_text": "A synthetic reflection"}
        state = self.post("/api/proposals/manual", {"revision": packet["revision"], "selection": packet["selection"], "reply_text": json.dumps(reply)})
        self.assertEqual(state["workspace"]["messages"][-1]["text"], reply["assistant_text"])
        self.assertFalse(self.provider.contexts)
        self.post("/api/proposals/manual", {"revision": packet["revision"], "selection": packet["selection"], "reply_text": json.dumps(reply)}, status=409)

    def test_bad_manual_output_and_inferred_limits_cannot_mutate_state(self):
        state = self.saved_message()
        selection = {"message_ids": [state["workspace"]["messages"][0]["id"]]}
        for reply, code in (("not JSON", 502), (json.dumps({"assistant_text": "Hi", "proposed_statements": [{"text": "An invented limit", "domain": "money", "source": "inferred", "flexibility": "hard_limit"}]}), 400)):
            error = self.post("/api/proposals/manual", {"revision": state["revision"], "selection": selection, "reply_text": reply}, status=code)
            if code == 502:
                self.assertIs(error["state_changed"], False)
            self.assertEqual(self.store.revision, state["revision"])

    def test_stale_model_result_does_not_overwrite_concurrent_edit(self):
        state = self.saved_message()
        self.provider.before_reply = lambda: self.app.state.bench.add_message(state["revision"], "Newer information")
        self.post("/api/generate", {"revision": state["revision"], "selection": {"message_ids": [state["workspace"]["messages"][0]["id"]]}, "model": "Lyra:latest"}, status=409)
        current = self.client.get("/api/state").json()
        self.assertEqual(len(current["workspace"]["messages"]), 2)
        self.assertFalse(current["workspace"]["proposals"])

    def test_only_installed_models_and_selected_context(self):
        state = self.saved_message()
        message_id = state["workspace"]["messages"][0]["id"]
        state = self.post("/api/messages", {"revision": state["revision"], "text": PRIVATE})
        body = {"revision": state["revision"], "selection": {"message_ids": [message_id]}, "model": "missing:latest"}
        self.post("/api/generate", body, status=400)
        self.assertFalse(self.provider.contexts)
        body["model"] = "Lyra:latest"
        self.post("/api/generate", body)
        self.assertNotIn(PRIVATE, self.provider.contexts[0].model_dump_json())

    def test_provider_failure_leaves_saved_conversation_intact(self):
        state = self.saved_message()
        before = self.store.load()
        self.provider.before_reply = lambda: (_ for _ in ()).throw(ProviderError("The local Ollama request timed out."))
        body = {"revision": state["revision"], "selection": {"message_ids": [state["workspace"]["messages"][0]["id"]]}, "model": "Lyra:latest"}
        error = self.post("/api/generate", body, status=502)
        self.assertIs(error["state_changed"], False)
        self.assertEqual(self.store.load(), before)
        self.provider.before_reply = None
        self.assertEqual(self.post("/api/generate", body)["revision"], state["revision"] + 1)

    def test_known_model_list_failure_is_safe_and_releases_the_generate_lease(self):
        state = self.saved_message()
        before = self.store.load()
        body = {"revision": state["revision"], "selection": {"message_ids": [state["workspace"]["messages"][0]["id"]]}, "model": "Lyra:latest"}
        with patch.object(self.provider, "list_models", side_effect=ProviderError("Could not contact local Ollama.")):
            response = self.client.get("/api/models")
            self.assertEqual(response.status_code, 502)
            self.assertIs(response.json()["state_changed"], False)
            self.assertIs(self.post("/api/generate", body, status=502)["state_changed"], False)
        self.assertEqual(self.store.load(), before)
        self.assertFalse(self.provider.contexts)
        self.assertEqual(self.post("/api/generate", body)["revision"], state["revision"] + 1)

    def test_storage_and_unexpected_failures_after_a_write_are_not_marked_safe(self):
        state = self.saved_message()
        selection = {"message_ids": [state["workspace"]["messages"][0]["id"]]}
        save = self.store.save
        for failure in (StorageError("Could not confirm the local save. Reload before retrying."), RuntimeError(PRIVATE)):
            with self.subTest(failure=type(failure).__name__):
                revision = self.store.revision

                def save_then_fail(payload, expected_revision, error=failure):
                    save(payload, expected_revision)
                    raise error

                with patch.object(self.store, "save", side_effect=save_then_fail):
                    error = self.post("/api/generate", {"revision": revision, "selection": selection, "model": "Lyra:latest"}, status=500)
                self.assertNotIn("state_changed", error)
                self.assertNotIn(PRIVATE, json.dumps(error))
                self.assertEqual(self.store.revision, revision + 1)

    def test_import_and_reset_need_explicit_confirmation(self):
        state = self.saved_message()
        self.post("/api/reset", {"revision": state["revision"]}, status=422)
        self.post("/api/import", {"revision": state["revision"], "workspace": state["workspace"], "confirmation": "DELETE"}, status=422)
        self.assertEqual(self.store.revision, state["revision"])

    def test_unknown_exceptions_are_not_echoed_or_rethrown_with_private_values(self):
        self.provider.list_models = lambda: (_ for _ in ()).throw(RuntimeError(PRIVATE))
        response = self.client.get("/api/models")
        self.assertEqual(response.status_code, 500)
        self.assertNotIn(PRIVATE, response.text)
        self.assertNotIn("state_changed", response.json())


@unittest.skipUnless(sys.platform == "win32", "Actual encrypted HTTP integration requires Windows DPAPI")
class EncryptedAppTests(unittest.TestCase):
    def test_http_roundtrip_uses_dpapi_ciphertext_on_disk(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "workspace.sqlite3"
            store = SQLiteStore(path)
            app = create_app(store=store, provider=SyntheticProvider(), token=TOKEN)
            with TestClient(app, base_url="http://127.0.0.1:8765", headers={"X-JobWolf-Token": TOKEN}) as client:
                response = client.post("/api/messages", json={"revision": 0, "text": PRIVATE})
                self.assertEqual(response.status_code, 200)
                self.assertIn(PRIVATE, client.get("/api/export").text)
            self.assertNotIn(PRIVATE.encode(), path.read_bytes())
            _, plaintext = SQLiteStore(path).load()
            assert plaintext is not None
            self.assertIn(PRIVATE.encode(), plaintext)


if __name__ == "__main__":
    unittest.main()
