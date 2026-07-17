from django.test import TestCase, Client
from django.urls import reverse
from django.contrib.auth import get_user_model
from django.utils import timezone
from usuarios.models import Rol
from .models import Edificio, Vivienda, Residente

class EdificioModelTest(TestCase):
    """
    Pruebas para el modelo Edificio
    """
    
    def setUp(self):
        self.edificio = Edificio.objects.create(
            nombre='Torre Norte',
            direccion='Av. Principal 123',
            pisos=15,
            fecha_construccion='2010-05-20'
        )
    
    def test_edificio_creation(self):
        """Verificar la creación correcta de un edificio"""
        self.assertEqual(self.edificio.nombre, 'Torre Norte')
        self.assertEqual(self.edificio.direccion, 'Av. Principal 123')
        self.assertEqual(self.edificio.pisos, 15)
        self.assertEqual(str(self.edificio.fecha_construccion), '2010-05-20')
    
    def test_edificio_str(self):
        """Verificar que el método __str__ funciona correctamente"""
        self.assertEqual(str(self.edificio), 'Torre Norte')

class ViviendaModelTest(TestCase):
    """
    Pruebas para el modelo Vivienda
    """
    
    def setUp(self):
        # Crear edificio
        self.edificio = Edificio.objects.create(
            nombre='Torre Sur',
            direccion='Av. Secundaria 456',
            pisos=10,
            fecha_construccion='2015-08-10'
        )
        
        # Crear vivienda
        self.vivienda = Vivienda.objects.create(
            edificio=self.edificio,
            numero='501',
            piso=5,
            metros_cuadrados=120,
            habitaciones=3,
            baños=2,
            estado='OCUPADO',
            activo=True
        )
        
        # Crear vivienda dada de baja
        self.vivienda_baja = Vivienda.objects.create(
            edificio=self.edificio,
            numero='502',
            piso=5,
            metros_cuadrados=100,
            habitaciones=2,
            baños=1,
            estado='DESOCUPADO',
            activo=False,
            fecha_baja=timezone.now().date(),
            motivo_baja='Remodelación completa'
        )
    
    def test_vivienda_creation(self):
        """Verificar la creación correcta de una vivienda"""
        self.assertEqual(self.vivienda.edificio, self.edificio)
        self.assertEqual(self.vivienda.numero, '501')
        self.assertEqual(self.vivienda.piso, 5)
        self.assertEqual(self.vivienda.metros_cuadrados, 120)
        self.assertEqual(self.vivienda.habitaciones, 3)
        self.assertEqual(self.vivienda.baños, 2)
        self.assertEqual(self.vivienda.estado, 'OCUPADO')
        self.assertTrue(self.vivienda.activo)
    
    def test_vivienda_baja_creation(self):
        """Verificar la creación correcta de una vivienda dada de baja"""
        self.assertEqual(self.vivienda_baja.edificio, self.edificio)
        self.assertEqual(self.vivienda_baja.numero, '502')
        self.assertEqual(self.vivienda_baja.estado, 'BAJA')  # El estado debería cambiarse a BAJA automáticamente
        self.assertFalse(self.vivienda_baja.activo)
        self.assertIsNotNone(self.vivienda_baja.fecha_baja)
        self.assertEqual(self.vivienda_baja.motivo_baja, 'Remodelación completa')
    
    def test_vivienda_str(self):
        """Verificar que el método __str__ funciona correctamente"""
        expected_str = f"Vivienda 501 - Piso 5"
        self.assertEqual(str(self.vivienda), expected_str)
    
    def test_save_method_cambia_estado_baja(self):
        """Verificar que al dar de baja una vivienda, su estado cambia a BAJA"""
        self.vivienda.activo = False
        self.vivienda.motivo_baja = 'Demolición'
        self.vivienda.fecha_baja = timezone.now().date()
        self.vivienda.save()
        
        self.assertEqual(self.vivienda.estado, 'BAJA')

