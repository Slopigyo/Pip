"""Local HTTP/UI adapter. Domain rules live in service.py, not in routes."""

import json
from collections.abc import AsyncGenerator, Callable, Iterator
from pathlib import Path
from threading import Lock
from typing import Protocol

import anyio
from anyio.lowlevel import checkpoint
from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from starlette.concurrency import run_in_threadpool
from starlette.types import Receive, Scope, Send

from .conversation import CHAT_MAX_REPLY_CHARS, select_chat_context
from .documents import DocumentError, preview_document
from .models import (
    DOMAINS,
    AcceptRequest,
    ChatRequest,
    ContextBundle,
    ContextRequest,
    DocumentDeleteRequest,
    DocumentPreviewRequest,
    DocumentSaveRequest,
    FeedbackRequest,
    GenerateRequest,
    ImportRequest,
    ManualReplyRequest,
    MessageRequest,
    ModelReply,
    ResetRequest,
    RevisionRequest,
    StatementRequest,
    Workspace,
)
from .providers import (
    ProviderError,
    ProviderInputError,
    build_prompt,
    parse_reply,
    structured_input_size,
)
from .security import LocalBoundary
from .service import Bench, MissingRecord, UserError, WorkspaceStore, render_brief
from .storage import ConflictError, StorageError


class Provider(Protocol):
    @property
    def base_url(self) -> str: ...
    def list_models(self) -> list[str]: ...
    def generate(self, model: str, context: ContextBundle) -> ModelReply: ...
    def stream_chat(self, model: str, context: ContextBundle) -> Iterator[str]: ...


STORAGE_DESCRIPTION = (
    "Windows DPAPI-encrypted private payloads in local SQLite. "
    "Database metadata is not encrypted. Exports are plaintext."
)


def _event(value: dict) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n"


def _close_chat(stream: Iterator[str]) -> None:
    close = getattr(stream, "close", None)
    if close is not None:
        close()


class _ChatResponse(StreamingResponse):
    """Own the inference lease independently of whether the body ever starts."""

    def __init__(self, content: AsyncGenerator[str, None], lock: Lock, preflight: Callable[[], None]):
        super().__init__(content, media_type="application/x-ndjson")
        self.content = content
        self.lock = lock
        self.preflight = preflight

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        # Acquiring in a route and releasing only in a generator's finally leaks
        # the lease if the response is disconnected/closed before first iteration.
        if not self.lock.acquire(blocking=False):
            raise HTTPException(409, "A local inference is already running. Wait before trying again.")
        try:
            await run_in_threadpool(self.preflight)
            # Listen even on ASGI 2.4: a disconnect during a network read must
            # cancel before committing, not just be noticed by the next send().
            async with anyio.create_task_group() as tasks:
                async def stream() -> None:
                    await self.stream_response(send)
                    tasks.cancel_scope.cancel()

                tasks.start_soon(stream)
                await self.listen_for_disconnect(receive)
                tasks.cancel_scope.cancel()
        finally:
            try:
                with anyio.CancelScope(shield=True):
                    await self.content.aclose()
            finally:
                self.lock.release()


