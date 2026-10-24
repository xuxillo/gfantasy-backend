# server_fantasy.py
from __future__ import annotations

import asyncio
import json
import re
import threading
import time
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Optional

import httpx
from curl_cffi.requests import AsyncSession as CurlAsyncSession
from fastapi import FastAPI, Depends, HTTPException, Header, Query, Body, Response, Request, Cookie
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, FileResponse
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, EmailStr
import jwt

import events
from fantasy_db import init_db, get_conn, hash_password, verify_password, generate_invite_code, now_iso
from fantasy_config import SOFASCORE_API, DEFAULT_FORMATION, FORMATIONS, CONTRACT_WEEKS, IMAGES_ROOT
from fantasy_services import (
    register_user, authenticate_user, create_league, join_league,
    get_squad, get_matchday_squad, update_matchday_squad,
    get_today_market, refresh_market, place_bid, get_my_bids, cancel_bid,
    create_user_offer, respond_to_offer, get_budget_info,
    sync_players_from_sofascore, sync_players_from_sofascore_db, estimate_market_value,
    username_of, run_matchday_scoring, close_matchday, run_nightly_maintenance,
    create_default_matchday_squad, get_user_context,
    get_user_season_stats, get_user_evolution, get_reminders,
    auto_close_all_leagues, auto_close_matchday,
    get_league_status,
    get_real_standings, get_real_fixtures, search_real_players,
    update_user_email, update_user_password,
    create_password_reset_token, apply_password_reset,
    real_current_matchday,
    admin_reset_league, admin_grant_budget, admin_set_market_size,
    admin_list_transfers, admin_cancel_transfer,
)
from password_reset import send_password_reset_email

import os

import sofa_auto_update

# JWT config
# En producción definir JWT_SECRET en el entorno (mínimo 32 caracteres).
SECRET_KEY = os.environ.get(
    "JWT_SECRET", "gurutze-fantasy-dev-secret-no-usar-en-produccion"
)
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60 * 24 * 7  # 7 days

security = HTTPBearer(auto_error=False)

# ---------- Cierre automático de jornadas ----------
# Loop de fondo que cierra cada jornada cuando ya hay resultados de SofaScore.
# Configuración: GURUTZE_AUTO_CLOSE_SECONDS (intervalo, por defecto 120) y
# GURUTZE_DISABLE_AUTO_CLOSE=1 para desactivarlo (tests/demo).
AUTO_CLOSE_INTERVAL_SECONDS = int(os.environ.get("GURUTZE_AUTO_CLOSE_SECONDS", "120"))
_auto_close_stop = threading.Event()


def _auto_close_loop() -> None:
    """Ejecuta el cierre automático de forma periódica mientras vive el servidor."""
    while not _auto_close_stop.is_set():
        try:
            for r in auto_close_all_leagues():
                if r.get("closed"):
                    print(
                        f"[auto-close] Liga {r['league_id']}: jornada {r['matchday']} "
                        f"cerrada -> jornada {r.get('advanced_to')}"
                    )
                    events.publish(
                        r["league_id"],
                        {
                            "type": "matchday_closed",
                            "league_id": r["league_id"],
                            "matchday": r.get("matchday"),
                            "advanced_to": r.get("advanced_to"),
                        },
                    )
        except Exception as e:  # pragma: no cover
            print(f"[auto-close] error en el ciclo: {e}")
        _auto_close_stop.wait(AUTO_CLOSE_INTERVAL_SECONDS)


# ---------- Mantenimiento nocturno y mercado (Canon §5: cambio a las 05:00) ----------
# Una vez por "día de mercado" (el día calendario, considerando que el mercado
# cambia a las 05:00) se resuelve el mercado y se corre el mantenimiento
# nocturno (contratos + supertablón) para todas las ligas.
# Configuración: GURUTZE_NIGHTLY_SECONDS (intervalo de comprobación, def. 300) y
# GURUTZE_NIGHTLY_DISABLE=1 para desactivarlo (tests/demo).
NIGHTLY_CHECK_SECONDS = int(os.environ.get("GURUTZE_NIGHTLY_SECONDS", "300"))
_nightly_stop = threading.Event()
_nightly_lock = threading.Lock()
_nightly_done_day: Optional[str] = None


def _nightly_market_day(now: Optional[datetime] = None) -> str:
    """Clave del 'día de mercado': cambia a las 05:00 (Canon §5)."""
    now = now or datetime.now()
    if now.hour < 5:
        return (now.date() - timedelta(days=1)).isoformat()
    return now.date().isoformat()


def _run_nightly_once(force_day: Optional[str] = None) -> None:
    """Ejecuta mercado + mantenimiento nocturno una vez por día de mercado."""
    global _nightly_done_day
    day = force_day or _nightly_market_day()
    with _nightly_lock:
        if day == _nightly_done_day:
            return
        try:
            with get_conn() as conn:
                leagues = [r["id"] for r in conn.execute("SELECT id FROM leagues").fetchall()]
        except Exception:
            leagues = []
        for lid in leagues:
            try:
                asyncio.run(refresh_market(lid))
                run_nightly_maintenance(lid)
            except Exception as e:  # pragma: no cover
                print(f"[nightly] error en liga {lid}: {e}")
        _nightly_done_day = day
        print(f"[nightly] mercado + mantenimiento aplicados ({day}, {len(leagues)} ligas)")


def _nightly_loop() -> None:
    """Comprueba periódicamente si tocó el cambio de mercado/mantenimiento y
    el refresco semanal de imágenes de SofaScore."""
    while not _nightly_stop.is_set():
        try:
            _run_nightly_once()
            _run_semanal_imagenes_sofa()
        except Exception as e:  # pragma: no cover
            print(f"[nightly] error en el ciclo: {e}")
        _nightly_stop.wait(NIGHTLY_CHECK_SECONDS)


def _run_semanal_imagenes_sofa() -> None:
    """Refresco SEMANAL de imágenes de SofaScore (escudos, fotos de jugadores,
    lesiones) y de incidentes.

    Son datos que cambian despacio: no se piden en cada ciclo de partidos,
    solo una vez cada 7 días. El estado queda en
    `output/{temporada}/ultimo_refresco_imagenes.json` (si cambia la
    temporada se vuelve a refrescar todo, que es justo lo que toca).
    """
    from configuracion import DIRECTORIO_SALIDA, TEMPORADA_CARPETA

    ruta_estado = DIRECTORIO_SALIDA / TEMPORADA_CARPETA / "ultimo_refresco_imagenes.json"
    hoy = date.today()
    if ruta_estado.exists():
        try:
            ultimo = date.fromisoformat(
                json.loads(ruta_estado.read_text(encoding="utf-8")).get("fecha", "")
            )
            if (hoy - ultimo).days < 7:
                return
        except Exception:
            pass  # estado corrupto o sin fecha: se refresca

    print("[sofa-imagenes] refresco semanal de escudos/fotos/lesiones...")
    try:
        from extraer_imagenes_incidentes import main as _imagenes_main

        _imagenes_main()
    except Exception as e:  # pragma: no cover
        print(f"[sofa-imagenes] error en el refresco semanal (se reintentará la semana que viene): {e}")
        return
    try:
        ruta_estado.parent.mkdir(parents=True, exist_ok=True)
        ruta_estado.write_text(json.dumps({"fecha": hoy.isoformat()}), encoding="utf-8")
    except Exception:  # pragma: no cover
        pass
    print(f"[sofa-imagenes] refresco semanal completado ({hoy.isoformat()})")


