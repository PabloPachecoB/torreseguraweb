# TorreSegura Agent API — Mobile

Contrato del agente conversacional para la app móvil. El cliente consume únicamente la API Django en `/api/v1/agente/`; LangGraph Studio y el servidor de desarrollo del puerto `2024` no forman parte de la integración mobile.

Autenticación, reservas directas, incidencias, visitas, puertas y finanzas están descritas en [TorreSegura Backend API — Mobile](API_MOBILE_BACKEND.md).

## Arquitectura relevante para el cliente

Qwen clasifica intención y extrae parámetros. LangGraph controla el flujo y las tools Django consultan permisos, disponibilidad y fuentes de verdad. Qwen nunca confirma ni ejecuta una mutación. Las cuatro acciones soportadas son:

| Intención | Acción | Fuente de verdad |
|---|---|---|
| `reservation` | `RESERVA_CREAR` | `areas_comunes.Reserva` |
| `incident` | `INCIDENCIA_CREAR` | `incidencias.Incidencia` |
| `lock` | `CERRADURA_ABRIR` | `accesos.AperturaPuerta` |
| `visitor` | `VISITA_CREAR` | `accesos.Visita` |

Todos los endpoints requieren `Authorization: Bearer <access_token>`. Cada usuario solo puede consultar y decidir sus propias acciones.

## Endpoints

| Método | Endpoint | Descripción |
|---|---|---|
| POST | `/api/v1/agente/acciones/chat/` | Envía un turno y crea/reanuda un thread. |
| GET | `/api/v1/agente/acciones/` | Acciones del usuario, más recientes primero. |
| GET | `/api/v1/agente/acciones/{id}/` | Detalle de una acción. |
| POST | `/api/v1/agente/acciones/{id}/confirmar/` | Confirma, ejecuta y verifica. |
| POST | `/api/v1/agente/acciones/{id}/rechazar/` | Rechaza sin ejecutar. |
| GET | `/api/v1/agente/acciones/health/` | Conectividad con el proveedor Qwen. |

## Conversación y threads

Primer turno:

```http
POST /api/v1/agente/acciones/chat/
Content-Type: application/json

{"message":"Reserva el Salón de eventos el 2026-07-29 de 09:00 a 10:00 para 5 personas"}
```

`message` admite hasta 4000 caracteres. Cada petición debe incluir `message` o
`interaction`; en texto libre se usa normalmente `message`. El backend genera un
UUID en el primer turno. Mobile debe guardarlo y enviarlo en todos los mensajes
posteriores de esa conversación:

```json
{
  "message": "Mejor para 6 personas",
  "thread_id": "24b708fa-680d-4210-9b81-33b44b7d6060"
}
```

Un `thread_id` inválido devuelve `400` con `error_code:invalid_thread_id`. No reutilice un thread entre usuarios. Cree una conversación nueva para una tarea independiente.

### Respuesta de chat

```json
{
  "thread_id": "24b708fa-680d-4210-9b81-33b44b7d6060",
  "message": "Confirma esta acción: ...",
  "intent": "reservation",
  "status": "awaiting_confirmation",
  "error": null,
  "checkpoint_backend": "sqlite",
  "durable": true,
  "requires_confirmation": true,
  "action_id": 31,
  "presentation": null,
  "trace_metadata": {
    "graph_version": "0.2.0",
    "intent": "reservation",
    "outcome": "success",
    "model_provider": "qwen_cloud",
    "model_name": "qwen3.6-flash",
    "tool_name": "create_reservation",
    "llm_invoked": true,
    "guardrail_triggered": false
  },
  "confirmation": {
    "type": "action_confirmation",
    "action_id": 31,
    "summary": "Reservar ...",
    "expires_in_seconds": 600,
    "requires_password": false
  }
}
```

Campos de control para UI:

| Campo | Valores/uso |
|---|---|
| `status` | `ok`, `awaiting_confirmation`, `error`. |
| `message` | Texto que se presenta al usuario. |
| `presentation` | `null` o un objeto estructurado para tarjetas y opciones interactivas. `message` siempre queda como fallback accesible. |
| `requires_confirmation` | Si es `true`, mostrar tarjeta de revisión con Confirmar/Rechazar. |
| `action_id` | Usar en los endpoints de decisión; nunca inferirlo del texto. |
| `intent` | `reservation`, `incident`, `lock`, `visitor`, `general`. Útil para iconografía, no para autorizar. |
| `error` | `null` o `{code,message}`. |
| `trace_metadata` | Diagnóstico; no condicionar lógica de negocio a este objeto. |
| `durable` | Indica checkpoint persistente; no reemplaza almacenamiento local del `thread_id`. |

Si existe una acción pendiente, nuevos mensajes en ese thread no cambian sus parámetros. El backend responderá que primero debe confirmarse o rechazarse.

## Presentaciones estructuradas

Las respuestas informativas pueden incluir `presentation`. El cliente debe decidir
el componente visual usando exclusivamente `presentation.type`; no debe analizar
el texto de `message` para descubrir listas, IDs u horarios.

