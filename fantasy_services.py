# fantasy_services.py
from __future__ import annotations

import hashlib
import json
import os
import random
import secrets
import httpx
import sqlite3
from curl_cffi.requests import AsyncSession as CurlAsyncSession
from datetime import datetime as dt_datetime, date as dt_date, timedelta, timezone as dt_timezone
from typing import Any, Optional

from fantasy_db import (
    get_conn, transaction, hash_password, verify_password, needs_rehash,
    generate_invite_code, now_iso, FANTASY_DB_PATH
)
from fantasy_config import (
    SOFASCORE_API, SOFASCORE_CDN, SOFASCORE_DB_PATH, INITIAL_BUDGET, MAX_SQUAD_SIZE, CONVOCATORIA_SIZE,
    CONTRACT_WEEKS, TOP_20_EXCLUDE, POSITION_ROLES, FORMATIONS, DEFAULT_FORMATION,
    PUFO_PERCENT, MARKET_SIZE, MARKET_STARS, CONTRACT_COMPENSATION, CAPTAIN_BONUS,
    SYNC_REQUIRE_ACTIVE_CLUB,
    ECON_INCOME_PER_POINT, ECON_GOAL_BONUS, ECON_RED_PENALTY,
    ECON_MVP_MATCHDAY_BONUS, ECON_CLASSIFICATION_LOW, ECON_CLASSIFICATION_HIGH,
)

import unicodedata
from functools import lru_cache


# ---------- Imágenes (fotos de jugador y escudos de club) ----------
def _norm_nombre(s: Optional[str]) -> str:
    """Normaliza un nombre para comparación (minúsculas, sin acentos)."""
    s = s or ""
    s = unicodedata.normalize("NFD", s)
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    return s.lower().strip()


@lru_cache(maxsize=1)
def _image_maps() -> tuple[dict, dict, dict]:
    """Devuelve (foto_por_jugador, escudo_por_equipo, nombre_norm->equipo_id).

    Lee la BD de SofaScore una sola vez para poder adjuntar a cada jugador su
    foto y el escudo de su club. `club_id` puede estar vacío en la BD del juego,
    por eso el escudo se resuelve por nombre de club. Si falta algo, devuelve
    mapas vacíos (la UI muestra un fallback).
    """
    fotos: dict = {}
    escudos: dict = {}
    nombre2equipo: dict = {}
    try:
        con = sqlite3.connect(str(SOFASCORE_DB_PATH), check_same_thread=False)
        con.row_factory = sqlite3.Row
        for r in con.execute("SELECT jugador_id, ruta FROM imagenes_jugadores"):
            fotos[r["jugador_id"]] = r["ruta"].replace("\\", "/")
        for r in con.execute("SELECT equipo_id, ruta FROM imagenes_equipos"):
            escudos[r["equipo_id"]] = r["ruta"].replace("\\", "/")
        for r in con.execute("SELECT id, nombre, nombre_corto FROM equipos"):
            for nm in (r["nombre"], r["nombre_corto"]):
                if nm:
                    nombre2equipo[_norm_nombre(nm)] = r["id"]
        con.close()
    except Exception:
        pass
    return fotos, escudos, nombre2equipo


def _con_imagenes(rows: list[dict]) -> list[dict]:
    """Añade a cada jugador (dict) `photo_url` y `club_crest_url`.

    Lee las rutas de imágenes desde la BD local de SofaScore.  Si la tabla
    `imagenes_jugadores` no existe o no tiene datos para un jugador,
    construye una ruta fallback `/sofa/imagenes/jugadores/<id>.webp` que el
    backend resuelve pidiendo al CDN externo de SoffaScore.
    """
    fotos, escudos, nombre2equipo = _image_maps()
    for row in rows:
        pid = row.get("player_id", row.get("id"))
        ruta_foto = fotos.get(pid) if pid is not None else None
        if ruta_foto:
            row["photo_url"] = f"/sofa/{ruta_foto}"
        elif pid is not None:
            # Fallback: el ID de jugador coincide con el ID de SoffaScore
            row["photo_url"] = f"/sofa/imagenes/jugadores/{pid}.webp"
        else:
            row["photo_url"] = None
        club = row.get("club_name")
        equipo_id = nombre2equipo.get(_norm_nombre(club)) if club else None
        ruta_escudo = escudos.get(equipo_id) if equipo_id else None
        if ruta_escudo:
            row["club_crest_url"] = f"/sofa/{ruta_escudo}"
        elif equipo_id is not None:
            row["club_crest_url"] = f"/sofa/imagenes/equipos/{equipo_id}.png"
        elif row.get("club_id") is not None:
            row["club_crest_url"] = f"/sofa/imagenes/equipos/{row['club_id']}.png"
        else:
            row["club_crest_url"] = None
    return rows


# ---------- Auth ----------
def register_user(username: str, email: str, password: str) -> dict:
    with transaction() as conn:
        try:
            cur = conn.execute(
                "INSERT INTO users (username, email, password_hash) VALUES (?, ?, ?)",
                (username, email, hash_password(password))
            )
            user_id = cur.lastrowid
        except sqlite3.IntegrityError as e:
            raise ValueError("Username or email already exists") from e
    return {"id": user_id, "username": username, "email": email}


def authenticate_user(username: str, password: str) -> Optional[dict]:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT id, username, email, password_hash FROM users WHERE username = ?",
            (username,)
        ).fetchone()
    if row and verify_password(password, row["password_hash"]):
        # Actualiza hashes antiguos (sha256 sin sal) al formato actual
        if needs_rehash(row["password_hash"]):
            with transaction() as conn:
                conn.execute(
                    "UPDATE users SET password_hash = ? WHERE id = ?",
                    (hash_password(password), row["id"])
                )
        return {"id": row["id"], "username": row["username"], "email": row["email"]}
    return None


def update_user_email(user_id: int, new_email: str) -> None:
    """Cambia el correo de la cuenta (Pantalla 06: Configuración)."""
    with get_conn() as conn:
        existing = conn.execute(
            "SELECT id FROM users WHERE email = ? AND id != ?", (new_email, user_id)
        ).fetchone()
    if existing:
        raise ValueError("Ese correo ya está en uso")
    with transaction() as conn:
        conn.execute("UPDATE users SET email = ? WHERE id = ?", (new_email, user_id))


def update_user_password(user_id: int, new_password: str) -> None:
    """Cambia la contraseña de la cuenta (Pantalla 06: Configuración)."""
    if len(new_password) < 4:
        raise ValueError("La contraseña nueva es demasiado corta (mínimo 4)")
    with transaction() as conn:
        conn.execute(
            "UPDATE users SET password_hash = ? WHERE id = ?",
            (hash_password(new_password), user_id),
        )


# ---------- Recuperación de contraseña (enlace por correo) ----------
RESET_TOKEN_TTL_MINUTES = 30
RESET_TOKEN_COOLDOWN_SECONDS = 60


