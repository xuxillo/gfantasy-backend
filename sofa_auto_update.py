"""Stub de compatibilidad: sofa_auto_update.

El cierre automático de jornadas se desactiva en producción
con GURUTZE_DISABLE_AUTO_CLOSE=1. Solo el import es necesario
para que el ASGI app arranque sin ImportError.
"""
import os

DISABLED = os.environ.get("GURUTZE_DISABLE_AUTO_CLOSE", "1") == "1"


def ejecutar(*args, **kwargs):
    """No-op: el auto-update está desactivado en PythonAnywhere."""
    return 0


def ejecutar_imagenes(*args, **kwargs):
    """No-op."""
    return 0