# Plan de procesos pendientes del agente

## Estado verificado

| Proceso | Backend de dominio | Integración conversacional |
|---|---|---|
| Reserva de área común | Completo | Completa |
| Reporte de incidencia | Completo | Completa |
| Apertura de cerradura | Parcial: apertura controlada y bitácora | Pendiente |
| Autorización de visita | Parcial: reserva futura y QR | Pendiente |

El grafo y la NLU solo reconocen `reservation`, `incident` y `general`. Por
tanto, la existencia de endpoints de puertas y visitas no significa que Qwen
pueda conducir esos procesos todavía.

## Fase 1: base extensible

1. Reemplazar las bifurcaciones binarias reserva/incidencia por un registro de
   procesos que defina intención, tipo de acción, tool, nombre y verificación.
2. Extender las respuestas y trazas sin asumir que toda acción no incidente es
   una reserva.
3. Mantener checkpoints en `SqliteSaver`; probar reanudación tras reiniciar la
   conexión y transiciones atómicas de `AgentAction` sobre SQLite.

## Fase 2: apertura de cerradura (P1)

1. Añadir intención `lock` y extracción tipada de puerta y operación. El primer
   alcance será únicamente **abrir**, porque el backend no ofrece cerrar ni leer
   el estado físico.
2. Extraer permisos y ejecución de `accesos/api_puertas.py` a un servicio de
   dominio reutilizable por la API y la tool; no invocar una vista desde el
   agente.
3. Crear una acción `CERRADURA_ABRIR` con expiración e idempotencia. Exigir
   contraseña en el endpoint de confirmación antes de reanudar LangGraph; nunca
   guardar la contraseña en mensajes, payloads o checkpoints.
4. Verificar la ejecución contra `AperturaPuerta` y completar `resultado`,
   `verification_status`, `executed_at` y `error_code` sin presentar timeouts
   como aperturas exitosas.
5. Cubrir puerta no autorizada, demo deshabilitada, contraseña ausente/errónea,
   hardware caído y doble confirmación.

## Fase 3: autorización de visita (P2)

1. Acordar el contrato móvil para exponer fecha, horario y cantidad de personas
   en `VisitanteSerializer`. Definir si foto, notificación y aprobación de
   llegada son reales o simuladas; hoy no existen esos contratos.
2. Crear un servicio de dominio idempotente. `Visita` necesita una clave única o
   referencia a `AgentAction` para impedir dos QR ante reintentos.
3. Añadir intención `visitor` y extracción de nombre, documento, fecha, ventana,
   cantidad y motivo. La vivienda se toma del usuario autenticado, nunca de Qwen.
4. Preparar, confirmar, crear y verificar `VISITA_CREAR`; devolver el ID y estado
   reales. No guardar imágenes ni contenido binario en el checkpoint.
5. Probar conversación multiturno, campos faltantes, fechas inválidas, vivienda
   ajena, doble confirmación, reinicio del proceso y validación anti-replay del QR.

## Criterio de finalización

Cada proceso debe demostrar el recorrido Qwen → esquema Pydantic → tool con
permisos → confirmación autenticada → escritura idempotente → verificación real,
además de pruebas deterministas de error y persistencia SQLite.