def _hash_reset_token(token: str) -> str:
    """El enlace viaja por correo: solo se guarda su hash SHA-256 en la BD."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _utcnow() -> dt_datetime:
    """Naive UTC para comparar fechas de forma consistente con la BD."""
    return dt_datetime.now(dt_timezone.utc).replace(tzinfo=None)


def create_password_reset_token(email: str) -> Optional[tuple[str, str]]:
    """Genera un token de recuperación (30 min) para la cuenta con ese correo.

    Invalida los tokens pendientes previos de ese usuario. Devuelve
    (token, username) o None si no existe usuario con ese correo.
    Lanza ValueError si la última petición fue hace menos de 60 s (anti-abuso).
    """
    email_norm = (email or "").strip().lower()
    with get_conn() as conn:
        row = conn.execute(
            "SELECT id, username FROM users WHERE email = ?", (email_norm,)
        ).fetchone()
    if not row:
        return None
    user_id, username = row["id"], row["username"]

    with get_conn() as conn:
        last = conn.execute(
            "SELECT created_at FROM password_reset_tokens "
            "WHERE user_id = ? AND used_at IS NULL ORDER BY id DESC LIMIT 1",
            (user_id,),
        ).fetchone()
    if last:
        last_created = dt_datetime.fromisoformat(last["created_at"])
        if _utcnow() - last_created < timedelta(seconds=RESET_TOKEN_COOLDOWN_SECONDS):
            raise ValueError(
                "Para no abusar del correo, espera un minuto antes de pedir otro enlace."
            )

    token = secrets.token_urlsafe(32)
    now = _utcnow()
    expires = now + timedelta(minutes=RESET_TOKEN_TTL_MINUTES)
    with transaction() as conn:
        # Un solo token pendiente por usuario: los anteriores quedan invalidados
        conn.execute(
            "UPDATE password_reset_tokens SET used_at = ? "
            "WHERE user_id = ? AND used_at IS NULL",
            (now.isoformat(timespec="seconds"), user_id),
        )
        conn.execute(
            "INSERT INTO password_reset_tokens "
            "(user_id, token_hash, email, created_at, expires_at) VALUES (?, ?, ?, ?, ?)",
            (
                user_id,
                _hash_reset_token(token),
                email_norm,
                now.isoformat(timespec="seconds"),
                expires.isoformat(timespec="seconds"),
            ),
        )
    return token, username


def apply_password_reset(token: str, new_password: str) -> str:
    """Cambia la contraseña con un token válido y lo marca como usado.

    Devuelve el username. Lanza ValueError si el token no existe, está
    caducado, ya fue usado, o la contraseña es demasiado corta.
    """
    if len(new_password) < 4:
        raise ValueError("La contraseña nueva es demasiado corta (mínimo 4)")
    token_hash = _hash_reset_token(token)
    with get_conn() as conn:
        row = conn.execute(
            "SELECT id, user_id, expires_at, used_at "
            "FROM password_reset_tokens WHERE token_hash = ?",
            (token_hash,),
        ).fetchone()
    if not row:
        raise ValueError("Enlace de recuperación no válido.")
    if row["used_at"] is not None or row["expires_at"] < _utcnow().isoformat(timespec="seconds"):
        raise ValueError("Este enlace de recuperación ya no es válido (caducado o ya usado).")

    with transaction() as conn:
        conn.execute(
            "UPDATE users SET password_hash = ? WHERE id = ?",
            (hash_password(new_password), row["user_id"]),
        )
        conn.execute(
            "UPDATE password_reset_tokens SET used_at = ? WHERE id = ?",
            (_utcnow().isoformat(timespec="seconds"), row["id"]),
        )
    with get_conn() as conn:
        user = conn.execute(
            "SELECT username FROM users WHERE id = ?", (row["user_id"],)
        ).fetchone()
    return user["username"]


def get_user_context(user_id: int) -> dict:
    """Devuelve el usuario junto a su liga activa (user_league_id, league_id, team_name)."""
    with get_conn() as conn:
        user = conn.execute(
            "SELECT id, username, email, avatar_url, created_at FROM users WHERE id = ?", (user_id,)
        ).fetchone()
        if not user:
            return {}
        ul = conn.execute(
            """
            SELECT ul.id AS user_league_id, ul.league_id, ul.team_name, ul.budget,
                   l.name AS league_name, l.current_matchday, l.invite_code
            FROM user_leagues ul
            JOIN leagues l ON l.id = ul.league_id
            WHERE ul.user_id = ?
            ORDER BY ul.id ASC
            LIMIT 1
            """,
            (user_id,)
        ).fetchone()
        leagues_count = conn.execute(
            "SELECT COUNT(*) AS c FROM user_leagues WHERE user_id = ?", (user_id,)
        ).fetchone()["c"]
    context = dict(user)
    context["leagues_count"] = leagues_count
    if ul:
        context.update(dict(ul))
    return context


# ---------- Leagues ----------
def create_league(
    owner_id: int,
    name: str,
    season_start: Optional[str] = None,
    team_name: Optional[str] = None,
) -> dict:
    """Crea una liga, mete al creador y le genera su plantilla inicial."""
    invite = generate_invite_code()
    owner_team = (team_name or "").strip() or f"{username_of(owner_id)} FC"
    with transaction() as conn:
        cur = conn.execute(
            "INSERT INTO leagues (name, owner_id, invite_code, season_start_date) VALUES (?, ?, ?, ?)",
            (name, owner_id, invite, season_start)
        )
        league_id = cur.lastrowid
        # Owner joins automatically
        conn.execute(
            "INSERT INTO user_leagues (user_id, league_id, team_name, budget) VALUES (?, ?, ?, ?)",
            (owner_id, league_id, owner_team, INITIAL_BUDGET)
        )
        ul_id = conn.execute("SELECT last_insert_rowid() as id").fetchone()["id"]
        # Generate initial balanced squad for owner
        generate_initial_squad(ul_id, league_id, conn)
    return {
        "id": league_id,
        "name": name,
        "invite_code": invite,
        "owner_id": owner_id,
        "user_league_id": ul_id,
        "team_name": owner_team,
    }


def join_league(user_id: int, invite_code: str, team_name: str) -> dict:
    code = (invite_code or "").strip().upper()
    with transaction() as conn:
        league = conn.execute(
            "SELECT id FROM leagues WHERE invite_code = ? AND status = 'active'", (code,)
        ).fetchone()
        if not league:
            raise ValueError("Invalid or expired invite code")
        league_id = league["id"]
        # Check if already in league
        existing = conn.execute(
            "SELECT id FROM user_leagues WHERE user_id = ? AND league_id = ?", (user_id, league_id)
        ).fetchone()
        if existing:
            raise ValueError("Already in this league")
        conn.execute(
            "INSERT INTO user_leagues (user_id, league_id, team_name, budget) VALUES (?, ?, ?, ?)",
            (user_id, league_id, team_name, INITIAL_BUDGET)
        )
        ul_id = conn.execute(
            "SELECT last_insert_rowid() as id"
        ).fetchone()["id"]
        # Generate initial balanced squad
        generate_initial_squad(ul_id, league_id, conn)
    return {"user_league_id": ul_id, "league_id": league_id, "team_name": team_name}


def username_of(user_id: int) -> str:
    with get_conn() as conn:
        row = conn.execute("SELECT username FROM users WHERE id = ?", (user_id,)).fetchone()
    return row["username"] if row else "User"


def _ensure_players_synced(conn: sqlite3.Connection) -> None:
    """Garantiza que la tabla players tiene jugadores; si no, la sincroniza desde la BD SofaScore."""
    count = conn.execute("SELECT COUNT(*) AS c FROM players WHERE market_value > 0").fetchone()["c"]
    if not count:
        sync_players_from_sofascore_db(conn)


def generate_initial_squad(user_league_id: int, league_id: int, conn: sqlite3.Connection) -> None:
    """
    Plantilla inicial equilibrada por posiciones (Canon §2):
    - Mismo número de POR/DEF/MED/DEL para todos.
    - Sin los 20 jugadores más caros (deben ficharse en el mercado).
    - Un jugador solo puede pertenecer a un usuario de la liga a la vez (Canon §1).
    """
    _ensure_players_synced(conn)

    players = conn.execute(
        "SELECT id, position, market_value FROM players WHERE market_value > 0 "
        "ORDER BY market_value DESC, id ASC"
    ).fetchall()

    # Jugadores que ya pertenecen a OTRO usuario de esta misma liga (Canon §1)
    taken = {
        r["player_id"]
        for r in conn.execute(
            "SELECT player_id FROM squad_players WHERE user_league_id IN "
            "(SELECT id FROM user_leagues WHERE league_id = ?)",
            (league_id,),
        )
    }
    # Jugadores que ya tiene este usuario (por si se regenera la plantilla)
    own = {
        r["player_id"]
        for r in conn.execute(
            "SELECT player_id FROM squad_players WHERE user_league_id = ?", (user_league_id,)
        )
    }

    # Top 20 por valor: fuera de la plantilla inicial, disponibles en el mercado (Canon §2)
    top_ids = {p["id"] for p in players[:TOP_20_EXCLUDE]}

    available = [
        p for p in players
        if p["id"] not in top_ids and p["id"] not in taken and p["id"] not in own
    ]

    by_pos: dict[str, list] = {"POR": [], "DEF": [], "MED": [], "DEL": []}
    for p in available:
        if p["position"] in by_pos:
            by_pos[p["position"]].append(p)

    targets = {"POR": 3, "DEF": 7, "MED": 7, "DEL": 8}  # 25 (Canon §2)
    needed = MAX_SQUAD_SIZE - len(own)
    selected: list[int] = []

    for pos, count in targets.items():
        pool = by_pos.get(pos, [])
        random.shuffle(pool)
        for p in pool[:count]:
            if len(selected) >= needed:
                break
            selected.append(p["id"])
        if len(selected) >= needed:
            break

    # Si falta algún hueco, se rellena con lo que quede libre
    if len(selected) < needed:
        remaining = [p["id"] for p in available if p["id"] not in selected]
        random.shuffle(remaining)
        selected.extend(remaining[: needed - len(selected)])

    for pid in selected[:needed]:
        conn.execute(
            "INSERT INTO squad_players (user_league_id, player_id, contract_weeks) VALUES (?, ?, ?)",
            (user_league_id, pid, CONTRACT_WEEKS),
        )

    # Alineación por defecto de la jornada 1 (solo si no existe ya)
    existing_ms = conn.execute(
        "SELECT id FROM matchday_squad WHERE user_league_id = ? AND matchday = 1",
        (user_league_id,),
    ).fetchone()
    if not existing_ms:
        create_default_matchday_squad(user_league_id, 1, conn)


def create_default_matchday_squad(user_league_id: int, matchday: int, conn: sqlite3.Connection) -> int:
    """Create a default matchday squad with best players in formation."""
    formation = FORMATIONS[DEFAULT_FORMATION]
    # Get squad players with their positions
    squad = conn.execute(
        """
        SELECT sp.player_id, p.position, p.market_value
        FROM squad_players sp
        JOIN players p ON p.id = sp.player_id
        WHERE sp.user_league_id = ?
        ORDER BY p.market_value DESC
        """,
        (user_league_id,)
    ).fetchall()

    # Assign to formation slots
    slots = []
    slot_order = 0
    for pos, count in formation.items():
        for i in range(count):
            slots.append((f"{pos}{i+1}", pos))
            slot_order += 1

    cur = conn.execute(
        "INSERT INTO matchday_squad (user_league_id, matchday, formation) VALUES (?, ?, ?)",
        (user_league_id, matchday, DEFAULT_FORMATION)
    )
    ms_id = cur.lastrowid

    assigned = set()
    for slot_name, pos in slots:
        # Find best available player of this position
        for p in squad:
            if p["player_id"] not in assigned and p["position"] == pos:
                conn.execute(
                    "INSERT INTO matchday_lineup (matchday_squad_id, player_id, position_slot, is_starter, slot_order) VALUES (?, ?, ?, 1, ?)",
                    (ms_id, p["player_id"], slot_name, slot_order)
                )
                assigned.add(p["player_id"])
                break

    # Fill bench with remaining (up to CONVOCATORIA_SIZE - 11 = 7)
    bench_players = [p for p in squad if p["player_id"] not in assigned]
    bench_order = 11
    for p in bench_players[:7]:
        conn.execute(
            "INSERT INTO matchday_lineup (matchday_squad_id, player_id, position_slot, is_starter, slot_order) VALUES (?, ?, ?, 0, ?)",
            (ms_id, p["player_id"], "BENCH", bench_order)
        )
        bench_order += 1

    # Set captain as highest value starter
    starter = conn.execute(
        "SELECT player_id FROM matchday_lineup WHERE matchday_squad_id = ? AND is_starter = 1 ORDER BY slot_order LIMIT 1",
        (ms_id,)
    ).fetchone()
    if starter:
        conn.execute(
            "UPDATE matchday_squad SET captain_player_id = ? WHERE id = ?",
            (starter["player_id"], ms_id)
        )

    return ms_id


# ---------- Player sync ----------
from functools import lru_cache


@lru_cache(maxsize=1)
def _read_players_from_sofascore_db() -> tuple[dict, ...]:
    """
    Lee (y cachea) los jugadores de la temporada activa desde la BD de SofaScore.

    La BD de SofaScore cambia poco, así que se cachea para no re-escanearla en
    cada creación de liga. Usa `refresh_players_cache()` para invalidar.
    """
    return tuple(_squad_score_players())


def refresh_players_cache() -> None:
    """Invalida la caché de jugadores (llamar tras re-extraer datos de SofaScore)."""
    _read_players_from_sofascore_db.cache_clear()


def _squad_score_players() -> list[dict]:
    """Lee los jugadores de la temporada activa desde la BD de SofaScore."""
    import json as _json

    try:
        from configuracion import TEMPORADA_ID
    except Exception:  # pragma: no cover - configuración opcional
        TEMPORADA_ID = None

    if not SOFASCORE_DB_PATH.exists():
        return []

    src = sqlite3.connect(str(SOFASCORE_DB_PATH))
    src.row_factory = sqlite3.Row
    try:
        # Club de cada jugador = equipo con el que aparece más veces en las estadísticas
        club_by_player: dict[int, str] = {}
        for row in src.execute(
            """
            SELECT jugador_id,
                   CASE WHEN localizacion = 'home' THEN equipo_local_nombre
                        ELSE equipo_visitante_nombre END AS club,
                   COUNT(*) AS n
            FROM estadisticas_jugador_partido
            WHERE club IS NOT NULL
            GROUP BY jugador_id, club
            """
        ):
            jid, club, n = row["jugador_id"], row["club"], row["n"]
            if club and (jid not in club_by_player or n > club_by_player[jid][1]):
                club_by_player[jid] = (club, n)

        active_clubs: set[str] = set()
        if TEMPORADA_ID is not None:
            active_clubs = {
                r["equipo_nombre"]
                for r in src.execute(
                    "SELECT DISTINCT equipo_nombre FROM clasificacion WHERE temporada_id = ?",
                    (TEMPORADA_ID,),
                )
                if r["equipo_nombre"]
            }

        result: list[dict] = []
        for row in src.execute("SELECT id, nombre, nombre_corto, equipo_nombre, raw FROM jugadores"):
            try:
                raw = _json.loads(row["raw"]) if row["raw"] else {}
            except (TypeError, ValueError):
                raw = {}

            venue = club_by_player.get(row["id"], (None, 0))[0]
            if not venue:
                venue = row["equipo_nombre"] or (raw.get("team") or {}).get("name")

            # Solo jugadores de clubes de la temporada activa (si hay datos de clasificación)
            if SYNC_REQUIRE_ACTIVE_CLUB and active_clubs and venue not in active_clubs:
                continue

            position = map_position(raw.get("position"))
            market_value = (raw.get("proposedMarketValueRaw") or {}).get("value") or 0
            if not market_value:
                market_value = {"POR": 2_000_000, "DEF": 3_000_000, "MED": 4_000_000, "DEL": 5_000_000}[position]

            result.append(
                {
                    "id": int(row["id"]),
                    "name": row["nombre"],
                    "short_name": row["nombre_corto"],
                    "position": position,
                    "role": raw.get("position"),
                    "club_name": venue,
                    "market_value": int(market_value),
                }
            )
        return result
    finally:
        src.close()


def sync_players_from_sofascore_db(conn: Optional[sqlite3.Connection] = None, use_cache: bool = True) -> int:
    """
    Sincroniza la tabla `players` del juego desde la BD local de SofaScore.

    No necesita que la API esté levantada (a diferencia del sync por HTTP), por lo
    que crear una liga siempre deja jugadores disponibles.
    """
    players = _read_players_from_sofascore_db() if use_cache else tuple(_squad_score_players())
    if not players:
        return 0

    rows = [
        (
            p["id"], p["name"], p["short_name"], p["position"], p["role"],
            p["club_name"], p["market_value"], now_iso(),
        )
        for p in players
    ]

    def _write(connection: sqlite3.Connection) -> int:
        connection.executemany(
            """
            INSERT INTO players (id, name, short_name, position, role, club_name, market_value, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                name = excluded.name,
                short_name = excluded.short_name,
                position = excluded.position,
                role = excluded.role,
                club_name = excluded.club_name,
                market_value = excluded.market_value,
                updated_at = excluded.updated_at
            """,
            rows,
        )
        return len(rows)

    if conn is not None:
        return _write(conn)
    with transaction() as connection:
        return _write(connection)


async def sync_players_from_sofascore() -> int:
    """Fetch players from SofaScore API and cache in fantasy DB.

    Intenta primero la API interna (configurada en SOFASCORE_API). Si falla
    o no está configurada, reintenta con el CDN externo de SoffaScore usando
    curl_cffi (TLS spoofing) para evitar bloqueos 403. Si ambos fallan
    silenciosamente, devuelve 0.
    """
    players = []

    # 1) Internal API (solo si está configurada — NO disponible en PythonAnywhere)
    if SOFASCORE_API:
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                resp = await client.get(f"{SOFASCORE_API}/api/players")
                resp.raise_for_status()
                data = resp.json()
                players = data.get("players", [])
        except Exception:
            pass

    # 2) External CDN fallback (curl_cffi for TLS spoofing)
    if not players:
        try:
            async with CurlAsyncSession(impersonate="chrome") as client:
                resp = await client.get(
                    f"{SOFASCORE_CDN}/players",
                    headers={
                        "Accept": "application/json, text/plain, */*",
                        "Referer": "https://www.sofascore.com/",
                        "Origin": "https://www.sofascore.com",
                    },
                    timeout=10,
                )
                if resp.status_code == 200:
                    data = resp.json()
                    players = data.get("content", data.get("items", data.get("players", [])))
        except Exception:
            pass

    if not players:
        return 0

    count = 0
    with transaction() as conn:
        for p in players:
            pos = map_position(p.get("raw", {}).get("position"))
            conn.execute(
                """
                INSERT OR REPLACE INTO players (id, name, short_name, position, role, club_id, club_name, market_value, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    p["id"], p["name"], p.get("short_name"), pos,
                    p.get("raw", {}).get("role"), p.get("team_id"), p.get("team"),
                    estimate_market_value(p), now_iso()
                )
            )
            count += 1
    return count


def map_position(sofa_pos: str) -> str:
    """
    Traduce la posición de SofaScore a POR/DEF/MED/DEL.

    SofaScore usa una sola letra: G (portero), D (defensa), M (medio), F (delantero).
    También se aceptan los códigos de varias letras por si cambia la fuente.
    """
    if not sofa_pos:
        return "MED"
    pos = sofa_pos.upper().strip()

    if pos in ("G", "GK", "POR", "GOALKEEPER", "GOALIE"):
        return "POR"
    if pos in ("D", "CB", "LB", "RB", "LWB", "RWB", "DEF", "CENTRAL", "LATERAL"):
        return "DEF"
    if pos in ("M", "CM", "CDM", "CAM", "LM", "RM", "DM", "AM", "MED",
               "MEDIODEFENSIVO", "MEDIOOFENSIVO"):
        return "MED"
    # Canon §2: los extremos cuentan como delanteros
    if pos in ("F", "ST", "CF", "LF", "RF", "DEL", "DELANTEROCENTRO", "EXTREMO", "W", "LW", "RW"):
        return "DEL"
    return "MED"


def estimate_market_value(player_data: dict) -> int:
    """Estimate market value based on rating, age, position."""
    rating = player_data.get("rating", 6.0)
    matches = player_data.get("matches", 0)
    pos = map_position(player_data.get("raw", {}).get("position", ""))
    base = {"POR": 2_000_000, "DEF": 3_000_000, "MED": 4_000_000, "DEL": 5_000_000}[pos]
    # Simple formula: base * (rating/6.5) * (1 + matches/50)
    return int(base * (rating / 6.5) * (1 + matches / 50))


