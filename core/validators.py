import string

from django.core.exceptions import ValidationError
from django.utils.translation import gettext_lazy as _


class SymbolPasswordValidator:
    """Require at least N symbol characters in the password.

    By default, "symbols" are characters from ``string.punctuation``.

    Django setting example:

        AUTH_PASSWORD_VALIDATORS = [
            {
                "NAME": "core.validators.SymbolPasswordValidator",
                "OPTIONS": {"min_symbols": 1},
            },
        ]
    """

    def __init__(self, min_symbols: int = 1, symbols: str | None = None):
        self.min_symbols = int(min_symbols)
        self.symbols = set(symbols) if symbols is not None else set(string.punctuation)

    def validate(self, password: str, user=None):
        symbol_count = sum(1 for ch in password if ch in self.symbols)
        if symbol_count < self.min_symbols:
            raise ValidationError(
                _("This password must contain at least %(min_symbols)d symbol(s)."),
                code="password_no_symbol",
                params={"min_symbols": self.min_symbols},
            )

    def get_help_text(self) -> str:
        return _("Your password must contain at least %(min_symbols)d symbol(s).") % {
            "min_symbols": self.min_symbols
        }
