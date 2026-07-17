"""Tools de reservas con autorización, confirmación e idempotencia."""

from datetime import timedelta
from typing import Any, Dict

from django.core.exceptions import ObjectDoesNotExist, ValidationError
from django.db import transaction
from django.utils import timezone
from pydantic import ValidationError as PydanticValidationError

from agente.models import AgentAction
from areas_comunes.api import _slots_disponibles
from areas_comunes.models import AreaComun, Reserva

from .schemas import ReservationRequest


class ReservationTools:
    action_type = "RESERVA_CREAR"
    create_tool_name = "create_reservation"

    def list_areas(self, context: Dict[str, Any]) -> Dict[str, Any]:
        building_id = context.get("building_id")
        if not context.get("resident_active") or not building_id:
            return self._error(
                "unauthorized",
                "resident_context_required",
                "No existe un residente activo con vivienda asociada.",
            )
        areas = AreaComun.objects.filter(
            edificio_id=building_id,
            activo=True,
        ).values(
            "id",
            "nombre",
            "capacidad_maxima",
            "horario_inicio",
            "horario_fin",
        )
        return {
            "status": "success",
            "areas": [
                {
                    "id": area["id"],
                    "name": area["nombre"],
                    "capacity": area["capacidad_maxima"],
                    "opening_time": area["horario_inicio"].strftime("%H:%M"),
                    "closing_time": area["horario_fin"].strftime("%H:%M"),
                }
                for area in areas
            ],
        }

    def get_availability(
        self,
        context: Dict[str, Any],
        parameters: Dict[str, Any],
    ) -> Dict[str, Any]:
        try:
            request = ReservationRequest.model_validate(parameters)
        except PydanticValidationError as exc:
            return self._error(
                "validation_error",
                "invalid_reservation_parameters",
                self._validation_message(exc),
            )

        if request.date < timezone.localdate():
            return self._error(
                "validation_error",
                "past_date",
                "La fecha de reserva no puede estar en el pasado.",
            )

        area = self._authorized_area(context, request.area_id)
        if area is None:
            return self._error(
                "not_found",
                "area_not_found",
                "El área no existe o no pertenece al edificio del residente.",
            )
        if request.attendees > area.capacidad_maxima:
            return self._error(
                "validation_error",
                "capacity_exceeded",
                f"La capacidad máxima de {area.nombre} es {area.capacidad_maxima}.",
            )
        if (
            request.start_time < area.horario_inicio
            or request.end_time > area.horario_fin
        ):
            return self._error(
                "validation_error",
                "outside_opening_hours",
                (
                    f"El horario de {area.nombre} es de "
                    f"{area.horario_inicio:%H:%M} a {area.horario_fin:%H:%M}."
                ),
            )

        conflict = Reserva.objects.filter(
            area_comun=area,
            fecha=request.date,
            estado__in=["pendiente", "confirmada"],
            hora_inicio__lt=request.end_time,
            hora_fin__gt=request.start_time,
        ).exists()
        if conflict:
            return {
                "status": "conflict",
                "error_code": "slot_unavailable",
                "message": "El horario solicitado ya no está disponible.",
                "alternatives": self._alternatives(area, request),
            }

        return {
            "status": "success",
            "area": {
                "id": area.pk,
                "name": area.nombre,
                "capacity": area.capacidad_maxima,
            },
            "date": request.date.isoformat(),
            "start_time": request.start_time.strftime("%H:%M"),
            "end_time": request.end_time.strftime("%H:%M"),
            "attendees": request.attendees,
        }

    def create_reservation(self, action_id: int, user_id: int) -> Dict[str, Any]:
        """Ejecuta únicamente una AgentAction confirmada y bloqueada."""
        with transaction.atomic():
            try:
                action = AgentAction.objects.select_for_update().get(
                    pk=action_id,
                    usuario_id=user_id,
                )
            except AgentAction.DoesNotExist:
                return self._error(
                    "not_found", "action_not_found", "La acción no existe."
                )

            if action.tipo_accion != self.action_type:
                return self._error(
                    "validation_error",
                    "invalid_action_type",
                    "La acción no corresponde a una reserva.",
                )
            if action.estado == AgentAction.EJECUTADA:
                return action.resultado or self._error(
                    "unknown_result",
                    "missing_action_result",
                    "La acción figura ejecutada pero no tiene resultado verificable.",
                )
            if action.estado != AgentAction.CONFIRMADA:
                return self._error(
                    "unauthorized",
                    "confirmation_required",
                    "La reserva requiere confirmación explícita.",
                )
            if not action.idempotency_key:
                return self._fail_action(
                    action,
                    "validation_error",
                    "idempotency_key_required",
                    "La acción no tiene clave de idempotencia.",
                )

            try:
                request = ReservationRequest.model_validate(action.payload)
            except PydanticValidationError as exc:
                return self._fail_action(
                    action,
                    "validation_error",
                    "invalid_reservation_parameters",
                    self._validation_message(exc),
                )

            try:
                resident = action.usuario.residente
            except ObjectDoesNotExist:
                return self._fail_action(
                    action,
                    "unauthorized",
                    "resident_context_required",
                    "El usuario no tiene un residente asociado.",
                )
            if not resident.activo or not resident.vivienda_id:
                return self._fail_action(
                    action,
                    "unauthorized",
                    "resident_context_required",
                    "El residente no está activo o no tiene vivienda.",
                )

            existing = Reserva.objects.filter(
                idempotency_key=action.idempotency_key,
            ).first()
            if existing is not None:
                return self._complete_action(action, existing, replayed=True)

            try:
                area = AreaComun.objects.select_for_update().get(
                    pk=request.area_id,
                    activo=True,
                    edificio_id=resident.vivienda.edificio_id,
                )
            except AreaComun.DoesNotExist:
                return self._fail_action(
                    action,
                    "not_found",
                    "area_not_found",
                    "El área no existe o no pertenece al edificio del residente.",
                )

            try:
                reservation = Reserva.objects.create(
                    area_comun=area,
                    residente=resident,
                    fecha=request.date,
                    hora_inicio=request.start_time,
                    hora_fin=request.end_time,
                    cantidad_personas=request.attendees,
                    motivo=request.reason,
                    idempotency_key=action.idempotency_key,
                )
            except ValidationError as exc:
                error_code = (
                    "slot_unavailable"
                    if "reserva en ese horario" in str(exc)
                    else "reservation_validation_error"
                )
                status = "conflict" if error_code == "slot_unavailable" else "validation_error"
                return self._fail_action(
                    action,
                    status,
                    error_code,
                    self._django_validation_message(exc),
                )
            return self._complete_action(action, reservation, replayed=False)

    def verify_reservation(self, action_id: int, user_id: int) -> Dict[str, Any]:
        try:
            action = AgentAction.objects.get(pk=action_id, usuario_id=user_id)
        except AgentAction.DoesNotExist:
            return self._error("not_found", "action_not_found", "La acción no existe.")
        reservation = Reserva.objects.filter(
            pk=action.backend_reference,
            residente__usuario_id=user_id,
            idempotency_key=action.idempotency_key,
        ).first()
        if reservation is None:
            action.verification_status = AgentAction.VERIFICACION_FALLIDA
            action.error_code = "reservation_not_verified"
            action.save(update_fields=["verification_status", "error_code"])
            return self._error(
                "unknown_result",
                "reservation_not_verified",
                "No se pudo verificar la reserva creada.",
            )
        action.verification_status = AgentAction.VERIFICACION_VERIFICADA
        action.save(update_fields=["verification_status"])
        return {
            "status": "success",
            "reservation_id": reservation.pk,
            "reservation_status": reservation.estado,
        }

    @staticmethod
    def _authorized_area(context: Dict[str, Any], area_id: int):
        if not context.get("resident_active") or not context.get("building_id"):
            return None
        return AreaComun.objects.filter(
            pk=area_id,
            edificio_id=context["building_id"],
            activo=True,
        ).first()

    @staticmethod
    def _alternatives(area: AreaComun, request: ReservationRequest):
        duration = int(
            (
                timedelta(
                    hours=request.end_time.hour,
                    minutes=request.end_time.minute,
                )
                - timedelta(
                    hours=request.start_time.hour,
                    minutes=request.start_time.minute,
                )
            ).total_seconds()
            // 60
        )
        alternatives = []
        for day_offset in range(0, 8):
            candidate_date = request.date + timedelta(days=day_offset)
            slots = _slots_disponibles(area, candidate_date, duration)
            if slots:
                alternatives.append(
                    {"date": candidate_date.isoformat(), "slots": slots[:3]}
                )
            if len(alternatives) == 3:
                break
        return alternatives

    @staticmethod
    def _complete_action(action: AgentAction, reservation: Reserva, replayed: bool):
        result = {
            "status": "success",
            "reservation_id": reservation.pk,
            "reservation_status": reservation.estado,
            "replayed": replayed,
        }
        action.estado_previo = action.estado
        action.estado = AgentAction.EJECUTADA
        action.resultado = result
        action.backend_reference = str(reservation.pk)
        action.executed_at = timezone.now()
        action.error_code = ""
        action.save(
            update_fields=[
                "estado_previo",
                "estado",
                "resultado",
                "backend_reference",
                "executed_at",
                "error_code",
            ]
        )
        return result

    @staticmethod
    def _fail_action(action, status, error_code, message):
        action.error_code = error_code
        action.resultado = {
            "status": status,
            "error_code": error_code,
            "message": message,
        }
        action.save(update_fields=["error_code", "resultado"])
        return action.resultado

    @staticmethod
    def _error(status, error_code, message):
        return {"status": status, "error_code": error_code, "message": message}

    @staticmethod
    def _validation_message(exc: PydanticValidationError) -> str:
        return "; ".join(error["msg"] for error in exc.errors())

    @staticmethod
    def _django_validation_message(exc: ValidationError) -> str:
        if hasattr(exc, "message_dict"):
            return "; ".join(
                str(message)
                for messages in exc.message_dict.values()
                for message in messages
            )
        return "; ".join(exc.messages)