# ---------- Squad CRUD ----------
def get_squad(user_league_id: int) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT sp.*, p.name, p.short_name, p.position, p.role, p.club_id, p.club_name, p.market_value
            FROM squad_players sp
            JOIN players p ON p.id = sp.player_id
            WHERE sp.user_league_id = ?
            ORDER BY 
                CASE p.position WHEN 'POR' THEN 1 WHEN 'DEF' THEN 2 WHEN 'MED' THEN 3 WHEN 'DEL' THEN 4 END,
                p.market_value DESC
            """,
            (user_league_id,)
        ).fetchall()
    return _con_imagenes([dict(r) for r in rows])


def get_matchday_squad(user_league_id: int, matchday: int) -> Optional[dict]:
    with get_conn() as conn:
        ms = conn.execute(
            "SELECT * FROM matchday_squad WHERE user_league_id = ? AND matchday = ?",
            (user_league_id, matchday)
        ).fetchone()
        if not ms:
            return None
        lineup = conn.execute(
            """
            SELECT ml.*, p.name, p.short_name, p.position, p.role, p.club_id, p.club_name, p.market_value,
                   sp.contract_weeks
            FROM matchday_lineup ml
            JOIN players p ON p.id = ml.player_id
            LEFT JOIN squad_players sp
                ON sp.player_id = ml.player_id AND sp.user_league_id = ?
            WHERE ml.matchday_squad_id = ?
            ORDER BY ml.slot_order
            """,
            (user_league_id, ms["id"]),
        ).fetchall()
    # Canon §3/§9: estado breve por jugador. Un titular de nuestro once cuyo
    # equipo ya ha confirmado alineaciones y está en ellas es "titular
    # confirmado"; el resto de titulares, "titular posible".
    confirmados = confirmados_jornada(matchday)
    rows = [dict(r) for r in lineup]
    for r in rows:
        r["titular_confirmado"] = 1 if (r["is_starter"] and r["player_id"] in confirmados) else 0
    return {
        **dict(ms),
        "lineup": _con_imagenes(rows)
    }


def update_matchday_squad(user_league_id: int, matchday: int, formation: str, lineup: list[dict], captain_id: Optional[int]) -> dict:
    """lineup = [{"player_id": int, "position_slot": str, "is_starter": bool, "slot_order": int}, ...]"""
    with transaction() as conn:
        # Upsert matchday_squad
        existing = conn.execute(
            "SELECT id FROM matchday_squad WHERE user_league_id = ? AND matchday = ?",
            (user_league_id, matchday)
        ).fetchone()
        if existing:
            ms_id = existing["id"]
            conn.execute(
                "UPDATE matchday_squad SET formation = ?, captain_player_id = ?, updated_at = ? WHERE id = ?",
                (formation, captain_id, now_iso(), ms_id)
            )
            # Clear old lineup
            conn.execute("DELETE FROM matchday_lineup WHERE matchday_squad_id = ?", (ms_id,))
        else:
            cur = conn.execute(
                "INSERT INTO matchday_squad (user_league_id, matchday, formation, captain_player_id) VALUES (?, ?, ?, ?)",
                (user_league_id, matchday, formation, captain_id)
            )
            ms_id = cur.lastrowid

        # Insert new lineup
        for item in lineup:
            conn.execute(
                "INSERT INTO matchday_lineup (matchday_squad_id, player_id, position_slot, is_starter, slot_order) VALUES (?, ?, ?, ?, ?)",
                (ms_id, item["player_id"], item["position_slot"], 1 if item.get("is_starter") else 0, item.get("slot_order", 0))
            )
    return {"success": True, "matchday_squad_id": ms_id}


# ---------- Market ----------
async def get_today_market(league_id: int) -> list[dict]:
    today = dt_date.today().isoformat()
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT mp.*, p.name, p.short_name, p.position, p.role, p.club_id, p.club_name, p.market_value
            FROM market_players mp
            JOIN players p ON p.id = mp.player_id
            WHERE mp.league_id = ? AND mp.listed_date = ? AND mp.status = 'active'
            ORDER BY p.market_value DESC
            """,
            (league_id, today)
        ).fetchall()
    return _con_imagenes([dict(r) for r in rows])


async def refresh_market(league_id: int) -> list[dict]:
    """
    Cambio de mercado diario (Canon §5):
    1. Resuelve las subastas de ayer: gana la puja más alta (empate: la más antigua).
    2. NO se devuelve dinero a los perdedores: nunca se les cobró la puja.
    3. Marca las pujas como resueltas para que no sigan contando como comprometido.
    4. Publica 20 jugadores nuevos (ninguno repetido del día anterior).
    """
    today = dt_date.today().isoformat()
    with transaction() as conn:
        yesterday = (dt_date.today() - timedelta(days=1)).isoformat()
        market_size = _market_size_for(league_id, conn)

        # --- 1) Resolver las subastas pendientes de ayer ---
        pending = conn.execute(
            """
            SELECT id, player_id FROM market_players
            WHERE league_id = ? AND listed_date = ? AND status = 'active'
            """,
            (league_id, yesterday),
        ).fetchall()

        for row in pending:
            mp_id = row["id"]
            highest = conn.execute(
                "SELECT * FROM bids WHERE market_player_id = ? "
                "ORDER BY amount DESC, created_at ASC LIMIT 1",
                (mp_id,),
            ).fetchone()

            if not highest:
                conn.execute("UPDATE market_players SET status = 'expired' WHERE id = ?", (mp_id,))
                continue

            winner_id = highest["user_league_id"]
            amount = highest["amount"]

            # Si el ganador ya tiene la plantilla llena, el fichaje no se cierra
            squad_size = conn.execute(
                "SELECT COUNT(*) AS c FROM squad_players WHERE user_league_id = ?", (winner_id,)
            ).fetchone()["c"]

            if squad_size >= MAX_SQUAD_SIZE:
                conn.execute("UPDATE market_players SET status = 'expired' WHERE id = ?", (mp_id,))
                conn.execute(
                    "INSERT INTO transactions (user_league_id, type, amount, related_player_id, description) "
                    "VALUES (?, 'purchase_failed', 0, ?, ?)",
                    (winner_id, row["player_id"], "Plantilla completa: fichaje no realizado"),
                )
            else:
                conn.execute(
                    "INSERT INTO squad_players (user_league_id, player_id, contract_weeks, purchase_price) "
                    "VALUES (?, ?, ?, ?)",
                    (winner_id, row["player_id"], CONTRACT_WEEKS, amount),
                )
                conn.execute(
                    "UPDATE user_leagues SET budget = budget - ? WHERE id = ?", (amount, winner_id)
                )
                conn.execute(
                    "INSERT INTO transactions (user_league_id, type, amount, related_player_id, description) "
                    "VALUES (?, 'purchase', ?, ?, ?)",
                    (winner_id, -amount, row["player_id"], f"Market purchase for {amount}€"),
                )
                conn.execute("UPDATE bids SET is_winning = 0 WHERE market_player_id = ?", (mp_id,))
                conn.execute("UPDATE bids SET is_winning = 1 WHERE id = ?", (highest["id"],))
                conn.execute("UPDATE market_players SET status = 'sold' WHERE id = ?", (mp_id,))

            # Las pujas resueltas dejan de contar como dinero comprometido
            conn.execute("UPDATE bids SET resolved = 1 WHERE market_player_id = ?", (mp_id,))

        # Pujas huérfanas (de mercados ya cerrados) tampoco deben contar
        conn.execute(
            """
            UPDATE bids SET resolved = 1
            WHERE resolved = 0 AND market_player_id IN (
                SELECT id FROM market_players WHERE league_id = ? AND status != 'active'
            )
            """,
            (league_id,),
        )

        # --- 2) Cerrar el mercado de hoy antes de publicar el nuevo ---
        conn.execute(
            "UPDATE market_players SET status = 'expired' "
            "WHERE league_id = ? AND listed_date = ? AND status = 'active'",
            (league_id, today),
        )

        # --- 3) Seleccionar 20 jugadores nuevos ---
        owned_ids = {
            r["player_id"]
            for r in conn.execute(
                "SELECT player_id FROM squad_players WHERE user_league_id IN "
                "(SELECT id FROM user_leagues WHERE league_id = ?)",
                (league_id,),
            )
        }
        recent_ids = {
            r["player_id"]
            for r in conn.execute(
                "SELECT player_id FROM market_players WHERE league_id = ? AND listed_date IN (?, ?)",
                (league_id, today, yesterday),
            )
        }

        excluded = owned_ids | recent_ids

        # Se filtra en Python (y no con un NOT IN gigante) para evitar límites de
        # parámetros SQLite con ligas grandes.
        candidates = conn.execute(
            """
            SELECT id, market_value FROM players
            WHERE market_value > 0
            ORDER BY market_value DESC, id ASC
            LIMIT 400
            """
        ).fetchall()
        candidates = [c for c in candidates if c["id"] not in excluded]

        # Los cracks SÍ pueden salir en el mercado (Canon §2): unos pocos fijos
        # entre los más caros y el resto al azar, para dar variedad.
        stars = [c["id"] for c in candidates[:MARKET_STARS]]
        rest = [c["id"] for c in candidates[MARKET_STARS:]]
        random.shuffle(rest)
        selected = stars + rest[: max(0, market_size - len(stars))]

        expires = (dt_datetime.now() + timedelta(days=1)).replace(
            hour=5, minute=0, second=0, microsecond=0
        ).isoformat()
        for pid in selected:
            conn.execute(
                "INSERT INTO market_players (league_id, player_id, listed_date, expires_at) "
                "VALUES (?, ?, ?, ?)",
                (league_id, pid, today, expires),
            )
    return await get_today_market(league_id)


def _committed_bids(conn: sqlite3.Connection, user_league_id: int) -> int:
    """Dinero comprometido en pujas AÚN VIVAS (las resueltas no cuentan)."""
    row = conn.execute(
        """
        SELECT COALESCE(SUM(b.amount), 0) AS total
        FROM bids b
        JOIN market_players mp ON mp.id = b.market_player_id
        WHERE b.user_league_id = ? AND b.resolved = 0 AND mp.status = 'active'
        """,
        (user_league_id,),
    ).fetchone()
    return int(row["total"] or 0)


def place_bid(market_player_id: int, user_league_id: int, amount: int) -> dict:
    with transaction() as conn:
        mp = conn.execute(
            "SELECT mp.*, p.market_value FROM market_players mp "
            "JOIN players p ON p.id = mp.player_id "
            "WHERE mp.id = ? AND mp.status = 'active'",
            (market_player_id,),
        ).fetchone()
        if not mp:
            raise ValueError("Player not on market")

        if amount < mp["market_value"]:
            raise ValueError(f"Bid must be at least market value ({mp['market_value']})")

        # Disponible = saldo + pufo (20% del valor de plantilla) - pujas vivas
        info = get_budget_info(user_league_id, conn=conn)
        if amount > info["disponible_para_pujas"]:
            raise ValueError(f"Insufficient funds. Available for bids: {info['disponible_para_pujas']}")

        # Una sola puja por jugador y usuario: se actualiza la existente
        existing = conn.execute(
            "SELECT id FROM bids WHERE market_player_id = ? AND user_league_id = ?",
            (market_player_id, user_league_id),
        ).fetchone()
        if existing:
            conn.execute("UPDATE bids SET amount = ? WHERE id = ?", (amount, existing["id"]))
        else:
            conn.execute(
                "INSERT INTO bids (market_player_id, user_league_id, amount) VALUES (?, ?, ?)",
                (market_player_id, user_league_id, amount),
            )
    return {"success": True}


