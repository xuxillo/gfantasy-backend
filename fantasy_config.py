# fantasy_config.py
from __future__ import annotations

import os
from pathlib import Path

# --- .env (configuración local, ignorada por git): GURUTZE_EMAIL_MODE,
# GURUTZE_GMAIL_*, GURUTZE_FRONTEND_URL, etc.
# Se carga al importar el módulo; las variables ya definidas en el entorno
# tienen prioridad (os.environ.setdefault).
_ENV_FILE = Path(__file__).resolve().parent / ".env"
if _ENV_FILE.exists():
    for _line in _ENV_FILE.read_text(encoding="utf-8").splitlines():
        _line = _line.strip()
        if not _line or _line.startswith("#") or "=" not in _line:
            continue
        _key, _value = _line.split("=", 1)
        os.environ.setdefault(_key.strip(), _value.strip().strip('"').strip("'"))

# Fantasy DB (separate from SofaScore DB)
# GURUTZE_FANTASY_DB permite apuntar a otra base de datos (usado por los tests
# para no tocar nunca la BD real del juego).
FANTASY_DB_DIR = Path(__file__).resolve().parent / "data_fantasy"
FANTASY_DB_PATH = (
    Path(os.environ["GURUTZE_FANTASY_DB"])
    if os.environ.get("GURUTZE_FANTASY_DB")
    else FANTASY_DB_DIR / "fantasy.db"
)
FANTASY_DB_PATH.parent.mkdir(parents=True, exist_ok=True)

# SofaScore DB
SOFASCORE_DB_DIR = Path(__file__).resolve().parent / "data"
SOFASCORE_DB_PATH = SOFASCORE_DB_DIR / "sofascore.db"
SOFASCORE_DB_DIR.mkdir(parents=True, exist_ok=True)

# Raíz de imágenes de SofaScore (fotos de jugadores y escudos de clubes).
# Las rutas guardadas en imagenes_jugadores/imagenes_equipos son relativas a
# este directorio, p. ej. "imagenes/jugadores/19013.webp". El backend las sirve
# en /sofa/... y la web la hace proxy (ver next.config.js).
# Se usa la carpeta de temporada actual (configuracion.TEMPORADA_CARPEDA = "26-27")
# para mantener coherencia con el extractor de imágenes.
try:
    from configuracion import TEMPORADA_CARPEDA
except Exception:  # pragma: no cover
    TEMPORADA_CARPEDA = "26-27"
IMAGES_ROOT = Path(__file__).resolve().parent / "output" / TEMPORADA_CARPEDA

# SofaScore API base (internal). En PythonAnywhere NO está corriendo, así que
# se puede desactivar con SOFASCORE_API="" (o "disabled") para saltar los
# timeouts de 30 s. La CDN externa funciona como fallback.
_raw_api = os.environ.get("SOFASCORE_API", "http://127.0.0.1:8000")
SOFASCORE_API = "" if _raw_api.lower() in ("", "disabled", "none") else _raw_api

# SoffaScore CDN externo (fallback cuando la API interna no esta disponible)
SOFASCORE_CDN = "https://api.sofascore.com/api/v1"

# Fantasy constants
INITIAL_BUDGET = 50_000_000  # 50M €
MAX_SQUAD_SIZE = 25
CONVOCATORIA_SIZE = 18
CONTRACT_WEEKS = 10
MAX_CHANGES_PER_MATCHDAY = 3  # Canon §3: 3 cambios por jornada
PUFO_PERCENT = 0.20  # Canon §5.1: hasta el 20% del VALOR DE PLANTILLA
TOP_20_EXCLUDE = 20  # Top 20 por valor: fuera de la plantilla inicial (Canon §2), SÍ fichables en el mercado
CAPTAIN_BONUS = 2  # Canon §3: el capitán añade +2 puntos (no multiplica)
MARKET_SIZE = 20  # Jugadores distintos listados por día
MARKET_STARS = 5  # De los 20, cuántos son "cracks" (los más caros disponibles)
CONTRACT_COMPENSATION = 0.10  # Canon §6: compensación al expirar contrato (% del valor)

# --- Economía de la jornada (Canon §8) ---
# VALORES DE REFERENCIA, calibrables. El Canon los deja "pendiente de
# simulación" (§12); se usan estos importes de referencia como arranque.
ECON_INCOME_PER_POINT = 50_000        # Ingresos por cada punto de la jornada (§8)
ECON_GOAL_BONUS = 50_000              # Premio por gol marcado por tus titulares (§8: goles)
ECON_RED_PENALTY = 25_000             # Pequeña penalización por cada tarjeta roja (§8)
ECON_MVP_MATCHDAY_BONUS = 500_000     # Bono al propietario del MVP de la jornada (§8)
ECON_CLASSIFICATION_LOW = 200_000     # Compensación por clasificación: 1er puesto (§8)
ECON_CLASSIFICATION_HIGH = 1_000_000  # Compensación por clasificación: último (§8, favorece al último)

# --- Sync de jugadores ---
# Solo se sincronizan jugadores de los clubes de la temporada activa.
SYNC_REQUIRE_ACTIVE_CLUB = True