# ---------- Auto-actualización de SofaScore ----------
# Loop de fondo que refresca la BD de SofaScore (partidos + stats nuevas)
# cada N minutos, para que los datos se descarguen solos cuando hay partido.
# Configuración: GURUTZE_SOFA_UPDATE_MINUTES (def. 15) y
# GURUTZE_DISABLE_SOFA_UPDATE=1 para desactivarlo (tests/demo).
SOFA_UPDATE_INTERVAL_MINUTES = int(os.environ.get("GURUTZE_SOFA_UPDATE_MINUTES", "15"))
_sofa_stop = threading.Event()


def _sofa_update_loop() -> None:
    """Refresca la BD de SofaScore de forma periódica (incremental).

    El intervalo base es SOFA_UPDATE_INTERVAL_MINUTES; si una pasada falla
    por bloqueo de la API (403/429), `sofa_auto_update.siguiente_espera`
    lo duplica en cada intento (hasta 2 h) y vuelve al base al recuperarse.
    """
    base_s = SOFA_UPDATE_INTERVAL_MINUTES * 60
    while not _sofa_stop.is_set():
        try:
            res = sofa_auto_update.ejecutar()
            if res.get("ejecutada"):
                est = res.get("estadisticas") or {}
                espera = sofa_auto_update.siguiente_espera(base_s)
                sufijo = (
                    f" | IP bloqueada: siguiente intento en {espera / 60:.0f} min"
                    if espera > base_s
                    else ""
                )
                print(
                    f"[sofa-update] pasada {res.get('duracion_s')}s | "
                    f"stats: {est.get('descargados', 0)}/{est.get('pendientes', 0)} partidos | "
                    f"error: {res.get('error') or '-'}{sufijo}",
                    flush=True
                )
            else:
                # Heartbeat: el ciclo se ejecutó pero no hubo datos nuevos
                print(f"[sofa-update] heartbeat – no hay datos nuevos", flush=True)
        except Exception as e:  # pragma: no cover
            print(f"[sofa-update] error en el ciclo: {e}", flush=True)
        _sofa_stop.wait(sofa_auto_update.siguiente_espera(base_s))


def _sofa_refresh_now() -> dict:
    """Fuerza un refresco de SofaScore ahora (hilo propio, no bloquea el API)."""
    import sofa_auto_update as _sofa

    if _sofa._in_progress:
        return {"en_progreso": True, "lanzado": False, "ultimo": _sofa.ultimo}
    t = threading.Thread(target=_sofa.ejecutar, name="sofa-refresh", daemon=True)
    t.start()
    return {"en_progreso": True, "lanzado": True, "ultimo": _sofa.ultimo}


