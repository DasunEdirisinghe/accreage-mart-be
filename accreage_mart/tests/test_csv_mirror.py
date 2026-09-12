import csv
import datetime
import io
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from accreage_mart.pricing import csv_mirror, forecasting

TEST_CATEGORY = "Test Mirror Category"
TEST_ITEM = "Test Mirror Item"
TEST_COMMODITY = f"{TEST_CATEGORY} - {TEST_ITEM}"  # matches naming.commodity_name_for()

TEST_CATEGORY_OTHER = "Test Mirror Category Other"
TEST_ITEM_OTHER = "Test Mirror Item Other"
TEST_COMMODITY_OTHER = f"{TEST_CATEGORY_OTHER} - {TEST_ITEM_OTHER}"


class TestCsvMirror(FrappeTestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		self._purge()

	def tearDown(self):
		self._purge()
		frappe.set_user("Administrator")

	def _purge(self):
		# generate_for_commodity() calls frappe.db.commit() internally (correct for the real
		# daily job), which breaks FrappeTestCase's usual auto-rollback - see
		# test_backfill.py's _purge for the full explanation. This cleanup must commit its own
		# deletes explicitly.
		for name in (TEST_COMMODITY, TEST_COMMODITY_OTHER):
			for file_name in frappe.get_all(
				"File", filters={"attached_to_doctype": "Commodity", "attached_to_name": name}, pluck="name"
			):
				frappe.delete_doc("File", file_name, force=True, ignore_permissions=True)
			for doctype in ("Forecast Accuracy Log", "Commodity Price Record"):
				for rec in frappe.get_all(doctype, filters={"commodity": name}, pluck="name"):
					frappe.delete_doc(doctype, rec, force=True, ignore_permissions=True)
			if frappe.db.exists("Price Forecast", name):
				frappe.delete_doc("Price Forecast", name, force=True, ignore_permissions=True)
			if frappe.db.exists("Commodity", name):
				frappe.delete_doc("Commodity", name, force=True, ignore_permissions=True)
		frappe.db.commit()

	def _seed_commodity_with_history(self, name, category, num_days=5, price=100.0):
		frappe.get_doc(
			{
				"doctype": "Commodity",
				"commodity_name": name,
				"harti_category": category,
				"market": "Peliyagoda",
				"unit": "kg",
			}
		).insert(ignore_permissions=True)

		today = frappe.utils.getdate()
		dates = []
		for i in range(num_days):
			d = today - datetime.timedelta(days=num_days - i)
			dates.append(d)
			frappe.get_doc(
				{
					"doctype": "Commodity Price Record",
					"commodity": name,
					"date": d,
					"min_price": price - 5,
					"max_price": price + 5,
					"average_price": price,
					"average_computed": 0,
				}
			).insert(ignore_permissions=True)
		frappe.db.commit()
		return dates

	def _read_csv(self, commodity):
		file_doc = frappe.get_doc(
			"File",
			frappe.db.get_value(
				"File",
				{
					"attached_to_doctype": "Commodity",
					"attached_to_name": commodity,
					"file_name": f"{commodity}.csv",
				},
			),
		)
		content = file_doc.get_content()
		if isinstance(content, bytes):
			content = content.decode("utf-8")
		return list(csv.reader(io.StringIO(content)))

	def test_generate_for_commodity_is_idempotent_one_file_record(self):
		self._seed_commodity_with_history(TEST_COMMODITY, TEST_CATEGORY, num_days=5)

		first_file = csv_mirror.generate_for_commodity(TEST_COMMODITY)
		second_file = csv_mirror.generate_for_commodity(TEST_COMMODITY)

		self.assertEqual(first_file, second_file)
		matches = frappe.get_all(
			"File",
			filters={"attached_to_doctype": "Commodity", "attached_to_name": TEST_COMMODITY},
		)
		self.assertEqual(len(matches), 1)

	def test_generated_csv_header_and_rows_match_schema_for_full_history(self):
		dates = self._seed_commodity_with_history(TEST_COMMODITY, TEST_CATEGORY, num_days=7, price=250.0)

		csv_mirror.generate_for_commodity(TEST_COMMODITY)

		rows = self._read_csv(TEST_COMMODITY)
		header, data_rows = rows[0], rows[1:]

		self.assertEqual(header, csv_mirror.CSV_COLUMNS)
		self.assertEqual(len(data_rows), len(dates))  # full history, not just recent

		first = dict(zip(header, data_rows[0]))
		self.assertEqual(first["ds"], str(dates[0]))
		self.assertAlmostEqual(float(first["y"]), 250.0, places=2)
		self.assertEqual(first["item"], TEST_ITEM)
		self.assertEqual(first["category"], TEST_CATEGORY)
		self.assertEqual(first["market"], "Peliyagoda")
		self.assertEqual(first["unit"], "kg")
		self.assertAlmostEqual(float(first["min_price"]), 245.0, places=2)
		self.assertAlmostEqual(float(first["max_price"]), 255.0, places=2)
		self.assertEqual(first["average_computed"], "False")

	def test_regenerating_reflects_newly_ingested_rows(self):
		self._seed_commodity_with_history(TEST_COMMODITY, TEST_CATEGORY, num_days=3)
		csv_mirror.generate_for_commodity(TEST_COMMODITY)
		rows_before = self._read_csv(TEST_COMMODITY)

		frappe.get_doc(
			{
				"doctype": "Commodity Price Record",
				"commodity": TEST_COMMODITY,
				"date": frappe.utils.getdate(),
				"min_price": 95.0,
				"max_price": 105.0,
				"average_price": 100.0,
				"average_computed": 0,
			}
		).insert(ignore_permissions=True)
		frappe.db.commit()

		csv_mirror.generate_for_commodity(TEST_COMMODITY)
		rows_after = self._read_csv(TEST_COMMODITY)

		self.assertEqual(len(rows_after) - 1, len(rows_before) - 1 + 1)  # one new data row

	def test_generate_all_with_explicit_commodities_writes_one_csv_each(self):
		self._seed_commodity_with_history(TEST_COMMODITY, TEST_CATEGORY, num_days=4)
		self._seed_commodity_with_history(TEST_COMMODITY_OTHER, TEST_CATEGORY_OTHER, num_days=6)

		result = csv_mirror.generate_all(commodities=[TEST_COMMODITY, TEST_COMMODITY_OTHER])

		self.assertEqual(result["generated"], 2)
		self.assertEqual(len(self._read_csv(TEST_COMMODITY)) - 1, 4)
		self.assertEqual(len(self._read_csv(TEST_COMMODITY_OTHER)) - 1, 6)

	def test_refresh_commodity_forecast_also_refreshes_the_mirror(self):
		# Story 3.7's orchestrator (forecasting.refresh_commodity_forecast) must call
		# generate_for_commodity as its 4th step, keeping the mirror current on every daily
		# run - not only when csv_mirror is invoked directly.
		class FakeProphet:
			def __init__(self, *args, **kwargs):
				pass

			def fit(self, df):
				return self

			def predict(self, future):
				out = future.copy()
				out["yhat"] = 100.0
				out["yhat_lower"] = 90.0
				out["yhat_upper"] = 110.0
				return out

		self._seed_commodity_with_history(TEST_COMMODITY, TEST_CATEGORY, num_days=60, price=100.0)

		with patch("accreage_mart.pricing.forecasting.Prophet", FakeProphet):
			forecasting.refresh_commodity_forecast(TEST_COMMODITY)

		matches = frappe.get_all(
			"File",
			filters={"attached_to_doctype": "Commodity", "attached_to_name": TEST_COMMODITY},
		)
		self.assertEqual(len(matches), 1)
		self.assertEqual(len(self._read_csv(TEST_COMMODITY)) - 1, 60)

	def test_generate_for_commodity_files_land_in_shared_folder(self):
		self._seed_commodity_with_history(TEST_COMMODITY, TEST_CATEGORY, num_days=2)

		csv_mirror.generate_for_commodity(TEST_COMMODITY)

		file_name = frappe.db.get_value(
			"File",
			{"attached_to_doctype": "Commodity", "attached_to_name": TEST_COMMODITY, "file_name": f"{TEST_COMMODITY}.csv"},
			"folder",
		)
		self.assertEqual(file_name, csv_mirror.FOLDER_PATH)
