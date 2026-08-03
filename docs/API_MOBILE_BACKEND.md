# TorreSegura Backend API — Mobile

Guía de integración para la API REST de Django. Todos los paths son relativos a `BASE_URL`, por ejemplo `http://127.0.0.1:8000` en desarrollo. La API usa JSON y JWT, salvo los endpoints marcados como `multipart/form-data`.

El contrato conversacional se documenta por separado en [TorreSegura Agent API — Mobile](API_MOBILE_AGENTE.md).

## Convenciones

Enviar el access token en cada endpoint protegido:

```http
Authorization: Bearer <access_token>
Content-Type: application/json
```

El access token dura 60 minutos y el refresh token un día. Las fechas usan `YYYY-MM-DD`, las horas `HH:MM` y los datetimes ISO 8601. Los importes llegan como strings decimales. Los listados funcionales devuelven arrays; el CRUD `GET /api/v1/alertas/` usa paginación DRF (`count`, `next`, `previous`, `results`). Límites globales: 120 solicitudes/minuto autenticadas y 30/minuto anónimas.

Los errores existentes no tienen una única envoltura: pueden usar `error`, `mensaje`, `detail` o errores por campo. El cliente debe decidir por HTTP status y mostrar el primer mensaje disponible.

## Autenticación y usuario

### Iniciar sesión

```http
POST /api/v1/auth/token/

{"username":"carlos","password":"carlos123"}
```

`username` también acepta correo. Respuesta `200`:

```json
{
  "refresh": "<jwt>",
  "access": "<jwt>",
  "user": {
    "id": 3,
    "username": "carlos",
    "email": "carlos@example.com",
    "first_name": "Carlos",
    "last_name": "González",
    "rol": {"id": 2, "nombre": "Residente", "descripcion": "..."},
    "telefono": "...",
    "tipo_documento": "CI",
    "numero_documento": "...",
    "foto": null,
    "vivienda_id": 1,
    "edificio_id": 1,
    "debe_cambiar_password": false
  },
  "debe_cambiar_password": false
}
```

El login devuelve `400` si las credenciales fallan, el correo no está verificado, las credenciales temporales expiraron o el rol debe entrar por web. Actualmente Administrador y Gerente están bloqueados por este endpoint JWT.

| Método | Endpoint | Uso |
|---|---|---|
| POST | `/api/v1/auth/token/refresh/` | Body `{"refresh":"<jwt>"}`; devuelve un nuevo `access`. |
| GET | `/api/v1/auth/me/` | Devuelve el usuario autenticado con el mismo esquema anterior. |
| GET | `/health/` | Health check público: `{"status":"ok",...}`. |

## Áreas comunes y reservas

| Método | Endpoint | Descripción |
|---|---|---|
| GET | `/api/v1/areas-comunes/` | Áreas activas del edificio del usuario. |
| GET | `/api/v1/areas-comunes/{areaId}/disponibilidad/?fecha=2026-07-29&duracion_minutos=60` | Slots libres y hasta tres fechas alternativas. |
| GET | `/api/v1/areas-comunes/{areaId}/reservas/` | Reservas futuras activas del área. |
| POST | `/api/v1/areas-comunes/{areaId}/reservar/` | Crear reserva; solo residentes. |
| GET | `/api/v1/areas-comunes/mis-reservas/` | Últimas 50 reservas del residente. |
| PATCH | `/api/v1/areas-comunes/reservas/{id}/cancelar/` | Cancelar una reserva propia. |

Área común:

```json
{
  "id": 1,
  "nombre": "Salón de eventos",
  "descripcion": "Área para eventos y reuniones",
  "buildingName": "Torre Aurora",
  "capacidad_maxima": 30,
  "horario_inicio": "08:00:00",
  "horario_fin": "22:00:00",
  "imageUrl": null,
  "activo": true
}
```

Crear reserva:

