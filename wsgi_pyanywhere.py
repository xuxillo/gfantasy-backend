"""WSGI adapter for PythonAnywhere.

Convierte la app FastAPI (ASGI) en un callable WSGI para servir
a través del servidor WSGI de PythonAnywhere (mod_wsgi).
"""
import os
import sys

# Add PythonAnywhere home directory to path
home = os.path.expanduser("~")
if home not in sys.path:
    sys.path.insert(0, home)

# Add backend subdirectory (in case code was imported from Git into a subfolder)
# IMPORTANT: insert gfantasy-backend LAST so it has highest priority (sys.path[0])
backend_subdirs = [
    os.path.join(home, "backend"),           # lower priority
    os.path.join(home, "gfantasy-backend"),  # highest priority (insert last)
]
for d in backend_subdirs:
    if os.path.isdir(d) and d not in sys.path:
        sys.path.insert(0, d)
# Force gfantasy-backend to position 0 (highest priority)
_main_backend = os.path.join(home, "gfantasy-backend")
if os.path.isdir(_main_backend):
    sys.path.remove(_main_backend)
    sys.path.insert(0, _main_backend)

# CORS: allow frontend on Vercel
os.environ.setdefault("CORS_ORIGINS", "https://gfweb-one.vercel.app")
os.environ.setdefault("GURUTZE_DISABLE_AUTO_CLOSE", "1")

from server_fantasy import app
from fantasy_db import init_db

# Initialize DB schema (safe: CREATE TABLE IF NOT EXISTS).
init_db()

# Debug: list routes and versions to stderr for error-log inspection
import platform as _plat
_rts = [r.path for r in app.routes if hasattr(r, "path")]
_sofa_rts = [r for r in _rts if "sofa" in r]

try:
    import fastapi as _fa
    _fa_ver = _fa.__version__
except Exception:
    _fa_ver = "unknown"

try:
    import starlette as _starlithe
    _star_ver = _starlithe.__version__
except Exception:
    _star_ver = "unknown"

print(f"[WSGI] Python: {_plat.python_version()}", file=sys.stderr)
print(f"[WSGI] FastAPI: {_fa_ver}", file=sys.stderr)
print(f"[WSGI] Starlette: {_star_ver}", file=sys.stderr)
print(f"[WSGI] Routes: {len(_rts)} total, {len(_sofa_rts)} sofa-routes", file=sys.stderr)
print(f"[WSGI] Sofa routes: {_sofa_rts}", file=sys.stderr)
for _r in _rts:
    print(f"  - {_r}", file=sys.stderr)


# Use a2wsgi for robust ASGI-to-WSGI conversion (handles {path:*} routes correctly).
_adapter = "none"
try:
    from a2wsgi import ASGItoWSGI as _A2WsgiASGItoWSGI
    _adapter = "a2wsgi"
except ImportError:
    _adapter = "fallback"
    print("[WSGI] a2wsgi not installed, using fallback adapter", file=sys.stderr)

print(f"[WSGI] Adapter: {_adapter}", file=sys.stderr)
print(f"[WSGI] sys.path[0:3]: {sys.path[:3]}", file=sys.stderr)


if _adapter == "a2wsgi":
    application = _A2WsgiASGItoWSGI(app)
else:
    # Minimal fallback: pure-Python ASGI-to-WSGI bridge (no external deps)
    import asyncio

    _STATUS_PHRASES = {
        200: "OK", 201: "Created", 204: "No Content",
        301: "Moved Permanently", 302: "Found", 304: "Not Modified",
        400: "Bad Request", 401: "Unauthorized", 403: "Forbidden",
        404: "Not Found", 405: "Method Not Allowed",
        500: "Internal Server Error", 502: "Bad Gateway", 503: "Service Unavailable",
    }

    class ASGItoWSGI:
        """Minimal ASGI (HTTP) to WSGI adapter."""

        def __init__(self, asgi_app):
            self.asgi_app = asgi_app

        def __call__(self, environ, start_response):
            scope = {
                "type": "http",
                "asgi": {"version": "3.0", "spec_version": "2.3"},
                "http_version": environ.get("SERVER_PROTOCOL", "HTTP/1.1").split("/")[-1],
                "scheme": environ.get("wsgi.url_scheme", "http"),
                "path": environ.get("PATH_INFO", "/"),
                "raw_path": environ.get("PATH_INFO", "/").encode("latin-1"),
                "query_string": environ.get("QUERY_STRING", "").encode("latin-1"),
                "headers": [
                    (k[5:].lower().encode("latin-1"), v.encode("latin-1"))
                    for k, v in environ.items()
                    if k.startswith("HTTP_")
                ],
                "client": (environ.get("REMOTE_ADDR", ""), 0),
                "server": (environ.get("SERVER_NAME", "localhost"), int(environ.get("SERVER_PORT", 80))),
                "method": environ.get("REQUEST_METHOD", "GET"),
                "root_path": environ.get("SCRIPT_NAME", ""),
                "type": "http",
            }

            try:
                body = environ["wsgi.input"].read()
            except Exception:
                body = b""

            async def receive():
                return {"type": "http.request", "body": body, "more_body": False}

            responses = []

            async def send(message):
                responses.append(message)

            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                loop.run_until_complete(self.asgi_app(scope, receive, send))
            finally:
                loop.close()

            status_code = 500
            headers = []
            body_output = b""

            for msg in responses:
                if msg["type"] == "http.response.start":
                    status_code = msg["status"]
                    headers = [
                        (k.decode("latin-1"), v.decode("latin-1"))
                        for k, v in msg.get("headers", [])
                    ]
                elif msg["type"] == "http.response.body":
                    body_output += msg.get("body", b"")

            status_text = _STATUS_PHRASES.get(status_code, "Unknown")
            start_response(f"{status_code} {status_text}", headers)
            return [body_output]

    application = ASGItoWSGI(app)
