"""One-time historical backfill: reads the standalone dataset-manager pipeline's
prophet_data/*.csv files (06_price_dataset/prophet_data) into Commodity + Commodity Price
Record.

Idempotent - safe to re-run; only ever inserts a Commodity or Commodity Price Record that
doesn't already exist. Not scheduled - run once via bench execute:

	bench --site <site> execute accreage_mart.pricing.backfill.run \\
		--kwargs "{'csv_dir': '/path/to/06_price_dataset/prophet_data'}"
"""

import csv
from pathlib import Path

import frappe

from accreage_mart.pricing.naming import commodity_name_for

# Every CSV in prophet_data/ matches this schema; ignore any file that doesn't have the
# expected header (e.g. conversion_log.csv, which lives in the same directory).
REQUIRED_COLUMNS = {"ds", "y", "item", "category", "market", "unit", "min_price", "max_price", "average_computed"}


def run(csv_dir=None):
	if not csv_dir:
		frappe.throw(
			"csv_dir is required, e.g. bench execute accreage_mart.pricing.backfill.run "
			"--kwargs \"{'csv_dir': '/path/to/06_price_dataset/prophet_data'}\""
		)

	csv_dir = Path(csv_dir)
	if not csv_dir.is_dir():
		frappe.throw(f"{csv_dir} is not a directory")

	commodities_created = 0
	records_created = 0
	records_skipped = 0
	files_skipped = []

	for csv_path in sorted(csv_dir.glob("*.csv")):
		with open(csv_path, newline="", encoding="utf-8") as f:
			reader = csv.DictReader(f)
			if not REQUIRED_COLUMNS.issubset(set(reader.fieldnames or [])):
				files_skipped.append(csv_path.name)
				continue
			rows = list(reader)

		if not rows:
			continue

		first = rows[0]
		commodity_name = commodity_name_for(first["item"], first["category"])

		if not frappe.db.exists("Commodity", commodity_name):
			frappe.get_doc(
				{
					"doctype": "Commodity",
					"commodity_name": commodity_name,
					"harti_category": first["category"],
					"market": first["market"],
					"unit": first.get("unit") or "",
				}
			).insert(ignore_permissions=True)
			commodities_created += 1

		for row in rows:
			date = row["ds"]
			record_name = f"{commodity_name}-{date}"
			if frappe.db.exists("Commodity Price Record", record_name):
				records_skipped += 1
				continue

			frappe.get_doc(
				{
					"doctype": "Commodity Price Record",
					"commodity": commodity_name,
					"date": date,
					"min_price": _to_float(row.get("min_price")),
					"max_price": _to_float(row.get("max_price")),
					"average_price": _to_float(row.get("y")),
					"average_computed": 1 if _to_bool(row.get("average_computed")) else 0,
				}
			).insert(ignore_permissions=True)
			records_created += 1

		frappe.db.commit()

	summary = {
		"commodities_created": commodities_created,
		"records_created": records_created,
		"records_skipped": records_skipped,
		"files_skipped": files_skipped,
	}
	print(
		f"Backfill done: {commodities_created} commodities created, "
		f"{records_created} price records created, {records_skipped} already existed"
		+ (f", {len(files_skipped)} non-price CSV(s) skipped: {files_skipped}" if files_skipped else ".")
	)
	return summary


def _to_float(value):
	if value in (None, ""):
		return None
	return float(value)


def _to_bool(value):
	return str(value).strip().lower() in ("true", "1")
