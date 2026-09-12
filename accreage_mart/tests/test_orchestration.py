import datetime
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from accreage_mart.pricing import tasks

TEST_COMMODITY = "Test Orchestration Rice"
TEST_COMMODITY_OTHER = "Test Orchestration Other"

# Two dedicated dates, never touched by any other test file, so this file's Price Ingestion
# Log cleanup can't collide with real data or another test's fixtures.
DOWNLOAD_FAILURE_DATE = datetime.date(2026, 2, 2)
NOT_FOUND_DATE = datetime.date(2026, 2, 3)


class TestOrchestration(FrappeTestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		self._purge_logs()

	def tearDown(self):
		self._purge_logs()
		frappe.set_user("Administrator")

	def _purge_logs(self):
		# _ingest_one_date -> _log() calls frappe.db.commit() internally, which breaks
		# FrappeTestCase's usual auto-rollback - see test_backfill.py's _purge for the full
		# explanation. This cleanup must commit its own deletes explicitly.
		for d in (DOWNLOAD_FAILURE_DATE, NOT_FOUND_DATE):
			if frappe.db.exists("Price Ingestion Log", d.isoformat()):
				frappe.delete_doc("Price Ingestion Log", d.isoformat(), force=True, ignore_permissions=True)
		# test_download_failure_is_logged_via_log_error's wraps=frappe.log_error call inserts a
		# real Error Log row; _log()'s frappe.db.commit() right after it commits that row too
		# (a db.commit() commits the whole pending transaction, not just its own write) - clean
		# it up explicitly, same transaction-wide-commit gotcha every other pricing test hit.
		# Verified against frappe/utils/error.py: log_error() sets method=title, error=message.
		for name in frappe.get_all(
			"Error Log",
			filters={
				"method": "Price ingestion: download failure",
				"error": ["like", f"%{DOWNLOAD_FAILURE_DATE.isoformat()}%"],
			},
			pluck="name",
		):
			frappe.delete_doc("Error Log", name, force=True, ignore_permissions=True)
		frappe.db.commit()

	# -- a total ingestion failure never prevents the forecast refresh from being enqueued --

	def test_total_ingestion_failure_still_enqueues_refresh(self):
		with (
			patch("accreage_mart.pricing.tasks._run_ingestion", side_effect=RuntimeError("boom")),
			patch("accreage_mart.pricing.tasks.frappe.enqueue") as mock_enqueue,
		):
			tasks.ingest_daily_prices()  # must not raise

		mock_enqueue.assert_called_once_with(
			"accreage_mart.pricing.tasks.refresh_price_forecasts", queue="long"
		)

	def test_total_ingestion_failure_is_logged(self):
		# wraps=frappe.log_error means the REAL function still runs (writes a real Error Log
		# entry, exactly as it would in production) - this only ADDS the ability to assert on
		# the call, it never silently swallows the real behavior the way a bare Mock() would.
		with (
			patch("accreage_mart.pricing.tasks._run_ingestion", side_effect=RuntimeError("boom")),
			patch("accreage_mart.pricing.tasks.frappe.enqueue"),
			patch("accreage_mart.pricing.tasks.frappe.log_error", wraps=frappe.log_error) as mock_log_error,
		):
			tasks.ingest_daily_prices()

		mock_log_error.assert_called_once()
		self.assertIn("boom", mock_log_error.call_args.kwargs.get("message", ""))

	# -- per-date failures: "failed" is logged via frappe.log_error, "not_found" is not --

	def test_download_failure_is_logged_via_log_error(self):
		with (
			patch(
				"accreage_mart.pricing.tasks.download_latest_pdf",
				return_value={"status": "failed", "content": None, "url": None, "detail": "network error"},
			),
			patch("accreage_mart.pricing.tasks.frappe.log_error", wraps=frappe.log_error) as mock_log_error,
		):
			tasks._ingest_one_date(DOWNLOAD_FAILURE_DATE, real_index={}, session=MagicMock())

		mock_log_error.assert_called_once()
		self.assertIn(DOWNLOAD_FAILURE_DATE.isoformat(), mock_log_error.call_args.kwargs.get("message", ""))
		log = frappe.get_doc("Price Ingestion Log", DOWNLOAD_FAILURE_DATE.isoformat())
		self.assertEqual(log.status, "failed")

	def test_not_found_does_not_call_log_error(self):
		# Expected/normal (HARTI simply didn't publish that day) - not alarming, shouldn't
		# clutter the Error Log list the way a genuine failure should.
		with (
			patch(
				"accreage_mart.pricing.tasks.download_latest_pdf",
				return_value={"status": "not_found", "content": None, "url": None, "detail": "no PDF found"},
			),
			patch("accreage_mart.pricing.tasks.frappe.log_error", wraps=frappe.log_error) as mock_log_error,
		):
			tasks._ingest_one_date(NOT_FOUND_DATE, real_index={}, session=MagicMock())

		mock_log_error.assert_not_called()
		log = frappe.get_doc("Price Ingestion Log", NOT_FOUND_DATE.isoformat())
		self.assertEqual(log.status, "not_found")

	# -- per-commodity refresh failure (Story 3.7) is logged, identifiable by commodity name --

	def test_per_commodity_refresh_failure_logs_with_commodity_name(self):
		def fake_refresh(commodity):
			if commodity == TEST_COMMODITY:
				raise ValueError("simulated failure")
			# TEST_COMMODITY_OTHER succeeds silently (no-op) - never touches real data since
			# refresh_commodity_forecast itself is mocked here, not just its Prophet call.

		with (
			patch(
				"accreage_mart.pricing.forecasting.refresh_commodity_forecast", side_effect=fake_refresh
			),
			patch("accreage_mart.pricing.tasks.frappe.log_error", wraps=frappe.log_error) as mock_log_error,
		):
			tasks.refresh_price_forecasts(commodities=[TEST_COMMODITY, TEST_COMMODITY_OTHER])

		mock_log_error.assert_called_once()
		self.assertIn(TEST_COMMODITY, mock_log_error.call_args.kwargs.get("message", ""))
