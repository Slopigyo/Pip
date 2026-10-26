"""Synthetic, loopback-only server for smoke_browser.mjs; never constructs Ollama.

The real app, HTTP streaming, session boundary, and Windows DPAPI SQLite store
are used. Authenticated test-only gates let Chrome observe each delayed chunk
before completion without racing a fixed sleep on a slow machine.
"""

import argparse
import secrets
import socket
import sys
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path
from threading import Event, Lock
from typing import Literal

import uvicorn
from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field

from jobwolf.app import create_app
from jobwolf.models import ContextBundle, Evidence, ModelReply, StatementDraft
from jobwolf.providers import ProviderError
from jobwolf.storage import SQLiteStore

MODEL = "Lyra:latest"
FAILURE_MESSAGE = "Synthetic browser test: [fail-once] keep my saved message for retry."
PRIVATE_ERROR = "SMOKE_PRIVATE_PROVIDER_ERROR_do_not_display"
UNTRUSTED = '<script>window.__injected = true</script><img src=x onerror="window.__injected = true">'
CHUNKS = (
    "Synthetic first chunk: let's explore bicycle repair, cafés, and greener paths 🌿. ",
    "Synthetic second chunk, still unfinished. " + UNTRUSTED + " " + "long-unbroken-text-" * 24,
    "\nSynthetic final chunk: what part feels most satisfying?",
)
FAILED_CHUNK = "Synthetic interrupted reply: this partial text must never enter saved history."


class AdvanceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    call: int = Field(ge=1)
    stage: Literal[1, 2]


class SyntheticProvider:
    def __init__(self, port: int) -> None:
        self.base_url = f"http://127.0.0.1:{port}/synthetic-provider-no-network"
        self._lock = Lock()
        self._calls: list[str] = []
        self._gates: list[tuple[Event, Event]] = []
        self._generate_calls = 0
        self._model_lists = 0
        self._failed_once = False

    def list_models(self) -> list[str]:
        with self._lock:
            self._model_lists += 1
        return [MODEL]

    def generate(self, model: str, context: ContextBundle) -> ModelReply:
        with self._lock:
            self._generate_calls += 1
            call = self._generate_calls
        if call == 1:
            raise ProviderError("Synthetic proposal provider failure. Nothing was saved; try again.")
        user = next(message for message in context.messages if message.role == "user")
        return ModelReply(
            assistant_text="Synthetic selected-context notes are ready for review.",
            proposed_statements=[StatementDraft(
                text="Synthetic interest in practical repairs", domain="work", source="explicit",
                evidence=[Evidence(message_id=user.id, quote=user.text[:50])],
            )],
        )

    def snapshot(self) -> dict:
        with self._lock:
            return {"message_ids": list(self._calls), "generate_calls": self._generate_calls, "model_lists": self._model_lists}

    def advance(self, request: AdvanceRequest) -> None:
        with self._lock:
            if request.call > len(self._gates):
                raise HTTPException(409, "That synthetic stream has not started.")
            self._gates[request.call - 1][request.stage - 1].set()

    @staticmethod
    def _wait(gate: Event) -> None:
        if not gate.wait(timeout=30):
            raise ProviderError("Browser smoke did not acknowledge a streamed chunk in time.")
        time.sleep(0.15)

    def stream_chat(self, model: str, context: ContextBundle) -> Iterator[str]:
        if model != MODEL or context.task != "conversation" or context.messages[-1].role != "user":
            raise ProviderError("Unexpected synthetic chat request.")
        latest = context.messages[-1]
        gates = (Event(), Event())
        with self._lock:
            self._calls.append(latest.id)
            self._gates.append(gates)
            fail = latest.text == FAILURE_MESSAGE and not self._failed_once
            if fail:
                self._failed_once = True
        time.sleep(0.15)
        yield FAILED_CHUNK if fail else CHUNKS[0]
        self._wait(gates[0])
        if fail:
            # create_app must sanitize this diagnostic, not echo it to the UI.
            raise ProviderError(PRIVATE_ERROR)
        yield CHUNKS[1]
        self._wait(gates[1])
        yield CHUNKS[2]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--temporary-dir", type=Path, required=True)
    args = parser.parse_args()
    if sys.platform != "win32":
        parser.error("Browser smoke requires Windows DPAPI; no plaintext fallback is allowed.")
    directory = args.temporary_dir.resolve()
    if (
        directory.parent != Path(tempfile.gettempdir()).resolve()
        or not directory.name.startswith("jobwolf-browser-")
        or not directory.is_dir()
    ):
        parser.error("Use only a fresh jobwolf-browser-* directory immediately under the OS temp directory.")
    workspace = directory / "workspace"
    workspace.mkdir(exist_ok=False)  # Refuse to adopt any existing workspace.
    store = SQLiteStore(workspace / "workspace.sqlite3")  # Real user-scoped DPAPI.

    # Like the launcher, reserve the socket before emitting the private link.
    # Port 0 avoids a close/rebind race with another bench or parallel smoke run.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        sock.bind(("127.0.0.1", 0))
        sock.listen(128)
        port = sock.getsockname()[1]
        provider = SyntheticProvider(port)
        token = secrets.token_urlsafe(32)
        app = create_app(store=store, provider=provider, token=token, port=port)
        app.state.bench.load()

        # These routes exist only in this fixture and retain LocalBoundary auth,
        # origin, content-type, and request-size checks under /api/.
        @app.get("/api/smoke")
        def smoke_state():
            return {
                **provider.snapshot(), "model": MODEL, "chunks": CHUNKS,
                "failure_message": FAILURE_MESSAGE, "failed_chunk": FAILED_CHUNK,
                "private_error": PRIVATE_ERROR, "untrusted": UNTRUSTED,
            }

        @app.post("/api/smoke/advance")
        def advance(body: AdvanceRequest):
            provider.advance(body)
            return {"ok": True}

        print(f"http://127.0.0.1:{port}/#token={token}", flush=True)
        config = uvicorn.Config(app, host="127.0.0.1", port=port, access_log=False, log_level="warning")
        uvicorn.Server(config).run(sockets=[sock])


if __name__ == "__main__":
    main()
