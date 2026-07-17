from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIClient

from usuarios.models import Rol
from viviendas.models import Edificio, Residente, Vivienda

from .models import EventoIncidencia, Incidencia

Usuario = get_user_model()


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
        self.assertEqual(incidencia.estado, Incidencia.REPORTADA)
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
        self.assertEqual(eventos.count(), 1)
        self.assertEqual(eventos.first().tipo_evento, EventoIncidencia.CREADA)

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
