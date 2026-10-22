from __future__ import annotations

import sqlite3
import json
from contextlib import contextmanager
from datetime import datetime
from typing import Any, Optional

# Import canonical DB path from config to ensure single source of truth.
from fantasy_config import SOFASCORE_DB_PATH

TEMPORADA_ID = 1
FANTASY_DB_PATH = "sofascore.db"


def obtener_eventos_con_incidentes(
    conexion: sqlite3.Connection, temporada_id: int = TEMPORADA_ID
) -> set[int]:
    cursor = conexion.cursor()
    query = "JOIN partidos p ON i.evento_id = p.id WHERE p.temporada_id = ?"
    cursor.execute(query, (temporada_id,))
    rows = cursor.fetchall()
    return {row[0] for row in rows}


def guardar_fila_clasificacion(
    conexion: sqlite3.Connection, fila: Any, torneo_id: int, temporada_id: int
) -> None:
    pass


SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT UNIQUE NOT NULL,
    email TEXT UNIQUE,
    password_hash TEXT NOT NULL,
    created_at TEXT DEFAULT (datetime('now')),
    avatar_url TEXT
);

CREATE TABLE IF NOT EXISTS leagues (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    owner_id INTEGER NOT NULL REFERENCES users(id),
    invite_code TEXT UNIQUE NOT NULL,
    season_start_date TEXT,
    current_matchday INTEGER DEFAULT 1,
    created_at TEXT DEFAULT (datetime('now')),
    status TEXT DEFAULT 'active'
);

CREATE TABLE IF NOT EXISTS user_leagues (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id),
    league_id INTEGER NOT NULL REFERENCES leagues(id),
    team_name TEXT NOT NULL,
    budget INTEGER DEFAULT 50000000,
    joined_at TEXT DEFAULT (datetime('now')),
    UNIQUE(user_id, league_id)
);

CREATE TABLE IF NOT EXISTS players (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    short_name TEXT,
    position TEXT NOT NULL,
    role TEXT,
    club_id INTEGER,
    club_name TEXT,
    market_value INTEGER DEFAULT 0,
    updated_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS squad_players (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_league_id INTEGER NOT NULL REFERENCES user_leagues(id),
    player_id INTEGER NOT NULL REFERENCES players(id),
    contract_weeks INTEGER DEFAULT 10,
    purchase_price INTEGER,
    purchased_at TEXT DEFAULT (datetime('now')),
    status TEXT DEFAULT 'squad'
);

CREATE TABLE IF NOT EXISTS matchday_squad (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_league_id INTEGER NOT NULL REFERENCES user_leagues(id),
    matchday INTEGER NOT NULL,
    formation TEXT DEFAULT '4-4-2',
    captain_player_id INTEGER REFERENCES players(id),
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now')),
    UNIQUE(user_league_id, matchday)
);

CREATE TABLE IF NOT EXISTS matchday_lineup (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    matchday_squad_id INTEGER NOT NULL REFERENCES matchday_squad(id),
    player_id INTEGER NOT NULL REFERENCES players(id),
    position_slot TEXT NOT NULL,
    is_starter INTEGER DEFAULT 1,
    slot_order INTEGER DEFAULT 0,
    UNIQUE(matchday_squad_id, player_id)
);

CREATE TABLE IF NOT EXISTS market_players (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    league_id INTEGER NOT NULL REFERENCES leagues(id),
    player_id INTEGER NOT NULL REFERENCES players(id),
    listed_date TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    status TEXT DEFAULT 'active',
    UNIQUE(league_id, player_id, listed_date)
);

CREATE TABLE IF NOT EXISTS bids (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    market_player_id INTEGER NOT NULL REFERENCES market_players(id),
    user_league_id INTEGER NOT NULL REFERENCES user_leagues(id),
    amount INTEGER NOT NULL,
    created_at TEXT DEFAULT (datetime('now')),
    is_winning INTEGER DEFAULT 0,
    resolved INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS user_offers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    from_user_league_id INTEGER NOT NULL REFERENCES user_leagues(id),
    to_user_league_id INTEGER NOT NULL REFERENCES user_leagues(id),
    target_player_id INTEGER NOT NULL REFERENCES players(id),
    offered_cash INTEGER DEFAULT 0,
    offered_player_ids TEXT DEFAULT '[]',
    status TEXT DEFAULT 'pending',
    created_at TEXT DEFAULT (datetime('now')),
    responded_at TEXT
);

CREATE TABLE IF NOT EXISTS transactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_league_id INTEGER NOT NULL REFERENCES user_leagues(id),
    type TEXT NOT NULL,
    amount INTEGER NOT NULL,
    related_player_id INTEGER REFERENCES players(id),
    description TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS matchday_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    league_id INTEGER NOT NULL REFERENCES leagues(id),
    matchday INTEGER NOT NULL,
    user_league_id INTEGER NOT NULL REFERENCES user_leagues(id),
    points INTEGER DEFAULT 0,
    goals INTEGER DEFAULT 0,
    assists INTEGER DEFAULT 0,
    clean_sheets INTEGER DEFAULT 0,
    mvp_player_id INTEGER REFERENCES players(id),
    finished_at TEXT,
    UNIQUE(league_id, matchday, user_league_id)
);