# Scoring (base from Canon §4.2) - CALIBRATED for LaLiga 26/27
# Eventos universales: se aplican a cualquier posición.
SCORING_BASE = {
    "goal": {"DEL": 4, "MED": 5, "DEF": 6, "POR": 7},
    "assist": {"DEL": 3, "MED": 4, "DEF": 5, "POR": 6},
    "penalty_scored": 3,
    "penalty_missed": -2,
    "penalty_conceded": -2,
    "yellow": -2,
    "double_yellow": -4,
    "red": -6,
    "clean_sheet": {"POR": 2, "DEF": 2, "MED": 0, "DEL": 0},  # Canon §4.3
    "penalty_saved": 4,  # Canon §4.3
    "own_goal": -6,
    # Canon §4.3: la portería a cero es el mecanismo para penalizar goles encajados.
    "goals_conceded": {"POR": 0, "DEF": 0, "MED": 0, "DEL": 0},
    # Falta cometida: -1 cada 3 (universal, pendiente de calibración).
    "fouls_committed_per_3": -1,
    "captain_bonus": 2,  # Canon §3: el capitán suma +2 puntos
}

# Canon §4.3 - Bonus por posición/rol.
# Las acciones defensivas de los medios usan bloques más bajos (mediodefensivo).
# Los medios ofensivos/extremos/delanteros no se penalizan por pérdidas.
# Las acciones que no encajan en la posición del jugador NO puntúan.
# Los tamaños de bloque se mantienen como parámetros de calibración.
POSITION_BONUSES = {
    "POR": {
        "saves_per_3": 1,            # paradas (pendiente de calibración)
    },
    "DEF": {
        "clearances_per_5": 1,       # +1 cada 5 despejes (Canon §4.3)
        "interceptions_per_5": 1,    # +1 cada 5 intercepciones
        "tackles_won_per_3": 1,      # +1 cada 3 duelos ganados
        "aerials_won": 1,            # +1 por duelo aéreo ganado
        "blocked_scoring_attempt": 1,
    },
    "MED": {
        "key_passes_per_2": 1,       # +1 cada 2 pases clave (Canon §4.3)
        "shots_on_target": 1,        # tiros a puerta
        "tackles_won_per_3": 1,      # acciones defensivas con bloque más bajo
        "interceptions_per_5": 1,
        "ball_recovery_per_5": 1,
        "dribbles_completed": 1,
    },
    "DEL": {
        "aerials_won": 1,            # Canon §4.3: duelos aéreos ganados
        "key_passes_per_2": 1,
        "shots_on_target": 1,
        "dribbles_completed": 1,     # extremos
    },
}

# Nombre del atributo de PlayerMatchStats asociado a cada bonus.
BONUS_STAT_ATTRS = {
    "interceptions_per_5": "interceptions",
    "clearances_per_5": "clearances",
    "ball_recovery_per_5": "ball_recovery",
    "tackles_won_per_3": "tackles_won",
    "saves_per_3": "saves",
    "key_passes_per_2": "key_passes",
    "fouls_suffered_per_2": "fouls_suffered",
    "fouls_committed_per_3": "fouls_committed",
    "dribbles_completed": "dribbles_completed",
    "aerials_won": "aerials_won",
    "shots_on_target": "shots_on_target",
    "shots_off_target": "shots_off_target",
    "blocked_scoring_attempt": "blocked_scoring_attempt",
    "penalties_saved": "penalties_saved",
}

# Tamaño de bloque para los bonus que puntúan cada N acciones.
BONUS_BLOCK_SIZES = {
    "interceptions_per_5": 5,
    "clearances_per_5": 5,
    "ball_recovery_per_5": 5,
    "tackles_won_per_3": 3,
    "saves_per_3": 3,
    "key_passes_per_2": 2,
    "fouls_suffered_per_2": 2,
    "fouls_committed_per_3": 3,
}

# Position mapping (Canon §2)
POSITION_ROLES = {
    "POR": ["Portero"],
    "DEF": ["Central", "Lateral"],
    "MED": ["MedioDefensivo", "MedioOfensivo", "Extremo"],
    "DEL": ["Extremo", "DelanteroCentro"],
}

# Formation templates (position counts)
FORMATIONS = {
    "4-4-2": {"POR": 1, "DEF": 4, "MED": 4, "DEL": 2},
    "4-3-3": {"POR": 1, "DEF": 4, "MED": 3, "DEL": 3},
    "3-5-2": {"POR": 1, "DEF": 3, "MED": 5, "DEL": 2},
    "4-5-1": {"POR": 1, "DEF": 4, "MED": 5, "DEL": 1},
    "3-4-3": {"POR": 1, "DEF": 3, "MED": 4, "DEL": 3},
    "5-3-2": {"POR": 1, "DEF": 5, "MED": 3, "DEL": 2},
    "4-2-3-1": {"POR": 1, "DEF": 4, "MED": 5, "DEL": 1},
    "5-4-1": {"POR": 1, "DEF": 5, "MED": 4, "DEL": 1},
}

DEFAULT_FORMATION = "4-4-2"

# --- Supabase Cloud Configuration ---
# Credenciales obtenidas al crear un proyecto en Supabase (gratis)
# Configura aquí tu URL y Key si quieres usar BD en la nube
# Si dejas estos valores vacíos, usará la base de datos local (data_fantasy/fantasy.db)
SUPABASE_URL = "https://gtrndlsulhyoahrgmpfg.supabase.co"
SUPABASE_KEY = "sb_publishable_QQGKuyQmmiVS9JWC4E83-w_n0hwrBTI"
DB_PASSWORD = "FantasyXuxillo23!"

# --- Sync de jugadores ---