from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from .models import AgentAction

Usuario = get_user_model()


class AgentActionModelTest(TestCase):
    def setUp(self):
        self.dueno = Usuario.objects.create_user(username='residente1', password='clave123')
        self.otro = Usuario.objects.create_user(username='residente2', password='clave123')
        self.accion = AgentAction.objects.create(
            usuario=self.dueno,
            tipo_accion='RESERVA_CREAR',
            payload={'area_id': 1},
        )

    def test_dueno_puede_confirmar(self):
        self.accion.confirmar(self.dueno)
        self.accion.refresh_from_db()
        self.assertEqual(self.accion.estado, AgentAction.CONFIRMADA)
        self.assertEqual(self.accion.confirmada_por, self.dueno)
        self.assertIsNotNone(self.accion.fecha_confirmacion)

    def test_estado_previo_registra_la_ultima_transicion(self):
        self.assertIsNone(self.accion.estado_previo)

        self.accion.confirmar(self.dueno)
        self.accion.refresh_from_db()
        self.assertEqual(self.accion.estado_previo, AgentAction.PENDIENTE)
        self.assertEqual(self.accion.estado, AgentAction.CONFIRMADA)

    def test_estado_previo_se_registra_al_expirar(self):
        self.accion.expira_en = timezone.now() - timedelta(minutes=1)
        self.accion.save(update_fields=['expira_en'])
        with self.assertRaises(ValueError):
            self.accion.confirmar(self.dueno)
        self.accion.refresh_from_db()
        self.assertEqual(self.accion.estado_previo, AgentAction.PENDIENTE)
        self.assertEqual(self.accion.estado, AgentAction.EXPIRADA)

    def test_otro_usuario_no_puede_confirmar(self):
        with self.assertRaises(PermissionError):
            self.accion.confirmar(self.otro)
        self.accion.refresh_from_db()
        self.assertEqual(self.accion.estado, AgentAction.PENDIENTE)

    def test_dueno_puede_rechazar(self):
        self.accion.rechazar(self.dueno)
        self.accion.refresh_from_db()
        self.assertEqual(self.accion.estado, AgentAction.RECHAZADA)

    def test_no_se_puede_confirmar_dos_veces(self):
        self.accion.confirmar(self.dueno)
        with self.assertRaises(ValueError):
            self.accion.confirmar(self.dueno)

    def test_accion_expirada_no_se_puede_confirmar(self):
        self.accion.expira_en = timezone.now() - timedelta(minutes=1)
        self.accion.save(update_fields=['expira_en'])
        with self.assertRaises(ValueError):
            self.accion.confirmar(self.dueno)
        self.accion.refresh_from_db()
        self.assertEqual(self.accion.estado, AgentAction.EXPIRADA)


class AgentActionApiTest(TestCase):
    def setUp(self):
        self.dueno = Usuario.objects.create_user(username='residente1', password='clave123')
        self.otro = Usuario.objects.create_user(username='residente2', password='clave123')
        self.accion = AgentAction.objects.create(
            usuario=self.dueno,
            tipo_accion='RESERVA_CREAR',
            payload={'area_id': 1},
        )
        self.client = APIClient()

    def test_dueno_puede_confirmar_via_api(self):
        self.client.force_authenticate(self.dueno)
        url = reverse('agent-actions-confirmar', kwargs={'pk': self.accion.pk})
        response = self.client.post(url)
        self.assertEqual(response.status_code, 200)
        self.accion.refresh_from_db()
        self.assertEqual(self.accion.estado, AgentAction.CONFIRMADA)

    def test_otro_usuario_no_ve_la_accion(self):
        self.client.force_authenticate(self.otro)
        url = reverse('agent-actions-confirmar', kwargs={'pk': self.accion.pk})
        response = self.client.post(url)
        self.assertEqual(response.status_code, 404)
        self.accion.refresh_from_db()
        self.assertEqual(self.accion.estado, AgentAction.PENDIENTE)

    def test_anonimo_no_puede_acceder(self):
        url = reverse('agent-actions-confirmar', kwargs={'pk': self.accion.pk})
        response = self.client.post(url)
        self.assertEqual(response.status_code, 401)

    def test_dueno_puede_listar_sus_acciones(self):
        self.client.force_authenticate(self.dueno)
        url = reverse('agent-actions-list')
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.data), 1)

    def test_dueno_puede_rechazar_via_api(self):
        self.client.force_authenticate(self.dueno)
        url = reverse('agent-actions-rechazar', kwargs={'pk': self.accion.pk})
        response = self.client.post(url)
        self.assertEqual(response.status_code, 200)
        self.accion.refresh_from_db()
        self.assertEqual(self.accion.estado, AgentAction.RECHAZADA)
