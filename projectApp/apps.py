from django.apps import AppConfig


class ProjectappConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'projectApp'


    def ready(self):
        import projectApp.signals  # noqa: F401
