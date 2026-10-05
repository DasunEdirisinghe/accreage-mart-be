"""The unit a HARTI price is quoted in.

HARTI quotes most items per kg, but eggs are priced per egg and several fruits per fruit. The
unit shows up inconsistently in the source: in the ``unit`` column ("Rs/Egg"), in the item name
("Ambul(Rs/Kg)") or in the category name ("Other Fruits (Rs/Fruit)"). This is the one place that
turns that into a short label for the listing form ("kg", "egg", "fruit"), so the price box never
calls an egg price a kilo price.
"""

import re

DEFAULT_PRICE_UNIT = "kg"

_RS_PER = re.compile(r"rs\s*/\s*([a-z]+)", re.IGNORECASE)
_ALIASES = {"fruits": "fruit", "eggs": "egg"}


def price_unit_label(*texts: str | None) -> str:
	"""First ``Rs/<unit>`` found in ``texts`` (checked in order), else per kg."""
	for text in texts:
		match = _RS_PER.search(text or "")
		if match:
			token = match.group(1).lower()
			return _ALIASES.get(token, token)
	return DEFAULT_PRICE_UNIT
