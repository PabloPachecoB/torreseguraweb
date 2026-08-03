from __future__ import annotations

from rest_framework import serializers

from .models import Visita


class VisitanteSerializer(serializers.ModelSerializer):
    # Campos "mobile" solicitados
    name = serializers.CharField(source="nombre_visitante", read_only=True)
    document = serializers.CharField(source="documento_visitante", read_only=True)
    purpose = serializers.CharField(source="motivo", read_only=True)
    entryDate = serializers.DateTimeField(source="fecha_hora_entrada", read_only=True)
    exitDate = serializers.DateTimeField(source="fecha_hora_salida", read_only=True)
    departmentNumber = serializers.SerializerMethodField()
    whoAuthorizes = serializers.SerializerMethodField()
    status = serializers.SerializerMethodField()
    visitDate = serializers.DateField(source="fecha_visita", read_only=True)
    startTime = serializers.TimeField(source="hora_inicio", read_only=True)
    endTime = serializers.TimeField(source="hora_fin", read_only=True)
    peopleCount = serializers.IntegerField(source="cantidad_personas", read_only=True)
    reservationStatus = serializers.CharField(source="estado", read_only=True)
    qrUsed = serializers.BooleanField(source="qr_usado", read_only=True)
    photoUrl = serializers.ImageField(source="foto_visitante", read_only=True)
    arrivalReportedAt = serializers.DateTimeField(source="llegada_reportada_en", read_only=True)
    residentDecisionAt = serializers.DateTimeField(source="decision_residente_en", read_only=True)
    notificationStatus = serializers.CharField(source="notificacion_estado", read_only=True)

    class Meta:
        model = Visita
        fields = [
            "id",
            "name",
            "document",
            "purpose",
            "entryDate",
            "exitDate",
            "departmentNumber",
            "whoAuthorizes",
            "status",
            "visitDate",
            "startTime",
            "endTime",
            "peopleCount",
            "reservationStatus",
            "qrUsed",
            "photoUrl",
            "arrivalReportedAt",
            "residentDecisionAt",
            "notificationStatus",
        ]

    def get_departmentNumber(self, obj: Visita):
        vivienda = getattr(obj, "vivienda_destino", None)
        return getattr(vivienda, "numero", None) if vivienda else None

    def get_whoAuthorizes(self, obj: Visita):
        residente = getattr(obj, "residente_autoriza", None)
        usuario = getattr(residente, "usuario", None) if residente else None
        if not usuario:
            return None
        full_name = f"{usuario.first_name} {usuario.last_name}".strip()
        return full_name or getattr(usuario, "username", None)

    def get_status(self, obj: Visita):
        if getattr(obj, "fecha_hora_salida", None):
            return "departed"
        if getattr(obj, "qr_usado", False):
            return "scanned"
        return "pending"
