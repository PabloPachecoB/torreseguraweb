# Consultas de información para residentes

El agente distingue consultas de solo lectura de acciones. Las consultas nunca
crean `AgentAction`, no piden confirmación y obtienen sus alcances desde el
usuario autenticado.

## Alcances

- Residente: perfil, pagos propios, reservas, accesos e incidencias propias.
- Vivienda: cuotas, pagos, estados de cuenta, QR y visitas de la vivienda.
- Edificio: áreas comunes, anuncios, alertas públicas y votaciones.
- Condominio: se conserva en el contexto para futuras entidades compartidas;
  actualmente las áreas y comunicaciones están modeladas por edificio.

La matriz ejecutable vive en `agente/policies/resident_visibility.py`.

## Temas disponibles

`resident_overview`, `profile_info`, `housing_info`, `pending_fees`,
`paid_fees`, `payment_history`, `my_payments`, `pending_payment_qrs`,
`account_statements`, `common_areas`, `area_availability`, `my_reservations`,
`scheduled_visits`, `visit_history`, `allowed_doors`, `access_history`,
`my_incidents`, `incident_detail`, `announcements`, `building_alerts` y
`active_polls`.

## Exclusiones

No se exponen credenciales bancarias, imágenes o identificadores QR, URLs de
hardware, accesos de terceros, documentos completos de visitantes, incidencias
ajenas, identidades de votos anónimos, gastos internos ni datos privados del
personal. Los gastos y contactos solo podrán exponerse cuando exista una marca
explícita de publicación en el modelo de dominio.

## Ejemplos

- `¿Qué sabes de mí?`
- `¿Tengo visitas agendadas?`
- `¿Cuánto debo?`
- `¿Cómo están mis incidencias?`
- `¿Qué avisos hay?`
- `¿Qué puertas puedo usar?`

Autorizar una visita, reservar un área, votar, reportar una incidencia o abrir
una puerta continúan siendo acciones separadas y conservan sus confirmaciones.
