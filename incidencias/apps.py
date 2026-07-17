from django.apps import AppConfig


class IncidenciasConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'incidencias'
    verbose_name = 'Incidencias y mantenimiento'

    def ready(self):
        from . import signals  # noqa: F401
