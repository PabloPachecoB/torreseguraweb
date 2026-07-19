from rest_framework import serializers
from rest_framework.reverse import reverse

from .models import (
    AprobacionIncidencia,
    EventoIncidencia,
    EvidenciaIncidencia,
    Incidencia,
    NotificacionIncidencia,
    OrdenTrabajo,
    RevisionIncidencia,
)


class AprobacionIncidenciaSerializer(serializers.ModelSerializer):
    usuario_nombre = serializers.CharField(source='usuario.get_full_name', read_only=True)

    class Meta:
        model = AprobacionIncidencia
        fields = ['rol', 'decision', 'usuario_nombre', 'comentario', 'fecha']
        read_only_fields = fields


class RevisionIncidenciaSerializer(serializers.ModelSerializer):
    aprobaciones = AprobacionIncidenciaSerializer(many=True, read_only=True)

    class Meta:
        model = RevisionIncidencia
        fields = [
            'id', 'version', 'categoria', 'prioridad', 'costo_estimado_min',
            'costo_estimado_max', 'moneda', 'tiempo_estimado_horas',
            'comentario', 'origen', 'vigente', 'fecha_creacion', 'aprobaciones',
        ]
        read_only_fields = fields


class OrdenTrabajoSerializer(serializers.ModelSerializer):
    tecnico_nombre = serializers.CharField(
        source='tecnico.nombre_completo', default=None, read_only=True,
    )

    class Meta:
        model = OrdenTrabajo
        fields = [
            'codigo', 'estado', 'tecnico_nombre', 'programada_inicio',
            'programada_fin', 'fecha_aprobacion', 'fecha_actualizacion',
        ]
        read_only_fields = fields


class NotificacionIncidenciaSerializer(serializers.ModelSerializer):
    incidencia_id = serializers.IntegerField(read_only=True)

    class Meta:
        model = NotificacionIncidencia
        fields = ['id', 'incidencia_id', 'tipo', 'mensaje', 'leida', 'fecha']
        read_only_fields = fields


class RevisionUpdateSerializer(serializers.Serializer):
    categoria = serializers.ChoiceField(choices=Incidencia.CATEGORIAS, required=False)
    prioridad = serializers.ChoiceField(choices=Incidencia.URGENCIAS, required=False)
    costo_estimado_min = serializers.DecimalField(
        max_digits=10, decimal_places=2, min_value=0, required=False,
        allow_null=True,
    )
    costo_estimado_max = serializers.DecimalField(
        max_digits=10, decimal_places=2, min_value=0, required=False,
        allow_null=True,
    )
    moneda = serializers.CharField(min_length=3, max_length=3, required=False)
    tiempo_estimado_horas = serializers.IntegerField(
        min_value=1, max_value=720, required=False, allow_null=True,
    )
    comentario = serializers.CharField(max_length=2000, required=False, allow_blank=True)
    empleado_id = serializers.IntegerField(min_value=1, required=False)

    def validate(self, attrs):
        if not attrs:
            raise serializers.ValidationError('Debes enviar al menos un ajuste.')
        minimum = attrs.get('costo_estimado_min')
        maximum = attrs.get('costo_estimado_max')
        if minimum is not None and maximum is not None and maximum < minimum:
            raise serializers.ValidationError(
                'El costo máximo debe ser mayor o igual al mínimo.'
            )
        return attrs


class DecisionRevisionSerializer(serializers.Serializer):
    comentario = serializers.CharField(max_length=2000, required=False, allow_blank=True)


class EventoIncidenciaSerializer(serializers.ModelSerializer):
    usuario = serializers.CharField(source='usuario.username', default=None, read_only=True)
    tipo_evento_display = serializers.CharField(source='get_tipo_evento_display', read_only=True)

    class Meta:
        model = EventoIncidencia
        fields = [
            'id', 'tipo_evento', 'tipo_evento_display',
            'estado_anterior', 'estado_nuevo', 'comentario', 'usuario', 'fecha',
        ]
        read_only_fields = fields


class EvidenciaIncidenciaSerializer(serializers.ModelSerializer):
    tipo_display = serializers.CharField(source='get_tipo_display', read_only=True)
    url_descarga = serializers.SerializerMethodField()

    class Meta:
        model = EvidenciaIncidencia
        fields = ['id', 'tipo', 'tipo_display', 'fecha_subida', 'url_descarga']
        read_only_fields = fields

    def get_url_descarga(self, obj):
        """Nunca exponemos `obj.archivo.url` (la ruta cruda de MEDIA_URL) —
        el archivo es "almacenamiento privado": solo se llega a el pasando
        por este endpoint autenticado, que valida permisos antes de servirlo.
        """
        request = self.context.get('request')
        url = reverse(
            'api_v1_descargar_evidencia',
            kwargs={'incidencia_id': obj.incidencia_id, 'evidencia_id': obj.pk},
        )
        return request.build_absolute_uri(url) if request else url


class IncidenciaSerializer(serializers.ModelSerializer):
    categoria_display = serializers.CharField(source='get_categoria_display', read_only=True)
    estado_display = serializers.CharField(source='get_estado_display', read_only=True)
    urgencia_display = serializers.CharField(source='get_urgencia_display', read_only=True)
    residente_nombre = serializers.SerializerMethodField()
    evidencias = EvidenciaIncidenciaSerializer(many=True, read_only=True)
    eventos = EventoIncidenciaSerializer(many=True, read_only=True)
    revisiones = RevisionIncidenciaSerializer(many=True, read_only=True)
    orden_trabajo = OrdenTrabajoSerializer(read_only=True)
    tecnico_asignado = serializers.CharField(
        source='empleado_asignado.nombre_completo', default=None, read_only=True,
    )

    class Meta:
        model = Incidencia
        fields = [
            'id', 'categoria', 'categoria_display', 'titulo', 'descripcion',
            'ubicacion', 'urgencia', 'urgencia_display', 'estimacion_preliminar',
            'estado', 'estado_display', 'residente_nombre',
            'tecnico_asignado',
            'fecha_creacion', 'fecha_actualizacion',
            'evidencias', 'eventos', 'revisiones', 'orden_trabajo',
        ]
        read_only_fields = [
            'id', 'estado', 'estimacion_preliminar', 'fecha_creacion',
            'fecha_actualizacion', 'evidencias', 'eventos',
            'revisiones', 'orden_trabajo',
        ]

    def get_residente_nombre(self, obj):
        usuario = obj.residente.usuario
        nombre = f'{usuario.first_name} {usuario.last_name}'.strip()
        return nombre or usuario.username


class IncidenciaListSerializer(serializers.ModelSerializer):
    """Version liviana para listados — sin evidencias/eventos anidados."""

    categoria_display = serializers.CharField(source='get_categoria_display', read_only=True)
    estado_display = serializers.CharField(source='get_estado_display', read_only=True)
    urgencia_display = serializers.CharField(source='get_urgencia_display', read_only=True)

    class Meta:
        model = Incidencia
        fields = [
            'id', 'categoria', 'categoria_display', 'titulo',
            'ubicacion', 'urgencia', 'urgencia_display',
            'estado', 'estado_display', 'fecha_creacion', 'fecha_actualizacion',
        ]
        read_only_fields = fields
