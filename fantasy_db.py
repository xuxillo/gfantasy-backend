from __future__ import annotations

TEMPORADA_ID = 1
FANTASY_DB_PATH = "sofascore.db"

import sqlite3
import json
from datetime import datetime
from typing import Any, Optional


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


def init_db(db_path: str = "sofascore.db") -> sqlite3.Connection:
    conexion = sqlite3.connect(db_path)
    return conexion


def get_conn(db_path: str = "sofascore.db") -> sqlite3.Connection:
    return sqlite3.connect(db_path)


def hash_password(contrasena: str) -> str:
    import hashlib
    return hashlib.sha256(contrasena.encode()).hexdigest()


def verify_password(contrasena_guardada: str, contrasena_ingresada: str) -> bool:
    import hashlib
    return hash_password(contrasena_ingresada) == contrasena_guardada


def generate_invite_code() -> str:
    import secrets
    import string
    caracteres = string.ascii_uppercase + string.digits
    return ''.join(secrets.choice(caracteres) for _ in range(8))


def now_iso() -> str:
    return datetime.now().isoformat()


def transaction(db_path: str = "sofascore.db") -> None:
    """Función de transacción básica para compatibilidad con fantasy_services.

    Args:
        db_path: Ruta a la base de datos SQLite.
    """
    pass


def needs_rehash() -> bool:
    """Verifica si es necesario rehash de contraseñas.

    Returns:
        False por defecto (compatibilidad).
    """
    return False