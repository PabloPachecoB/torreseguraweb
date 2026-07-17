# Documentación de API — para Huascar (motor conversacional, HU-01.1)

Este documento cubre los endpoints de `/api/v1/` que ya existen y que tu motor de
chat va a necesitar llamar (o, en el caso de `agente`, generar filas) para poder
conversar con un residente y ejecutar acciones reales. Está ordenado por relevancia
para tu HU-01.1, no alfabéticamente: primero lo que vas a usar todo el tiempo, al
final lo que es solo referencia.

Todo bajo `https://<host>/api/v1/...`, autenticado con JWT (`Authorization: Bearer <token>`).

---

## 1. Lo más importante: `agente.AgentAction`

Este es el modelo que arma el "loop" de confirmación de tu motor: **vos creás la
fila** (cuando decidís que hay una acción para proponerle al residente),
**el endpoint ya existente confirma/rechaza** (HU-01.2, ya construido y probado).

### 1.1 Qué es y qué NO es

- Es una fila de **estado actual**, no un log — una `AgentAction` = una propuesta de
  acción concreta, que nace `PENDIENTE` y termina en `CONFIRMADA`, `RECHAZADA` o
  `EJECUTADA` (o `EXPIRADA` si le pusiste `expira_en` y nadie respondió a tiempo).
- **Vos (tu motor conversacional) sos quien la crea.** No hay endpoint HTTP público
  de creación — es intencional (HU-01.2 es solo la capa de confirmar/auditar). Como
  tu motor corre dentro del mismo backend Django(aunque no se si es asi o lo tienes en otra framework), la creás directamente con el ORM:

```python
from agente.models import AgentAction

accion = AgentAction.objects.create(
    usuario=residente_usuario,       # el Usuario autenticado en la conversación
    tipo_accion="RESERVA_CREAR",     # ver convención de nombres abajo
    payload={"area_id": 3, "fecha": "2026-07-20", "hora_inicio": "18:00", "hora_fin": "19:00"},
    expira_en=None,                  # opcional, datetime — si querés que expire sola
)
```

- Campos del modelo (todos accesibles luego vía la API de solo lectura):

| Campo | Tipo | Notas |
|---|---|---|
| `usuario` | FK Usuario | Dueño — el único que puede confirmar/rechazar |
| `tipo_accion` | string libre | Ver convención abajo — **hay que acordarla entre todos** |
| `payload` | JSON | Los parámetros de la acción propuesta |
| `estado` | `PENDIENTE`/`CONFIRMADA`/`EJECUTADA`/`RECHAZADA`/`EXPIRADA` | Arranca en `PENDIENTE` |
| `estado_previo` | igual que `estado`, nullable | Se llena solo al confirmar/rechazar/expirar |
| `fecha_creacion` | datetime, auto | — |
| `fecha_confirmacion` | datetime, nullable | Se llena al confirmar/rechazar |
| `confirmada_por` | FK Usuario, nullable | Igual a `usuario` normalmente |
| `expira_en` | datetime, nullable | Si se pasa sin confirmar, pasa a `EXPIRADA` |
| `resultado` | JSON, nullable | **Vacío hoy** — lo llenarías vos (o quien ejecute la acción real) cuando la acción pase a `EJECUTADA` |

### 1.2 Convención de `tipo_accion` (propuesta, falta acordar con el equipo)

No hay un enum fijo — es texto libre a propósito, para que cada HU lo use sin
esperar un cambio de modelo. Sugerido (avisar en el chat de equipo si lo cambian):

- `RESERVA_CREAR` — para HU-02.x (áreas comunes), `payload` con `area_id`, `fecha`, `hora_inicio`, `hora_fin`.
- `INCIDENCIA_CREAR` — para HU-03.1 (todavía no construido).
- `CERRADURA_ABRIR` / `CERRADURA_CERRAR` — para EP-04 (Pachecock), `payload` con `puerta_id`.

### 1.3 Endpoints (ya construidos, `IsAuthenticated`)

Todos scoped al usuario autenticado — **nunca** devuelven ni dejan tocar acciones de
otro usuario (404, no 403, para no filtrar que existen).

