from django.apps import AppConfig


class UserAppConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'user_app'

    def ready(self):
        # Ensure signals are registered when Django starts
        import user_app.signals  # noqa: F401