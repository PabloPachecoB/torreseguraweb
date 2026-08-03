# Flujo de aprobación de incidencias

## Estados y participantes

1. El residente describe el problema y adjunta evidencia opcional.
2. El agente propone categoría, prioridad, costo orientativo y tiempo estimado.
3. La confirmación del residente crea la incidencia y la versión 1 de la evaluación.
4. Se notifica a los administradores del edificio y al técnico cuando sea asignado.
5. Administrador o técnico pueden crear una nueva revisión ajustando categoría,
   prioridad, costo o tiempo. Cada ajuste invalida las aprobaciones previas.
6. La revisión vigente requiere aprobación del residente y administrador; si hay
   técnico asignado también requiere su aprobación.
7. Al reunir todas las aprobaciones se crea una `OrdenTrabajo`, la incidencia pasa
   a `APROBADA` y se notifica al residente.

Las estimaciones del agente son preliminares. El rango monetario está en BOB y se
obtiene de una política determinista por categoría; no representa una cotización.

## API

```http
GET  /api/v1/incidencias/pendientes-revision/
POST /api/v1/incidencias/{id}/revision/
POST /api/v1/incidencias/{id}/aprobar/
POST /api/v1/incidencias/{id}/solicitar-revision/
GET  /api/v1/incidencias/notificaciones/
```

`revision/` acepta opcionalmente `categoria`, `prioridad`,
`costo_estimado_min`, `costo_estimado_max`, `moneda`,
`tiempo_estimado_horas`, `comentario` y `empleado_id`. Solamente administradores
pueden asignar técnicos y solamente el técnico asignado puede decidir o ajustar
como técnico.

La API de detalle devuelve `revisiones`, sus `aprobaciones`,
`tecnico_asignado` y `orden_trabajo`.

## Presentaciones para la app

El agente puede devolver:

- `incident_initial_evaluation`: evaluación que confirma el residente.
- `incident_review_status`: versión y aprobaciones actuales.
- `work_order`: orden aprobada y programación.

La confirmación inicial sigue usando el endpoint autenticado de `AgentAction`;
escribir “sí” en el chat no crea la incidencia.