# ---------- Startup ----------
@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Inicializa la base de datos y arranca el cierre automático de jornadas."""
    init_db()
    events.init_hub(asyncio.get_running_loop())
    if os.environ.get("GURUTZE_DISABLE_AUTO_CLOSE", "") not in ("1", "true", "True"):
        t = threading.Thread(target=_auto_close_loop, name="auto-close", daemon=True)
        t.start()
        print(
            f"[auto-close] activado: revisa cada {AUTO_CLOSE_INTERVAL_SECONDS}s "
            f"si ya hay resultados de SofaScore para cerrar la jornada",
            flush=True
        )
    if os.environ.get("GURUTZE_NIGHTLY_DISABLE", "") not in ("1", "true", "True"):
        nt = threading.Thread(target=_nightly_loop, name="nightly", daemon=True)
        nt.start()
        print(
            f"[nightly] activado: comprueba cada {NIGHTLY_CHECK_SECONDS}s el "
            f"cambio de mercado/mantenimiento nocturno (05:00)",
            flush=True
        )
    if os.environ.get("GURUTZE_DISABLE_SOFA_UPDATE", "") not in ("1", "true", "True"):
        st = threading.Thread(target=_sofa_update_loop, name="sofa-update", daemon=True)
        st.start()
        print(
            f"[sofa-update] activado: refresca SofaScore cada "
            f"{SOFA_UPDATE_INTERVAL_MINUTES} min (partidos + stats nuevas)",
            flush=True
        )
    yield
    _auto_close_stop.set()
    _nightly_stop.set()
    _sofa_stop.set()


app = FastAPI(title="Gurutze Fantasy API", version="1.0.0", lifespan=lifespan)

# CORS: orígenes desde variable de entorno CORS_ORIGINS, separados por comas.
# Ejemplo: http://localhost:3000,http://127.0.0.1:3000
_cors_origins_raw = os.environ.get(
    "CORS_ORIGINS", "http://localhost:3000,http://127.0.0.1:3000,http://lvh.me:3000,https://gfweb-one.vercel.app"
)
# Convertir la cadena separada por comas a lista de cadenas
_allowed_origins = [o.strip() for o in _cors_origins_raw.split(",") if o.strip()]

app.add_middleware(
    CORSMiddleware,
    allow_origins=_allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Imágenes de SofaScore (fotos de jugadores y escudos de clubes).
# La web las referencia como /sofa/<ruta> y las hace proxy a través de
# Next.js (ver next.config.js).
# Antes de servir desde el disco, se completa la raíz estable con las fotos
# que la BD referencia pero que solo existen en la carpeta de otra temporada.
try:
    from completar_imagenes import asegurar_imagenes_stables
    _copiadas, _ausentes = asegurar_imagenes_stables()
    if _copiadas:
        print(f"[sofa-images] {_copiadas} imagen(es) copiada(s) a {IMAGES_ROOT}")
    if _ausentes:
        print(f"[sofa-images] {_ausentes} ruta(s) sin archivo en ninguna temporada")
except Exception as _e:  # nunca debe tumbar el arranque del servidor
    print(f"[sofa-images] aviso: no se pudo completar la carpeta de imágenes: {_e}")

# CDN externo de SoffaScore (se usa como fallback cuando no hay archivos locales)
SOFASCORE_CDN = "https://api.sofascore.com/api/v1"


def _cdn_url_for_sofa_path(path: str) -> Optional[str]:
    """Convierte una ruta local /sofa/imagenes/... en la URL del CDN de SoffaScore."""
    m = re.match(r"imagenes/jugadores/(\d+)", path)
    if m:
        return f"{SOFASCORE_CDN}/player/{m.group(1)}/image"
    m = re.match(r"imagenes/equipos/(\d+)", path)
    if m:
        return f"{SOFASCORE_CDN}/team/{m.group(1)}/image"
    return None


_SOFA_CDN_HEADERS = {
    "Referer": "https://www.sofascore.com/",
    "Origin": "https://www.sofascore.com",
    "Accept": "*/*",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "X-Requested-With": "XMLHttpRequest",
}

# Tamanio minimo para considerar que la CDN ha devuelto una imagen real
# (el CDN a veces responde 200 con un PNG placeholder de 68 bytes).
_MIN_REAL_IMAGE_BYTES = 100


# ---------- SVG placeholder helpers ---------- #
def _svg_response(initials: str, bg_color: str = "#4A90D9") -> Response:
    """Genera un SVG con las iniciales sobre un fondo de color."""
    from html import escape as _escape
    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">'
        f'<rect width="100" height="100" rx="50" fill="{bg_color}"/>'
        f'<text x="50" y="60" font-family="Arial, sans-serif" '
        f'font-size="40" font-weight="bold" fill="white" '
        f'text-anchor="middle">{_escape(initials)}</text></svg>'
    )
    return Response(content=svg, media_type="image/svg+xml")


def _svg_player_placeholder(player_id: int) -> Response:
    """Look up player name in fantasy DB and generate an SVG with initials."""
    try:
        with get_conn() as conn:
            row = conn.execute(
                "SELECT name FROM players WHERE id = ?", (player_id,)
            ).fetchone()
            if row and row["name"]:
                name = row["name"]
                initials = "".join(w[0].upper() for w in name.split()[:2] if w)
                if initials:
                    return _svg_response(initials, "#4A90D9")
    except Exception:
        pass
    return _svg_response(str(player_id)[-4:], "#6c757d")


def _svg_team_placeholder(team_id: int) -> Response:
    """Look up club name in fantasy DB and generate an SVG with initials."""
    try:
        with get_conn() as conn:
            row = conn.execute(
                "SELECT club_name FROM players WHERE club_id = ? LIMIT 1", (team_id,)
            ).fetchone()
            if row and row["club_name"]:
                initials = "".join(w[0].upper() for w in row["club_name"].split()[:2] if w)
                if initials:
                    return _svg_response(initials, "#28a745")
    except Exception:
        pass
    return _svg_response("?", "#6c757d")


def _svg_placeholder_for_path(path: str) -> Optional[Response]:
    """Devuelve un SVG placeholder para jugadores o equipos, o None."""
    m = re.match(r"imagenes/jugadores/(\d+)", path)
    if m:
        return _svg_player_placeholder(int(m.group(1)))
    m = re.match(r"imagenes/equipos/(\d+)", path)
    if m:
        return _svg_team_placeholder(int(m.group(1)))
    return None


# ---------- Image proxy ---------- #
@app.get("/sofa/{foto_path:path}")
async def serve_sofa_image(foto_path: str):
    """Sirve imágenes de SoffaScore: disco local -> API interna -> CDN externo -> SVG fallback.

    1) Intenta servir desde el directorio local (IMAGES_ROOT).
    2) Intenta la API interna de SoffaScore (http://127.0.0.1:8000/sofa/...).
    3) Fallback: CDN público de SoffaScore usando curl_cffi (TLS fingerprint
       spoofing) para evitar el bloqueo 403. Detecta placeholders de 68 bytes.
    4) Genera un SVG con las iniciales del jugador/equipo como última instancia.
    """
    # 1) Directorio local
    if IMAGES_ROOT.exists():
        file_path = IMAGES_ROOT / foto_path
        if file_path.exists() and file_path.is_file():
            return FileResponse(file_path)

    # 2) API interna de SoffaScore
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(f"{SOFASCORE_API}/sofa/{foto_path}")
            if resp.status_code == 200 and len(resp.content) > _MIN_REAL_IMAGE_BYTES:
                return Response(
                    content=resp.content,
                    media_type=resp.headers.get("content-type", "image/webp"),
                )
    except Exception:
        pass

    # 3) CDN externo (curl_cffi para evitar 403 anti-bot)
    cdn_url = _cdn_url_for_sofa_path(foto_path)
    if cdn_url:
        try:
            async with CurlAsyncSession(impersonate="chrome") as client:
                resp = await client.get(cdn_url, headers=_SOFA_CDN_HEADERS, timeout=15)
                if resp.status_code == 200 and len(resp.content) > _MIN_REAL_IMAGE_BYTES:
                    return Response(
                        content=resp.content,
                        media_type=resp.headers.get("content-type", "image/webp"),
                    )
        except Exception:
            pass

    # 4) SVG fallback (iniciales del jugador/equipo)
    svg = _svg_placeholder_for_path(foto_path)
    if svg:
        return svg

    raise HTTPException(status_code=404, detail="Image not found")


# ---------- Debug: list all registered routes ---------- #
@app.get("/debug/routes")
async def debug_routes():
    """Debug endpoint to list all registered route paths."""
    routes = []
    for r in app.routes:
        if hasattr(r, "path"):
            methods = getattr(r, "methods", None)
            routes.append({"path": r.path, "methods": list(methods) if methods else None})
    return {"count": len(routes), "routes": routes}


# ---------- SoffaScore API proxy ---------- #
@app.get("/sofascore-proxy/{foto_path:path}")
async def sofascore_api_proxy(foto_path: str, request: Request):
    """Proxy SoffaScore API requests: internal API -> external CDN.

    Permite al frontend acceder a /api/players, /api/standings, etc. a través
    del proxy de Vercel -> backend -> API de SoffaScore.

    - Primero intenta la API interna (http://127.0.0.1:8000).
    - Si falla (no disponible en producción), intenta el CDN externo con
      curl_cffi (TLS fingerprinting) para evitar bloqueos 403.
    """
    params = dict(request.query_params) if request.query_params else None

    # 1) Internal SoffaScore API
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(f"{SOFASCORE_API}/{foto_path}", params=params)
            if resp.status_code == 200:
                return Response(
                    content=resp.content,
                    media_type=resp.headers.get("content-type", "application/json"),
                )
    except Exception:
        pass

    # 2) External CDN (strip "api/" prefix since CDN URL already has /api/v1)
    cdn_path = foto_path[4:] if foto_path.startswith("api/") else foto_path
    cdn_url = f"{SOFASCORE_CDN}/{cdn_path}"
    try:
        async with CurlAsyncSession(impersonate="chrome") as client:
            resp = await client.get(
                cdn_url, headers=_SOFA_CDN_HEADERS, params=params, timeout=30
            )
            return Response(
                content=resp.content,
                media_type=resp.headers.get("content-type", "application/json"),
            )
    except Exception:
        pass

    raise HTTPException(
        status_code=502,
        detail="SofaScore API unavailable (internal API and CDN fallback both failed)",
    )


# ---------- Auth helpers ----------
def create_token(user_id: int, username: str) -> str:
    payload = {"sub": str(user_id), "username": username, "exp": int(time.time()) + 60 * 60 * 24 * 7}
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)


def set_token_cookie(response: Response, token: str, request: Request = None):
    """Set httpOnly cookie with JWT token. Works for both localhost and lvh.me."""
    # Detect if we're on localhost or lvh.me
    cookie_domain = "lvh.me"
    if request:
        host = request.headers.get("host", "")
        if "localhost" in host or "127.0.0.1" in host:
            cookie_domain = None  # No domain = current host only (localhost)
    
    response.set_cookie(
        key="fantasy_token",
        value=token,
        httponly=True,
        secure=False,  # True in production with HTTPS
        samesite="lax",
        domain=cookie_domain,  # None for localhost, "lvh.me" for lvh.me
        max_age=60 * 60 * 24 * 7,  # 7 days
        path="/",
    )


def clear_token_cookie(response: Response, request: Request = None):
    """Clear the auth cookie."""
    cookie_domain = "lvh.me"
    if request:
        host = request.headers.get("host", "")
        if "localhost" in host or "127.0.0.1" in host:
            cookie_domain = None
    
    response.delete_cookie(key="fantasy_token", path="/", httponly=True, samesite="lax", domain=cookie_domain)


def decode_token(token: str) -> dict:
    try:
        return jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
    except jwt.PyJWTError:
        raise HTTPException(status_code=401, detail="Invalid token")


async def get_current_user(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
    token_cookie: Optional[str] = Cookie(None, alias="fantasy_token"),
    token_param: Optional[str] = Query(None, alias="token"),
) -> dict:
    token = credentials.credentials if credentials else (token_cookie or token_param)
    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated")
    payload = decode_token(token)
    user_id = int(payload["sub"])
    with get_conn() as conn:
        row = conn.execute("SELECT id, username, email FROM users WHERE id = ?", (user_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=401, detail="User not found")
    return dict(row)


def get_user_league(user: dict, league_id: int) -> int:
    """Verify user belongs to league and return user_league_id."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT id FROM user_leagues WHERE user_id = ? AND league_id = ?",
            (user["id"], league_id)
        ).fetchone()
    if not row:
        raise HTTPException(status_code=403, detail="Not a member of this league")
    return row["id"]


