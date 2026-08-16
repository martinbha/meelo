"""Institution-specific screenshot parsers."""

from __future__ import annotations

from ..contracts import ScreenshotParser
from .base import InstitutionParser, InstitutionProfile
from .hyundai_card import HyundaiCardParser
from .kakao_bank import KakaoBankParser
from .kb_bank import KbBankParser
from .samsung_card import SamsungCardParser
from .shinhan_bank import ShinhanBankParser
from .toss_bank import TossBankParser

#: Every institution parser the registry should offer, in a stable order.
INSTITUTION_PARSER_CLASSES: tuple[type[InstitutionParser], ...] = (
    HyundaiCardParser,
    KakaoBankParser,
    KbBankParser,
    SamsungCardParser,
    ShinhanBankParser,
    TossBankParser,
)


def build_institution_parsers() -> tuple[ScreenshotParser, ...]:
    """Instantiate one parser per supported institution."""

    return tuple(parser_class() for parser_class in INSTITUTION_PARSER_CLASSES)


__all__ = [
    "INSTITUTION_PARSER_CLASSES",
    "HyundaiCardParser",
    "InstitutionParser",
    "InstitutionProfile",
    "KakaoBankParser",
    "KbBankParser",
    "SamsungCardParser",
    "ShinhanBankParser",
    "TossBankParser",
    "build_institution_parsers",
]
