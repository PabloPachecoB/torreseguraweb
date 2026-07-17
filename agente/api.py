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
        try:
            accion.confirmar(request.user)
        except PermissionError as exc:
            return Response({'error': str(exc)}, status=status.HTTP_403_FORBIDDEN)
        except ValueError as exc:
            return Response({'error': str(exc)}, status=status.HTTP_409_CONFLICT)
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
