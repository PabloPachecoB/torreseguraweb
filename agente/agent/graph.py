"""Grafo conversacional base del agente TorreSegura."""

from datetime import timedelta
import hashlib
import json
from typing import Any, Dict, Iterable, List

from django.utils import timezone
from django.utils.dateparse import parse_date
from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, SystemMessage
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from agente.llm import QwenAdapter, get_llm_adapter
from agente.models import AgentAction
from agente.presentation import (
    availability_options,
    common_area_cards,
    friendly_date_label,
    incident_initial_evaluation,
    incident_review_status,
)
from agente.tools import (
    DoorTools,
    IncidentTools,
    InformationTools,
    ReservationTools,
    VisitorTools,
)

from .guardrails import guard_unverified_action_claim
from .nlu import QwenNLU
from .state import AgentState


GRAPH_VERSION = "0.8.0"


class AgentGraphNodes:
    # La memoria se conserva por dominio. Esto permite pausar un flujo, consultar
    # otra cosa y retomarlo sin entregar campos de un esquema a otro extractor.
    _MEMORY_FIELDS = {
        "reservation": {
            "area_id", "date", "start_time", "end_time", "attendees", "reason",
        },
        "incident": {
            "title", "description", "location", "category", "urgency",
            "preliminary_estimate",
        },
        "lock": {"door_id"},
        "visitor": {
            "name", "document", "date", "start_time", "end_time",
            "attendees", "reason",
        },
        "residence_info": {
            "topic", "area_id", "date", "duration_minutes", "record_id",
        },
        "finance_info": {
            "topic", "record_id",
        },
        "resident_info": {
            "topic", "record_id",
        },
    }

    def __init__(
        self,
        adapter: QwenAdapter = None,
        reservation_tools=None,
        incident_tools=None,
        door_tools=None,
        visitor_tools=None,
        information_tools=None,
    ):
        self.adapter = adapter or get_llm_adapter()
        self.nlu = QwenNLU(self.adapter)
        self.reservation_tools = reservation_tools or ReservationTools()
        self.incident_tools = incident_tools or IncidentTools()
        self.door_tools = door_tools or DoorTools()
        self.visitor_tools = visitor_tools or VisitorTools()
        self.information_tools = information_tools or InformationTools()

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
            "conversation_context": state.get("conversation_context", {}),
            "missing_fields": state.get("missing_fields", []),
            "confirmation_status": state.get("confirmation_status", "not_required"),
            "verification_status": state.get("verification_status", "not_started"),
            "llm_invoked": False,
            "guardrail_triggered": False,
            "presentation": None,
            "availability_follow_up": False,
        }

    def classify_intent(self, state: AgentState) -> Dict:
        previous_intent = state.get("intent")
        interaction = state.get("interaction") or {}
        interaction_type = interaction.get("type")
        if interaction_type in {"start_reservation", "select_reservation_slot"}:
            intent = "reservation"
            llm_invoked = False
        elif interaction_type == "check_area_availability":
            intent = "residence_info"
            llm_invoked = False
        else:
            result = self.nlu.classify(
                self._last_human_text(state.get("messages", [])),
                current_intent=previous_intent,
            )
            if result.get("status") != "success":
                return self._nlu_error(result)
            intent = result["data"].intent
            llm_invoked = True
        update = {
            "intent": intent,
            "llm_invoked": llm_invoked,
        }
        if previous_intent and previous_intent != intent:
            conversation_context = self._transition_context(
                state,
                previous_intent=previous_intent,
                next_intent=intent,
            )
            update.update(
                {
                    "collected_fields": self._restored_fields(
                        conversation_context,
                        intent,
                    ),
                    "conversation_context": conversation_context,
                    "missing_fields": [],
                    "proposed_action": None,
                    "pending_action_id": None,
                    "confirmation_status": "not_required",
                    "tool_result": None,
                    "error": None,
                }
            )
        return update

    @classmethod
    def _transition_context(
        cls,
        state: AgentState,
        previous_intent: str,
        next_intent: str,
    ) -> Dict[str, Dict[str, Any]]:
        """Guarda el flujo anterior y transfiere solo referencias compatibles."""
        memory = {
            key: dict(value)
            for key, value in (state.get("conversation_context") or {}).items()
            if isinstance(value, dict) and key in cls._MEMORY_FIELDS
        }
        previous_fields = cls._filter_memory_fields(
            previous_intent,
            state.get("collected_fields", {}),
        )
        if previous_fields:
            memory[previous_intent] = previous_fields

        # Una consulta de disponibilidad y una reserva sí representan el mismo
        # espacio/fecha. Ningún otro par comparte campos implícitamente.
        if {previous_intent, next_intent} == {"residence_info", "reservation"}:
            target = dict(memory.get(next_intent, {}))
            for field in ("area_id", "date"):
                if previous_fields.get(field) is not None:
                    target[field] = previous_fields[field]
            if target:
                memory[next_intent] = cls._filter_memory_fields(next_intent, target)
        return memory

    @classmethod
    def _restored_fields(
        cls,
        conversation_context: Dict[str, Dict[str, Any]],
        intent: str,
    ) -> Dict[str, Any]:
        return cls._filter_memory_fields(
            intent,
            conversation_context.get(intent, {}),
        )

    @classmethod
    def _filter_memory_fields(cls, intent: str, fields: Dict) -> Dict[str, Any]:
        allowed = cls._MEMORY_FIELDS.get(intent, set())
        return {
            key: value
            for key, value in dict(fields or {}).items()
            if key in allowed and value is not None
        }

    @staticmethod
    def route_intent(state: AgentState) -> str:
        if state.get("error"):
            return "generate_response"
        if state.get("intent") == "reservation":
            return "collect_reservation_information"
        if state.get("intent") == "incident":
            return "collect_incident_information"
        if state.get("intent") == "lock":
            return "collect_door_information"
        if state.get("intent") == "visitor":
            return "collect_visitor_information"
        if state.get("intent") in {
            "residence_info",
            "finance_info",
            "resident_info",
        }:
            return "collect_information_query"
        return "generate_response"

    def collect_information_query(self, state: AgentState) -> Dict:
        context = state.get("authenticated_context", {})
        areas_result = self.information_tools.list_common_areas(context)
        areas = areas_result.get("areas", [])
        existing = dict(state.get("collected_fields", {}))
        interaction = state.get("interaction") or {}
        if interaction.get("type") == "check_area_availability":
            extracted = {
                "topic": "area_availability",
                "area_id": interaction.get("payload", {}).get("area_id"),
            }
            llm_invoked = False
        else:
            extraction = self.nlu.extract_information(
                message=self._last_human_text(state.get("messages", [])),
                existing_fields=existing,
                authorized_areas=areas,
                current_date=timezone.localdate(),
                intent=state.get("intent", "general"),
            )
            if extraction.get("status") != "success":
                return self._nlu_error(extraction)
            extracted = extraction["data"].as_state_fields()
            llm_invoked = True
        if existing.get("topic") and existing["topic"] != extracted.get("topic"):
            fields = {}
        else:
            fields = existing
        fields.update(extracted)

        allowed_area_ids = {item["id"] for item in areas}
        if fields.get("area_id") not in allowed_area_ids:
            fields.pop("area_id", None)

        missing = []
        if fields.get("topic") == "area_availability":
            missing = [name for name in ("area_id", "date") if not fields.get(name)]
        return {
            "collected_fields": fields,
            "missing_fields": missing,
            "tool_result": {
                "status": "success",
                "areas": areas,
                "building": areas_result.get("building", ""),
            },
            "error": None,
            "llm_invoked": llm_invoked,
        }

    @staticmethod
    def route_information_query(state: AgentState) -> str:
        if state.get("error"):
            return "generate_response"
        if state.get("missing_fields"):
            return "generate_information_missing_response"
        return "execute_information_query"

    @staticmethod
    def generate_information_missing_response(state: AgentState) -> Dict:
        missing = state.get("missing_fields", [])
        areas = state.get("tool_result", {}).get("areas", [])
        if missing == ["area_id", "date"]:
            names = ", ".join(item["name"] for item in areas)
            content = "¿Qué área y para qué fecha deseas consultar?"
            if names:
                content += f" Las áreas de tu edificio son: {names}."
        elif "area_id" in missing:
            names = ", ".join(item["name"] for item in areas)
            content = "¿Qué área deseas consultar?"
            if names:
                content += f" Puedes elegir: {names}."
        else:
            content = "¿Para qué fecha deseas consultar la disponibilidad?"
        return {"messages": [AIMessage(content=content)]}

    def execute_information_query(self, state: AgentState) -> Dict:
        context = state.get("authenticated_context", {})
        fields = state.get("collected_fields", {})
        topic = fields.get("topic")
        if topic == "common_areas":
            result = self.information_tools.list_common_areas(context)
        elif topic == "area_availability":
            result = self.information_tools.get_area_availability(
                context,
                fields["area_id"],
                parse_date(fields["date"]),
                fields.get("duration_minutes", 60),
            )
        elif topic == "my_reservations":
            result = self.information_tools.list_my_reservations(context)
        elif topic == "pending_fees":
            result = self.information_tools.get_pending_fees(context)
        elif topic == "paid_fees":
            result = self.information_tools.get_paid_fees(context)
        elif topic == "payment_history":
            result = self.information_tools.get_payment_history(context)
        elif topic == "my_payments":
            result = self.information_tools.get_payment_history(
                context, only_resident=True
            )
        elif topic == "housing_info":
            result = self.information_tools.get_housing(context)
        elif topic == "profile_info":
            result = self.information_tools.get_profile(context)
        elif topic == "resident_overview":
            result = self.information_tools.get_resident_overview(context)
        elif topic == "pending_payment_qrs":
            result = self.information_tools.get_pending_payment_qrs(context)
        elif topic == "account_statements":
            result = self.information_tools.get_account_statements(context)
        elif topic == "scheduled_visits":
            result = self.information_tools.get_visits(context)
        elif topic == "visit_history":
            result = self.information_tools.get_visits(context, scheduled_only=False)
        elif topic == "allowed_doors":
            result = self.information_tools.get_allowed_doors(context)
        elif topic == "access_history":
            result = self.information_tools.get_access_history(context)
        elif topic == "my_incidents":
            result = self.information_tools.get_my_incidents(context)
        elif topic == "incident_detail":
            result = self.information_tools.get_my_incidents(
                context, fields.get("record_id")
            )
        elif topic == "announcements":
            result = self.information_tools.get_announcements(context)
        elif topic == "building_alerts":
            result = self.information_tools.get_building_alerts(context)
        elif topic == "active_polls":
            result = self.information_tools.get_active_polls(context)
        else:
            result = {
                "status": "error",
                "error_code": "unsupported_information_topic",
                "message": "No puedo consultar ese tipo de información todavía.",
            }
        return {"tool_result": result}

    @staticmethod
    def render_information_response(state: AgentState) -> Dict:
        result = state.get("tool_result", {})
        if result.get("status") != "success":
            return {
                "error": {
                    "code": result.get("error_code", "information_query_failed"),
                    "message": result.get("message", "No se pudo consultar la información."),
                },
                "messages": [
                    AIMessage(
                        content=result.get(
                            "message", "No se pudo consultar la información."
                        )
                    )
                ],
            }

        topic = result["topic"]
        presentation = None
        if topic == "common_areas":
            areas = result["areas"]
            if not areas:
                content = "No hay áreas comunes activas registradas para tu edificio."
            else:
                rendered = [
                    (
                        f"- {area['name']}: capacidad para {area['capacity']} personas, "
                        f"horario {area['opening_time']}–{area['closing_time']}"
                        + (f". {area['description']}" if area["description"] else ".")
                    )
                    for area in areas
                ]
                content = (
                    f"Estos son los espacios comunes disponibles en "
                    f"{result['building']}:\n"
                    + "\n".join(rendered)
                    + "\nPuedes preguntarme por la disponibilidad de cualquiera de ellos."
                )
                presentation = common_area_cards(result["building"], areas)
        elif topic == "area_availability":
            slots = result["slots"]
            if slots:
                rendered = ", ".join(
                    f"{slot['hora_inicio']}–{slot['hora_fin']}" for slot in slots
                )
                content = (
                    f"{result['area']['name']} tiene estos horarios disponibles para "
                    f"{friendly_date_label(parse_date(result['date'])).lower()}: "
                    f"{rendered}."
                )
                presentation = availability_options(
                    area=result["area"],
                    alternatives=[{"date": result["date"], "slots": slots}],
                    requested={
                        "date": result["date"],
                        "duration_minutes": result["duration_minutes"],
                    },
                )
            else:
                content = (
                    f"{result['area']['name']} no tiene horarios disponibles el "
                    f"{result['date']} para esa duración."
                )
        elif topic == "my_reservations":
            items = result["reservations"]
            content = AgentGraphNodes._render_rows(
                "No tienes reservas próximas.",
                "Tus próximas reservas son:",
                items,
                lambda item: (
                    f"- {item['area']}, {item['date']} de {item['start_time']} "
                    f"a {item['end_time']} ({item['status']})"
                ),
            )
        elif topic == "pending_fees":
            items = result["fees"]
            content = AgentGraphNodes._render_rows(
                "No tienes cuotas pendientes.",
                f"Tu deuda pendiente total es Bs {result['total']}:",
                items,
                lambda item: (
                    f"- {item['concept']}: Bs {item['total']}, vence {item['due_date']}"
                    + (" (vencida)" if item["overdue"] else "")
                ),
            )
        elif topic == "paid_fees":
            content = AgentGraphNodes._render_rows(
                "No hay cuotas pagadas registradas.",
                "Las cuotas pagadas más recientes son:",
                result["fees"],
                lambda item: (
                    f"- {item['concept']}: Bs {item['amount']}, vencimiento {item['due_date']}"
                ),
            )
        elif topic in {"payment_history", "my_payments"}:
            title = (
                "Tus pagos registrados más recientes son:"
                if topic == "my_payments"
                else "Los pagos más recientes de tu vivienda son:"
            )
            content = AgentGraphNodes._render_rows(
                "No hay pagos registrados.",
                title,
                result["payments"],
                lambda item: (
                    f"- Bs {item['amount']} el {item['date']}, {item['method']} "
                    f"({item['status']})"
                ),
            )
        elif topic == "resident_overview":
            profile = result["profile"]
            housing = result["housing"]
            counts = result["counts"]
            content = (
                f"{profile['name']}, estás registrado como {profile['resident_type'].lower()} "
                f"de la vivienda {housing['number']} en {housing['building']}. "
                f"Tienes {counts['pending_fees']} cuotas pendientes, "
                f"{counts['scheduled_visits']} visitas agendadas, "
                f"{counts['upcoming_reservations']} reservas próximas, "
                f"{counts['open_incidents']} incidencias abiertas, "
                f"{counts['active_announcements']} anuncios activos y "
                f"{counts['active_polls']} votaciones publicadas."
            )
        elif topic == "pending_payment_qrs":
            content = AgentGraphNodes._render_rows(
                "No tienes QR de pago pendientes.",
                "Tus QR de pago pendientes son:",
                result["qrs"],
                lambda item: (
                    f"- Bs {item['amount']}, vence {item['expires']}: "
                    f"{item['description']} ({item['status']})"
                ),
            )
        elif topic == "account_statements":
            content = AgentGraphNodes._render_rows(
                "No tienes estados de cuenta registrados.",
                "Tus estados de cuenta más recientes son:",
                result["statements"],
                lambda item: (
                    f"- {item['period_start']} a {item['period_end']}: "
                    f"saldo Bs {item['balance']}, cuotas Bs {item['fees']}, "
                    f"pagos Bs {item['payments']}"
                ),
            )
        elif topic in {"scheduled_visits", "visit_history"}:
            title = (
                "Tus visitas agendadas son:"
                if topic == "scheduled_visits"
                else "El historial reciente de visitas de tu vivienda es:"
            )
            content = AgentGraphNodes._render_rows(
                "No hay visitas registradas para esa consulta.",
                title,
                result["visits"],
                lambda item: (
                    f"- {item['name']} (documento {item['document']}), "
                    + (
                        f"{item['date']} de {item['start_time']} a {item['end_time']}, "
                        if item["date"] else ""
                    )
                    + f"{item['people']} personas ({item['status']})"
                ),
            )
        elif topic == "allowed_doors":
            content = AgentGraphNodes._render_rows(
                "No tienes puertas habilitadas.",
                "Puedes usar estas puertas:",
                result["doors"],
                lambda item: f"- {item['name']} ({item['type_display']})",
            )
        elif topic == "access_history":
            rows = [
                f"- Apertura de {item['door']} el {item['date']} "
                f"({'exitosa' if item['success'] else 'fallida'})"
                for item in result["openings"][:10]
            ]
            rows += [
                f"- Movimiento: entrada {item['entry'] or 'sin registro'}, "
                f"salida {item['exit'] or 'sin registro'}"
                for item in result["movements"][:10]
            ]
            content = (
                "Tu historial de accesos reciente es:\n" + "\n".join(rows)
                if rows else "No tienes accesos registrados."
            )
        elif topic in {"my_incidents", "incident_detail"}:
            content = AgentGraphNodes._render_rows(
                "No tienes incidencias registradas.",
                "Tus incidencias son:",
                result["incidents"],
                lambda item: (
                    f"- #{item['id']} {item['title']}: {item['status']}, "
                    f"urgencia {item['urgency']}"
                    + (f". {item.get('description', '')}" if item.get("description") else "")
                ),
            )
        elif topic == "announcements":
            content = AgentGraphNodes._render_rows(
                "No hay anuncios activos para tu edificio.",
                "Los anuncios activos son:",
                result["announcements"],
                lambda item: f"- {item['title']} ({item['category']}): {item['content']}",
            )
        elif topic == "building_alerts":
            content = AgentGraphNodes._render_rows(
                "No hay alertas públicas activas para tu edificio.",
                "Las alertas públicas recientes son:",
                result["alerts"],
                lambda item: f"- {item['type']}: {item['description']} ({item['status']})",
            )
        elif topic == "active_polls":
            content = AgentGraphNodes._render_rows(
                "No hay votaciones publicadas para tu edificio.",
                "Las votaciones son:",
                result["polls"],
                lambda item: (
                    f"- {item['title']} ({'abierta' if item['open'] else 'cerrada'}); "
                    f"tu voto: {item['my_vote'] or 'no registrado'}; opciones: "
                    + ", ".join(
                        f"{option['text']} ({option['votes']})" for option in item["options"]
                    )
                ),
            )
        elif topic == "housing_info":
            item = result["housing"]
            residence = f", {item['condominium']}" if item["condominium"] else ""
            content = (
                f"Tu vivienda es la {item['number']}, piso {item['floor']}, "
                f"en {item['building']}{residence}. Tiene {item['square_meters']} m², "
                f"{item['rooms']} habitaciones y {item['bathrooms']} baños. "
                f"Dirección: {item['address']}."
            )
        elif topic == "profile_info":
            item = result["profile"]
            email = item["email"] or "no registrado"
            phone = item["phone"] or "no registrado"
            content = (
                f"Tu perfil: {item['name']} ({item['resident_type']}). "
                f"Usuario: {item['username']}; correo: {email}; teléfono: {phone}; "
                f"vehículos registrados: {item['vehicles']}."
            )
        else:
            content = "No puedo presentar esa información todavía."
        return {
            "messages": [AIMessage(content=content)],
            "presentation": presentation,
        }

    @staticmethod
    def _render_rows(empty: str, title: str, rows: List[Dict], renderer) -> str:
        if not rows:
            return empty
        visible = rows[:10]
        content = title + "\n" + "\n".join(renderer(item) for item in visible)
        if len(rows) > len(visible):
            content += f"\nY {len(rows) - len(visible)} registros más."
        return content

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
        existing_fields = self._reservation_fields(
            state.get("collected_fields", {})
        )
        interaction = state.get("interaction") or {}
        if interaction.get("type") in {
            "start_reservation",
            "select_reservation_slot",
        }:
            extracted_fields = dict(interaction.get("payload", {}))
            llm_invoked = False
        else:
            extraction = self.nlu.extract_reservation(
                message=self._last_human_text(state.get("messages", [])),
                existing_fields=existing_fields,
                authorized_areas=areas,
                current_date=timezone.localdate(),
            )
            if extraction.get("status") != "success":
                return self._nlu_error(extraction)
            extracted_fields = extraction["data"].as_state_fields()
            llm_invoked = True

        fields = existing_fields
        allowed_area_ids = {item["id"] for item in areas}
        if (
            "area_id" in extracted_fields
            and extracted_fields["area_id"] not in allowed_area_ids
        ):
            extracted_fields.pop("area_id")
        fields.update(extracted_fields)

        required = ["area_id", "date", "start_time", "end_time", "attendees"]
        missing = [field for field in required if not fields.get(field)]
        previous_result = state.get("tool_result") or {}
        follow_up = bool(
            previous_result.get("alternatives")
            and self._is_availability_follow_up(
                self._last_human_text(state.get("messages", []))
            )
            and not interaction
        )
        return {
            "collected_fields": fields,
            "missing_fields": missing,
            "tool_result": (
                previous_result
                if follow_up
                else {"status": "success", "areas": areas}
            ),
            "availability_follow_up": follow_up,
            "error": None,
            "llm_invoked": llm_invoked,
        }

    @staticmethod
    def route_reservation_fields(state: AgentState) -> str:
        if state.get("error"):
            return "generate_response"
        if state.get("missing_fields"):
            return "generate_missing_fields_response"
        if state.get("availability_follow_up"):
            return "generate_availability_response"
        return "query_reservation_availability"

    def generate_missing_fields_response(self, state: AgentState) -> Dict:
        fields = state.get("collected_fields", {})
        missing = state["missing_fields"]
        areas = state.get("tool_result", {}).get("areas", [])
        if "area_id" in missing:
            names = ", ".join(item["name"] for item in areas)
            content = "Claro. ¿Qué espacio deseas reservar?"
            if names:
                content += f" Puedes elegir entre: {names}."
            return {"messages": [AIMessage(content=content)]}

        selected_area = next(
            (item for item in areas if item["id"] == fields.get("area_id")),
            None,
        )
        area_name = selected_area["name"] if selected_area else "el área seleccionada"
        questions = []
        if "date" in missing:
            questions.append("para qué fecha la necesitas")
        if "start_time" in missing and "end_time" in missing:
            questions.append("en qué horario")
        elif "start_time" in missing:
            questions.append("a qué hora comienza")
        elif "end_time" in missing:
            questions.append("a qué hora termina")
        if "attendees" in missing:
            questions.append("para cuántas personas será")

        content = f"Perfecto, vamos a preparar la reserva de {area_name}."
        if questions:
            if len(questions) == 1:
                rendered_question = questions[0]
            else:
                rendered_question = ", ".join(questions[:-1]) + f" y {questions[-1]}"
            content += f" ¿{rendered_question[0].upper()}{rendered_question[1:]}?"
        if len(missing) >= 3:
            content += (
                " Puedes responder, por ejemplo: mañana de 10:00 a 14:00 "
                "para 20 personas."
            )
        return {"messages": [AIMessage(content=content)]}

    def query_reservation_availability(self, state: AgentState) -> Dict:
        parameters = self._reservation_fields(state["collected_fields"])
        parameters.setdefault("reason", "")
        result = self.reservation_tools.get_availability(
            state["authenticated_context"],
            parameters,
        )
        return {"tool_result": result}

    @staticmethod
    def _reservation_fields(fields: Dict) -> Dict:
        allowed = {
            "area_id",
            "date",
            "start_time",
            "end_time",
            "attendees",
            "reason",
        }
        return {key: value for key, value in fields.items() if key in allowed}

    @staticmethod
    def _is_availability_follow_up(message: str) -> bool:
        normalized = message.lower().strip()
        markers = (
            "no hay",
            "hay para",
            "algún horario",
            "algun horario",
            "otra hora",
            "otro horario",
            "qué horarios",
            "que horarios",
        )
        return any(marker in normalized for marker in markers)

    @staticmethod
    def route_availability(state: AgentState) -> str:
        if state.get("tool_result", {}).get("status") == "success":
            return "prepare_reservation_action"
        return "generate_availability_response"

    def generate_availability_response(self, state: AgentState) -> Dict:
        result = state.get("tool_result", {})
        alternatives = result.get("alternatives", [])
        presentation = None
        if alternatives:
            requested = result.get("requested") or state.get("collected_fields", {})
            requested_date = requested.get("date")
            requested_label = (
                friendly_date_label(parse_date(requested_date)).lower()
                if requested_date else "esa fecha"
            )
            same_day = [
                option for option in alternatives if option["date"] == requested_date
            ]
            if state.get("availability_follow_up"):
                if same_day:
                    content = f"Sí. Para {requested_label} encontré estas opciones:"
                else:
                    content = (
                        f"Para {requested_label} no encontré otro bloque disponible. "
                        "Estas son las primeras alternativas que tengo:"
                    )
            else:
                content = (
                    "Ese horario ya está ocupado, pero encontré estas alternativas:"
                )
            rendered = []
            for option in alternatives:
                slots = ", ".join(
                    f'{slot["hora_inicio"]}–{slot["hora_fin"]}'
                    for slot in option["slots"]
                )
                label = friendly_date_label(parse_date(option["date"]))
                rendered.append(f'- {label}: {slots}')
            content += "\n" + "\n".join(rendered)
            presentation = availability_options(
                area=result["area"],
                alternatives=alternatives,
                requested=requested,
                title="Elige el horario que prefieras",
            )
        else:
            content = result.get(
                "message", "No pude consultar la disponibilidad en este momento."
            )
        return {
            "messages": [AIMessage(content=content)],
            "presentation": presentation,
            "availability_follow_up": False,
        }

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
        action_type = state.get("proposed_action", {}).get("action_type")
        is_lock = action_type == DoorTools.action_type
        decision = interrupt(
            {
                "type": "action_confirmation",
                "action_id": action_id,
                "summary": state["proposed_action"]["summary"],
                "expires_in_seconds": 300 if is_lock else 600,
                "requires_password": is_lock,
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
            action_type = state.get("proposed_action", {}).get("action_type")
            if action_type == IncidentTools.action_type:
                return "execute_incident"
            if action_type == DoorTools.action_type:
                return "execute_door"
            if action_type == VisitorTools.action_type:
                return "execute_visitor"
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
            "conversation_context": self._without_intent_memory(
                state,
                "reservation",
            ),
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
        latest_message = self._last_human_text(state.get("messages", [])).strip()
        location_source = " ".join(
            value
            for value in (
                latest_message,
                fields.get("title"),
                fields.get("description"),
            )
            if isinstance(value, str)
        )
        inferred_location = self._infer_incident_location(location_source)
        if not fields.get("location") and inferred_location:
            fields["location"] = inferred_location
        if inferred_location and fields.get("category") == "OTRO":
            fields["category"] = "SEGURIDAD"
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
    def _infer_incident_location(message: str) -> str:
        """Reconoce ubicaciones inequívocas sin inventar datos del residente."""
        normalized = " ".join(message.lower().split())
        if "mi puerta" in normalized:
            return "puerta de mi vivienda"
        dwelling_markers = (
            "puerta de mi vivienda",
            "puerta de mi departamento",
            "puerta de mi depto",
        )
        return next(
            (marker for marker in dwelling_markers if marker in normalized),
            "",
        )

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
            "presentation": incident_initial_evaluation(estimate),
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
        presentation = None
        if result.get("status") == "success" and state.get("verification_status") == "verified":
            from incidencias.models import Incidencia

            incident = Incidencia.objects.select_related(
                "empleado_asignado__usuario"
            ).get(pk=result["incident_id"])
            content = (
                f"Listo, creé el reporte #{incident.pk}. La evaluación inicial "
                "quedó pendiente de revisión administrativa."
            )
            if incident.empleado_asignado_id:
                technician_name = (
                    incident.empleado_asignado.nombre_completo
                    or incident.empleado_asignado.usuario.username
                )
                content += f" También notifiqué al técnico asignado, {technician_name}."
            else:
                content += " Te avisaré cuando se asigne un técnico."
            content += (
                " Puedes adjuntar evidencia desde este chat o desde el módulo "
                "de Incidencias."
            )
            presentation = incident_review_status(incident)
        else:
            content = result.get(
                "message",
                "No se pudo verificar la creación de la incidencia.",
            )
        return {
            "messages": [AIMessage(content=content)],
            "presentation": presentation,
            "intent": "general",
            "collected_fields": {},
            "conversation_context": AgentGraphNodes._without_intent_memory(
                state,
                "incident",
            ),
            "missing_fields": [],
            "proposed_action": None,
            "pending_action_id": None,
            "confirmation_status": "not_required",
        }

    def collect_door_information(self, state: AgentState) -> Dict:
        result = self.door_tools.list_doors(state.get("authenticated_context", {}))
        if result.get("status") != "success":
            return {
                "error": {
                    "code": result.get("error_code", "unauthorized"),
                    "message": result.get("message", "No puedes abrir puertas."),
                }
            }
        doors = result["doors"]
        extraction = self.nlu.extract_door(
            self._last_human_text(state.get("messages", [])),
            dict(state.get("collected_fields", {})),
            doors,
        )
        if extraction.get("status") != "success":
            return self._nlu_error(extraction)
        fields = dict(state.get("collected_fields", {}))
        extracted = extraction["data"].as_state_fields()
        allowed_ids = {door["id"] for door in doors}
        if extracted.get("door_id") not in allowed_ids:
            extracted.pop("door_id", None)
        fields.update(extracted)
        return {
            "collected_fields": fields,
            "missing_fields": [] if fields.get("door_id") else ["door_id"],
            "tool_result": {"status": "success", "doors": doors},
            "error": None,
            "llm_invoked": True,
        }

    @staticmethod
    def route_door_fields(state: AgentState) -> str:
        if state.get("error"):
            return "generate_response"
        if state.get("missing_fields"):
            return "generate_door_missing_response"
        return "prepare_door_action"

    @staticmethod
    def generate_door_missing_response(state: AgentState) -> Dict:
        doors = state.get("tool_result", {}).get("doors", [])
        names = ", ".join(f'{door["name"]} (puerta {door["id"]})' for door in doors)
        content = "Indica qué puerta deseas abrir."
        if names:
            content += f" Puertas autorizadas: {names}."
        return {"messages": [AIMessage(content=content)]}

    def prepare_door_action(self, state: AgentState) -> Dict:
        door_id = state["collected_fields"]["door_id"]
        doors = state.get("tool_result", {}).get("doors", [])
        door = next(item for item in doors if item["id"] == door_id)
        parameters = {"door_id": door_id}
        summary = f'Abrir {door["name"]} en modo controlado.'
        return self._create_pending_action(
            state,
            action_type=DoorTools.action_type,
            tool_name=DoorTools.create_tool_name,
            parameters=parameters,
            summary=summary,
            expires_minutes=5,
        )

    def execute_door(self, state: AgentState) -> Dict:
        return {
            "tool_result": self.door_tools.open(
                state["pending_action_id"], state["user_id"]
            )
        }

    @staticmethod
    def route_door_execution(state: AgentState) -> str:
        if state.get("tool_result", {}).get("opening_id"):
            return "verify_door"
        return "generate_door_result"

    def verify_door(self, state: AgentState) -> Dict:
        result = self.door_tools.verify(
            state["pending_action_id"], state["user_id"]
        )
        return {
            "verification_status": "verified" if result.get("opening_id") else "unknown",
            "tool_result": result,
        }

    @staticmethod
    def generate_door_result(state: AgentState) -> Dict:
        result = state.get("tool_result", {})
        if result.get("success") and state.get("verification_status") == "verified":
            content = (
                f"Apertura verificada. Registro {result['opening_id']}; "
                f"estado del hardware: {result['hardware_status']}."
            )
        else:
            content = result.get("message", "No se pudo verificar la apertura.")
        response = AgentGraphNodes._finished_action_response(content, state, "lock")
        if not result.get("success"):
            response["error"] = {
                "code": result.get("error_code") or "opening_failed",
                "message": content,
            }
        return response

    def collect_visitor_information(self, state: AgentState) -> Dict:
        context = state.get("authenticated_context", {})
        if not context.get("resident_active") or not context.get("apartment_id"):
            return {
                "error": {
                    "code": "resident_context_required",
                    "message": "Necesitas un residente activo con vivienda para autorizar visitas.",
                }
            }
        extraction = self.nlu.extract_visitor(
            self._last_human_text(state.get("messages", [])),
            dict(state.get("collected_fields", {})),
            timezone.localdate(),
        )
        if extraction.get("status") != "success":
            return self._nlu_error(extraction)
        fields = dict(state.get("collected_fields", {}))
        fields.update(extraction["data"].as_state_fields())
        required = [
            "name", "document", "date", "start_time", "end_time", "attendees"
        ]
        return {
            "collected_fields": fields,
            "missing_fields": [name for name in required if not fields.get(name)],
            "tool_result": {"status": "success"},
            "error": None,
            "llm_invoked": True,
        }

    @staticmethod
    def route_visitor_fields(state: AgentState) -> str:
        if state.get("error"):
            return "generate_response"
        if state.get("missing_fields"):
            return "generate_visitor_missing_response"
        return "prepare_visitor_action"

    @staticmethod
    def generate_visitor_missing_response(state: AgentState) -> Dict:
        labels = {
            "name": "nombre del visitante",
            "document": "documento",
            "date": "fecha",
            "start_time": "hora de inicio",
            "end_time": "hora de fin",
            "attendees": "cantidad de personas",
        }
        missing = ", ".join(labels[item] for item in state["missing_fields"])
        return {
            "messages": [AIMessage(content=f"Para autorizar la visita falta: {missing}.")]
        }

    def prepare_visitor_action(self, state: AgentState) -> Dict:
        fields = dict(state["collected_fields"])
        parameters = {
            key: fields[key]
            for key in (
                "name", "document", "date", "start_time", "end_time", "attendees"
            )
        }
        parameters["reason"] = fields.get("reason", "")
        summary = (
            f"Autorizar a {parameters['name']} (documento {parameters['document']}) "
            f"el {parameters['date']} de {parameters['start_time']} a "
            f"{parameters['end_time']} para {parameters['attendees']} personas."
        )
        return self._create_pending_action(
            state,
            action_type=VisitorTools.action_type,
            tool_name=VisitorTools.create_tool_name,
            parameters=parameters,
            summary=summary,
        )

    def execute_visitor(self, state: AgentState) -> Dict:
        return {
            "tool_result": self.visitor_tools.create(
                state["pending_action_id"], state["user_id"]
            )
        }

    @staticmethod
    def route_visitor_execution(state: AgentState) -> str:
        if state.get("tool_result", {}).get("visit_id"):
            return "verify_visitor"
        return "generate_visitor_result"

    def verify_visitor(self, state: AgentState) -> Dict:
        result = self.visitor_tools.verify(
            state["pending_action_id"], state["user_id"]
        )
        return {
            "verification_status": "verified" if result.get("visit_id") else "unknown",
            "tool_result": result,
        }

    @staticmethod
    def generate_visitor_result(state: AgentState) -> Dict:
        result = state.get("tool_result", {})
        if result.get("status") == "success" and state.get("verification_status") == "verified":
            content = (
                f"Visita autorizada y verificada. ID {result['visit_id']}; "
                f"estado real: {result['visit_status']}; QR creado."
            )
        else:
            content = result.get("message", "No se pudo verificar la autorización.")
        return AgentGraphNodes._finished_action_response(content, state, "visitor")

    def _create_pending_action(
        self,
        state: AgentState,
        action_type: str,
        tool_name: str,
        parameters: Dict,
        summary: str,
        expires_minutes: int = 10,
    ) -> Dict:
        canonical = json.dumps(parameters, sort_keys=True, separators=(",", ":"))
        idempotency_key = hashlib.sha256(
            f"{state['user_id']}:{state['thread_id']}:{action_type}:{canonical}".encode()
        ).hexdigest()
        action, _ = AgentAction.objects.get_or_create(
            usuario_id=state["user_id"],
            idempotency_key=idempotency_key,
            defaults={
                "thread_id": state["thread_id"],
                "tipo_accion": action_type,
                "payload": parameters,
                "requires_confirmation": True,
                "tool_name": tool_name,
                "expira_en": timezone.now() + timedelta(minutes=expires_minutes),
            },
        )
        return {
            "proposed_action": {
                "action_type": action_type,
                "parameters": parameters,
                "summary": summary,
            },
            "pending_action_id": action.pk,
            "confirmation_status": "pending",
            "messages": [
                AIMessage(
                    content=(
                        f"Confirma esta acción: {summary} "
                        f"La confirmación vence en {expires_minutes} minutos."
                    )
                )
            ],
        }

    @staticmethod
    def _finished_action_response(
        content: str,
        state: AgentState,
        intent: str,
    ) -> Dict:
        return {
            "messages": [AIMessage(content=content)],
            "intent": "general",
            "collected_fields": {},
            "conversation_context": AgentGraphNodes._without_intent_memory(
                state,
                intent,
            ),
            "missing_fields": [],
            "proposed_action": None,
            "pending_action_id": None,
            "confirmation_status": "not_required",
        }

    @staticmethod
    def generate_rejected_response(state: AgentState) -> Dict:
        nouns = {
            IncidentTools.action_type: "incidencia",
            ReservationTools.action_type: "reserva",
            DoorTools.action_type: "apertura",
            VisitorTools.action_type: "visita",
        }
        noun = nouns.get(
            state.get("proposed_action", {}).get("action_type"), "acción"
        )
        intents = {
            IncidentTools.action_type: "incident",
            ReservationTools.action_type: "reservation",
            DoorTools.action_type: "lock",
            VisitorTools.action_type: "visitor",
        }
        rejected_intent = intents.get(
            state.get("proposed_action", {}).get("action_type"),
            state.get("intent", "general"),
        )
        return {
            "messages": [AIMessage(content=f"Acción rechazada. No se creó la {noun}.")],
            "intent": "general",
            "collected_fields": {},
            "conversation_context": AgentGraphNodes._without_intent_memory(
                state,
                rejected_intent,
            ),
            "missing_fields": [],
            "proposed_action": None,
            "pending_action_id": None,
            "confirmation_status": "not_required",
        }

    @staticmethod
    def _without_intent_memory(
        state: AgentState,
        intent: str,
    ) -> Dict[str, Dict[str, Any]]:
        memory = {
            key: dict(value)
            for key, value in (state.get("conversation_context") or {}).items()
            if isinstance(value, dict)
        }
        memory.pop(intent, None)
        return memory

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
        tool_result = state.get("tool_result") or {}
        inferred = (
            ("reservation", ReservationTools.create_tool_name)
            if "reservation_id" in tool_result
            else ("incident", IncidentTools.create_tool_name)
            if "incident_id" in tool_result
            else ("lock", DoorTools.create_tool_name)
            if "opening_id" in tool_result
            else ("visitor", VisitorTools.create_tool_name)
            if "visit_id" in tool_result
            else (state.get("intent", "general"), InformationTools.tool_name)
            if tool_result.get("topic")
            else (state.get("intent", "general"), "")
        )
        result_status = tool_result.get("status")
        return {
            "trace_metadata": {
                "graph_version": GRAPH_VERSION,
                "intent": inferred[0],
                "outcome": (
                    "error"
                    if state.get("error") or result_status not in {None, "success"}
                    else "success"
                ),
                "model_provider": self.adapter.provider,
                "model_name": self.adapter.model,
                "llm_invoked": llm_invoked,
                "guardrail_triggered": bool(
                    state.get("guardrail_triggered", False)
                ),
                "tool_name": inferred[1] or (
                    ReservationTools.create_tool_name
                    if state.get("intent") == "reservation"
                    else IncidentTools.create_tool_name
                    if state.get("intent") == "incident"
                    else DoorTools.create_tool_name
                    if state.get("intent") == "lock"
                    else VisitorTools.create_tool_name
                    if state.get("intent") == "visitor"
                    else InformationTools.tool_name
                    if state.get("intent") in {
                        "residence_info",
                        "finance_info",
                        "resident_info",
                    }
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
    door_tools=None,
    visitor_tools=None,
    information_tools=None,
):
    nodes = AgentGraphNodes(
        adapter=adapter,
        reservation_tools=reservation_tools,
        incident_tools=incident_tools,
        door_tools=door_tools,
        visitor_tools=visitor_tools,
        information_tools=information_tools,
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
    builder.add_node("collect_door_information", nodes.collect_door_information)
    builder.add_node("generate_door_missing_response", nodes.generate_door_missing_response)
    builder.add_node("prepare_door_action", nodes.prepare_door_action)
    builder.add_node("execute_door", nodes.execute_door)
    builder.add_node("verify_door", nodes.verify_door)
    builder.add_node("generate_door_result", nodes.generate_door_result)
    builder.add_node("collect_visitor_information", nodes.collect_visitor_information)
    builder.add_node("generate_visitor_missing_response", nodes.generate_visitor_missing_response)
    builder.add_node("prepare_visitor_action", nodes.prepare_visitor_action)
    builder.add_node("execute_visitor", nodes.execute_visitor)
    builder.add_node("verify_visitor", nodes.verify_visitor)
    builder.add_node("generate_visitor_result", nodes.generate_visitor_result)
    builder.add_node("collect_information_query", nodes.collect_information_query)
    builder.add_node(
        "generate_information_missing_response",
        nodes.generate_information_missing_response,
    )
    builder.add_node("execute_information_query", nodes.execute_information_query)
    builder.add_node("render_information_response", nodes.render_information_response)
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
    builder.add_conditional_edges("collect_door_information", nodes.route_door_fields)
    builder.add_edge("generate_door_missing_response", "audit_execution")
    builder.add_edge("prepare_door_action", "request_confirmation")
    builder.add_conditional_edges("execute_door", nodes.route_door_execution)
    builder.add_edge("verify_door", "generate_door_result")
    builder.add_edge("generate_door_result", "audit_execution")
    builder.add_conditional_edges("collect_visitor_information", nodes.route_visitor_fields)
    builder.add_edge("generate_visitor_missing_response", "audit_execution")
    builder.add_edge("prepare_visitor_action", "request_confirmation")
    builder.add_conditional_edges("execute_visitor", nodes.route_visitor_execution)
    builder.add_edge("verify_visitor", "generate_visitor_result")
    builder.add_edge("generate_visitor_result", "audit_execution")
    builder.add_conditional_edges(
        "collect_information_query", nodes.route_information_query
    )
    builder.add_edge("generate_information_missing_response", "audit_execution")
    builder.add_edge("execute_information_query", "render_information_response")
    builder.add_edge("render_information_response", "audit_execution")
    builder.add_edge("generate_response", "audit_execution")
    builder.add_edge("audit_execution", END)
    return builder.compile(checkpointer=checkpointer, name="torresegura_agent")
