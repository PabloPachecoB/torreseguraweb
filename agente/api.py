from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
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

        # Una cerradura siempre exige reautenticación, también cuando la acción
        # está vinculada a un thread de LangGraph. La contraseña nunca entra al
        # estado conversacional ni al checkpoint.
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

        # Las acciones conversacionales se reanudan
        # desde el interrupt de LangGraph.
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

        # La transición condicional de AgentAction confirma una sola petición;
        # funciona de forma atómica también sobre SQLite.
        try:
            accion.confirmar(request.user)
        except PermissionError as exc:
            return Response({'error': str(exc)}, status=status.HTTP_403_FORBIDDEN)
        except ValueError as exc:
            return Response({'error': str(exc)}, status=status.HTTP_409_CONFLICT)

        if accion.tipo_accion == 'CERRADURA_ABRIR':
            from .tools import DoorTools

            tools = DoorTools()
            result = tools.open(accion.pk, request.user.pk)
            if result.get('opening_id'):
                tools.verify(accion.pk, request.user.pk)
            accion.refresh_from_db()
            return Response(
                {
                    'abierta': bool(result.get('success')),
                    'mensaje': result.get('message'),
                    'hardware_status': result.get('hardware_status'),
                    'error_code': result.get('error_code'),
                    'accion': self.get_serializer(accion).data,
                },
                status=(
                    status.HTTP_200_OK
                    if result.get('success')
                    else status.HTTP_502_BAD_GATEWAY
                    if result.get('opening_id')
                    else status.HTTP_403_FORBIDDEN
                ),
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

    @action(
        detail=False,
        methods=['post'],
        parser_classes=[JSONParser, MultiPartParser, FormParser],
    )
    def chat(self, request):
        serializer = AgentChatRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        message = serializer.validated_data.get('message', '')
        transcription = None
        audio = serializer.validated_data.get('audio')
        if audio:
            audio_format = audio.name.rsplit('.', 1)[-1].lower()
            images = [
                {
                    'data': image.read(),
                    'content_type': image.content_type,
                }
                for image in serializer.validated_data.get('images', [])
            ]
            transcription_result = get_llm_adapter().transcribe_audio(
                audio.read(), audio_format, images=images,
            )
            if not transcription_result.get('healthy'):
                return Response(
                    {
                        'error_code': transcription_result.get(
                            'error_code', 'voice_transcription_unavailable',
                        ),
                        'detail': (
                            'No pude transcribir el mensaje de voz. Verifica la '
                            'configuración de Qwen Cloud e inténtalo nuevamente.'
                        ),
                    },
                    status=status.HTTP_503_SERVICE_UNAVAILABLE,
                )
            transcription = transcription_result['transcription']
            message = transcription
        service = get_conversation_service()
        try:
            chat_kwargs = {
                'user': request.user,
                'message': message,
                'thread_id': serializer.validated_data.get('thread_id'),
            }
            if serializer.validated_data.get('interaction'):
                chat_kwargs['interaction'] = serializer.validated_data[
                    'interaction'
                ]
            payload = service.chat(**chat_kwargs)
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
        if transcription:
            payload['transcription'] = transcription
        response_status = (
            status.HTTP_200_OK
            if payload['status'] in {'ok', 'awaiting_confirmation'}
            else status.HTTP_503_SERVICE_UNAVAILABLE
        )
        return Response(payload, status=response_status)