# ---------- Models ----------
class RegisterRequest(BaseModel):
    username: str
    email: EmailStr
    password: str
    team_name: Optional[str] = None
    league_name: Optional[str] = None
    invite_code: Optional[str] = None


class LoginRequest(BaseModel):
    username: str
    password: str


class CreateLeagueRequest(BaseModel):
    name: str
    season_start: Optional[str] = None


class JoinLeagueRequest(BaseModel):
    invite_code: str
    team_name: str


class UpdateSquadRequest(BaseModel):
    formation: str
    lineup: list[dict]  # [{"player_id": int, "position_slot": str, "is_starter": bool, "slot_order": int}]
    captain_id: int


class BidRequest(BaseModel):
    amount: int


class OfferRequest(BaseModel):
    target_player_id: int
    cash: int = 0
    player_ids: list[int] = []


class OfferResponseRequest(BaseModel):
    action: str  # accept, reject, counter


# ---------- Auth endpoints ----------
@app.post("/auth/register")
def auth_register(req: RegisterRequest, response: Response, request: Request):
    """
    Registro de usuario.

    - Si se envía `invite_code`, el usuario entra en esa liga existente.
    - Si no, se crea una liga nueva para él (y su plantilla inicial).
    Así, al registrarse, el usuario ya puede jugar sin pasos extra.
    """
    try:
        user = register_user(req.username, req.email, req.password)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    team_name = (req.team_name or "").strip() or f"{req.username} FC"

    try:
        if req.invite_code:
            join_league(user["id"], req.invite_code.strip().upper(), team_name)
        else:
            league_name = (req.league_name or "").strip() or f"Liga de {req.username}"
            create_league(user["id"], league_name, team_name=team_name)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    token = create_token(user["id"], user["username"])
    set_token_cookie(response, token, request)  # keep for localhost dev
    context = get_user_context(user["id"])
    return {"token": token, **context}


@app.post("/auth/login")
def auth_login(req: LoginRequest, response: Response, request: Request):
    user = authenticate_user(req.username, req.password)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid credentials")
    token = create_token(user["id"], user["username"])
    set_token_cookie(response, token, request)  # keep for localhost dev
    context = get_user_context(user["id"])
    return {"token": token, **context}


@app.post("/auth/logout")
def auth_logout(response: Response, request: Request):
    clear_token_cookie(response, request)
    return {"ok": True}


@app.get("/auth/me")
def auth_me(user: dict = Depends(get_current_user)):
    context = get_user_context(user["id"])
    # Si el usuario no tiene liga todavía, se le devuelve igualmente su identidad
    return context or user


class UpdateEmailRequest(BaseModel):
    new_email: EmailStr
    current_password: str


class UpdatePasswordRequest(BaseModel):
    current_password: str
    new_password: str


def _verify_current_password(user: dict, current_password: str) -> None:
    """Exige la contraseña actual antes de tocar la cuenta (Pantalla 06)."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT password_hash FROM users WHERE id = ?", (user["id"],)
        ).fetchone()
    if not row or not verify_password(current_password, row["password_hash"]):
        raise HTTPException(status_code=400, detail="Contraseña actual incorrecta")


@app.put("/auth/email")
def auth_update_email(req: UpdateEmailRequest, user: dict = Depends(get_current_user)):
    """Cambia el correo de la cuenta (Pantalla 06: Configuración)."""
    _verify_current_password(user, req.current_password)
    try:
        update_user_email(user["id"], req.new_email)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True, "email": req.new_email}


@app.put("/auth/password")
def auth_update_password(req: UpdatePasswordRequest, user: dict = Depends(get_current_user)):
    """Cambia la contraseña de la cuenta (Pantalla 06: Configuración)."""
    _verify_current_password(user, req.current_password)
    try:
        update_user_password(user["id"], req.new_password)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True}


class RequestResetRequest(BaseModel):
    email: EmailStr


class ResetPasswordRequest(BaseModel):
    token: str
    new_password: str


@app.post("/auth/request-reset")
def auth_request_reset(req: RequestResetRequest):
    """Solicita el correo de recuperación de contraseña.

    Devuelve SIEMPRE 200 (no revela si el correo está registrado).
    En modo dev el enlace se imprime en la terminal del servidor.
    """
    try:
        result = create_password_reset_token(req.email)
    except ValueError as e:
        raise HTTPException(status_code=429, detail=str(e))
    if result:
        token, username = result
        try:
            send_password_reset_email(req.email, username, token)
        except RuntimeError as e:
            raise HTTPException(status_code=500, detail=str(e))
    return {"ok": True}


@app.post("/auth/reset-password")
def auth_reset_password(req: ResetPasswordRequest):
    """Cambia la contraseña usando el token del enlace del correo."""
    try:
        username = apply_password_reset(req.token, req.new_password)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True, "username": username}


# ---------- League endpoints ----------
@app.post("/leagues")
def league_create(req: CreateLeagueRequest, user: dict = Depends(get_current_user)):
    league = create_league(user["id"], req.name, req.season_start)
    return league


@app.post("/leagues/join")
def league_join(req: JoinLeagueRequest, user: dict = Depends(get_current_user)):
    try:
        result = join_league(user["id"], req.invite_code.upper(), req.team_name)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return result


@app.get("/leagues/{league_id}")
def league_get(league_id: int, user: dict = Depends(get_current_user)):
    with get_conn() as conn:
        league = conn.execute("SELECT * FROM leagues WHERE id = ?", (league_id,)).fetchone()
        if not league:
            raise HTTPException(status_code=404, detail="League not found")
        members = conn.execute(
            """
            SELECT ul.id as user_league_id, u.username, ul.team_name, ul.budget, ul.joined_at
            FROM user_leagues ul
            JOIN users u ON u.id = ul.user_id
            WHERE ul.league_id = ?
            """,
            (league_id,)
        ).fetchall()
    league_data = dict(league)
    try:
        league_data["real_current_matchday"] = real_current_matchday()
    except Exception:
        league_data["real_current_matchday"] = 1
    return {"league": league_data, "members": [dict(m) for m in members]}


@app.get("/leagues/{league_id}/status")
def league_status(league_id: int, user: dict = Depends(get_current_user)):
    """Estado en vivo de la liga (jornada actual + si ya hay resultados)."""
    get_user_league(user, league_id)
    return get_league_status(league_id)


@app.get("/leagues/{league_id}/events")
async def league_events(league_id: int, user: dict = Depends(get_current_user)):
    """Stream SSE de eventos en tiempo real de la liga (p. ej. cierre de jornada).

    Emite primero el estado actual y luego un evento cada vez que se cierra una
    jornada (auto o manual). Incluye keep-alives para mantener la conexión viva.
    El `EventSource` del frontend lo consume y reconecta solo si se cae.
    """
    get_user_league(user, league_id)
    status = get_league_status(league_id)
    queue: "asyncio.Queue" = asyncio.Queue()
    events.register(league_id, queue)

    def sse(payload: dict) -> str:
        return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"

    async def gen():
        try:
            yield sse(
                {
                    "type": "matchday_status",
                    "league_id": league_id,
                    "current_matchday": status["current_matchday"],
                    "results_available": status["results_available"],
                }
            )
            while True:
                try:
                    ev = await asyncio.wait_for(queue.get(), timeout=20)
                    yield sse(ev)
                except asyncio.TimeoutError:
                    # Keep-alive: comentario SSE (el cliente lo ignora).
                    yield ": ping\n\n"
        except asyncio.CancelledError:
            raise
        finally:
            events.unregister(league_id, queue)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.get("/leagues")
def league_list(user: dict = Depends(get_current_user)):
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT l.*, ul.id as user_league_id, ul.team_name, ul.budget
            FROM leagues l
            JOIN user_leagues ul ON ul.league_id = l.id
            WHERE ul.user_id = ?
            """,
            (user["id"],)
        ).fetchall()
    return [dict(r) for r in rows]