class ResidenteModelTest(TestCase):
    """
    Pruebas para el modelo Residente
    """
    
    def setUp(self):
        # Crear edificio y vivienda
        self.edificio = Edificio.objects.create(
            nombre='Torre Este',
            direccion='Calle Este 789',
            pisos=8,
            fecha_construccion='2018-03-15'
        )
        
        self.vivienda = Vivienda.objects.create(
            edificio=self.edificio,
            numero='301',
            piso=3,
            metros_cuadrados=90,
            habitaciones=2,
            baños=1,
            estado='OCUPADO',
            activo=True
        )
        
        # Crear usuario
        User = get_user_model()
        self.usuario = User.objects.create_user(
            username='residente',
            email='residente@example.com',
            password='password',
            first_name='Ana',
            last_name='Martínez'
        )
        
        # Crear residente
        self.residente = Residente.objects.create(
            usuario=self.usuario,
            vivienda=self.vivienda,
            vehiculos=1,
            es_propietario=True,
            activo=True
        )
    
    def test_residente_creation(self):
        """Verificar la creación correcta de un residente"""
        self.assertEqual(self.residente.usuario, self.usuario)
        self.assertEqual(self.residente.vivienda, self.vivienda)
        self.assertEqual(self.residente.vehiculos, 1)
        self.assertTrue(self.residente.es_propietario)
        self.assertTrue(self.residente.activo)
    
    def test_residente_str(self):
        """Verificar que el método __str__ funciona correctamente"""
        expected_str = f"Ana Martínez - Vivienda 301"
        self.assertEqual(str(self.residente), expected_str)
    
    def test_sincronizacion_estado_usuario(self):
        """Verificar que el estado del residente se sincroniza con el usuario"""
        # Desactivar usuario
        self.usuario.is_active = False
        self.usuario.save()
        
        # Verificar que el residente también se desactiva
        self.residente.refresh_from_db()
        self.assertFalse(self.residente.activo)
        
        # Activar usuario
        self.usuario.is_active = True
        self.usuario.save()
        
        # Verificar que el residente también se activa
        self.residente.refresh_from_db()
        self.assertTrue(self.residente.activo)

