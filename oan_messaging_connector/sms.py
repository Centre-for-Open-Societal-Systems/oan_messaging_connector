"""Rules OAN enforces: one SMS part (160 GSM-7 septets, else 70 UTF-16 units) and an
Ethiopian mobile recipient (09 or 07 series)."""

import re

GSM7_BASIC = frozenset(
	"@£$¥èéùìòÇ\nØø\rÅåΔ_ΦΓΛΩΠΨΣΘΞÆæßÉ !\"#¤%&'()*+,-./0123456789:;<=>?"
	"¡ABCDEFGHIJKLMNOPQRSTUVWXYZÄÖÑÜ§¿abcdefghijklmnopqrstuvwxyzäöñüà"
)
GSM7_EXTENSION = frozenset("^{}\\[~]|€\f")


def fits_one_sms(text: str) -> bool:
	"""Same rule OAN enforces: 160 GSM-7 septets (extension chars = 2), else 70 UTF-16."""
	if all(c in GSM7_BASIC or c in GSM7_EXTENSION for c in text):
		return sum(2 if c in GSM7_EXTENSION else 1 for c in text) <= 160
	return len(text.encode("utf-16-le")) // 2 <= 70


#: The recipient forms OAN accepts (its ``ET_MOBILE_INPUT_PATTERN``).
ET_MOBILE = re.compile(r"^(\+?251[79][0-9]{8}|0[79][0-9]{8})$")
_SEPARATORS = re.compile(r"[\s\-().]")


def clean_mobile(raw: str | None) -> str | None:
	"""``raw`` without spaces, dashes, dots or brackets if it is an Ethiopian mobile OAN
	accepts (``09…``, ``07…``, ``2519…``, ``+2519…``, ``2517…``, ``+2517…``), else None."""
	candidate = _SEPARATORS.sub("", raw or "")
	return candidate if ET_MOBILE.fullmatch(candidate) else None
