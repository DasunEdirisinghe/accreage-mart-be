"""Story 3.12: idempotency regression pass. No new production code - this exercises Stories
3.1-3.11's existing behavior end to end, twice in a row against the same day's fixture data,
and proves nothing duplicates or drifts on a rerun.

Note on frappe.enqueue: it does NOT run synchronously under frappe.flags.in_test by default
(call_directly requires now=True or is_async=False - verified against
frappe/utils/background_jobs.py rather than assumed). So this test mocks frappe.enqueue (same
as every other story's tests) and explicitly calls refresh_price_forecasts(commodities=[...])
itself afterward, scoped to the test commodity only - this genuinely exercises the real
ingest -> close-out/rescore/regenerate -> mirror chain without touching real Redis or the real
65-commodity dataset.
"""

import datetime
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from accreage_mart.pricing import tasks

# commodity_name must actually follow "{category} - {item}" (naming.commodity_name_for) - a
# mismatched harti_category vs commodity_name makes csv_mirror.item_for() raise internally,
# silently caught by refresh_price_forecasts()'s per-commodity failure isolation (which is
# exactly what happened here on the first version of this fixture: every other assertion
# still passed, only the File-count one caught the CSV mirror step being silently skipped).
TEST_CATEGORY = "Rice"
TEST_ITEM = "Test Idempotency"
TEST_COMMODITY = f"{TEST_CATEGORY} - {TEST_ITEM}"


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


