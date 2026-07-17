# Anexo Técnico — Agente de Incidencias

> **Decisión vigente (2026-07-17):** este anexo describe una propuesta anterior.
> Para el P0 conversacional, la fuente de verdad es `incidencias.Incidencia`;
> `alertas.Alerta` queda para alertas, emergencias y anuncios. No implementar los
> cambios a `Alerta` descritos abajo como parte del MVP. Consulte `AGENT_MVP.md`.

Complemento del documento de flujo del agente. Resuelve los 9 huecos detectados
en la revisión técnica (2026-07-13) y documenta las colisiones con el código
existente que hay que evitar. **Leer junto con el documento principal del flujo.**

---

## Cambios al modelo de datos (Dilan — día 1)

### Campos nuevos en `Alerta` (alertas/models.py)

| Campo | Tipo | Para qué |
|---|---|---|
| `categoria` | CharField choices, null | plomeria / electricidad / ascensores / vidrios / limpieza / jardineria / normas / otro |
| `prioridad` | CharField choices, default `media` | critica / alta / media / baja — la asigna el agente |
| `duplicado_de` | FK a `self`, null | agrupa reportes del mismo problema |
| `requiere_atencion_manual` | Boolean, default False | freno del agente cuando agotó la escalera |

- Agregar `('Incidencia', 'Incidencia')` a `TIPOS_ALERTA`. **NO tocar los 6 tipos
  existentes** — la app móvil los manda tal cual y el dashboard web elige íconos
  por tipo (tiene rama else, las incidencias caen ahí sin romper nada).
- Una incidencia del chat = `tipo='Incidencia'` + `categoria='plomeria'` (p. ej.).

### Modelos nuevos

**`EvidenciaIncidencia`**: FK a Alerta, `ImageField(upload_to='evidencias/')`,
`tipo_evidencia` (inicial/avance/final), `subido_por` (null — puede venir de un
link externo), fecha. `MEDIA_ROOT` ya está configurado.
⚠️ Verificar **Pillow** en requirements antes de la migración (ImageField lo exige).
⚠️ En ECS el filesystem es efímero: fotos se pierden al redesplegar. OK para demo;
documentar OSS de Alibaba como roadmap (suma puntos: más servicios Alibaba).

**`Proveedor`**: edificio FK, empresa, persona_responsable, especialidad
(mismas choices que `categoria`), telefono, correo, observaciones, activo.
Solo para **empresas sin cuenta de usuario**. Datos sembrados; alta por Django
admin, SIN CRUD propio.
⚠️ NO confundir con `Empleado(tipo_contrato='EXTERNO')` que ya existe:
Empleado = persona con cuenta (peldaño 1); Proveedor = empresa del directorio
(peldaño 2). No duplicar directorios.

**`OrdenTrabajo`**: alerta FK, responsable_empleado FK null / responsable_proveedor
FK null (uno de los dos), estados propios `asignada → aceptada → en_ejecucion →
terminada` (+ `rechazada`), `responder_antes_de` (datetime, para timeouts),
hora_estimada_inicio, tiempo_estimado, observaciones, token de acceso externo.
**NO tocar la máquina de estados de Alerta** (pendiente/en_proceso/resuelto con
transiciones validadas — ya arreglada y probada).

**`DecisionLog`**: alerta FK null, actor (`agente`/`humano`), usuario FK null,
accion, razonamiento (texto), fecha. Única fuente para: timeline del residente,
dashboard de razonamiento y auditoría. El timeline con autor 🤖/👤 es la prueba
visual de autonomía en el video.

### Usuario de servicio `agente`

Crear un Usuario `agente` (sin login humano, password inusable). Firma
`atendido_por` y `DecisionLog`.
⚠️ **Motivo crítico:** `Alerta.clean()` exige `atendido_por` (FK a Usuario) para
marcar `resuelto`, y el proveedor externo NO tiene cuenta. Sin este usuario, el
agente no puede cerrar ningún caso de proveedor. El responsable real queda
registrado en la OrdenTrabajo.

---

## Reglas de hierro (todo el equipo)

1. **Transiciones atómicas.** Toda transición de estado se hace con
   `UPDATE ... WHERE estado='<estado_anterior>'` (o `select_for_update`). Si el
   tick del agente corre dos veces, solo un proceso gana la transición — el otro
   no afecta filas y se retira. Evita dobles reasignaciones / dobles cierres.