### Tarjetas de áreas comunes

```json
{
  "message": "Estos son los espacios comunes disponibles en Torre Aurora: ...",
  "presentation": {
    "type": "common_area_cards",
    "title": "Espacios comunes de Torre Aurora",
    "areas": [
      {
        "id": 1,
        "name": "Salón de eventos",
        "description": "Área para eventos y reuniones",
        "capacity": 30,
        "opening_time": "08:00",
        "closing_time": "22:00",
        "actions": [
          {
            "type": "check_area_availability",
            "label": "Ver horarios",
            "payload": {"area_id": 1}
          },
          {
            "type": "start_reservation",
            "label": "Reservar",
            "payload": {"area_id": 1}
          }
        ]
      }
    ]
  }
}
```

### Opciones de disponibilidad

```json
{
  "message": "Ese horario ya está ocupado, pero encontré estas alternativas:",
  "presentation": {
    "type": "availability_options",
    "title": "Elige el horario que prefieras",
    "area": {"id": 1, "name": "Salón de eventos", "capacity": 30},
    "requested": {
      "date": "2026-07-20",
      "start_time": "10:00",
      "end_time": "15:00",
      "attendees": 19
    },
    "dates": [
      {
        "date": "2026-07-20",
        "label": "Mañana",
        "slots": [
          {
            "start_time": "15:00",
            "end_time": "20:00",
            "label": "15:00–20:00",
            "action": {
              "type": "select_reservation_slot",
              "label": "Elegir 15:00–20:00",
              "payload": {
                "area_id": 1,
                "date": "2026-07-20",
                "start_time": "15:00",
                "end_time": "20:00"
              }
            }
          }
        ]
      }
    ]
  }
}
```

Las fechas y horarios de estas presentaciones provienen de las tools del backend,
no de texto libre generado por Qwen.

### Enviar una interacción

Mobile puede reenviar la acción de una tarjeta en el mismo endpoint. `message` es
opcional cuando existe `interaction`, pero `thread_id` debe conservarse:

```http
POST /api/v1/agente/acciones/chat/
Content-Type: application/json

{
  "thread_id": "24b708fa-680d-4210-9b81-33b44b7d6060",
  "interaction": {
    "type": "select_reservation_slot",
    "payload": {
      "area_id": 1,
      "date": "2026-07-20",
      "start_time": "15:00",
      "end_time": "20:00"
    }
  }
}
```

Interacciones aceptadas:

| `type` | Payload | Resultado |
|---|---|---|
| `check_area_availability` | `area_id` | Pregunta la fecha y consulta horarios. |
| `start_reservation` | `area_id` | Inicia la recopilación de fecha, horario y asistentes. |
| `select_reservation_slot` | `area_id`, `date`, `start_time`, `end_time` | Selecciona una opción ofrecida y conserva los asistentes/motivo del thread. La reserva sigue requiriendo confirmación. |

El backend vuelve a validar edificio, área, capacidad y disponibilidad aunque el
payload provenga de una tarjeta. Una interacción nunca equivale por sí sola a una
confirmación de la acción.

## Confirmación y rechazo

Una respuesta `awaiting_confirmation` no significa que la operación fue ejecutada. Mobile debe mostrar exactamente el resumen y enviar una decisión explícita.

Confirmar reserva, incidencia o visita:

```http
POST /api/v1/agente/acciones/31/confirmar/
Content-Type: application/json

{}
```

Confirmar apertura de puerta requiere reingresar la contraseña:

```http
POST /api/v1/agente/acciones/32/confirmar/
Content-Type: application/json

{"password":"contraseña-actual"}
```

La contraseña se envía únicamente a este endpoint y no se guarda en mensajes ni checkpoints. Contraseña ausente devuelve `400`; incorrecta devuelve `403` y no ejecuta la apertura.

Rechazar:

```http
POST /api/v1/agente/acciones/31/rechazar/

{}
```

La respuesta de confirmar/rechazar contiene el objeto `AgentAction` actualizado y, para acciones conversacionales, un campo `conversation` con el mismo contrato de chat:

```json
{
  "id": 31,
  "tipo_accion": "RESERVA_CREAR",
  "payload": {"area_id":1,"date":"2026-07-29","start_time":"09:00","end_time":"10:00","attendees":5,"reason":""},
  "thread_id": "24b708fa-680d-4210-9b81-33b44b7d6060",
  "requires_confirmation": true,
  "confirmation_method": "authenticated_api",
  "idempotency_key": "<sha256>",
  "tool_name": "create_reservation",
  "estado": "EJECUTADA",
  "estado_previo": "CONFIRMADA",
  "fecha_creacion": "2026-07-18T10:00:00-04:00",
  "fecha_confirmacion": "2026-07-18T10:01:00-04:00",
  "confirmada_por": 3,
  "expira_en": "2026-07-18T10:10:00-04:00",
  "resultado": {"status":"success","reservation_id":8,"reservation_status":"confirmada","replayed":false},
  "backend_reference": "8",
  "executed_at": "2026-07-18T10:01:00-04:00",
  "verification_status": "VERIFICADA",
  "error_code": "",
  "conversation": {
    "thread_id": "24b708fa-680d-4210-9b81-33b44b7d6060",
    "message": "Reserva creada y verificada. ID 8; estado real: confirmada.",
    "intent": "reservation",
    "status": "ok",
    "error": null,
    "requires_confirmation": false,
    "action_id": null
  }
}
```

