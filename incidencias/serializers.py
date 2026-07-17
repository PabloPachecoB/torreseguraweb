from rest_framework import serializers
from rest_framework.reverse import reverse

from .models import EventoIncidencia, EvidenciaIncidencia, Incidencia


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
    residente_nombre = serializers.SerializerMethodField()
    evidencias = EvidenciaIncidenciaSerializer(many=True, read_only=True)
    eventos = EventoIncidenciaSerializer(many=True, read_only=True)

    class Meta:
        model = Incidencia
        fields = [
            'id', 'categoria', 'categoria_display', 'titulo', 'descripcion',
            'estado', 'estado_display', 'residente_nombre',
            'fecha_creacion', 'fecha_actualizacion',
            'evidencias', 'eventos',
        ]
        read_only_fields = ['id', 'estado', 'fecha_creacion', 'fecha_actualizacion', 'evidencias', 'eventos']

    def get_residente_nombre(self, obj):
        usuario = obj.residente.usuario
        nombre = f'{usuario.first_name} {usuario.last_name}'.strip()
        return nombre or usuario.username


class IncidenciaListSerializer(serializers.ModelSerializer):
    """Version liviana para listados — sin evidencias/eventos anidados."""

    categoria_display = serializers.CharField(source='get_categoria_display', read_only=True)
    estado_display = serializers.CharField(source='get_estado_display', read_only=True)

    class Meta:
        model = Incidencia
        fields = [
            'id', 'categoria', 'categoria_display', 'titulo',
            'estado', 'estado_display', 'fecha_creacion', 'fecha_actualizacion',
        ]
        read_only_fields = fields
