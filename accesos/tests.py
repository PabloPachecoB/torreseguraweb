from django.test import TestCase, Client
from django.urls import reverse
from django.contrib.auth import get_user_model
from django.utils import timezone
from rest_framework.test import APIClient
from .models import Visita, MovimientoResidente
from usuarios.models import Rol
from viviendas.models import Edificio, Vivienda, Residente
from datetime import date, timedelta

class VisitaModelTest(TestCase):
    """
    Pruebas para el modelo Visita
    """
    
    def setUp(self):
        # Crear usuario administrador
        self.rol_admin = Rol.objects.create(nombre='Administrador', descripcion='Administrador del sistema')
        
        User = get_user_model()
        self.admin_user = User.objects.create_user(
            username='admin',
            email='admin@example.com',
            password='adminpassword',
            rol=self.rol_admin
        )
        
        # Crear edificio, vivienda y residente
        self.edificio = Edificio.objects.create(
            nombre='Edificio Test',
            direccion='Calle Test 123',
            pisos=10
        )
        
        self.vivienda = Vivienda.objects.create(
            edificio=self.edificio,
            numero='101',
            piso=1,
            metros_cuadrados=80,
            habitaciones=2,
            baños=1
        )
        
        self.usuario_residente = User.objects.create_user(
            username='residente',
            email='residente@example.com',
            password='password',
            first_name='Juan',
            last_name='Pérez'
        )
        
        self.residente = Residente.objects.create(
            usuario=self.usuario_residente,
            vivienda=self.vivienda,
            es_propietario=True
        )
        
        # Crear visita
        self.fecha_entrada = timezone.now()
        self.visita = Visita.objects.create(
            nombre_visitante='Ana Gómez',
            documento_visitante='12345678',
            vivienda_destino=self.vivienda,
            residente_autoriza=self.residente,
            fecha_hora_entrada=self.fecha_entrada,
            motivo='Visita familiar',
            registrado_por=self.admin_user
        )
    
    def test_visita_creation(self):
        """Verificar la creación correcta de una visita"""
        from django.utils import timezone
        
        self.assertEqual(self.visita.nombre_visitante, 'Ana Gómez')
        self.assertEqual(self.visita.documento_visitante, '12345678')
        self.assertEqual(self.visita.vivienda_destino, self.vivienda)
        self.assertEqual(self.visita.residente_autoriza, self.residente)
        
        # En lugar de comparar los datetime objects directamente,
        # comparemos solo hasta los segundos o usemos assertAlmostEqual
        self.assertTrue(abs(self.visita.fecha_hora_entrada - self.fecha_entrada) < timezone.timedelta(seconds=1))
        
        self.assertIsNone(self.visita.fecha_hora_salida)
        self.assertEqual(self.visita.motivo, 'Visita familiar')
        self.assertEqual(self.visita.registrado_por, self.admin_user)
    
    def test_visita_str(self):
        """Verificar que el método __str__ funciona correctamente"""
        expected_str = f"Ana Gómez - {self.vivienda} - {self.fecha_entrada.strftime('%d/%m/%Y %H:%M')}"
        self.assertEqual(str(self.visita), expected_str)