# ---------- Squad endpoints ----------
@app.get("/leagues/{league_id}/squad")
def squad_get(league_id: int, user: dict = Depends(get_current_user)):
    ul_id = get_user_league(user, league_id)
    squad = get_squad(ul_id)
    return {"squad": squad}


@app.get("/leagues/{league_id}/matchday/{matchday}")
def matchday_squad_get(league_id: int, matchday: int, user: dict = Depends(get_current_user)):
    ul_id = get_user_league(user, league_id)
    ms = get_matchday_squad(ul_id, matchday)
    if not ms:
        # Auto-create default for this matchday
        with get_conn() as conn:
            ms_id = create_default_matchday_squad(ul_id, matchday, conn)
        ms = get_matchday_squad(ul_id, matchday)
    return ms


@app.put("/leagues/{league_id}/matchday/{matchday}")
def matchday_squad_put(league_id: int, matchday: int, req: UpdateSquadRequest, user: dict = Depends(get_current_user)):
    ul_id = get_user_league(user, league_id)
    # Validate formation
    if req.formation not in FORMATIONS:
        raise HTTPException(status_code=400, detail=f"Invalid formation. Allowed: {list(FORMATIONS.keys())}")
    # Validate starters count = 11
    starters = [p for p in req.lineup if p.get("is_starter")]
    if len(starters) != 11:
        raise HTTPException(status_code=400, detail="Must have exactly 11 starters")
    # Validate captain is a starter (0 = sin capitán, canon §3)
    starter_ids = {p["player_id"] for p in starters}
    if req.captain_id and req.captain_id not in starter_ids:
        raise HTTPException(status_code=400, detail="Captain must be a starter")
    # Validate all players belong to user's squad
    with get_conn() as conn:
        squad_ids = {r["player_id"] for r in conn.execute(
            "SELECT player_id FROM squad_players WHERE user_league_id = ?", (ul_id,)
        ).fetchall()}
    for p in req.lineup:
        if p["player_id"] not in squad_ids:
            raise HTTPException(status_code=400, detail=f"Player {p['player_id']} not in your squad")
    result = update_matchday_squad(ul_id, matchday, req.formation, req.lineup, req.captain_id or None)
    return result


# ---------- Market endpoints ----------
@app.get("/leagues/{league_id}/market")
async def market_get(league_id: int, user: dict = Depends(get_current_user)):
    get_user_league(user, league_id)  # verify membership
    market = await get_today_market(league_id)
    if not market:
        # Si todavía no existe el mercado de hoy (no hay cron), se genera ahora.
        market = await refresh_market(league_id)
    return {"market": market, "date": date.today().isoformat()}


@app.post("/leagues/{league_id}/market/refresh")
async def market_refresh(league_id: int, user: dict = Depends(get_current_user)):
    # Only league owner can manually refresh (normally done by cron at 05:00)
    with get_conn() as conn:
        league = conn.execute("SELECT owner_id FROM leagues WHERE id = ?", (league_id,)).fetchone()
    if league["owner_id"] != user["id"]:
        raise HTTPException(status_code=403, detail="Only league owner can refresh market")
    market = await refresh_market(league_id)
    return {"market": market, "date": date.today().isoformat()}


@app.post("/leagues/{league_id}/market/{market_player_id}/bid")
def market_bid(league_id: int, market_player_id: int, req: BidRequest, user: dict = Depends(get_current_user)):
    ul_id = get_user_league(user, league_id)
    try:
        place_bid(market_player_id, ul_id, req.amount)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"success": True}


@app.get("/leagues/{league_id}/my-bids")
def my_bids(league_id: int, user: dict = Depends(get_current_user)):
    ul_id = get_user_league(user, league_id)
    bids = get_my_bids(ul_id)
    return {"bids": bids}


