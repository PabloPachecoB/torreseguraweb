"""Generador de viviendas por edificio.

Crea en lote las viviendas de un edificio según su esquema de numeración,
sin tocar NUNCA las existentes (solo agrega; ver Plan - Generador de
Viviendas en el cerebro del proyecto).

Reglas de seguridad:
- Topes duros de volumen (MAX_PISOS, MAX_DEPTOS_POR_PISO, MAX_POR_CORRIDA).
- Pre-validación completa ANTES de insertar: un error de datos no envenena
  la transacción (los inválidos se reportan, los válidos se crean).
- Idempotente: get_or_create por (edificio, numero); correrlo dos veces
  no duplica.
- dry_run=True calcula y reporta sin escribir nada.
"""
from django.core.exceptions import ValidationError
from django.db import transaction

from .models import Edificio, Vivienda


def edificios_administrados(user):
    """Queryset de edificios que el usuario administra.

    - Administrador/superuser: todos.
    - Gerente con condominio: todas las torres de su condominio.
    - Gerente sin condominio: su edificio único (comportamiento histórico).
    - Otros roles: ninguno.

    Punto único de verdad para el alcance del gerente: el refactor gradual
    de los usos directos de `user.gerente.edificio` debe apuntar aquí.
    """
    rol = getattr(getattr(user, 'rol', None), 'nombre', None)
    if user.is_superuser or rol == 'Administrador':
        return Edificio.objects.all()
    gerente = getattr(user, 'gerente', None)
    if gerente is None:
        return Edificio.objects.none()
    if gerente.condominio_id:
        return Edificio.objects.filter(condominio=gerente.condominio)
    if gerente.edificio_id:
        return Edificio.objects.filter(pk=gerente.edificio_id)
    return Edificio.objects.none()

# Topes duros para evitar generar miles de filas por un typo
MAX_PISOS = 60
MAX_DEPTOS_POR_PISO = 12
MAX_POR_CORRIDA = 600

LETRAS_DEFAULT = 'ABCDEFGHIJKL'
NUMERO_MAX_LEN = 10  # max_length de Vivienda.numero


def _numero_para(esquema, piso, unidad, letras, padding, contador):
    """Devuelve el número de vivienda según el esquema."""
    if esquema == 'PISO_LETRA':
        return f"{piso}-{letras[unidad - 1]}"
    if esquema == 'PISO_UNIDAD':
        return f"{piso}{unidad:0{padding}d}"
    if esquema == 'CORRELATIVO':
        return f"{contador:0{padding}d}" if padding else str(contador)
    raise ValueError(f"Esquema no soportado por el generador: {esquema}")


def generar_viviendas(
    edificio,
    esquema=None,
    deptos_por_piso=None,
    pisos_desde=1,
    pisos_hasta=None,
    letras=LETRAS_DEFAULT,
    padding=2,
    metros_cuadrados=60,
    habitaciones=2,
    banios=1,
    crear_puertas=False,
    dry_run=False,
):
    """Genera las viviendas que falten en `edificio`.

    Devuelve un dict-reporte:
      {'creadas': [...], 'existentes': [...], 'errores': [(numero, motivo)...],
       'dry_run': bool}
    Lanza ValueError si los parámetros violan los topes (no toca la BD).
    """
    esquema = esquema or edificio.esquema_numeracion
    deptos_por_piso = deptos_por_piso if deptos_por_piso is not None else edificio.deptos_por_piso

    if esquema == 'MANUAL':
        raise ValueError('El edificio está en esquema MANUAL: no se genera nada.')
    if not deptos_por_piso or deptos_por_piso < 1:
        raise ValueError('deptos_por_piso debe ser al menos 1.')
    if deptos_por_piso > MAX_DEPTOS_POR_PISO:
        raise ValueError(f'Tope superado: máximo {MAX_DEPTOS_POR_PISO} deptos por piso.')
    if esquema == 'PISO_LETRA' and deptos_por_piso > len(letras):
        raise ValueError(f'No hay letras suficientes ({len(letras)}) para {deptos_por_piso} deptos por piso.')

    pisos_hasta = min(pisos_hasta or edificio.pisos, edificio.pisos)
    if pisos_desde < 1 or pisos_desde > pisos_hasta:
        raise ValueError(f'Rango de pisos inválido: {pisos_desde}..{pisos_hasta}.')
    total_pisos = pisos_hasta - pisos_desde + 1
    if total_pisos > MAX_PISOS:
        raise ValueError(f'Tope superado: máximo {MAX_PISOS} pisos por corrida.')

    total = total_pisos * deptos_por_piso
    if total > MAX_POR_CORRIDA:
        raise ValueError(f'Tope superado: {total} viviendas > máximo {MAX_POR_CORRIDA} por corrida.')

    # --- Pre-cálculo y pre-validación (sin tocar la BD) ---
    existentes_en_bd = set(
        Vivienda.objects.filter(edificio=edificio).values_list('numero', flat=True)
    )
    plan, errores = [], []
    contador = 1
    for piso in range(pisos_desde, pisos_hasta + 1):
        for unidad in range(1, deptos_por_piso + 1):
            numero = _numero_para(esquema, piso, unidad, letras, padding, contador)
            contador += 1
            if len(numero) > NUMERO_MAX_LEN:
                errores.append((numero, f'número excede {NUMERO_MAX_LEN} caracteres'))
                continue
            plan.append((numero, piso))

    creadas, existentes = [], []
    if dry_run:
        for numero, _ in plan:
            (existentes if numero in existentes_en_bd else creadas).append(numero)
        return {'creadas': creadas, 'existentes': existentes, 'errores': errores, 'dry_run': True}

    # --- Creación real (solo filas pre-validadas; get_or_create = idempotente) ---
    from accesos.models import Puerta

    with transaction.atomic():
        for numero, piso in plan:
            try:
                vivienda, creada = Vivienda.objects.get_or_create(
                    edificio=edificio,
                    numero=numero,
                    defaults={
                        'piso': piso,
                        'metros_cuadrados': metros_cuadrados,
                        'habitaciones': habitaciones,
                        'baños': banios,
                        'estado': 'DESOCUPADO',
                    },
                )
            except ValidationError as e:
                # full_clean corre en save(); el error se captura ANTES de que
                # llegue un INSERT inválido a la BD, así el lote sigue.
                errores.append((numero, '; '.join(e.messages)))
                continue

            (creadas if creada else existentes).append(numero)

            if crear_puertas:
                # No pisa puertas existentes ni su webhook_url (hardware).
                Puerta.objects.get_or_create(
                    tipo=Puerta.TIPO_VIVIENDA,
                    vivienda=vivienda,
                    defaults={'nombre': numero},
                )

    return {'creadas': creadas, 'existentes': existentes, 'errores': errores, 'dry_run': False}