```http
POST /api/v1/areas-comunes/1/reservar/
Idempotency-Key: <uuid-generado-por-mobile>

{
  "fecha": "2026-07-29",
  "hora_inicio": "09:00",
  "hora_fin": "10:00",
  "cantidad_personas": 5,
  "motivo": "Reunión familiar"
}
```

Respuesta `201`: `{"mensaje":"Reserva creada correctamente.","reserva":{...}}`. Un retry con la misma clave y parámetros devuelve `200` y `replayed:true`; reutilizar la clave con otros parámetros devuelve `409`.

## Incidencias

`incidencias.Incidencia` es la fuente de verdad de reportes de mantenimiento. `alertas.Alerta` se usa para alertas, emergencias y anuncios.

| Método | Endpoint | Descripción |
|---|---|---|
| POST | `/api/v1/incidencias/crear/` | Crear incidencia, con evidencias opcionales. Solo residente. |
| GET | `/api/v1/incidencias/mis-incidencias/` | Resumen de las últimas 50 incidencias propias. |
| GET | `/api/v1/incidencias/{id}/` | Detalle con evidencias y timeline. |
| POST | `/api/v1/incidencias/{id}/evidencias/` | Agregar una o varias evidencias. |
| GET | `/api/v1/incidencias/{id}/evidencias/{evidenciaId}/descargar/` | Descarga autenticada del archivo. |
| PATCH | `/api/v1/incidencias/{id}/cambiar-estado/` | Solo Administrador, Gerente o Personal. |

La creación y carga de evidencia usan `multipart/form-data`; repetir el campo `evidencias` para varios archivos:

```bash
curl -X POST "$BASE_URL/api/v1/incidencias/crear/" \
  -H "Authorization: Bearer $ACCESS_TOKEN" \
  -H "Idempotency-Key: incidencia-mobile-001" \
  -F categoria=PLOMERIA \
  -F titulo='Fuga bajo lavaplatos' \
  -F descripcion='El agua sale continuamente' \
  -F ubicacion='Departamento 1A' \
  -F urgencia=ALTA \
  -F evidencias=@foto.jpg
```

Categorías: `PLOMERIA`, `ELECTRICIDAD`, `ASCENSOR`, `SEGURIDAD`, `LIMPIEZA`, `OTRO`. Urgencias: `BAJA`, `MEDIA`, `ALTA`, `CRITICA`. Estados: `REPORTADA`, `EN_REVISION`, `APROBADA`, `EN_PROCESO`, `RESUELTA`, `RECHAZADA`, `CANCELADA`.

Cambio de estado:

```json
{"estado":"EN_PROCESO","comentario":"Técnico asignado"}
```

## Visitas y QR

### Crear autorización

```http
POST /api/v1/accesos/visitas/crear/
Idempotency-Key: <uuid>

{
  "nombre_visitante": "Ana Pérez",
  "documento_visitante": "1234567",
  "vivienda_destino_id": 1,
  "fecha_visita": "2026-07-30",
  "hora_inicio": "18:00",
  "hora_fin": "19:00",
  "cantidad_personas": 2,
  "motivo": "Cena familiar"
}
```

Para un ingreso inmediato se omiten los tres campos de fecha/horario. Una reserva futura exige los tres. La respuesta incluye `id`, `estado`, ventana horaria, `qr_base64`, `qr_payload:{id,nonce,firma}` y `replayed`.

### Gestión de visitantes

| Método | Endpoint | Roles/uso |
|---|---|---|
| GET | `/api/v1/visitantes/?status=pending&search=Ana` | Residente: su vivienda; Vigilante: su edificio; Admin: todas. `status`: `pending`, `scanned`, `departed`. |
| GET | `/api/v1/visitantes/{id}/` | Detalle mobile. |
| DELETE | `/api/v1/visitantes/{id}/` | Residente propietario o Admin; falla si el QR ya fue usado. |
| POST | `/api/v1/visitantes/{id}/report-arrival/` | Vigilante/Admin reporta llegada. Enviar `{}` como JSON. |
| POST | `/api/v1/visitantes/{id}/approve/` | Residente autorizador aprueba llegada. |
| POST | `/api/v1/visitantes/{id}/reject/` | Residente autorizador rechaza llegada. |
| PATCH | `/api/v1/visitantes/{id}/mark-exit/` | Vigilante/Admin registra salida. |
| POST | `/api/v1/accesos/visitas/verificar-qr/` | Vigilante/Admin consume QR una sola vez. |