**`GET /api/v1/agente/acciones/`** — lista las acciones del usuario logueado.
```json
[
  {
    "id": 1,
    "tipo_accion": "RESERVA_CREAR",
    "payload": {"area_id": 3, "fecha": "2026-07-20", "hora_inicio": "18:00", "hora_fin": "19:00"},
    "estado": "PENDIENTE",
    "estado_previo": null,
    "fecha_creacion": "2026-07-16T10:00:00-04:00",
    "fecha_confirmacion": null,
    "confirmada_por": null,
    "expira_en": null,
    "resultado": null
  }
]
```

**`GET /api/v1/agente/acciones/<id>/`** — detalle de una (mismo shape que arriba).

**`POST /api/v1/agente/acciones/<id>/confirmar/`** — el residente confirma. Body vacío.
- `200`: acción actualizada, `estado="CONFIRMADA"`.
- `403`: el usuario logueado no es el dueño (no debería pasar si el chat ya filtra por sesión, pero cuidado con esto igual).
- `409`: ya no estaba `PENDIENTE` (ya se confirmó/rechazó antes).

**`POST /api/v1/agente/acciones/<id>/rechazar/`** — igual que confirmar pero a `RECHAZADA`.

### 1.4 Lo que falta y probablemente sea tuyo construir

- **Ejecutar la acción de verdad** cuando pasa a `CONFIRMADA` (ej. llamar a
  `crear_reserva` de `areas_comunes` con los datos del `payload`) y guardar el
  resultado en `resultado` + mover `estado` a `EJECUTADA`. Hoy `confirmar()` solo dice
  "confirmada", no ejecuta nada — es a propósito, quedó desacoplado para que el
  ejecutor lo construya quien maneje el motor conversacional.
- Nadie llama `AgentAction.objects.create(...)` todavía en el código real — hoy solo
  lo hacemos a mano por shell para probar (ver `inst/probar_hu_01_2_postman.md`).

---

## 2. `areas_comunes` — reservas (relevante para HU-01.1 si el chat reserva áreas, y directo para tu HU-02.3)

### 2.1 `GET /api/v1/areas-comunes/` — listar áreas disponibles para el usuario

Devuelve solo las áreas del edificio del usuario (Administrador ve todas).
```json
[
  {"id": 3, "nombre": "Salon de eventos", "descripcion": "", "buildingName": "Torre Aurora",
   "capacidad_maxima": 30, "horario_inicio": "08:00:00", "horario_fin": "20:00:00",
   "imageUrl": null, "activo": true}
]
```

### 2.2 `GET /api/v1/areas-comunes/<area_id>/disponibilidad/` — consultar horarios libres (HU-02.1)

Query params: `fecha` (obligatorio, `YYYY-MM-DD`), `duracion_minutos` (opcional, default 60).

```json
{
  "area": { "...": "..." },
  "fecha_consultada": "2026-07-20",
  "duracion_minutos": 60,
  "slots_disponibles": [
    {"hora_inicio": "08:00", "hora_fin": "09:00"},
    {"hora_inicio": "09:00", "hora_fin": "10:00"}
  ],
  "alternativas": []
}
```
Si `slots_disponibles` viene vacío, `alternativas` trae hasta 3 fechas futuras (de
los próximos 7 días) que sí tienen lugar, con el mismo formato de slots — **útil
para tu HU-02.3** ("recibir alternativas si el horario cambia/expira"): en vez de
inventar una respuesta, tu motor puede leer directo este campo.

Si falta `fecha`: `400` con `{"mensaje": "...", "campos_faltantes": ["fecha"]}` —
pensado justo para que tu motor sepa qué preguntarle al residente si todavía no se
lo pidió en la conversación.

### 2.3 `POST /api/v1/areas-comunes/<area_id>/reservar/` — crear la reserva

Body: `{"fecha": "2026-07-20", "hora_inicio": "18:00", "hora_fin": "19:00", "motivo": "opcional"}`.

- `201`: `{"mensaje": "Reserva creada correctamente.", "reserva": {...}}`.
- `400`: faltan campos, o **solapamiento con otra reserva** — mensaje textual
  describiendo el problema (útil para HU-02.3: podés usar este mismo mensaje o
  detectarlo y ofrecer `disponibilidad` de nuevo para dar alternativas).
- `404`: área no encontrada o de otro edificio (scoping por edificio, ya corregido).
- `403`: el usuario no es residente.

