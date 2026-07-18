from datetime import date, timedelta
import json
import re
import unicodedata
from unittest.mock import Mock, patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from langgraph.checkpoint.memory import InMemorySaver
from rest_framework.test import APIClient

from areas_comunes.models import AreaComun, Reserva
from alertas.models import Alerta
from incidencias.models import Incidencia
from usuarios.models import Rol
from viviendas.models import Edificio, Residente, Vivienda

from .agent.checkpoints import CheckpointRuntime, CheckpointSettings
from .agent.nlu import QwenNLU
from .agent.service import AgentConversationService
from .config import LLMSettings
from .llm import OllamaLocalAdapter, QwenAdapter
from .models import AgentAction
from .observability import (
    ObservabilitySettings,
    SafeTraceRecorder,
    sanitize_trace_data,
)
from .tools import IncidentTools, ReservationTools

Usuario = get_user_model()


class FakeQwenAdapter:
    provider = 'qwen_local'
    model = 'qwen3:8b'

    def __init__(self, response=None):
        self.response = response

    def chat(self, messages):
        human_turns = sum(item['role'] == 'user' for item in messages)
        return {
            'healthy': True,
            'response': self.response or f'Turnos recibidos: {human_turns}',
        }

    def chat_json(self, messages):
        system = messages[0]['content']
        user_message = next(
            item['content'] for item in reversed(messages) if item['role'] == 'user'
        )
        payload = json.loads(user_message)
        if 'TAREA=CLASSIFY_INTENT' in system:
            data = self._classify(payload)
        elif 'TAREA=EXTRACT_RESERVATION' in system:
            data = self._extract_reservation(payload)
        elif 'TAREA=EXTRACT_INCIDENT' in system:
            data = self._extract_incident(payload)
        else:
            raise AssertionError(f'Tarea NLU desconocida: {system[:80]}')
        return {'healthy': True, 'structured_response': data}

    @classmethod
    def _classify(cls, payload):
        text = cls._normalize(payload['latest_message'])
        if any(word in text for word in ('reserv', 'salon', 'parrill')):
            intent = 'reservation'
        elif any(word in text for word in ('fuga', 'problema', 'incidenc', 'mantenimiento')):
            intent = 'incident'
        elif payload.get('current_intent') in {'reservation', 'incident'}:
            intent = payload['current_intent']
        else:
            intent = 'general'
        return {'intent': intent}

    @classmethod
    def _extract_reservation(cls, payload):
        fields = {
            key: payload.get('existing_fields', {}).get(key)
            for key in (
                'area_id', 'date', 'start_time', 'end_time', 'attendees', 'reason'
            )
        }
        text = payload['latest_message']
        normalized = cls._normalize(text)
        for area in payload['authorized_areas']:
            if cls._normalize(area['name']) in normalized:
                fields['area_id'] = area['id']
                break
        area_id = re.search(r'\barea\s*#?\s*(\d+)\b', normalized)
        if area_id:
            fields['area_id'] = int(area_id.group(1))

        current_date = date.fromisoformat(payload['current_date'])
        if 'pasado manana' in normalized:
            fields['date'] = (current_date + timedelta(days=2)).isoformat()
        elif 'manana' in normalized:
            fields['date'] = (current_date + timedelta(days=1)).isoformat()
        else:
            iso_date = re.search(r'\b\d{4}-\d{2}-\d{2}\b', normalized)
            if iso_date:
                fields['date'] = iso_date.group(0)

        times = re.search(
            r'\b(\d{1,2}:\d{2})\s*(?:a|hasta|-)\s*(\d{1,2}:\d{2})\b',
            normalized,
        )
        if times:
            fields['start_time'] = times.group(1)
            fields['end_time'] = times.group(2)
        attendees = re.search(r'\b(?:para\s+)?(\d+)\s+personas?\b', normalized)
        if attendees:
            fields['attendees'] = int(attendees.group(1))
        return fields

    @classmethod
    def _extract_incident(cls, payload):
        existing = payload.get('existing_fields', {})
        fields = {
            key: existing.get(key)
            for key in ('title', 'description', 'location', 'category', 'urgency')
        }
        text = payload['latest_message'].strip()
        normalized = cls._normalize(text)
        if not fields['description'] and not normalized.startswith('ubicacion:'):
            fields['description'] = text
        location = re.search(r'ubicacion\s*:\s*([^.;\n]+)', normalized)
        if location:
            fields['location'] = text[location.start(1):location.end(1)].strip()

        source = cls._normalize(f"{fields.get('description') or ''} {text}")
        if any(word in source for word in ('agua', 'fuga', 'tuber')):
            fields['category'] = 'PLOMERIA'
        elif any(word in source for word in ('electric', 'cable', 'luz')):
            fields['category'] = 'ELECTRICIDAD'
        else:
            fields['category'] = fields['category'] or 'OTRO'
        if any(word in source for word in ('fuga', 'no deja de salir', 'roto')):
            fields['urgency'] = 'ALTA'
        else:
            fields['urgency'] = fields['urgency'] or 'MEDIA'
        explicit_urgency = re.search(
            r'urgencia\s*:\s*(baja|media|alta|critica)', normalized
        )
        if explicit_urgency:
            fields['urgency'] = explicit_urgency.group(1).upper()
        if fields['description'] and not fields['title']:
            fields['title'] = fields['description'].split('.', 1)[0][:150]
        return fields

    @staticmethod
    def _normalize(value):
        normalized = unicodedata.normalize('NFKD', value)
        return ''.join(
            char for char in normalized if not unicodedata.combining(char)
        ).lower()