Verificar QR:

```json
{"id":15,"firma":"<firma-del-qr>","nonce":"<nonce-del-qr>"}
```

Un QR válido devuelve `valido:true` y datos de visitante; un segundo escaneo devuelve `409`. `POST /api/v1/visitantes/` no crea visitas y responde `405`; use siempre `/api/v1/accesos/visitas/crear/`.

El detalle mobile usa camelCase: `name`, `document`, `purpose`, `entryDate`, `exitDate`, `departmentNumber`, `whoAuthorizes`, `status`, `visitDate`, `startTime`, `endTime`, `peopleCount`, `reservationStatus`, `qrUsed`, `photoUrl`, `arrivalReportedAt`, `residentDecisionAt`, `notificationStatus`.

Aunque el servicio admite una foto opcional de llegada, el ViewSet hereda actualmente el parser global JSON y no acepta `multipart/form-data`; mobile no debe enviar `photo` hasta que backend habilite el parser multipart.

## Puertas

| Método | Endpoint | Descripción |
|---|---|---|
| GET | `/api/v1/accesos/puertas/` | Puertas activas permitidas para el usuario. |
| POST | `/api/v1/accesos/puertas/{id}/abrir/` | Crea solicitud pendiente; no abre todavía. |
| GET | `/api/v1/accesos/puertas/aperturas/` | Últimas 50 aperturas visibles. |

La solicitud devuelve `202`:

```json
{
  "abierta": false,
  "requiere_confirmacion": true,
  "accion_id": 27,
  "mensaje": "Confirma la apertura ... con tu contraseña.",
  "puerta": {"id":1,"nombre":"Puerta 1A","tipo":"VIVIENDA","tiene_hardware":false}
}
```

Mobile debe continuar en `POST /api/v1/agente/acciones/27/confirmar/` con `{"password":"..."}`. Solo pueden solicitarse puertas con demo remota habilitada.

## Finanzas

| Método | Endpoint | Descripción |
|---|---|---|
| GET | `/api/v1/financiero/cuotas/pendientes/` | Cuotas pendientes con recargo actualizado. |
| GET | `/api/v1/financiero/cuotas/pagadas/` | Últimas 50 cuotas pagadas. |
| GET | `/api/v1/financiero/pagos/` | Últimos 50 pagos. |
| POST | `/api/v1/financiero/pagos/registrar/` | Registra pago manual pendiente de verificación. |
| POST | `/api/v1/financiero/pagos/qr/generar/` | Genera o reutiliza QR BNB activo. |
| GET | `/api/v1/financiero/pagos/qr/{qrId}/verificar/` | Consulta `GENERADO`, `PAGADO` o `EXPIRADO`. |
| GET | `/api/v1/financiero/pagos/qr/pendientes/` | QRs activos del residente. |

Pago manual:

```json
{
  "cuota_ids": [4, 5],
  "metodo_pago": "TRANSFERENCIA",
  "referencia": "TRX-9081",
  "notas": "Pago desde banca móvil"
}
```

Métodos: `EFECTIVO`, `TRANSFERENCIA`, `CHEQUE`, `TARJETA`, `QR_BNB`, `OTRO`. La respuesta `201` contiene `pago_id`, `monto` y estado `PENDIENTE`. El campo `comprobante` aparece en el serializer pero actualmente no se persiste en este endpoint; mobile no debe enviarlo hasta que backend habilite multipart y almacenamiento.