@app.delete("/leagues/{league_id}/my-bids/{bid_id}")
def cancel_my_bid(league_id: int, bid_id: int, user: dict = Depends(get_current_user)):
    ul_id = get_user_league(user, league_id)
    try:
        return cancel_bid(bid_id, ul_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


# ---------- User Offers ----------
@app.post("/leagues/{league_id}/offers")
def offer_create(league_id: int, req: OfferRequest, user: dict = Depends(get_current_user)):
    ul_id = get_user_league(user, league_id)
    # Find target owner
    with get_conn() as conn:
        target = conn.execute(
            "SELECT user_league_id FROM squad_players WHERE player_id = ? AND user_league_id IN (SELECT id FROM user_leagues WHERE league_id = ?)",
            (req.target_player_id, league_id)
        ).fetchone()
    if not target:
        raise HTTPException(status_code=404, detail="Player not found in this league")
    try:
        offer = create_user_offer(ul_id, target["user_league_id"], req.target_player_id, req.cash, req.player_ids)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return offer


@app.get("/leagues/{league_id}/offers")
def offer_list(league_id: int, user: dict = Depends(get_current_user)):
    ul_id = get_user_league(user, league_id)
    with get_conn() as conn:
        received = conn.execute(
            """
            SELECT o.*, u.username as from_username, ul.team_name as from_team,
                   p.name as player_name, p.position, p.market_value
            FROM user_offers o
            JOIN user_leagues ul ON ul.id = o.from_user_league_id
            JOIN users u ON u.id = ul.user_id
            JOIN players p ON p.id = o.target_player_id
            WHERE o.to_user_league_id = ?
            ORDER BY o.created_at DESC
            """,
            (ul_id,)
        ).fetchall()
        sent = conn.execute(
            """
            SELECT o.*, u.username as to_username, ul.team_name as to_team,
                   p.name as player_name, p.position, p.market_value
            FROM user_offers o
            JOIN user_leagues ul ON ul.id = o.to_user_league_id
            JOIN users u ON u.id = ul.user_id
            JOIN players p ON p.id = o.target_player_id
            WHERE o.from_user_league_id = ?
            ORDER BY o.created_at DESC
            """,
            (ul_id,)
        ).fetchall()
    return {"received": [dict(r) for r in received], "sent": [dict(r) for r in sent]}


@app.post("/leagues/{league_id}/offers/{offer_id}/respond")
def offer_respond(league_id: int, offer_id: int, req: OfferResponseRequest, user: dict = Depends(get_current_user)):
    ul_id = get_user_league(user, league_id)
    try:
        result = respond_to_offer(offer_id, ul_id, req.action)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return result


# ---------- Budget ----------
@app.get("/leagues/{league_id}/budget")
def budget_get(league_id: int, user: dict = Depends(get_current_user)):
    ul_id = get_user_league(user, league_id)
    return get_budget_info(ul_id)


# ---------- Transactions ----------
@app.get("/leagues/{league_id}/transactions")
def transactions_get(league_id: int, user: dict = Depends(get_current_user), limit: int = 50):
    ul_id = get_user_league(user, league_id)
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM transactions WHERE user_league_id = ? ORDER BY created_at DESC LIMIT ?",
            (ul_id, limit)
        ).fetchall()
    return {"transactions": [dict(r) for r in rows]}


# ---------- Supertablón ----------
@app.get("/leagues/{league_id}/supertablon")
def supertablon_get(league_id: int, user: dict = Depends(get_current_user), limit: int = 20):
    get_user_league(user, league_id)
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM supertablon_entries WHERE league_id = ? ORDER BY created_at DESC LIMIT ?",
            (league_id, limit)
        ).fetchall()
    entries = [dict(r) for r in rows]
    # Mark as read for this user
    ul_id = get_user_league(user, league_id)
    for e in entries:
        read_by = json.loads(e["read_by"])
        if ul_id not in read_by:
            read_by.append(ul_id)
            with get_conn() as conn:
                conn.execute("UPDATE supertablon_entries SET read_by = ? WHERE id = ?", (json.dumps(read_by), e["id"]))
    return {"entries": entries}


# ---------- Liga Standings ----------
@app.get("/leagues/{league_id}/standings")
def league_standings_get(league_id: int, user: dict = Depends(get_current_user), matchday: int = None):
    get_user_league(user, league_id)
    with get_conn() as conn:
        # Get all users in league with their points
        if matchday:
            # Standings for a specific matchday
            rows = conn.execute(
                """
                SELECT ul.id as user_league_id, u.username, ul.team_name,
                       COALESCE(mr.points, 0) as points,
                       COALESCE(mr.goals, 0) as goals,
                       COALESCE(mr.assists, 0) as assists,
                       (SELECT SUM(p.market_value) FROM squad_players sp
                        JOIN players p ON p.id = sp.player_id
                        WHERE sp.user_league_id = ul.id) as squad_value
                FROM user_leagues ul
                JOIN users u ON u.id = ul.user_id
                LEFT JOIN matchday_results mr ON mr.user_league_id = ul.id AND mr.matchday = ?
                WHERE ul.league_id = ?
                ORDER BY points DESC, goals DESC
                """,
                (matchday, league_id)
            ).fetchall()
        else:
            # General standings (accumulated)
            rows = conn.execute(
                """
                SELECT ul.id as user_league_id, u.username, ul.team_name,
                       COALESCE(SUM(mr.points), 0) as points,
                       COALESCE(SUM(mr.goals), 0) as goals,
                       COALESCE(SUM(mr.assists), 0) as assists,
                       (SELECT SUM(p.market_value) FROM squad_players sp
                        JOIN players p ON p.id = sp.player_id
                        WHERE sp.user_league_id = ul.id) as squad_value
                FROM user_leagues ul
                JOIN users u ON u.id = ul.user_id
                LEFT JOIN matchday_results mr ON mr.user_league_id = ul.id
                WHERE ul.league_id = ?
                GROUP BY ul.id, u.username, ul.team_name
                ORDER BY points DESC, goals DESC
                """,
                (league_id,)
            ).fetchall()
    return {"standings": [dict(r) for r in rows]}


