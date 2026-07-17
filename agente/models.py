from django.conf import settings
from django.db import models, transaction
from django.utils import timezone


class AgentAction(models.Model):
    """Accion propuesta por el agente conversacional (HU-01.1) que un
    residente debe revisar y confirmar antes de que se ejecute (HU-01.2).

    Estados provisionales — HU-01.1 (Huascar) todavia no aterriza, asi que
    este modelo se disena desacoplado del motor de chat: no depende de una
    sesion/mensaje, solo del usuario dueno y un payload generico reutilizable
    por HU-02.x (reservas) y HU-03.x (incidencias).
    """

    PENDIENTE = 'PENDIENTE'
    CONFIRMADA = 'CONFIRMADA'
    EJECUTADA = 'EJECUTADA'
    RECHAZADA = 'RECHAZADA'
    EXPIRADA = 'EXPIRADA'

    VERIFICACION_NO_INICIADA = 'NO_INICIADA'
    VERIFICACION_VERIFICADA = 'VERIFICADA'
    VERIFICACION_FALLIDA = 'FALLIDA'
    VERIFICACION_DESCONOCIDA = 'DESCONOCIDA'

    ESTADOS = [
        (PENDIENTE, 'Pendiente de confirmacion'),
        (CONFIRMADA, 'Confirmada, pendiente de ejecucion'),
        (EJECUTADA, 'Ejecutada'),
        (RECHAZADA, 'Rechazada por el usuario'),
        (EXPIRADA, 'Expirada sin confirmar'),
    ]

    usuario = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='acciones_agente',
        help_text='Usuario para quien el agente propuso la accion.',
    )
    thread_id = models.CharField(max_length=36, blank=True, db_index=True)
    tipo_accion = models.CharField(
        max_length=50,
        help_text='Identificador libre del tipo de accion (ej. RESERVA_CREAR, INCIDENCIA_CREAR).',
    )
    payload = models.JSONField(
        help_text='Parametros de la accion que el agente ejecutaria al confirmarse.',
    )
    requires_confirmation = models.BooleanField(default=True)
    confirmation_method = models.CharField(max_length=30, blank=True)
    idempotency_key = models.CharField(max_length=64, null=True, blank=True)
    tool_name = models.CharField(max_length=100, blank=True)
    backend_reference = models.CharField(max_length=100, blank=True)
    estado = models.CharField(max_length=15, choices=ESTADOS, default=PENDIENTE)
    estado_previo = models.CharField(
        max_length=15,
        choices=ESTADOS,
        null=True,
        blank=True,
        help_text='Estado inmediatamente anterior a `estado`, para poder ver la ultima transicion sin una tabla de historial aparte.',
    )

    fecha_creacion = models.DateTimeField(auto_now_add=True)
    fecha_confirmacion = models.DateTimeField(null=True, blank=True)
    confirmada_por = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='acciones_agente_confirmadas',
    )
    expira_en = models.DateTimeField(
        null=True,
        blank=True,
        help_text='Si se define y se supera sin confirmar, la accion pasa a EXPIRADA.',
    )
    resultado = models.JSONField(
        null=True,
        blank=True,
        help_text='Salida de la ejecucion real (la llena el ejecutor de HU-01.1 al procesar la confirmacion).',
    )
    executed_at = models.DateTimeField(null=True, blank=True)
    verification_status = models.CharField(
        max_length=20,
        choices=[
            (VERIFICACION_NO_INICIADA, 'No iniciada'),
            (VERIFICACION_VERIFICADA, 'Verificada'),
            (VERIFICACION_FALLIDA, 'Fallida'),
            (VERIFICACION_DESCONOCIDA, 'Desconocida'),
        ],
        default=VERIFICACION_NO_INICIADA,
    )
    error_code = models.CharField(max_length=50, blank=True)

    class Meta:
        verbose_name = 'Accion del agente'
        verbose_name_plural = 'Acciones del agente'
        ordering = ['-fecha_creacion']
        constraints = [
            models.UniqueConstraint(
                fields=['usuario', 'idempotency_key'],
                name='unique_agent_action_idempotency_per_user',
            ),
        ]

    def __str__(self):
        return f'{self.tipo_accion} ({self.estado}) — {self.usuario}'

    @property
    def esta_expirada(self):
        return bool(self.expira_en and timezone.now() > self.expira_en)

    def confirmar(self, usuario):
        """Marca la accion como CONFIRMADA. Solo el usuario dueno puede confirmar
        (HU-01.2 / SEC-01) y solo si sigue PENDIENTE y no expiro.
        """
        expired = False
        with transaction.atomic():
            action = AgentAction.objects.select_for_update().get(pk=self.pk)
            if usuario.pk != action.usuario_id:
                raise PermissionError('Solo el usuario dueno de la accion puede confirmarla.')
            if action.esta_expirada:
                action.estado_previo = action.estado
                action.estado = self.EXPIRADA
                action.save(update_fields=['estado', 'estado_previo'])
                expired = True
            elif action.estado != self.PENDIENTE:
                raise ValueError(
                    f'La accion no esta pendiente (estado actual: {action.estado}).'
                )
            else:
                action.estado_previo = action.estado
                action.estado = self.CONFIRMADA
                action.fecha_confirmacion = timezone.now()
                action.confirmada_por = usuario
                action.save(
                    update_fields=[
                        'estado',
                        'estado_previo',
                        'fecha_confirmacion',
                        'confirmada_por',
                    ]
                )
        self._sync_transition_fields(action)
        if expired:
            raise ValueError('La accion ya expiro y no puede confirmarse.')
        return self

    def rechazar(self, usuario):
        """Marca la accion como RECHAZADA. Mismo control de dueno que confirmar()."""
        with transaction.atomic():
            action = AgentAction.objects.select_for_update().get(pk=self.pk)
            if usuario.pk != action.usuario_id:
                raise PermissionError('Solo el usuario dueno de la accion puede rechazarla.')
            if action.estado != self.PENDIENTE:
                raise ValueError(
                    f'La accion no esta pendiente (estado actual: {action.estado}).'
                )

            action.estado_previo = action.estado
            action.estado = self.RECHAZADA
            action.fecha_confirmacion = timezone.now()
            action.confirmada_por = usuario
            action.save(
                update_fields=[
                    'estado',
                    'estado_previo',
                    'fecha_confirmacion',
                    'confirmada_por',
                ]
            )
        self._sync_transition_fields(action)
        return self

    def _sync_transition_fields(self, action):
        self.estado = action.estado
        self.estado_previo = action.estado_previo
        self.fecha_confirmacion = action.fecha_confirmacion
        self.confirmada_por = action.confirmada_por
