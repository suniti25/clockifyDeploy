import unicodedata

from django.core.exceptions import ValidationError
from django.utils.translation import gettext_lazy as _


class SymbolPasswordValidator:
    """Require at least N symbol characters in the password."""

    def __init__(self, min_symbols: int = 1, symbols: str | None = None):
        self.min_symbols = int(min_symbols)
        self.symbols = set(symbols) if symbols is not None else None

    def _is_symbol(self, ch: str) -> bool:
        if self.symbols is not None:
            return ch in self.symbols

        if ch.isspace():
            return False

        category = unicodedata.category(ch)
        return bool(category) and category[0] in {"P", "S"}

    def validate(self, password: str, user=None):
        symbol_count = sum(1 for ch in password if self._is_symbol(ch))
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


class UppercasePasswordValidator:
    """Require at least N uppercase letters in the password."""

    def __init__(self, min_uppercase: int = 1):
        self.min_uppercase = int(min_uppercase)

    def validate(self, password: str, user=None):
        uppercase_count = sum(1 for ch in (password or "") if ch.isupper())
        if uppercase_count < self.min_uppercase:
            raise ValidationError(
                _(
                    "This password must contain at least %(min_uppercase)d uppercase letter(s)."
                ),
                code="password_no_uppercase",
                params={"min_uppercase": self.min_uppercase},
            )

    def get_help_text(self) -> str:
        return _(
            "Your password must contain at least %(min_uppercase)d uppercase letter(s)."
        ) % {"min_uppercase": self.min_uppercase}


class DigitPasswordValidator:
    """Require at least N digits (0-9) in the password."""

    def __init__(self, min_digits: int = 1):
        self.min_digits = int(min_digits)

    def validate(self, password: str, user=None):
        digit_count = sum(1 for ch in (password or "") if ch.isdigit())
        if digit_count < self.min_digits:
            raise ValidationError(
                _("This password must contain at least %(min_digits)d number(s)."),
                code="password_no_digit",
                params={"min_digits": self.min_digits},
            )

    def get_help_text(self) -> str:
        return _("Your password must contain at least %(min_digits)d number(s).") % {
            "min_digits": self.min_digits
        }
