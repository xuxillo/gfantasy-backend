"""WSGI adapter for PythonAnywhere.

Convierte la app FastAPI (ASGI) en un callable WSGI para servir
a través del servidor WSGI de PythonAnywhere (mod_wsgi).

Usa un event loop síncrono por request (suficiente para HTTP request/response).
Las cookies httpOnly se mantienen para desarrollo local, pero el token también
se devuelve en el JSON para que el frontend lo almacene en localStorage y use
Bearer auth (compatible con CORS cross-domain desde Vercel).
"""
import asyncio
import os
import sys

# Add PythonAnywhere home directory to path
home = os.path.expanduser("~")
if home not in sys.path:
    sys.path.insert(0, home)

# Add backend subdirectory (in case code was imported from Git into a subfolder)
backend_subdirs = [
    os.path.join(home, "gfantasy-backend"),
    os.path.join(home, "backend"),
]
for d in backend_subdirs:
    if os.path.isdir(d) and d not in sys.path:
        sys.path.insert(0, d)

# CORS: allow frontend on Vercel
os.environ.setdefault("CORS_ORIGINS", "https://gfweb-one.vercel.app")
os.environ.setdefault("GURUTZE_DISABLE_AUTO_CLOSE", "1")

from server_fantasy import app
from fantasy_db import init_db

# Explicitly initialize DB schema since WSGI doesn't trigger FastAPI lifespan events.
# init_db uses CREATE TABLE IF NOT EXISTS, so it's safe to call on every startup.
init_db()

class ASGItoWSGI:
    def __init__(self, asgi_app):
        self.asgi_app = asgi_app

    def __call__(self, environ, start_response):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        headers = []
        for key, value in environ.items():
            if key.startswith("HTTP_"):
                headers.append((key[5:].lower().encode(), value.encode()))
        if "CONTENT_TYPE" in environ:
            headers.append((b"content-type", environ["CONTENT_TYPE"].encode()))
        if "CONTENT_LENGTH" in environ:
            headers.append((b"content-length", environ["CONTENT_LENGTH"].encode()))

        scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": environ.get("SERVER_PROTOCOL", "HTTP/1.1").split("/")[1],
            "method": environ.get("REQUEST_METHOD", "GET"),
            "path": environ.get("PATH_INFO", "/"),
            "raw_path": environ.get("PATH_INFO", "/").encode(),
            "query_string": environ.get("QUERY_STRING", "").encode(),
            "headers": headers,
            "scheme": environ.get("wsgi.url_scheme", "http"),
            "client": (environ.get("REMOTE_ADDR", ""), int(environ.get("REMOTE_PORT", 0))),
            "server": (environ.get("SERVER_NAME", ""), int(environ.get("SERVER_PORT", 80))),
        }

        body = b""
        if environ.get("REQUEST_METHOD") in ("POST", "PUT", "PATCH"):
            body = environ.get("wsgi.input").read() if environ.get("wsgi.input") else b""

        status_res = {"code": 200, "headers": []}
        body_res = []

        async def receive():
            return {"type": "http.request", "body": body, "more_body": False}

        async def send(message):
            if message["type"] == "http.response.start":
                status_res["code"] = message.get("status", 200)
                status_res["headers"] = message.get("headers", [])
            elif message["type"] == "http.response.body":
                body_res.append(message.get("body", b""))

        try:
            loop.run_until_complete(self.asgi_app(scope, receive, send))
        finally:
            loop.close()

        status_map = {200: "OK", 302: "Found", 404: "Not Found", 401: "Unauthorized", 500: "Error"}
        wsgi_headers = [(k.decode(), v.decode()) for k, v in status_res["headers"]]
        start_response(f"{status_res['code']} {status_map.get(status_res['code'], 'Unknown')}", wsgi_headers)
        return [b"".join(body_res)]


application = ASGItoWSGI(app)