def get_my_bids(user_league_id: int) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT b.*, mp.player_id, mp.listed_date, mp.expires_at,
                   p.name, p.short_name, p.position, p.club_name, p.market_value
            FROM bids b
            JOIN market_players mp ON mp.id = b.market_player_id
            JOIN players p ON p.id = mp.player_id
            WHERE b.user_league_id = ? AND mp.status = 'active'
            ORDER BY mp.expires_at ASC
            """,
            (user_league_id,)
        ).fetchall()
    return _con_imagenes([dict(r) for r in rows])


def cancel_bid(bid_id: int, user_league_id: int) -> dict:
    """Retira una puja aún viva (resolved = 0). Libera el dinero comprometido."""
    with transaction() as conn:
        row = conn.execute(
            "SELECT id FROM bids WHERE id = ? AND user_league_id = ? AND resolved = 0",
            (bid_id, user_league_id),
        ).fetchone()
        if not row:
            raise ValueError("No puedes cancelar esa puja")
        conn.execute("DELETE FROM bids WHERE id = ?", (bid_id,))
        conn.execute(
            "UPDATE user_leagues SET last_updated_at = ? WHERE id = ?",
            (now_iso(), user_league_id),
        )
        return {"success": True}


# ---------- User Offers ----------
def create_user_offer(from_id: int, to_id: int, target_player_id: int, cash: int, player_ids: list[int]) -> dict:
    """Oferta privada por un jugador (Canon §6): dinero y/o jugadores."""
    if from_id == to_id:
        raise ValueError("You cannot make an offer to yourself")
    if cash < 0:
        raise ValueError("Cash cannot be negative")

    with transaction() as conn:
        # Validar: el jugador pretendido es del usuario destino
        owner = conn.execute(
            "SELECT user_league_id FROM squad_players WHERE player_id = ?", (target_player_id,)
        ).fetchone()
        if not owner or owner["user_league_id"] != to_id:
            raise ValueError("Target player not owned by destination user")

        # Validar: los jugadores ofrecidos son del usuario ofertante (Canon §6)
        player_ids = list(dict.fromkeys(player_ids))  # sin duplicados
        if target_player_id in player_ids:
            raise ValueError("You cannot offer the target player")
        if player_ids:
            placeholders = ",".join("?" for _ in player_ids)
            mine = {
                r["player_id"]
                for r in conn.execute(
                    f"SELECT player_id FROM squad_players WHERE user_league_id = ? "
                    f"AND player_id IN ({placeholders})",
                    [from_id] + player_ids,
                )
            }
            if mine != set(player_ids):
                raise ValueError("You can only offer players from your own squad")

        # Validar: valor de la oferta >= valor de mercado del jugador (Canon §6)
        target = conn.execute(
            "SELECT market_value, name FROM players WHERE id = ?", (target_player_id,)
        ).fetchone()
        offered_players_value = 0
        if player_ids:
            placeholders = ",".join("?" for _ in player_ids)
            vals = conn.execute(
                f"SELECT SUM(market_value) as total FROM players WHERE id IN ({placeholders})",
                player_ids,
            ).fetchone()
            offered_players_value = vals["total"] or 0

        if cash + offered_players_value < target["market_value"]:
            raise ValueError("Offer value must be at least player's market value")

        # Validar: hay saldo para cubrir el dinero ofrecido
        info = get_budget_info(from_id, conn=conn)
        if cash > info["disponible_para_pujas"]:
            raise ValueError(
                f"You do not have that much cash available ({info['disponible_para_pujas']})"
            )

        # Validar: plantillas dentro del límite tras el intercambio
        from_size = _squad_size(conn, from_id)
        to_size = _squad_size(conn, to_id)
        if from_size - len(player_ids) + 1 > MAX_SQUAD_SIZE:
            raise ValueError(f"Your squad cannot exceed {MAX_SQUAD_SIZE} players")
        if to_size + len(player_ids) - 1 > MAX_SQUAD_SIZE:
            raise ValueError("The other user's squad cannot exceed the maximum size")

        cur = conn.execute(
            "INSERT INTO user_offers (from_user_league_id, to_user_league_id, target_player_id, offered_cash, offered_player_ids) VALUES (?, ?, ?, ?, ?)",
            (from_id, to_id, target_player_id, cash, json.dumps(player_ids)),
        )
    return {"id": cur.lastrowid, "status": "pending"}


def _squad_size(conn: sqlite3.Connection, user_league_id: int) -> int:
    return conn.execute(
        "SELECT COUNT(*) AS c FROM squad_players WHERE user_league_id = ?", (user_league_id,)
    ).fetchone()["c"]


def respond_to_offer(offer_id: int, user_league_id: int, action: str) -> dict:
    """action: accept, reject, counter (Canon §6)."""
    with transaction() as conn:
        offer = conn.execute(
            "SELECT * FROM user_offers WHERE id = ? AND to_user_league_id = ? AND status = 'pending'",
            (offer_id, user_league_id)
        ).fetchone()
        if not offer:
            raise ValueError("Offer not found or already resolved")

        offerer_id = offer["from_user_league_id"]   # quien ofertó (recibe al jugador)
        owner_id = offer["to_user_league_id"]       # quien acepta (dueño del jugador)
        offered_players = json.loads(offer["offered_player_ids"] or "[]")

        if action == "accept":
            # --- Revalidar que todo sigue siendo válido (Canon §6) ---
            target_owner = conn.execute(
                "SELECT id FROM squad_players WHERE player_id = ? AND user_league_id = ?",
                (offer["target_player_id"], owner_id),
            ).fetchone()
            if not target_owner:
                raise ValueError("The target player is no longer owned by that user")

            if offered_players:
                placeholders = ",".join("?" for _ in offered_players)
                still_mine = conn.execute(
                    f"SELECT COUNT(*) AS c FROM squad_players WHERE user_league_id = ? "
                    f"AND player_id IN ({placeholders})",
                    [offerer_id] + offered_players,
                ).fetchone()["c"]
                if still_mine != len(offered_players):
                    raise ValueError("The offered players are no longer available")

            info = get_budget_info(offerer_id, conn=conn)
            if offer["offered_cash"] > info["disponible_para_pujas"]:
                raise ValueError("The other user no longer has enough cash")

            # El que ofertó debe tener hueco contando los jugadores que entrega
            if _squad_size(conn, offerer_id) - len(offered_players) >= MAX_SQUAD_SIZE:
                raise ValueError(f"The other user's squad is full ({MAX_SQUAD_SIZE} players)")

            # --- Traspaso: el jugador pretendido pasa al que ofertó, con contrato nuevo ---
            conn.execute(
                "UPDATE squad_players SET user_league_id = ?, contract_weeks = ?, "
                "purchased_at = ?, purchase_price = ? WHERE player_id = ?",
                (offerer_id, CONTRACT_WEEKS, now_iso(), offer["offered_cash"], offer["target_player_id"]),
            )
            # Los jugadores ofrecidos pasan al que aceptó, con contrato nuevo
            if offered_players:
                placeholders = ",".join("?" for _ in offered_players)
                conn.execute(
                    f"UPDATE squad_players SET user_league_id = ?, contract_weeks = ?, purchased_at = ? "
                    f"WHERE player_id IN ({placeholders})",
                    [owner_id, CONTRACT_WEEKS, now_iso()] + offered_players,
                )
            # Dinero: lo paga quien ofertó y lo cobra quien acepta
            if offer["offered_cash"] > 0:
                conn.execute(
                    "UPDATE user_leagues SET budget = budget - ? WHERE id = ?",
                    (offer["offered_cash"], offerer_id),
                )
                conn.execute(
                    "UPDATE user_leagues SET budget = budget + ? WHERE id = ?",
                    (offer["offered_cash"], owner_id),
                )
                conn.execute(
                    "INSERT INTO transactions (user_league_id, type, amount, related_player_id, description) "
                    "VALUES (?, 'offer_paid', ?, ?, ?)",
                    (offerer_id, -offer["offered_cash"], offer["target_player_id"], "Offer accepted (paid)"),
                )
                conn.execute(
                    "INSERT INTO transactions (user_league_id, type, amount, related_player_id, description) "
                    "VALUES (?, 'offer_received', ?, ?, ?)",
                    (owner_id, offer["offered_cash"], offer["target_player_id"], "Offer accepted (received)"),
                )

            conn.execute(
                "UPDATE user_offers SET status = 'accepted', responded_at = ? WHERE id = ?",
                (now_iso(), offer_id),
            )
        elif action == "reject":
            conn.execute(
                "UPDATE user_offers SET status = 'rejected', responded_at = ? WHERE id = ?",
                (now_iso(), offer_id),
            )
        elif action == "counter":
            # La contraoferta se enviará como una oferta nueva en sentido inverso
            raise ValueError("counter not implemented yet")
        else:
            raise ValueError(f"Unknown action: {action}")
    return {"success": True, "action": action}


# ---------- Budget helpers ----------
def get_budget_info(user_league_id: int, conn: Optional[sqlite3.Connection] = None) -> dict:
    """
    Información económica del usuario (Canon §5.1).

    - saldo: presupuesto actual (puede ser negativo).
    - pufo_permitido: descubierto aceptado = 20% del VALOR DE PLANTILLA (Canon §5.1).
    - comprometido_en_pujas: suma de pujas todavía vivas.
    - disponible_para_pujas: saldo + pufo - comprometido.
    """
    def _compute(connection: sqlite3.Connection) -> dict:
        ul = connection.execute(
            "SELECT budget FROM user_leagues WHERE id = ?", (user_league_id,)
        ).fetchone()
        budget = ul["budget"] if ul else 0
        squad_value = connection.execute(
            "SELECT COALESCE(SUM(p.market_value), 0) AS total FROM squad_players sp "
            "JOIN players p ON p.id = sp.player_id WHERE sp.user_league_id = ?",
            (user_league_id,),
        ).fetchone()["total"]
        committed = _committed_bids(connection, user_league_id)
        pufo = int(squad_value * PUFO_PERCENT)
        return {
            "saldo": budget,
            "valor_plantilla": squad_value,
            "pufo_permitido": pufo,
            "disponible_para_pujas": max(0, budget + pufo - committed),
            "comprometido_en_pujas": committed,
            "en_negativo": budget < 0,
        }

    if conn is not None:
        return _compute(conn)
    with get_conn() as own:
        return _compute(own)


# ---------- Estadísticas y evolución del usuario (pantalla Datos) ----------
BUY_TYPES = ("purchase", "offer_paid")
SELL_TYPES = ("sale", "offer_received", "contract_compensation")


def get_user_season_stats(user_league_id: int, league_id: int, conn: Optional[sqlite3.Connection] = None) -> dict:
    """Estadísticas, economía y récords PERSONALES reales del usuario."""
    def _compute(conn):
        totals = conn.execute(
            "SELECT COALESCE(SUM(points),0) AS points, COALESCE(SUM(goals),0) AS goals, "
            "COALESCE(SUM(assists),0) AS assists, COALESCE(SUM(clean_sheets),0) AS clean_sheets, "
            "COUNT(*) AS matchdays_played FROM matchday_results "
            "WHERE user_league_id = ? AND finished_at IS NOT NULL",
            (user_league_id,)).fetchone()
        mvp_count = conn.execute(
            "SELECT COUNT(*) AS c FROM matchday_results "
            "WHERE user_league_id = ? AND mvp_player_id IS NOT NULL",
            (user_league_id,)).fetchone()["c"]
        standings = conn.execute(
            "SELECT ul.id AS user_league_id, COALESCE(SUM(mr.points),0) AS points, "
            "COALESCE(SUM(mr.goals),0) AS goals FROM user_leagues ul "
            "LEFT JOIN matchday_results mr ON mr.user_league_id = ul.id "
            "WHERE ul.league_id = ? GROUP BY ul.id ORDER BY points DESC, goals DESC",
            (league_id,)).fetchall()
        total_teams = len(standings)
        position = next((i + 1 for i, r in enumerate(standings) if r["user_league_id"] == user_league_id), total_teams)
        economic = conn.execute(
            "SELECT COALESCE(SUM(CASE WHEN type IN ('purchase','offer_paid') THEN -amount ELSE 0 END),0) AS total_spent, "
            "COALESCE(SUM(CASE WHEN type IN ('sale','offer_received','contract_compensation') THEN amount ELSE 0 END),0) AS total_received, "
            "COALESCE(SUM(CASE WHEN type IN ('purchase','offer_paid') THEN 1 ELSE 0 END),0) AS compras, "
            "COALESCE(SUM(CASE WHEN type IN ('sale','offer_received') THEN 1 ELSE 0 END),0) AS ventas "
            "FROM transactions WHERE user_league_id = ?",
            (user_league_id,)).fetchone()
        total_spent = int(economic["total_spent"])
        total_received = int(economic["total_received"])
        mejor_jornada = conn.execute(
            "SELECT matchday, points FROM matchday_results WHERE user_league_id = ? AND finished_at IS NOT NULL "
            "ORDER BY points DESC, matchday ASC LIMIT 1", (user_league_id,)).fetchone()
        peor_jornada = conn.execute(
            "SELECT matchday, points FROM matchday_results WHERE user_league_id = ? AND finished_at IS NOT NULL "
            "ORDER BY points ASC, matchday ASC LIMIT 1", (user_league_id,)).fetchone()
        mejor_goleada = conn.execute(
            "SELECT matchday, goals FROM matchday_results WHERE user_league_id = ? AND finished_at IS NOT NULL "
            "AND goals > 0 ORDER BY goals DESC, points DESC, matchday ASC LIMIT 1", (user_league_id,)).fetchone()
        ordered = conn.execute(
            "SELECT points FROM matchday_results WHERE user_league_id = ? AND finished_at IS NOT NULL "
            "ORDER BY matchday ASC", (user_league_id,)).fetchall()
        best_streak = cur_streak = 0
        for row in ordered:
            cur_streak = cur_streak + 1 if row["points"] > 0 else 0
            best_streak = max(best_streak, cur_streak)
        mejor_compra = conn.execute(
            "SELECT p.name AS player_name, sp.purchase_price AS purchased_for, p.market_value AS current_value, "
            "(p.market_value - sp.purchase_price) AS gain FROM squad_players sp "
            "JOIN players p ON p.id = sp.player_id "
            "WHERE sp.user_league_id = ? AND sp.status = 'squad' AND sp.purchase_price IS NOT NULL "
            "ORDER BY gain DESC, sp.purchase_price DESC LIMIT 1", (user_league_id,)).fetchone()
        return {
            "totals": {"points": int(totals["points"]), "goals": int(totals["goals"]),
                       "assists": int(totals["assists"]), "clean_sheets": int(totals["clean_sheets"]),
                       "mvp_count": int(mvp_count), "matchdays_played": int(totals["matchdays_played"]),
                       "position": int(position), "total_teams": int(total_teams)},
            "economic": {"total_spent": total_spent, "total_received": total_received,
                         "beneficio": int(total_received - total_spent),
                         "compras": int(economic["compras"]), "ventas": int(economic["ventas"])},
            "records": {
                "mejor_jornada": ({"matchday": int(mejor_jornada["matchday"]), "points": int(mejor_jornada["points"])}
                                  if mejor_jornada else None),
                "peor_jornada": ({"matchday": int(peor_jornada["matchday"]), "points": int(peor_jornada["points"])}
                                 if peor_jornada else None),
                "mejor_goleada": ({"matchday": int(mejor_goleada["matchday"]), "goals": int(mejor_goleada["goals"])}
                                  if mejor_goleada else None),
                "mejor_racha": int(best_streak),
                "mejor_compra": ({"player_name": mejor_compra["player_name"],
                                  "purchased_for": int(mejor_compra["purchased_for"]),
                                  "current_value": int(mejor_compra["current_value"]),
                                  "gain": int(mejor_compra["gain"])}
                                 if mejor_compra and mejor_compra["gain"] > 0 else None),
            },
        }
    if conn is not None:
        return _compute(conn)
    with get_conn() as own:
        return _compute(own)


def get_user_evolution(user_league_id: int, league_id: int, conn: Optional[sqlite3.Connection] = None) -> list:
    """Serie por jornada del usuario: puntos/goles/asistencias de esa jornada,
    puntos acumulados y posición en la liga al final de cada jornada.

    La posición se calcula sobre los puntos ACUMULADOS de todos los equipos
    hasta esa jornada (desc, desempate por goles). Devuelve [] si no hay
    ninguna jornada jugada todavía.
    """
    def _compute(conn):
        rows = conn.execute(
            "SELECT matchday, user_league_id, points, goals, assists, clean_sheets "
            "FROM matchday_results WHERE league_id = ? AND finished_at IS NOT NULL",
            (league_id,)).fetchall()
        if not rows:
            return []
        max_md = max(r["matchday"] for r in rows)
        metric = {}
        for r in rows:
            metric[(r["matchday"], r["user_league_id"])] = dict(r)
        all_uls = {r["user_league_id"] for r in rows}
        all_uls.add(user_league_id)
        cum_points = {}
        cum_goals = {}
        for md in range(1, max_md + 1):
            for ul in all_uls:
                cum_points[(md, ul)] = cum_points.get((md - 1, ul), 0) + (metric.get((md, ul), {}).get("points") or 0)
                cum_goals[(md, ul)] = cum_goals.get((md - 1, ul), 0) + (metric.get((md, ul), {}).get("goals") or 0)
        out = []
        for md in range(1, max_md + 1):
            ranked = sorted(all_uls, key=lambda ul: (cum_points[(md, ul)], cum_goals[(md, ul)]), reverse=True)
            my = metric.get((md, user_league_id), {})
            out.append({
                "matchday": md,
                "points": int(my.get("points") or 0),
                "goals": int(my.get("goals") or 0),
                "assists": int(my.get("assists") or 0),
                "clean_sheets": int(my.get("clean_sheets") or 0),
                "cumulative_points": int(cum_points[(md, user_league_id)]),
                "position": ranked.index(user_league_id) + 1,
            })
        return out
    if conn is not None:
        return _compute(conn)
    with get_conn() as own:
        return _compute(own)


# ---------- Scoring Engine ----------
from scoring_engine import PlayerMatchStats, ScoringParams, calculate_points


def calculate_matchday_points(user_league_id: int, matchday: int, params: Optional[ScoringParams] = None) -> int:
    """
    Calcula y guarda los puntos de la alineación de un usuario en una jornada.

    - Solo puntúan los titulares.
    - El capitán suma +2 puntos (Canon §3).
    - Se guardan también goles y asistencias para los logros de la liga.
    """
    if params is None:
        params = ScoringParams.default()

    with get_conn() as conn:
        # Alineación + capitán elegido
        ms = conn.execute(
            "SELECT id, captain_player_id FROM matchday_squad WHERE user_league_id = ? AND matchday = ?",
            (user_league_id, matchday),
        ).fetchone()
        if not ms:
            return 0

        lineup = conn.execute(
            """
            SELECT ml.player_id, ml.is_starter,
                   p.name, p.position, p.role
            FROM matchday_lineup ml
            JOIN players p ON p.id = ml.player_id
            WHERE ml.matchday_squad_id = ?
            """,
            (ms["id"],),
        ).fetchall()

        captain_id = ms["captain_player_id"]
        total_points = 0
        total_goals = 0
        total_assists = 0
        total_clean_sheets = 0

        for player in lineup:
            if not player["is_starter"]:
                continue
            player_stats = get_player_matchday_stats(player["player_id"], matchday)
            if not player_stats:
                continue

            stats_data = {
                k: v for k, v in player_stats.items()
                if k not in ("player_id", "position", "role")
            }
            stats_obj = PlayerMatchStats(
                player_id=player["player_id"],
                position=player["position"],
                role=player["role"],
                **stats_data,
            )
            # Canon §3: el capitán suma un bonus fijo
            stats_obj.is_captain = player["player_id"] == captain_id

            total_points += calculate_points(stats_obj, params)
            total_goals += int(stats_obj.goals or 0)
            total_assists += int(stats_obj.assists or 0)
            if stats_obj.clean_sheet and stats_obj.minutes >= 60:
                total_clean_sheets += 1

        conn.execute(
            """
            INSERT OR REPLACE INTO matchday_results
            (league_id, matchday, user_league_id, points, goals, assists, clean_sheets, finished_at)
            VALUES ((SELECT league_id FROM user_leagues WHERE id = ?), ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user_league_id, matchday, user_league_id,
                total_points, total_goals, total_assists, total_clean_sheets, now_iso(),
            ),
        )

        return total_points