class MovimientoResidenteModelTest(TestCase):
    """
    Pruebas para el modelo MovimientoResidente
    """
    
    def setUp(self):
        # Crear edificio, vivienda y residente
        self.edificio = Edificio.objects.create(
            nombre='Edificio Test',
            direccion='Calle Test 123',
            pisos=10
        )
        
        self.vivienda = Vivienda.objects.create(
            edificio=self.edificio,
            numero='101',
            piso=1,
            metros_cuadrados=80,
            habitaciones=2,
            baños=1
        )
        
        User = get_user_model()
        self.usuario_residente = User.objects.create_user(
            username='residente',
            email='residente@example.com',
            password='password',
            first_name='Juan',
            last_name='Pérez'
        )
        
        self.residente = Residente.objects.create(
            usuario=self.usuario_residente,
            vivienda=self.vivienda,
            es_propietario=True
        )
        
        # Crear movimientos
        self.fecha_entrada = timezone.now()
        self.fecha_salida = self.fecha_entrada + timedelta(hours=2)
        
        # Movimiento de entrada
        self.movimiento_entrada = MovimientoResidente.objects.create(
            residente=self.residente,
            fecha_hora_entrada=self.fecha_entrada,
            fecha_hora_salida=None,
            vehiculo=True,
            placa_vehiculo='ABC123'
        )
        
        # Movimiento de salida
        self.movimiento_salida = MovimientoResidente.objects.create(
            residente=self.residente,
            fecha_hora_entrada=None,
            fecha_hora_salida=self.fecha_salida,
            vehiculo=False,
            placa_vehiculo=''
        )
    
    def test_movimiento_entrada_creation(self):
        """Verificar la creación correcta de un movimiento de entrada"""
        self.assertEqual(self.movimiento_entrada.residente, self.residente)
        self.assertEqual(self.movimiento_entrada.fecha_hora_entrada, self.fecha_entrada)
        self.assertIsNone(self.movimiento_entrada.fecha_hora_salida)
        self.assertTrue(self.movimiento_entrada.vehiculo)
        self.assertEqual(self.movimiento_entrada.placa_vehiculo, 'ABC123')
    
    def test_movimiento_salida_creation(self):
        """Verificar la creación correcta de un movimiento de salida"""
        self.assertEqual(self.movimiento_salida.residente, self.residente)
        self.assertIsNone(self.movimiento_salida.fecha_hora_entrada)
        self.assertEqual(self.movimiento_salida.fecha_hora_salida, self.fecha_salida)
        self.assertFalse(self.movimiento_salida.vehiculo)
        self.assertEqual(self.movimiento_salida.placa_vehiculo, '')
    
    def test_movimiento_str(self):
        """Verificar que el método __str__ funciona correctamente"""
        # Para movimiento de entrada
        expected_str_entrada = f"{self.residente} - Entrada - {self.fecha_entrada.strftime('%d/%m/%Y %H:%M')}"
        self.assertEqual(str(self.movimiento_entrada), expected_str_entrada)
        
        # Para movimiento de salida
        expected_str_salida = f"{self.residente} - Salida - {self.fecha_salida.strftime('%d/%m/%Y %H:%M')}"
        self.assertEqual(str(self.movimiento_salida), expected_str_salida)