class LLMSettingsTest(TestCase):
    def test_configuracion_local_tiene_defaults_seguros(self):
        config = LLMSettings.from_env({})

        self.assertEqual(config.provider, 'qwen_local')
        self.assertEqual(config.model, 'qwen3:8b')
        self.assertEqual(config.base_url, 'http://127.0.0.1:11434/v1')
        self.assertEqual(config.temperature, 0)

    def test_cloud_requiere_url_explicita(self):
        with self.assertRaisesMessage(ValueError, 'QWEN_BASE_URL'):
            LLMSettings.from_env({'LLM_PROVIDER': 'qwen_cloud'})

    def test_cloud_requiere_api_key(self):
        with self.assertRaisesMessage(ValueError, 'QWEN_API_KEY'):
            LLMSettings.from_env({
                'LLM_PROVIDER': 'qwen_cloud',
                'QWEN_BASE_URL': 'https://model.example/v1',
            })


class QwenNLUTest(TestCase):
    def test_clasifica_intencion_con_salida_estructurada(self):
        adapter = Mock()
        adapter.chat_json.return_value = {
            'healthy': True,
            'structured_response': {'intent': 'reservation'},
        }

        result = QwenNLU(adapter).classify('Quiero usar el salón mañana')

        self.assertEqual(result['status'], 'success')
        self.assertEqual(result['data'].intent, 'reservation')
        adapter.chat_json.assert_called_once()

    def test_reintenta_una_salida_invalida_y_normaliza_tipos(self):
        adapter = Mock()
        adapter.chat_json.side_effect = [
            {
                'healthy': True,
                'structured_response': {
                    'area_id': 1,
                    'date': '2026-07-25',
                    'start_time': '09:00',
                    'end_time': '10:00',
                    'attendees': 'cinco',
                    'reason': None,
                },
            },
            {
                'healthy': True,
                'structured_response': {
                    'area_id': 1,
                    'date': '2026-07-25',
                    'start_time': '09:00',
                    'end_time': '10:00',
                    'attendees': 5,
                    'reason': None,
                },
            },
        ]

        result = QwenNLU(adapter).extract_reservation(
            message='El 25 de julio de 09:00 a 10:00 para cinco personas',
            existing_fields={},
            authorized_areas=[{'id': 1, 'name': 'Salón de eventos'}],
            current_date=date(2026, 7, 17),
        )

        self.assertEqual(result['status'], 'success')
        self.assertEqual(result['data'].as_state_fields()['attendees'], 5)
        self.assertEqual(adapter.chat_json.call_count, 2)