def get_player_matchday_stats(player_id: int, matchday: int) -> Optional[dict]:
    """Estadísticas de un jugador en una jornada concreta de la temporada activa."""
    conn = sqlite3.connect(str(SOFASCORE_DB_PATH))
    conn.row_factory = sqlite3.Row
    try:
        try:
            from configuracion import TEMPORADA_ID
        except Exception:  # pragma: no cover
            TEMPORADA_ID = None

        row = None
        if TEMPORADA_ID is not None:
            # Solo la temporada activa: puntuar con stats de la temporada
            # anterior (misma ronda, temporada distinta) sería un error.
            row = conn.execute(
                """
                SELECT estadisticas, posicion, titular, capitan, rating, resultado,
                       marcador_local, marcador_visitante, localizacion
                FROM estadisticas_jugador_partido
                WHERE jugador_id = ? AND ronda = ? AND temporada_id = ?
                LIMIT 1
                """,
                (player_id, matchday, TEMPORADA_ID),
            ).fetchone()
        else:
            # Sin temporada configurada: query sin filtro (datos legacy).
            row = conn.execute(
                """
                SELECT estadisticas, posicion, titular, capitan, rating, resultado,
                       marcador_local, marcador_visitante, localizacion
                FROM estadisticas_jugador_partido
                WHERE jugador_id = ? AND ronda = ?
                LIMIT 1
                """,
                (player_id, matchday),
            ).fetchone()

        if not row:
            return None
            
        stats_json = json.loads(row["estadisticas"])
        
        # Map SofaScore position to fantasy position
        pos_map = {"G": "POR", "D": "DEF", "M": "MED", "F": "DEL"}
        fantasy_pos = pos_map.get(row["posicion"], "MED")
        
        # Determine clean sheet: player's team conceded 0 goals
        if row["localizacion"] == "home":
            goals_conceded = row["marcador_visitante"]
        else:
            goals_conceded = row["marcador_local"]
        clean_sheet = (goals_conceded == 0) and row["titular"] == 1
        
        # Extract cards from stats (may not be in JSON, check rating field or infer)
        yellow_cards = stats_json.get("yellowCard", 0)
        red_cards = stats_json.get("redCard", 0)
        second_yellow = stats_json.get("secondYellow", False)
        
        # Extract penalties
        penalties_scored = stats_json.get("penaltyScored", 0)
        penalties_missed = stats_json.get("penaltyMissed", 0)
        penalties_conceded = stats_json.get("penaltyConceded", 0)
        penalties_saved = stats_json.get("penaltySaved", 0)
        
        # Shots
        shots_on_target = stats_json.get("shotsOnTarget", 0)
        shots_off_target = stats_json.get("shotsOffTarget", stats_json.get("totalShots", 0) - shots_on_target)
        
        # Defensive stats
        interceptions = stats_json.get("interceptionWon", 0)
        clearances = stats_json.get("totalClearance", 0)
        tackles_won = stats_json.get("wonTackle", 0)
        fouls_committed = stats_json.get("fouls", 0)
        fouls_suffered = stats_json.get("wasFouled", 0)
        ball_recovery = stats_json.get("ballRecovery", 0)
        blocked_scoring_attempt = stats_json.get("blockedScoringAttempt", 0)
        
        # Passing/dribbling
        key_passes = stats_json.get("keyPass", stats_json.get("expectedAssists", 0))
        dribbles_completed = stats_json.get("dribbleWon", 0)
        aerials_won = stats_json.get("aerialWon", 0)
        saves = stats_json.get("saves", 0)
        
        # Goals conceded (for GK/DEF)
        goals_conceded_val = goals_conceded if fantasy_pos in ["POR", "DEF"] else 0
        
        return {
            "player_id": player_id,
            "position": fantasy_pos,
            "role": row["posicion"],  # Will be mapped in scoring
            "minutes": stats_json.get("minutesPlayed", 0),
            "goals": stats_json.get("goals", 0),
            "assists": stats_json.get("goalAssist", 0),
            "shots_on_target": shots_on_target,
            "shots_off_target": max(0, shots_off_target),
            "key_passes": int(key_passes) if isinstance(key_passes, (int, float)) else 0,
            "dribbles_completed": dribbles_completed,
            "aerials_won": aerials_won,
            "interceptions": interceptions,
            "clearances": clearances,
            "tackles_won": tackles_won,
            "fouls_committed": fouls_committed,
            "fouls_suffered": fouls_suffered,
            "yellow_cards": yellow_cards,
            "red_cards": red_cards,
            "second_yellow": second_yellow,
            "penalties_scored": penalties_scored,
            "penalties_missed": penalties_missed,
            "penalties_conceded": penalties_conceded,
            "penalties_saved": penalties_saved,
            "clean_sheet": clean_sheet,
            "own_goals": stats_json.get("ownGoals", 0),
            "goals_conceded": goals_conceded_val,
            "saves": saves,
            "ball_recovery": ball_recovery,
            "blocked_scoring_attempt": blocked_scoring_attempt,
            "rating": row["rating"],
            "is_starter": bool(row["titular"]),
            "is_captain": bool(row["capitan"]),
        }
    except Exception as e:
        print(f"Error fetching stats for player {player_id} matchday {matchday}: {e}")
        return None
    finally:
        conn.close()


def confirmados_jornada(matchday: int) -> set:
    """IDs de jugadores incluidos en el once confirmado de la jornada.

    Se lee de la BD de SofaScore (`estadisticas_jugador_partido`,
    `titular = 1` de la ronda/jornada activa). Canon §3: deben mostrarse las
    alineaciones confirmadas por partido cuando estén disponibles. Si no hay
    datos (aún no hay alineaciones confirmadas) devuelve el conjunto vacío.
    """
    ids: set = set()
    try:
        conn = sqlite3.connect(str(SOFASCORE_DB_PATH))
        conn.row_factory = sqlite3.Row
        try:
            from configuracion import TEMPORADA_ID
        except Exception:  # pragma: no cover
            TEMPORADA_ID = None
        rows = None
        if TEMPORADA_ID is not None:
            rows = conn.execute(
                "SELECT DISTINCT jugador_id FROM estadisticas_jugador_partido "
                "WHERE ronda = ? AND temporada_id = ? AND titular = 1",
                (matchday, TEMPORADA_ID),
            ).fetchall()
        if not rows:
            rows = conn.execute(
                "SELECT DISTINCT jugador_id FROM estadisticas_jugador_partido "
                "WHERE ronda = ? AND titular = 1",
                (matchday,),
            ).fetchall()
        ids = {r["jugador_id"] for r in rows}
        conn.close()
    except Exception:  # pragma: no cover - sin BD de SofaScore: sin datos confirmados
        ids = set()
    return ids