class VisitaViewsTest(TestCase):
    """
    Pruebas para las vistas relacionadas con Visitas
    """
    
    def setUp(self):
        # Crear usuario administrador
        self.rol_admin = Rol.objects.create(nombre='Administrador', descripcion='Administrador del sistema')
        
        User = get_user_model()
        self.admin_user = User.objects.create_user(
            username='admin',
            email='admin@example.com',
            password='adminpassword',
            rol=self.rol_admin
        )
        
        # Crear edificio, vivienda y residente
        self.edificio = Edificio.objects.create(
            nombre='Edificio Test',
            direccion='Calle Test 123',
            pisos=10
        )
        
        self.vivienda = Vivienda.objects.create(
            edificio=self.edificio,
            numero='101',
            piso=1,
            metros_cuadrados=80,
            habitaciones=2,
            baños=1
        )
        
        self.usuario_residente = User.objects.create_user(
            username='residente',
            email='residente@example.com',
            password='password',
            first_name='Juan',
            last_name='Pérez'
        )
        
        self.residente = Residente.objects.create(
            usuario=self.usuario_residente,
            vivienda=self.vivienda,
            es_propietario=True
        )
        
        # Crear visitas
        self.fecha_entrada = timezone.now()
        self.visita_activa = Visita.objects.create(
            nombre_visitante='Ana Gómez',
            documento_visitante='12345678',
            vivienda_destino=self.vivienda,
            residente_autoriza=self.residente,
            fecha_hora_entrada=self.fecha_entrada,
            motivo='Visita familiar',
            registrado_por=self.admin_user
        )
        
        self.visita_completa = Visita.objects.create(
            nombre_visitante='Carlos Ruiz',
            documento_visitante='87654321',
            vivienda_destino=self.vivienda,
            residente_autoriza=self.residente,
            fecha_hora_entrada=self.fecha_entrada - timedelta(hours=3),
            fecha_hora_salida=self.fecha_entrada - timedelta(hours=1),
            motivo='Entrega de paquete',
            registrado_por=self.admin_user
        )
        
        # Iniciar sesión
        self.client.login(username='admin', password='adminpassword')
    
    def test_visita_list_view(self):
        """Verificar que la vista de lista de visitas funciona correctamente"""
        response = self.client.get(reverse('visita-list'))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'accesos/visita_list.html')
        
        # Verificar que solo muestra visitas activas
        self.assertEqual(len(response.context['visitas']), 1)
        self.assertEqual(response.context['visitas'][0], self.visita_activa)
        
        # Verificar que cuenta correctamente las visitas históricas
        self.assertEqual(response.context['visitas_historicas'], 1)
    
    def test_visita_create_view(self):
        """Verificar que la vista de creación de visitas funciona correctamente"""
        response = self.client.get(reverse('visita-create'))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'accesos/visita_form.html')
    
    def test_registrar_salida_visita(self):
        """Verificar que el registro de salida funciona correctamente"""
        # La vista solo acepta POST; un GET redirige sin registrar nada
        response = self.client.post(reverse('visita-salida', args=[self.visita_activa.id]))
        self.assertRedirects(response, reverse('visita-list'))
        
        # Verificar que se registró la salida
        self.visita_activa.refresh_from_db()
        self.assertIsNotNone(self.visita_activa.fecha_hora_salida)

