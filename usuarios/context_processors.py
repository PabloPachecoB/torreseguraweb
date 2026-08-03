from .models import ClientePotencial


def clientes_potenciales_count(request):
    rol = getattr(request.user, 'rol', None)
    if (
        request.user.is_authenticated
        and rol is not None
        and rol.nombre == 'Administrador'
    ):
        return {
            'clientes_potenciales_count': ClientePotencial.objects.count()
        }
    return {}
