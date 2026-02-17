from __future__ import annotations

import re

from django.core.exceptions import ValidationError


class SymbolPasswordValidator:
    def validate(self, password, user=None):
        if not re.search(r"[^A-Za-z0-9]", password or ""):
            raise ValidationError(
                "Password must contain at least 1 symbol (e.g. @, #, !).",
                code="password_no_symbol",
            )

    def get_help_text(self):
        return "Your password must contain at least 1 symbol (e.g. @, #, !)."
