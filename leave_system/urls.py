from django.contrib import admin
from django.urls import path, include
from drf_spectacular.views import (
    SpectacularAPIView,
    SpectacularSwaggerView,
    SpectacularRedocView,
)

urlpatterns = [
    path("LMS-Admin/", admin.site.urls),
    path("api/auth/", include("auth_app.urls")),
    path("api/user/", include("user_app.urls")),
    path("api/form/", include("form_app.urls")),
    path("api/admin/", include("admin_app.urls")),
    path("api/discord/", include("discord_app.urls")),
    path("discord/", include("discord_app.urls")),
    path("api/", include("integrations.urls")),
    path("api/schema/", SpectacularAPIView.as_view(), name="schema"),
    path(
        "api/docs/",
        SpectacularSwaggerView.as_view(url_name="schema"),
        name="swagger-ui",
    ),
    path("api/redoc/", SpectacularRedocView.as_view(url_name="schema"), name="redoc"),
]
