from datetime import date, timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIClient

from usuarios.models import Rol
from viviendas.models import Edificio, Residente, Vivienda

from .models import AreaComun, Reserva

Usuario = get_user_model()


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
