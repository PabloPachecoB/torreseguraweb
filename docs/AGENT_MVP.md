# TorreSegura Agent MVP

El agente vive dentro de Django para reutilizar JWT, permisos y reglas de dominio. Qwen clasifica la intención y extrae campos estructurados; LangGraph mantiene el estado. `areas_comunes.Reserva` e `incidencias.Incidencia` son las fuentes de verdad. `alertas.Alerta` sigue reservado para alertas, emergencias y anuncios.

## Inicio local

```bash
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
ollama serve
ollama pull qwen3:8b
python manage.py migrate
python manage.py check_agent_model
python manage.py runserver
```

Para desarrollo use `LLM_PROVIDER=qwen_local`, `QWEN_BASE_URL=http://127.0.0.1:11434/v1` y `AGENT_CHECKPOINT_BACKEND=memory`. Memoria no sobrevive al reinicio del proceso. Para la demo durable configure:

```env
AGENT_CHECKPOINT_BACKEND=postgres
AGENT_CHECKPOINT_DATABASE_URL=postgresql://usuario:clave@host:5432/torresegura
LANGGRAPH_STRICT_MSGPACK=true
```

El primer arranque ejecuta `PostgresSaver.setup()` de forma idempotente.

## Contrato móvil

Obtenga JWT en `POST /api/v1/auth/token/`. Envíe cada turno a:

```http
POST /api/v1/agente/acciones/chat/
Authorization: Bearer <access>
Content-Type: application/json

{"message":"Reserva Salón mañana 09:00 a 10:00 para 5 personas","thread_id":null}
```

Conserve el `thread_id` devuelto. Si `status=awaiting_confirmation`, muestre `message` y `confirmation`; no interprete un “sí” de texto como consentimiento. Use el `action_id` autenticado:

`trace_metadata.llm_invoked` indica si el turno realizó una inferencia. Reservas
e incidencias usan Qwen para clasificación y extracción JSON validada con
Pydantic. Qwen no autoriza ni ejecuta: disponibilidad, permisos, confirmación,
escritura y verificación pertenecen a las tools Django. En respuestas generales,
una guardia determinista reemplaza afirmaciones de ejecución no respaldadas;
`guardrail_triggered=true` permite auditar ese caso.

```http
POST /api/v1/agente/acciones/{action_id}/confirmar/
POST /api/v1/agente/acciones/{action_id}/rechazar/
```

La confirmación crea y verifica una sola reserva o incidencia. Para evidencia de una incidencia ya creada:

Si el usuario cambia parámetros después del resumen, primero rechace la acción
pendiente y envíe nuevamente la solicitud completa. El backend no acepta texto
como confirmación y nunca reutiliza el consentimiento para parámetros distintos.

```http
POST /api/v1/incidencias/{incident_id}/evidencias/
Content-Type: multipart/form-data
evidencias=<archivo>
```

## Qwen Cloud y observabilidad

Cambiar a Qwen Cloud solo requiere `LLM_PROVIDER=qwen_cloud`, `QWEN_MODEL`, `QWEN_BASE_URL` y `QWEN_API_KEY`; el grafo y las tools no cambian.

LangSmith está apagado por defecto. Al definir `LANGSMITH_TRACING=true` y `LANGSMITH_API_KEY`, se registra únicamente intención, proveedor, modelo, versión del grafo, uso del LLM, activación de la guardia, tool y resultado. La captura automática del estado se desactiva: no se envían mensajes, JWT, IDs de usuario, ubicaciones, archivos ni payloads.

## Verificación

```bash
python manage.py test agente areas_comunes incidencias
python manage.py makemigrations --check --dry-run
python manage.py check_agent_model
python manage.py validate_agent_dataset
```

El dataset reproducible está en `agente/evaluation/dataset.json`. PostgreSQL debe estar disponible para validar persistencia real entre procesos; con `memory` solo se prueba reanudación al reconstruir el grafo dentro del proceso.
