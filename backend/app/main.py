import argparse
import asyncio
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import uvicorn
from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.datastructures import Headers

import app.db as db
from app import api, config, importer, ingest, miniapp, rev, security
from app.delivery import DeliveryWorker
from app.tg import TelegramClient
from app.updater import Updater

MAX_BODY_BYTES = 524_288
HTTP_TIMEOUT = httpx.Timeout(connect=5.0, read=10.0, write=10.0, pool=5.0)
# The Telegram long poll holds the connection open for up to poll_timeout
# seconds, so it needs its own client with a longer read timeout.
POLL_TIMEOUT = 30
POLL_HTTP_TIMEOUT = httpx.Timeout(connect=5.0, read=POLL_TIMEOUT + 15.0,
                                  write=10.0, pool=5.0)
UPDATER_DISABLED_ENV = "RELAY_DISABLE_UPDATER"
SHUTDOWN_TIMEOUT_S = 2.0
SHUTDOWN_DRAIN_S = 0.5
_BODY_METHODS = frozenset({"POST", "PUT"})

BACKEND_DIR = Path(__file__).resolve().parent
MINIAPP_STATIC_DIR = BACKEND_DIR / "static" / "app"
MINIAPP_ASSETS_DIR = MINIAPP_STATIC_DIR / "assets"
MINIAPP_INDEX = MINIAPP_STATIC_DIR / "index.html"

# The shell deliberately sets <base href="/relay/app/">, so base-uri must
# allow 'self' ("none" would make the browser ignore the base element).
MINIAPP_CSP = (
    "default-src 'self'; "
    "script-src 'self' https://telegram.org; "
    "style-src 'self'; "
    "img-src 'self' data:; "
    "connect-src 'self'; "
    "frame-ancestors 'self' https://web.telegram.org "
    "https://*.telegram.org; "
    "base-uri 'self'; "
    "form-action 'none'; "
    "object-src 'none'"
)

MINIAPP_SECURITY_HEADERS = (
    ("Content-Security-Policy", MINIAPP_CSP),
    ("Referrer-Policy", "no-referrer"),
    ("X-Content-Type-Options", "nosniff"),
    ("Permissions-Policy", "camera=(), microphone=(), geolocation=()"),
)


class BodySizeLimitMiddleware:
    """Reject request bodies exceeding the configured byte cap.

    Declared Content-Length is rejected before reading the body; otherwise the
    stream is buffered in chunks and aborted at the first chunk over the limit.
    Only applies to methods that carry bodies (POST/PUT).
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] not in _BODY_METHODS:
            await self.app(scope, receive, send)
            return
        limit = getattr(getattr(scope.get("app"), "state", None),
                        "max_body_bytes", MAX_BODY_BYTES)
        headers = Headers(scope=scope)
        declared = headers.get("content-length")
        if declared is not None and declared.isdigit() and int(declared) > limit:
            await self._reject(scope, receive, send)
            return
        if declared is None:
            messages, over = await self._buffer(receive, limit)
            if over:
                await self._reject(scope, receive, send)
                return
            receive = _ReplayReceive(messages)
        await self.app(scope, receive, send)

    async def _buffer(self, receive, limit):
        messages = []
        total = 0
        while True:
            message = await receive()
            messages.append(message)
            if message["type"] != "http.request":
                return messages, False
            total += len(message.get("body", b""))
            if total > limit:
                return messages, True
            if not message.get("more_body", False):
                return messages, False

    async def _reject(self, scope, receive, send):
        response = JSONResponse({"detail": "payload too large"}, status_code=413)
        await response(scope, receive, send)


class _ReplayReceive:
    """Replays buffered request messages to the app, then yields EOF."""

    def __init__(self, messages):
        self._messages = list(messages)
        self._index = 0

    async def __call__(self):
        if self._index < len(self._messages):
            message = self._messages[self._index]
            self._index += 1
            return message
        return {"type": "http.request", "body": b"", "more_body": False}


class MiniAppSecurityMiddleware:
    """Adds browser hardening headers to the /app/ tree only.

    The response headers are injected at the ASGI message level, so they are
    present on 401/403/404 responses and redirects too, not just on 200s.
    X-Frame-Options is deliberately never set: Telegram embeds the Mini App
    in an iframe, and frame-ancestors in the CSP is the only frame control.
    CORS is deliberately not configured either.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")
        if path != "/app" and not path.startswith("/app/"):
            await self.app(scope, receive, send)
            return
        cache_control = ("no-store"
                         if path == "/app" or path.startswith("/app/api/")
                         else "no-cache")
        extra = [(name.encode("latin-1"), value.encode("latin-1"))
                 for name, value in MINIAPP_SECURITY_HEADERS]
        extra.append((b"cache-control", cache_control.encode("latin-1")))

        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", ()))
                present = {name.lower() for name, _ in headers}
                headers.extend(item for item in extra
                               if item[0].lower() not in present)
                message = dict(message, headers=headers)
            await send(message)

        await self.app(scope, receive, send_with_headers)


def _validation_fields(exc: RequestValidationError) -> list:
    fields = []
    for error in exc.errors():
        loc = ".".join(str(part) for part in error.get("loc", ())
                       if part != "body")
        message = str(error.get("msg", "invalid value"))
        fields.append(f"{loc}: {message}" if loc else message)
    return fields


def _bearer_credential(request: Request):
    """Return the bearer credential of a request, or None when absent."""
    header = request.headers.get("authorization")
    if not header:
        return None
    scheme, _, credential = header.partition(" ")
    if scheme.lower() != "bearer":
        return None
    credential = credential.strip()
    return credential or None


