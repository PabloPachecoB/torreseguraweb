from rest_framework import serializers

from .models import AgentAction


class AgentChatRequestSerializer(serializers.Serializer):
    message = serializers.CharField(max_length=4000, trim_whitespace=True)
    thread_id = serializers.UUIDField(required=False)


class AgentActionSerializer(serializers.ModelSerializer):
    class Meta:
        model = AgentAction
        fields = [
            'id',
            'tipo_accion',
            'payload',
            'thread_id',
            'requires_confirmation',
            'confirmation_method',
            'idempotency_key',
            'tool_name',
            'estado',
            'estado_previo',
            'fecha_creacion',
            'fecha_confirmacion',
            'confirmada_por',
            'expira_en',
            'resultado',
            'backend_reference',
            'executed_at',
            'verification_status',
            'error_code',
        ]
        read_only_fields = fields
