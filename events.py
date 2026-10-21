# events.py - Módulo de eventos para server_fantasy.py
# Importado en la línea 20 del servidor para notificar cierres de jornadas

class EventPublisher:
    """Publicador de eventos del sistema Fantasy.
    
    Almacena eventos importantes como cierres de jornadas,
    para que puedan procesarse automáticamente (sumar puntos,
    actualizar tablas, etc.).
    """
    
    # Lista para guardar todos los eventos que se publiquen
    published_events = []
    
    @staticmethod
    def publish(league_id, event_data):
        """
        Publica un evento del sistema.
        
        Este es el punto de entrada que usa server_fantasy.py 
        para avisar cuando una jornada se cierra automáticamente.
        
        Args:
            league_id: ID de la liga donde ocurre el evento
            event_data: Diccionario con los datos del evento:
                - type: tipo de evento (ej: "matchday_closed")
                - league_id: ID de la liga
                - matchday: jornada número
                - advanced_to: a qué jornada avanzó
        """
        # 1. Guardamos el evento en nuestra lista interna
        event_entry = {
            "league_id": league_id,
            "type": event_data.get("type", "unknown"),
            "data": event_data,
        }
        EventPublisher.published_events.append(event_entry)
        
        # 2. Mostramos en consola qué pasó (para que el admin lo vea)
        print(f"[evento_sistema] Liga {league_id}: {event_data.get('type', 'unknown')}")
        
        # 3. Aquí podría ir lógica adicional futura:
        # - Sumar puntos automáticamente
        # - Actualizar tablas de posiciones  
        # - Enviar notificaciones
        # - Cerrar la jornada en la base de datos
        
        # Por ahora, solo imprimimos y guardamos el registro
        return event_entry


# Instancia global que importará server_fantasy.py
# server_fantasy.py hará: `from events import events` (la instancia abajo)
events = EventPublisher()