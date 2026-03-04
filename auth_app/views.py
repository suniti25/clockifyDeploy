from __future__ import annotations

import logging
import secrets
from datetime import timedelta
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from django.conf import settings
from django.contrib.auth.models import User
from django.core.mail import send_mail
from django.db import transaction
from django.utils import timezone

from rest_framework import status
from rest_framework.authentication import SessionAuthentication
from rest_framework.exceptions import ValidationError as DRFValidationError
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.exceptions import TokenError
from rest_framework_simplejwt.tokens import RefreshToken

from drf_spectacular.utils import extend_schema
from drf_spectacular.types import OpenApiTypes

from user_app.models import Profile

from admin_app.permissions import IsAdminRole

from .models import PasswordResetToken
from .serializers import (
    ForgotPasswordSerializer,
    LoginSerializer,
    RegisterSerializer,
    ResetPasswordSerializer,
    UpdateEmailSerializer,
    ChangePasswordSerializer,
)

logger = logging.getLogger(__name__)


def _flatten_error_detail(detail):
    if isinstance(detail, list):
        flattened = [_flatten_error_detail(x) for x in detail]
        return flattened[0] if len(flattened) == 1 else flattened
    if isinstance(detail, dict):
        return {k: _flatten_error_detail(v) for k, v in detail.items()}
    return detail


def _password_error_response(exc: DRFValidationError) -> Response:
    data = _flatten_error_detail(getattr(exc, "detail", exc))
    if isinstance(data, dict) and set(data.keys()) == {"non_field_errors"}:
        data = {"detail": data.get("non_field_errors")}
    return Response(data, status=status.HTTP_400_BAD_REQUEST)


class CsrfExemptSessionAuthentication(SessionAuthentication):
    def enforce_csrf(self, request):
        return


class LoginView(APIView):
    permission_classes = [AllowAny]

    @extend_schema(
        description="Login and return access token. Refresh token is set in an HTTP-only cookie.",
        request=LoginSerializer,
        responses={200: OpenApiTypes.OBJECT, 400: OpenApiTypes.OBJECT},
    )
    def post(self, request):
        ser = LoginSerializer(data=request.data)
        ser.is_valid(raise_exception=True)

        user = ser.validated_data["user"]
        refresh = RefreshToken.for_user(user)

        profile = Profile.objects.filter(user=user).only("role", "employee_id").first()

        resp = Response(
            {
                "access": str(refresh.access_token),
                "username": user.username,
                "role": profile.role if profile else None,
                "employee_id": profile.employee_id
                if (profile and profile.employee_id)
                else None,
            },
            status=status.HTTP_200_OK,
        )

        resp.set_cookie(
            "refresh_token",
            str(refresh),
            httponly=True,
            secure=True,
            samesite="None",
            max_age=60 * 60 * 24 * 30,  # 30 days
            path="/",
        )
        return resp


