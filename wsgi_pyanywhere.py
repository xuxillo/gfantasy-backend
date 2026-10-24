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

# Initialize DB schema (safe: CREATE TABLE IF NOT EXISTS).
init_db()

# Use a2wsgi for robust ASGI-to-WSGI conversion (handles {path:*} routes correctly).
try:
    from a2wsgi import ASGItoWSGI
    _adapter = "a2wsgi"
except ImportError:
    _adapter = "fallback"

import platform as _plat
import fastapi as _fa
import starlette as _starlithe

# Debug: list routes and versions to stderr for error-log inspection
_routes = [r.path for r in app.routes if hasattr(r, "path")]
_sofa_routes = [r for r in _routes if "sofa" in r]
print(f"[WSGI] Python: {_plat.python_version()}", file=sys.stderr)
print(f"[WSGI] FastAPI: {_fa.__version__}", file=sys.stderr)
print(f"[WSGI] Starlette: {_starlithe.__version__}", file=sys.stderr)
print(f"[WSGI] Adapter: {_adapter}", file=sys.stderr)
print(f"[WSGI] Routes: {len(_routes)} total, {len(_sofa_routes)} sofa-routes", file=sys.stderr)
print(f"[WSGI] Sofa routes: {_sofa_routes}", file=sys.stderr)
for _r in _routes:
    print(f"  - {_r}", file=sys.stderr)

if _adapter == "a2wsgi":
    application = ASGItoWSGI(app)
else:
    # Fallback: minimal ASGI-to-WSGI adapter
    import asyncio

    class ASGItoWSGI:
        def __init__(self, asgi_app):
            self.asgi_app = asgi_app

        def __call__(self, environ, start_response):
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                from a2wsgi import ASGItoWSGI as _Impl
                adapter = _Impl(self.asgi_app)
                return adapter(environ, start_response)
            finally:
                loop.close()

    application = ASGItoWSGI(app)