### 2.4 `GET /api/v1/areas-comunes/mis-reservas/` — reservas del residente autenticado (últimas 50).

### 2.5 `PATCH /api/v1/areas-comunes/reservas/<reserva_id>/cancelar/` — cancelar una reserva propia (o cualquiera si sos admin).
- `409` si ya estaba `cancelada` o `completada`.

---

## 3. `accesos` — control de puertas (EP-04, Pachecock — soporte tuyo ahí)

Recién mergeado (`origin/PabloDev`), sin capa de confirmación todavía (no pasa por
`agente.AgentAction` — abre directo). Si el chat termina disparando esto, avisale a
Pachecock que probablemente convenga que pase por el mismo patrón de confirmar/HU-01.2.

**`GET /api/v1/accesos/puertas/`** — puertas que el usuario puede abrir según su rol
(residente: principal + su edificio + su vivienda; vigilante: principal + su
edificio; admin: todas).
```json
[{"id": 1, "nombre": "Puerta principal", "tipo": "PRINCIPAL", "tipo_display": "Puerta principal",
  "edificio": null, "vivienda": null, "tiene_hardware": false}]
```

**`POST /api/v1/accesos/puertas/<puerta_id>/abrir/`** — abre ya mismo (sin
confirmación previa). Si la puerta tiene `webhook_url` configurada, llama al
hardware (ESP32); si no, responde OK en "modo software".
- `200`: `{"abierta": true, "mensaje": "...", "puerta": {...}}`.
- `403`: sin permiso sobre esa puerta.
- `502`: el hardware no respondió (nunca finge éxito).

**`GET /api/v1/accesos/puertas/aperturas/`** — historial (admin/vigilante ven todo lo permitido; residente solo lo propio).

---

## 4. Autenticación (referencia rápida)

**`POST /api/v1/auth/token/`** — login. Body `{"username": "...", "password": "..."}`.
```json
{"refresh": "...", "access": "...", "user": {"id": 3, "username": "carlos", "rol": {"nombre": "Residente"}, "vivienda_id": 1, "edificio_id": 1, "..."}}
```
Errores esperables: `{"error": "Debe verificar su correo electrónico..."}` (email no
confirmado) y `{"error": "Su rol debe ingresar desde la web"}` (Administrador/Gerente
no pueden loguear por acá — solo Residente/Vigilante/Personal).

**`POST /api/v1/auth/token/refresh/`** — renovar el `access` con el `refresh`.

**`GET /api/v1/auth/me/`** — datos del usuario autenticado (mismo shape que el `user` del login).

---

## 5. Otros endpoints que existen (fuera del alcance directo de tu HU, solo referencia)

| App | Ruta base | Para qué |
|---|---|---|
| `alertas` | `/api/v1/alertas/` | CRUD de alertas de emergencia |
| `accesos` (visitas) | `/api/v1/visitas/...`, `/api/v1/visitantes/` (router) | Visitas de terceros, QR de acceso |
| `financiero` | `/api/v1/financiero/` | Cuotas pendientes/pagadas, pagos, QR BNB |
| `usuarios` | `/api/v1/auth/clientes-potenciales/...` | Registro de leads (no conversacional) |

---

## 6. Convenciones generales que vas a ver repetidas en toda la API

- **404 en vez de 403** cuando el recurso existe pero no es tuyo (dueño/edificio) —
  para no confirmar su existencia a quien no debería verlo. Vale para `agente` y
  `areas_comunes`.
- **400 con mensaje + a veces `campos_faltantes`** cuando falta un dato — pensado
  para que un motor conversacional sepa qué re-preguntar sin tener que parsear texto libre.
- **409** cuando el estado del recurso no permite la acción (ej. confirmar algo que
  ya no está pendiente, cancelar algo ya cancelado).
- Todos los endpoints nuevos son `IsAuthenticated` + JWT — no hay endpoints públicos.

## 🔗 Relacionadas

`inst/explicacion_cambios_hu01_2_hu02_1.md` (cómo funciona `AgentAction` por dentro,
línea a línea), `inst/mensajes_equipo.md` (resumen de estado del sprint),
`inst/probar_hu_01_2_postman.md` / `inst/probar_hu_02_1_postman.md` (guías de prueba
manual con ejemplos reales de `curl`/Postman).
