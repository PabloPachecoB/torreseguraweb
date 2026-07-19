"""Tool controlada para apertura de puertas."""

from typing import Any, Dict

from django.contrib.auth import get_user_model
from django.utils import timezone
from pydantic import ValidationError as PydanticValidationError

from accesos.services import allowed_doors, open_door, serialize_door, verify_opening
from agente.models import AgentAction

from .schemas import DoorOpenRequest


class DoorTools:
    action_type = 'CERRADURA_ABRIR'
    create_tool_name = 'open_door'

    def list_doors(self, context: Dict[str, Any]) -> Dict[str, Any]:
        user = get_user_model().objects.filter(pk=context.get('user_id')).first()
        if user is None:
            return self._error('unauthorized', 'user_not_found', 'Usuario no encontrado.')
        doors = [
            serialize_door(door)
            for door in allowed_doors(user).filter(habilitada_para_demo=True)
        ]
        if not doors:
            return self._error(
                'not_found',
                'no_demo_doors',
                'No hay puertas autorizadas habilitadas para la demo remota.',
            )
        return {'status': 'success', 'doors': doors}

    def open(self, action_id: int, user_id: int) -> Dict[str, Any]:
        try:
            action = AgentAction.objects.select_related('usuario').get(
                pk=action_id,
                usuario_id=user_id,
            )
        except AgentAction.DoesNotExist:
            return self._error('not_found', 'action_not_found', 'La acción no existe.')
        if action.tipo_accion != self.action_type:
            return self._error('validation_error', 'invalid_action_type', 'Tipo de acción inválido.')
        if action.estado == AgentAction.EJECUTADA:
            return action.resultado or self._error(
                'unknown_result', 'missing_action_result', 'Falta el resultado de apertura.'
            )
        if action.estado != AgentAction.CONFIRMADA:
            return self._error('unauthorized', 'confirmation_required', 'La apertura requiere confirmación.')
        if not action.idempotency_key:
            return self._fail(action, 'validation_error', 'idempotency_key_required', 'Falta idempotencia.')
        try:
            request = DoorOpenRequest.model_validate(action.payload)
        except PydanticValidationError as exc:
            return self._fail(
                action,
                'validation_error',
                'invalid_door_parameters',
                '; '.join(error['msg'] for error in exc.errors()),
            )

        result = open_door(action.usuario, request.door_id, action.idempotency_key)
        if result.get('opening_id'):
            action.estado_previo = action.estado
            action.estado = AgentAction.EJECUTADA
            action.resultado = result
            action.backend_reference = str(result['opening_id'])
            action.executed_at = timezone.now()
            action.error_code = result.get('error_code') or ''
            action.save(update_fields=[
                'estado_previo', 'estado', 'resultado', 'backend_reference',
                'executed_at', 'error_code',
            ])
        else:
            self._store_error(action, result)
        return result

    def verify(self, action_id: int, user_id: int) -> Dict[str, Any]:
        try:
            action = AgentAction.objects.select_related('usuario').get(
                pk=action_id,
                usuario_id=user_id,
            )
        except AgentAction.DoesNotExist:
            return self._error('not_found', 'action_not_found', 'La acción no existe.')
        result = verify_opening(
            action.usuario,
            int(action.backend_reference or 0),
            action.idempotency_key or '',
        )
        action.verification_status = (
            AgentAction.VERIFICACION_VERIFICADA
            if result.get('opening_id')
            else AgentAction.VERIFICACION_FALLIDA
        )
        action.save(update_fields=['verification_status'])
        return result

    @classmethod
    def _fail(cls, action, status, error_code, message):
        result = cls._error(status, error_code, message)
        cls._store_error(action, result)
        return result

    @staticmethod
    def _store_error(action, result):
        action.error_code = result.get('error_code') or ''
        action.resultado = result
        action.save(update_fields=['error_code', 'resultado'])

    @staticmethod
    def _error(status, error_code, message):
        return {'status': status, 'success': False, 'error_code': error_code, 'message': message}
