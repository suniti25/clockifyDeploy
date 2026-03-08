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
