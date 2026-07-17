from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from .agent import get_conversation_service
from .llm import get_llm_adapter
from .models import AgentAction
from .serializers import AgentActionSerializer, AgentChatRequestSerializer


class AgentActionViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet):
    """API movil de HU-01.2: revisar y confirmar/rechazar acciones propuestas
    por el agente conversacional.

    Solo expone las acciones del usuario autenticado — nunca las de otro
    residente (SEC-01). La creacion de AgentAction queda fuera de esta HU
    (la produce el motor conversacional de HU-01.1).
    """

    serializer_class = AgentActionSerializer
    permission_classes = [IsAuthenticated]
    pagination_class = None

    def get_queryset(self):
        return AgentAction.objects.filter(usuario=self.request.user).order_by('-fecha_creacion')

    @action(detail=True, methods=['post'])
    def confirmar(self, request, pk=None):
        accion = self.get_object()
        if accion.thread_id:
            try:
                conversation = get_conversation_service().resume_confirmation(
                    request.user,
                    accion,
                    approved=True,
                )
            except (PermissionError, ValueError) as exc:
                return Response({'error': str(exc)}, status=status.HTTP_409_CONFLICT)
            except Exception:
                return Response(
                    {
                        'error_code': 'confirmation_resume_failed',
                        'detail': 'No se pudo reanudar la acción.',
                    },
                    status=status.HTTP_503_SERVICE_UNAVAILABLE,
                )
            accion.refresh_from_db()
            payload = dict(self.get_serializer(accion).data)
            payload['conversation'] = conversation
            return Response(payload)

        # HU-04.2 (LOCK-04): las acciones de cerradura exigen confirmación
        # reforzada — segundo factor = re-autenticación con la contraseña.
        if accion.tipo_accion.startswith('CERRADURA_'):
            password = request.data.get('password', '')
            if not password:
                return Response(
                    {'error': 'Confirmación reforzada: debes reingresar tu contraseña.'},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            if not request.user.check_password(password):
                return Response(
                    {'error': 'Contraseña incorrecta. La apertura no se ejecutó.'},
                    status=status.HTTP_403_FORBIDDEN,
                )

        try:
            accion.confirmar(request.user)
        except PermissionError as exc:
            return Response({'error': str(exc)}, status=status.HTTP_403_FORBIDDEN)
        except ValueError as exc:
            return Response({'error': str(exc)}, status=status.HTTP_409_CONFLICT)

        # Ejecutar la apertura física tras la confirmación (HU-04.2 → 04.3).
        if accion.tipo_accion == 'CERRADURA_ABRIR':
            from accesos.api_puertas import ejecutar_apertura, _puertas_permitidas

            try:
                puerta = _puertas_permitidas(request.user).get(pk=accion.payload.get('puerta_id'))
            except Exception:
                accion.resultado = {'abierta': False, 'detalle': 'Puerta no encontrada o sin permiso.'}
                accion.save(update_fields=['resultado'])
                return Response(
                    {'error': 'Puerta no encontrada o sin permiso.', 'accion': self.get_serializer(accion).data},
                    status=status.HTTP_403_FORBIDDEN,
                )

            exito, detalle = ejecutar_apertura(puerta, request.user)
            accion.estado_previo = accion.estado
            accion.estado = AgentAction.EJECUTADA
            accion.resultado = {'abierta': exito, 'detalle': detalle}
            accion.save(update_fields=['estado', 'estado_previo', 'resultado'])

            # HU-04.3: nunca presentar un fallo como éxito.
            return Response(
                {
                    'abierta': exito,
                    'mensaje': f'{puerta.nombre} abierta correctamente.' if exito
                               else 'La puerta no respondió. Intenta de nuevo.',
                    'accion': self.get_serializer(accion).data,
                },
                status=status.HTTP_200_OK if exito else status.HTTP_502_BAD_GATEWAY,
            )

        return Response(self.get_serializer(accion).data)

    @action(detail=True, methods=['post'])
    def rechazar(self, request, pk=None):
        accion = self.get_object()
        if accion.thread_id:
            try:
                conversation = get_conversation_service().resume_confirmation(
                    request.user,
                    accion,
                    approved=False,
                )
            except (PermissionError, ValueError) as exc:
                return Response({'error': str(exc)}, status=status.HTTP_409_CONFLICT)
            except Exception:
                return Response(
                    {
                        'error_code': 'confirmation_resume_failed',
                        'detail': 'No se pudo reanudar la acción.',
                    },
                    status=status.HTTP_503_SERVICE_UNAVAILABLE,
                )
            accion.refresh_from_db()
            payload = dict(self.get_serializer(accion).data)
            payload['conversation'] = conversation
            return Response(payload)
        try:
            accion.rechazar(request.user)
        except PermissionError as exc:
            return Response({'error': str(exc)}, status=status.HTTP_403_FORBIDDEN)
        except ValueError as exc:
            return Response({'error': str(exc)}, status=status.HTTP_409_CONFLICT)
        return Response(self.get_serializer(accion).data)

    @action(detail=False, methods=['get'])
    def health(self, request):
        adapter = get_llm_adapter()
        payload = adapter.health_check()
        return Response(payload)

    @action(detail=False, methods=['post'])
    def chat(self, request):
        serializer = AgentChatRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        service = get_conversation_service()
        try:
            payload = service.chat(
                user=request.user,
                message=serializer.validated_data['message'],
                thread_id=serializer.validated_data.get('thread_id'),
            )
        except ValueError as exc:
            return Response(
                {'error_code': 'invalid_thread_id', 'detail': str(exc)},
                status=status.HTTP_400_BAD_REQUEST,
            )
        except Exception:
            return Response(
                {
                    'error_code': 'conversation_unavailable',
                    'detail': 'La conversación no está disponible temporalmente.',
                },
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        response_status = (
            status.HTTP_200_OK
            if payload['status'] in {'ok', 'awaiting_confirmation'}
            else status.HTTP_503_SERVICE_UNAVAILABLE
        )
        return Response(payload, status=response_status)