# ---------- Datos de fútbol real (Canon §9: pantalla Datos) ----------
def _sofascore_conn():
    conn = sqlite3.connect(str(SOFASCORE_DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def _temporada_id() -> Optional[int]:
    try:
        from configuracion import TEMPORADA_ID
        return TEMPORADA_ID
    except Exception:  # pragma: no cover
        return None


def get_real_standings() -> list[dict]:
    """Clasificación real de LaLiga de la temporada activa, calculada EN VIVO
    a partir de los partidos terminados (la tabla `clasificacion` es una foto
    del momento en que se extrajo y se queda obsoleta).

    Si la temporada activa aún no tiene partidos terminados, se usa la foto.
    """
    try:
        conn = _sofascore_conn()
        tid = _temporada_id()
        rows = conn.execute(
            """
            SELECT equipo_id, equipo_id AS id,
                   MAX(nombre) AS equipo_nombre,
                   SUM(jugados) AS jugados,
                   SUM(victorias) AS victorias,
                   SUM(empates) AS empates,
                   SUM(derrotas) AS derrotas,
                   SUM(goles_favor) AS goles_favor,
                   SUM(goles_contra) AS goles_contra,
                   SUM(goles_favor) - SUM(goles_contra) AS diferencia_goles,
                   3 * SUM(victorias) + SUM(empates) AS puntos
            FROM (
                SELECT equipo_local_id AS equipo_id, equipo_local_nombre AS nombre,
                       1 AS jugados,
                       (marcador_local > marcador_visitante) AS victorias,
                       (marcador_local = marcador_visitante) AS empates,
                       (marcador_local < marcador_visitante) AS derrotas,
                       marcador_local AS goles_favor, marcador_visitante AS goles_contra
                FROM partidos
                WHERE temporada_id = ? AND tipo_estado = 'finished'
                  AND marcador_local IS NOT NULL AND marcador_visitante IS NOT NULL
                UNION ALL
                SELECT equipo_visitante_id, equipo_visitante_nombre,
                       1,
                       (marcador_visitante > marcador_local),
                       (marcador_visitante = marcador_local),
                       (marcador_visitante < marcador_local),
                       marcador_visitante, marcador_local
                FROM partidos
                WHERE temporada_id = ? AND tipo_estado = 'finished'
                  AND marcador_local IS NOT NULL AND marcador_visitante IS NOT NULL
            )
            GROUP BY equipo_id
            """,
            (tid, tid),
        ).fetchall()

        if not rows:
            # Sin partidos terminados aún: se usa la foto (clasificacion) si existe
            if tid is not None:
                rows = conn.execute(
                    "SELECT * FROM clasificacion WHERE temporada_id = ? ORDER BY posicion", (tid,)
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT c.* FROM clasificacion c "
                    "JOIN (SELECT MAX(temporada_id) AS tid FROM clasificacion) t ON c.temporada_id = t.tid "
                    "ORDER BY c.posicion"
                ).fetchall()
        else:
            rows.sort(key=lambda r: (-r["puntos"], -r["diferencia_goles"], -r["goles_favor"]))

        forms = _forma_clubes(conn, tid)
        out = []
        for i, r in enumerate(rows, start=1):
            d = dict(r)
            d["posicion"] = i
            d["forma"] = forms.get(r["equipo_id"], [])
            out.append(d)
        conn.close()
        return out
    except Exception:  # pragma: no cover - sin datos reales
        return []


def real_current_matchday() -> int:
    """Jornada real en curso: la primera jornada de la temporada activa con
    menos de la mitad de sus partidos terminados. Si todas están terminadas
    (o no hay datos), devuelve la última jornada + 1... limitado a 38."""
    try:
        conn = _sofascore_conn()
        tid = _temporada_id()
        if tid is None:
            conn.close()
            return 1
        rows = conn.execute(
            """
            SELECT ronda,
                   COUNT(*) AS n,
                   SUM(CASE WHEN tipo_estado = 'finished' THEN 1 ELSE 0 END) AS finished
            FROM partidos WHERE temporada_id = ?
            GROUP BY ronda ORDER BY ronda
            """,
            (tid,),
        ).fetchall()
        conn.close()
        for r in rows:
            if r["finished"] * 2 < r["n"]:
                return r["ronda"]
        return (rows[-1]["ronda"] + 1) if rows else 1
    except Exception:  # pragma: no cover - sin datos reales
        return 1


def _forma_clubes(conn, temporada_id) -> dict:
    """Últimos 5 resultados ('V'/'E'/'D') por club, en orden cronológico."""
    forms: dict = {}
    try:
        rows = conn.execute(
            """
            SELECT equipo_local_id, equipo_visitante_id, marcador_local, marcador_visitante
            FROM partidos
            WHERE temporada_id = ? AND tipo_estado = 'finished'
              AND marcador_local IS NOT NULL AND marcador_visitante IS NOT NULL
            ORDER BY ronda ASC
            """,
            (temporada_id,),
        ).fetchall()
        for r in rows:
            gl, gv = r["marcador_local"], r["marcador_visitante"]
            # V=0, E=1, D=2
            resultados = (
                ("V", "E", "D")[int(gl < gv) * 2 + int(gl == gv)],
                ("V", "E", "D")[int(gv < gl) * 2 + int(gv == gl)],
            )
            for cid, res in ((r["equipo_local_id"], resultados[0]), (r["equipo_visitante_id"], resultados[1])):
                forms.setdefault(cid, []).append(res)
    except Exception:  # pragma: no cover
        pass
    return {cid: res[-5:] for cid, res in forms.items()}


def get_real_fixtures(ronda: int) -> list[dict]:
    """Partidos reales de la jornada (ronda) de la temporada activa."""
    try:
        conn = _sofascore_conn()
        tid = _temporada_id()
        sql = (
            "SELECT id, fecha, equipo_local_id, equipo_local_nombre, "
            "equipo_visitante_id, equipo_visitante_nombre, "
            "marcador_local, marcador_visitante, tipo_estado, descripcion_estado "
            "FROM partidos WHERE "
        )
        if tid is not None:
            rows = conn.execute(
                sql + "temporada_id = ? AND ronda = ? ORDER BY COALESCE(fecha, '') ASC",
                (tid, ronda),
            ).fetchall()
        else:
            rows = conn.execute(
                sql + "ronda = ? AND temporada_id = (SELECT MAX(temporada_id) FROM partidos) "
                "ORDER BY COALESCE(fecha, '') ASC",
                (ronda,),
            ).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception:  # pragma: no cover
        return []


def get_real_player_injuries() -> set:
    """Jugadores con lesión activa según la BD de SofaScore (Canon §9)."""
    try:
        conn = _sofascore_conn()
        rows = conn.execute(
            "SELECT DISTINCT jugador_id FROM lesiones "
            "WHERE estado IN ('out', 'dayToDay') "
            "AND COALESCE(fecha_fin_estimada, 0) > CAST(strftime('%s', 'now') AS INTEGER)"
        ).fetchall()
        conn.close()
        return {r["jugador_id"] for r in rows}
    except Exception:  # pragma: no cover
        return set()


def search_real_players(q: str = "", pos: str = "", equipo: str = "", lesionado: Optional[bool] = None, limit: int = 25) -> list[dict]:
    """Búsqueda de jugadores reales (Canon §9).

    `q` filtra por nombre; `pos` por posición real (G/D/M/F); `equipo` por
    club; `lesionado` por estado de lesión (usa la tabla `lesiones`).
    """
    try:
        conn = _sofascore_conn()
        sql = """
            SELECT j.id, j.nombre, j.equipo_id, j.equipo_nombre, s.posicion,
                   CASE WHEN l.jugador_id IS NOT NULL THEN 1 ELSE 0 END AS lesionado
            FROM jugadores j
            LEFT JOIN (
                SELECT x.jugador_id, x.posicion
                FROM estadisticas_jugador_partido x
                JOIN (
                    SELECT jugador_id, MAX(marca_tiempo) AS mt
                    FROM estadisticas_jugador_partido
                    GROUP BY jugador_id
                ) y ON x.jugador_id = y.jugador_id AND x.marca_tiempo = y.mt
            ) s ON s.jugador_id = j.id
            LEFT JOIN (
                SELECT DISTINCT jugador_id FROM lesiones
                WHERE estado IN ('out', 'dayToDay')
                  AND COALESCE(fecha_fin_estimada, 0) > CAST(strftime('%s', 'now') AS INTEGER)
            ) l ON l.jugador_id = j.id
            WHERE j.nombre LIKE ?
        """
        params: list = [f"%{q}%"]
        if pos:
            sql += " AND s.posicion = ?"
            params.append(pos)
        if equipo:
            sql += " AND j.equipo_nombre LIKE ?"
            params.append(f"%{equipo}%")
        if lesionado:
            sql += " AND l.jugador_id IS NOT NULL"
        elif lesionado is False:
            sql += " AND l.jugador_id IS NULL"
        sql += " ORDER BY j.nombre ASC LIMIT ?"
        params.append(limit)
        rows = conn.execute(sql, params).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception:  # pragma: no cover
        return []


def run_matchday_scoring(matchday: int, league_id: int = 1, params: Optional[ScoringParams] = None) -> dict:
    """Run scoring for all users in a league for a given matchday."""
    with get_conn() as conn:
        users = conn.execute(
            "SELECT id FROM user_leagues WHERE league_id = ?", (league_id,)
        ).fetchall()

    results = {}
    for u in users:
        pts = calculate_matchday_points(u["id"], matchday, params)
        results[u["id"]] = pts

    return results


def get_reminders(user_league_id: int) -> list[dict]:
    """Avisos de la Home para un usuario (Canon §9).

    Devuelve solo los avisos **pendientes** (los que aún no se han visto) y los
    marca como leídos, de modo que "tras consultarlos desaparezcan". Hoy cubre
    las fuentes con datos reales:
      - Ofertas recibidas pendientes de respuesta.
      - Contratos de la plantilla próximos a expirar (1-2 jornadas).
      - Pujas propias en curso en el mercado.
      - Lesiones activas de jugadores de tu plantilla (datos reales de
        SofaScore).
    (Sanciones y logros se conectarán cuando existan en el juego.)
    """
    with get_conn() as conn:
        read_set = {
            r["key"]
            for r in conn.execute(
                "SELECT key FROM read_reminders WHERE user_league_id = ?",
                (user_league_id,),
            ).fetchall()
        }
        pending: list[dict] = []

        # 1) Ofertas recibidas pendientes.
        for o in conn.execute(
            """
            SELECT o.id, p.name AS player_name
            FROM user_offers o
            JOIN players p ON p.id = o.target_player_id
            WHERE o.to_user_league_id = ? AND o.status = 'pending'
            """,
            (user_league_id,),
        ).fetchall():
            pending.append({
                "key": f"offer:{o['id']}",
                "tipo": "oferta",
                "texto": f"Oferta recibida por {o['player_name'] or 'un jugador'}.",
            })

        # 2) Contratos próximos a expirar.
        for c in conn.execute(
            """
            SELECT sp.player_id, sp.contract_weeks, p.name
            FROM squad_players sp
            JOIN players p ON p.id = sp.player_id
            WHERE sp.user_league_id = ? AND sp.contract_weeks BETWEEN 1 AND 2
            """,
            (user_league_id,),
        ).fetchall():
            n = c["contract_weeks"]
            when = "esta jornada" if n == 1 else f"en {n} jornadas"
            pending.append({
                "key": f"contract:{c['player_id']}",
                "tipo": "contrato",
                "texto": f"El contrato de {c['name']} expira {when}.",
            })

        # 3) Pujas propias en curso.
        nb = conn.execute(
            """
            SELECT COUNT(*) AS c FROM bids b
            JOIN market_players mp ON mp.id = b.market_player_id
            WHERE b.user_league_id = ? AND b.resolved = 0 AND mp.status = 'active'
            """,
            (user_league_id,),
        ).fetchone()["c"]
        if nb:
            pending.append({
                "key": "bids:active",
                "tipo": "pujas",
                "texto": f"Tienes {nb} puja{'s' if nb != 1 else ''} en curso en el mercado.",
            })

        # 4) Lesiones de jugadores de tu plantilla (datos reales de SofaScore).
        lesionados = get_real_player_injuries()
        for lrow in conn.execute(
            """
            SELECT sp.player_id, p.name
            FROM squad_players sp
            JOIN players p ON p.id = sp.player_id
            WHERE sp.user_league_id = ?
            """,
            (user_league_id,),
        ).fetchall():
            if lrow["player_id"] in lesionados:
                pending.append({
                    "key": f"injury:{lrow['player_id']}",
                    "tipo": "lesion",
                    "texto": f"{lrow['name']} está lesionado ahora mismo.",
                })

        unseen = [r for r in pending if r["key"] not in read_set]
        for r in unseen:
            conn.execute(
                "INSERT OR IGNORE INTO read_reminders (user_league_id, key) VALUES (?, ?)",
                (user_league_id, r["key"]),
            )
        return unseen


def _classification_payout(rank: int, n: int, low: int, high: int) -> int:
    """Compensación por clasificación de jornada (Canon §8).

    `rank` 1 = mejor posición, `n` = número de equipos. Favorece al último:
    el último cobra `high` y el primero `low`, interpolando en lineal.
    """
    if n <= 1:
        return low
    return int(round(low + (rank - 1) * (high - low) / (n - 1)))


def _team_matchday_econ(user_league_id: int, matchday: int, params: ScoringParams):
    """Goles, rojas y jugador de más puntos entre los titulares de un equipo en una jornada."""
    with get_conn() as conn:
        ms = conn.execute(
            "SELECT id FROM matchday_squad WHERE user_league_id = ? AND matchday = ?",
            (user_league_id, matchday),
        ).fetchone()
        if not ms:
            return 0, 0, 0, None
        rows = conn.execute(
            """
            SELECT ml.player_id, p.position, p.role
            FROM matchday_lineup ml
            JOIN players p ON p.id = ml.player_id
            WHERE ml.matchday_squad_id = ? AND ml.is_starter = 1
            """,
            (ms["id"],),
        ).fetchall()
    goals = reds = 0
    top_pts = 0
    top_player_id = None
    for r in rows:
        st = get_player_matchday_stats(r["player_id"], matchday)
        if not st:
            continue
        stats_data = {
            k: v for k, v in st.items()
            if k not in ("player_id", "position", "role")
        }
        sobj = PlayerMatchStats(
            player_id=r["player_id"],
            position=r["position"],
            role=r["role"],
            **stats_data,
        )
        goals += int(sobj.goals or 0)
        reds += int(sobj.red_cards or 0) + (1 if sobj.second_yellow else 0)
        pts = calculate_points(sobj, params)
        if pts > top_pts:
            top_pts = pts
            top_player_id = r["player_id"]
    return goals, reds, top_pts, top_player_id


def apply_matchday_economics(
    league_id: int, matchday: int, results: dict, params: Optional[ScoringParams] = None
) -> dict:
    """Aplica la economía de la jornada (Canon §8) a todos los equipos de la liga.

    `results` es el dict {user_league_id: puntos} de `run_matchday_scoring`.
    Abona: ingresos por puntos, premio por goles, penalización por rojas,
    compensación por clasificación de jornada (favorece al último) y el bono al
    propietario del MVP de la jornada. Devuelve el desglose por equipo.
    """
    if params is None:
        params = ScoringParams.default()
    with get_conn() as conn:
        teams = conn.execute(
            "SELECT id FROM user_leagues WHERE league_id = ?", (league_id,)
        ).fetchall()
    team_ids = [t["id"] for t in teams]
    if not team_ids:
        return {}

    econ = {}
    for ul in team_ids:
        goals, reds, top_pts, top_player_id = _team_matchday_econ(ul, matchday, params)
        econ[ul] = {"goals": goals, "reds": reds, "top_pts": top_pts, "top_player_id": top_player_id}

    ranked = sorted(team_ids, key=lambda u: int(results.get(u, 0) or 0), reverse=True)
    n = len(ranked)
    rank_of = {ul: i + 1 for i, ul in enumerate(ranked)}
    mvp_team = None
    if econ:
        best = max(team_ids, key=lambda u: econ[u]["top_pts"])
        if econ[best]["top_pts"] > 0:
            mvp_team = best

    breakdowns = {}
    with transaction() as conn:
        for ul in team_ids:
            points = int(results.get(ul, 0) or 0)
            income = points * ECON_INCOME_PER_POINT
            goal_bonus = econ[ul]["goals"] * ECON_GOAL_BONUS
            red_pen = -econ[ul]["reds"] * ECON_RED_PENALTY
            class_payout = _classification_payout(
                rank_of[ul], n, ECON_CLASSIFICATION_LOW, ECON_CLASSIFICATION_HIGH
            )
            mvp_bonus = ECON_MVP_MATCHDAY_BONUS if ul == mvp_team else 0
            total = income + goal_bonus + red_pen + class_payout + mvp_bonus
            if total != 0:
                conn.execute(
                    "UPDATE user_leagues SET budget = budget + ? WHERE id = ?",
                    (total, ul),
                )
                parts = [f"ingresos x{points} pts"]
                if goal_bonus:
                    parts.append(f"{econ[ul]['goals']} goles")
                if red_pen:
                    parts.append(f"-{econ[ul]['reds']} rojas")
                if class_payout:
                    parts.append(f"clasificación P{rank_of[ul]}")
                if mvp_bonus:
                    parts.append("MVP jornada")
                mvp_player = (
                    econ.get(mvp_team, {}).get("top_player_id") if ul == mvp_team else None
                )
                conn.execute(
                    "INSERT INTO transactions "
                    "(user_league_id, type, amount, related_player_id, description) "
                    "VALUES (?, 'matchday_economy', ?, ?, ?)",
                    (ul, total, mvp_player, f"Jornada {matchday}: " + ", ".join(parts)),
                )
            breakdowns[ul] = {
                "points": points,
                "income": income,
                "goal_bonus": goal_bonus,
                "red_penalty": red_pen,
                "classification": class_payout,
                "mvp_bonus": mvp_bonus,
                "total": total,
                "rank": rank_of[ul],
            }
    return breakdowns


def close_matchday(league_id: int, matchday: int, params: Optional[ScoringParams] = None) -> dict:
    """
    Cierra una jornada: puntúa a todos los equipos y, si se cierra la jornada
    en curso, avanza `current_matchday` a la siguiente.

    Idempotente: re-ejecutar el scoring de una jornada ya cerrada recalcula los
    puntos pero NO vuelve a avanzar la jornada ni repite la economía. Devuelve:
      {"results": {user_league_id: puntos}, "advanced_to": int | None, "economy": dict}
    """
    results = run_matchday_scoring(matchday, league_id, params)
    advanced_to = None
    with transaction() as conn:
        league = conn.execute(
            "SELECT id, current_matchday FROM leagues WHERE id = ?", (league_id,)
        ).fetchone()
        if league and league["current_matchday"] == matchday:
            advanced_to = matchday + 1
            conn.execute(
                "UPDATE leagues SET current_matchday = ? WHERE id = ?",
                (advanced_to, league_id),
            )
    # Economía de la jornada (Canon §8): solo al cerrar (avanzar) la jornada.
    economy = (
        apply_matchday_economics(league_id, matchday, results, params)
        if advanced_to is not None
        else {}
    )
    return {"results": results, "advanced_to": advanced_to, "economy": economy}


def matchday_results_available(matchday: int, min_rows: Optional[int] = None) -> bool:
    """
    Indica si la BD de SofaScore ya tiene resultados suficientes de la jornada
    `matchday` (ronda) de la temporada activa para poder cerrarla.

    Sirve de puerta de entrada al cierre automático: solo se cierra una jornada
    cuando hay datos reales, evitando puntuar con 0 una jornada a medias. El
    umbral se puede configurar con `GURUTZE_AUTO_CLOSE_MIN_STATS` (por defecto 300).
    """
    if min_rows is None:
        try:
            min_rows = int(os.environ.get("GURUTZE_AUTO_CLOSE_MIN_STATS", "300"))
        except Exception:
            min_rows = 300
    try:
        conn = sqlite3.connect(str(SOFASCORE_DB_PATH))
        conn.row_factory = sqlite3.Row
        try:
            try:
                from configuracion import TEMPORADA_ID
            except Exception:  # pragma: no cover
                TEMPORADA_ID = None
            row = None
            if TEMPORADA_ID is not None:
                # Solo cuentan las stats de la temporada activa: las de la
                # temporada anterior no pueden hacer que una jornada aparezca
                # como "con resultados" (mezclas de temporadas).
                row = conn.execute(
                    "SELECT COUNT(*) AS c FROM estadisticas_jugador_partido "
                    "WHERE ronda = ? AND temporada_id = ?",
                    (matchday, TEMPORADA_ID),
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT COUNT(*) AS c FROM estadisticas_jugador_partido WHERE ronda = ?",
                    (matchday,),
                ).fetchone()
            return bool(row and row["c"] >= min_rows)
        finally:
            conn.close()
    except Exception:  # pragma: no cover - sin BD de SofaScore / tabla ausente: no cerrar
        return False


def auto_close_matchday(league_id: int) -> dict:
    """
    Cierra automáticamente la jornada actual de una liga si ya hay resultados de
    SofaScore disponibles y todas las partidas de esa ronda están terminadas.
    Devuelve el estado del cierre:

    - {"closed": False, "reason": "no_results_yet"}: aún no hay datos, se espera.
    - {"closed": False, "reason": "matches_in_progress"}: hay partidas sin terminar.
    - {"closed": True, "advanced_to": ...}: se cerró y la liga avanzó.
    """
    with get_conn() as conn:
        league = conn.execute(
            "SELECT id, current_matchday FROM leagues WHERE id = ?", (league_id,)
        ).fetchone()
    if not league:
        return {"league_id": league_id, "closed": False, "reason": "no_league"}
    current = league["current_matchday"]
    if not matchday_results_available(current):
        return {
            "league_id": league_id,
            "matchday": current,
            "closed": False,
            "reason": "no_results_yet",
        }
    if not matchday_all_finished(current):
        return {
            "league_id": league_id,
            "matchday": current,
            "closed": False,
            "reason": "matches_in_progress",
        }
    out = close_matchday(league_id, current)
    return {
        "league_id": league_id,
        "matchday": current,
        "closed": True,
        "advanced_to": out["advanced_to"],
        "results": out["results"],
    }


def matchday_all_finished(matchday: int) -> bool:
    """Devuelve True si todas las partidas de la ronda de la temporada activa están terminadas."""
    try:
        conn = sqlite3.connect(str(SOFASCORE_DB_PATH))
        conn.row_factory = sqlite3.Row
        try:
            from configuracion import TEMPORADA_ID
        except Exception:  # pragma: no cover
            TEMPORADA_ID = None
        if TEMPORADA_ID is None:
            return False
        row = conn.execute(
            "SELECT COUNT(*) AS total, SUM(CASE WHEN estado = 'finished' THEN 1 ELSE 0 END) AS finished "
            "FROM partidos WHERE ronda = ? AND temporada_id = ?",
            (matchday, TEMPORADA_ID),
        ).fetchone()
        return bool(row and row["total"] > 0 and row["finished"] == row["total"])
    except Exception:  # pragma: no cover
        return False
    finally:
        try:
            conn.close()
        except Exception:
            pass


def matchday_progress(matchday: int) -> dict:
    """Devuelve {total, finished} de partidos de la ronda de la temporada activa."""
    try:
        conn = sqlite3.connect(str(SOFASCORE_DB_PATH))
        conn.row_factory = sqlite3.Row
        try:
            from configuracion import TEMPORADA_ID
        except Exception:  # pragma: no cover
            TEMPORADA_ID = None
        if TEMPORADA_ID is None:
            return {"total": 0, "finished": 0}
        row = conn.execute(
            "SELECT COUNT(*) AS total, SUM(CASE WHEN estado = 'finished' THEN 1 ELSE 0 END) AS finished "
            "FROM partidos WHERE ronda = ? AND temporada_id = ?",
            (matchday, TEMPORADA_ID),
        ).fetchone()
        return {"total": row["total"] if row else 0, "finished": row["finished"] if row else 0}
    except Exception:  # pragma: no cover
        return {"total": 0, "finished": 0}
    finally:
        try:
            conn.close()
        except Exception:
            pass


def auto_close_all_leagues() -> list:
    """Recorre todas las ligas activas y cierra la jornada actual si hay resultados."""
    with get_conn() as conn:
        leagues = conn.execute("SELECT id FROM leagues WHERE status = 'active'").fetchall()
    out = []
    for lg in leagues:
        try:
            out.append(auto_close_matchday(lg["id"]))
        except Exception as e:  # pragma: no cover - defensa del loop de fondo
            out.append({"league_id": lg["id"], "closed": False, "reason": f"error: {e}"})
    return out


def get_league_status(league_id: int) -> dict:
    """Estado "en vivo" de la liga para el frontend.

    Devuelve la jornada actual y si ya hay resultados de esa jornada en la
    BD de SofaScore (indica que el auto-cierre la cerrará en poco). Está
    pensado para ser sondeado (polling) de forma ligera desde la app.
    """
    with get_conn() as conn:
        league = conn.execute(
            "SELECT id, current_matchday FROM leagues WHERE id = ?",
            (league_id,),
        ).fetchone()
    if not league:
        raise ValueError("Liga no existe")
    current = league["current_matchday"]
    progress = matchday_progress(current)
    return {
        "league_id": league_id,
        "current_matchday": current,
        "results_available": matchday_results_available(current),
        "matches_total": progress["total"],
        "matches_finished": progress["finished"],
        "can_auto_close": progress["finished"] == progress["total"] and progress["total"] > 0,
    }


def set_league_matchday(league_id: int, matchday: int) -> dict:
    """Actualiza current_matchday de una liga (solo el creador).

    Útil cuando la BD de SofaScore está desactualizada en PythonAnywhere y
    se necesita forzar la jornada actual a mano.
    """
    matchday = int(matchday)
    if matchday < 1 or matchday > MAX_SEASON_MATCHDAY:
        raise ValueError(f"La jornada debe estar entre 1 y {MAX_SEASON_MATCHDAY}")
    with transaction() as conn:
        league = conn.execute("SELECT id, current_matchday FROM leagues WHERE id = ?", (league_id,)).fetchone()
        if not league:
            raise ValueError("Liga no existe")
        old = league["current_matchday"]
        conn.execute("UPDATE leagues SET current_matchday = ? WHERE id = ?", (matchday, league_id))
    return {"league_id": league_id, "old_matchday": old, "new_matchday": matchday}


def get_sofascore_info() -> dict:
    """Info de la BD local de SofaScore para debugging en producción."""
    info = {
        "db_exists": bool(SOFASCORE_DB_PATH.exists()),
        "db_size": SOFASCORE_DB_PATH.stat().st_size if SOFASCORE_DB_PATH.exists() else 0,
        "db_path": str(SOFASCORE_DB_PATH),
        "real_current_matchday": real_current_matchday(),
    }
    if info["db_exists"]:
        try:
            conn = sqlite3.connect(str(SOFASCORE_DB_PATH))
            conn.row_factory = sqlite3.Row
            try:
                from configuracion import TEMPORADA_ID
            except Exception:
                TEMPORADA_ID = None
            # Jornadas disponibles
            rows = conn.execute(
                "SELECT DISTINCT ronda FROM partidos ORDER BY ronda"
            ).fetchall()
            info["available_matchdays"] = [r["ronda"] for r in rows]
            # Cantidad de partidos y resultados
            if TEMPORADA_ID is not None:
                counts = conn.execute(
                    "SELECT ronda, COUNT(*) as total, SUM(CASE WHEN tipo_estado='finished' THEN 1 ELSE 0 END) as finished "
                    "FROM partidos WHERE temporada_id = ? GROUP BY ronda ORDER BY ronda",
                    (TEMPORADA_ID,),
                ).fetchall()
            else:
                counts = conn.execute(
                    "SELECT ronda, COUNT(*) as total, SUM(CASE WHEN tipo_estado='finished' THEN 1 ELSE 0 END) as finished "
                    "FROM partidos GROUP BY ronda ORDER BY ronda"
                ).fetchall()
            info["matchday_details"] = [dict(r) for r in counts]
            # Jugadores
            player_count = conn.execute("SELECT COUNT(*) as c FROM jugadores").fetchone()
            info["player_count"] = player_count["c"] if player_count else 0
            # Equipos
            team_count = conn.execute("SELECT COUNT(*) as c FROM clasificacion").fetchone()
            info["team_count"] = team_count["c"] if team_count else 0
        except Exception as e:
            info["error"] = str(e)
        finally:
            conn.close()
    return info


# ---------- Herramientas de administración de liga (solo el creador) ----------
MIN_MARKET_SIZE = 5
MAX_MARKET_SIZE = 60
MAX_SEASON_MATCHDAY = 39


def _league_user_league_ids(league_id: int, conn: sqlite3.Connection) -> list[int]:
    """IDs de `user_leagues` de los miembros de una liga."""
    return [
        r["id"]
        for r in conn.execute(
            "SELECT id FROM user_leagues WHERE league_id = ?", (league_id,)
        ).fetchall()
    ]


def admin_reset_league(league_id: int, from_matchday: Optional[int] = None) -> dict:
    """Reinicia la liga desde cero (solo el creador):

    - Borra puntos, alineaciones de jornada, mercado, pujas, ofertas,
      movimientos económicos y Supertablón.
    - Restaura los presupuestos al inicial y regenera la plantilla inicial
      de cada equipo.
    - Pone `current_matchday` en `from_matchday` (por defecto, la jornada
      real en curso según SofaScore).
    """
    if from_matchday is None:
        from_matchday = real_current_matchday()
    from_matchday = int(from_matchday)
    if from_matchday < 1 or from_matchday > MAX_SEASON_MATCHDAY:
        raise ValueError(f"La jornada de inicio debe estar entre 1 y {MAX_SEASON_MATCHDAY}")

    with transaction() as conn:
        league = conn.execute("SELECT id FROM leagues WHERE id = ?", (league_id,)).fetchone()
        if not league:
            raise ValueError("Liga no existe")
        ul_ids = _league_user_league_ids(league_id, conn)
        if not ul_ids:
            raise ValueError("La liga no tiene miembros")
        ph = ",".join("?" for _ in ul_ids)

        # Alineaciones y puntos por jornada
        conn.execute(
            "DELETE FROM matchday_lineup WHERE matchday_squad_id IN "
            "(SELECT id FROM matchday_squad WHERE user_league_id IN (%s))" % ph,
            ul_ids,
        )
        conn.execute("DELETE FROM matchday_squad WHERE user_league_id IN (%s)" % ph, ul_ids)
        conn.execute("DELETE FROM matchday_results WHERE league_id = ?", (league_id,))
        # Mercado y pujas
        conn.execute(
            "DELETE FROM bids WHERE market_player_id IN "
            "(SELECT id FROM market_players WHERE league_id = ?)",
            (league_id,),
        )
        conn.execute("DELETE FROM market_players WHERE league_id = ?", (league_id,))
        # Ofertas entre miembros
        conn.execute(
            "DELETE FROM user_offers WHERE from_user_league_id IN (%s) "
            "OR to_user_league_id IN (%s)" % (ph, ph),
            ul_ids + ul_ids,
        )
        # Historial económico, Supertablón y plantillas
        conn.execute("DELETE FROM transactions WHERE user_league_id IN (%s)" % ph, ul_ids)
        conn.execute("DELETE FROM supertablon_entries WHERE league_id = ?", (league_id,))
        conn.execute("DELETE FROM squad_players WHERE user_league_id IN (%s)" % ph, ul_ids)
        conn.execute(
            "UPDATE user_leagues SET budget = ? WHERE league_id = ?",
            (INITIAL_BUDGET, league_id),
        )
        conn.execute(
            "UPDATE leagues SET current_matchday = ? WHERE id = ?",
            (from_matchday, league_id),
        )
        for ul_id in ul_ids:
            generate_initial_squad(ul_id, league_id, conn)
    return {"league_id": league_id, "from_matchday": from_matchday, "teams": len(ul_ids)}


def admin_grant_budget(
    league_id: int, target_user_league_id: int, amount: int, description: str = ""
) -> dict:
    """El creador da (o quita, con cantidad negativa) dinero a un equipo."""
    amount = int(amount)
    if amount == 0:
        raise ValueError("La cantidad no puede ser cero")
    with transaction() as conn:
        league = conn.execute("SELECT id FROM leagues WHERE id = ?", (league_id,)).fetchone()
        if not league:
            raise ValueError("Liga no existe")
        target = conn.execute(
            "SELECT ul.id, u.username FROM user_leagues ul "
            "JOIN users u ON u.id = ul.user_id "
            "WHERE ul.id = ? AND ul.league_id = ?",
            (target_user_league_id, league_id),
        ).fetchone()
        if not target:
            raise ValueError("Ese equipo no pertenece a la liga")
        conn.execute(
            "UPDATE user_leagues SET budget = budget + ? WHERE id = ?",
            (amount, target["id"]),
        )
        conn.execute(
            "INSERT INTO transactions (user_league_id, type, amount, related_player_id, description) "
            "VALUES (?, 'admin_grant', ?, NULL, ?)",
            (
                target["id"],
                amount,
                description or f"Ayuda del creador de la liga ({amount:+d}€)",
            ),
        )
        budget = conn.execute(
            "SELECT budget FROM user_leagues WHERE id = ?", (target["id"],)
        ).fetchone()["budget"]
    return {"user_league_id": target["id"], "amount": amount, "budget": budget}


def admin_set_market_size(league_id: int, market_size: int) -> int:
    """Cambia el nº de jugadores que publica el mercado de la liga."""
    market_size = int(market_size)
    if market_size < MIN_MARKET_SIZE or market_size > MAX_MARKET_SIZE:
        raise ValueError(
            f"El tamaño del mercado debe estar entre {MIN_MARKET_SIZE} y {MAX_MARKET_SIZE}"
        )
    with transaction() as conn:
        league = conn.execute("SELECT id FROM leagues WHERE id = ?", (league_id,)).fetchone()
        if not league:
            raise ValueError("Liga no existe")
        conn.execute("UPDATE leagues SET market_size = ? WHERE id = ?", (market_size, league_id))
    return market_size


def _market_size_for(league_id: int, conn: Optional[sqlite3.Connection] = None) -> int:
    """Tamaño de mercado efectivo de la liga (su override o el global)."""
    def _read(c: sqlite3.Connection) -> int:
        row = c.execute("SELECT market_size FROM leagues WHERE id = ?", (league_id,)).fetchone()
        size = row["market_size"] if row and row["market_size"] else 0
        return size if size > 0 else MARKET_SIZE

    if conn is not None:
        return _read(conn)
    with get_conn() as own:
        return _read(own)


def admin_list_transfers(league_id: int, limit: int = 15) -> dict:
    """Últimos 'fichajes' cancelables por el creador:

    - `purchases`: pujas ganadas del mercado (transactions tipo 'purchase').
    - `offers`: ofertas aceptadas entre miembros.
    """
    with get_conn() as conn:
        ul_ids = _league_user_league_ids(league_id, conn)
        if not ul_ids:
            return {"purchases": [], "offers": []}
        ph = ",".join("?" for _ in ul_ids)
        purchases = [
            dict(r)
            for r in conn.execute(
                """
                SELECT t.id, p.name AS player, ul.team_name, t.amount, t.created_at
                FROM transactions t
                JOIN players p ON p.id = t.related_player_id
                JOIN user_leagues ul ON ul.id = t.user_league_id
                WHERE t.type = 'purchase' AND t.user_league_id IN (%s)
                ORDER BY t.id DESC LIMIT ?
                """ % ph,
                [*ul_ids, limit],
            ).fetchall()
        ]
        offer_rows = conn.execute(
            """
            SELECT o.id, pt.name AS target_player, o.offered_player_ids,
                   o.offered_cash, o.responded_at,
                   ou.team_name AS offerer_team, tu.team_name AS target_team
            FROM user_offers o
            JOIN user_leagues ou ON ou.id = o.from_user_league_id
            JOIN user_leagues tu ON tu.id = o.to_user_league_id
            JOIN players pt ON pt.id = o.target_player_id
            WHERE o.status = 'accepted'
              AND (o.from_user_league_id IN (%s) OR o.to_user_league_id IN (%s))
            ORDER BY o.id DESC LIMIT ?
            """ % (ph, ph),
            [*ul_ids, *ul_ids, limit],
        ).fetchall()
        offers: list[dict] = []
        for r in offer_rows:
            try:
                ids = [int(i) for i in json.loads(r["offered_player_ids"] or "[]")]
            except Exception:
                ids = []
            if ids:
                names = {
                    rr["id"]: rr["name"]
                    for rr in conn.execute(
                        "SELECT id, name FROM players WHERE id IN (%s)"
                        % ",".join("?" for _ in ids),
                        ids,
                    ).fetchall()
                }
            else:
                names = {}
            d = dict(r)
            d["offered_players"] = [names.get(i, f"Jugador {i}") for i in ids]
            offers.append(d)
    return {"purchases": purchases, "offers": offers}


def admin_cancel_transfer(league_id: int, kind: str, ref_id: int) -> dict:
    """Deshace un fichaje mal hecho (solo el creador):

    - `kind='purchase'`: `ref_id` es el id de la transacción 'purchase'. El
      jugador sale de la plantilla y el dinero vuelve al presupuesto.
    - `kind='offer'`: `ref_id` es el id de la oferta aceptada. Jugadores y
      dinero vuelven a su estado anterior.
    """
    with transaction() as conn:
        league = conn.execute("SELECT id FROM leagues WHERE id = ?", (league_id,)).fetchone()
        if not league:
            raise ValueError("Liga no existe")

        if kind == "purchase":
            t = conn.execute(
                """
                SELECT t.id, t.user_league_id, t.amount, t.related_player_id
                FROM transactions t
                JOIN user_leagues ul ON ul.id = t.user_league_id
                WHERE t.id = ? AND t.type = 'purchase' AND ul.league_id = ?
                """,
                (ref_id, league_id),
            ).fetchone()
            if not t:
                raise ValueError("Compra no encontrada (o no es cancelable)")
            player_id = t["related_player_id"]
            paid = -t["amount"] if t["amount"] and t["amount"] < 0 else 0
            sp = conn.execute(
                "SELECT id FROM squad_players WHERE player_id = ? AND user_league_id = ?",
                (player_id, t["user_league_id"]),
            ).fetchone()
            if not sp:
                raise ValueError("Ese jugador ya no está en la plantilla")
            conn.execute("DELETE FROM squad_players WHERE id = ?", (sp["id"],))
            if paid:
                conn.execute(
                    "UPDATE user_leagues SET budget = budget + ? WHERE id = ?",
                    (paid, t["user_league_id"]),
                )
                conn.execute(
                    "INSERT INTO transactions (user_league_id, type, amount, related_player_id, description) "
                    "VALUES (?, 'admin_cancel', ?, ?, ?)",
                    (
                        t["user_league_id"],
                        paid,
                        player_id,
                        "Fichaje cancelado por el creador: dinero devuelto",
                    ),
                )
            return {"kind": "purchase", "cancelled": True, "player_id": player_id}

        if kind == "offer":
            o = conn.execute(
                """
                SELECT o.* FROM user_offers o
                JOIN user_leagues ul ON ul.id = o.from_user_league_id
                WHERE o.id = ? AND o.status = 'accepted' AND ul.league_id = ?
                """,
                (ref_id, league_id),
            ).fetchone()
            if not o:
                raise ValueError("Oferta aceptada no encontrada")
            try:
                offered_ids = [int(i) for i in json.loads(o["offered_player_ids"] or "[]")]
            except Exception:
                offered_ids = []

            # El jugador objetivo vuelve a su equipo original
            moved = conn.execute(
                "UPDATE squad_players SET user_league_id = ?, contract_weeks = ? "
                "WHERE player_id = ? AND user_league_id = ?",
                (
                    o["to_user_league_id"],
                    CONTRACT_WEEKS,
                    o["target_player_id"],
                    o["from_user_league_id"],
                ),
            ).rowcount
            if not moved:
                raise ValueError("El jugador ya no está en la plantilla de quien lo fichó")
            # Los jugadores ofrecidos vuelven al que los ofertó
            if offered_ids:
                ph = ",".join("?" for _ in offered_ids)
                conn.execute(
                    "UPDATE squad_players SET user_league_id = ?, contract_weeks = ? "
                    "WHERE player_id IN (%s) AND user_league_id = ?" % ph,
                    [o["from_user_league_id"], CONTRACT_WEEKS] + offered_ids
                    + [o["to_user_league_id"]],
                )
            # El dinero se invierte
            if o["offered_cash"]:
                conn.execute(
                    "UPDATE user_leagues SET budget = budget + ? WHERE id = ?",
                    (o["offered_cash"], o["from_user_league_id"]),
                )
                conn.execute(
                    "UPDATE user_leagues SET budget = budget - ? WHERE id = ?",
                    (o["offered_cash"], o["to_user_league_id"]),
                )
            conn.execute(
                "INSERT INTO transactions (user_league_id, type, amount, related_player_id, description) "
                "VALUES (?, 'admin_cancel', ?, ?, ?)",
                (
                    o["from_user_league_id"],
                    -o["offered_cash"] if o["offered_cash"] else 0,
                    o["target_player_id"],
                    "Oferta cancelada por el creador: jugadores y dinero revertidos",
                ),
            )
            conn.execute("UPDATE user_offers SET status = 'cancelled' WHERE id = ?", (o["id"],))
            return {"kind": "offer", "cancelled": True, "player_id": o["target_player_id"]}

        raise ValueError("Tipo no válido: usa 'purchase' o 'offer'")


# ---------- Contracts & Supertablón ----------
def run_nightly_maintenance(league_id: int = 1) -> dict:
    """Run nightly tasks: decrement contracts, handle expirations, generate Supertablón."""
    with transaction() as conn:
        # 1. Decrement contract weeks for all players in league
        expiring = conn.execute(
            """
            SELECT sp.player_id, sp.user_league_id, sp.contract_weeks,
                   p.name, ul.team_name
            FROM squad_players sp
            JOIN players p ON p.id = sp.player_id
            JOIN user_leagues ul ON ul.id = sp.user_league_id
            WHERE ul.league_id = ? AND sp.contract_weeks > 0
            """,
            (league_id,)
        ).fetchall()

        expiring_soon = []
        for row in expiring:
            new_weeks = row["contract_weeks"] - 1
            if new_weeks <= 0:
                # Contract expired - player goes to market
                conn.execute("DELETE FROM squad_players WHERE player_id = ? AND user_league_id = ?",
                           (row["player_id"], row["user_league_id"]))
                # Add to market for tomorrow
                tomorrow = (dt_date.today() + timedelta(days=1)).isoformat()
                expires = (dt_datetime.now() + timedelta(days=2)).replace(hour=5, minute=0, second=0, microsecond=0).isoformat()
                conn.execute(
                    "INSERT INTO market_players (league_id, player_id, listed_date, expires_at) VALUES (?, ?, ?, ?)",
                    (league_id, row["player_id"], tomorrow, expires)
                )
                # Compensación al expirar (Canon §6: % del valor de mercado, calibrable)
                player = conn.execute("SELECT market_value FROM players WHERE id = ?", (row["player_id"],)).fetchone()
                compensation = int(player["market_value"] * CONTRACT_COMPENSATION) if player else 0
                conn.execute("UPDATE user_leagues SET budget = budget + ? WHERE id = ?",
                           (compensation, row["user_league_id"]))
                conn.execute(
                    "INSERT INTO transactions (user_league_id, type, amount, related_player_id, description) VALUES (?, 'contract_compensation', ?, ?, ?)",
                    (row["user_league_id"], compensation, row["player_id"], f"Contract expired: {row['name']}")
                )
            else:
                conn.execute(
                    "UPDATE squad_players SET contract_weeks = ? WHERE player_id = ? AND user_league_id = ?",
                    (new_weeks, row["player_id"], row["user_league_id"])
                )
                if new_weeks <= 2:
                    expiring_soon.append({
                        "player": row["name"],
                        "team": row["team_name"],
                        "weeks_left": new_weeks
                    })

        # 2. Generate Supertablón entries from recent events
        # Market results (yesterday's resolved bids)
        yesterday_dt = dt_date.today() - timedelta(days=1)
        yesterday = yesterday_dt.isoformat()
        market_results = conn.execute(
            """
            SELECT mp.player_id, p.name, mp.listed_date,
                   b.user_league_id, b.amount, ul.team_name as buyer_team
            FROM market_players mp
            JOIN bids b ON b.market_player_id = mp.id AND b.is_winning = 1
            JOIN players p ON p.id = mp.player_id
            JOIN user_leagues ul ON ul.id = b.user_league_id
            WHERE mp.league_id = ? AND mp.listed_date = ? AND mp.status = 'sold'
            """,
            (league_id, yesterday)
        ).fetchall()

        for mr in market_results:
            conn.execute(
                """INSERT INTO supertablon_entries (league_id, type, title, content, related_user_league_ids, related_player_ids, matchday)
                VALUES (?, 'mercado', ?, ?, ?, ?, ?)""",
                (league_id,
                 f"💰 {mr['buyer_team']} ficha a {mr['name']}",
                 f"Ha pagado {mr['amount']:,} € por {mr['name']}. El mercado se mueve.".replace(",", "."),
                 json.dumps([mr["user_league_id"]]),
                 json.dumps([mr["player_id"]]),
                 None)
            )

        # User offers accepted
        recent_offers = conn.execute(
            """
            SELECT o.*, ul_from.team_name as from_team, ul_to.team_name as to_team, p.name as player_name
            FROM user_offers o
            JOIN user_leagues ul_from ON ul_from.id = o.from_user_league_id
            JOIN user_leagues ul_to ON ul_to.id = o.to_user_league_id
            JOIN players p ON p.id = o.target_player_id
            WHERE ul_from.league_id = ? AND o.status = 'accepted' AND o.responded_at >= ?
            """,
            (league_id, yesterday)
        ).fetchall()
        for of in recent_offers:
            conn.execute(
                """INSERT INTO supertablon_entries (league_id, type, title, content, related_user_league_ids, related_player_ids, matchday)
                VALUES (?, 'salseo', ?, ?, ?, ?, ?)""",
                (league_id,
                 f"🤝 Intercambio entre {of['from_team']} y {of['to_team']}",
                 f"{of['from_team']} cede a {of['player_name']} a cambio de {of['offered_cash']:,} €.".replace(",", "."),
                 json.dumps([of["from_user_league_id"], of["to_user_league_id"]]),
                 json.dumps([of["target_player_id"]]),
                 None)
            )

        # Highlights: top scorers, biggest wins, etc. (simplified)
        # Could add more sophisticated narrative generation here

    return {
        "expiring_soon": expiring_soon,
        "market_results_count": len(market_results),
        "offers_accepted": len(recent_offers)
    }