class TestIdempotencyRegression(FrappeTestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		self._purge()

	def tearDown(self):
		self._purge()
		frappe.set_user("Administrator")

	def _purge(self):
		for doctype in ("Forecast Accuracy Log", "Commodity Price Record"):
			for rec in frappe.get_all(doctype, filters={"commodity": TEST_COMMODITY}, pluck="name"):
				frappe.delete_doc(doctype, rec, force=True, ignore_permissions=True)
		for f in frappe.get_all(
			"File", filters={"attached_to_doctype": "Commodity", "attached_to_name": TEST_COMMODITY}, pluck="name"
		):
			frappe.delete_doc("File", f, force=True, ignore_permissions=True)
		if frappe.db.exists("Price Forecast", TEST_COMMODITY):
			frappe.delete_doc("Price Forecast", TEST_COMMODITY, force=True, ignore_permissions=True)
		if frappe.db.exists("Commodity", TEST_COMMODITY):
			frappe.delete_doc("Commodity", TEST_COMMODITY, force=True, ignore_permissions=True)
		yesterday = frappe.utils.getdate() - datetime.timedelta(days=1)
		if frappe.db.exists("Price Ingestion Log", yesterday.isoformat()):
			frappe.delete_doc("Price Ingestion Log", yesterday.isoformat(), force=True, ignore_permissions=True)
		frappe.db.commit()

	def _run_full_chain_once(self):
		def fake_download(d, session=None, real_index=None, **kwargs):
			return {"status": "downloaded", "content": b"%PDF-fake", "url": "http://x/y.pdf", "detail": ""}

		def fake_parse(pdf_bytes, date):
			return {
				"records": [
					{
						"commodity_name": TEST_COMMODITY, "harti_category": TEST_CATEGORY, "market": "Pettah",
						"date": date, "unit": "Rs/kg",
						"min_price": 95.0, "max_price": 105.0, "average_price": 100.0,
						"average_computed": False,
					}
				],
				"warnings": [],
			}

		with (
			patch("accreage_mart.pricing.tasks.fetch_real_pdf_index", return_value={}),
			patch("accreage_mart.pricing.tasks.download_latest_pdf", side_effect=fake_download),
			patch("accreage_mart.pricing.tasks.parse_pdf", side_effect=fake_parse),
			patch("accreage_mart.pricing.tasks.frappe.enqueue"),  # never touch real Redis
		):
			tasks.ingest_daily_prices()

		with patch("accreage_mart.pricing.forecasting.Prophet", FakeProphet):
			tasks.refresh_price_forecasts(commodities=[TEST_COMMODITY])

	def test_full_chain_is_idempotent_on_rerun(self):
		today = frappe.utils.getdate()
		yesterday = today - datetime.timedelta(days=1)

		frappe.get_doc(
			{"doctype": "Commodity", "commodity_name": TEST_COMMODITY, "harti_category": TEST_CATEGORY, "market": "Pettah"}
		).insert(ignore_permissions=True)

		# 60 days of history ending 2 days ago, so _next_date_to_attempt() computes exactly
		# one new date (yesterday) to ingest on the first pass, and zero on the second.
		for i in range(60):
			d = today - datetime.timedelta(days=61 - i)  # today-61 .. today-2
			frappe.get_doc(
				{
					"doctype": "Commodity Price Record", "commodity": TEST_COMMODITY, "date": d,
					"min_price": 95.0, "max_price": 105.0, "average_price": 100.0, "average_computed": 0,
				}
			).insert(ignore_permissions=True)

		# A pre-existing forecast targeting yesterday, made before this history existed - once
		# yesterday's real price is ingested on the first pass, close-out should score this
		# (a non-trivial accuracy-log row, not just an empty-both-times comparison).
		frappe.get_doc(
			{
				"doctype": "Price Forecast",
				"commodity": TEST_COMMODITY,
				"generated_on": yesterday,
				"forecast_days": [
					{"forecast_date": yesterday, "horizon_days_ahead": 1, "predicted_price": 105.0,
					 "lower_bound": 95.0, "upper_bound": 115.0}
				],
			}
		).insert(ignore_permissions=True)
		frappe.db.commit()

		self._run_full_chain_once()

		price_records_1 = frappe.get_all("Commodity Price Record", filters={"commodity": TEST_COMMODITY})
		accuracy_logs_1 = frappe.get_all("Forecast Accuracy Log", filters={"commodity": TEST_COMMODITY})
		forecast_1 = frappe.get_doc("Price Forecast", TEST_COMMODITY)
		commodity_1 = frappe.get_doc("Commodity", TEST_COMMODITY)
		ingestion_log_count_1 = frappe.db.count("Price Ingestion Log", {"date": yesterday.isoformat()})

		self.assertEqual(len(price_records_1), 61)  # 60 seeded + 1 newly ingested (yesterday)
		self.assertEqual(len(accuracy_logs_1), 1)  # the pre-existing forecast day, now scoreable
		self.assertEqual(len(forecast_1.forecast_days), 30)  # wholesale-regenerated
		self.assertEqual(ingestion_log_count_1, 1)

		# --- second pass: same day, nothing new to ingest, nothing new to score ---
		self._run_full_chain_once()

		price_records_2 = frappe.get_all("Commodity Price Record", filters={"commodity": TEST_COMMODITY})
		accuracy_logs_2 = frappe.get_all("Forecast Accuracy Log", filters={"commodity": TEST_COMMODITY})
		forecast_2 = frappe.get_doc("Price Forecast", TEST_COMMODITY)
		commodity_2 = frappe.get_doc("Commodity", TEST_COMMODITY)
		ingestion_log_count_2 = frappe.db.count("Price Ingestion Log", {"date": yesterday.isoformat()})

		self.assertEqual(len(price_records_2), len(price_records_1))
		self.assertEqual(len(accuracy_logs_2), len(accuracy_logs_1))
		self.assertEqual(len(forecast_2.forecast_days), 30)
		self.assertEqual(ingestion_log_count_2, 1)  # still exactly one, never two

		self.assertEqual(commodity_2.mape_1_7d, commodity_1.mape_1_7d)
		self.assertEqual(commodity_2.mape_8_14d, commodity_1.mape_8_14d)
		self.assertEqual(commodity_2.mape_15_30d, commodity_1.mape_15_30d)
		self.assertEqual(commodity_2.sample_size_1_7d, commodity_1.sample_size_1_7d)
		self.assertEqual(commodity_2.sample_size_8_14d, commodity_1.sample_size_8_14d)
		self.assertEqual(commodity_2.sample_size_15_30d, commodity_1.sample_size_15_30d)

		# the CSV mirror (Story 3.10, wired as forecasting.py's 4th step) stays a single File
		files = frappe.get_all(
			"File", filters={"attached_to_doctype": "Commodity", "attached_to_name": TEST_COMMODITY}
		)
		self.assertEqual(len(files), 1)
