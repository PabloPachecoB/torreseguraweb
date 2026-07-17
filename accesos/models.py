from django.core.exceptions import ValidationError
from django.db import models
import secrets
from django.utils import timezone
from usuarios.models import Usuario
from viviendas.models import Vivienda, Residente

class Visita(models.Model):
    RESERVADA = 'RESERVADA'
    CONFIRMADA = 'CONFIRMADA'
    EXPIRADA = 'EXPIRADA'
    CANCELADA = 'CANCELADA'
    ESTADOS = [
        (RESERVADA, 'Reservada, esperando llegada'),
        (CONFIRMADA, 'Ingreso confirmado'),
        (EXPIRADA, 'Expirada sin confirmar'),
        (CANCELADA, 'Cancelada'),
    ]

    nombre_visitante = models.CharField(max_length=100)
    documento_visitante = models.CharField(max_length=20)
    vivienda_destino = models.ForeignKey(Vivienda, on_delete=models.CASCADE, related_name='visitas')
    residente_autoriza = models.ForeignKey(Residente, on_delete=models.CASCADE, related_name='visitas_autorizadas')
    # Nulo mientras la visita esta RESERVADA (a futuro) y todavia no llega nadie;
    # se llena recien cuando el vigilante confirma el ingreso real por QR.
    fecha_hora_entrada = models.DateTimeField(null=True, blank=True)
    fecha_hora_salida = models.DateTimeField(null=True, blank=True)
    motivo = models.TextField(blank=True)
    registrado_por = models.ForeignKey(Usuario, on_delete=models.SET_NULL, null=True)

    # Reserva a futuro con cantidad de personas (un solo QR para todo el grupo)
    cantidad_personas = models.PositiveIntegerField(default=1)
    fecha_visita = models.DateField(
        null=True, blank=True,
        help_text='Solo si es una reserva a futuro; vacio = registro inmediato (comportamiento historico).',
    )
    hora_inicio = models.TimeField(null=True, blank=True, help_text='Inicio de la ventana de validez del QR.')
    hora_fin = models.TimeField(null=True, blank=True, help_text='Fin de la ventana de validez del QR.')
    estado = models.CharField(max_length=12, choices=ESTADOS, default=CONFIRMADA)

    # Anti-replay QR
    qr_nonce = models.CharField(max_length=32, blank=True, default='', db_index=True)
    qr_usado = models.BooleanField(default=False)
    qr_usado_en = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-id']
        verbose_name = 'Visita'
        verbose_name_plural = 'Visitas'
        indexes = [
            models.Index(fields=['vivienda_destino', '-fecha_hora_entrada']),
            models.Index(fields=['residente_autoriza', '-fecha_hora_entrada']),
            models.Index(fields=['qr_usado']),
        ]

    def clean(self):
        if self.documento_visitante and len(self.documento_visitante.strip()) < 6:
            raise ValidationError({
                'documento_visitante': 'El documento debe tener al menos 6 caracteres.'
            })
        if self.fecha_hora_salida and self.fecha_hora_entrada:
            if self.fecha_hora_salida < self.fecha_hora_entrada:
                raise ValidationError({
                    'fecha_hora_salida': 'La fecha de salida no puede ser anterior a la de entrada.'
                })
        if self.fecha_visita and (not self.hora_inicio or not self.hora_fin):
            raise ValidationError({
                'hora_inicio': 'Si reserva con fecha, debe indicar hora_inicio y hora_fin.'
            })
        if self.hora_inicio and self.hora_fin and self.hora_fin <= self.hora_inicio:
            raise ValidationError({
                'hora_fin': 'La hora de fin debe ser mayor a la hora de inicio.'
            })

    def save(self, *args, **kwargs):
        if not self.qr_nonce:
            self.qr_nonce = secrets.token_urlsafe(24)[:32]
        super().save(*args, **kwargs)

    @property
    def ventana_expirada(self):
        """True si es una reserva a futuro y ya paso la hora_fin sin confirmar."""
        if not (self.fecha_visita and self.hora_fin):
            return False
        limite = timezone.datetime.combine(self.fecha_visita, self.hora_fin)
        if timezone.is_naive(limite):
            limite = timezone.make_aware(limite)
        return timezone.now() > limite

    @property
    def ventana_no_iniciada(self):
        """True si es una reserva a futuro y todavia no llega la hora_inicio."""
        if not (self.fecha_visita and self.hora_inicio):
            return False
        inicio = timezone.datetime.combine(self.fecha_visita, self.hora_inicio)
        if timezone.is_naive(inicio):
            inicio = timezone.make_aware(inicio)
        return timezone.now() < inicio

    def __str__(self):
        return f"{self.nombre_visitante} - {self.vivienda_destino} - {self.fecha_hora_entrada.strftime('%d/%m/%Y %H:%M')}"