@app.get("/leagues/{league_id}/achievements")
def league_achievements_get(league_id: int, user: dict = Depends(get_current_user)):
    get_user_league(user, league_id)
    # Calculate achievements based on matchday_results and transactions
    achievements = []
    with get_conn() as conn:
        # Best streak
        best_streak = conn.execute(
            """
            SELECT ul.team_name, u.username, MAX(streak) as best_streak
            FROM (
                SELECT user_league_id,
                       SUM(CASE WHEN points > 0 THEN 1 ELSE 0 END) OVER (PARTITION BY user_league_id ORDER BY matchday ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) as streak
                FROM matchday_results
                WHERE league_id = ?
            ) s
            JOIN user_leagues ul ON ul.id = s.user_league_id
            JOIN users u ON u.id = ul.user_id
            GROUP BY ul.team_name, u.username
            ORDER BY best_streak DESC LIMIT 1
            """,
            (league_id,)
        ).fetchone()
        if best_streak:
            achievements.append({"type": "mejor_racha", "title": "🔥 Mejor racha", "owner": best_streak["team_name"], "value": best_streak["best_streak"]})

        # Best economic performance
        best_econ = conn.execute(
            """
            SELECT ul.team_name, u.username, SUM(CASE WHEN t.type = 'sale' THEN t.amount ELSE 0 END) as profit
            FROM transactions t
            JOIN user_leagues ul ON ul.id = t.user_league_id
            JOIN users u ON u.id = ul.user_id
            WHERE ul.league_id = ?
            GROUP BY ul.team_name, u.username
            ORDER BY profit DESC LIMIT 1
            """,
            (league_id,)
        ).fetchone()
        if best_econ and best_econ["profit"] > 0:
            achievements.append({"type": "mejor_rendimiento_economico", "title": "💰 Mejor rendimiento económico", "owner": best_econ["team_name"], "value": f"{best_econ['profit']:,} €".replace(",", ".")})

        # Cheapest XI
        cheapest_xi = conn.execute(
            """
            SELECT ul.team_name, u.username, SUM(p.market_value) as total_value
            FROM matchday_lineup ml
            JOIN matchday_squad ms ON ms.id = ml.matchday_squad_id
            JOIN user_leagues ul ON ul.id = ms.user_league_id
            JOIN users u ON u.id = ul.user_id
            JOIN players p ON p.id = ml.player_id
            WHERE ml.is_starter = 1 AND ul.league_id = ?
            GROUP BY ul.team_name, u.username
            ORDER BY total_value ASC LIMIT 1
            """,
            (league_id,)
        ).fetchone()
        if cheapest_xi:
            achievements.append({"type": "once_mas_barato", "title": "💸 Once más barato", "owner": cheapest_xi["team_name"], "value": f"{cheapest_xi['total_value']:,} €".replace(",", ".")})

        # Most expensive XI
        expensive_xi = conn.execute(
            """
            SELECT ul.team_name, u.username, SUM(p.market_value) as total_value
            FROM matchday_lineup ml
            JOIN matchday_squad ms ON ms.id = ml.matchday_squad_id
            JOIN user_leagues ul ON ul.id = ms.user_league_id
            JOIN users u ON u.id = ul.user_id
            JOIN players p ON p.id = ml.player_id
            WHERE ml.is_starter = 1 AND ul.league_id = ?
            GROUP BY ul.team_name, u.username
            ORDER BY total_value DESC LIMIT 1
            """,
            (league_id,)
        ).fetchone()
        if expensive_xi:
            achievements.append({"type": "once_mas_caro", "title": "👟 Once más caro", "owner": expensive_xi["team_name"], "value": f"{expensive_xi['total_value']:,} €".replace(",", ".")})

        # Highest score
        highest_score = conn.execute(
            """
            SELECT ul.team_name, u.username, MAX(points) as max_points
            FROM matchday_results mr
            JOIN user_leagues ul ON ul.id = mr.user_league_id
            JOIN users u ON u.id = ul.user_id
            WHERE mr.league_id = ?
            GROUP BY ul.team_name, u.username
            ORDER BY max_points DESC LIMIT 1
            """,
            (league_id,)
        ).fetchone()
        if highest_score:
            achievements.append({"type": "mayor_puntuacion", "title": "⭐ Mayor puntuación", "owner": highest_score["team_name"], "value": highest_score["max_points"]})

        # Most goals
        most_goals = conn.execute(
            """
            SELECT ul.team_name, u.username, SUM(goals) as total_goals
            FROM matchday_results mr
            JOIN user_leagues ul ON ul.id = mr.user_league_id
            JOIN users u ON u.id = ul.user_id
            WHERE mr.league_id = ?
            GROUP BY ul.team_name, u.username
            ORDER BY total_goals DESC LIMIT 1
            """,
            (league_id,)
        ).fetchone()
        if most_goals:
            achievements.append({"type": "mas_goles", "title": "⚽ Más goles", "owner": most_goals["team_name"], "value": most_goals["total_goals"]})

        # Most MVPs
        most_mvp = conn.execute(
            """
            SELECT ul.team_name, u.username, COUNT(*) as mvp_count
            FROM matchday_results mr
            JOIN user_leagues ul ON ul.id = mr.user_league_id
            JOIN users u ON u.id = ul.user_id
            WHERE mr.league_id = ? AND mr.mvp_player_id IS NOT NULL
            GROUP BY ul.team_name, u.username
            ORDER BY mvp_count DESC LIMIT 1
            """,
            (league_id,)
        ).fetchone()
        if most_mvp:
            achievements.append({"type": "mas_mvp", "title": "👑 Más MVP", "owner": most_mvp["team_name"], "value": most_mvp["mvp_count"]})

        # Most red cards
        most_red = conn.execute(
            """
            SELECT ul.team_name, u.username, COUNT(*) as red_count
            FROM transactions t
            JOIN user_leagues ul ON ul.id = t.user_league_id
            JOIN users u ON u.id = ul.user_id
            WHERE ul.league_id = ? AND t.type = 'penalty_red'
            GROUP BY ul.team_name, u.username
            ORDER BY red_count DESC LIMIT 1
            """,
            (league_id,)
        ).fetchone()
        if most_red:
            achievements.append({"type": "mas_expulsados", "title": "🟥 Más expulsados", "owner": most_red["team_name"], "value": most_red["red_count"]})

    return {"achievements": achievements}


# ---------- Player sync ----------
@app.post("/admin/sync-players")
async def admin_sync_players(user: dict = Depends(get_current_user)):
    # In production, restrict to admin users
    # First try the live API; if that returns 0 (API blocked), fall back to
    # reading directly from the local SoffaScore SQLite DB.
    count = await sync_players_from_sofascore()
    if count == 0:
        count = sync_players_from_sofascore_db()
    return {"synced": count}


# ---------- Health ---------- #
@app.get("/health")
def health():
    return {
        "status": "ok",
        "service": "gurutze-fantasy",
        "sofascore_api": SOFASCORE_API,
        "version": "v2-debug-fix",
        "routes_count": len([r for r in app.routes if hasattr(r, "path")]),
    }


# ---------- Matchday Scoring ----------
@app.post("/leagues/{league_id}/matchday/{matchday}/score")
def score_matchday(league_id: int, matchday: int, user: dict = Depends(get_current_user)):
    """Cierra una jornada: puntúa a todos y avanza la jornada actual (solo dueño)."""
    with get_conn() as conn:
        league = conn.execute("SELECT owner_id FROM leagues WHERE id = ?", (league_id,)).fetchone()
    if league["owner_id"] != user["id"]:
        raise HTTPException(status_code=403, detail="Only league owner can close matchday")

    out = close_matchday(league_id, matchday)
    if out["advanced_to"] is not None:
        events.publish(
            league_id,
            {
                "type": "matchday_closed",
                "league_id": league_id,
                "matchday": matchday,
                "advanced_to": out["advanced_to"],
            },
        )
    return {"matchday": matchday, "results": out["results"], "advanced_to": out["advanced_to"]}


# ---------- Administración de la liga (solo el creador) ----------
def _require_league_owner(user: dict, league_id: int) -> None:
    """404 si la liga no existe; 403 si el usuario no es su creador."""
    with get_conn() as conn:
        league = conn.execute(
            "SELECT owner_id FROM leagues WHERE id = ?", (league_id,)
        ).fetchone()
    if not league:
        raise HTTPException(status_code=404, detail="League not found")
    if league["owner_id"] != user["id"]:
        raise HTTPException(status_code=403, detail="Only the league creator can do this")


class ResetLeagueRequest(BaseModel):
    from_matchday: Optional[int] = None


class GrantBudgetRequest(BaseModel):
    user_league_id: int
    amount: int
    description: Optional[str] = None


class SetMarketSizeRequest(BaseModel):
    market_size: int


class CancelTransferRequest(BaseModel):
    kind: str
    ref_id: int


@app.post("/leagues/{league_id}/reset")
def league_reset(league_id: int, body: ResetLeagueRequest, user: dict = Depends(get_current_user)):
    """Reinicia la liga desde cero: jornada inicial, 0 puntos y plantillas nuevas."""
    _require_league_owner(user, league_id)
    try:
        out = admin_reset_league(league_id, body.from_matchday)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    events.publish(
        league_id,
        {
            "type": "league_reset",
            "league_id": league_id,
            "from_matchday": out["from_matchday"],
        },
    )
    return out