class MovimientoResidenteViewsTest(TestCase):
    """
    Pruebas para las vistas relacionadas con Movimientos de Residentes
    """
    
    def setUp(self):
        # Crear usuario administrador
        self.rol_admin = Rol.objects.create(nombre='Administrador', descripcion='Administrador del sistema')
        
        User = get_user_model()
        self.admin_user = User.objects.create_user(
            username='admin',
            email='admin@example.com',
            password='adminpassword',
            rol=self.rol_admin
        )
        
        # Crear edificio, vivienda y residente
        self.edificio = Edificio.objects.create(
            nombre='Edificio Test',
            direccion='Calle Test 123',
            pisos=10
        )
        
        self.vivienda = Vivienda.objects.create(
            edificio=self.edificio,
            numero='101',
            piso=1,
            metros_cuadrados=80,
            habitaciones=2,
            baños=1
        )
        
        self.usuario_residente = User.objects.create_user(
            username='residente',
            email='residente@example.com',
            password='password',
            first_name='Juan',
            last_name='Pérez'
        )
        
        self.residente = Residente.objects.create(
            usuario=self.usuario_residente,
            vivienda=self.vivienda,
            es_propietario=True
        )
        
        # Crear movimiento
        self.fecha = timezone.now()
        self.movimiento = MovimientoResidente.objects.create(
            residente=self.residente,
            fecha_hora_entrada=self.fecha,
            vehiculo=True,
            placa_vehiculo='ABC123'
        )
        
        # Iniciar sesión
        self.client.login(username='admin', password='adminpassword')
    
    def test_movimiento_list_view(self):
        """Verificar que la vista de lista de movimientos funciona correctamente"""
        response = self.client.get(reverse('movimiento-list'))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'accesos/movimiento_list.html')
        
        # Verificar que muestra el movimiento creado
        self.assertEqual(len(response.context['movimientos']), 1)
    
    def test_movimiento_entrada_view(self):
        """Verificar que la vista de registro de entrada funciona correctamente"""
        response = self.client.get(reverse('movimiento-entrada'))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'accesos/movimiento_entrada_form.html')
        
        # Probar el POST
        data = {
            'residente': self.residente.id,
            'vehiculo': False,
            'placa_vehiculo': ''
        }
        
        # Cerrar la entrada activa del setUp: el formulario no permite
        # registrar una entrada si el residente ya está dentro
        self.movimiento.fecha_hora_salida = timezone.now()
        self.movimiento.save()

        response = self.client.post(reverse('movimiento-entrada'), data)
        self.assertRedirects(response, reverse('movimiento-list'))
        
        # Verificar que se creó un nuevo movimiento
        self.assertEqual(MovimientoResidente.objects.count(), 2)
        nuevo_movimiento = MovimientoResidente.objects.latest('id')
        self.assertEqual(nuevo_movimiento.residente, self.residente)
        self.assertIsNotNone(nuevo_movimiento.fecha_hora_entrada)
        self.assertIsNone(nuevo_movimiento.fecha_hora_salida)
    
    def test_movimiento_salida_view(self):
        """Verificar que la vista de registro de salida funciona correctamente"""
        response = self.client.get(reverse('movimiento-salida'))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'accesos/movimiento_salida_form.html')
        
        # Probar el POST
        data = {
            'residente': self.residente.id,
            'vehiculo': True,
            'placa_vehiculo': 'XYZ789'
        }
        
        response = self.client.post(reverse('movimiento-salida'), data)
        self.assertRedirects(response, reverse('movimiento-list'))
        
        # Verificar que se creó un nuevo movimiento
        self.assertEqual(MovimientoResidente.objects.count(), 2)
        nuevo_movimiento = MovimientoResidente.objects.latest('id')
        self.assertEqual(nuevo_movimiento.residente, self.residente)
        self.assertIsNone(nuevo_movimiento.fecha_hora_entrada)
        self.assertIsNotNone(nuevo_movimiento.fecha_hora_salida)
        self.assertTrue(nuevo_movimiento.vehiculo)
        self.assertEqual(nuevo_movimiento.placa_vehiculo, 'XYZ789')