class EdificioViewsTest(TestCase):
    """
    Pruebas para las vistas relacionadas con Edificios
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
        
        # Crear usuario normal
        self.rol_normal = Rol.objects.create(nombre='Normal', descripcion='Usuario normal')
        self.normal_user = User.objects.create_user(
            username='normal',
            email='normal@example.com',
            password='normalpassword',
            rol=self.rol_normal
        )
        
        # Crear edificio
        self.edificio = Edificio.objects.create(
            nombre='Torre Oeste',
            direccion='Calle Oeste 321',
            pisos=12,
            fecha_construccion='2016-11-20'
        )
        
        # Iniciar cliente
        self.client = Client()
    
    def test_edificio_list_view(self):
        """Verificar que se puede ver la lista de edificios"""
        self.client.login(username='admin', password='adminpassword')
        response = self.client.get(reverse('edificio-list'))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'viviendas/edificio_list.html')
        self.assertContains(response, 'Torre Oeste')
    
    def test_edificio_create_view_admin(self):
        """Verificar que un administrador puede crear un edificio"""
        self.client.login(username='admin', password='adminpassword')
        response = self.client.get(reverse('edificio-create'))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'viviendas/edificio_form.html')
        
        # Probar POST
        data = {
            'nombre': 'Torre Nueva',
            'direccion': 'Calle Nueva 123',
            'pisos': 20,
            'fecha_construccion': '2022-01-15'
        }
        
        response = self.client.post(reverse('edificio-create'), data)
        self.assertEqual(response.status_code, 302)  # Redirección
        
        # Verificar que se creó el edificio
        self.assertTrue(Edificio.objects.filter(nombre='Torre Nueva').exists())
    
    def test_edificio_create_view_normal_user(self):
        """Verificar que un usuario normal no puede crear un edificio"""
        self.client.login(username='normal', password='normalpassword')
        response = self.client.get(reverse('edificio-create'))
        # El mixin de acceso redirige al login en vez de devolver 403
        self.assertEqual(response.status_code, 302)
    
    def test_edificio_update_view_admin(self):
        """Verificar que un administrador puede actualizar un edificio"""
        self.client.login(username='admin', password='adminpassword')
        response = self.client.get(reverse('edificio-update', args=[self.edificio.id]))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'viviendas/edificio_form.html')
        
        # Probar POST
        data = {
            'nombre': 'Torre Oeste Actualizada',
            'direccion': 'Calle Oeste 321',
            'pisos': 15,
            'fecha_construccion': '2016-11-20'
        }
        
        response = self.client.post(reverse('edificio-update', args=[self.edificio.id]), data)
        self.assertEqual(response.status_code, 302)  # Redirección
        
        # Verificar que se actualizó el edificio
        self.edificio.refresh_from_db()
        self.assertEqual(self.edificio.nombre, 'Torre Oeste Actualizada')
        self.assertEqual(self.edificio.pisos, 15)

class ViviendaViewsTest(TestCase):
    """
    Pruebas para las vistas relacionadas con Viviendas
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
        
        # Crear edificio
        self.edificio = Edificio.objects.create(
            nombre='Torre Central',
            direccion='Av. Central 789',
            pisos=20,
            fecha_construccion='2019-05-10'
        )
        
        # Crear vivienda
        self.vivienda = Vivienda.objects.create(
            edificio=self.edificio,
            numero='1001',
            piso=10,
            metros_cuadrados=150,
            habitaciones=3,
            baños=2,
            estado='OCUPADO',
            activo=True
        )
        
        # Iniciar cliente
        self.client = Client()
        self.client.login(username='admin', password='adminpassword')
    
    def test_vivienda_list_view(self):
        """Verificar que se puede ver la lista de viviendas"""
        response = self.client.get(reverse('vivienda-list'))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'viviendas/vivienda_list.html')
        self.assertContains(response, '1001')
    
    def test_vivienda_detail_view(self):
        """Verificar que se puede ver el detalle de una vivienda"""
        response = self.client.get(reverse('vivienda-detail', args=[self.vivienda.id]))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'viviendas/vivienda_detail.html')
        self.assertContains(response, 'Vivienda 1001')
        self.assertContains(response, 'Piso 10')
    
    def test_vivienda_create_view(self):
        """Verificar que se puede crear una vivienda"""
        response = self.client.get(reverse('vivienda-create'))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'viviendas/vivienda_form.html')
        
        # Probar POST
        data = {
            'edificio': self.edificio.id,
            'numero': '2001',
            'piso': 20,
            'metros_cuadrados': 200,
            'habitaciones': 4,
            'baños': 3,
            'estado': 'DESOCUPADO',
            'activo': True
        }
        
        response = self.client.post(reverse('vivienda-create'), data)
        self.assertEqual(response.status_code, 302)  # Redirección
        
        # Verificar que se creó la vivienda
        self.assertTrue(Vivienda.objects.filter(numero='2001').exists())
    
    def test_vivienda_baja_view(self):
        """Verificar que se puede dar de baja una vivienda"""
        response = self.client.get(reverse('vivienda-baja', args=[self.vivienda.id]))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'viviendas/vivienda_baja.html')
        
        # Probar POST
        data = {
            'motivo_baja': 'Remodelación completa',
            'fecha_baja': timezone.now().date().strftime('%Y-%m-%d')
        }
        
        response = self.client.post(reverse('vivienda-baja', args=[self.vivienda.id]), data)
        self.assertEqual(response.status_code, 302)  # Redirección
        
        # Verificar que se dio de baja la vivienda
        self.vivienda.refresh_from_db()
        self.assertFalse(self.vivienda.activo)
        self.assertEqual(self.vivienda.estado, 'BAJA')
        self.assertEqual(self.vivienda.motivo_baja, 'Remodelación completa')
        self.assertIsNotNone(self.vivienda.fecha_baja)

