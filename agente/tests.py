from datetime import date, time, timedelta
import json
import re
from tempfile import TemporaryDirectory
import unicodedata
from unittest.mock import Mock, patch

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from langgraph.checkpoint.memory import InMemorySaver
from rest_framework.test import APIClient

from areas_comunes.models import AreaComun, Reserva
from accesos.models import AperturaPuerta, Puerta, Visita
from alertas.models import Alerta, Anuncio, OpcionVoto
from incidencias.models import Incidencia
from financiero.models import ConceptoCuota, Cuota, EstadoCuenta, Pago, PagoQR
from usuarios.models import Rol
from viviendas.models import Edificio, Residente, Vivienda

from .agent.checkpoints import (
    CheckpointRuntime,
    CheckpointSettings,
    build_checkpoint_runtime,
)
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
from .tools import (
    DoorTools,
    IncidentTools,
    InformationTools,
    ReservationTools,
    VisitorTools,
)

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
        elif 'TAREA=EXTRACT_INFORMATION' in system:
            data = self._extract_information(payload)
        elif 'TAREA=EXTRACT_RESERVATION' in system:
            data = self._extract_reservation(payload)
        elif 'TAREA=EXTRACT_INCIDENT' in system:
            data = self._extract_incident(payload)
        elif 'TAREA=EXTRACT_DOOR' in system:
            data = self._extract_door(payload)
        elif 'TAREA=EXTRACT_VISITOR' in system:
            data = self._extract_visitor(payload)
        else:
            raise AssertionError(f'Tarea NLU desconocida: {system[:80]}')
        return {'healthy': True, 'structured_response': data}

    @classmethod
    def _classify(cls, payload):
        text = cls._normalize(payload['latest_message'])
        if (
            any(word in text for word in ('reportar', 'problema', 'incidenc'))
            and any(word in text for word in ('puerta', 'cerradura'))
        ):
            intent = 'incident'
        elif any(word in text for word in ('abre', 'abrir', 'puerta', 'cerradura')):
            intent = 'lock'
        elif any(word in text for word in ('visitante', 'visita de', 'autoriza a')):
            intent = 'visitor'
        elif any(word in text for word in (
            'cuanto debo', 'cuota', 'deuda', 'pago', 'pagado', 'qr',
            'estado de cuenta',
        )):
            intent = 'finance_info'
        elif any(word in text for word in (
            'que espacios', 'areas comunes', 'area comun', 'disponible',
            'mis reservas', 'visitas agendadas', 'tengo visitas',
            'historial de visitas', 'que puertas', 'puertas puedo',
            'mis accesos', 'historial de accesos', 'anuncios', 'avisos',
            'alertas', 'votacion', 'votaciones', 'reglas',
        )):
            intent = 'residence_info'
        elif any(word in text for word in (
            'mi perfil', 'mis datos', 'mi vivienda', 'mi departamento',
            'que sabes de mi', 'mis incidencias', 'estado de mi incidencia',
        )):
            intent = 'resident_info'
        elif any(word in text for word in ('reserv', 'salon', 'parrill')):
            intent = 'reservation'
        elif any(word in text for word in ('fuga', 'problema', 'incidenc', 'mantenimiento')):
            intent = 'incident'
        elif payload.get('current_intent') in {
            'reservation', 'incident', 'lock', 'visitor', 'residence_info',
            'finance_info', 'resident_info',
        }:
            intent = payload['current_intent']
        else:
            intent = 'general'
        return {'intent': intent}

    @classmethod
    def _extract_information(cls, payload):
        normalized = cls._normalize(payload['latest_message'])
        existing = payload.get('existing_fields', {})
        if 'mis reservas' in normalized:
            topic = 'my_reservations'
        elif 'visitas agendadas' in normalized or 'tengo visitas' in normalized:
            topic = 'scheduled_visits'
        elif 'historial de visitas' in normalized:
            topic = 'visit_history'
        elif 'que puertas' in normalized or 'puertas puedo' in normalized:
            topic = 'allowed_doors'
        elif 'accesos' in normalized:
            topic = 'access_history'
        elif 'que sabes de mi' in normalized:
            topic = 'resident_overview'
        elif 'mis incidencias' in normalized:
            topic = 'my_incidents'
        elif 'estado de mi incidencia' in normalized:
            topic = 'incident_detail'
        elif 'votacion' in normalized:
            topic = 'active_polls'
        elif 'alerta' in normalized:
            topic = 'building_alerts'
        elif any(word in normalized for word in ('anuncio', 'aviso', 'reglas')):
            topic = 'announcements'
        elif 'qr' in normalized:
            topic = 'pending_payment_qrs'
        elif 'estado de cuenta' in normalized:
            topic = 'account_statements'
        elif 'disponib' in normalized or existing.get('topic') == 'area_availability':
            topic = 'area_availability'
        elif 'que espacios' in normalized or 'area' in normalized:
            topic = 'common_areas'
        elif 'cuanto debo' in normalized or 'pendiente' in normalized or 'deuda' in normalized:
            topic = 'pending_fees'
        elif 'cuotas pagadas' in normalized:
            topic = 'paid_fees'
        elif 'mis pagos' in normalized:
            topic = 'my_payments'
        elif 'pago' in normalized:
            topic = 'payment_history'
        elif 'vivienda' in normalized or 'departamento' in normalized:
            topic = 'housing_info'
        elif 'perfil' in normalized or 'datos' in normalized:
            topic = 'profile_info'
        else:
            topic = existing.get('topic', 'profile_info')

        result = {
            'topic': topic,
            'area_id': existing.get('area_id') if existing.get('topic') == topic else None,
            'date': existing.get('date') if existing.get('topic') == topic else None,
            'duration_minutes': (
                existing.get('duration_minutes') if existing.get('topic') == topic else None
            ),
            'record_id': None,
        }
        for area in payload['authorized_areas']:
            if cls._normalize(area['name']) in normalized:
                result['area_id'] = area['id']
                break
        current_date = date.fromisoformat(payload['current_date'])
        if 'pasado manana' in normalized:
            result['date'] = (current_date + timedelta(days=2)).isoformat()
        elif 'manana' in normalized:
            result['date'] = (current_date + timedelta(days=1)).isoformat()
        else:
            iso_date = re.search(r'\b\d{4}-\d{2}-\d{2}\b', normalized)
            if iso_date:
                result['date'] = iso_date.group(0)
        duration = re.search(r'\b(\d+)\s*(?:minutos?|min)\b', normalized)
        if duration:
            result['duration_minutes'] = int(duration.group(1))
        record = re.search(r'(?:incidencia|#)\s*#?(\d+)', normalized)
        if record:
            result['record_id'] = int(record.group(1))
        return result

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

    @classmethod
    def _extract_door(cls, payload):
        existing = payload.get('existing_fields', {})
        fields = {'door_id': existing.get('door_id')}
        normalized = cls._normalize(payload['latest_message'])
        for door in payload['authorized_doors']:
            if cls._normalize(door['name']) in normalized:
                fields['door_id'] = door['id']
                break
        explicit = re.search(r'puerta\s*#?\s*(\d+)', normalized)
        if explicit:
            fields['door_id'] = int(explicit.group(1))
        return fields

    @classmethod
    def _extract_visitor(cls, payload):
        fields = {
            key: payload.get('existing_fields', {}).get(key)
            for key in (
                'name', 'document', 'date', 'start_time', 'end_time',
                'attendees', 'reason',
            )
        }
        text = payload['latest_message']
        normalized = cls._normalize(text)
        name = re.search(r'(?:visita de|autoriza a)\s+(.+?)\s+documento', text, re.I)
        if name:
            fields['name'] = name.group(1).strip()
        document = re.search(r'documento\s+([a-zA-Z0-9-]{6,20})', text, re.I)
        if document:
            fields['document'] = document.group(1)
        current_date = date.fromisoformat(payload['current_date'])
        if 'manana' in normalized:
            fields['date'] = (current_date + timedelta(days=1)).isoformat()
        else:
            iso_date = re.search(r'\b\d{4}-\d{2}-\d{2}\b', text)
            if iso_date:
                fields['date'] = iso_date.group(0)
        times = re.search(
            r'\b(\d{1,2}:\d{2})\s*(?:a|hasta|-)\s*(\d{1,2}:\d{2})\b',
            normalized,
        )
        if times:
            fields['start_time'], fields['end_time'] = times.groups()
        attendees = re.search(r'\b(?:para\s+)?(\d+)\s+personas?\b', normalized)
        if attendees:
            fields['attendees'] = int(attendees.group(1))
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
    def test_sqlite_es_el_backend_durable_por_defecto(self):
        config = CheckpointSettings.from_env({})

        self.assertEqual(config.backend, 'sqlite')
        self.assertTrue(config.sqlite_path.endswith('agent_checkpoints.sqlite3'))

    def test_rechaza_backend_no_soportado(self):
        with self.assertRaisesMessage(ValueError, "'memory' o 'sqlite'"):
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

    def test_sqlite_reanuda_hilo_despues_de_cerrar_la_conexion(self):
        with TemporaryDirectory() as directory:
            settings = CheckpointSettings.from_env({
                'AGENT_CHECKPOINT_BACKEND': 'sqlite',
                'AGENT_CHECKPOINT_SQLITE_PATH': f'{directory}/checkpoints.sqlite3',
            })
            first_runtime = build_checkpoint_runtime(settings)
            first_service = AgentConversationService(
                runtime=first_runtime,
                adapter=FakeQwenAdapter(),
            )
            first = first_service.chat(self.user, 'Hola durable')
            first_runtime.close()

            second_runtime = build_checkpoint_runtime(settings)
            try:
                second_service = AgentConversationService(
                    runtime=second_runtime,
                    adapter=FakeQwenAdapter(),
                )
                second = second_service.chat(
                    self.user,
                    '¿Qué dije antes?',
                    thread_id=first['thread_id'],
                )
            finally:
                second_runtime.close()

        self.assertEqual(second['message'], 'Turnos recibidos: 2')
        self.assertEqual(second['checkpoint_backend'], 'sqlite')
        self.assertTrue(second['durable'])

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