2. **Guardarraíles en las tools, no en el prompt.** Ej.: `buscar_en_internet`
   RECHAZA categorías que no sean de reparación (nunca buscar "empresa para
   vecino ruidoso"). El prompt orienta; la tool es el límite duro.
3. **Una sola vía de procesamiento.** Muchas puertas de entrada (chat, botón de
   Alertas clásico, web), UN solo flujo: el tick del orquestador **adopta** las
   alertas creadas por la vía clásica (las clasifica y las mete al mismo flujo).
   Pitch: "no importa por dónde entre el reporte, el agente lo gestiona".
4. **Permisos en el serializer.** `prioridad`, `estado`, `categoria` NO editables
   por residentes vía API (solo agente/gerencia). Cierra también la deuda anotada:
   filtros de Gerente por `alerta.edificio` (no por relaciones del autor) y el
   PUT genérico restringido a su edificio.

---

## Resolución de los 9 huecos

**H3 Prioridad:** la asigna el agente al clasificar; la gerencia puede corregirla.
Consecuencias reales: define el timeout (abajo) y el orden del dashboard.
Prioridad sin consecuencias = adorno.

**H4 Timeouts:** SIN Celery. `OrdenTrabajo.responder_antes_de` + tick del
orquestador cada N min: órdenes vencidas → el agente decide reasignar (1 vez) o
escalar a gerencia. Plazos sugeridos: crítica 1h, alta 2h, media 8h, baja 24h.

**H5 Callejón:** escalera agotada → `requiere_atencion_manual=True` + notificación
a gerencia con resumen de lo intentado. El flag es el FRENO: sin él, el agente
reintenta la escalera en cada tick (loop infinito de tokens y spam). La asignación
manual desde el dashboard crea OrdenTrabajo con actor 👤 en el mismo DecisionLog.

**H6 Cierre verificado:** responsable termina → alerta `resuelto` (firmada por el
usuario `agente`) → email al REPORTANTE con link tokenizado "¿quedó solucionado?".
Sí → cerrado. No → reabre (`resuelto→en_proceso`, transición ya soportada).
Reglas del link: un solo uso, expira, y si la alerta ya no está en `resuelto`
muestra "el caso cambió" (evita carrera reportante vs gerencia). Máximo
**1 reapertura** por reportante; la segunda va a gerencia.

**H7 Duplicados:** al recibir un reporte, el agente consulta alertas abiertas del
edificio (48h, categoría afín) y decide si es el mismo problema →
`duplicado_de=principal`, SIN segunda OrdenTrabajo. Al resolver el principal,
notificar a TODOS los reportantes (principal + duplicados). El orquestador procesa
EN SERIE por edificio (cola simple) para que el segundo reporte siempre vea al
primero. Candidato a escena del video.

**H8 Rutas por categoría:** reparación → escalera de asignación;
normas/convivencia → directo a gerencia (SIN orden de trabajo, SIN internet);
limpieza/jardinería → personal interno directo. La decisión de ruta es del agente;
el límite lo pone la tool (regla 2).

**H9 Canales:** email (Gmail SMTP ya configurado) + dashboard web (polling con
sonido ya existente). El agente solo emailea lo importante (incidencia crítica/alta,
checkpoint, cierre); el resto vive en el dashboard. Push móvil → roadmap README.

---

## Contrato del chat (app móvil — YA implementado en el cliente)

`POST /api/v1/agente/chat/` body `{mensaje, conversacion_id?}` →
`{respuesta, conversacion_id}`. Futuro: mensajes `tipo:"estado"` para tarjetas
de seguimiento con stepper. La app renderiza; el backend traduce estados internos
a la línea visible (Reportado → En gestión → Asignado → En reparación → Resuelto
→ Cerrado). La app NUNCA conoce los estados internos.

## Visibilidad (resumen)

Reporte = privado (reportante + gerencia del edificio). Impacto general → el
agente publica un `Anuncio` (modelo existente) sin exponer al reportante.
Administrador = plano plataforma (soporte), NO participa del flujo operativo.
Checkpoints: los aprueba el PRIMER gerente que responda (registrar `aprobado_por`,
idempotente). Personal/proveedor externo: solo links tokenizados por email,
nunca la app.
