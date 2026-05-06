from django.contrib import admin
from django.http import JsonResponse
from django.urls import path, include
from django.views.decorators.http import require_http_methods
from drf_spectacular.views import (
    SpectacularAPIView,
    SpectacularSwaggerView,
    SpectacularRedocView,
)

from auth_app.views import LoginView


@require_http_methods(["GET", "HEAD"])
def health_check(request):
    return JsonResponse({"status": "ok"}, status=200)


urlpatterns = [
    path("LMS-Admin/", admin.site.urls),
    path("health", health_check, name="health"),
    path("health/", health_check, name="health_slash"),
    # Compatibility alias: some frontends call /login/ directly
    path("login/", LoginView.as_view(), name="login"),
    path("api/auth/", include("auth_app.urls")),
    path("api/user/", include("user_app.urls")),
    path("api/form/", include("form_app.urls")),
    path("api/admin/", include("admin_app.urls")),
    path("api/discord/", include("discord_app.urls")),
    path("discord/", include("discord_app.urls")),
    # Time tracking (Clockify-style); isolated under /api/time/
    path("api/time/", include("time_tracking.urls")),
    path("api/", include("integrations.urls")),
    path("api/schema/", SpectacularAPIView.as_view(), name="schema"),
    path(
        "api/docs/",
        SpectacularSwaggerView.as_view(url_name="schema"),
        name="swagger-ui",
    ),
    path("api/redoc/", SpectacularRedocView.as_view(url_name="schema"), name="redoc"),
]
