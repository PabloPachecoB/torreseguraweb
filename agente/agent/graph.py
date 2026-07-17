"""Grafo conversacional base del agente TorreSegura."""

from datetime import timedelta
import hashlib
import json
from typing import Dict, Iterable, List

from django.utils import timezone
from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, SystemMessage
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from agente.llm import QwenAdapter, get_llm_adapter
from agente.models import AgentAction
from agente.tools import IncidentTools, ReservationTools

from .guardrails import guard_unverified_action_claim
from .nlu import QwenNLU
from .state import AgentState


GRAPH_VERSION = "0.3.0"


class AgentGraphNodes:
    def __init__(
        self,
        adapter: QwenAdapter = None,
        reservation_tools=None,
        incident_tools=None,
    ):
        self.adapter = adapter or get_llm_adapter()
        self.nlu = QwenNLU(self.adapter)
        self.reservation_tools = reservation_tools or ReservationTools()
        self.incident_tools = incident_tools or IncidentTools()

    def load_authenticated_context(self, state: AgentState) -> Dict:
        context = state.get("authenticated_context", {})
        if context.get("user_id") != state.get("user_id"):
            return {
                "error": {
                    "code": "unauthorized_context",
                    "message": "El contexto autenticado no coincide con el usuario.",
                }
            }
        return {
            "residence_id": context.get("resident_id"),
            "apartment_id": context.get("apartment_id"),
            "collected_fields": state.get("collected_fields", {}),
            "missing_fields": state.get("missing_fields", []),
            "confirmation_status": state.get("confirmation_status", "not_required"),
            "verification_status": state.get("verification_status", "not_started"),
            "llm_invoked": False,
            "guardrail_triggered": False,
        }

    def classify_intent(self, state: AgentState) -> Dict:
        result = self.nlu.classify(
            self._last_human_text(state.get("messages", [])),
            current_intent=state.get("intent"),
        )
        if result.get("status") != "success":
            return self._nlu_error(result)
        return {
            "intent": result["data"].intent,
            "llm_invoked": True,
        }

    @staticmethod
    def route_intent(state: AgentState) -> str:
        if state.get("error"):
            return "generate_response"
        if state.get("intent") == "reservation":
            return "collect_reservation_information"
        if state.get("intent") == "incident":
            return "collect_incident_information"
        return "generate_response"

    def collect_reservation_information(self, state: AgentState) -> Dict:
        context = state.get("authenticated_context", {})
        areas_result = self.reservation_tools.list_areas(context)
        if areas_result.get("status") != "success":
            return {
                "error": {
                    "code": areas_result.get("error_code", "unauthorized"),
                    "message": areas_result.get("message", "No puedes realizar reservas."),
                }
            }

        areas = areas_result["areas"]
        extraction = self.nlu.extract_reservation(
            message=self._last_human_text(state.get("messages", [])),
            existing_fields=dict(state.get("collected_fields", {})),
            authorized_areas=areas,
            current_date=timezone.localdate(),
        )
        if extraction.get("status") != "success":
            return self._nlu_error(extraction)

        fields = dict(state.get("collected_fields", {}))
        extracted_fields = extraction["data"].as_state_fields()
        allowed_area_ids = {item["id"] for item in areas}
        if (
            "area_id" in extracted_fields
            and extracted_fields["area_id"] not in allowed_area_ids
        ):
            extracted_fields.pop("area_id")
        fields.update(extracted_fields)

        required = ["area_id", "date", "start_time", "end_time", "attendees"]
        missing = [field for field in required if not fields.get(field)]
        return {
            "collected_fields": fields,
            "missing_fields": missing,
            "tool_result": {"status": "success", "areas": areas},
            "error": None,
            "llm_invoked": True,
        }

    @staticmethod
    def route_reservation_fields(state: AgentState) -> str:
        if state.get("error"):
            return "generate_response"
        if state.get("missing_fields"):
            return "generate_missing_fields_response"
        return "query_reservation_availability"

    def generate_missing_fields_response(self, state: AgentState) -> Dict:
        labels = {
            "area_id": "área",
            "date": "fecha",
            "start_time": "hora de inicio",
            "end_time": "hora de fin",
            "attendees": "cantidad de personas",
        }
        missing = ", ".join(labels[item] for item in state["missing_fields"])
        areas = state.get("tool_result", {}).get("areas", [])
        area_names = ", ".join(
            f'{item["name"]} (área {item["id"]})' for item in areas
        )
        content = f"Para consultar la reserva falta: {missing}."
        if "area_id" in state["missing_fields"] and area_names:
            content += f" Áreas disponibles en tu edificio: {area_names}."
        content += " Puedes usar fecha YYYY-MM-DD y horario HH:MM a HH:MM."
        return {"messages": [AIMessage(content=content)]}

    def query_reservation_availability(self, state: AgentState) -> Dict:
        parameters = dict(state["collected_fields"])
        parameters.setdefault("reason", "")
        result = self.reservation_tools.get_availability(
            state["authenticated_context"],
            parameters,
        )
        return {"tool_result": result}

    @staticmethod
    def route_availability(state: AgentState) -> str:
        if state.get("tool_result", {}).get("status") == "success":
            return "prepare_reservation_action"
        return "generate_availability_response"

    def generate_availability_response(self, state: AgentState) -> Dict:
        result = state.get("tool_result", {})
        content = result.get("message", "No se pudo consultar la disponibilidad.")
        alternatives = result.get("alternatives", [])
        if alternatives:
            rendered = []
            for option in alternatives:
                slots = ", ".join(
                    f'{slot["hora_inicio"]}-{slot["hora_fin"]}'
                    for slot in option["slots"]
                )
                rendered.append(f'{option["date"]}: {slots}')
            content += " Alternativas reales: " + "; ".join(rendered) + "."
        return {"messages": [AIMessage(content=content)]}

    def prepare_reservation_action(self, state: AgentState) -> Dict:
        parameters = dict(state["collected_fields"])
        parameters.setdefault("reason", "")
        availability = state["tool_result"]
        area_name = availability["area"]["name"]
        summary = (
            f"Reservar {area_name} el {parameters['date']} de "
            f"{parameters['start_time']} a {parameters['end_time']} para "
            f"{parameters['attendees']} personas."
        )
        canonical = json.dumps(parameters, sort_keys=True, separators=(",", ":"))
        idempotency_key = hashlib.sha256(
            (
                f"{state['user_id']}:{state['thread_id']}:"
                f"{ReservationTools.action_type}:{canonical}"
            ).encode("utf-8")
        ).hexdigest()
        action, _ = AgentAction.objects.get_or_create(
            usuario_id=state["user_id"],
            idempotency_key=idempotency_key,
            defaults={
                "thread_id": state["thread_id"],
                "tipo_accion": ReservationTools.action_type,
                "payload": parameters,
                "requires_confirmation": True,
                "tool_name": ReservationTools.create_tool_name,
                "expira_en": timezone.now() + timedelta(minutes=10),
            },
        )
        return {
            "proposed_action": {
                "action_type": ReservationTools.action_type,
                "parameters": parameters,
                "summary": summary,
            },
            "pending_action_id": action.pk,
            "confirmation_status": "pending",
            "messages": [
                AIMessage(
                    content=(
                        f"Confirma esta acción: {summary} "
                        "La confirmación vence en 10 minutos."
                    )
                )
            ],
        }

    def request_confirmation(self, state: AgentState) -> Dict:
        action_id = state["pending_action_id"]
        decision = interrupt(
            {
                "type": "action_confirmation",
                "action_id": action_id,
                "summary": state["proposed_action"]["summary"],
                "expires_in_seconds": 600,
            }
        )
        if not isinstance(decision, dict) or decision.get("action_id") != action_id:
            return {
                "error": {
                    "code": "invalid_confirmation",
                    "message": "La confirmación no corresponde a la acción pendiente.",
                },
                "confirmation_status": "invalid",
            }

        try:
            action = AgentAction.objects.get(
                pk=action_id,
                usuario_id=state["user_id"],
            )
            if decision.get("approved"):
                action.confirmar(action.usuario)
                action.confirmation_method = "authenticated_api"
                action.save(update_fields=["confirmation_method"])
                return {"confirmation_status": "confirmed"}
            action.rechazar(action.usuario)
            action.confirmation_method = "authenticated_api"
            action.save(update_fields=["confirmation_method"])
            return {"confirmation_status": "rejected"}
        except (AgentAction.DoesNotExist, PermissionError, ValueError) as exc:
            return {
                "error": {
                    "code": "confirmation_failed",
                    "message": str(exc),
                },
                "confirmation_status": "invalid",
            }

    @staticmethod
    def route_confirmation(state: AgentState) -> str:
        if state.get("error"):
            return "generate_response"
        if state.get("confirmation_status") == "confirmed":
            if state.get("proposed_action", {}).get("action_type") == IncidentTools.action_type:
                return "execute_incident"
            return "execute_reservation"
        return "generate_rejected_response"

    def execute_reservation(self, state: AgentState) -> Dict:
        result = self.reservation_tools.create_reservation(
            action_id=state["pending_action_id"],
            user_id=state["user_id"],
        )
        return {"tool_result": result}

    @staticmethod
    def route_execution(state: AgentState) -> str:
        if state.get("tool_result", {}).get("status") == "success":
            return "verify_reservation"
        return "generate_reservation_result"

    def verify_reservation(self, state: AgentState) -> Dict:
        result = self.reservation_tools.verify_reservation(
            action_id=state["pending_action_id"],
            user_id=state["user_id"],
        )
        return {
            "verification_status": (
                "verified" if result.get("status") == "success" else "unknown"
            ),
            "tool_result": result,
        }

    def generate_reservation_result(self, state: AgentState) -> Dict:
        result = state.get("tool_result", {})
        if result.get("status") == "success" and state.get("verification_status") == "verified":
            content = (
                f"Reserva creada y verificada. ID {result['reservation_id']}; "
                f"estado real: {result['reservation_status']}."
            )
        else:
            content = result.get(
                "message",
                "No se pudo verificar la creación de la reserva.",
            )
        return {
            "messages": [AIMessage(content=content)],
            "intent": "general",
            "collected_fields": {},
            "missing_fields": [],
            "proposed_action": None,
            "pending_action_id": None,
            "confirmation_status": "not_required",
        }

    def collect_incident_information(self, state: AgentState) -> Dict:
        context = state.get("authenticated_context", {})
        if not context.get("resident_active") or not context.get("apartment_id"):
            return {
                "error": {
                    "code": "resident_context_required",
                    "message": "Necesitas un residente activo con vivienda para reportar.",
                }
            }

        extraction = self.nlu.extract_incident(
            message=self._last_human_text(state.get("messages", [])).strip(),
            existing_fields=dict(state.get("collected_fields", {})),
        )
        if extraction.get("status") != "success":
            return self._nlu_error(extraction)

        fields = dict(state.get("collected_fields", {}))
        fields.update(extraction["data"].as_state_fields())
        classification = None
        if fields.get("category") and fields.get("urgency"):
            classification = self.incident_tools.build_preliminary_estimate(
                category=fields["category"],
                urgency=fields["urgency"],
            )
            fields["preliminary_estimate"] = classification
        missing = [
            name
            for name in ("title", "description", "location", "category", "urgency")
            if not fields.get(name)
        ]
        return {
            "collected_fields": fields,
            "missing_fields": missing,
            "tool_result": {
                "status": "success",
                "classification": classification,
            },
            "error": None,
            "llm_invoked": True,
        }

    @staticmethod
    def route_incident_fields(state: AgentState) -> str:
        if state.get("error"):
            return "generate_response"
        if state.get("missing_fields"):
            return "generate_incident_missing_response"
        return "prepare_incident_action"

    @staticmethod
    def generate_incident_missing_response(state: AgentState) -> Dict:
        labels = {
            "title": "título",
            "description": "descripción",
            "location": "ubicación",
            "category": "categoría preliminar",
            "urgency": "urgencia preliminar",
        }
        missing = ", ".join(labels[item] for item in state["missing_fields"])
        classification = state.get("tool_result", {}).get("classification", {})
        content = f"Para preparar el reporte falta: {missing}."
        if "location" in state["missing_fields"]:
            content += " Indícala como “ubicación: ...”."
        if classification:
            content += (
                f" Clasificación preliminar: {classification['category']}; "
                f"urgencia estimada: {classification['urgency']}. "
                f"{classification['disclaimer']}"
            )
        return {"messages": [AIMessage(content=content)]}

    def prepare_incident_action(self, state: AgentState) -> Dict:
        fields = state["collected_fields"]
        parameters = {
            key: fields[key]
            for key in (
                "title",
                "description",
                "location",
                "category",
                "urgency",
                "preliminary_estimate",
            )
        }
        estimate = parameters["preliminary_estimate"]
        summary = (
            f"Reportar “{parameters['title']}” en {parameters['location']}. "
            f"Categoría preliminar {parameters['category']}; urgencia estimada "
            f"{parameters['urgency']}. {estimate['response_window']} "
            f"{estimate['cost_note']} {estimate['disclaimer']}"
        )
        canonical = json.dumps(parameters, sort_keys=True, separators=(",", ":"))
        idempotency_key = hashlib.sha256(
            (
                f"{state['user_id']}:{state['thread_id']}:"
                f"{IncidentTools.action_type}:{canonical}"
            ).encode("utf-8")
        ).hexdigest()
        action, _ = AgentAction.objects.get_or_create(
            usuario_id=state["user_id"],
            idempotency_key=idempotency_key,
            defaults={
                "thread_id": state["thread_id"],
                "tipo_accion": IncidentTools.action_type,
                "payload": parameters,
                "requires_confirmation": True,
                "tool_name": IncidentTools.create_tool_name,
                "expira_en": timezone.now() + timedelta(minutes=10),
            },
        )
        return {
            "proposed_action": {
                "action_type": IncidentTools.action_type,
                "parameters": parameters,
                "summary": summary,
            },
            "pending_action_id": action.pk,
            "confirmation_status": "pending",
            "messages": [
                AIMessage(
                    content=(
                        f"Confirma la creación de esta incidencia: {summary} "
                        "La confirmación vence en 10 minutos."
                    )
                )
            ],
        }

    def execute_incident(self, state: AgentState) -> Dict:
        result = self.incident_tools.create_incident(
            action_id=state["pending_action_id"],
            user_id=state["user_id"],
        )
        return {"tool_result": result}

    @staticmethod
    def route_incident_execution(state: AgentState) -> str:
        if state.get("tool_result", {}).get("status") == "success":
            return "verify_incident"
        return "generate_incident_result"

    def verify_incident(self, state: AgentState) -> Dict:
        result = self.incident_tools.verify_incident(
            action_id=state["pending_action_id"],
            user_id=state["user_id"],
        )
        return {
            "verification_status": (
                "verified" if result.get("status") == "success" else "unknown"
            ),
            "tool_result": result,
        }

    @staticmethod
    def generate_incident_result(state: AgentState) -> Dict:
        result = state.get("tool_result", {})
        if result.get("status") == "success" and state.get("verification_status") == "verified":
            content = (
                f"Incidencia creada y verificada. ID {result['incident_id']}; "
                f"estado real: {result['incident_status']}. Puedes adjuntar evidencia "
                f"en /api/v1/incidencias/{result['incident_id']}/evidencias/."
            )
        else:
            content = result.get(
                "message",
                "No se pudo verificar la creación de la incidencia.",
            )
        return {
            "messages": [AIMessage(content=content)],
            "intent": "general",
            "collected_fields": {},
            "missing_fields": [],
            "proposed_action": None,
            "pending_action_id": None,
            "confirmation_status": "not_required",
        }

    @staticmethod
    def generate_rejected_response(state: AgentState) -> Dict:
        noun = (
            "incidencia"
            if state.get("proposed_action", {}).get("action_type") == IncidentTools.action_type
            else "reserva"
        )
        return {
            "messages": [AIMessage(content=f"Acción rechazada. No se creó la {noun}.")],
            "intent": "general",
            "collected_fields": {},
            "missing_fields": [],
            "proposed_action": None,
            "pending_action_id": None,
            "confirmation_status": "not_required",
        }

    def generate_response(self, state: AgentState) -> Dict:
        if state.get("error"):
            return {
                "messages": [AIMessage(content=state["error"]["message"])],
            }

        system_message = SystemMessage(
            content=(
                "Eres el asistente de TorreSegura. Responde en español, de forma "
                "breve y útil. Esta ruta no puede ejecutar ni confirmar acciones. "
                "No inventes datos ni digas que una reserva, incidencia, visita o "
                "evento está reservado, confirmado, programado, creado o registrado. "
                "Si te piden redactar una confirmación, identifícala explícitamente "
                "como borrador no operativo e indica que no se ejecutó ninguna acción. "
                "Las mutaciones requieren confirmación autenticada, herramientas del "
                "backend y verificación posterior."
            )
        )
        provider_messages = self._to_provider_messages(
            [system_message, *state.get("messages", [])]
        )
        result = self.adapter.chat(provider_messages)
        if not result.get("healthy"):
            return {
                "llm_invoked": True,
                "guardrail_triggered": False,
                "error": {
                    "code": result.get("error_code", "provider_unavailable"),
                    "message": "El asistente no está disponible temporalmente.",
                },
                "messages": [
                    AIMessage(
                        content="El asistente no está disponible temporalmente."
                    )
                ],
            }
        safe_response, guardrail_triggered = guard_unverified_action_claim(
            result["response"]
        )
        return {
            "messages": [AIMessage(content=safe_response)],
            "llm_invoked": True,
            "guardrail_triggered": guardrail_triggered,
        }

    def audit_execution(self, state: AgentState) -> Dict:
        llm_invoked = bool(state.get("llm_invoked", False))
        return {
            "trace_metadata": {
                "graph_version": GRAPH_VERSION,
                "intent": state.get("intent", "general"),
                "outcome": "error" if state.get("error") else "success",
                "model_provider": self.adapter.provider,
                "model_name": self.adapter.model,
                "llm_invoked": llm_invoked,
                "guardrail_triggered": bool(
                    state.get("guardrail_triggered", False)
                ),
                "tool_name": (
                    ReservationTools.create_tool_name
                    if state.get("intent") == "reservation"
                    else IncidentTools.create_tool_name
                    if state.get("intent") == "incident"
                    else ""
                ),
            }
        }

    @staticmethod
    def _nlu_error(result: Dict) -> Dict:
        return {
            "llm_invoked": True,
            "error": {
                "code": result.get("error_code", "invalid_nlu_output"),
                "message": result.get(
                    "message",
                    "No pude interpretar la solicitud de forma segura.",
                ),
            },
        }

    @staticmethod
    def _last_human_text(messages: Iterable[AnyMessage]) -> str:
        for message in reversed(list(messages)):
            if isinstance(message, HumanMessage):
                return str(message.content)
        return ""

    @staticmethod
    def _to_provider_messages(messages: Iterable[AnyMessage]) -> List[Dict[str, str]]:
        result = []
        for message in messages:
            if isinstance(message, SystemMessage):
                role = "system"
            elif isinstance(message, AIMessage):
                role = "assistant"
            else:
                role = "user"
            result.append({"role": role, "content": str(message.content)})
        return result