class SetRefreshCookieView(APIView):
    permission_classes = [AllowAny]

    @extend_schema(
        description="Set refresh token cookie from a provided refresh token string.",
        request=OpenApiTypes.OBJECT,
        responses={
            200: OpenApiTypes.OBJECT,
            400: OpenApiTypes.OBJECT,
            401: OpenApiTypes.OBJECT,
        },
    )
    def post(self, request):
        refresh_token = request.data.get("refresh")
        if not refresh_token:
            return Response(
                {"detail": "No refresh token provided"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            refresh = RefreshToken(refresh_token)
        except TokenError:
            return Response(
                {"detail": "Invalid refresh token"}, status=status.HTTP_401_UNAUTHORIZED
            )

        resp = Response({"detail": "Refresh cookie set"}, status=status.HTTP_200_OK)
        resp.set_cookie(
            "refresh_token",
            str(refresh),
            httponly=True,
            secure=True,
            samesite="None",
            max_age=60 * 60 * 24 * 30,  # 30 days
            path="/",
        )
        return resp


class RegisterView(APIView):
    permission_classes = [IsAuthenticated, IsAdminRole]

    @extend_schema(
        description="Register a new user (admin-only).",
        request=RegisterSerializer,
        responses={201: OpenApiTypes.OBJECT, 400: OpenApiTypes.OBJECT},
    )
    def post(self, request):
        ser = RegisterSerializer(data=request.data)
        ser.is_valid(raise_exception=True)

        user = ser.save()

        return Response(
            {
                "message": "User registered successfully",
                "user": {
                    "id": user.id,
                    "username": user.username,
                    "email": user.email,
                    "first_name": user.first_name,
                    "last_name": user.last_name,
                    "date_joined": user.date_joined,
                    "joined_date": user.date_joined,
                },
            },
            status=status.HTTP_201_CREATED,
        )


class UpdateEmailView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(
        description="Update logged-in user's email.",
        request=UpdateEmailSerializer,
        responses={200: OpenApiTypes.OBJECT, 400: OpenApiTypes.OBJECT},
    )
    def post(self, request):
        return self.patch(request)

    @extend_schema(
        description="Update logged-in user's email (PATCH).",
        request=UpdateEmailSerializer,
        responses={200: OpenApiTypes.OBJECT, 400: OpenApiTypes.OBJECT},
    )
    def patch(self, request):
        ser = UpdateEmailSerializer(data=request.data, context={"user": request.user})
        ser.is_valid(raise_exception=True)
        ser.save()

        return Response(
            {"message": "Email updated successfully", "email": request.user.email},
            status=status.HTTP_200_OK,
        )


class RefreshTokenView(APIView):
    permission_classes = [AllowAny]

    @extend_schema(
        description="Return a new access token using refresh_token cookie.",
        responses={200: OpenApiTypes.OBJECT, 401: OpenApiTypes.OBJECT},
    )
    def post(self, request):
        refresh_token = request.COOKIES.get("refresh_token")
        if not refresh_token:
            return Response(
                {"detail": "Refresh token missing"}, status=status.HTTP_401_UNAUTHORIZED
            )

        try:
            refresh = RefreshToken(refresh_token)
            return Response(
                {"access": str(refresh.access_token)}, status=status.HTTP_200_OK
            )
        except TokenError:
            return Response(
                {"detail": "Invalid or expired refresh token"},
                status=status.HTTP_401_UNAUTHORIZED,
            )


class ChangePasswordView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(
        description="Change current user's password.",
        request=ChangePasswordSerializer,
        responses={200: OpenApiTypes.OBJECT, 400: OpenApiTypes.OBJECT},
    )
    def post(self, request):
        ser = ChangePasswordSerializer(
            data=request.data, context={"user": request.user}
        )
        try:
            ser.is_valid(raise_exception=True)
        except DRFValidationError as exc:
            return _password_error_response(exc)
        ser.save()
        return Response(
            {"detail": "Password changed successfully"},
            status=status.HTTP_200_OK,
        )


class ForgotPasswordView(APIView):
    permission_classes = [AllowAny]

    @extend_schema(
        description="Request password reset email. Always returns generic success to avoid user enumeration.",
        request=ForgotPasswordSerializer,
        responses={200: OpenApiTypes.OBJECT},
    )
    def post(self, request):
        ser = ForgotPasswordSerializer(data=request.data)
        ser.is_valid(raise_exception=True)

        email = (ser.validated_data.get("email") or "").strip().lower()

        generic_resp = Response(
            {"detail": "If the email exists, we sent a reset link."},
            status=status.HTTP_200_OK,
        )

        if not email:
            return generic_resp

        user = User.objects.filter(email__iexact=email).first()
        if not user or not user.is_active:
            return generic_resp

        raw_token = secrets.token_urlsafe(32)
        token_hash = PasswordResetToken.hash_raw_token(raw_token)
        expires_at = timezone.now() + timedelta(minutes=30)

        PasswordResetToken.objects.filter(
            user=user,
            used_at__isnull=True,
            expires_at__lt=timezone.now(),
        ).delete()

        PasswordResetToken.objects.create(
            user=user,
            token_hash=token_hash,
            expires_at=expires_at,
        )

        base = (getattr(settings, "FRONTEND_PASSWORD_RESET_URL", "") or "").strip()
        if base:
            parts = urlsplit(base)
            query = dict(parse_qsl(parts.query, keep_blank_values=True))
            query["token"] = raw_token
            reset_link = urlunsplit(
                (
                    parts.scheme,
                    parts.netloc,
                    parts.path,
                    urlencode(query),
                    parts.fragment,
                )
            )
        else:
            reset_link = request.build_absolute_uri(
                f"/reset-password?token={raw_token}"
            )

        subject = "Reset your password"
        message = (
            "You requested a password reset.\n\n"
            f"Reset link: {reset_link}\n\n"
            "This link expires in 30 minutes. If you did not request this, ignore this email."
        )

        try:
            send_mail(
                subject,
                message,
                settings.DEFAULT_FROM_EMAIL,
                [user.email],
                fail_silently=False,
            )
        except Exception:
            logger.exception("Password reset email send failed")
            return generic_resp

        return generic_resp


class ResetPasswordView(APIView):
    permission_classes = [AllowAny]

    @extend_schema(
        description="Reset password using token + newPassword.",
        request=ResetPasswordSerializer,
        responses={200: OpenApiTypes.OBJECT, 400: OpenApiTypes.OBJECT},
    )
    @transaction.atomic
    def post(self, request):
        ser = ResetPasswordSerializer(data=request.data)
        try:
            ser.is_valid(raise_exception=True)
        except DRFValidationError as exc:
            return _password_error_response(exc)

        raw_token = (ser.validated_data.get("token") or "").strip()
        pwd = (ser.validated_data.get("newPassword") or "").strip()

        token_hash = PasswordResetToken.hash_raw_token(raw_token)
        prt = (
            PasswordResetToken.objects.select_related("user")
            .select_for_update()
            .filter(token_hash=token_hash, used_at__isnull=True)
            .first()
        )

        if not prt or prt.is_expired():
            return Response(
                {"detail": "Invalid or expired token"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        user = prt.user
        user.set_password(pwd)
        user.save(update_fields=["password"])

        prt.used_at = timezone.now()
        prt.save(update_fields=["used_at"])

        return Response(
            {"detail": "Password updated successfully"}, status=status.HTTP_200_OK
        )
