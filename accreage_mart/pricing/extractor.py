"""Extract daily commodity prices from a HARTI "Wholesale Prices" PDF report.

Ported from the standalone 06_price_dataset project's extract_pdf_prices.py. The parsing logic
itself - page classification, category detection, item-name alias normalization, and the
"don't guess" rule for ambiguous rows - is UNCHANGED from the original; these are load-bearing
fixes for real PDF inconsistencies documented in 06_price_dataset/README.md ("Data quality
fixes made along the way"). Do not simplify them.

What changed for Frappe: operates on PDF bytes (never touches disk - the daily ingestion job
parses and discards), takes the target date as an explicit argument instead of guessing it from
a filename/page text (the caller already knows which date it's fetching), and returns records
keyed by commodity_name (via accreage_mart.pricing.naming.commodity_name_for) instead of
writing per-item JSON files.

Two kinds of tables are pulled out, wherever they appear in the PDF:
  - Rice & Subsidiary Food Crops -> Pettah market (native Range + Average)
  - Vegetables/Fruits            -> Peliyagoda market (Range only; average price is computed
    as the midpoint of the range, since the source PDF prints no native average for it)

Pages are NOT assumed to be at fixed indices. Each page's table is inspected and classified by
its actual header content before parsing, and only the first matching page of each kind is
used - copes with PDFs that omit a table entirely, reorder pages, or repeat a Sinhala duplicate.
"""

import io
import re

import pdfplumber

from accreage_mart.pricing.naming import commodity_name_for

PAGE1_CATEGORY_NAMES = {
	"rice",
	"imported rice",
	"dried chillies",
	"onion",
	"big onion",
	"potatoes",
	"pulses",
	"consumption item",
	"eggs",
}
PAGE1_SKIP_LINES = {"subsidiary food crops"}

# Items deliberately excluded from extraction, keyed by (category, item) exactly as they
# appear in the parsed records. Ported unchanged from the standalone pipeline.
IGNORED_ITEMS = {
	("Dried Chillies", "Local"),
	("Imported Rice", "Raw red"),
	("Imported Rice", "Nadu"),
	("Up Country Vegetable", "Cabbage (N'Eliya)"),
	("Big Onion", "Local"),
	("Low country Vegetable", "Big-onion Local"),
	("Pulses", "Cowpea (White)"),
	("Other Fruits (Rs/Fruit)", "Karathakolomban"),
	("Other Fruits (Rs/Fruit)", "Mango - Betti"),
}

# The name group excludes any decimal-point number so a rare rendering glitch where a stray
# decimal value appears before the real range is rejected as ambiguous rather than silently
# swallowed into the item name.
ITEM_LINE_RE = re.compile(
	r"^(?P<name>(?:(?!\d+\.\d+).)*?)\s*(?P<min>\d+\.\d+)\s*-\s*(?P<max>\d+\.\d+)\s+(?P<avg>\d+\.\d+)"
)
DECIMAL_RE = re.compile(r"\d+\.\d+")
RANGE_RE = re.compile(r"(\d+(?:\.\d+)?)\s*-\s*(\d+(?:\.\d+)?)")

VEG_CATEGORY_RE = re.compile(r"vegetable|banana|other fruit", re.IGNORECASE)

PELIYAGODA_NAME_ALIASES = [
	(re.compile(r"karathakol", re.IGNORECASE), "Karathakolomban"),
	(re.compile(r"passion\s*fruit", re.IGNORECASE), "Passion Fruits"),
	(re.compile(r"^medium$", re.IGNORECASE), "Pineapple - Medium"),
	(re.compile(r"^small$", re.IGNORECASE), "Pineapple - Small"),
]


def strip_unit(line: str):
	"""If a line is 'Name (Unit)' with nothing else after it, split them."""
	m = re.match(r"^(.*?)\s*\(([^)]*)\)\s*$", line)
	if m:
		return m.group(1).strip(), m.group(2).strip()
	return line.strip(), None


def classify_page(page):
	"""Decide if a page's first table is the Pettah rice/food-crop table, the (English)
	Peliyagoda vegetable table, or neither. Returns (kind, table) - kind is 'pettah',
	'peliyagoda', or None."""
	tables = page.extract_tables()
	if not tables:
		return None, None
	table = tables[0]
	if not table or not table[0]:
		return None, None

	header_cells = " ".join(str(c) for row in table[:2] for c in row if c)
	first_header_cell = (table[0][0] or "").strip()

	if first_header_cell == "Item" and "Pettah" in header_cells:
		return "pettah", table
	if first_header_cell == "Variety" and "Peliyagoda" in header_cells:
		return "peliyagoda", table
	return None, None


