from __future__ import annotations

import logging

from django.conf import settings
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import exception_handler as drf_exception_handler

logger = logging.getLogger(__name__)


def api_exception_handler(exc, context):
    """Return JSON responses for all API exceptions.
    DRF handles `APIException` types by default. For unexpected exceptions,
    DRF may re-raise (resulting in Django HTML 500 pages), which breaks
    frontends expecting JSON.
    """

    response = drf_exception_handler(exc, context)
    if response is not None:
        return response

    view = context.get("view") if isinstance(context, dict) else None
    logger.exception(
        "Unhandled exception in API view=%s",
        getattr(view, "__class__", type(view)).__name__,
    )

    payload = {"detail": "Internal server error"}
    if settings.DEBUG:
        payload["error_type"] = exc.__class__.__name__
        payload["error"] = str(exc)

    return Response(payload, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
