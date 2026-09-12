"""Single source of truth for how a HARTI source row's (item, category) pair maps to a
Commodity's commodity_name.

Why this exists: the source CSVs' "item" column alone is NOT globally unique - four distinct
Pettah commodities (Big Onion / Dried Chillies / Onion / Potatoes, all literally "Imported")
and two distinct rice types (both literally "Raw White") only differ by their "category"
column. Using "item" alone as commodity_name would silently merge unrelated price series.

Verified against the full 65-item historical dataset: (item, category) together IS globally
unique (no market qualifier needed). Every caller that creates or looks up a Commodity from
source data - the one-time backfill (Story 3.2) and the daily ingestion parser (Story 3.3/3.4)
- must go through commodity_name_for() rather than reimplementing this rule, so the two paths
can never drift apart and start creating duplicate Commodity records for the same real item.
"""


def commodity_name_for(item: str, category: str) -> str:
	item = (item or "").strip()
	category = (category or "").strip()
	if not item:
		raise ValueError("item is required to build a commodity_name")
	if not category:
		# Every real HARTI row has a category; an empty one here means bad input, not a
		# legitimately uncategorized commodity - fail loudly rather than silently
		# producing a name that might collide with another item lacking a category too.
		raise ValueError(f"category is required to build a commodity_name for item {item!r}")
	return f"{category} - {item}"


def item_for(commodity_name: str, category: str) -> str:
	"""Reverses commodity_name_for(): recovers the original "item" text from a commodity_name
	given its already-known category (Commodity.harti_category, the same source column
	commodity_name_for() was built from). Used by the CSV mirror (Story 3.10) to reconstruct
	the standalone pipeline's original item/category columns without a second, drifting
	naming rule."""
	category = (category or "").strip()
	prefix = f"{category} - "
	if not commodity_name.startswith(prefix):
		raise ValueError(
			f"commodity_name {commodity_name!r} does not start with expected prefix {prefix!r} "
			f"derived from category {category!r}"
		)
	return commodity_name[len(prefix) :]
