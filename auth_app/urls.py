from django.urls import path

from .views import (
    LoginView,
    RegisterView,
    SetRefreshCookieView,
    UpdateEmailView,
    RefreshTokenView,
    ChangePasswordView,
    ForgotPasswordView,
    ResetPasswordView,
)

urlpatterns = [
    path("login/", LoginView.as_view(), name="login"),
    path("register/", RegisterView.as_view(), name="register"),
    path("email/", UpdateEmailView.as_view(), name="update_email"),
    path("setcookie/", SetRefreshCookieView.as_view(), name="set_refresh_cookie"),
    path("cookie/", RefreshTokenView.as_view(), name="refresh_token"),
    path("change-password/", ChangePasswordView.as_view(), name="change_password"),
    path("forgot-password/", ForgotPasswordView.as_view(), name="forgot_password"),
    path("reset-password/", ResetPasswordView.as_view(), name="reset_password"),
]
