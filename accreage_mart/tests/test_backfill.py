import csv
import shutil
import tempfile
from pathlib import Path

import frappe
from frappe.tests.utils import FrappeTestCase

from accreage_mart.pricing.backfill import run as run_backfill

FIELDS = ["ds", "y", "item", "category", "market", "unit", "min_price", "max_price", "average_computed"]

# Two commodities with the SAME item text ("Imported") but different categories - regression
# coverage for the naming collision found while building this story (see pricing/naming.py).
FIXTURE_ROWS = {
	"onion_imported.csv": [
		{"ds": "2024-01-01", "y": "100.0", "item": "Imported", "category": "Onion", "market": "Pettah",
		 "unit": "Rs/kg", "min_price": "95.0", "max_price": "105.0", "average_computed": "False"},
		{"ds": "2024-01-02", "y": "102.0", "item": "Imported", "category": "Onion", "market": "Pettah",
		 "unit": "Rs/kg", "min_price": "97.0", "max_price": "107.0", "average_computed": "False"},
	],
	"big_onion_imported.csv": [
		{"ds": "2024-01-01", "y": "150.0", "item": "Imported", "category": "Big Onion", "market": "Pettah",
		 "unit": "Rs/kg", "min_price": "145.0", "max_price": "155.0", "average_computed": "False"},
	],
}


class TestBackfill(FrappeTestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		self.tmpdir = Path(tempfile.mkdtemp())
		for filename, rows in FIXTURE_ROWS.items():
			with open(self.tmpdir / filename, "w", newline="", encoding="utf-8") as f:
				writer = csv.DictWriter(f, fieldnames=FIELDS)
				writer.writeheader()
				writer.writerows(rows)
		self._purge()

	def tearDown(self):
		self._purge()
		shutil.rmtree(self.tmpdir, ignore_errors=True)

	def _purge(self):
		# run_backfill() calls frappe.db.commit() internally (correct for the real one-off
		# script), which breaks FrappeTestCase's usual auto-rollback for this test - so this
		# cleanup must commit its own deletes explicitly, or they'd be undone by the outer
		# rollback while the earlier commit's inserts survive, leaking test fixtures into the
		# real database.
		for name in ("Onion - Imported", "Big Onion - Imported"):
			for rec in frappe.get_all("Commodity Price Record", filters={"commodity": name}, pluck="name"):
				frappe.delete_doc("Commodity Price Record", rec, force=True, ignore_permissions=True)
			if frappe.db.exists("Commodity", name):
				frappe.delete_doc("Commodity", name, force=True, ignore_permissions=True)
		frappe.db.commit()

	def test_backfill_creates_commodities_and_records(self):
		result = run_backfill(csv_dir=str(self.tmpdir))
		self.assertEqual(result["commodities_created"], 2)
		self.assertEqual(result["records_created"], 3)
		self.assertEqual(result["records_skipped"], 0)

		# the naming-collision fix: same "item" text, different "category" -> two distinct Commodities
		self.assertTrue(frappe.db.exists("Commodity", "Onion - Imported"))
		self.assertTrue(frappe.db.exists("Commodity", "Big Onion - Imported"))

		onion_avg = frappe.db.get_value("Commodity Price Record", "Onion - Imported-2024-01-01", "average_price")
		big_onion_avg = frappe.db.get_value(
			"Commodity Price Record", "Big Onion - Imported-2024-01-01", "average_price"
		)
		self.assertEqual(onion_avg, 100.0)
		self.assertEqual(big_onion_avg, 150.0)

	def test_backfill_is_idempotent(self):
		run_backfill(csv_dir=str(self.tmpdir))
		second = run_backfill(csv_dir=str(self.tmpdir))
		self.assertEqual(second["commodities_created"], 0)
		self.assertEqual(second["records_created"], 0)
		self.assertEqual(second["records_skipped"], 3)