CREATE TABLE IF NOT EXISTS supertablon_entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    league_id INTEGER NOT NULL REFERENCES leagues(id),
    type TEXT NOT NULL,
    title TEXT NOT NULL,
    content TEXT NOT NULL,
    related_user_league_ids TEXT DEFAULT '[]',
    related_player_ids TEXT DEFAULT '[]',
    matchday INTEGER,
    created_at TEXT DEFAULT (datetime('now')),
    read_by TEXT DEFAULT '[]'
);

CREATE TABLE IF NOT EXISTS scoring_params (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    league_id INTEGER NOT NULL REFERENCES leagues(id),
    param_key TEXT NOT NULL,
    param_value REAL NOT NULL,
    UNIQUE(league_id, param_key)
);

CREATE TABLE IF NOT EXISTS read_reminders (
    user_league_id INTEGER NOT NULL REFERENCES user_leagues(id),
    key TEXT NOT NULL,
    read_at TEXT DEFAULT (datetime('now')),
    PRIMARY KEY (user_league_id, key)
);

CREATE TABLE IF NOT EXISTS password_reset_tokens (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id),
    token_hash TEXT UNIQUE NOT NULL,
    email TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    used_at TEXT
);
"""

INDEXES = """
CREATE INDEX IF NOT EXISTS idx_squad_players_user_league ON squad_players(user_league_id);
CREATE INDEX IF NOT EXISTS idx_squad_players_player ON squad_players(player_id);
CREATE INDEX IF NOT EXISTS idx_matchday_lineup_squad ON matchday_lineup(matchday_squad_id);
CREATE INDEX IF NOT EXISTS idx_bids_market_player ON bids(market_player_id);
CREATE INDEX IF NOT EXISTS idx_bids_user_unresolved ON bids(user_league_id, resolved);
CREATE INDEX IF NOT EXISTS idx_user_offers_to ON user_offers(to_user_league_id);
CREATE INDEX IF NOT EXISTS idx_transactions_user ON transactions(user_league_id);
CREATE INDEX IF NOT EXISTS idx_password_reset_tokens_user ON password_reset_tokens(user_id);
"""

# Columnas añadidas después de la primera versión del esquema (idempotentes).
MIGRATIONS = [
    ("bids", "resolved", "INTEGER DEFAULT 0"),
    ("leagues", "market_size", "INTEGER DEFAULT 0"),
]


def _apply_migrations(conn: sqlite3.Connection) -> None:
    """Añade columnas nuevas a bases de datos ya existentes (idempotente)."""
    for table, column, definition in MIGRATIONS:
        exists = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
        ).fetchone()
        if not exists:
            continue
        columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        if column not in columns:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def init_db(db_path: str = str(SOFASCORE_DB_PATH)) -> sqlite3.Connection:
    """Crea el esquema de la BD si no existe y aplica migraciones."""
    conexion = sqlite3.connect(db_path, check_same_thread=False)
    conexion.row_factory = sqlite3.Row
    conexion.executescript(SCHEMA)
    _apply_migrations(conexion)
    conexion.executescript(INDEXES)
    conexion.commit()
    return conexion


def get_conn(db_path: str = str(SOFASCORE_DB_PATH)) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def hash_password(contrasena: str) -> str:
    import hashlib
    return hashlib.sha256(contrasena.encode()).hexdigest()


def verify_password(contrasena_guardada: str, contrasena_ingresada: str) -> bool:
    """Verifica que una contraseña en texto plano coincida con el hash guardado.

    Soporta PBKDF2 (formato antiguo) y SHA256 (formato actual).
    """
    import hashlib, secrets
    if not contrasena_ingresada:
        return False
    if contrasena_ingresada.startswith("pbkdf2_sha256$"):
        try:
            _, iters, salt_hex, digest_hex = contrasena_ingresada.split("$")
            digest = hashlib.pbkdf2_hmac(
                "sha256", contrasena_guardada.encode(), bytes.fromhex(salt_hex), int(iters)
            )
            return secrets.compare_digest(digest.hex(), digest_hex)
        except (ValueError, TypeError):
            return False
    return hash_password(contrasena_guardada) == contrasena_ingresada


def generate_invite_code() -> str:
    import secrets
    import string
    caracteres = string.ascii_uppercase + string.digits
    return ''.join(secrets.choice(caracteres) for _ in range(8))


def now_iso() -> str:
    return datetime.now().isoformat()


@contextmanager
def transaction(db_path: str = str(SOFASCORE_DB_PATH)):
    """Context manager que yield una conexión sqlite y commit/rollback automático.

    Args:
        db_path: Ruta a la base de datos SQLite.
    """
    conn = get_conn(db_path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def needs_rehash(password_hash: str = "") -> bool:
    """Verifica si es necesario rehash de contraseñas.

    Args:
        password_hash: Hash guardado actual de la contraseña.

    Returns:
        False por defecto (compatibilidad con código que no pasa el hash).
    """
    return False