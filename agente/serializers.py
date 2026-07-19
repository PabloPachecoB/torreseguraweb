from rest_framework import serializers

from .models import AgentAction


class AgentInteractionSerializer(serializers.Serializer):
    type = serializers.ChoiceField(choices=[
        'check_area_availability',
        'start_reservation',
        'select_reservation_slot',
    ])
    payload = serializers.JSONField()

    def validate_payload(self, payload):
        if not isinstance(payload, dict):
            raise serializers.ValidationError('Debe ser un objeto.')
        return payload

    def validate(self, attrs):
        payload = attrs['payload']
        if attrs['type'] == 'select_reservation_slot':
            allowed = {'area_id', 'date', 'start_time', 'end_time'}
            serializer_class = ReservationSlotInteractionSerializer
        else:
            allowed = {'area_id'}
            serializer_class = AreaInteractionSerializer
        if set(payload) != allowed:
            raise serializers.ValidationError({
                'payload': f'Debe incluir únicamente {", ".join(sorted(allowed))}.'
            })
        serializer = serializer_class(data=payload)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        normalized = {'area_id': data['area_id']}
        if attrs['type'] == 'select_reservation_slot':
            normalized.update({
                'date': data['date'].isoformat(),
                'start_time': data['start_time'].strftime('%H:%M'),
                'end_time': data['end_time'].strftime('%H:%M'),
            })
        attrs['payload'] = normalized
        return attrs


class AreaInteractionSerializer(serializers.Serializer):
    area_id = serializers.IntegerField(min_value=1)


class ReservationSlotInteractionSerializer(serializers.Serializer):
    area_id = serializers.IntegerField(min_value=1)
    date = serializers.DateField()
    start_time = serializers.TimeField()
    end_time = serializers.TimeField()

    def validate(self, attrs):
        if attrs['end_time'] <= attrs['start_time']:
            raise serializers.ValidationError(
                'end_time debe ser posterior a start_time.'
            )
        return attrs


class AgentChatRequestSerializer(serializers.Serializer):
    message = serializers.CharField(
        max_length=4000, trim_whitespace=True, required=False
    )
    thread_id = serializers.UUIDField(required=False)
    interaction = AgentInteractionSerializer(required=False)
    audio = serializers.FileField(required=False, write_only=True)
    images = serializers.ListField(
        child=serializers.FileField(),
        required=False,
        write_only=True,
        max_length=3,
    )

    def validate(self, attrs):
        audio = attrs.get('audio')
        if not attrs.get('message') and not attrs.get('interaction') and not audio:
            raise serializers.ValidationError(
                'Debe enviar message, audio o una interaction.'
            )
        if audio:
            # Qwen limita la cadena Base64 a 10 MB; 7 MB binarios dejan margen
            # para la expansión Base64 y el resto del JSON.
            if audio.size > 7 * 1024 * 1024:
                raise serializers.ValidationError({
                    'audio': 'El mensaje de voz no puede superar 7 MB.',
                })
            extension = audio.name.rsplit('.', 1)[-1].lower() if '.' in audio.name else ''
            if extension not in {'wav', 'mp3', 'aac', 'amr', '3gp', '3gpp'}:
                raise serializers.ValidationError({
                    'audio': 'Formato de audio no admitido.',
                })
        images = attrs.get('images', [])
        for image in images:
            content_type = (getattr(image, 'content_type', '') or '').lower()
            if not content_type.startswith('image/'):
                raise serializers.ValidationError({
                    'images': 'Sólo se admiten imágenes como contexto del audio.',
                })
            if image.size > 5 * 1024 * 1024:
                raise serializers.ValidationError({
                    'images': 'Cada imagen debe pesar menos de 5 MB.',
                })
        return attrs


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