Estados de acción: `PENDIENTE`, `CONFIRMADA`, `EJECUTADA`, `RECHAZADA`, `EXPIRADA`. Verificación: `NO_INICIADA`, `VERIFICADA`, `FALLIDA`, `DESCONOCIDA`.

## Happy paths por proceso

### Reserva

```json
{"message":"Reserva el Salón de eventos el 2026-07-29 de 09:00 a 10:00 para 5 personas por una reunión familiar"}
```

El agente valida área autorizada, capacidad, horario y disponibilidad. Si falta un campo responde `status:ok` con una pregunta; continúe con el mismo `thread_id`. Cuando están completos devuelve confirmación por 10 minutos. Tras confirmar, `resultado` incluye `reservation_id`, `reservation_status` y `replayed`.

### Incidencia

```json
{"message":"Reporta una fuga de agua continua debajo del lavaplatos de mi departamento; el piso se está mojando rápidamente y la urgencia es alta"}
```

Qwen propone título, categoría y urgencia; la respuesta debe mostrarlas como estimaciones preliminares. La confirmación dura 10 minutos. Tras ejecutar, `resultado` incluye `incident_id`, `incident_status` y `replayed`. Evidencias se adjuntan después mediante `/api/v1/incidencias/{incident_id}/evidencias/`.

### Visita

```json
{"message":"Autoriza la visita de Ana Pérez, documento 1234567, el 2026-07-30 de 18:00 a 19:00 para 2 personas por una cena familiar"}
```

La confirmación dura 10 minutos. El resultado incluye `visit_id`, `visit_status`, QR e idempotencia en backend. Mobile recupera el detalle desde `/api/v1/visitantes/{visit_id}/` y gestiona llegada/aprobación con los endpoints de visitas.

### Puerta

```json
{"message":"Abre la Puerta departamento Carlos"}
```

Solo aparecen puertas autorizadas y habilitadas para demo remota. La confirmación expira en 5 minutos y `requires_password` es `true`. Tras confirmar con contraseña, el resultado incluye `opening_id`, `hardware_status` y `success`. Si existe hardware, un timeout o fallo se devuelve como error; no debe mostrarse “abierta” hasta recibir `success:true` y verificación exitosa.

## Máquina de estados recomendada en mobile

```text
idle
  -> sending
  -> message_received (status=ok)
  -> awaiting_confirmation
       -> confirming -> completed | failed
       -> rejecting  -> rejected  | failed
```

Reglas de implementación:

1. Deshabilitar el envío repetido mientras una petición está en curso.
2. Persistir `thread_id` por conversación y usuario.
3. Cuando `requires_confirmation=true`, bloquear nuevos parámetros y mostrar Confirmar/Rechazar.
4. Confirmar siempre por `action_id`; un mensaje textual como “sí” no confirma nada.
5. No hacer optimistic success para reservas, visitas, incidencias o puertas.
6. Ante timeout del cliente, consultar `GET /acciones/{id}/` antes de repetir confirmación.
7. Eliminar contraseña de memoria inmediatamente después del request de puerta.

## Errores del agente

| HTTP | Ejemplo | Tratamiento |
|---|---|---|
| 400 | `invalid_thread_id`, validación de `message`, contraseña ausente | Corregir input; no retry automático. |
| 401 | JWT ausente/expirado | Refresh y repetir una vez. |
| 403 | Contraseña incorrecta o acción ajena | Mostrar error; no ejecutar. |
| 404 | Acción no encontrada para el usuario | Cerrar tarjeta y refrescar lista. |
| 409 | Acción expirada, ya decidida o checkpoint incompatible | Consultar detalle y sincronizar UI. |
| 503 | `conversation_unavailable`, `confirmation_resume_failed`, proveedor Qwen no disponible | Mostrar indisponibilidad y permitir retry controlado. |

Un fallo de Qwen impide interpretar el turno, pero nunca ejecuta parcialmente una operación. Una mutación solo se considera finalizada cuando `estado=EJECUTADA`, `verification_status=VERIFICADA` y la referencia de dominio aparece en `resultado`.

## Health check

```http
GET /api/v1/agente/acciones/health/
```

Respuesta típica:

```json
{
  "healthy": true,
  "provider": "qwen_cloud",
  "model": "qwen3.6-flash",
  "model_available": true,
  "status": "ok"
}
```

Este endpoint comprueba el proveedor, no el estado completo de Django ni de cada herramienta de dominio.
