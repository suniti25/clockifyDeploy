from datetime import timedelta
from pathlib import Path
import os
import dj_database_url

BASE_DIR = Path(__file__).resolve().parent.parent

# LOAD ENV VARIABLES
try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None

if load_dotenv:
    load_dotenv(BASE_DIR / ".env")


DISCORD_CRON_SECRET = os.getenv("DISCORD_CRON_SECRET", "")
DISCORD_DEBUG = os.getenv("DISCORD_DEBUG", "")

# Optional: proxy interactions for a specific channel to a local/ngrok URL.
# This allows keeping Discord's Interactions Endpoint URL pointed at Azure while still
# testing locally for a dev channel.
DISCORD_INTERACTIONS_PROXY_URL = os.getenv("DISCORD_INTERACTIONS_PROXY_URL", "").strip()
DISCORD_INTERACTIONS_PROXY_CHANNEL_ID = os.getenv("DISCORD_INTERACTIONS_PROXY_CHANNEL_ID", "").strip()

def env_bool(key: str, default: bool = False) -> bool:
    return os.getenv(key, str(default)).strip().lower() in ("1", "true", "yes", "y", "on")


def env_list(key: str, default=None, sep: str = ","):
    if default is None:
        default = []
    val = os.getenv(key, "")
    if not val:
        return default
    return [x.strip() for x in val.split(sep) if x.strip()]

# DJANGO CORE

SECRET_KEY = (os.getenv("SECRET_KEY") or "").strip()
if not SECRET_KEY:
    raise RuntimeError("Missing required configuration: SECRET_KEY")


DEBUG = env_bool("DEBUG", False)

ALLOWED_HOSTS = env_list("ALLOWED_HOSTS", default=["localhost", "127.0.0.1"])

# APPS

INSTALLED_APPS = [
    "corsheaders",
    "rest_framework",
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "drf_spectacular",

    "user_app.apps.UserAppConfig",
    "auth_app",
    "form_app.apps.FormAppConfig",
    "admin_app",
    "discord_app",
    "integrations",

    "rest_framework_simplejwt.token_blacklist",
]
# MIDDLEWARE
MIDDLEWARE = [
    "corsheaders.middleware.CorsMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
USE_X_FORWARDED_HOST = True


ROOT_URLCONF = "leave_system.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "leave_system.wsgi.application"

# DATABASE 
DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL is not set. Put it in .env or environment variables.")

DATABASES = {
    "default": dj_database_url.parse(DATABASE_URL, conn_max_age=600),
}

# PASSWORD VALIDATION
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator", "OPTIONS": {"min_length": 8}},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
    {"NAME": "core.validators.SymbolPasswordValidator"},
]

# DRF
REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": (
        "rest_framework_simplejwt.authentication.JWTAuthentication",
    ),
    "DEFAULT_PERMISSION_CLASSES": (
        "rest_framework.permissions.IsAuthenticated",
    ),
    "DEFAULT_PARSER_CLASSES": (
        "rest_framework.parsers.JSONParser",
        "rest_framework.parsers.FormParser",
        "rest_framework.parsers.MultiPartParser",
    ),
    "DEFAULT_RENDERER_CLASSES": (
        "rest_framework.renderers.JSONRenderer",
    ),
      "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
}

# SIMPLE JWT 

SIMPLE_JWT = {
    "ACCESS_TOKEN_LIFETIME": timedelta(minutes=10),
    "REFRESH_TOKEN_LIFETIME": timedelta(days=30),
    "AUTH_HEADER_TYPES": ("Bearer",),
    "ROTATE_REFRESH_TOKENS": True,
    "BLACKLIST_AFTER_ROTATION": True,
}

# CORS

CORS_ALLOW_CREDENTIALS = True

CORS_ALLOWED_ORIGINS = env_list("CORS_ALLOWED_ORIGINS")

# In production, DO NOT allow fallback origins
if not DEBUG and not CORS_ALLOWED_ORIGINS:
    raise RuntimeError(
        "CORS_ALLOWED_ORIGINS must be set in production when CORS_ALLOW_CREDENTIALS=True"
    )

