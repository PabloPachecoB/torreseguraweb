from datetime import date, timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from rest_framework.test import APIClient

from usuarios.models import Rol
from viviendas.models import Edificio, Residente, Vivienda

from .models import AreaComun, Reserva

Usuario = get_user_model()


@override_settings(SECURE_SSL_REDIRECT=False)
class DisponibilidadApiTest(TestCase):
    def setUp(self):
        self.edificio = Edificio.objects.create(nombre='Torre Test', direccion='Calle 1', pisos=5)
        self.otro_edificio = Edificio.objects.create(nombre='Torre Otra', direccion='Calle 2', pisos=3)

        self.vivienda = Vivienda.objects.create(
            edificio=self.edificio, numero='101', piso=1, metros_cuadrados=80,
        )

        rol_residente, _ = Rol.objects.get_or_create(nombre='Residente')
        self.usuario = Usuario.objects.create_user(username='carlos_t', password='clave123')
        self.usuario.rol = rol_residente
        self.usuario.save()
        self.residente = Residente.objects.create(usuario=self.usuario, vivienda=self.vivienda)

        self.area = AreaComun.objects.create(
            nombre='Salon de eventos',
            edificio=self.edificio,
            horario_inicio='08:00',
            horario_fin='10:00',
        )

        self.client = APIClient()
        self.client.force_authenticate(self.usuario)
        self.manana = date.today() + timedelta(days=1)

    def _url(self, area_id=None):
        return reverse('api_v1_disponibilidad_area', kwargs={'area_id': area_id or self.area.pk})

    def test_fecha_faltante_devuelve_400_con_campo_faltante(self):
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, 400)
        self.assertIn('fecha', response.data['campos_faltantes'])

    def test_fecha_invalida_devuelve_400(self):
        response = self.client.get(self._url(), {'fecha': 'no-es-una-fecha'})
        self.assertEqual(response.status_code, 400)

    def test_fecha_pasada_devuelve_400(self):
        ayer = date.today() - timedelta(days=1)
        response = self.client.get(self._url(), {'fecha': ayer.isoformat()})
        self.assertEqual(response.status_code, 400)

    def test_area_de_otro_edificio_da_404(self):
        area_otro_edificio = AreaComun.objects.create(
            nombre='Piscina', edificio=self.otro_edificio,
        )
        response = self.client.get(self._url(area_otro_edificio.pk), {'fecha': self.manana.isoformat()})
        self.assertEqual(response.status_code, 404)

    def test_sin_reservas_devuelve_todos_los_slots_del_horario(self):
        response = self.client.get(self._url(), {'fecha': self.manana.isoformat()})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.data['slots_disponibles'],
            [
                {'hora_inicio': '08:00', 'hora_fin': '09:00'},
                {'hora_inicio': '09:00', 'hora_fin': '10:00'},
            ],
        )
        self.assertEqual(response.data['alternativas'], [])

    def test_respeta_reservas_existentes(self):
        Reserva.objects.create(
            area_comun=self.area,
            residente=self.residente,
            fecha=self.manana,
            hora_inicio='08:00',
            hora_fin='09:00',
        )
        response = self.client.get(self._url(), {'fecha': self.manana.isoformat()})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.data['slots_disponibles'],
            [{'hora_inicio': '09:00', 'hora_fin': '10:00'}],
        )

    def test_propone_alternativas_cuando_no_hay_lugar(self):
        Reserva.objects.create(
            area_comun=self.area,
            residente=self.residente,
            fecha=self.manana,
            hora_inicio='08:00',
            hora_fin='10:00',
        )
        response = self.client.get(self._url(), {'fecha': self.manana.isoformat()})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['slots_disponibles'], [])
        self.assertTrue(len(response.data['alternativas']) > 0)
        primera_alterna = response.data['alternativas'][0]
        self.assertEqual(primera_alterna['fecha'], (self.manana + timedelta(days=1)).isoformat())
        self.assertTrue(len(primera_alterna['slots_disponibles']) > 0)

    def test_anonimo_no_puede_consultar(self):
        client = APIClient()
        response = client.get(self._url(), {'fecha': self.manana.isoformat()})
        self.assertEqual(response.status_code, 401)