class ObservabilityTest(TestCase):
    def test_sanitiza_secretos_y_datos_personales(self):
        sanitized = sanitize_trace_data({
            'api_key': 'secreto',
            'message': 'Escribe a persona@example.com',
            'authorization': 'Bearer abc.def',
        })

        self.assertEqual(sanitized['api_key'], '[REDACTED]')
        self.assertNotIn('persona@example.com', sanitized['message'])
        self.assertEqual(sanitized['authorization'], '[REDACTED]')

    def test_tracing_activo_requiere_api_key(self):
        with self.assertRaisesMessage(ValueError, 'LANGSMITH_API_KEY'):
            ObservabilitySettings.from_env({'LANGSMITH_TRACING': 'true'})

    def test_trace_recorder_solo_envia_metadata_en_lista_blanca(self):
        client = Mock()
        settings = ObservabilitySettings(
            tracing=True,
            api_key='test-key',
            endpoint='https://langsmith.example',
            project='test-project',
            environment='test',
        )
        recorder = SafeTraceRecorder(settings=settings, client=client)

        recorder.record({
            'intent': 'reservation',
            'outcome': 'success',
            'llm_invoked': True,
            'guardrail_triggered': False,
            'user_id': 99,
            'message': 'dato privado',
        })

        kwargs = client.create_run.call_args.kwargs
        self.assertEqual(kwargs['inputs'], {})
        self.assertEqual(kwargs['outputs'], {})
        metadata = kwargs['extra']['metadata']
        self.assertEqual(metadata['intent'], 'reservation')
        self.assertTrue(metadata['llm_invoked'])
        self.assertFalse(metadata['guardrail_triggered'])
        self.assertNotIn('user_id', metadata)
        self.assertNotIn('message', metadata)


class CheckpointSettingsTest(TestCase):
    def test_postgres_requiere_url(self):
        with self.assertRaisesMessage(ValueError, 'DATABASE_URL'):
            CheckpointSettings.from_env({'AGENT_CHECKPOINT_BACKEND': 'postgres'})


class AgentConversationServiceTest(TestCase):
    def setUp(self):
        self.user = Usuario.objects.create_user(
            username='agente_residente',
            password='clave123',
        )
        self.runtime = CheckpointRuntime(
            saver=InMemorySaver(),
            backend='memory',
        )

    def test_reanuda_hilo_al_reconstruir_el_grafo(self):
        first_service = AgentConversationService(
            runtime=self.runtime,
            adapter=FakeQwenAdapter(),
        )
        first = first_service.chat(self.user, 'Hola')

        restarted_service = AgentConversationService(
            runtime=self.runtime,
            adapter=FakeQwenAdapter(),
        )
        second = restarted_service.chat(
            self.user,
            '¿Qué dije antes?',
            thread_id=first['thread_id'],
        )

        self.assertEqual(second['message'], 'Turnos recibidos: 2')
        self.assertEqual(second['intent'], 'general')
        self.assertEqual(second['thread_id'], first['thread_id'])
        self.assertTrue(second['trace_metadata']['llm_invoked'])
        self.assertFalse(second['trace_metadata']['guardrail_triggered'])

    def test_hilo_esta_aislado_por_usuario(self):
        other_user = Usuario.objects.create_user(
            username='otro_agente_residente',
            password='clave123',
        )
        service = AgentConversationService(
            runtime=self.runtime,
            adapter=FakeQwenAdapter(),
        )
        first = service.chat(self.user, 'Hola')
        other = service.chat(
            other_user,
            'Hola',
            thread_id=first['thread_id'],
        )

        self.assertEqual(other['message'], 'Turnos recibidos: 1')

    def test_bloquea_afirmacion_llm_de_accion_no_verificada(self):
        service = AgentConversationService(
            runtime=self.runtime,
            adapter=FakeQwenAdapter(
                response=(
                    '¡Gracias por confirmar tu evento! '
                    'Tu reunión está programada para mañana.'
                )
            ),
        )

        result = service.chat(
            self.user,
            'Redacta una confirmación amable para un evento.',
        )

        self.assertNotIn('está programada', result['message'])
        self.assertIn('no existe una acción verificada', result['message'])
        self.assertTrue(result['trace_metadata']['llm_invoked'])
        self.assertTrue(result['trace_metadata']['guardrail_triggered'])


