from django.contrib import admin
from django.urls import path, include

urlpatterns = [
    path("admin/", admin.site.urls),

    path("api/auth/", include("auth_app.urls")),
    path("api/user/", include("user_app.urls")),
    path("api/form/", include("form_app.urls")),
    path("api/admin/", include("admin_app.urls")),

    path("api/discord/", include("discord_app.urls")),
    
    path("discord/", include("discord_app.urls")),

    path("api/", include("integrations.urls")),
]
