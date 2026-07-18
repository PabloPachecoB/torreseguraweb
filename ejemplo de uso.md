### Nota:
    Con `checkpoint_backend=memory`, el hilo se mantiene mientras Django no se reinicie y se use un solo proceso.

Estos ejemplos pueden ejecutarlos backend, móvil y QA.

## Preparación común

```bash
export API_URL="http://127.0.0.1:8000"

read -rp "Usuario: " TS_USER
read -srp "Contraseña: " TS_PASSWORD
echo

ACCESS_TOKEN=$(curl -sS "$API_URL/api/v1/auth/token/" \
  -H 'Content-Type: application/json' \
  -d "$(jq -n \
    --arg username "$TS_USER" \
    --arg password "$TS_PASSWORD" \
    '{username:$username,password:$password}')" |
  jq -er '.access')

unset TS_PASSWORD
```

El usuario debe ser un residente activo, con vivienda y áreas comunes asignadas.

## Prueba 1: reserva en varios turnos

Primer mensaje, incompleto:

```bash
R1=$(curl -sS "$API_URL/api/v1/agente/acciones/chat/" \
  -H "Authorization: Bearer $ACCESS_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{
    "message":"Quiero organizar una reunión familiar en el Salón de eventos."
  }')

echo "$R1" | jq
```

Guarda el hilo:

```bash
R_THREAD=$(echo "$R1" | jq -er '.thread_id')
```

Completa los datos usando el mismo `thread_id`:

```bash
R2=$(curl -sS "$API_URL/api/v1/agente/acciones/chat/" \
  -H "Authorization: Bearer $ACCESS_TOKEN" \
  -H 'Content-Type: application/json' \
  -d "$(jq -n \
    --arg thread_id "$R_THREAD" \
    --arg message "Será el 2026-09-10, de 15:00 a 16:00, para 8 personas." \
    '{thread_id:$thread_id,message:$message}')")

echo "$R2" | jq
```

Validación esperada:

```bash
echo "$R2" | jq '{
  thread_id,
  intent,
  status,
  requires_confirmation,
  action_id,
  model_provider: .trace_metadata.model_provider,
  model_name: .trace_metadata.model_name,
  llm_invoked: .trace_metadata.llm_invoked
}'
```

Debe aparecer:

- `intent: reservation`
- `status: awaiting_confirmation`
- `model_provider: qwen_cloud`
- `requires_confirmation: true`

Confirma:

```bash
R_ACTION=$(echo "$R2" | jq -er '.action_id')

R_CONFIRM=$(curl -sS -X POST \
  "$API_URL/api/v1/agente/acciones/$R_ACTION/confirmar/" \
  -H "Authorization: Bearer $ACCESS_TOKEN")

echo "$R_CONFIRM" | jq '{
  estado,
  verification_status,
  resultado,
  backend_reference
}'
```

Debe quedar `EJECUTADA` y `VERIFICADA`.

## Prueba 2: incidencia en varios turnos

Primer mensaje sin ubicación:

```bash
I1=$(curl -sS "$API_URL/api/v1/agente/acciones/chat/" \
  -H "Authorization: Bearer $ACCESS_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{
    "message":"Necesito reportar una fuga de agua que comenzó esta mañana y continúa empeorando."
  }')

echo "$I1" | jq
```

Guarda el hilo:

```bash
I_THREAD=$(echo "$I1" | jq -er '.thread_id')
```

Completa la ubicación:

```bash
I2=$(curl -sS "$API_URL/api/v1/agente/acciones/chat/" \
  -H "Authorization: Bearer $ACCESS_TOKEN" \
  -H 'Content-Type: application/json' \
  -d "$(jq -n \
    --arg thread_id "$I_THREAD" \
    --arg message "La ubicación es el baño de mi departamento, detrás del inodoro." \
    '{thread_id:$thread_id,message:$message}')")

echo "$I2" | jq
```

La respuesta debe contener clasificación y urgencia preliminares, disclaimer y `awaiting_confirmation`.

Confirma:

```bash
I_ACTION=$(echo "$I2" | jq -er '.action_id')

I_CONFIRM=$(curl -sS -X POST \
  "$API_URL/api/v1/agente/acciones/$I_ACTION/confirmar/" \
  -H "Authorization: Bearer $ACCESS_TOKEN")

echo "$I_CONFIRM" | jq '{
  estado,
  verification_status,
  resultado,
  backend_reference
}'
```

Debe devolver un `incident_id` real y estado `REPORTADA`.

## Prueba 3: “sí” por chat no confirma

Antes de confirmar una acción pendiente:

```bash
curl -sS "$API_URL/api/v1/agente/acciones/chat/" \
  -H "Authorization: Bearer $ACCESS_TOKEN" \
  -H 'Content-Type: application/json' \
  -d "$(jq -n \
    --arg thread_id "$R_THREAD" \
    '{thread_id:$thread_id,message:"Sí, confirma la acción"}')" |
  jq
```

No debe ejecutar la acción. La confirmación válida solo se realiza mediante:

```text
POST /api/v1/agente/acciones/{action_id}/confirmar/
```

## Qué debe comprobar cada integrante

- **Backend:** estados, idempotencia, registros reales y permisos.
- **App móvil:** conservar `thread_id`, mostrar mensajes, resumen y botón de confirmación.
- **QA:** probar campos incompletos, rechazo, conflictos de horario, doble confirmación y acceso con otro usuario.
- **Agente:** verificar `model_provider=qwen_cloud`, `llm_invoked=true` y que no se inventen IDs o estados.

Mientras se use `memory`, el equipo no debe reiniciar Django entre turnos. Para pruebas con reinicios o varios workers será necesario activar PostgreSQL como checkpointer.