class ReservaVisitaApiTest(TestCase):
    """Reserva de visitas a futuro con cantidad de personas y ventana horaria."""

    def setUp(self):
        self.rol_residente, _ = Rol.objects.get_or_create(nombre='Residente')
        self.rol_vigilante, _ = Rol.objects.get_or_create(nombre='Vigilante')

        User = get_user_model()
        self.usuario_residente = User.objects.create_user(
            username='residente_r', password='clave123', rol=self.rol_residente,
        )
        self.vigilante_user = User.objects.create_user(
            username='vigilante_r', password='clave123', rol=self.rol_vigilante,
        )

        self.edificio = Edificio.objects.create(nombre='Torre Test', direccion='Calle 1', pisos=5)
        self.vivienda = Vivienda.objects.create(
            edificio=self.edificio, numero='201', piso=2, metros_cuadrados=80,
        )
        self.residente = Residente.objects.create(usuario=self.usuario_residente, vivienda=self.vivienda)

        self.client = APIClient()
        self.manana = date.today() + timedelta(days=1)

    def _reservar(self, **overrides):
        data = {
            'nombre_visitante': 'Juan Perez',
            'documento_visitante': '1234567',
            'vivienda_destino_id': self.vivienda.id,
            'cantidad_personas': 3,
            'fecha_visita': self.manana.isoformat(),
            'hora_inicio': '13:00',
            'hora_fin': '15:00',
        }
        data.update(overrides)
        self.client.force_authenticate(self.usuario_residente)
        return self.client.post(reverse('api_v1_crear_visita'), data, format='json')

    def test_reservar_a_futuro_crea_visita_reservada(self):
        response = self._reservar()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['estado'], Visita.RESERVADA)
        visita = Visita.objects.get(pk=response.data['id'])
        self.assertIsNone(visita.fecha_hora_entrada)
        self.assertEqual(visita.cantidad_personas, 3)

    def test_sin_campos_de_reserva_mantiene_comportamiento_inmediato(self):
        response = self._reservar(fecha_visita=None, hora_inicio=None, hora_fin=None)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['estado'], Visita.CONFIRMADA)
        visita = Visita.objects.get(pk=response.data['id'])
        self.assertIsNotNone(visita.fecha_hora_entrada)

    def test_falta_hora_fin_da_400_con_campos_faltantes(self):
        response = self._reservar(hora_fin=None)
        self.assertEqual(response.status_code, 400)
        self.assertIn('hora_fin', response.data['campos_faltantes'])

    def test_cantidad_personas_invalida_da_400(self):
        response = self._reservar(cantidad_personas=0)
        self.assertEqual(response.status_code, 400)

    def _verificar_qr(self, visita):
        self.client.force_authenticate(self.vigilante_user)
        return self.client.post(
            reverse('api_v1_verificar_qr_visita'),
            {'id': visita.id, 'nonce': visita.qr_nonce, 'firma': self._firma(visita)},
            format='json',
        )

    def _firma(self, visita):
        from .qr_firma_utils import generar_firma_qr
        return generar_firma_qr(visita.id, nonce=visita.qr_nonce)

    def test_verificar_dentro_de_ventana_confirma_ingreso(self):
        ahora = timezone.localtime()
        visita = Visita.objects.create(
            nombre_visitante='Juan Perez', documento_visitante='1234567',
            vivienda_destino=self.vivienda, residente_autoriza=self.residente,
            cantidad_personas=4, estado=Visita.RESERVADA,
            fecha_visita=ahora.date(),
            hora_inicio=(ahora - timedelta(minutes=30)).time(),
            hora_fin=(ahora + timedelta(minutes=30)).time(),
        )
        response = self._verificar_qr(visita)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data['valido'])
        self.assertEqual(response.data['cantidad_personas'], 4)
        visita.refresh_from_db()
        self.assertEqual(visita.estado, Visita.CONFIRMADA)
        self.assertIsNotNone(visita.fecha_hora_entrada)

    def test_verificar_antes_de_hora_inicio_da_403(self):
        ahora = timezone.localtime()
        visita = Visita.objects.create(
            nombre_visitante='Juan Perez', documento_visitante='1234567',
            vivienda_destino=self.vivienda, residente_autoriza=self.residente,
            estado=Visita.RESERVADA,
            fecha_visita=ahora.date(),
            hora_inicio=(ahora + timedelta(hours=1)).time(),
            hora_fin=(ahora + timedelta(hours=2)).time(),
        )
        response = self._verificar_qr(visita)
        self.assertEqual(response.status_code, 403)
        self.assertFalse(response.data['valido'])

    def test_verificar_despues_de_hora_fin_da_403_y_expira(self):
        ahora = timezone.localtime()
        visita = Visita.objects.create(
            nombre_visitante='Juan Perez', documento_visitante='1234567',
            vivienda_destino=self.vivienda, residente_autoriza=self.residente,
            estado=Visita.RESERVADA,
            fecha_visita=ahora.date(),
            hora_inicio=(ahora - timedelta(hours=2)).time(),
            hora_fin=(ahora - timedelta(hours=1)).time(),
        )
        response = self._verificar_qr(visita)
        self.assertEqual(response.status_code, 403)
        visita.refresh_from_db()
        self.assertEqual(visita.estado, Visita.EXPIRADA)


from accesos.models import Puerta, AperturaPuerta