def _rejected_bearer(request: Request, settings) -> bool:
    """True when the request does not carry a valid accepted token.

    FastAPI parses the request body before it solves the route dependencies,
    so a malformed body fails validation before the auth dependency runs.
    Comparing the presented token here keeps an unauthenticated caller from
    learning anything about the request schema.
    """
    presented = _bearer_credential(request)
    if presented is None:
        # No credential at all: the route dependency would reject it with
        # 401 anyway, so answer 401 instead of leaking the schema.
        return True
    accepted = (settings.phone_a_token, settings.admin_token)
    return not any(security.token_equal(presented, token) for token in accepted)


def _updater_disabled() -> bool:
    return os.environ.get(UPDATER_DISABLED_ENV, "") == "1"


def create_app(settings, *, run_workers=True) -> FastAPI:
    http = httpx.AsyncClient(timeout=HTTP_TIMEOUT)
    telegram = TelegramClient(settings.telegram_bot_token, http)
    # Separate client for the long poll: its read timeout must exceed the
    # server-side poll timeout, otherwise every poll dies with a timeout.
    poll_http = httpx.AsyncClient(timeout=POLL_HTTP_TIMEOUT)
    poll_telegram = TelegramClient(settings.telegram_bot_token, poll_http)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.db_conn = db.open_db(app.state.settings.database_path)
        db.migrate(app.state.db_conn)
        # Long-poll state must exist before any worker can mutate the DB.
        rev.init_rev(app)
        stop = asyncio.Event()
        app.state.stop_event = stop
        app.state.worker_tasks = []
        app.state.workers_enabled = run_workers
        app.state.poke_event = asyncio.Event()
        if run_workers:
            poke = app.state.poke_event
            worker = DeliveryWorker(app.state.db_conn, app.state.settings,
                                    app.state.tg, poke_event=poke)
            tasks = [asyncio.create_task(worker.run(stop),
                                         name="delivery-worker")]
            app.state.delivery_worker = worker
            if not _updater_disabled():
                updater = Updater(app.state.db_conn, app.state.settings,
                                  app.state.poll_tg, poke_event=poke,
                                  on_change=lambda: rev.bump_rev(app))
                tasks.append(asyncio.create_task(updater.run(stop),
                                                 name="updater"))
                app.state.updater = updater
            app.state.worker_tasks = tasks
        try:
            yield
        finally:
            stop.set()
            worker_tasks = list(app.state.worker_tasks or [])
            if worker_tasks:
                for task in worker_tasks:
                    task.cancel()
                done, pending = await asyncio.wait(worker_tasks,
                                                   timeout=SHUTDOWN_TIMEOUT_S)
                if done:
                    await asyncio.gather(*done, return_exceptions=True)
                if pending:
                    for task in pending:
                        task.cancel()
                    await asyncio.wait(pending, timeout=SHUTDOWN_DRAIN_S)
            await app.state.http.aclose()
            await app.state.poll_http.aclose()
            app.state.db_conn.close()

    app = FastAPI(lifespan=lifespan)
    app.state.settings = settings
    app.state.http = http
    app.state.tg = telegram
    app.state.poll_http = poll_http
    app.state.poll_tg = poll_telegram
    app.state.max_body_bytes = MAX_BODY_BYTES
    app.add_middleware(BodySizeLimitMiddleware)
    app.add_middleware(MiniAppSecurityMiddleware)

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(request: Request,
                                       exc: RequestValidationError):
        # The Mini App authenticates with a header instead of a bearer
        # token, so an invalid body there is a 422, not an auth failure.
        if (request.url.path == "/app"
                or request.url.path.startswith("/app/")):
            return JSONResponse(
                status_code=422,
                content={"detail": {"error": "validation_error",
                                    "fields": _validation_fields(exc)}},
            )
        if _rejected_bearer(request, settings):
            return JSONResponse(status_code=401,
                                content={"detail": "unauthorized"},
                                headers={"WWW-Authenticate": "Bearer"})
        return JSONResponse(
            status_code=422,
            content={"detail": {"error": "validation_error",
                                "fields": _validation_fields(exc)}},
        )

    app.include_router(ingest.router,
                       dependencies=[Depends(security.require_device(settings))])
    app.include_router(
        importer.router,
        dependencies=[Depends(security.require_device(settings))],
    )
    app.include_router(api.build_router(settings))
    app.include_router(miniapp.build_router(settings))

    app.mount("/app/assets", StaticFiles(directory=str(MINIAPP_ASSETS_DIR)),
              name="miniapp-assets")

    @app.get("/app", include_in_schema=False)
    @app.get("/app/", include_in_schema=False)
    async def miniapp_shell():
        return FileResponse(MINIAPP_INDEX, media_type="text/html",
                            headers={"Cache-Control": "no-store"})

    @app.get("/health")
    async def health():
        return {"ok": True}

    return app


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="sms-relay", description="SMS relay backend")
    parser.add_argument("--env-file", default=None,
                        help="path to an env file with the relay settings")
    parser.add_argument("--host", default=None,
                        help="interface to bind (default from settings)")
    parser.add_argument("--port", type=int, default=None,
                        help="port to bind (default from settings)")
    parser.add_argument("--log-level", default=None,
                        help="uvicorn log level (default from settings)")
    args = parser.parse_args(argv)
    settings = config.load_settings(env_file=args.env_file)
    logging.basicConfig(level=settings.log_level.upper())
    # httpx logs the full request URL at INFO, and the Telegram API keeps
    # the bot token in the URL path. Logging it would leak the token into
    # the journal, so the http client stays quiet.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    app = create_app(settings)
    uvicorn.run(app,
                host=args.host if args.host is not None else settings.uvicorn_host,
                port=args.port if args.port is not None else settings.uvicorn_port,
                log_level=(args.log_level or settings.log_level).lower())


if __name__ == "__main__":
    main()
