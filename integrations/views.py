from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.http import HttpResponseBadRequest, HttpResponseForbidden
from django.shortcuts import redirect
from django.urls import reverse
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build

from django.utils import timezone
from integrations.crypto import encrypt_str
from integrations.models import GoogleCalendarCredential

SCOPES = ["https://www.googleapis.com/auth/calendar.events"]


def _is_admin_user(user) -> bool:
    if not user or not user.is_authenticated:
        return False
    if user.is_staff or user.is_superuser:
        return True
    profile = getattr(user, "profile", None)
    return bool(getattr(profile, "role", "").upper() == "ADMIN")


def _client_config():
    return {
        "web": {
            "client_id": settings.GOOGLE_OAUTH_CLIENT_ID,
            "client_secret": settings.GOOGLE_OAUTH_CLIENT_SECRET,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": [settings.GOOGLE_OAUTH_REDIRECT_URI],
        }
    }


@login_required
def google_connect(request):

    if not _is_admin_user(request.user):
        return HttpResponseForbidden("Admin access required.")

    if not settings.GOOGLE_OAUTH_CLIENT_ID or not settings.GOOGLE_OAUTH_CLIENT_SECRET:
        return HttpResponseBadRequest("Missing GOOGLE_OAUTH_CLIENT_ID/SECRET in env")

    if not settings.GOOGLE_OAUTH_REDIRECT_URI:
        return HttpResponseBadRequest("Missing GOOGLE_OAUTH_REDIRECT_URI in env")

    # Guardrail: redirect_uri must be the callback URL (Google will redirect there with ?code=...)
    # If this is misconfigured (e.g. points to /connect/), OAuth will loop or the callback will never get a code.
    expected_callback = request.build_absolute_uri(
        reverse("integrations_google_callback")
    )
    configured = settings.GOOGLE_OAUTH_REDIRECT_URI.strip()
    if configured.rstrip("/") != expected_callback.rstrip("/"):
        return HttpResponseBadRequest(
            "Invalid GOOGLE_OAUTH_REDIRECT_URI. "
            f"Expected: {expected_callback} ; Got: {configured}. "
            "Update the Azure env var and Google Cloud Console Authorized redirect URI to match exactly."
        )

    flow = Flow.from_client_config(
        _client_config(),
        scopes=SCOPES,
        redirect_uri=settings.GOOGLE_OAUTH_REDIRECT_URI,
    )

    auth_url, state = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        prompt="consent",
    )

    request.session["google_oauth_state"] = state
    return redirect(auth_url)


@login_required
def google_callback(request):
    """
    Google redirects here,token and save refresh token in DB.
    """
    if not _is_admin_user(request.user):
        return HttpResponseForbidden("Admin access required.")

    # If the user cancels consent or Google blocks the request,
    # Google may redirect back with an error instead of a code.
    oauth_error = request.GET.get("error")
    if oauth_error:
        oauth_error_description = request.GET.get("error_description") or ""
        msg = f"Google OAuth error: {oauth_error}"
        if oauth_error_description:
            msg = f"{msg}. {oauth_error_description}"
        return HttpResponseBadRequest(msg)

    # query parameters from Google OAuth callback
    code = request.GET.get("code")
    state = request.GET.get("state")

    if not code:
        return HttpResponseBadRequest(
            "Missing code from Google. Don’t open this callback URL directly; start from /api/admin/google/connect/ and complete the Google consent flow."
        )

    expected_state = request.session.get("google_oauth_state")
    if expected_state and state != expected_state:
        return HttpResponseBadRequest("Invalid OAuth state")

    flow = Flow.from_client_config(
        _client_config(),
        scopes=SCOPES,
        redirect_uri=settings.GOOGLE_OAUTH_REDIRECT_URI,
        state=state,
    )

    flow.fetch_token(code=code)
    creds = flow.credentials

    if not creds.refresh_token:
        return HttpResponseBadRequest(
            "No refresh token returned. Remove app access in Google Account -> Security -> Third-party access, then try again."
        )

    # get google email
    google_email = ""
    try:
        oauth2 = build("oauth2", "v2", credentials=creds, cache_discovery=False)
        me = oauth2.userinfo().get().execute()
        google_email = (me or {}).get("email", "")
    except Exception:
        google_email = ""

    token_expiry = creds.expiry
    if token_expiry and timezone.is_naive(token_expiry):
        token_expiry = timezone.make_aware(token_expiry, timezone=timezone.UTC)

    GoogleCalendarCredential.objects.update_or_create(
        user=request.user,
        defaults={
            "google_email": google_email,
            "calendar_id": "primary",
            "refresh_token_encrypted": encrypt_str(creds.refresh_token),
            "access_token": creds.token or "",
            "token_expiry": token_expiry,
        },
    )

    request.session.pop("google_oauth_state", None)
    return redirect("/LMS-Admin/")  # back to Django admin dashboard
