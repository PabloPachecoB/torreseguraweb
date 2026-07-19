"""Contratos de presentación estructurada para clientes del agente."""

from datetime import date, timedelta
from typing import Any, Dict, List, Literal, Optional

from django.utils import timezone
from django.utils.formats import date_format
from pydantic import BaseModel, ConfigDict


class PresentationModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PresentationAction(PresentationModel):
    type: Literal[
        "check_area_availability",
        "start_reservation",
        "select_reservation_slot",
    ]
    label: str
    payload: Dict[str, Any]


class CommonAreaCard(PresentationModel):
    id: int
    name: str
    description: str = ""
    capacity: int
    opening_time: str
    closing_time: str
    actions: List[PresentationAction]


class CommonAreaCardsPresentation(PresentationModel):
    type: Literal["common_area_cards"] = "common_area_cards"
    title: str
    areas: List[CommonAreaCard]


class AvailabilitySlot(PresentationModel):
    start_time: str
    end_time: str
    label: str
    action: PresentationAction


class AvailabilityDate(PresentationModel):
    date: date
    label: str
    slots: List[AvailabilitySlot]


class AvailabilityOptionsPresentation(PresentationModel):
    type: Literal["availability_options"] = "availability_options"
    title: str
    area: Dict[str, Any]
    requested: Optional[Dict[str, Any]] = None
    dates: List[AvailabilityDate]


class IncidentEvaluationPresentation(PresentationModel):
    type: Literal["incident_initial_evaluation"] = "incident_initial_evaluation"
    title: str = "Evaluación inicial"
    category: str
    priority: str
    estimated_hours: int
    estimated_cost_min: Optional[float] = None
    estimated_cost_max: Optional[float] = None
    currency: str = "BOB"
    note: str


class IncidentReviewPresentation(PresentationModel):
    type: Literal["incident_review_status"] = "incident_review_status"
    title: str = "Revisión del trabajo"
    incident_id: int
    status: str
    version: int
    approvals: List[Dict[str, Any]]
    technician: Optional[str] = None
    evaluation: Dict[str, Any]


class WorkOrderPresentation(PresentationModel):
    type: Literal["work_order"] = "work_order"
    title: str = "Orden de trabajo"
    code: str
    status: str
    category: str
    priority: str
    technician: Optional[str] = None
    scheduled_start: Optional[str] = None
    scheduled_end: Optional[str] = None


def common_area_cards(building: str, areas: List[Dict[str, Any]]) -> Dict[str, Any]:
    presentation = CommonAreaCardsPresentation(
        title=f"Espacios comunes de {building}" if building else "Espacios comunes",
        areas=[
            CommonAreaCard(
                **area,
                actions=[
                    PresentationAction(
                        type="check_area_availability",
                        label="Ver horarios",
                        payload={"area_id": area["id"]},
                    ),
                    PresentationAction(
                        type="start_reservation",
                        label="Reservar",
                        payload={"area_id": area["id"]},
                    ),
                ],
            )
            for area in areas
        ],
    )
    return presentation.model_dump(mode="json")


def availability_options(
    *,
    area: Dict[str, Any],
    alternatives: List[Dict[str, Any]],
    requested: Optional[Dict[str, Any]] = None,
    title: str = "Horarios disponibles",
) -> Dict[str, Any]:
    dates = []
    for option in alternatives:
        option_date = date.fromisoformat(option["date"])
        slots = []
        for slot in option["slots"]:
            start = slot["hora_inicio"]
            end = slot["hora_fin"]
            payload = {
                "area_id": area["id"],
                "date": option["date"],
                "start_time": start,
                "end_time": end,
            }
            slots.append(
                AvailabilitySlot(
                    start_time=start,
                    end_time=end,
                    label=f"{start}–{end}",
                    action=PresentationAction(
                        type="select_reservation_slot",
                        label=f"Elegir {start}–{end}",
                        payload=payload,
                    ),
                )
            )
        dates.append(
            AvailabilityDate(
                date=option_date,
                label=friendly_date_label(option_date),
                slots=slots,
            )
        )
    presentation = AvailabilityOptionsPresentation(
        title=title,
        area=area,
        requested=requested,
        dates=dates,
    )
    return presentation.model_dump(mode="json")


def incident_initial_evaluation(estimate: Dict[str, Any]) -> Dict[str, Any]:
    return IncidentEvaluationPresentation(
        category=estimate["category"],
        priority=estimate["urgency"],
        estimated_hours=estimate["estimated_hours"],
        estimated_cost_min=estimate.get("estimated_cost_min"),
        estimated_cost_max=estimate.get("estimated_cost_max"),
        currency=estimate.get("currency", "BOB"),
        note=estimate["disclaimer"],
    ).model_dump(mode="json")


def incident_review_status(incident) -> Dict[str, Any]:
    revision = incident.revisiones.filter(vigente=True).prefetch_related(
        "aprobaciones__usuario"
    ).first()
    approvals = []
    if revision:
        approvals = [
            {
                "role": approval.rol,
                "decision": approval.decision,
                "actor": approval.usuario.get_full_name() or approval.usuario.username,
                "date": approval.fecha.isoformat(),
            }
            for approval in revision.aprobaciones.all()
        ]
    evaluation = {
        "category": revision.categoria if revision else incident.categoria,
        "priority": revision.prioridad if revision else incident.urgencia,
        "estimated_hours": revision.tiempo_estimado_horas if revision else None,
        "estimated_cost_min": (
            float(revision.costo_estimado_min)
            if revision and revision.costo_estimado_min is not None else None
        ),
        "estimated_cost_max": (
            float(revision.costo_estimado_max)
            if revision and revision.costo_estimado_max is not None else None
        ),
        "currency": revision.moneda if revision else "BOB",
    }
    return IncidentReviewPresentation(
        incident_id=incident.pk,
        status=incident.estado,
        version=revision.version if revision else 0,
        approvals=approvals,
        technician=(
            incident.empleado_asignado.nombre_completo
            if incident.empleado_asignado_id else None
        ),
        evaluation=evaluation,
    ).model_dump(mode="json")


def work_order_card(order) -> Dict[str, Any]:
    revision = order.revision_aprobada
    return WorkOrderPresentation(
        code=order.codigo,
        status=order.estado,
        category=revision.categoria,
        priority=revision.prioridad,
        technician=order.tecnico.nombre_completo if order.tecnico_id else None,
        scheduled_start=(
            order.programada_inicio.isoformat() if order.programada_inicio else None
        ),
        scheduled_end=(
            order.programada_fin.isoformat() if order.programada_fin else None
        ),
    ).model_dump(mode="json")


def friendly_date_label(value: date) -> str:
    today = timezone.localdate()
    if value == today:
        return "Hoy"
    if value == today + timedelta(days=1):
        return "Mañana"
    return date_format(value, "l j \\d\\e F").capitalize()
