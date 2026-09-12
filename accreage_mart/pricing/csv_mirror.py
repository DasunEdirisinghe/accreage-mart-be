"""Per-commodity CSV mirror: writes a commodity's ENTIRE Commodity Price Record history (its
full 2024-to-today date range, not just what changed today) to a CSV attached to its Commodity
document, in the shared "Commodity Datasets" File Manager folder - same schema as the
standalone dataset-manager pipeline's prophet_data/<item>.csv output.

Purely for dataset-visibility during evaluation, not a second source of truth - the daily job
and the read APIs always read Commodity Price Record directly, never this file.

Invoked two ways:
  - Once for every commodity as the last step of the one-off bootstrap sequence, right after
	the Story 3.2 backfill and Story 3.6 bootstrap accuracy:
		bench --site <site> execute accreage_mart.pricing.csv_mirror.generate_all
  - Again from each day's refresh (Story 3.7's refresh_commodity_forecast), to keep it current.
"""

import csv
import io

import frappe

from accreage_mart.pricing.naming import item_for

FOLDER_NAME = "Commodity Datasets"
FOLDER_PATH = f"Home/{FOLDER_NAME}"

# Matches the standalone pipeline's prophet_data/<item>.csv schema exactly.
CSV_COLUMNS = ["ds", "y", "item", "category", "market", "unit", "min_price", "max_price", "average_computed"]


def _ensure_folder() -> str:
	"""Creates the shared "Commodity Datasets" File folder under Home if it doesn't already
	exist, returns its name (docname)."""
	if frappe.db.exists("File", FOLDER_PATH):
		return FOLDER_PATH
	folder = frappe.get_doc({"doctype": "File", "file_name": FOLDER_NAME, "is_folder": 1, "folder": "Home"})
	folder.insert(ignore_permissions=True)
	return folder.name


def _csv_content_for(commodity_doc) -> bytes:
	category = commodity_doc.harti_category or ""
	item = item_for(commodity_doc.commodity_name, category)

	rows = frappe.get_all(
		"Commodity Price Record",
		filters={"commodity": commodity_doc.name},
		fields=["date", "average_price", "min_price", "max_price", "average_computed"],
		order_by="date asc",
	)

	buf = io.StringIO()
	writer = csv.DictWriter(buf, fieldnames=CSV_COLUMNS)
	writer.writeheader()
	for r in rows:
		writer.writerow(
			{
				"ds": r.date,
				"y": r.average_price,
				"item": item,
				"category": category,
				"market": commodity_doc.market,
				"unit": commodity_doc.unit or "",
				"min_price": r.min_price,
				"max_price": r.max_price,
				"average_computed": bool(r.average_computed),
			}
		)
	return buf.getvalue().encode("utf-8")


def generate_for_commodity(commodity: str) -> str:
	"""Regenerates commodity's full-history CSV and writes it to its attached File. Overwrites
	the existing File in place (same filename, same docname) when one already exists rather
	than inserting a second File record - safe to call repeatedly. Returns the File docname.
	"""
	doc = frappe.get_doc("Commodity", commodity)
	content = _csv_content_for(doc)
	file_name = f"{commodity}.csv"

	existing = frappe.db.get_value(
		"File",
		{"attached_to_doctype": "Commodity", "attached_to_name": commodity, "file_name": file_name},
	)
	if existing:
		file_doc = frappe.get_doc("File", existing)
		# save_file(overwrite=True) rewrites the file at its EXISTING file_name/file_url on
		# disk instead of Frappe's default new-insert behavior of content-hash-deduping or
		# suffixing the name - this is what makes it a true in-place overwrite rather than a
		# second File record. Document.save() alone would not rewrite disk content at all;
		# there is no on_update hook wired to re-run save_file() for an existing File doc.
		file_doc.save_file(content=content, overwrite=True, ignore_existing_file_check=True)
		file_doc.save(ignore_permissions=True)
	else:
		file_doc = frappe.get_doc(
			{
				"doctype": "File",
				"file_name": file_name,
				"attached_to_doctype": "Commodity",
				"attached_to_name": commodity,
				"folder": _ensure_folder(),
				"is_private": 0,
				"content": content,
			}
		)
		file_doc.insert(ignore_permissions=True)

	frappe.db.commit()
	return file_doc.name


def generate_all(commodities=None) -> dict:
	"""Regenerates the mirror for every active Commodity by default; exposed as a bench execute
	entrypoint, run once as the last step of the one-off bootstrap sequence so the folder holds
	every commodity's full 2024-to-today CSV immediately, not a day later.

	commodities is normally looked up from the DB; tests pass an explicit list instead, so a
	test run never touches the real 65-commodity dataset's files (same pattern as Story 3.6's
	bootstrap_all and Story 3.7's refresh_price_forecasts)."""
	if commodities is None:
		commodities = frappe.get_all("Commodity", filters={"is_active": 1}, pluck="name")

	generated = 0
	for commodity in commodities:
		generate_for_commodity(commodity)
		generated += 1

	print(f"CSV mirror done: {generated} commodities written to '{FOLDER_NAME}'.")
	return {"generated": generated}
