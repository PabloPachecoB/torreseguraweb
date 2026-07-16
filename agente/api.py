from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from .models import AgentAction
from .serializers import AgentActionSerializer


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
        try:
            accion.rechazar(request.user)
        except PermissionError as exc:
            return Response({'error': str(exc)}, status=status.HTTP_403_FORBIDDEN)
        except ValueError as exc:
            return Response({'error': str(exc)}, status=status.HTTP_409_CONFLICT)
        return Response(self.get_serializer(accion).data)