class ReservationToolsTest(TestCase):
    def setUp(self):
        building = Edificio.objects.create(
            nombre='Torre Agente',
            direccion='Calle 1',
            pisos=5,
        )
        apartment = Vivienda.objects.create(
            edificio=building,
            numero='101',
            piso=1,
            metros_cuadrados=80,
        )
        role, _ = Rol.objects.get_or_create(nombre='Residente')
        self.user = Usuario.objects.create_user(
            username='tool_residente',
            password='clave123',
            rol=role,
        )
        self.resident = Residente.objects.create(
            usuario=self.user,
            vivienda=apartment,
        )
        self.area = AreaComun.objects.create(
            nombre='Salón de eventos',
            edificio=building,
            capacidad_maxima=20,
            horario_inicio='08:00',
            horario_fin='20:00',
        )
        self.tomorrow = date.today() + timedelta(days=1)
        self.payload = {
            'area_id': self.area.pk,
            'date': self.tomorrow.isoformat(),
            'start_time': '09:00',
            'end_time': '10:00',
            'attendees': 10,
            'reason': 'Reunión familiar',
        }
        self.action = AgentAction.objects.create(
            usuario=self.user,
            tipo_accion='RESERVA_CREAR',
            payload=self.payload,
            idempotency_key='reservation-tool-test',
            tool_name='create_reservation',
        )
        self.tools = ReservationTools()

    def _conversation_service(self):
        runtime = CheckpointRuntime(
            saver=InMemorySaver(),
            backend='memory',
        )
        return AgentConversationService(
            runtime=runtime,
            adapter=FakeQwenAdapter(),
            reservation_tools=self.tools,
        )

    def test_no_ejecuta_sin_confirmacion(self):
        result = self.tools.create_reservation(self.action.pk, self.user.pk)

        self.assertEqual(result['error_code'], 'confirmation_required')
        self.assertEqual(Reserva.objects.count(), 0)

    def test_creacion_confirmada_es_idempotente_y_verificada(self):
        self.action.confirmar(self.user)

        first = self.tools.create_reservation(self.action.pk, self.user.pk)
        second = self.tools.create_reservation(self.action.pk, self.user.pk)
        verified = self.tools.verify_reservation(self.action.pk, self.user.pk)

        self.assertEqual(first['status'], 'success')
        self.assertEqual(second['reservation_id'], first['reservation_id'])
        self.assertEqual(Reserva.objects.count(), 1)
        self.assertEqual(verified['status'], 'success')
        self.action.refresh_from_db()
        self.assertEqual(self.action.estado, AgentAction.EJECUTADA)
        self.assertEqual(
            self.action.verification_status,
            AgentAction.VERIFICACION_VERIFICADA,
        )

    def test_conflicto_entre_consulta_y_creacion_no_inventa_exito(self):
        context = {
            'building_id': self.area.edificio_id,
            'resident_active': True,
        }
        availability = self.tools.get_availability(context, self.payload)
        self.assertEqual(availability['status'], 'success')
        Reserva.objects.create(
            area_comun=self.area,
            residente=self.resident,
            fecha=self.tomorrow,
            hora_inicio='09:00',
            hora_fin='10:00',
        )
        self.action.confirmar(self.user)

        result = self.tools.create_reservation(self.action.pk, self.user.pk)

        self.assertEqual(result['status'], 'conflict')
        self.assertEqual(result['error_code'], 'slot_unavailable')
        self.action.refresh_from_db()
        self.assertEqual(self.action.estado, AgentAction.CONFIRMADA)

    def test_conversacion_reserva_pausa_confirma_ejecuta(self):
        service = self._conversation_service()
        pending = service.chat(
            self.user,
            (
                'Quiero reservar Salón de eventos mañana de 09:00 a 10:00 '
                'para 10 personas'
            ),
        )

        self.assertEqual(pending['status'], 'awaiting_confirmation')
        self.assertTrue(pending['requires_confirmation'])
        self.assertTrue(pending['trace_metadata']['llm_invoked'])
        self.assertFalse(pending['trace_metadata']['guardrail_triggered'])
        self.assertEqual(Reserva.objects.count(), 0)
        action = AgentAction.objects.get(pk=pending['action_id'])
        self.assertEqual(action.estado, AgentAction.PENDIENTE)

        completed = service.resume_confirmation(self.user, action, approved=True)
        replayed = service.resume_confirmation(self.user, action, approved=True)

        self.assertEqual(completed['status'], 'ok')
        self.assertIn('creada y verificada', completed['message'])
        self.assertEqual(replayed['status'], 'ok')
        self.assertEqual(Reserva.objects.count(), 1)
        action.refresh_from_db()
        self.assertEqual(action.estado, AgentAction.EJECUTADA)
        self.assertEqual(
            action.verification_status,
            AgentAction.VERIFICACION_VERIFICADA,
        )

    def test_conversacion_reserva_recopila_campos_en_varios_turnos(self):
        service = self._conversation_service()
        first = service.chat(self.user, 'Quiero hacer una reserva')

        self.assertEqual(first['status'], 'ok')
        self.assertIn('falta', first['message'])
        pending = service.chat(
            self.user,
            (
                'Salón de eventos mañana 15:00 a 16:00 '
                'para 6 personas'
            ),
            thread_id=first['thread_id'],
        )

        self.assertEqual(pending['status'], 'awaiting_confirmation')

    def test_id_de_area_inventado_por_nlu_no_crea_accion(self):
        service = self._conversation_service()

        result = service.chat(
            self.user,
            'Reserva área 999 mañana 15:00 a 16:00 para 6 personas',
        )

        self.assertEqual(result['status'], 'ok')
        self.assertIn('área', result['message'])
        self.assertTrue(result['trace_metadata']['llm_invoked'])
        self.assertFalse(
            AgentAction.objects.filter(thread_id=result['thread_id']).exists()
        )

    def test_mensaje_no_confirma_una_accion_pendiente(self):
        service = self._conversation_service()
        pending = service.chat(
            self.user,
            (
                'Reserva Salón de eventos mañana 16:00 a 17:00 '
                'para 4 personas'
            ),
        )

        repeated = service.chat(
            self.user,
            'Sí, y cambia a las 18:00',
            thread_id=pending['thread_id'],
        )

        self.assertEqual(repeated['status'], 'awaiting_confirmation')
        self.assertEqual(repeated['action_id'], pending['action_id'])
        self.assertEqual(Reserva.objects.count(), 0)
        self.assertEqual(
            AgentAction.objects.filter(
                thread_id=pending['thread_id'],
                estado='PENDIENTE',
            ).count(),
            1,
        )

    def test_conversacion_reserva_rechazada_no_ejecuta(self):
        service = self._conversation_service()
        pending = service.chat(
            self.user,
            (
                'Reserva Salón de eventos mañana 11:00 a 12:00 '
                'para 5 personas'
            ),
        )
        action = AgentAction.objects.get(pk=pending['action_id'])

        rejected = service.resume_confirmation(self.user, action, approved=False)

        self.assertEqual(rejected['status'], 'ok')
        self.assertIn('rechazada', rejected['message'].lower())
        self.assertEqual(Reserva.objects.count(), 0)
        action.refresh_from_db()
        self.assertEqual(action.estado, AgentAction.RECHAZADA)

    @override_settings(SECURE_SSL_REDIRECT=False)
    def test_api_reserva_end_to_end(self):
        service = self._conversation_service()
        client = APIClient()
        client.force_authenticate(self.user)
        chat_url = reverse('agent-actions-chat')

        with patch('agente.api.get_conversation_service', return_value=service):
            pending = client.post(
                chat_url,
                {
                    'message': (
                        'Reserva Salón de eventos mañana 13:00 a 14:00 '
                        'para 8 personas'
                    )
                },
                format='json',
            )
            confirm_url = reverse(
                'agent-actions-confirmar',
                kwargs={'pk': pending.data['action_id']},
            )
            completed = client.post(confirm_url, format='json')

        self.assertEqual(pending.status_code, 200)
        self.assertEqual(pending.data['status'], 'awaiting_confirmation')
        self.assertEqual(completed.status_code, 200)
        self.assertEqual(completed.data['estado'], AgentAction.EJECUTADA)
        self.assertEqual(completed.data['verification_status'], 'VERIFICADA')
        self.assertEqual(Reserva.objects.count(), 1)


