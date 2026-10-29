"""A small local HTTP boundary; localhost alone is not authentication."""

import secrets

from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

MAX_REQUEST_BYTES = 9 * 1024 * 1024
SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "Content-Security-Policy": (
        "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; "
        "img-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
    ),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Permissions-Policy": "camera=(), microphone=(self), geolocation=()",
}


class LocalBoundary:
    def __init__(self, app: ASGIApp, token: str, port: int):
        self.app = app
        self.token = token.encode("ascii")
        self.hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
        self.origins = {"http://" + host for host in self.hosts}

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        started = False

        async def secured_send(message: Message) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
                headers = list(message.get("headers", []))
                names = {key.lower().encode("ascii") for key in SECURITY_HEADERS}
                headers = [(k, v) for k, v in headers if k.lower() not in names]
                headers.extend((k.lower().encode("ascii"), v.encode("ascii")) for k, v in SECURITY_HEADERS.items())
                message = {**message, "headers": headers}
            await send(message)

        async def reject(status: int, detail: str) -> None:
            await JSONResponse({"detail": detail}, status_code=status)(scope, receive, secured_send)

        headers = Headers(scope=scope)
        if len(headers.getlist("host")) != 1 or headers.get("host") not in self.hosts:
            await reject(400, "Use the local startup link; this Host is not permitted.")
            return
        if headers.get("origin") is not None and headers["origin"] not in self.origins:
            await reject(403, "Cross-origin access is not permitted.")
            return
        if headers.get("sec-fetch-site") == "cross-site":
            await reject(403, "Cross-site access is not permitted.")
            return
        is_api = scope["path"] == "/api" or scope["path"].startswith("/api/")
        if is_api:
            supplied = headers.get("x-jobwolf-token", "").encode("latin-1")
            if not secrets.compare_digest(supplied, self.token):
                await reject(401, "Open the startup link from this running JobWolf session.")
                return
        mutation = scope["method"] in {"POST", "PUT", "PATCH", "DELETE"}
        if mutation and headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
            await reject(415, "This operation requires application/json.")
            return
        try:
            declared = int(headers.get("content-length", "0"))
        except ValueError:
            declared = -1
        if declared < 0:
            await reject(400, "Invalid request length.")
            return
        if declared > MAX_REQUEST_BYTES:
            await reject(413, "Request exceeds the 9 MiB size limit.")
            return
        # Bound streamed/chunked bodies too, before JSON parsing or validation can
        # echo an input in an exception. Static resources never need a body.
        body = bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            body.extend(message.get("body", b""))
            if len(body) > MAX_REQUEST_BYTES:
                await reject(413, "Request exceeds the 9 MiB size limit.")
                return
            if not message.get("more_body", False):
                break
        consumed = False

        async def buffered_receive() -> Message:
            nonlocal consumed
            if not consumed:
                consumed = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}
            return await receive()

        try:
            await self.app(scope, buffered_receive, secured_send)
        except Exception:  # noqa: BLE001 -- Private inputs must not reach exception logs.
            # Raw Pydantic, parser, or adapter exceptions can contain private
            # values. Unexpected errors fail closed rather than entering logs.
            if not started:
                await reject(500, "Unexpected local error. No private diagnostic content was logged; reload before retrying.")
