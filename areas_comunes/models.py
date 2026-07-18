from django.db import models
from django.core.exceptions import ValidationError
from django.utils import timezone
from django.utils.dateparse import parse_time
from viviendas.models import Edificio, Residente


class AreaComun(models.Model):
    nombre = models.CharField(max_length=100)
    descripcion = models.TextField(blank=True)
    edificio = models.ForeignKey(
        Edificio, on_delete=models.CASCADE, related_name="areas_comunes"
    )
    capacidad_maxima = models.PositiveIntegerField(default=20)
    horario_inicio = models.TimeField(default="08:00")
    horario_fin = models.TimeField(default="22:00")
    imagen = models.ImageField(upload_to="areas_comunes/", blank=True, null=True)
    activo = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["nombre"]
        verbose_name = "Area Comun"
        verbose_name_plural = "Areas Comunes"

    def __str__(self):
        return f"{self.nombre} - {self.edificio.nombre}"


class Reserva(models.Model):
    ESTADOS = [
        ("pendiente", "Pendiente"),
        ("confirmada", "Confirmada"),
        ("cancelada", "Cancelada"),
        ("completada", "Completada"),
    ]

    area_comun = models.ForeignKey(
        AreaComun, on_delete=models.CASCADE, related_name="reservas"
    )
    residente = models.ForeignKey(
        Residente, on_delete=models.CASCADE, related_name="reservas"
    )
    fecha = models.DateField()
    hora_inicio = models.TimeField()
    hora_fin = models.TimeField()
    estado = models.CharField(max_length=20, choices=ESTADOS, default="confirmada")
    cantidad_personas = models.PositiveIntegerField(default=1)
    motivo = models.CharField(max_length=200, blank=True)
    idempotency_key = models.CharField(
        max_length=64,
        unique=True,
        null=True,
        blank=True,
        help_text="Evita crear dos reservas para una misma solicitud.",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-fecha", "-hora_inicio"]
        verbose_name = "Reserva"
        verbose_name_plural = "Reservas"
        indexes = [
            models.Index(fields=["area_comun", "fecha"]),
            models.Index(fields=["residente", "-fecha"]),
        ]

    def clean(self):
        if self.fecha and self.fecha < timezone.localdate():
            raise ValidationError({"fecha": "La fecha no puede estar en el pasado."})

        if self.hora_fin <= self.hora_inicio:
            raise ValidationError(
                {"hora_fin": "La hora de fin debe ser mayor a la hora de inicio."}
            )

        if self.area_comun_id:
            area_start = self.area_comun.horario_inicio
            area_end = self.area_comun.horario_fin
            if isinstance(area_start, str):
                area_start = parse_time(area_start)
            if isinstance(area_end, str):
                area_end = parse_time(area_end)
            if self.hora_inicio < area_start:
                raise ValidationError(
                    {"hora_inicio": "La hora de inicio está fuera del horario del área."}
                )
            if self.hora_fin > area_end:
                raise ValidationError(
                    {"hora_fin": "La hora de fin está fuera del horario del área."}
                )
            if self.cantidad_personas > self.area_comun.capacidad_maxima:
                raise ValidationError(
                    {
                        "cantidad_personas": (
                            "La cantidad de personas supera la capacidad del área."
                        )
                    }
                )

        solapadas = Reserva.objects.filter(
            area_comun=self.area_comun,
            fecha=self.fecha,
            estado__in=["pendiente", "confirmada"],
            hora_inicio__lt=self.hora_fin,
            hora_fin__gt=self.hora_inicio,
        ).exclude(pk=self.pk)

        if solapadas.exists():
            raise ValidationError(
                "Ya existe una reserva en ese horario para esta area."
            )

    def save(self, *args, **kwargs):
        self.full_clean()
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.area_comun.nombre} - {self.residente} - {self.fecha}"