@app.post("/leagues/{league_id}/admin/budget")
def league_admin_budget(league_id: int, body: GrantBudgetRequest, user: dict = Depends(get_current_user)):
    """El creador da (o quita, con cantidad negativa) dinero a un equipo."""
    _require_league_owner(user, league_id)
    try:
        out = admin_grant_budget(
            league_id, body.user_league_id, body.amount, body.description or ""
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return out


@app.post("/leagues/{league_id}/admin/market")
def league_admin_market(league_id: int, body: SetMarketSizeRequest, user: dict = Depends(get_current_user)):
    """El creador cambia el nº de jugadores publicados por el mercado."""
    _require_league_owner(user, league_id)
    try:
        size = admin_set_market_size(league_id, body.market_size)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"market_size": size}


@app.get("/leagues/{league_id}/admin/transfers")
def league_admin_transfers(league_id: int, user: dict = Depends(get_current_user)):
    """Últimos fichajes (pujas ganadas y ofertas aceptadas) cancelables."""
    _require_league_owner(user, league_id)
    return admin_list_transfers(league_id)


@app.post("/leagues/{league_id}/admin/transfers/cancel")
def league_admin_cancel_transfer(league_id: int, body: CancelTransferRequest, user: dict = Depends(get_current_user)):
    """El creador deshace un fichaje mal hecho (puja ganada u oferta aceptada)."""
    _require_league_owner(user, league_id)
    try:
        out = admin_cancel_transfer(league_id, body.kind, body.ref_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return out


@app.get("/leagues/{league_id}/admin/sofa-status")
def league_admin_sofa_status(league_id: int, user: dict = Depends(get_current_user)):
    """Datos de SofaScore: última pasada del auto-update y estado actual de la BD.

    Solo el creador de la liga.
    """
    _require_league_owner(user, league_id)
    return sofa_auto_update.estado()


@app.post("/leagues/{league_id}/admin/sofa-refresh")
def league_admin_sofa_refresh(league_id: int, user: dict = Depends(get_current_user)):
    """Fuerza un refresco de SofaScore ahora (corre en segundo plano).

    Solo el creador de la liga. Si ya hay una pasada en marcha, no arranca otra.
    """
    _require_league_owner(user, league_id)
    return _sofa_refresh_now()


@app.get("/leagues/{league_id}/matchday/{matchday}/results")
def matchday_results_get(league_id: int, matchday: int, user: dict = Depends(get_current_user)):
    get_user_league(user, league_id)
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT mr.*, u.username, ul.team_name
            FROM matchday_results mr
            JOIN user_leagues ul ON ul.id = mr.user_league_id
            JOIN users u ON u.id = ul.user_id
            WHERE mr.league_id = ? AND mr.matchday = ?
            ORDER BY mr.points DESC
            """,
            (league_id, matchday)
        ).fetchall()
    return {"results": [dict(r) for r in rows]}


# ---------- Datos del usuario (pantalla Datos) ----------
@app.get("/leagues/{league_id}/me/season")
def my_season_stats_get(league_id: int, user: dict = Depends(get_current_user)):
    """Estadísticas, economía y récords personales reales del usuario."""
    ul_id = get_user_league(user, league_id)
    return get_user_season_stats(ul_id, league_id)


@app.get("/leagues/{league_id}/me/evolution")
def my_evolution_get(league_id: int, user: dict = Depends(get_current_user)):
    """Evolución por jornada del usuario: puntos, acumulados y posición."""
    ul_id = get_user_league(user, league_id)
    return {"evolution": get_user_evolution(ul_id, league_id)}


# ---------- Recordatorios de Home (Canon §9) ----------
@app.get("/leagues/{league_id}/me/reminders")
def my_reminders(league_id: int, user: dict = Depends(get_current_user)):
    """Avisos de la Home pendientes para el usuario.

    Es una sola lectura: al devolverlos se marcan como leídos (Canon §9:
    "tras consultarlos desaparecen").
    """
    ul_id = get_user_league(user, league_id)
    return {"reminders": get_reminders(ul_id)}


# ---------- Datos de fútbol real (Canon §9: pantalla Datos) ----------
@app.get("/leagues/{league_id}/datos/clasificacion")
def datos_clasificacion(league_id: int, user: dict = Depends(get_current_user)):
    """Clasificación real de LaLiga (BD SofaScore) con racha de cada club."""
    get_user_league(user, league_id)
    return {"standings": get_real_standings()}


@app.get("/leagues/{league_id}/datos/partidos")
def datos_partidos(league_id: int, user: dict = Depends(get_current_user), ronda: Optional[int] = None):
    """Partidos reales de una jornada (por defecto, la jornada activa de la liga)."""
    ul_id = get_user_league(user, league_id)
    with get_conn() as conn:
        cur = conn.execute(
            "SELECT l.current_matchday FROM leagues l "
            "JOIN user_leagues ul ON ul.league_id = l.id WHERE ul.id = ?",
            (ul_id,),
        ).fetchone()
    r = ronda or (cur["current_matchday"] if cur else 1)
    return {"ronda": r, "fixtures": get_real_fixtures(r)}


@app.get("/leagues/{league_id}/datos/jugadores")
def datos_jugadores(
    league_id: int,
    user: dict = Depends(get_current_user),
    q: str = "",
    pos: str = "",
    equipo: str = "",
    lesionado: Optional[bool] = None,
):
    """Búsqueda de jugadores reales con filtros (Canon §9)."""
    get_user_league(user, league_id)
    return {"players": search_real_players(q=q, pos=pos, equipo=equipo, lesionado=lesionado)}


# ---------- Nightly Maintenance ----------
@app.post("/leagues/{league_id}/maintenance/nightly")
def nightly_maintenance(league_id: int, user: dict = Depends(get_current_user)):
    """Run nightly maintenance: contracts, Supertablón generation (owner only)."""
    with get_conn() as conn:
        league = conn.execute("SELECT owner_id FROM leagues WHERE id = ?", (league_id,)).fetchone()
    if league["owner_id"] != user["id"]:
        raise HTTPException(status_code=403, detail="Only league owner can run maintenance")
    result = run_nightly_maintenance(league_id)
    return {"success": True, "result": result}


@app.post("/leagues/{league_id}/maintenance/auto-close")
def auto_close_trigger(league_id: int, user: dict = Depends(get_current_user)):
    """Fuerza el cierre automático de la jornada actual si ya hay resultados de SofaScore."""
    with get_conn() as conn:
        league = conn.execute("SELECT owner_id FROM leagues WHERE id = ?", (league_id,)).fetchone()
    if league["owner_id"] != user["id"]:
        raise HTTPException(status_code=403, detail="Only league owner can trigger auto-close")
    out = auto_close_matchday(league_id)
    if out.get("closed"):
        events.publish(
            league_id,
            {
                "type": "matchday_closed",
                "league_id": league_id,
                "matchday": out.get("matchday"),
                "advanced_to": out.get("advanced_to"),
            },
        )
    return out


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8001)