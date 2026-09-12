import datetime
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from accreage_mart.pricing import tasks

TEST_COMMODITY = "Rice - Test Ingestion Nadu"
TODAY = frappe.utils.getdate()


class TestIngestDailyPrices(FrappeTestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		self._purge()

	def tearDown(self):
		self._purge()
		frappe.set_user("Administrator")

	def _purge(self):
		# tasks._log() (called via _ingest_one_date) calls frappe.db.commit() internally,
		# which breaks FrappeTestCase's usual auto-rollback - see test_backfill.py's _purge
		# for the full explanation. This cleanup must commit its own deletes explicitly.
		for rec in frappe.get_all(
			"Commodity Price Record", filters={"commodity": TEST_COMMODITY}, pluck="name"
		):
			frappe.delete_doc("Commodity Price Record", rec, force=True, ignore_permissions=True)
		if frappe.db.exists("Commodity", TEST_COMMODITY):
			frappe.delete_doc("Commodity", TEST_COMMODITY, force=True, ignore_permissions=True)
		for iso_date in ("2026-01-01", "2026-01-02", "2026-01-03", "2026-01-04"):
			if frappe.db.exists("Price Ingestion Log", iso_date):
				frappe.delete_doc("Price Ingestion Log", iso_date, force=True, ignore_permissions=True)
		frappe.db.commit()

	# -- date-range logic (max_date injected directly - no DB monkeypatching needed) --

	def test_next_date_to_attempt_uses_given_max_date(self):
		start = tasks._next_date_to_attempt(datetime.date(2026, 9, 5), max_date="2026-09-01")
		self.assertEqual(start, datetime.date(2026, 9, 2))

	def test_next_date_to_attempt_floors_at_max_lookback_when_no_data(self):
		# max_date="" (falsy, not None) simulates an empty table without falling through to
		# the real DB query - max_date=None would mean "no override, use the real lookup,"
		# which in a shared dev DB may already have real backfilled data.
		today = datetime.date(2026, 9, 5)
		start = tasks._next_date_to_attempt(today, max_date="")
		self.assertEqual(start, today - datetime.timedelta(days=tasks.MAX_LOOKBACK_DAYS))

	# -- per-date ingestion --

	def test_ingest_one_date_downloaded_and_parsed_creates_records(self):
		target_date = datetime.date(2026, 1, 1)
		fake_result = {
			"records": [
				{
					"commodity_name": TEST_COMMODITY, "harti_category": "Rice", "market": "Pettah",
					"date": target_date.isoformat(), "unit": "Rs/kg",
					"min_price": 100.0, "max_price": 110.0, "average_price": 105.0,
					"average_computed": False,
				},
				{
					# null-price row must be skipped entirely, not stored as a null row
					"commodity_name": TEST_COMMODITY, "harti_category": "Rice", "market": "Pettah",
					"date": target_date.isoformat(), "unit": "Rs/kg",
					"min_price": None, "max_price": None, "average_price": None,
					"average_computed": False,
				},
			],
			"warnings": [],
		}

		with (
			patch(
				"accreage_mart.pricing.tasks.download_latest_pdf",
				return_value={"status": "downloaded", "content": b"%PDF-fake", "url": "http://x/y.pdf", "detail": ""},
			),
			patch("accreage_mart.pricing.tasks.parse_pdf", return_value=fake_result),
		):
			tasks._ingest_one_date(target_date, real_index={}, session=MagicMock())

		self.assertTrue(frappe.db.exists("Commodity", TEST_COMMODITY))
		self.assertTrue(
			frappe.db.exists("Commodity Price Record", f"{TEST_COMMODITY}-{target_date.isoformat()}")
		)
		log = frappe.get_doc("Price Ingestion Log", target_date.isoformat())
		self.assertEqual(log.status, "parsed")
		self.assertEqual(log.commodities_updated_count, 1)

	def test_ingest_one_date_not_found_logs_cleanly(self):
		target_date = datetime.date(2026, 1, 2)
		with patch(
			"accreage_mart.pricing.tasks.download_latest_pdf",
			return_value={"status": "not_found", "content": None, "url": None, "detail": "no PDF found"},
		):
			tasks._ingest_one_date(target_date, real_index={}, session=MagicMock())

		log = frappe.get_doc("Price Ingestion Log", target_date.isoformat())
		self.assertEqual(log.status, "not_found")
		self.assertEqual(log.commodities_updated_count, 0)

	def test_ingest_one_date_parse_error_logs_failed_without_raising(self):
		target_date = datetime.date(2026, 1, 3)
		with (
			patch(
				"accreage_mart.pricing.tasks.download_latest_pdf",
				return_value={"status": "downloaded", "content": b"%PDF-fake", "url": "http://x/y.pdf", "detail": ""},
			),
			patch("accreage_mart.pricing.tasks.parse_pdf", side_effect=ValueError("boom")),
		):
			tasks._ingest_one_date(target_date, real_index={}, session=MagicMock())  # must not raise

		log = frappe.get_doc("Price Ingestion Log", target_date.isoformat())
		self.assertEqual(log.status, "failed")
		self.assertIn("boom", log.detail)

	def test_upsert_price_record_is_idempotent(self):
		rec = {
			"commodity_name": TEST_COMMODITY, "harti_category": "Rice", "market": "Pettah",
			"date": "2026-01-04", "unit": "Rs/kg",
			"min_price": 100.0, "max_price": 110.0, "average_price": 105.0,
			"average_computed": False,
		}
		tasks._upsert_price_record(rec)
		tasks._upsert_price_record(rec)  # second call must not raise or duplicate

		matches = frappe.get_all(
			"Commodity Price Record", filters={"commodity": TEST_COMMODITY, "date": "2026-01-04"}
		)
		self.assertEqual(len(matches), 1)

	# -- orchestration --

	def test_ingest_daily_prices_always_enqueues_refresh(self):
		with (
			patch("accreage_mart.pricing.tasks._next_date_to_attempt", return_value=TODAY),
			patch("accreage_mart.pricing.tasks.frappe.enqueue") as mock_enqueue,
		):
			# start_date (today) > end_date (yesterday) -> the date loop is skipped entirely,
			# and the refresh job must still be enqueued.
			tasks.ingest_daily_prices()

		mock_enqueue.assert_called_once_with(
			"accreage_mart.pricing.tasks.refresh_price_forecasts", queue="long"
		)