class IncidentToolsTest(TestCase):
    def setUp(self):
        building = Edificio.objects.create(
            nombre='Torre Incidencias',
            direccion='Calle 2',
            pisos=6,
        )
        apartment = Vivienda.objects.create(
            edificio=building,
            numero='202',
            piso=2,
            metros_cuadrados=75,
        )
        role, _ = Rol.objects.get_or_create(nombre='Residente')
        self.user = Usuario.objects.create_user(
            username='incident_resident',
            password='clave123',
            rol=role,
        )
        Residente.objects.create(usuario=self.user, vivienda=apartment)
        self.tools = IncidentTools()
        self.runtime = CheckpointRuntime(
            saver=InMemorySaver(),
            backend='memory',
        )
        self.service = AgentConversationService(
            runtime=self.runtime,
            adapter=FakeQwenAdapter(),
            incident_tools=self.tools,
        )

    def test_estimacion_aplica_politica_a_sugerencia_de_qwen(self):
        estimate = self.tools.build_preliminary_estimate('PLOMERIA', 'ALTA')

        self.assertEqual(estimate['category'], 'PLOMERIA')
        self.assertEqual(estimate['urgency'], 'ALTA')
        self.assertIn('estimaciones preliminares', estimate['disclaimer'])
        self.assertIn('pendiente', estimate['cost_note'].lower())

    def test_incidencia_conversacional_pausa_confirma_y_verifica(self):
        first = self.service.chat(
            self.user,
            'Hay una fuga de agua que no deja de salir',
        )
        self.assertEqual(first['status'], 'ok')
        self.assertIn('ubicación', first['message'])

        pending = self.service.chat(
            self.user,
            'ubicación: pasillo del piso 2',
            thread_id=first['thread_id'],
        )
        self.assertEqual(pending['status'], 'awaiting_confirmation')
        self.assertTrue(pending['trace_metadata']['llm_invoked'])
        self.assertIn('estimaciones preliminares', pending['message'])
        self.assertEqual(Incidencia.objects.count(), 0)
        action = AgentAction.objects.get(pk=pending['action_id'])

        completed = self.service.resume_confirmation(
            self.user,
            action,
            approved=True,
        )

        self.assertEqual(completed['status'], 'ok')
        self.assertIn('Incidencia creada y verificada', completed['message'])
        self.assertEqual(Incidencia.objects.count(), 1)
        self.assertEqual(Alerta.objects.count(), 0)
        incident = Incidencia.objects.get()
        self.assertEqual(incident.categoria, Incidencia.PLOMERIA)
        self.assertEqual(incident.urgencia, Incidencia.URGENCIA_ALTA)
        self.assertEqual(incident.ubicacion, 'pasillo del piso 2')
        self.assertIn('disclaimer', incident.estimacion_preliminar)
        action.refresh_from_db()
        self.assertEqual(action.estado, AgentAction.EJECUTADA)
        self.assertEqual(action.verification_status, 'VERIFICADA')

    def test_incidencia_rechazada_no_escribe_dominio(self):
        pending = self.service.chat(
            self.user,
            (
                'Problema de electricidad en un cable. '
                'ubicación: garaje; urgencia: alta'
            ),
        )
        action = AgentAction.objects.get(pk=pending['action_id'])

        rejected = self.service.resume_confirmation(
            self.user,
            action,
            approved=False,
        )

        self.assertIn('rechazada', rejected['message'].lower())
        self.assertEqual(Incidencia.objects.count(), 0)


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