def parse_pettah_page(text: str, date: str) -> list:
	records = []
	current_category = None
	current_unit = None

	for raw_line in text.splitlines():
		line = raw_line.strip()
		if not line:
			continue

		name_only, unit = strip_unit(line)

		if name_only.lower() in PAGE1_CATEGORY_NAMES:
			current_category = name_only
			current_unit = unit
			continue
		if name_only.lower() in PAGE1_SKIP_LINES:
			continue

		if current_category is None:
			continue

		m = ITEM_LINE_RE.match(line)
		if m:
			item_name = re.sub(r"[\s\-]+$", "", m.group("name")).strip()
			min_price = float(m.group("min"))
			max_price = float(m.group("max"))
			avg_price = float(m.group("avg"))
			records.append(
				{
					"date": date,
					"item": item_name,
					"category": current_category,
					"unit": current_unit,
					"market": "Pettah",
					"min_price": min_price,
					"max_price": max_price,
					"average_price": avg_price,
					"average_computed": False,
				}
			)
		else:
			# No clean Pettah range/average match - genuine no-data row, or an ambiguous
			# line rejected above. Only take the item's name, truncated at the first
			# decimal number so no stray price value ever leaks into it.
			decimal_match = DECIMAL_RE.search(line)
			item_name = line[: decimal_match.start()] if decimal_match else line
			item_name = re.sub(r"[\s\-]+$", "", item_name).strip()
			if not item_name:
				continue
			records.append(
				{
					"date": date,
					"item": item_name,
					"category": current_category,
					"unit": current_unit,
					"market": "Pettah",
					"min_price": None,
					"max_price": None,
					"average_price": None,
					"average_computed": False,
				}
			)
	return records


def normalize_peliyagoda_name(name: str) -> str:
	name = name.strip()
	name = re.sub(r"^-+\s*", "", name)
	name = re.sub(r"[\s\-]+$", "", name)
	for pattern, canonical in PELIYAGODA_NAME_ALIASES:
		if pattern.search(name):
			return canonical
	return name


def parse_peliyagoda_page(table: list, date: str) -> list:
	records = []
	current_category = "Up Country Vegetable"

	for row in table[2:]:  # first 2 rows are date/market headers
		if not row or row[0] is None:
			continue
		first_cell = row[0].strip()
		if not first_cell:
			continue

		rest = row[1:]
		all_rest_empty = all((c is None or str(c).strip() in ("", "-")) for c in rest)

		if VEG_CATEGORY_RE.search(first_cell):
			current_category = first_cell
			continue

		if all_rest_empty:
			continue

		peliyagoda_cell = rest[0] if rest else None
		combined = f"{first_cell} {peliyagoda_cell or ''}".strip()
		m = RANGE_RE.search(combined)

		if m:
			item_name = normalize_peliyagoda_name(combined[: m.start()])
			min_price = float(m.group(1))
			max_price = float(m.group(2))
			avg_price = round((min_price + max_price) / 2, 2)
			records.append(
				{
					"date": date,
					"item": item_name,
					"category": current_category,
					"unit": None,
					"market": "Peliyagoda",
					"min_price": min_price,
					"max_price": max_price,
					"average_price": avg_price,
					"average_computed": True,
				}
			)
		else:
			records.append(
				{
					"date": date,
					"item": normalize_peliyagoda_name(first_cell),
					"category": current_category,
					"unit": None,
					"market": "Peliyagoda",
					"min_price": None,
					"max_price": None,
					"average_price": None,
					"average_computed": True,
				}
			)
	return records


def parse(pdf_bytes: bytes, date: str) -> dict:
	"""Extract Pettah + Peliyagoda prices from one day's PDF bytes.

	Returns {"records": [...], "warnings": [...]}. Each record has commodity_name,
	harti_category, market, date, unit, min_price, max_price, average_price,
	average_computed. Items in IGNORED_ITEMS are filtered out before commodity_name
	resolution. Never writes to disk.
	"""
	pettah_records, peliyagoda_records = [], []
	pettah_page_found, peliyagoda_page_found = False, False

	with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
		for page in pdf.pages:
			if pettah_page_found and peliyagoda_page_found:
				break

			kind, table = classify_page(page)

			if kind == "pettah" and not pettah_page_found:
				pettah_page_found = True
				pettah_records = parse_pettah_page(page.extract_text() or "", date)

			elif kind == "peliyagoda" and not peliyagoda_page_found:
				peliyagoda_page_found = True
				peliyagoda_records = parse_peliyagoda_page(table, date)

	raw_records = [
		r for r in (pettah_records + peliyagoda_records) if (r["category"], r["item"]) not in IGNORED_ITEMS
	]

	records = [
		{
			"commodity_name": commodity_name_for(r["item"], r["category"]),
			"harti_category": r["category"],
			"market": r["market"],
			"date": r["date"],
			"unit": r["unit"],
			"min_price": r["min_price"],
			"max_price": r["max_price"],
			"average_price": r["average_price"],
			"average_computed": r["average_computed"],
		}
		for r in raw_records
	]

	warnings = []
	if not pettah_page_found:
		warnings.append("no Pettah (Rice & Subsidiary Food Crops) table found")
	if not peliyagoda_page_found:
		warnings.append("no Peliyagoda (vegetable) table found")

	return {"records": records, "warnings": warnings}