def create_app(*, store: WorkspaceStore, provider: Provider, token: str, port: int = 8765) -> FastAPI:
    if not token or len(token) < 24 or not token.isascii():
        raise ValueError("A random ASCII local session token of at least 24 characters is required.")
    if not 1 <= port <= 65535:
        raise ValueError("The port must be between 1 and 65535.")
    app = FastAPI(title="JobWolf local bench", docs_url=None, redoc_url=None, openapi_url=None, redirect_slashes=False)
    app.add_middleware(LocalBoundary, token=token, port=port)
    bench = Bench(store)
    inference_lock = Lock()
    parsing_lock = Lock()
    app.state.bench = bench
    web = Path(__file__).parent / "web"

    def state(result: tuple[int, Workspace]) -> dict:
        revision, workspace = result
        return {"revision": revision, "workspace": workspace.model_dump(mode="json"), "domains": DOMAINS, "storage": STORAGE_DESCRIPTION}

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request: Request, error: RequestValidationError):
        return JSONResponse({"detail": "Invalid request: check required fields, allowed values, sizes, and export format version. Nothing was saved."}, status_code=422)

    @app.exception_handler(ConflictError)
    async def conflict(request: Request, error: ConflictError):
        return JSONResponse({"detail": str(error)}, status_code=409)

    @app.exception_handler(StorageError)
    async def storage_error(request: Request, error: StorageError):
        return JSONResponse({"detail": str(error)}, status_code=500)

    @app.exception_handler(UserError)
    async def user_error(request: Request, error: UserError):
        return JSONResponse({"detail": str(error)}, status_code=404 if isinstance(error, MissingRecord) else 400)

    @app.exception_handler(DocumentError)
    async def document_error(request: Request, error: DocumentError):
        return JSONResponse({"detail": str(error), "state_changed": False}, status_code=400)

    @app.exception_handler(ProviderError)
    async def provider_error(request: Request, error: ProviderError):
        # Known provider failures precede persistence; storage/unexpected failures
        # must remain uncertain and are deliberately handled elsewhere.
        return JSONResponse(
            {"detail": str(error), "state_changed": False},
            status_code=400 if isinstance(error, ProviderInputError) else 502,
        )

    @app.get("/")
    def index():
        return FileResponse(web / "index.html", media_type="text/html")

    @app.get("/assets/{name}")
    def asset(name: str):
        types = {"app.js": "text/javascript", "style.css": "text/css"}
        if name not in types:
            raise HTTPException(404, "Resource not found.")
        return FileResponse(web / name, media_type=types[name])

    @app.get("/api/state")
    def get_state():
        return state(bench.load())

    @app.get("/api/models")
    def models():
        return {"models": provider.list_models(), "endpoint": provider.base_url, "preferred_model": "Lyra:latest"}

    @app.post("/api/messages")
    def message(body: MessageRequest):
        return state(bench.add_message(body.revision, body.text, body.document_ranges))

    @app.post("/api/documents/preview")
    def document_preview(body: DocumentPreviewRequest):
        if not parsing_lock.acquire(blocking=False):
            raise DocumentError("A document preview is already being prepared. Wait before trying again.")
        try:
            return preview_document(body.filename, body.content_base64)
        finally:
            parsing_lock.release()

    @app.post("/api/documents")
    def save_document(body: DocumentSaveRequest):
        return state(bench.add_document(body))

    @app.get("/api/documents/{identifier}/impact")
    def document_impact(identifier: str):
        return bench.document_impact(identifier)

    @app.delete("/api/documents/{identifier}")
    def delete_document(identifier: str, body: DocumentDeleteRequest):
        return state(bench.delete_document(body.revision, identifier))

    @app.post("/api/statements")
    def add_statement(body: StatementRequest):
        return state(bench.save_statement(body.revision, body.statement))

    @app.put("/api/statements/{identifier}")
    def update_statement(identifier: str, body: StatementRequest):
        return state(bench.save_statement(body.revision, body.statement, identifier))

    @app.delete("/api/statements/{identifier}")
    def delete_statement(identifier: str, body: RevisionRequest):
        return state(bench.delete_statement(body.revision, identifier))

    @app.post("/api/context")
    def context(body: ContextRequest):
        selected = bench.context(body.revision, body.selection)
        return {"revision": body.revision, "selection": body.selection.model_dump(), "prompt": build_prompt(selected)}

    @app.post("/api/context/size")
    def context_size(body: ContextRequest):
        selected = bench.context(body.revision, body.selection)
        size = structured_input_size(selected)
        return {
            "revision": body.revision, "selection": body.selection.model_dump(),
            "input_bytes": size.input_bytes, "input_budget": size.input_budget, "fits": size.fits,
        }

    @app.post("/api/generate")
    def generate(body: GenerateRequest):
        if not inference_lock.acquire(blocking=False):
            raise HTTPException(409, "A local inference is already running. Wait before trying again.")
        try:
            selected = bench.context(body.revision, body.selection)
            structured_input_size(selected).require_fits()
            if body.model not in provider.list_models():
                raise UserError("Select an installed local model. JobWolf never downloads models automatically.")
            reply = provider.generate(body.model, selected)
            return state(bench.propose(body.revision, body.selection, reply, "Ollama / " + body.model))
        finally:
            inference_lock.release()

    @app.post("/api/chat")
    def chat(body: ChatRequest):
        _, workspace = bench.load(body.revision)
        selection, selected = select_chat_context(workspace, body.message_id)

        def preflight() -> None:
            bench.load(body.revision)
            try:
                installed = provider.list_models()
            except Exception:  # noqa: BLE001 -- Adapter diagnostics may contain private data.
                raise ProviderError("Could not check local models. Check Ollama and retry; your saved message is unchanged.") from None
            if body.model not in installed:
                raise UserError("Select an installed local model. Pip never downloads models automatically.")
            bench.load(body.revision)

        async def events() -> AsyncGenerator[str, None]:
            upstream = None
            try:
                yield _event({
                    "type": "context", "selection": selection.model_dump(mode="json"),
                    "omitted_messages": len(workspace.messages) - len(selected.messages),
                    "omitted_statements": len(workspace.statements) - len(selected.statements),
                })
                upstream = await run_in_threadpool(provider.stream_chat, body.model, selected)
                pieces: list[str] = []
                size = 0
                finished = object()
                while True:
                    # Do not abandon a worker mid-read: wait for its bounded read,
                    # then honor cancellation before yielding or saving anything.
                    chunk = await run_in_threadpool(next, upstream, finished)
                    await checkpoint()
                    if chunk is finished:
                        break
                    if not isinstance(chunk, str):
                        raise UserError("Invalid chat chunk.")
                    size += len(chunk)
                    if size > CHAT_MAX_REPLY_CHARS:
                        raise UserError("Chat reply exceeded its size limit.")
                    chunk.encode("utf-8")
                    if chunk:
                        pieces.append(chunk)
                        yield _event({"type": "token", "text": chunk})
                await run_in_threadpool(_close_chat, upstream)
                upstream = None
                await checkpoint()
                result = await run_in_threadpool(bench.answer_chat, body.revision, body.message_id, "".join(pieces))
                yield _event({"type": "done", "state": state(result)})
            except ConflictError:
                yield _event({"type": "error", "status": 409, "detail": "The workspace changed during this reply. Reload before retrying; no assistant reply was saved."})
            except UserError:
                yield _event({"type": "error", "detail": "The chat reply was empty, too long, or invalid for this workspace. Try a shorter request; no assistant reply was saved."})
            except StorageError:
                yield _event({"type": "error", "detail": "The completed reply could not be saved locally. Check workspace storage and reload before retrying."})
            except Exception:  # noqa: BLE001 -- Private provider errors must not reach logs or clients.
                # Even third-party adapters must not echo private exception text.
                yield _event({"type": "error", "detail": "The local model could not complete this reply. Check Ollama and the selected model, then retry; no assistant reply was saved."})
            finally:
                if upstream is not None:
                    with anyio.CancelScope(shield=True):
                        try:
                            await run_in_threadpool(_close_chat, upstream)
                        except Exception:  # noqa: BLE001, S110 -- Never log private cleanup diagnostics.
                            pass

        return _ChatResponse(events(), inference_lock, preflight)

    @app.post("/api/proposals/manual")
    def manual_reply(body: ManualReplyRequest):
        bench.context(body.revision, body.selection)
        return state(bench.propose(body.revision, body.selection, parse_reply(body.reply_text, task=body.selection.task), "manual handoff (unverified provider)"))

    @app.post("/api/proposals/{identifier}/accept")
    def accept(identifier: str, body: AcceptRequest):
        return state(bench.accept(identifier, body))

    @app.post("/api/proposals/{identifier}/reject")
    def reject(identifier: str, body: RevisionRequest):
        return state(bench.reject(body.revision, identifier))

    @app.post("/api/opportunities/{identifier}/feedback")
    def feedback(identifier: str, body: FeedbackRequest):
        return state(bench.feedback(body.revision, identifier, body.feedback, body.note))

    @app.get("/api/export")
    def export():
        _, workspace = bench.load()
        return Response(workspace.model_dump_json(indent=2), media_type="application/json", headers={
            "Content-Disposition": 'attachment; filename="jobwolf-workspace.json"',
        })

    @app.get("/api/brief.md")
    def brief():
        _, workspace = bench.load()
        return Response(render_brief(workspace), media_type="text/markdown", headers={
            "Content-Disposition": 'attachment; filename="jobwolf-search-brief.md"',
        })

    @app.post("/api/import")
    def import_workspace(body: ImportRequest):
        return state(bench.replace(body.revision, body.workspace))

    @app.post("/api/reset")
    def reset(body: ResetRequest):
        return state(bench.reset(body.revision))

    return app