class AperturaConfirmacionReforzadaTest(TestCase):
    """HU-04.2: demo controlada + confirmación reforzada (segundo factor)."""

    def setUp(self):
        from django.contrib.auth import get_user_model
        from usuarios.models import Rol
        from viviendas.models import Edificio, Vivienda, Residente

        rol_res, _ = Rol.objects.get_or_create(nombre='Residente')
        User = get_user_model()
        self.user = User.objects.create_user(
            username='res.cerradura', password='clave.segura1', rol=rol_res
        )
        edificio = Edificio.objects.create(nombre='Torre Test HU42', direccion='x', pisos=3)
        vivienda = Vivienda.objects.create(
            edificio=edificio, numero='1-A', piso=1,
            metros_cuadrados=60, habitaciones=2, baños=1,
        )
        Residente.objects.create(usuario=self.user, vivienda=vivienda, activo=True)
        self.puerta = Puerta.objects.create(
            nombre='1-A', tipo=Puerta.TIPO_VIVIENDA, vivienda=vivienda,
        )
        self.client.force_login(self.user)

    def _abrir(self):
        return self.client.post(f'/api/v1/accesos/puertas/{self.puerta.id}/abrir/')

    def test_puerta_sin_demo_bloqueada(self):
        resp = self._abrir()
        self.assertEqual(resp.status_code, 403)
        self.assertIn('demo controlada', resp.json()['mensaje'])

    def test_abrir_crea_accion_pendiente_sin_ejecutar(self):
        from agente.models import AgentAction
        self.puerta.habilitada_para_demo = True
        self.puerta.save()
        resp = self._abrir()
        self.assertEqual(resp.status_code, 202)
        data = resp.json()
        self.assertTrue(data['requiere_confirmacion'])
        accion = AgentAction.objects.get(pk=data['accion_id'])
        self.assertEqual(accion.estado, AgentAction.PENDIENTE)
        self.assertEqual(accion.tipo_accion, 'CERRADURA_ABRIR')
        # NO se abrió nada todavía
        self.assertEqual(AperturaPuerta.objects.count(), 0)

    def test_confirmar_sin_password_rechazado(self):
        self.puerta.habilitada_para_demo = True
        self.puerta.save()
        accion_id = self._abrir().json()['accion_id']
        resp = self.client.post(f'/api/v1/agente/acciones/{accion_id}/confirmar/', content_type='application/json')
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(AperturaPuerta.objects.count(), 0)

    def test_confirmar_password_incorrecta_rechazado(self):
        self.puerta.habilitada_para_demo = True
        self.puerta.save()
        accion_id = self._abrir().json()['accion_id']
        resp = self.client.post(
            f'/api/v1/agente/acciones/{accion_id}/confirmar/',
            {'password': 'incorrecta'}, content_type='application/json',
        )
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(AperturaPuerta.objects.count(), 0)

    def test_confirmar_password_correcta_ejecuta(self):
        from agente.models import AgentAction
        self.puerta.habilitada_para_demo = True
        self.puerta.save()
        accion_id = self._abrir().json()['accion_id']
        resp = self.client.post(
            f'/api/v1/agente/acciones/{accion_id}/confirmar/',
            {'password': 'clave.segura1'}, content_type='application/json',
        )
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()['abierta'])
        accion = AgentAction.objects.get(pk=accion_id)
        self.assertEqual(accion.estado, AgentAction.EJECUTADA)
        self.assertTrue(accion.resultado['abierta'])
        self.assertEqual(AperturaPuerta.objects.filter(exito=True).count(), 1)

    def test_otro_usuario_no_puede_confirmar(self):
        from django.contrib.auth import get_user_model
        self.puerta.habilitada_para_demo = True
        self.puerta.save()
        accion_id = self._abrir().json()['accion_id']
        otro = get_user_model().objects.create_user(username='otro.res', password='clave.segura2')
        self.client.force_login(otro)
        resp = self.client.post(
            f'/api/v1/agente/acciones/{accion_id}/confirmar/',
            {'password': 'clave.segura2'}, content_type='application/json',
        )
        # el queryset del viewset solo expone acciones propias -> 404
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(AperturaPuerta.objects.count(), 0)
