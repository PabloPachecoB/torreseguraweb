# Estado de los procesos del agente

## Estado verificado

| Proceso | Backend de dominio | Integración conversacional |
|---|---|---|
| Reserva de área común | Completo | Completa |
| Reporte de incidencia | Completo | Completa |
| Apertura de cerradura | Completo para demo controlada | Completa |
| Autorización de visita | Completo para reserva futura y QR | Completa |

El grafo y la NLU reconocen `reservation`, `incident`, `lock`, `visitor` y
`general`. Las cuatro mutaciones pasan por confirmación autenticada, ejecución
idempotente y verificación del registro de dominio.

## Implementación común

- Checkpoints durables en `SqliteSaver` y transiciones atómicas de
  `AgentAction` sobre SQLite.
- Servicios de dominio en `accesos/services.py`, separados de las vistas HTTP.
- Claves únicas de idempotencia en `Visita` y `AperturaPuerta`.
- Trazas y respuestas diferenciadas para los cuatro tipos de acción.

## Apertura de cerradura (P1)

- Alcance deliberado: solo **abrir**; no existe cierre ni lectura de estado físico.
- Requiere `habilitada_para_demo=True` y reingreso de contraseña.
- La contraseña nunca se guarda en mensajes, payloads o checkpoints.
- `AperturaPuerta` registra estado del hardware, error e idempotencia. Un timeout
  se informa como fallo verificable, nunca como apertura exitosa.

## Autorización de visita (P2)

- Extrae nombre, documento, fecha, ventana, cantidad y motivo.
- La vivienda proviene del usuario autenticado, nunca de Qwen.
- `VisitanteSerializer` expone fecha, horario, cantidad, estado y uso del QR.
- La firma y nonce del QR se generan una sola vez por clave idempotente.
- El vigilante puede reportar la llegada con foto opcional; la app del residente
  obtiene una notificación local por polling y permite aprobar o rechazar.
- No se afirma entrega push: `REGISTRADA_LOCAL` describe exactamente el backend
  disponible mientras no exista un proveedor de mensajería.

## Criterio de finalización

Cada proceso demuestra el recorrido Qwen → esquema Pydantic → tool con
permisos → confirmación autenticada → escritura idempotente → verificación real,
además de pruebas deterministas de error y persistencia SQLite.
