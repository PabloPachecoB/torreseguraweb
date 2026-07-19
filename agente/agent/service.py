"""Servicio de aplicacion para conversaciones autenticadas."""

from functools import lru_cache
from typing import Dict, Optional
from uuid import UUID, uuid4

from django.core.exceptions import ObjectDoesNotExist
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.types import Command
from langsmith import tracing_context

from agente.models import AgentAction
from agente.observability import SafeTraceRecorder

from .checkpoints import CheckpointRuntime, get_checkpoint_runtime
from .graph import build_agent_graph


ACTION_METADATA = {
    'RESERVA_CREAR': {
        'intent': 'reservation', 'noun': 'reserva',
        'result_id': 'reservation_id', 'result_status': 'reservation_status',
    },
    'INCIDENCIA_CREAR': {
        'intent': 'incident', 'noun': 'incidencia',
        'result_id': 'incident_id', 'result_status': 'incident_status',
    },
    'CERRADURA_ABRIR': {
        'intent': 'lock', 'noun': 'apertura',
        'result_id': 'opening_id', 'result_status': 'hardware_status',
    },
    'VISITA_CREAR': {
        'intent': 'visitor', 'noun': 'visita',
        'result_id': 'visit_id', 'result_status': 'visit_status',
    },
}


class AgentConversationService:
    def __init__(
        self,
        runtime: CheckpointRuntime = None,
        adapter=None,
        reservation_tools=None,
        incident_tools=None,
        door_tools=None,
        visitor_tools=None,
        information_tools=None,
        trace_recorder=None,
    ):
        self.runtime = runtime or get_checkpoint_runtime()
        self.graph = build_agent_graph(
            self.runtime.saver,
            adapter=adapter,
            reservation_tools=reservation_tools,
            incident_tools=incident_tools,
            door_tools=door_tools,
            visitor_tools=visitor_tools,
            information_tools=information_tools,
        )
        self.trace_recorder = trace_recorder or SafeTraceRecorder()

    def chat(
        self,
        user,
        message: str,
        thread_id: Optional[str] = None,
        interaction: Optional[Dict] = None,
    ) -> Dict:
        public_thread_id = self._normalize_thread_id(thread_id)
        internal_thread_id = f"user:{user.pk}:{public_thread_id}"
        pending_action = AgentAction.objects.filter(
            usuario_id=user.pk,
            thread_id=public_thread_id,
            estado=AgentAction.PENDIENTE,
        ).order_by("-fecha_creacion").first()
        if pending_action is not None:
            metadata = self._action_metadata(pending_action.tipo_accion)
            noun = metadata['noun']
            return {
                "thread_id": public_thread_id,
                "message": (
                    f"Hay una {noun} pendiente. Confirma o rechaza la acción "
                    "antes de enviar parámetros nuevos."
                ),
                "intent": metadata['intent'],
                "status": "awaiting_confirmation",
                "error": None,
                "checkpoint_backend": self.runtime.backend,
                "durable": self.runtime.durable,
                "requires_confirmation": True,
                "action_id": pending_action.pk,
                "confirmation": {
                    "type": "action_confirmation",
                    "action_id": pending_action.pk,
                    "requires_password": (
                        pending_action.tipo_accion == 'CERRADURA_ABRIR'
                    ),
                },
                "presentation": None,
                "trace_metadata": {
                    "llm_invoked": False,
                    "guardrail_triggered": False,
                },
            }
        context = self._authenticated_context(user)
        interaction_type = (interaction or {}).get("type")
        effective_message = message or {
            "check_area_availability": "Quiero consultar los horarios de esta área.",
            "start_reservation": "Quiero reservar esta área.",
            "select_reservation_slot": "Seleccioné una opción disponible.",
        }.get(interaction_type, "Continuar.")
        input_state = {
            "messages": [HumanMessage(content=effective_message)],
            "thread_id": public_thread_id,
            "user_id": user.pk,
            "residence_id": context.get("resident_id"),
            "apartment_id": context.get("apartment_id"),
            "authenticated_context": context,
            "interaction": interaction,
        }
        # La traza automática incluiría mensajes y contexto. Se desactiva y se
        # registra después un resumen explícitamente sanitizado.
        with tracing_context(enabled=False):
            result = self.graph.invoke(
                input_state,
                config={"configurable": {"thread_id": internal_thread_id}},
            )
        response = self._build_response(public_thread_id, result)
        self._record_trace(response)
        return response

    def resume_confirmation(self, user, action: AgentAction, approved: bool) -> Dict:
        if action.usuario_id != user.pk:
            raise PermissionError("La acción pertenece a otro usuario.")
        if not action.thread_id:
            raise ValueError("La acción no está vinculada a una conversación.")

        action.refresh_from_db()
        if approved and action.estado == AgentAction.EJECUTADA:
            result = action.resultado or {}
            metadata = self._action_metadata(action.tipo_accion)
            reference = result.get(metadata['result_id'])
            backend_status = result.get(metadata['result_status'])
            noun = metadata['noun'].capitalize()
            succeeded = result.get('status') == 'success'
            message = (
                f"{noun} creada y verificada. ID {reference}; "
                f"estado real: {backend_status}."
            )
            if action.tipo_accion == 'CERRADURA_ABRIR':
                message = (
                    f"Apertura verificada. Registro {reference}; "
                    f"estado del hardware: {backend_status}."
                    if succeeded
                    else result.get('message', 'El hardware no confirmó la apertura.')
                )
            return {
                "thread_id": action.thread_id,
                "message": message,
                "intent": metadata['intent'],
                "status": "ok" if succeeded else "error",
                "error": (
                    None
                    if succeeded
                    else {
                        'code': result.get('error_code') or 'action_failed',
                        'message': message,
                    }
                ),
                "checkpoint_backend": self.runtime.backend,
                "durable": self.runtime.durable,
                "requires_confirmation": False,
                "action_id": action.pk,
                "presentation": None,
                "trace_metadata": {
                    "llm_invoked": False,
                    "guardrail_triggered": False,
                },
            }
        if not approved and action.estado == AgentAction.RECHAZADA:
            metadata = self._action_metadata(action.tipo_accion)
            noun = metadata['noun']
            return {
                "thread_id": action.thread_id,
                "message": f"Acción rechazada. No se creó la {noun}.",
                "intent": metadata['intent'],
                "status": "ok",
                "error": None,
                "checkpoint_backend": self.runtime.backend,
                "durable": self.runtime.durable,
                "requires_confirmation": False,
                "action_id": action.pk,
                "presentation": None,
                "trace_metadata": {
                    "llm_invoked": False,
                    "guardrail_triggered": False,
                },
            }

        internal_thread_id = f"user:{user.pk}:{action.thread_id}"
        with tracing_context(enabled=False):
            result = self.graph.invoke(
                Command(resume={"approved": approved, "action_id": action.pk}),
                config={"configurable": {"thread_id": internal_thread_id}},
            )
        response = self._build_response(action.thread_id, result)
        response["intent"] = self._action_metadata(action.tipo_accion)['intent']
        self._record_trace(response)
        return response

    def _build_response(self, public_thread_id: str, result: Dict) -> Dict:
        assistant_message = self._last_assistant_message(result["messages"])
        interrupts = result.get("__interrupt__", ())
        pending = interrupts[0].value if interrupts else None
        trace_metadata = dict(result.get("trace_metadata", {}) or {})
        trace_metadata.setdefault(
            "llm_invoked",
            bool(result.get("llm_invoked", False)),
        )
        trace_metadata.setdefault(
            "guardrail_triggered",
            bool(result.get("guardrail_triggered", False)),
        )
        response = {
            "thread_id": public_thread_id,
            "message": assistant_message,
            "intent": result.get("intent", "general"),
            "status": (
                "awaiting_confirmation"
                if pending
                else "error" if result.get("error") else "ok"
            ),
            "error": result.get("error"),
            "checkpoint_backend": self.runtime.backend,
            "durable": self.runtime.durable,
            "requires_confirmation": bool(pending),
            "action_id": (
                pending.get("action_id")
                if pending
                else result.get("pending_action_id")
            ),
            "trace_metadata": trace_metadata,
            "presentation": result.get("presentation"),
        }
        if pending:
            response["confirmation"] = pending
        return response

    def _record_trace(self, response: Dict):
        metadata = dict(response.get("trace_metadata", {}))
        metadata.update(
            {
                "intent": response.get("intent", "general"),
                "outcome": response.get("status", "unknown"),
                "action_type": {
                    "reservation": "RESERVA_CREAR",
                    "incident": "INCIDENCIA_CREAR",
                    "lock": "CERRADURA_ABRIR",
                    "visitor": "VISITA_CREAR",
                }.get(response.get("intent"), ""),
            }
        )
        self.trace_recorder.record(metadata)

    @staticmethod
    def _normalize_thread_id(thread_id: Optional[str]) -> str:
        if not thread_id:
            return str(uuid4())
        try:
            return str(UUID(str(thread_id)))
        except (TypeError, ValueError, AttributeError) as exc:
            raise ValueError("thread_id debe ser un UUID valido.") from exc

    @staticmethod
    def _action_metadata(action_type: str) -> Dict[str, str]:
        return ACTION_METADATA.get(
            action_type,
            {
                'intent': 'general', 'noun': 'acción',
                'result_id': 'id', 'result_status': 'status',
            },
        )

    @staticmethod
    def _authenticated_context(user) -> Dict:
        context = {
            "user_id": user.pk,
            "resident_id": None,
            "apartment_id": None,
            "building_id": None,
            "condominium_id": None,
            "resident_active": False,
        }
        try:
            resident = user.residente
        except ObjectDoesNotExist:
            return context

        context.update(
            {
                "resident_id": resident.pk,
                "apartment_id": resident.vivienda_id,
                "building_id": (
                    resident.vivienda.edificio_id if resident.vivienda_id else None
                ),
                "condominium_id": (
                    resident.vivienda.edificio.condominio_id
                    if resident.vivienda_id else None
                ),
                "resident_active": resident.activo,
            }
        )
        return context

    @staticmethod
    def _last_assistant_message(messages) -> str:
        for message in reversed(messages):
            if isinstance(message, AIMessage):
                return str(message.content)
        return ""


@lru_cache(maxsize=1)
def get_conversation_service() -> AgentConversationService:
    return AgentConversationService()
