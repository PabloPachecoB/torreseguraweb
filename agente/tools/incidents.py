"""Tools de incidencias basadas exclusivamente en incidencias.Incidencia."""

from typing import Any, Dict

from django.core.exceptions import ObjectDoesNotExist
from django.db import transaction
from django.utils import timezone
from pydantic import ValidationError as PydanticValidationError

from agente.models import AgentAction
from incidencias.models import Incidencia

from .schemas import IncidentRequest, PreliminaryIncidentEstimate


class IncidentTools:
    action_type = "INCIDENCIA_CREAR"
    create_tool_name = "create_incident"
    disclaimer = (
        "Clasificación, urgencia y plazo son estimaciones preliminares. "
        "El personal o administrador valida costo, responsable, proveedor y prioridad final."
    )

    def build_preliminary_estimate(
        self,
        category: str,
        urgency: str,
    ) -> Dict[str, Any]:
        """Aplica mensajes de política a la sugerencia estructurada de Qwen."""
        response_windows = {
            "CRITICA": "Atención inmediata y activación del protocolo de emergencia.",
            "ALTA": "Revisión prioritaria por personal de mantenimiento.",
            "MEDIA": "Revisión inicial por personal de mantenimiento.",
            "BAJA": "Revisión programable según disponibilidad del personal.",
        }
        estimate = PreliminaryIncidentEstimate(
            category=category,
            urgency=urgency,
            response_window=response_windows[urgency],
            cost_note="Costo pendiente de inspección técnica; no se asigna responsable de pago.",
            disclaimer=self.disclaimer,
        )
        return estimate.model_dump()

    def create_incident(self, action_id: int, user_id: int) -> Dict[str, Any]:
        with transaction.atomic():
            try:
                action = AgentAction.objects.select_for_update().get(
                    pk=action_id,
                    usuario_id=user_id,
                )
            except AgentAction.DoesNotExist:
                return self._error("not_found", "action_not_found", "La acción no existe.")
            if action.tipo_accion != self.action_type:
                return self._error(
                    "validation_error",
                    "invalid_action_type",
                    "La acción no corresponde a una incidencia.",
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
                    "La incidencia requiere confirmación explícita.",
                )
            if not action.idempotency_key:
                return self._fail_action(
                    action,
                    "validation_error",
                    "idempotency_key_required",
                    "La acción no tiene clave de idempotencia.",
                )
            try:
                request = IncidentRequest.model_validate(action.payload)
            except PydanticValidationError as exc:
                return self._fail_action(
                    action,
                    "validation_error",
                    "invalid_incident_parameters",
                    "; ".join(error["msg"] for error in exc.errors()),
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

            existing = Incidencia.objects.filter(
                idempotency_key=action.idempotency_key,
            ).first()
            if existing is not None:
                return self._complete_action(action, existing, replayed=True)

            incident = Incidencia.objects.create(
                residente=resident,
                categoria=request.category,
                titulo=request.title,
                descripcion=request.description,
                ubicacion=request.location,
                urgencia=request.urgency,
                estimacion_preliminar=request.preliminary_estimate.model_dump(),
                idempotency_key=action.idempotency_key,
            )
            return self._complete_action(action, incident, replayed=False)

    def verify_incident(self, action_id: int, user_id: int) -> Dict[str, Any]:
        try:
            action = AgentAction.objects.get(pk=action_id, usuario_id=user_id)
        except AgentAction.DoesNotExist:
            return self._error("not_found", "action_not_found", "La acción no existe.")
        incident = Incidencia.objects.filter(
            pk=action.backend_reference,
            residente__usuario_id=user_id,
            idempotency_key=action.idempotency_key,
        ).first()
        if incident is None:
            action.verification_status = AgentAction.VERIFICACION_FALLIDA
            action.error_code = "incident_not_verified"
            action.save(update_fields=["verification_status", "error_code"])
            return self._error(
                "unknown_result",
                "incident_not_verified",
                "No se pudo verificar la incidencia creada.",
            )
        action.verification_status = AgentAction.VERIFICACION_VERIFICADA
        action.save(update_fields=["verification_status"])
        return {
            "status": "success",
            "incident_id": incident.pk,
            "incident_status": incident.estado,
        }

    @staticmethod
    def _complete_action(action: AgentAction, incident: Incidencia, replayed: bool):
        result = {
            "status": "success",
            "incident_id": incident.pk,
            "incident_status": incident.estado,
            "replayed": replayed,
        }
        action.estado_previo = action.estado
        action.estado = AgentAction.EJECUTADA
        action.resultado = result
        action.backend_reference = str(incident.pk)
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