@override_settings(SECURE_SSL_REDIRECT=False)
class CrearReservaApiTest(TestCase):
    def setUp(self):
        self.edificio = Edificio.objects.create(nombre='Torre Test', direccion='Calle 1', pisos=5)
        self.otro_edificio = Edificio.objects.create(nombre='Torre Otra', direccion='Calle 2', pisos=3)

        self.vivienda = Vivienda.objects.create(
            edificio=self.edificio, numero='101', piso=1, metros_cuadrados=80,
        )

        rol_residente, _ = Rol.objects.get_or_create(nombre='Residente')
        self.usuario = Usuario.objects.create_user(username='carlos_t', password='clave123')
        self.usuario.rol = rol_residente
        self.usuario.save()
        self.residente = Residente.objects.create(usuario=self.usuario, vivienda=self.vivienda)

        self.area_propia = AreaComun.objects.create(
            nombre='Salon de eventos', edificio=self.edificio,
            horario_inicio='08:00', horario_fin='20:00',
        )
        self.area_otro_edificio = AreaComun.objects.create(
            nombre='Piscina', edificio=self.otro_edificio,
            horario_inicio='08:00', horario_fin='20:00',
        )

        self.client = APIClient()
        self.client.force_authenticate(self.usuario)
        self.manana = date.today() + timedelta(days=1)

    def _url(self, area_id):
        return reverse('api_v1_crear_reserva', kwargs={'area_id': area_id})

    def test_puede_reservar_area_de_su_propio_edificio(self):
        response = self.client.post(
            self._url(self.area_propia.pk),
            {'fecha': self.manana.isoformat(), 'hora_inicio': '09:00', 'hora_fin': '10:00'},
            format='json',
        )
        self.assertEqual(response.status_code, 201)

    def test_no_puede_reservar_area_de_otro_edificio(self):
        response = self.client.post(
            self._url(self.area_otro_edificio.pk),
            {'fecha': self.manana.isoformat(), 'hora_inicio': '09:00', 'hora_fin': '10:00'},
            format='json',
        )
        self.assertEqual(response.status_code, 404)
        self.assertEqual(Reserva.objects.filter(area_comun=self.area_otro_edificio).count(), 0)

    def test_idempotency_key_no_duplica_reserva(self):
        payload = {
            'fecha': self.manana.isoformat(),
            'hora_inicio': '09:00',
            'hora_fin': '10:00',
            'cantidad_personas': 5,
        }

        first = self.client.post(
            self._url(self.area_propia.pk),
            payload,
            format='json',
            HTTP_IDEMPOTENCY_KEY='reserva-api-test',
        )
        second = self.client.post(
            self._url(self.area_propia.pk),
            payload,
            format='json',
            HTTP_IDEMPOTENCY_KEY='reserva-api-test',
        )

        self.assertEqual(first.status_code, 201)
        self.assertEqual(second.status_code, 200)
        self.assertTrue(second.data['replayed'])
        self.assertEqual(Reserva.objects.count(), 1)

    def test_idempotency_key_rechaza_parametros_distintos(self):
        payload = {
            'fecha': self.manana.isoformat(),
            'hora_inicio': '11:00',
            'hora_fin': '12:00',
            'cantidad_personas': 5,
        }
        self.client.post(
            self._url(self.area_propia.pk),
            payload,
            format='json',
            HTTP_IDEMPOTENCY_KEY='reserva-api-conflict',
        )
        changed = dict(payload, cantidad_personas=8)

        response = self.client.post(
            self._url(self.area_propia.pk),
            changed,
            format='json',
            HTTP_IDEMPOTENCY_KEY='reserva-api-conflict',
        )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(Reserva.objects.count(), 1)