class ResidentInformationAgentTest(TestCase):
    def setUp(self):
        self.building = Edificio.objects.create(
            nombre='Torre Norte', direccion='Av. Principal 100', pisos=10
        )
        self.other_building = Edificio.objects.create(
            nombre='Torre Sur', direccion='Av. Principal 100', pisos=8
        )
        self.housing = Vivienda.objects.create(
            edificio=self.building,
            numero='3A',
            piso=3,
            metros_cuadrados='85.50',
            habitaciones=2,
            baños=2,
        )
        self.user = Usuario.objects.create_user(
            username='residente_info',
            password='clave123',
            first_name='Ana',
            last_name='Pérez',
            email='ana@example.com',
            telefono='70000000',
        )
        self.resident = Residente.objects.create(
            usuario=self.user,
            vivienda=self.housing,
            activo=True,
            es_propietario=True,
            vehiculos=1,
        )
        self.lounge = AreaComun.objects.create(
            nombre='Salón de eventos',
            descripcion='Espacio para reuniones.',
            edificio=self.building,
            capacidad_maxima=40,
            horario_inicio=time(8, 0),
            horario_fin=time(12, 0),
        )
        AreaComun.objects.create(
            nombre='Gimnasio cerrado',
            edificio=self.building,
            activo=False,
        )
        AreaComun.objects.create(
            nombre='Piscina ajena',
            edificio=self.other_building,
        )
        self.runtime = CheckpointRuntime(saver=InMemorySaver(), backend='memory')
        self.service = AgentConversationService(
            runtime=self.runtime,
            adapter=FakeQwenAdapter(),
        )

    def test_lista_solo_areas_activas_del_edificio_autenticado(self):
        response = self.service.chat(
            self.user, '¿Qué espacios hay en la residencia?'
        )

        self.assertEqual(response['status'], 'ok')
        self.assertEqual(response['intent'], 'residence_info')
        self.assertIn('Salón de eventos', response['message'])
        self.assertIn('capacidad para 40 personas', response['message'])
        self.assertNotIn('Gimnasio cerrado', response['message'])
        self.assertNotIn('Piscina ajena', response['message'])
        self.assertFalse(response['requires_confirmation'])
        self.assertFalse(AgentAction.objects.exists())
        self.assertEqual(
            response['trace_metadata']['tool_name'],
            InformationTools.tool_name,
        )
        self.assertEqual(response['presentation']['type'], 'common_area_cards')
        card = response['presentation']['areas'][0]
        self.assertEqual(card['name'], 'Salón de eventos')
        self.assertEqual(card['actions'][0]['type'], 'check_area_availability')

    def test_disponibilidad_pide_fecha_y_reanuda_la_consulta(self):
        first = self.service.chat(self.user, '¿Está disponible el Salón de eventos?')

        self.assertEqual(first['status'], 'ok')
        self.assertIn('fecha', first['message'])
        second = self.service.chat(
            self.user,
            'mañana',
            thread_id=first['thread_id'],
        )

        self.assertEqual(second['status'], 'ok')
        self.assertIn('Salón de eventos tiene estos horarios', second['message'])
        self.assertIn('08:00–09:00', second['message'])
        self.assertEqual(
            second['presentation']['type'], 'availability_options'
        )
        self.assertEqual(
            second['presentation']['dates'][0]['slots'][0]['action']['type'],
            'select_reservation_slot',
        )
        self.assertFalse(AgentAction.objects.exists())

    def test_acciones_de_tarjeta_inician_el_flujo_sin_reinterpretar_texto(self):
        areas = self.service.chat(self.user, '¿Qué espacios hay?')
        check_action, reserve_action = areas['presentation']['areas'][0]['actions']

        check = self.service.chat(
            self.user,
            '',
            thread_id=areas['thread_id'],
            interaction=check_action,
        )

        self.assertEqual(check['intent'], 'residence_info')
        self.assertIn('fecha', check['message'])

        reserve = self.service.chat(
            self.user,
            '',
            thread_id=areas['thread_id'],
            interaction=reserve_action,
        )

        self.assertEqual(reserve['intent'], 'reservation')
        self.assertIn('en qué horario', reserve['message'])
        self.assertIn('Salón de eventos', reserve['message'])
        self.assertNotIn('YYYY-MM-DD', reserve['message'])
        self.assertFalse(AgentAction.objects.exists())

    def test_disponibilidad_transfiere_area_y_fecha_a_una_reserva(self):
        availability = self.service.chat(
            self.user,
            '¿Está disponible el Salón de eventos mañana?',
        )

        reservation = self.service.chat(
            self.user,
            'Quiero reservar de 08:00 a 09:00 para 5 personas',
            thread_id=availability['thread_id'],
        )

        self.assertEqual(reservation['intent'], 'reservation')
        self.assertEqual(reservation['status'], 'awaiting_confirmation')
        action = AgentAction.objects.get(pk=reservation['action_id'])
        self.assertEqual(action.payload['area_id'], self.lounge.pk)
        self.assertEqual(
            action.payload['date'],
            (timezone.localdate() + timedelta(days=1)).isoformat(),
        )

    def test_reanuda_un_flujo_previo_sin_mezclar_campos_de_otro_dominio(self):
        incident = self.service.chat(
            self.user,
            'Hay una fuga de agua debajo del lavaplatos',
        )
        finance = self.service.chat(
            self.user,
            '¿Cuánto debo?',
            thread_id=incident['thread_id'],
        )
        resumed = self.service.chat(
            self.user,
            'Continuemos con la incidencia, ubicación: cocina',
            thread_id=incident['thread_id'],
        )

        self.assertEqual(finance['intent'], 'finance_info')
        self.assertEqual(resumed['intent'], 'incident')
        self.assertEqual(resumed['status'], 'awaiting_confirmation')
        action = AgentAction.objects.get(pk=resumed['action_id'])
        self.assertIn('fuga de agua', action.payload['description'].lower())
        self.assertEqual(action.payload['location'], 'cocina')
        self.assertNotIn('topic', action.payload)

    def test_consulta_deuda_calcula_total_sin_modificar_recargo(self):
        concept = ConceptoCuota.objects.create(
            nombre='Expensas',
            monto_base='100.00',
            porcentaje_recargo='10.00',
        )
        fee = Cuota.objects.create(
            concepto=concept,
            vivienda=self.housing,
            monto='100.00',
            fecha_emision=timezone.localdate() - timedelta(days=60),
            fecha_vencimiento=timezone.localdate() - timedelta(days=30),
        )
        initial_surcharge = fee.recargo

        response = self.service.chat(self.user, '¿Cuánto debo en cuotas pendientes?')

        fee.refresh_from_db()
        self.assertEqual(response['intent'], 'finance_info')
        self.assertIn('deuda pendiente total', response['message'])
        self.assertIn('Expensas', response['message'])
        self.assertEqual(fee.recargo, initial_surcharge)
        self.assertFalse(AgentAction.objects.exists())

    def test_perfil_y_vivienda_provienen_del_residente_autenticado(self):
        profile = self.service.chat(self.user, 'Muéstrame mi perfil')
        housing = self.service.chat(self.user, '¿Cuál es mi vivienda?')

        self.assertIn('Ana Pérez', profile['message'])
        self.assertIn('70000000', profile['message'])
        self.assertIn('3A', housing['message'])
        self.assertIn('Torre Norte', housing['message'])

    def test_historial_distingue_vivienda_de_pagos_del_residente(self):
        other_user = Usuario.objects.create_user(
            username='copropietario', password='clave123'
        )
        other_resident = Residente.objects.create(
            usuario=other_user, vivienda=self.housing, activo=True
        )
        Pago.objects.create(
            vivienda=self.housing,
            residente=self.resident,
            monto='50.00',
            metodo_pago='EFECTIVO',
            registrado_por=self.user,
        )
        Pago.objects.create(
            vivienda=self.housing,
            residente=other_resident,
            monto='75.00',
            metodo_pago='TRANSFERENCIA',
            registrado_por=other_user,
        )
        context = self.service._authenticated_context(self.user)

        housing_history = InformationTools().get_payment_history(context)
        my_history = InformationTools().get_payment_history(
            context, only_resident=True
        )

        self.assertEqual(len(housing_history['payments']), 2)
        self.assertEqual(len(my_history['payments']), 1)
        self.assertEqual(my_history['payments'][0]['amount'], '50.00')

    def test_identificador_de_otra_area_no_supera_el_contexto_autenticado(self):
        foreign_area = AreaComun.objects.get(nombre='Piscina ajena')
        context = self.service._authenticated_context(self.user)

        result = InformationTools().get_area_availability(
            context,
            foreign_area.pk,
            timezone.localdate() + timedelta(days=1),
        )

        self.assertEqual(result['status'], 'error')
        self.assertEqual(result['error_code'], 'area_not_found')

    def test_visitas_agendadas_no_se_confunden_con_reserva_de_area(self):
        Visita.objects.create(
            nombre_visitante='Carlos Rojas',
            documento_visitante='12345678',
            vivienda_destino=self.housing,
            residente_autoriza=self.resident,
            registrado_por=self.user,
            fecha_visita=timezone.localdate() + timedelta(days=1),
            hora_inicio=time(15, 0),
            hora_fin=time(16, 0),
            estado=Visita.RESERVADA,
        )

        response = self.service.chat(self.user, '¿Tengo visitas agendadas?')

        self.assertEqual(response['intent'], 'residence_info')
        self.assertIn('Carlos Rojas', response['message'])
        self.assertIn('******78', response['message'])
        self.assertNotIn('Para consultar la reserva falta', response['message'])
        self.assertFalse(AgentAction.objects.exists())

    def test_visitas_de_otra_vivienda_no_son_visibles(self):
        foreign_housing = Vivienda.objects.create(
            edificio=self.other_building,
            numero='2B', piso=2, metros_cuadrados='70.00', habitaciones=2, baños=1,
        )
        foreign_user = Usuario.objects.create_user(
            username='residente_ajeno', password='clave123'
        )
        foreign_resident = Residente.objects.create(
            usuario=foreign_user, vivienda=foreign_housing, activo=True
        )
        Visita.objects.create(
            nombre_visitante='Visita ajena',
            documento_visitante='87654321',
            vivienda_destino=foreign_housing,
            residente_autoriza=foreign_resident,
            fecha_visita=timezone.localdate() + timedelta(days=1),
            hora_inicio=time(10, 0), hora_fin=time(11, 0), estado=Visita.RESERVADA,
        )

        result = InformationTools().get_visits(
            self.service._authenticated_context(self.user)
        )

        self.assertEqual(result['visits'], [])

    def test_resumen_integral_usa_solo_contexto_autenticado(self):
        response = self.service.chat(self.user, '¿Qué sabes de mí?')

        self.assertEqual(response['intent'], 'resident_info')
        self.assertIn('Ana Pérez', response['message'])
        self.assertIn('vivienda 3A', response['message'])
        self.assertFalse(response['requires_confirmation'])

    def test_anuncios_y_alertas_se_filtran_por_edificio_y_publicacion(self):
        Anuncio.objects.create(
            titulo='Corte de agua', contenido='Mañana a las 09:00',
            categoria='mantenimiento', autor=self.user, edificio=self.building,
        )
        Anuncio.objects.create(
            titulo='Anuncio ajeno', contenido='No visible',
            autor=self.user, edificio=self.other_building,
        )
        Alerta.objects.create(
            tipo='Seguridad', descripcion='Portón en mantenimiento',
            enviado_por=self.user, edificio=self.building,
        )
        Alerta.objects.create(
            tipo='Incidencia', descripcion='Reporte privado',
            enviado_por=self.user, edificio=self.building, vivienda=self.housing,
        )
        context = self.service._authenticated_context(self.user)

        announcements = InformationTools().get_announcements(context)
        alerts = InformationTools().get_building_alerts(context)

        self.assertEqual([item['title'] for item in announcements['announcements']], ['Corte de agua'])
        self.assertEqual([item['description'] for item in alerts['alerts']], ['Portón en mantenimiento'])

    def test_solo_lista_incidencias_del_residente(self):
        own = Incidencia.objects.create(
            residente=self.resident, titulo='Ascensor detenido',
            descripcion='No funciona', categoria=Incidencia.ASCENSOR,
        )
        context = self.service._authenticated_context(self.user)

        result = InformationTools().get_my_incidents(context)
        detail = InformationTools().get_my_incidents(context, own.pk)

        self.assertEqual([item['id'] for item in result['incidents']], [own.pk])
        self.assertEqual(detail['incidents'][0]['description'], 'No funciona')

    def test_qr_y_estado_de_cuenta_no_exponen_imagen_ni_identificador_bancario(self):
        PagoQR.objects.create(
            vivienda=self.housing, residente=self.resident, monto='120.00',
            glosa='Expensas julio', qr_id='secreto-bnb', qr_image='base64-secreto',
            fecha_expiracion=timezone.localdate() + timedelta(days=2),
        )
        statement = EstadoCuenta.objects.create(
            vivienda=self.housing,
            fecha_inicio=timezone.localdate() - timedelta(days=30),
            fecha_fin=timezone.localdate(),
        )
        EstadoCuenta.objects.filter(pk=statement.pk).update(saldo_final='120.00')
        context = self.service._authenticated_context(self.user)

        qrs = InformationTools().get_pending_payment_qrs(context)
        statements = InformationTools().get_account_statements(context)

        self.assertEqual(qrs['qrs'][0]['amount'], '120.00')
        self.assertNotIn('qr_id', qrs['qrs'][0])
        self.assertNotIn('qr_image', qrs['qrs'][0])
        self.assertEqual(statements['statements'][0]['balance'], '120.00')


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
        self.assertIn('Qué espacio deseas reservar', first['message'])
        pending = service.chat(
            self.user,
            (
                'Salón de eventos mañana 15:00 a 16:00 '
                'para 6 personas'
            ),
            thread_id=first['thread_id'],
        )

        self.assertEqual(pending['status'], 'awaiting_confirmation')

    def test_cambio_de_consulta_de_areas_a_reserva_limpia_campos_anteriores(self):
        service = self._conversation_service()
        information = service.chat(self.user, '¿Qué espacios hay en la residencia?')

        pending = service.chat(
            self.user,
            (
                'Puedo reservar Salón de eventos mañana de 11:00 a 14:00 '
                'para 20 personas'
            ),
            thread_id=information['thread_id'],
        )

        self.assertEqual(information['intent'], 'residence_info')
        self.assertEqual(pending['intent'], 'reservation')
        self.assertEqual(pending['status'], 'awaiting_confirmation')
        self.assertNotIn('Extra inputs', pending['message'])
        action = AgentAction.objects.get(pk=pending['action_id'])
        self.assertNotIn('topic', action.payload)

    def test_error_por_parametro_extra_no_expone_mensaje_tecnico(self):
        context = {
            'building_id': self.area.edificio_id,
            'resident_active': True,
        }

        result = self.tools.get_availability(
            context,
            {**self.payload, 'topic': 'common_areas'},
        )

        self.assertEqual(result['error_code'], 'invalid_reservation_parameters')
        self.assertNotIn('Extra inputs are not permitted', result['message'])
        self.assertIn('no corresponden a una reserva', result['message'])

    @override_settings(SECURE_SSL_REDIRECT=False)
    def test_alternativas_se_exponen_y_se_pueden_seleccionar_sin_texto(self):
        Reserva.objects.create(
            area_comun=self.area,
            residente=self.resident,
            fecha=self.tomorrow,
            hora_inicio='10:00',
            hora_fin='15:00',
            cantidad_personas=5,
        )
        service = self._conversation_service()
        conflict = service.chat(
            self.user,
            (
                'Reserva Salón de eventos mañana 10:00 a 15:00 '
                'para 10 personas'
            ),
        )

        self.assertEqual(conflict['status'], 'ok')
        self.assertIn('horario ya está ocupado', conflict['message'])
        self.assertEqual(
            conflict['presentation']['type'], 'availability_options'
        )
        slot = conflict['presentation']['dates'][0]['slots'][0]
        self.assertEqual(slot['action']['type'], 'select_reservation_slot')

        follow_up = service.chat(
            self.user,
            '¿No hay para mañana?',
            thread_id=conflict['thread_id'],
        )

        self.assertIn('Para mañana', follow_up['message'])
        self.assertEqual(
            follow_up['presentation']['type'], 'availability_options'
        )

        client = APIClient()
        client.force_authenticate(self.user)
        chat_url = reverse('agent-actions-chat')
        with patch('agente.api.get_conversation_service', return_value=service):
            selected = client.post(
                chat_url,
                {
                    'thread_id': conflict['thread_id'],
                    'interaction': slot['action'],
                },
                format='json',
            )

        self.assertEqual(selected.status_code, 200)
        self.assertEqual(selected.data['status'], 'awaiting_confirmation')
        action = AgentAction.objects.get(pk=selected.data['action_id'])
        self.assertEqual(action.payload['date'], slot['action']['payload']['date'])
        self.assertEqual(
            action.payload['start_time'], slot['action']['payload']['start_time']
        )

    def test_id_de_area_inventado_por_nlu_no_crea_accion(self):
        service = self._conversation_service()

        result = service.chat(
            self.user,
            'Reserva área 999 mañana 15:00 a 16:00 para 6 personas',
        )

        self.assertEqual(result['status'], 'ok')
        self.assertIn('Qué espacio deseas reservar', result['message'])
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
        self.assertEqual(
            pending['presentation']['type'],
            'incident_initial_evaluation',
        )
        self.assertGreater(pending['presentation']['estimated_hours'], 0)
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
        self.assertIn('creé el reporte #', completed['message'])
        self.assertIn('pendiente de revisión administrativa', completed['message'])
        self.assertIn('cuando se asigne un técnico', completed['message'])
        self.assertNotIn('/api/', completed['message'])
        self.assertNotIn('creada y verificada', completed['message'].lower())
        self.assertEqual(
            completed['presentation']['type'],
            'incident_review_status',
        )
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

    def test_mi_puerta_es_ubicacion_suficiente_para_reportar(self):
        pending = self.service.chat(
            self.user,
            'Quiero reportar un problema con mi puerta. No cierra bien',
        )

        self.assertEqual(pending['intent'], 'incident')
        self.assertEqual(pending['status'], 'awaiting_confirmation')
        self.assertNotIn('falta: ubicación', pending['message'].lower())
        action = AgentAction.objects.get(pk=pending['action_id'])
        self.assertEqual(action.payload['location'], 'puerta de mi vivienda')
        self.assertEqual(action.payload['category'], 'SEGURIDAD')
        self.assertEqual(Incidencia.objects.count(), 0)

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


