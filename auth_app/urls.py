from django.urls import path
from .views import LoginView, RegisterView, RefreshCookieView, LogoutView, SetCookieView, UpdateEmailView

urlpatterns = [
    path('login/', LoginView.as_view(), name='login'),
    path('register/', RegisterView.as_view(), name='register'),
    path('refresh/', RefreshCookieView.as_view(), name='token_refresh'),
    path('logout/', LogoutView.as_view(), name='logout'),
    path('set-cookie/', SetCookieView.as_view(), name='set_cookie'),
    path('email/', UpdateEmailView.as_view(), name='update_email'),
]