@override_settings(SECURE_SSL_REDIRECT=False)
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

    def test_health_endpoint_reporta_estado_del_adapter_local(self):
        self.client.force_authenticate(self.dueno)
        url = reverse('agent-actions-health')

        adapter = Mock()
        adapter.health_check.return_value = {
            'healthy': True,
            'provider': 'qwen_local',
            'model': 'qwen3:8b',
            'model_available': True,
            'status': 'ok',
        }
        with patch('agente.api.get_llm_adapter', return_value=adapter):
            response = self.client.get(url)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['provider'], 'qwen_local')
        self.assertTrue(response.data['healthy'])

    def test_adapter_local_revisa_salud_con_endpoint_configurado(self):
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            'data': [{'id': 'qwen3:8b', 'object': 'model'}],
        }

        with patch('agente.llm.requests.get', return_value=mock_response) as mock_get:
            adapter = OllamaLocalAdapter(base_url='http://localhost:8001', api_key='test-key')
            result = adapter.health_check()

        self.assertTrue(result['healthy'])
        self.assertEqual(result['status'], 'ok')
        self.assertTrue(result['model_available'])
        mock_get.assert_called_once_with(
            'http://localhost:8001/models',
            headers={
                'Content-Type': 'application/json',
                'Authorization': 'Bearer test-key',
            },
            timeout=30.0,
        )

    def test_adapter_local_envia_prompts_a_ollama(self):
        mock_post_response = Mock()
        mock_post_response.status_code = 200
        mock_post_response.json.return_value = {
            'choices': [{
                'message': {'role': 'assistant', 'content': 'Hola desde Ollama'},
                'finish_reason': 'stop',
            }],
            'usage': {'total_tokens': 12},
        }

        with patch('agente.llm.requests.post', return_value=mock_post_response) as mock_post:
            adapter = OllamaLocalAdapter(base_url='http://localhost:11434/v1', model='qwen3:8b')
            result = adapter.generate('Hola Ollama')

        call = mock_post.call_args
        self.assertEqual(call.args[0], 'http://localhost:11434/v1/chat/completions')
        self.assertEqual(
            call.kwargs['json']['messages'],
            [{'role': 'user', 'content': 'Hola Ollama'}],
        )
        self.assertEqual(call.kwargs['json']['reasoning_effort'], 'none')
        self.assertEqual(result['provider'], 'qwen_local')
        self.assertEqual(result['response'], 'Hola desde Ollama')
        self.assertTrue(result['healthy'])
        self.assertNotIn('prompt', result)

    def test_adapter_cloud_usa_mismo_contrato_sin_parametro_local(self):
        response = Mock()
        response.json.return_value = {
            'choices': [{
                'message': {'role': 'assistant', 'content': 'Hola desde cloud'},
                'finish_reason': 'stop',
            }],
            'usage': {},
        }
        settings = LLMSettings(
            provider='qwen_cloud',
            model='qwen-plus',
            base_url='https://model.example/v1',
            api_key='cloud-key',
            timeout_seconds=20,
            temperature=0,
            max_tokens=100,
        )

        with patch('agente.llm.requests.post', return_value=response) as mock_post:
            result = QwenAdapter(settings).generate('Hola')

        request_payload = mock_post.call_args.kwargs['json']
        headers = mock_post.call_args.kwargs['headers']
        self.assertNotIn('reasoning_effort', request_payload)
        self.assertEqual(headers['Authorization'], 'Bearer cloud-key')
        self.assertEqual(result['provider'], 'qwen_cloud')

    def test_chat_endpoint_devuelve_respuesta_del_adapter_local(self):
        self.client.force_authenticate(self.dueno)
        url = reverse('agent-actions-chat')

        service = Mock()
        service.chat.return_value = {
            'thread_id': '0f18835f-cbae-4b82-ad6c-2911ae569db4',
            'message': 'Respuesta de prueba',
            'intent': 'general',
            'status': 'ok',
            'error': None,
            'checkpoint_backend': 'memory',
            'durable': False,
            'trace_metadata': {},
        }
        with patch('agente.api.get_conversation_service', return_value=service):
            response = self.client.post(url, {'message': 'Hola'}, format='json')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['message'], 'Respuesta de prueba')
        service.chat.assert_called_once_with(
            user=self.dueno,
            message='Hola',
            thread_id=None,
        )

    def test_chat_endpoint_rechaza_mensaje_vacio(self):
        self.client.force_authenticate(self.dueno)
        url = reverse('agent-actions-chat')

        response = self.client.post(url, {'message': '  '}, format='json')

        self.assertEqual(response.status_code, 400)
        self.assertIn('message', response.data)

    def test_chat_endpoint_reporta_proveedor_no_disponible(self):
        self.client.force_authenticate(self.dueno)
        url = reverse('agent-actions-chat')
        service = Mock()
        service.chat.side_effect = RuntimeError('Postgres no disponible')

        with patch('agente.api.get_conversation_service', return_value=service):
            response = self.client.post(url, {'message': 'Hola'}, format='json')

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.data['error_code'], 'conversation_unavailable')

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