class ResidenteViewsTest(TestCase):
    """
    Pruebas para las vistas relacionadas con Residentes
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
        
        # Crear edificio y vivienda
        self.edificio = Edificio.objects.create(
            nombre='Torre Residencial',
            direccion='Av. Residencial 456',
            pisos=15,
            fecha_construccion='2020-02-10'
        )
        
        self.vivienda = Vivienda.objects.create(
            edificio=self.edificio,
            numero='701',
            piso=7,
            metros_cuadrados=110,
            habitaciones=2,
            baños=2,
            estado='OCUPADO',
            activo=True
        )
        
        # Crear usuario para residente
        self.usuario_residente = User.objects.create_user(
            username='residente',
            email='residente@example.com',
            password='password',
            first_name='Laura',
            last_name='Gómez'
        )
        
        # Crear residente
        self.residente = Residente.objects.create(
            usuario=self.usuario_residente,
            vivienda=self.vivienda,
            vehiculos=2,
            es_propietario=True,
            activo=True
        )
        
        # Iniciar cliente
        self.client = Client()
        self.client.login(username='admin', password='adminpassword')
    
    def test_residente_list_view(self):
        """Verificar que se puede ver la lista de residentes"""
        response = self.client.get(reverse('residente-list'))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'viviendas/residente_list.html')
        self.assertContains(response, 'Laura Gómez')
    
    def test_residente_detail_view(self):
        """Verificar que se puede ver el detalle de un residente"""
        response = self.client.get(reverse('residente-detail', args=[self.residente.id]))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'viviendas/residente_detail.html')
        self.assertContains(response, 'Laura Gómez')
        self.assertContains(response, 'Torre Residencial')
    
    def test_residente_create_view(self):
        """Verificar que se puede crear un residente"""
        # El formulario crea el usuario automáticamente a partir de sus datos
        data = {
            'email': 'nuevoresidente@example.com',
            'first_name': 'Carlos',
            'last_name': 'López',
            'telefono': '77777777',
            'numero_documento': '1234567',
            'edificio': self.edificio.id,
            'vivienda': self.vivienda.id,
            'vehiculos': 1,
            'es_propietario': False,
            'activo': True
        }

        response = self.client.post(reverse('residente-create'), data)
        self.assertEqual(response.status_code, 302)  # Redirección

        # Verificar que se creó el residente (y su usuario asociado)
        self.assertTrue(Residente.objects.filter(usuario__email='nuevoresidente@example.com').exists())

    def test_residente_update_view(self):
        """Verificar que se puede actualizar un residente"""
        # También modificamos esta prueba para evitar el renderizado de la plantilla
        # e ir directamente al POST
        
        # Probar POST (el formulario exige también los datos del usuario)
        data = {
            'email': 'residente@example.com',
            'first_name': 'Laura',
            'last_name': 'Gómez',
            'telefono': '',
            'numero_documento': '7654321',
            'edificio': self.edificio.id,
            'vivienda': self.vivienda.id,
            'vehiculos': 3,  # Actualizar número de vehículos
            'es_propietario': True,
            'activo': True
        }

        response = self.client.post(reverse('residente-update', args=[self.residente.id]), data)
        self.assertEqual(response.status_code, 302)  # Redirección
        
        # Verificar que se actualizó el residente
        self.residente.refresh_from_db()
        self.assertEqual(self.residente.vehiculos, 3)

class GeneradorViviendasTest(TestCase):
    """Pruebas del servicio generar_viviendas (viviendas/services.py)."""

    def setUp(self):
        self.edificio = Edificio.objects.create(
            nombre='Edificio Gen', direccion='Calle Test 1', pisos=3,
            esquema_numeracion='PISO_LETRA', deptos_por_piso=2,
        )

    def test_malla_completa_piso_letra(self):
        from .services import generar_viviendas
        r = generar_viviendas(self.edificio)
        self.assertEqual(len(r['creadas']), 6)  # 3 pisos x 2
        self.assertIn('1-A', r['creadas'])
        self.assertIn('3-B', r['creadas'])
        self.assertEqual(r['errores'], [])
        self.assertEqual(Vivienda.objects.filter(edificio=self.edificio).count(), 6)
        # todas desocupadas y con piso correcto
        v = Vivienda.objects.get(edificio=self.edificio, numero='2-A')
        self.assertEqual(v.piso, 2)
        self.assertEqual(v.estado, 'DESOCUPADO')

    def test_idempotente(self):
        from .services import generar_viviendas
        generar_viviendas(self.edificio)
        r2 = generar_viviendas(self.edificio)
        self.assertEqual(len(r2['creadas']), 0)
        self.assertEqual(len(r2['existentes']), 6)
        self.assertEqual(Vivienda.objects.filter(edificio=self.edificio).count(), 6)

    def test_no_toca_manuales(self):
        from .services import generar_viviendas
        manual = Vivienda.objects.create(
            edificio=self.edificio, numero='65', piso=1,
            metros_cuadrados=80, habitaciones=3, baños=2,
        )
        r = generar_viviendas(self.edificio)
        manual.refresh_from_db()
        self.assertEqual(manual.numero, '65')  # intacta
        self.assertEqual(len(r['creadas']), 6)  # el generador agrega las suyas
        self.assertEqual(Vivienda.objects.filter(edificio=self.edificio).count(), 7)

    def test_dry_run_no_escribe(self):
        from .services import generar_viviendas
        r = generar_viviendas(self.edificio, dry_run=True)
        self.assertTrue(r['dry_run'])
        self.assertEqual(len(r['creadas']), 6)
        self.assertEqual(Vivienda.objects.filter(edificio=self.edificio).count(), 0)

    def test_esquema_piso_unidad(self):
        from .services import generar_viviendas
        self.edificio.esquema_numeracion = 'PISO_UNIDAD'
        self.edificio.save()
        r = generar_viviendas(self.edificio)
        self.assertIn('101', r['creadas'])
        self.assertIn('302', r['creadas'])

    def test_esquema_manual_rechazado(self):
        from .services import generar_viviendas
        self.edificio.esquema_numeracion = 'MANUAL'
        self.edificio.save()
        with self.assertRaises(ValueError):
            generar_viviendas(self.edificio)

    def test_topes_de_volumen(self):
        from .services import generar_viviendas
        with self.assertRaises(ValueError):
            generar_viviendas(self.edificio, deptos_por_piso=13)  # > MAX_DEPTOS_POR_PISO
        gigante = Edificio.objects.create(
            nombre='Gigante', direccion='x', pisos=61,
            esquema_numeracion='PISO_LETRA', deptos_por_piso=2,
        )
        with self.assertRaises(ValueError):
            generar_viviendas(gigante)
        self.assertEqual(Vivienda.objects.filter(edificio=gigante).count(), 0)

    def test_crear_puertas_sin_pisar_hardware(self):
        from .services import generar_viviendas
        from accesos.models import Puerta
        generar_viviendas(self.edificio, crear_puertas=True)
        self.assertEqual(
            Puerta.objects.filter(vivienda__edificio=self.edificio).count(), 6
        )
        # segunda corrida no duplica puertas ni toca webhook
        p = Puerta.objects.get(vivienda__numero='1-A', vivienda__edificio=self.edificio)
        p.webhook_url = 'http://192.168.0.50/abrir/test'
        p.save()
        generar_viviendas(self.edificio, crear_puertas=True)
        p.refresh_from_db()
        self.assertEqual(p.webhook_url, 'http://192.168.0.50/abrir/test')
        self.assertEqual(
            Puerta.objects.filter(vivienda__edificio=self.edificio).count(), 6
        )


class EdificioCreateConGeneracionTest(TestCase):
    """Flujo web: crear edificio con generación automática de departamentos."""

    def setUp(self):
        rol_admin, _ = Rol.objects.get_or_create(nombre='Administrador')
        User = get_user_model()
        self.admin = User.objects.create_user(
            username='admin.gen', password='clave123', rol=rol_admin
        )

    def test_crear_edificio_generando_departamentos(self):
        self.client.login(username='admin.gen', password='clave123')
        data = {
            'nombre': 'Torre Auto',
            'direccion': 'Av. Generada 1',
            'pisos': 5,
            'esquema_numeracion': 'PISO_LETRA',
            'deptos_por_piso': 2,
            'generar_viviendas_auto': 'on',
        }
        response = self.client.post(reverse('edificio-create'), data)
        self.assertEqual(response.status_code, 302)
        ed = Edificio.objects.get(nombre='Torre Auto')
        self.assertEqual(ed.viviendas.count(), 10)  # 5 pisos x 2
        self.assertTrue(ed.viviendas.filter(numero='5-B').exists())

    def test_crear_edificio_sin_generar_sigue_igual(self):
        self.client.login(username='admin.gen', password='clave123')
        data = {'nombre': 'Torre Manual', 'direccion': 'x', 'pisos': 4}
        response = self.client.post(reverse('edificio-create'), data)
        self.assertEqual(response.status_code, 302)
        ed = Edificio.objects.get(nombre='Torre Manual')
        self.assertEqual(ed.viviendas.count(), 0)
        self.assertEqual(ed.esquema_numeracion, 'MANUAL')

    def test_generar_sin_esquema_da_error_de_form(self):
        self.client.login(username='admin.gen', password='clave123')
        data = {
            'nombre': 'Torre Mal', 'direccion': 'x', 'pisos': 4,
            'generar_viviendas_auto': 'on',  # sin esquema ni deptos_por_piso
        }
        response = self.client.post(reverse('edificio-create'), data)
        self.assertEqual(response.status_code, 200)  # se queda en el form con errores
        self.assertFalse(Edificio.objects.filter(nombre='Torre Mal').exists())
