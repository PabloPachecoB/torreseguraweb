from rest_framework import serializers

from .models import AgentAction


class AgentActionSerializer(serializers.ModelSerializer):
    class Meta:
        model = AgentAction
        fields = [
            'id',
            'tipo_accion',
            'payload',
            'estado',
            'estado_previo',
            'fecha_creacion',
            'fecha_confirmacion',
            'confirmada_por',
            'expira_en',
            'resultado',
        ]
        read_only_fields = fields
