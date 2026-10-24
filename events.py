"""Hub de eventos en tiempo real para Gurutze Fantasy (SSE).

Mantiene las colas de eventos por liga de los clientes conectados y permite
**publicar** un evento (p. ej. "se ha cerrado una jornada") desde cualquier
hilo (el loop de auto-cierre, un endpoint, etc.) hacia todos los clientes de
esa liga. El punto de entrada `publish` es seguro entre hilos: usa
`loop.call_soon_threadsafe` para inyectar el evento en el event loop de
FastAPI.
"""
import asyncio
import threading
from typing import Any, Optional

# league_id -> conjunto de colas (una por cliente conectado)
_queues: dict[int, set[asyncio.Queue]] = {}
_lock = threading.Lock()
_loop: Optional[asyncio.AbstractEventLoop] = None


def init_hub(loop: asyncio.AbstractEventLoop) -> None:
    """Se llama en el arranque del servidor (lifespan) para guardarse el loop."""
    global _loop
    _loop = loop


def _deliver(league_id: int, payload: dict) -> None:
    """Ejecutado EN el event loop: inyecta el evento en las colas de la liga."""
    with _lock:
        queues = list(_queues.get(league_id, ()))
    for q in queues:
        try:
            q.put_nowait(payload)
        except Exception:
            # La cola podría cerrarse en el momento exacto de la desconexión.
            pass


def publish(league_id: int, payload: dict) -> None:
    """Publica un evento a los clientes de una liga. Usable desde cualquier hilo.

    Si el servidor no está activo (loop None/cerrado) es un no-op.
    """
    loop = _loop
    if loop is None or loop.is_closed():
        return
    try:
        loop.call_soon_threadsafe(_deliver, league_id, payload)
    except RuntimeError:
        # El loop ya se cerró (apagando el servidor): no hacemos nada.
        pass


def register(league_id: int, queue: "asyncio.Queue[Any]") -> None:
    with _lock:
        _queues.setdefault(league_id, set()).add(queue)


def unregister(league_id: int, queue: "asyncio.Queue[Any]") -> None:
    with _lock:
        queues = _queues.get(league_id)
        if queues:
            queues.discard(queue)
            if not queues:
                _queues.pop(league_id, None)


def connected(league_id: int) -> int:
    with _lock:
        return len(_queues.get(league_id, ()))