@override_settings(SECURE_SSL_REDIRECT=False)
class DoorAndVisitorAgentTest(TestCase):
    def setUp(self):
        building = Edificio.objects.create(
            nombre='Torre Procesos', direccion='Calle 4', pisos=4,
        )
        apartment = Vivienda.objects.create(
            edificio=building,
            numero='301',
            piso=3,
            metros_cuadrados=70,
        )
        role, _ = Rol.objects.get_or_create(nombre='Residente')
        self.user = Usuario.objects.create_user(
            username='resident_processes',
            password='clave.segura1',
            rol=role,
        )
        Residente.objects.create(
            usuario=self.user,
            vivienda=apartment,
            activo=True,
        )
        self.door = Puerta.objects.create(
            nombre='Puerta departamento 301',
            tipo=Puerta.TIPO_VIVIENDA,
            vivienda=apartment,
            habilitada_para_demo=True,
        )
        self.runtime = CheckpointRuntime(saver=InMemorySaver(), backend='memory')
        self.service = AgentConversationService(
            runtime=self.runtime,
            adapter=FakeQwenAdapter(),
            door_tools=DoorTools(),
            visitor_tools=VisitorTools(),
        )

    def test_cerradura_conversacional_exige_password_y_verifica(self):
        pending = self.service.chat(
            self.user,
            'Abre la Puerta departamento 301',
        )
        self.assertEqual(pending['status'], 'awaiting_confirmation')
        self.assertEqual(pending['intent'], 'lock')
        self.assertTrue(pending['confirmation']['requires_password'])
        self.assertEqual(pending['confirmation']['expires_in_seconds'], 300)
        action = AgentAction.objects.get(pk=pending['action_id'])
        self.assertNotIn('password', action.payload)

        client = APIClient()
        client.force_authenticate(self.user)
        url = reverse('agent-actions-confirmar', kwargs={'pk': action.pk})
        with patch('agente.api.get_conversation_service', return_value=self.service):
            missing = client.post(url, {}, format='json')
            completed = client.post(
                url,
                {'password': 'clave.segura1'},
                format='json',
            )

        self.assertEqual(missing.status_code, 400)
        self.assertEqual(completed.status_code, 200)
        self.assertTrue(completed.data['resultado']['success'])
        self.assertEqual(AperturaPuerta.objects.count(), 1)
        action.refresh_from_db()
        self.assertEqual(action.estado, AgentAction.EJECUTADA)
        self.assertEqual(action.verification_status, 'VERIFICADA')

    def test_reintento_de_apertura_no_genera_segunda_orden(self):
        pending = self.service.chat(
            self.user,
            'Abre la Puerta departamento 301',
        )
        action = AgentAction.objects.get(pk=pending['action_id'])
        completed = self.service.resume_confirmation(self.user, action, approved=True)
        replayed = self.service.resume_confirmation(self.user, action, approved=True)

        self.assertEqual(completed['status'], 'ok')
        self.assertEqual(replayed['status'], 'ok')
        self.assertEqual(AperturaPuerta.objects.count(), 1)

    def test_visita_conversacional_crea_qr_idempotente_y_verifica(self):
        pending = self.service.chat(
            self.user,
            (
                'Autoriza visita de Ana Pérez documento 1234567 mañana '
                '18:00 a 19:00 para 2 personas'
            ),
        )
        self.assertEqual(pending['status'], 'awaiting_confirmation')
        self.assertEqual(pending['intent'], 'visitor')
        action = AgentAction.objects.get(pk=pending['action_id'])

        completed = self.service.resume_confirmation(self.user, action, approved=True)
        replayed = self.service.resume_confirmation(self.user, action, approved=True)

        self.assertIn('Visita autorizada y verificada', completed['message'])
        self.assertEqual(replayed['status'], 'ok')
        self.assertEqual(Visita.objects.count(), 1)
        visit = Visita.objects.get()
        self.assertEqual(visit.nombre_visitante, 'Ana Pérez')
        self.assertTrue(visit.qr_nonce)
        self.assertEqual(visit.idempotency_key, action.idempotency_key)
        action.refresh_from_db()
        self.assertEqual(action.verification_status, 'VERIFICADA')


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

    def test_chat_endpoint_transcribe_audio_y_conserva_el_hilo(self):
        self.client.force_authenticate(self.dueno)
        url = reverse('agent-actions-chat')
        adapter = Mock()
        adapter.transcribe_audio.return_value = {
            'healthy': True,
            'transcription': 'La puerta de mi vivienda no cierra bien.',
        }
        service = Mock()
        service.chat.return_value = {
            'thread_id': '0f18835f-cbae-4b82-ad6c-2911ae569db4',
            'message': '¿Deseas reportar esta incidencia?',
            'intent': 'incident',
            'status': 'ok',
            'error': None,
            'checkpoint_backend': 'memory',
            'durable': False,
            'trace_metadata': {},
        }
        audio = SimpleUploadedFile(
            'mensaje.wav', b'audio-de-prueba', content_type='audio/wav',
        )
        image = SimpleUploadedFile(
            'puerta.jpg', b'imagen-de-prueba', content_type='image/jpeg',
        )

        with patch('agente.api.get_llm_adapter', return_value=adapter), patch(
            'agente.api.get_conversation_service', return_value=service,
        ):
            response = self.client.post(
                url,
                {
                    'audio': audio,
                    'images': [image],
                    'thread_id': '0f18835f-cbae-4b82-ad6c-2911ae569db4',
                },
                format='multipart',
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.data['transcription'],
            'La puerta de mi vivienda no cierra bien.',
        )
        adapter.transcribe_audio.assert_called_once_with(
            b'audio-de-prueba',
            'wav',
            images=[{
                'data': b'imagen-de-prueba',
                'content_type': 'image/jpeg',
            }],
        )
        service.chat.assert_called_once()
        chat_kwargs = service.chat.call_args.kwargs
        self.assertEqual(chat_kwargs['user'], self.dueno)
        self.assertEqual(
            chat_kwargs['message'], 'La puerta de mi vivienda no cierra bien.',
        )
        self.assertEqual(
            str(chat_kwargs['thread_id']),
            '0f18835f-cbae-4b82-ad6c-2911ae569db4',
        )

    def test_adapter_omni_transcribe_audio_sin_persistirlo(self):
        settings = LLMSettings(
            provider='qwen_cloud',
            model='qwen-plus',
            base_url='https://model.example/v1',
            api_key='cloud-key',
            timeout_seconds=20,
            temperature=0,
            max_tokens=512,
        )
        provider_response = Mock()
        provider_response.iter_lines.return_value = [
            'data: {"choices":[{"delta":{"content":"Hay una fuga "}}]}',
            'data: {"choices":[{"delta":{"content":"en el pasillo."}}]}',
            'data: [DONE]',
        ]

        with patch('agente.llm.requests.post', return_value=provider_response) as post:
            result = QwenAdapter(settings).transcribe_audio(
                b'audio',
                'wav',
                images=[{'data': b'image', 'content_type': 'image/jpeg'}],
            )

        payload = post.call_args.kwargs['json']
        self.assertEqual(payload['model'], 'qwen3.5-omni-plus')
        self.assertEqual(payload['modalities'], ['text'])
        self.assertTrue(payload['stream'])
        self.assertEqual(
            payload['messages'][1]['content'][0]['type'], 'image_url',
        )
        self.assertEqual(payload['messages'][1]['content'][1]['type'], 'input_audio')
        self.assertEqual(result['transcription'], 'Hay una fuga en el pasillo.')

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
