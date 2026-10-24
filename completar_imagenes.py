# completar_imagenes.py
#
# Mantenimiento: asegura que TODAS las imágenes referenciadas en
# data/sofascore.db (tablas imagenes_jugadores e imagenes_equipos) estén
# presentes en la raíz estable que el backend sirve por /sofa
# (fantasy_config.IMAGES_ROOT = output/25-26).
#
# Problema que resuelve: el extractor descarga las fotos en la carpeta de la
# temporada activa (configuracion.TEMPORADA_CARPETA, p. ej. output/26-27).
# Cuando cambia la temporada, las rutas guardadas en la BD apuntan a archivos
# que ya no están en la raíz estable y el frontend recibe 404 (y muestra el
# avatar con iniciales). Este script copia desde cualquier carpeta de
# temporada de output/ los archivos que falten en la raíz estable.
#
# Es idempotente: solo copia lo que no existe todavía. Uso:
#   python completar_imagenes.py            # en seco, con resumen
#   python completar_imagenes.py --copy     # copia de verdad
# El servidor (server_fantasy.py) lo llama automáticamente en cada arranque.

from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path

from configuracion import DIRECTORIO_SALIDA, RUTA_BD
from fantasy_config import IMAGES_ROOT


def rutas_referenciadas(db: Path = RUTA_BD) -> list[str]:
    """Todas las rutas relativas de imágenes guardadas en la BD SofaScore."""
    rutas: list[str] = []
    con = sqlite3.connect(str(db))
    try:
        for tabla in ("imagenes_jugadores", "imagenes_equipos"):
            try:
                for (ruta,) in con.execute(f"SELECT ruta FROM {tabla}"):
                    rutas.append(str(ruta).replace("\\", "/").lstrip("/"))
            except sqlite3.OperationalError:
                # Tabla inexistente (BD antigua): se ignora sin romper nada.
                continue
    finally:
        con.close()
    return sorted(set(rutas))


def asegurar_imagenes_stables(
    raiz_stable: Path = IMAGES_ROOT,
    raiz_temporadas: Path = DIRECTORIO_SALIDA,
    db: Path = RUTA_BD,
    copiar: bool = True,
) -> tuple[int, int]:
    """Copia en raiz_stable las imágenes referenciadas en la BD que falten.

    Devuelve (copiadas, ausentes): 'ausentes' son rutas de la BD para las
    que no existe ningún archivo en ninguna carpeta de temporada.
    """
    copiadas = 0
    ausentes: list[str] = []
    raiz_stable.mkdir(parents=True, exist_ok=True)
    temporadas = sorted(p for p in raiz_temporadas.iterdir() if p.is_dir()) if raiz_temporadas.exists() else []

    for ruta in rutas_referenciadas(db):
        destino = raiz_stable / ruta
        if destino.exists():
            continue
        origen = None
        for carpeta in temporadas:
            candidato = carpeta / ruta
            if candidato.is_file():
                origen = candidato
                break
        if origen is None:
            ausentes.append(ruta)
            continue
        if copiar:
            destino.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(origen, destino)
        copiadas += 1

    return copiadas, ausentes


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Completa la carpeta de imágenes estable con las fotos que faltan.")
    parser.add_argument("--copy", action="store_true", help="copia de verdad (por defecto solo avisa)")
    args = parser.parse_args()

    faltantes, ausentes = asegurar_imagenes_stables(copiar=args.copy)
    modo = "copiadas" if args.copy else "faltantes"
    print(f"Imágenes {modo}: {faltantes} (raíz estable: {IMAGES_ROOT})")
    if ausentes:
        print(f"Rutas sin archivo en ninguna temporada ({len(ausentes)}):")
        for ruta in ausentes[:20]:
            print(f"  - {ruta}")
        if len(ausentes) > 20:
            print(f"  ... y {len(ausentes) - 20} más")


if __name__ == "__main__":
    main()