class Puerta(models.Model):
    """Puerta controlable desde la app (principal, de edificio o de vivienda).

    Si `webhook_url` está configurada, al abrir se envía la orden al hardware
    (ESP32/relé/cerradura). Si está vacía, la apertura se registra y se
    responde OK (modo software).
    """
    TIPO_PRINCIPAL = 'PRINCIPAL'
    TIPO_EDIFICIO = 'EDIFICIO'
    TIPO_VIVIENDA = 'VIVIENDA'
    TIPOS = [
        (TIPO_PRINCIPAL, 'Puerta principal'),
        (TIPO_EDIFICIO, 'Puerta de edificio'),
        (TIPO_VIVIENDA, 'Puerta de vivienda'),
    ]

    nombre = models.CharField(max_length=100)
    tipo = models.CharField(max_length=10, choices=TIPOS)
    edificio = models.ForeignKey(
        'viviendas.Edificio', on_delete=models.CASCADE, null=True, blank=True,
        related_name='puertas',
        help_text='Requerido si el tipo es Puerta de edificio.'
    )
    vivienda = models.ForeignKey(
        Vivienda, on_delete=models.CASCADE, null=True, blank=True,
        related_name='puertas',
        help_text='Requerido si el tipo es Puerta de vivienda.'
    )
    activa = models.BooleanField(default=True)
    webhook_url = models.URLField(
        blank=True, default='',
        help_text='URL del dispositivo (ESP32/relé). Vacío = modo software.'
    )
    # HU-04.2 (LOCK-04): "abrir" solo funciona en demo controlada.
    habilitada_para_demo = models.BooleanField(
        default=False,
        help_text='Si está apagado, la apertura remota queda bloqueada (demo controlada).'
    )

    class Meta:
        ordering = ['tipo', 'nombre']
        verbose_name = 'Puerta'
        verbose_name_plural = 'Puertas'

    def clean(self):
        if self.tipo == self.TIPO_EDIFICIO and not self.edificio:
            raise ValidationError({'edificio': 'Una puerta de edificio debe tener edificio asignado.'})
        if self.tipo == self.TIPO_VIVIENDA and not self.vivienda:
            raise ValidationError({'vivienda': 'Una puerta de vivienda debe tener vivienda asignada.'})

    def __str__(self):
        return f"{self.nombre} ({self.get_tipo_display()})"


class AperturaPuerta(models.Model):
    puerta = models.ForeignKey(Puerta, on_delete=models.CASCADE, related_name='aperturas')
    usuario = models.ForeignKey(Usuario, on_delete=models.SET_NULL, null=True, related_name='aperturas_puerta')
    fecha_hora = models.DateTimeField(auto_now_add=True)
    exito = models.BooleanField(default=True)
    detalle = models.CharField(max_length=200, blank=True, default='')

    class Meta:
        ordering = ['-fecha_hora']
        verbose_name = 'Apertura de puerta'
        verbose_name_plural = 'Aperturas de puertas'
        indexes = [
            models.Index(fields=['puerta', '-fecha_hora']),
            models.Index(fields=['usuario', '-fecha_hora']),
        ]

    def __str__(self):
        return f"{self.puerta} - {self.usuario} - {self.fecha_hora.strftime('%d/%m/%Y %H:%M')}"


class MovimientoResidente(models.Model):
    residente = models.ForeignKey(Residente, on_delete=models.CASCADE, related_name='movimientos')
    fecha_hora_entrada = models.DateTimeField(null=True, blank=True)
    fecha_hora_salida = models.DateTimeField(null=True, blank=True)
    vehiculo = models.BooleanField(default=False)
    placa_vehiculo = models.CharField(max_length=10, blank=True)
    
    class Meta:
        ordering = ['-fecha_hora_entrada', '-fecha_hora_salida']
        verbose_name = 'Movimiento de Residente'
        verbose_name_plural = 'Movimientos de Residentes'
        indexes = [
            models.Index(fields=['residente', '-fecha_hora_entrada']),
            models.Index(fields=['-fecha_hora_salida']),
        ]

    def clean(self):
        if self.vehiculo and not self.placa_vehiculo.strip():
            raise ValidationError({
                'placa_vehiculo': 'Debe indicar la placa del vehículo.'
            })
        if self.fecha_hora_salida and self.fecha_hora_entrada:
            if self.fecha_hora_salida < self.fecha_hora_entrada:
                raise ValidationError({
                    'fecha_hora_salida': 'La fecha de salida no puede ser anterior a la de entrada.'
                })

    def __str__(self):
        tipo = "Entrada" if self.fecha_hora_entrada and not self.fecha_hora_salida else "Salida"
        fecha = self.fecha_hora_entrada if tipo == "Entrada" else self.fecha_hora_salida
        return f"{self.residente} - {tipo} - {fecha.strftime('%d/%m/%Y %H:%M') if fecha else 'N/A'}"