def build_agent_graph(
    checkpointer,
    adapter: QwenAdapter = None,
    reservation_tools=None,
    incident_tools=None,
):
    nodes = AgentGraphNodes(
        adapter=adapter,
        reservation_tools=reservation_tools,
        incident_tools=incident_tools,
    )
    builder = StateGraph(AgentState)
    builder.add_node("load_authenticated_context", nodes.load_authenticated_context)
    builder.add_node("classify_intent", nodes.classify_intent)
    builder.add_node("collect_reservation_information", nodes.collect_reservation_information)
    builder.add_node("generate_missing_fields_response", nodes.generate_missing_fields_response)
    builder.add_node("query_reservation_availability", nodes.query_reservation_availability)
    builder.add_node("generate_availability_response", nodes.generate_availability_response)
    builder.add_node("prepare_reservation_action", nodes.prepare_reservation_action)
    builder.add_node("request_confirmation", nodes.request_confirmation)
    builder.add_node("execute_reservation", nodes.execute_reservation)
    builder.add_node("verify_reservation", nodes.verify_reservation)
    builder.add_node("generate_reservation_result", nodes.generate_reservation_result)
    builder.add_node("generate_rejected_response", nodes.generate_rejected_response)
    builder.add_node("collect_incident_information", nodes.collect_incident_information)
    builder.add_node("generate_incident_missing_response", nodes.generate_incident_missing_response)
    builder.add_node("prepare_incident_action", nodes.prepare_incident_action)
    builder.add_node("execute_incident", nodes.execute_incident)
    builder.add_node("verify_incident", nodes.verify_incident)
    builder.add_node("generate_incident_result", nodes.generate_incident_result)
    builder.add_node("generate_response", nodes.generate_response)
    builder.add_node("audit_execution", nodes.audit_execution)

    builder.add_edge(START, "load_authenticated_context")
    builder.add_edge("load_authenticated_context", "classify_intent")
    builder.add_conditional_edges("classify_intent", nodes.route_intent)
    builder.add_conditional_edges(
        "collect_reservation_information",
        nodes.route_reservation_fields,
    )
    builder.add_edge("generate_missing_fields_response", "audit_execution")
    builder.add_conditional_edges(
        "query_reservation_availability",
        nodes.route_availability,
    )
    builder.add_edge("generate_availability_response", "audit_execution")
    builder.add_edge("prepare_reservation_action", "request_confirmation")
    builder.add_conditional_edges("request_confirmation", nodes.route_confirmation)
    builder.add_conditional_edges("execute_reservation", nodes.route_execution)
    builder.add_edge("verify_reservation", "generate_reservation_result")
    builder.add_edge("generate_reservation_result", "audit_execution")
    builder.add_edge("generate_rejected_response", "audit_execution")
    builder.add_conditional_edges("collect_incident_information", nodes.route_incident_fields)
    builder.add_edge("generate_incident_missing_response", "audit_execution")
    builder.add_edge("prepare_incident_action", "request_confirmation")
    builder.add_conditional_edges("execute_incident", nodes.route_incident_execution)
    builder.add_edge("verify_incident", "generate_incident_result")
    builder.add_edge("generate_incident_result", "audit_execution")
    builder.add_edge("generate_response", "audit_execution")
    builder.add_edge("audit_execution", END)
    return builder.compile(checkpointer=checkpointer, name="torresegura_agent")