Generar QR:

```json
{"cuota_ids":[4,5]}
```

La respuesta incluye `qr_id`, `qr_image` en base64, `monto`, `glosa`, `fecha_expiracion` y `mensaje`. Errores del proveedor BNB usan `502`.

## Alertas y anuncios

Alertas admiten tipos `Incendio`, `Sismo`, `Seguridad`, `Salud`, `Aviso importante`, `Reunión` e `Incidencia`; estados `pendiente`, `en_proceso`, `resuelto`.

| Método | Endpoint | Uso |
|---|---|---|
| POST | `/api/v1/alertas/crear/` | Crear `{"tipo":"Seguridad","descripcion":"...","vivienda":1}`; edificio se asigna por usuario. |
| GET | `/api/v1/alertas/mis/` | Alertas creadas por el usuario. |
| GET | `/api/v1/alertas/edificio/` | Últimas 50 del edificio. |
| GET | `/api/v1/alertas/nuevas/?since=<ISO-8601>` | Polling; solo Vigilante/Gerente, máximo 20. |
| GET/POST | `/api/v1/alertas/` | CRUD list/create; el listado es paginado. |
| GET/PUT/PATCH/DELETE | `/api/v1/alertas/{id}/` | Update: Admin/Gerente; delete: Admin. |
| PUT | `/api/v1/alertas/{id}/estado/` | Admin/Gerente; `{"estado":"en_proceso"}`. |
| GET | `/api/v1/alertas/anuncios/` | Anuncios activos del edificio. |
| POST | `/api/v1/alertas/anuncios/crear/` | Crear anuncio; votaciones solo Admin/Gerente. |
| POST | `/api/v1/alertas/anuncios/{id}/votar/` | `{"opcion_id":5}`; también permite cambiar voto. |
| DELETE | `/api/v1/alertas/anuncios/{id}/eliminar/` | Autor, Admin o Gerente; desactivación lógica. |

Categorías de anuncio: `general`, `mantenimiento`, `reunion`, `evento`, `reglas`, `financiero`. Una votación se crea con `es_votacion:true`, `opciones:["Sí","No"]`, `voto_anonimo` y `fecha_cierre_votacion` opcionales.

## Clientes potenciales (módulo opcional)

Estos endpoints pertenecen al formulario comercial y normalmente no se incluyen en la app de residentes:

| Método | Endpoint | Acceso |
|---|---|---|
| POST | `/api/v1/auth/clientes-potenciales/crear/` | Público; requiere `nombre_completo` y `email`; admite `telefono`, `ubicacion`, `mensaje`. |
| POST | `/api/v1/auth/clientes-potenciales/crear-simple/` | Público, vista Django alternativa. No usar si ya se integra la anterior. |
| GET | `/api/v1/auth/clientes-potenciales/` | JWT más permiso Django `usuarios.view_cliente_potencial`. |
| GET | `/api/v1/auth/clientes-potenciales/estadisticas/` | JWT más permiso Django; devuelve totales de semana y mes. |

## Códigos HTTP y estrategia mobile

| Código | Acción recomendada |
|---|---|
| 200/201/202 | Procesar body; `202` de puerta requiere confirmación posterior. |
| 400 | Mostrar validación; no reintentar automáticamente. |
| 401 | Intentar refresh una vez; si falla, cerrar sesión. |
| 403 | Usuario autenticado sin permiso; no repetir. |
| 404 | Recurso inexistente o no visible para el usuario. |
| 409 | Conflicto, acción expirada, replay o estado incompatible; refrescar recurso. |
| 429 | Aplicar backoff respetando el throttle. |
| 502/503 | Dependencia externa o servicio temporalmente indisponible; permitir retry controlado. |

Para mutaciones de reserva, incidencia y visita futura, mobile debe generar una `Idempotency-Key` distinta por intención del usuario y conservarla durante todos los retries de esa operación.