# Safe defaults ONLY for local development
if DEBUG and not CORS_ALLOWED_ORIGINS:
    CORS_ALLOWED_ORIGINS = [
        "http://localhost:3000",
        "http://localhost:5173",
        "http://127.0.0.1:3000",
        "http://127.0.0.1:5173",
        "https://leave-management-system-frontend-gilt.vercel.app",
    ]


#  CSRF

CSRF_TRUSTED_ORIGINS = env_list(
    "CSRF_TRUSTED_ORIGINS",
    default=[
        "http://localhost:3000",
        "http://localhost:5173",
        "http://127.0.0.1:3000",
        "http://127.0.0.1:5173",
    ],
)

CORS_ALLOW_HEADERS = [
    "authorization",
    "content-type",
    "accept",
    "origin",
    "user-agent",
    "x-csrftoken",
    "x-requested-with",
    "ngrok-skip-browser-warning",
]

CORS_ALLOWED_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"]


# INTERNATIONALIZATION

LANGUAGE_CODE = "en-us"
TIME_ZONE = "Asia/Kathmandu"
USE_I18N = True
USE_L10N = True
USE_TZ = True

APPEND_SLASH = False

# STATIC
STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"


# EMAIL
EMAIL_BACKEND = os.getenv("EMAIL_BACKEND", "django.core.mail.backends.console.EmailBackend")
DEFAULT_FROM_EMAIL = os.getenv("DEFAULT_FROM_EMAIL", "noreply@leavesystem.com")

EMAIL_HOST = os.getenv("EMAIL_HOST", "")
EMAIL_PORT = int(os.getenv("EMAIL_PORT", "587"))
EMAIL_USE_TLS = env_bool("EMAIL_USE_TLS", True)
EMAIL_HOST_USER = os.getenv("EMAIL_HOST_USER", "")
EMAIL_HOST_PASSWORD = os.getenv("EMAIL_HOST_PASSWORD", "")

if EMAIL_HOST.strip() and (not EMAIL_HOST_USER.strip() or not EMAIL_HOST_PASSWORD.strip()):
    raise RuntimeError(
        "EMAIL_HOST is set but EMAIL_HOST_USER/EMAIL_HOST_PASSWORD is missing. "
        "Provide full SMTP credentials or unset EMAIL_HOST to disable SMTP."
    )

# Frontend URL for password reset page
FRONTEND_PASSWORD_RESET_URL = (
    (os.getenv("FRONTEND_PASSWORD_RESET_URL") or os.getenv("FRONTEND_RESET_PASSWORD_URL") or "").strip())

# DISCORD 
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "")
DISCORD_PUBLIC_KEY = os.getenv("DISCORD_PUBLIC_KEY", "")
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "")

DISCORD_ADMIN_CHANNEL_ID = os.getenv("DISCORD_ADMIN_CHANNEL_ID", "")
DISCORD_EMPLOYEE_CHANNEL_ID = os.getenv("DISCORD_EMPLOYEE_CHANNEL_ID", "")

DISCORD_ADMIN_CHANNEL_NAME = os.getenv("DISCORD_ADMIN_CHANNEL_NAME", "admin_channel")
DISCORD_EMPLOYEE_CHANNEL_NAME = os.getenv("DISCORD_EMPLOYEE_CHANNEL_NAME", "leaves_and_notices")


# PRODUCTION SECURITY

if not DEBUG:
    SECURE_SSL_REDIRECT = True
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True

    SECURE_HSTS_SECONDS = 31536000
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True
    SECURE_HSTS_PRELOAD = True

# GOOGLE OAUTH
GOOGLE_OAUTH_CLIENT_ID = os.getenv("GOOGLE_OAUTH_CLIENT_ID", "")
GOOGLE_OAUTH_CLIENT_SECRET = os.getenv("GOOGLE_OAUTH_CLIENT_SECRET", "")
GOOGLE_OAUTH_REDIRECT_URI = os.getenv("GOOGLE_OAUTH_REDIRECT_URI", "")
GOOGLE_TOKEN_ENCRYPTION_KEY = os.getenv("GOOGLE_TOKEN_ENCRYPTION_KEY", "")
