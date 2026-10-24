from __future__ import annotations

from pathlib import Path

# ============================================================
# API SOFASCORE
# ============================================================
API_BASE = "https://api.sofascore.com/api/v1"

# ============================================================
# TORNEO / TEMPORADA POR DEFECTO
# ============================================================

# LaLiga 26/27
TORNEO_ID = 8
TEMPORADA_ID = 97268
TORNEO_NOMBRE = "LaLiga"
TEMPORADA_NOMBRE = "LaLiga 26/27"
TEMPORADA_CARPETA = "26-27"

# ============================================================
# JUGADOR POR DEFECTO (para pruebas)
# ============================================================
JUGADOR_ID = 1402912
JUGADOR_NOMBRE = "Lamine Yamal"

# ============================================================
# HTTP / LIMITES DE PETICIONES
# ============================================================
PAUSA_SEGUNDOS = 8.0
JITTER_MAXIMO = 3.0
INTERVALO_MINIMO_PETICION = 4.0
TIEMPO_ESPERA = 60
MAX_PAGINAS_PARTIDOS_JUGADOR = 10
MAX_PAGINAS_LAST_EQUIPO = 5
MAX_PAGINAS_NEXT_EQUIPO = 2
PAGINAS_SIN_LALIGA_CONSECUTIVAS = 2
REINTENTOS_MAXIMOS = 5
TIEMPO_REINTENTO_BASE = 10

# ============================================================
# RUTAS
# ============================================================
RAIZ = Path(__file__).resolve().parent
DIRECTORIO_DATOS = RAIZ / "data"
DIRECTORIO_SALIDA = RAIZ / "output"
DIRECTORIO_RESULTADOS = RAIZ / "resultados"
DIRECTORIO_IMAGENES = DIRECTORIO_SALIDA / TEMPORADA_CARPETA
RUTA_BD = DIRECTORIO_DATOS / "sofascore.db"
RUTA_IMAGENES_EQUIPOS = DIRECTORIO_IMAGENES / "equipos"
RUTA_IMAGENES_JUGADORES = DIRECTORIO_IMAGENES / "jugadores"

DIRECTORIO_DATOS.mkdir(parents=True, exist_ok=True)
DIRECTORIO_SALIDA.mkdir(parents=True, exist_ok=True)
DIRECTORIO_RESULTADOS.mkdir(parents=True, exist_ok=True)
DIRECTORIO_IMAGENES.mkdir(parents=True, exist_ok=True)
RUTA_IMAGENES_EQUIPOS.mkdir(parents=True, exist_ok=True)
RUTA_IMAGENES_JUGADORES.mkdir(parents=True, exist_ok=True)