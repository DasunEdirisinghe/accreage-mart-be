"""Seed the marketplace categories and link them to HARTI commodities.

Run it once (and again any time, it is safe to repeat)::

    bench --site accreage-mart.localhost execute accreage_mart.setup.seed_categories.run

One category per product, not per variant ("Embul Kesel", not one for every market or grade).
Each category is linked to the HARTI commodity whose price forecast best stands for it, so the
listing form can show an AI price suggestion. A few categories have no commodity on purpose
(Coconut, Organic Fertilizer, Farm Tools, and the catch-all "Other"): there is no price data for them, and the form says so.

Rules, so the script can be run on a site that staff have already edited:
  * a category is matched by its title; an existing one is never duplicated;
  * a commodity link is only added to a category that has none, never replaced;
  * a commodity that can't be found is reported and the category is created unlinked.

Not hooked into ``migrate``: this is starter data, created only when someone asks for it.
"""

import re

import frappe

# (title, Category area, commodity). The commodity is named like the backfill names it:
# "<HARTI category> - <item>" (see pricing/naming.py). None means "no price data".
CATEGORIES: tuple[tuple[str, str, str | None], ...] = (
	# Fruits
	("Embul Kesel", "Fruits", "Banana - Ambul(Rs/Kg)"),
	("Kolikuttu Kesel", "Fruits", "Banana - Kolikuttu"),
	("Seeni Kesel", "Fruits", "Banana - Seeni"),
	("Papaya", "Fruits", "Banana - Papaya (Rs/Kg)"),
	("Passion Fruit", "Fruits", "Banana - Passion Fruits"),
	("Pineapple", "Fruits", "Other Fruits (Rs/Fruit) - Pineapple - Medium"),
	("Avocado", "Fruits", "Other Fruits (Rs/Fruit) - Avocado"),
	("Orange", "Fruits", "Other Fruits (Rs/Fruit) - Orange"),
	("Wood Apple", "Fruits", "Other Fruits (Rs/Fruit) - Woodapple"),
	# Vegetables
	("Carrot", "Vegetables", "Up Country Vegetable - Carrot"),
	("Beans", "Vegetables", "Up Country Vegetable - Beans"),
	("Beetroot", "Vegetables", "Up Country Vegetable - Beet root"),
	("Cabbage", "Vegetables", "Up Country Vegetable - Cabbage (Kandy)"),
	("Knolkhol", "Vegetables", "Up Country Vegetable - Knolkhol"),
	("Leeks", "Vegetables", "Up Country Vegetable - Leeks"),
	("Radish", "Vegetables", "Up Country Vegetable - Raddish"),
	("Tomato", "Vegetables", "Up Country Vegetable - Tomato"),
	("Brinjal", "Vegetables", "Low country Vegetable - Brinjals"),
	("Bitter Gourd", "Vegetables", "Low country Vegetable - Bitter Gourd"),
	("Capsicum", "Vegetables", "Low country Vegetable - Capsicum"),
	("Cucumber", "Vegetables", "Low country Vegetable - Cucumber"),
	("Drumstick", "Vegetables", "Low country Vegetable - Drumstick"),
	("Green Chillies", "Vegetables", "Low country Vegetable - Green Chillies"),
	("Ladies Fingers", "Vegetables", "Low country Vegetable - Ladies Fingers"),
	("Lime", "Vegetables", "Low country Vegetable - Lime"),
	("Long Beans", "Vegetables", "Low country Vegetable - Long Beans"),
	("Luffa", "Vegetables", "Low country Vegetable - Luffa"),
	("Manioc", "Vegetables", "Low country Vegetable - Manioc"),
	("Pumpkin", "Vegetables", "Low country Vegetable - Pumpkin"),
	("Snake Gourd", "Vegetables", "Low country Vegetable - Snake Gourd"),
	("Sweet Potato", "Vegetables", "Low country Vegetable - Sweet Potatoe"),
	("Alu Kesel", "Vegetables", "Low country Vegetable - Ash Plantains"),
	("Potato", "Vegetables", "Potatoes - Nuwaraeliya"),
	("Big Onion", "Vegetables", "Big Onion - Imported"),
	("Small Onion", "Vegetables", "Onion - Sinnan"),
	# Rice
	("Nadu Rice", "Rice", "Rice - Nadu 1"),
	("Samba Rice", "Rice", "Rice - Samba 1"),
	("Keeri Samba", "Rice", "Rice - Keeri Samba"),
	("Red Rice", "Rice", "Rice - Raw red"),
	("White Rice", "Rice", "Rice - Raw White"),
	# Other produce
	("Eggs", "Other", "Eggs - White"),
	("Red Dhal", "Other", "Pulses - Red Dhal"),
	("Green Gram", "Other", "Pulses - Green Gram"),
	("Cowpea", "Other", "Pulses - Cowpea"),
	# No price data: the listing form says so
	("Coconut", "Other", None),
	# The catch-all for anything that doesn't fit a category above
	("Other", "Other", None),
	("Organic Fertilizer", "Fertilizer", None),
	("Farm Tools", "Tools", None),
)


def normalize(text: str) -> str:
	"""Lower case, punctuation to spaces, single spaces."""
	return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()


def find_commodity(hint: str | None, commodity_names: list[str]) -> str | None:
	"""The commodity that ``hint`` names, tolerating case, spacing and punctuation differences.

	An exact (normalized) match wins. Otherwise every word of the hint must appear in the
	commodity's name; of several such commodities the one with the fewest extra words is taken.
	"""
	if not hint:
		return None
	wanted = normalize(hint)
	by_normal = {normalize(name): name for name in commodity_names}
	if wanted in by_normal:
		return by_normal[wanted]

	words = set(wanted.split())
	candidates = [
		(len(normal.split()), name) for normal, name in by_normal.items() if words <= set(normal.split())
	]
	return min(candidates)[1] if candidates else None


def run(commit: bool = True) -> dict:
	"""Create the missing categories and link what can be linked. Returns (and prints) a summary.

	``bench execute`` does not commit on its own, so this commits at the end. Tests pass
	``commit=False`` to stay inside their own transaction."""
	commodity_names = frappe.get_all("Commodity", pluck="name")
	summary: dict[str, list[str]] = {
		"created": [],
		"linked_existing": [],
		"already_present": [],
		"commodity_not_found": [],
	}

	for title, area, hint in CATEGORIES:
		commodity = find_commodity(hint, commodity_names)
		if hint and not commodity:
			summary["commodity_not_found"].append(f"{title} (looked for: {hint})")

		existing = frappe.db.get_value("Category", {"title": title}, ["name", "commodity"], as_dict=True)
		if existing:
			if commodity and not existing.commodity:
				frappe.db.set_value("Category", existing.name, "commodity", commodity)
				summary["linked_existing"].append(title)
			else:
				summary["already_present"].append(title)
			continue

		frappe.get_doc(
			{"doctype": "Category", "title": title, "area": area, "commodity": commodity}
		).insert(ignore_permissions=True)
		summary["created"].append(title)

	if commit:
		frappe.db.commit()

	_print(summary)
	return summary


def _print(summary: dict[str, list[str]]) -> None:
	print(
		f"Categories: {len(summary['created'])} created, {len(summary['linked_existing'])} linked, "
		f"{len(summary['already_present'])} already there."
	)
	if summary["commodity_not_found"]:
		print("No matching commodity for (created without a price link):")
		for line in summary["commodity_not_found"]:
			print(f"  - {line}")
