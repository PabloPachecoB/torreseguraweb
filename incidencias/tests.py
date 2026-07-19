from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from usuarios.models import Rol
from personal.models import Empleado, Puesto
from viviendas.models import Edificio, Residente, Vivienda

from .models import (
    AprobacionIncidencia,
    EventoIncidencia,
    Incidencia,
    NotificacionIncidencia,
    OrdenTrabajo,
    RevisionIncidencia,
)
from .services import crear_evaluacion_inicial, revision_vigente

Usuario = get_user_model()


@override_settings(SECURE_SSL_REDIRECT=False)
class IncidenciaApiTest(TestCase):
    def setUp(self):
        self.edificio = Edificio.objects.create(nombre='Torre Test', direccion='Calle 1', pisos=5)
        self.vivienda = Vivienda.objects.create(
            edificio=self.edificio, numero='101', piso=1, metros_cuadrados=80,
        )

        rol_residente, _ = Rol.objects.get_or_create(nombre='Residente')
        self.usuario = Usuario.objects.create_user(username='carlos_t', password='clave123')
        self.usuario.rol = rol_residente
        self.usuario.save()
        self.residente = Residente.objects.create(usuario=self.usuario, vivienda=self.vivienda)

        self.otro_usuario = Usuario.objects.create_user(username='maria_t', password='clave123')
        self.otro_usuario.rol = rol_residente
        self.otro_usuario.save()
        self.otro_vivienda = Vivienda.objects.create(
            edificio=self.edificio, numero='102', piso=1, metros_cuadrados=80,
        )
        self.otro_residente = Residente.objects.create(usuario=self.otro_usuario, vivienda=self.otro_vivienda)

        rol_admin, _ = Rol.objects.get_or_create(nombre='Administrador')
        self.admin = Usuario.objects.create_user(username='admin_t', password='clave123')
        self.admin.rol = rol_admin
        self.admin.save()

        rol_personal, _ = Rol.objects.get_or_create(nombre='Personal')
        self.tecnico_user = Usuario.objects.create_user(
            username='tecnico_t', password='clave123', rol=rol_personal,
            first_name='Ana', last_name='Técnica',
        )
        puesto = Puesto.objects.create(nombre='Técnico de mantenimiento')
        self.tecnico = Empleado.objects.create(
            usuario=self.tecnico_user,
            puesto=puesto,
            edificio=self.edificio,
            fecha_contratacion=timezone.localdate(),
        )

        self.client = APIClient()

    def test_residente_crea_incidencia_sin_evidencia(self):
        self.client.force_authenticate(self.usuario)
        response = self.client.post(
            reverse('api_v1_crear_incidencia'),
            {'titulo': 'Fuga de agua', 'descripcion': 'Hay una fuga en el bano', 'categoria': 'PLOMERIA'},
            format='multipart',
        )
        self.assertEqual(response.status_code, 201)
        incidencia = Incidencia.objects.get(pk=response.data['incidencia']['id'])
        self.assertEqual(incidencia.estado, Incidencia.EN_REVISION)
        self.assertEqual(incidencia.residente, self.residente)

    def test_timeline_registra_evento_de_creacion(self):
        self.client.force_authenticate(self.usuario)
        response = self.client.post(
            reverse('api_v1_crear_incidencia'),
            {'titulo': 'Fuga de agua', 'descripcion': 'Hay una fuga en el bano', 'categoria': 'PLOMERIA'},
            format='multipart',
        )
        incidencia_id = response.data['incidencia']['id']
        eventos = EventoIncidencia.objects.filter(incidencia_id=incidencia_id)
        self.assertEqual(eventos.count(), 2)
        self.assertEqual(eventos.first().tipo_evento, EventoIncidencia.CREADA)
        self.assertEqual(eventos.last().estado_nuevo, Incidencia.EN_REVISION)

    def test_crear_incidencia_con_evidencia_adjunta(self):
        self.client.force_authenticate(self.usuario)
        archivo = SimpleUploadedFile('foto.jpg', b'contenido-falso-de-imagen', content_type='image/jpeg')
        response = self.client.post(
            reverse('api_v1_crear_incidencia'),
            {
                'titulo': 'Fuga de agua', 'descripcion': 'Hay una fuga', 'categoria': 'PLOMERIA',
                'evidencias': [archivo],
            },
            format='multipart',
        )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(len(response.data['incidencia']['evidencias']), 1)
        self.assertEqual(response.data['incidencia']['evidencias'][0]['tipo'], 'FOTO')

    def test_faltan_campos_obligatorios(self):
        self.client.force_authenticate(self.usuario)
        response = self.client.post(
            reverse('api_v1_crear_incidencia'), {'categoria': 'PLOMERIA'}, format='multipart',
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn('titulo', response.data['campos_faltantes'])
        self.assertIn('descripcion', response.data['campos_faltantes'])

    def test_no_residente_no_puede_crear(self):
        self.client.force_authenticate(self.admin)
        response = self.client.post(
            reverse('api_v1_crear_incidencia'),
            {'titulo': 'x', 'descripcion': 'y'}, format='multipart',
        )
        self.assertEqual(response.status_code, 403)

    def test_dueno_ve_su_incidencia_en_detalle(self):
        incidencia = Incidencia.objects.create(
            residente=self.residente, titulo='Fuga', descripcion='desc',
        )
        self.client.force_authenticate(self.usuario)
        response = self.client.get(reverse('api_v1_detalle_incidencia', kwargs={'incidencia_id': incidencia.pk}))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.data['eventos']), 1)

    def test_otro_residente_no_ve_incidencia_ajena(self):
        incidencia = Incidencia.objects.create(
            residente=self.residente, titulo='Fuga', descripcion='desc',
        )
        self.client.force_authenticate(self.otro_usuario)
        response = self.client.get(reverse('api_v1_detalle_incidencia', kwargs={'incidencia_id': incidencia.pk}))
        self.assertEqual(response.status_code, 404)

    def test_admin_ve_incidencia_de_cualquier_residente(self):
        incidencia = Incidencia.objects.create(
            residente=self.residente, titulo='Fuga', descripcion='desc',
        )
        self.client.force_authenticate(self.admin)
        response = self.client.get(reverse('api_v1_detalle_incidencia', kwargs={'incidencia_id': incidencia.pk}))
        self.assertEqual(response.status_code, 200)

    def test_descargar_evidencia_protegida(self):
        incidencia = Incidencia.objects.create(
            residente=self.residente, titulo='Fuga', descripcion='desc',
        )
        archivo = SimpleUploadedFile('foto.jpg', b'contenido', content_type='image/jpeg')
        from .models import EvidenciaIncidencia
        evidencia = EvidenciaIncidencia.objects.create(
            incidencia=incidencia, archivo=archivo, tipo=EvidenciaIncidencia.FOTO, subido_por=self.usuario,
        )

        self.client.force_authenticate(self.otro_usuario)
        response = self.client.get(reverse(
            'api_v1_descargar_evidencia',
            kwargs={'incidencia_id': incidencia.pk, 'evidencia_id': evidencia.pk},
        ))
        self.assertEqual(response.status_code, 404)

        self.client.force_authenticate(self.usuario)
        response = self.client.get(reverse(
            'api_v1_descargar_evidencia',
            kwargs={'incidencia_id': incidencia.pk, 'evidencia_id': evidencia.pk},
        ))
        self.assertEqual(response.status_code, 200)

    def test_residente_no_puede_cambiar_estado(self):
        incidencia = Incidencia.objects.create(
            residente=self.residente, titulo='Fuga', descripcion='desc',
        )
        self.client.force_authenticate(self.usuario)
        response = self.client.patch(
            reverse('api_v1_cambiar_estado_incidencia', kwargs={'incidencia_id': incidencia.pk}),
            {'estado': 'EN_REVISION'}, format='json',
        )
        self.assertEqual(response.status_code, 403)

    def test_admin_cambia_estado_y_queda_en_timeline(self):
        incidencia = Incidencia.objects.create(
            residente=self.residente, titulo='Fuga', descripcion='desc',
        )
        self.client.force_authenticate(self.admin)
        response = self.client.patch(
            reverse('api_v1_cambiar_estado_incidencia', kwargs={'incidencia_id': incidencia.pk}),
            {'estado': 'EN_REVISION', 'comentario': 'Se asigno tecnico'}, format='json',
        )
        self.assertEqual(response.status_code, 200)
        incidencia.refresh_from_db()
        self.assertEqual(incidencia.estado, 'EN_REVISION')
        eventos = EventoIncidencia.objects.filter(incidencia=incidencia)
        self.assertEqual(eventos.count(), 2)
        self.assertEqual(eventos.last().tipo_evento, EventoIncidencia.CAMBIO_ESTADO)
        self.assertEqual(eventos.last().estado_anterior, Incidencia.REPORTADA)

    def test_anonimo_no_puede_listar(self):
        response = self.client.get(reverse('api_v1_mis_incidencias'))
        self.assertEqual(response.status_code, 401)

    def test_idempotency_key_no_duplica_incidencia(self):
        self.client.force_authenticate(self.usuario)
        payload = {
            'titulo': 'Fuga repetida',
            'descripcion': 'Hay una fuga en el pasillo',
            'categoria': 'PLOMERIA',
            'ubicacion': 'Pasillo piso 1',
            'urgencia': 'ALTA',
        }

        first = self.client.post(
            reverse('api_v1_crear_incidencia'),
            payload,
            format='multipart',
            HTTP_IDEMPOTENCY_KEY='incident-api-test',
        )
        second = self.client.post(
            reverse('api_v1_crear_incidencia'),
            payload,
            format='multipart',
            HTTP_IDEMPOTENCY_KEY='incident-api-test',
        )

        self.assertEqual(first.status_code, 201)
        self.assertEqual(second.status_code, 200)
        self.assertTrue(second.data['replayed'])
        self.assertEqual(Incidencia.objects.count(), 1)

    def test_residente_agrega_evidencia_despues_de_crear(self):
        incidencia = Incidencia.objects.create(
            residente=self.residente,
            titulo='Fuga',
            descripcion='Fuga en pasillo',
            ubicacion='Pasillo',
        )
        self.client.force_authenticate(self.usuario)
        archivo = SimpleUploadedFile(
            'evidencia.jpg',
            b'contenido-de-prueba',
            content_type='image/jpeg',
        )

        response = self.client.post(
            reverse(
                'api_v1_agregar_evidencia',
                kwargs={'incidencia_id': incidencia.pk},
            ),
            {'evidencias': [archivo]},
            format='multipart',
        )

        self.assertEqual(response.status_code, 201)
        self.assertEqual(len(response.data['evidencias']), 1)

    def test_revision_conjunta_genera_orden_solo_con_todas_las_aprobaciones(self):
        self.client.force_authenticate(self.usuario)
        created = self.client.post(
            reverse('api_v1_crear_incidencia'),
            {
                'titulo': 'Puerta dañada',
                'descripcion': 'La puerta de la vivienda no cierra',
                'categoria': 'SEGURIDAD',
                'ubicacion': 'Puerta de la vivienda',
            },
            format='multipart',
        )
        incident_id = created.data['incidencia']['id']
        incident = Incidencia.objects.get(pk=incident_id)
        initial = incident.revisiones.get(vigente=True)
        self.assertTrue(initial.aprobaciones.filter(
            rol=AprobacionIncidencia.RESIDENTE,
            decision=AprobacionIncidencia.APROBADA,
        ).exists())
        self.assertTrue(NotificacionIncidencia.objects.filter(
            destinatario=self.admin,
            tipo='REVISION_REQUERIDA',
        ).exists())

        self.client.force_authenticate(self.admin)
        adjusted = self.client.post(
            reverse('api_v1_ajustar_revision_incidencia', kwargs={'incidencia_id': incident_id}),
            {
                'empleado_id': self.tecnico.pk,
                'prioridad': 'ALTA',
                'costo_estimado_min': '150.00',
                'costo_estimado_max': '250.00',
                'tiempo_estimado_horas': 24,
                'comentario': 'Revisión administrativa',
            },
            format='json',
        )
        self.assertEqual(adjusted.status_code, 200)
        revision = RevisionIncidencia.objects.get(incidencia=incident, vigente=True)
        self.assertEqual(revision.version, 2)
        self.assertFalse(revision.aprobaciones.exists())
        self.assertTrue(NotificacionIncidencia.objects.filter(
            destinatario=self.tecnico_user,
            tipo='EVALUACION_ACTUALIZADA',
        ).exists())

        self.client.force_authenticate(self.usuario)
        resident_approval = self.client.post(
            reverse('api_v1_aprobar_revision_incidencia', kwargs={'incidencia_id': incident_id}),
            {}, format='json',
        )
        self.assertFalse(resident_approval.data['orden_generada'])
        self.client.force_authenticate(self.admin)
        admin_approval = self.client.post(
            reverse('api_v1_aprobar_revision_incidencia', kwargs={'incidencia_id': incident_id}),
            {}, format='json',
        )
        self.assertFalse(admin_approval.data['orden_generada'])
        self.assertFalse(OrdenTrabajo.objects.filter(incidencia=incident).exists())

        self.client.force_authenticate(self.tecnico_user)
        tech_approval = self.client.post(
            reverse('api_v1_aprobar_revision_incidencia', kwargs={'incidencia_id': incident_id}),
            {}, format='json',
        )
        self.assertTrue(tech_approval.data['orden_generada'])
        order = OrdenTrabajo.objects.get(incidencia=incident)
        self.assertEqual(order.revision_aprobada, revision)
        incident.refresh_from_db()
        self.assertEqual(incident.estado, Incidencia.APROBADA)
        self.assertTrue(NotificacionIncidencia.objects.filter(
            destinatario=self.usuario,
            tipo='ORDEN_APROBADA',
        ).exists())

    def test_ajuste_tecnico_crea_version_y_reinicia_aprobaciones(self):
        self.client.force_authenticate(self.usuario)
        created = self.client.post(
            reverse('api_v1_crear_incidencia'),
            {'titulo': 'Ventana rota', 'descripcion': 'El vidrio está quebrado'},
            format='multipart',
        )
        incident_id = created.data['incidencia']['id']
        self.client.force_authenticate(self.admin)
        self.client.post(
            reverse('api_v1_ajustar_revision_incidencia', kwargs={'incidencia_id': incident_id}),
            {'empleado_id': self.tecnico.pk, 'tiempo_estimado_horas': 48},
            format='json',
        )
        self.client.post(
            reverse('api_v1_aprobar_revision_incidencia', kwargs={'incidencia_id': incident_id}),
            {}, format='json',
        )
        self.client.force_authenticate(self.usuario)
        self.client.post(
            reverse('api_v1_aprobar_revision_incidencia', kwargs={'incidencia_id': incident_id}),
            {}, format='json',
        )

        self.client.force_authenticate(self.tecnico_user)
        adjusted = self.client.post(
            reverse('api_v1_ajustar_revision_incidencia', kwargs={'incidencia_id': incident_id}),
            {'tiempo_estimado_horas': 72, 'comentario': 'Requiere repuesto'},
            format='json',
        )
        self.assertEqual(adjusted.status_code, 200)
        current = RevisionIncidencia.objects.get(
            incidencia_id=incident_id, vigente=True,
        )
        self.assertEqual(current.version, 3)
        self.assertFalse(current.aprobaciones.exists())
        self.assertFalse(OrdenTrabajo.objects.filter(incidencia_id=incident_id).exists())

    def test_dashboard_admin_lista_y_abre_incidencias(self):
        incidencia = Incidencia.objects.create(
            residente=self.residente,
            titulo='Puerta averiada',
            descripcion='La cerradura no funciona',
        )
        crear_evaluacion_inicial(incidencia, self.usuario)
        self.client.force_authenticate(user=None)
        self.client.force_login(self.admin)

        listado = self.client.get(reverse('incidencia-revision-list'))
        detalle = self.client.get(reverse(
            'incidencia-revision-detail', kwargs={'incidencia_id': incidencia.pk},
        ))

        self.assertEqual(listado.status_code, 200)
        self.assertContains(listado, 'Puerta averiada')
        self.assertEqual(detalle.status_code, 200)
        self.assertContains(detalle, 'Evaluación actual')

    def test_dashboard_tecnico_solo_ve_incidencias_asignadas(self):
        asignada = Incidencia.objects.create(
            residente=self.residente,
            titulo='Incidencia asignada',
            descripcion='Descripción',
            empleado_asignado=self.tecnico,
        )
        crear_evaluacion_inicial(asignada, self.usuario)
        no_asignada = Incidencia.objects.create(
            residente=self.residente,
            titulo='Incidencia de otro técnico',
            descripcion='Descripción',
        )
        crear_evaluacion_inicial(no_asignada, self.usuario)
        self.client.force_authenticate(user=None)
        self.client.force_login(self.tecnico_user)

        listado = self.client.get(reverse('incidencia-revision-list'))

        self.assertContains(listado, 'Incidencia asignada')
        self.assertNotContains(listado, 'Incidencia de otro técnico')

    def test_dashboard_ajuste_admin_crea_nueva_version(self):
        incidencia = Incidencia.objects.create(
            residente=self.residente,
            titulo='Ventana rota',
            descripcion='Vidrio quebrado',
        )
        crear_evaluacion_inicial(incidencia, self.usuario)
        self.client.force_authenticate(user=None)
        self.client.force_login(self.admin)

        response = self.client.post(
            reverse('incidencia-revision-ajustar', kwargs={'incidencia_id': incidencia.pk}),
            {
                'categoria': Incidencia.OTRO,
                'prioridad': Incidencia.URGENCIA_ALTA,
                'costo_estimado_min': '180.00',
                'costo_estimado_max': '250.00',
                'moneda': 'BOB',
                'tiempo_estimado_horas': 24,
                'empleado': self.tecnico.pk,
                'comentario': 'Requiere inspección del marco.',
            },
        )

        self.assertRedirects(response, reverse(
            'incidencia-revision-detail', kwargs={'incidencia_id': incidencia.pk},
        ))
        incidencia.refresh_from_db()
        self.assertEqual(incidencia.empleado_asignado, self.tecnico)
        self.assertEqual(revision_vigente(incidencia).version, 2)

    def test_residente_no_puede_entrar_al_dashboard_operativo(self):
        self.client.force_authenticate(user=None)
        self.client.force_login(self.usuario)
        response = self.client.get(reverse('incidencia-revision-list'))
        self.assertEqual(response.status_code, 403)
