import datetime
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from accreage_mart.pricing import forecasting, tasks

TEST_COMMODITY = "Test Forecast Refresh Rice"
TEST_COMMODITY_OTHER = "Test Forecast Refresh Other"


class FakeProphet:
	"""fit() is a no-op; predict() always returns a fixed yhat/yhat_lower/yhat_upper band,
	so the regenerate step's math is exactly checkable without a real cmdstan fit."""

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


class TestRefreshPriceForecasts(FrappeTestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		self._purge()

	def tearDown(self):
		self._purge()
		frappe.set_user("Administrator")

	def _purge(self):
		# forecasting.py's functions all call frappe.db.commit() internally (correct for the
		# real daily job), which breaks FrappeTestCase's usual auto-rollback - see
		# test_backfill.py's _purge for the full explanation. This cleanup must commit its
		# own deletes explicitly.
		for name in (TEST_COMMODITY, TEST_COMMODITY_OTHER):
			for doctype in ("Forecast Accuracy Log", "Commodity Price Record"):
				for rec in frappe.get_all(doctype, filters={"commodity": name}, pluck="name"):
					frappe.delete_doc(doctype, rec, force=True, ignore_permissions=True)
			if frappe.db.exists("Price Forecast", name):
				frappe.delete_doc("Price Forecast", name, force=True, ignore_permissions=True)
			if frappe.db.exists("Commodity", name):
				frappe.delete_doc("Commodity", name, force=True, ignore_permissions=True)
		frappe.db.commit()

	def _seed_commodity_with_history(self, name, num_days=60, price=100.0):
		frappe.get_doc(
			{"doctype": "Commodity", "commodity_name": name, "market": "Peliyagoda"}
		).insert(ignore_permissions=True)
		today = frappe.utils.getdate()
		for i in range(num_days):
			d = today - datetime.timedelta(days=num_days - i)
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

	def _seed_price_forecast(self, name, day_specs):
		"""day_specs: list of (forecast_date, predicted_price) tuples."""
		doc = frappe.get_doc(
			{
				"doctype": "Price Forecast",
				"commodity": name,
				"generated_on": frappe.utils.today(),
				"forecast_days": [
					{
						"forecast_date": d,
						"horizon_days_ahead": i + 1,
						"predicted_price": predicted,
						"lower_bound": predicted - 10,
						"upper_bound": predicted + 10,
					}
					for i, (d, predicted) in enumerate(day_specs)
				],
			}
		)
		doc.insert(ignore_permissions=True)
		frappe.db.commit()
		return doc

	# -- close-out --

	def test_close_out_scores_realized_prediction_and_is_idempotent(self):
		yesterday = frappe.utils.getdate() - datetime.timedelta(days=1)
		self._seed_commodity_with_history(TEST_COMMODITY, num_days=5, price=100.0)
		self._seed_price_forecast(TEST_COMMODITY, [(yesterday, 105.0)])

		forecasting._close_out_realized_predictions(TEST_COMMODITY)

		log_name = f"{TEST_COMMODITY}-{yesterday}"
		self.assertTrue(frappe.db.exists("Forecast Accuracy Log", log_name))
		log = frappe.get_doc("Forecast Accuracy Log", log_name)
		self.assertAlmostEqual(log.pct_error, 5.0, places=2)  # |100-105|/100*100
		self.assertEqual(log.horizon_days_ahead, 1)

		forecasting._close_out_realized_predictions(TEST_COMMODITY)  # must not duplicate
		matches = frappe.get_all("Forecast Accuracy Log", filters={"commodity": TEST_COMMODITY})
		self.assertEqual(len(matches), 1)

	def test_close_out_skips_when_actual_not_yet_ingested(self):
		far_past = frappe.utils.getdate() - datetime.timedelta(days=200)
		self._seed_commodity_with_history(TEST_COMMODITY, num_days=5, price=100.0)
		# forecast day for a date with NO matching Commodity Price Record
		self._seed_price_forecast(TEST_COMMODITY, [(far_past, 105.0)])

		forecasting._close_out_realized_predictions(TEST_COMMODITY)  # must not raise

		matches = frappe.get_all("Forecast Accuracy Log", filters={"commodity": TEST_COMMODITY})
		self.assertEqual(len(matches), 0)

	# -- rescore --

	def test_rescore_computes_median_mape_per_bucket(self):
		self._seed_commodity_with_history(TEST_COMMODITY, num_days=5)
		today = frappe.utils.getdate()
		for i, pct_error in enumerate([4.0, 6.0, 5.0]):  # median = 5.0
			frappe.get_doc(
				{
					"doctype": "Forecast Accuracy Log",
					"commodity": TEST_COMMODITY,
					"forecast_date": today - datetime.timedelta(days=i + 1),
					"horizon_days_ahead": i + 1,  # all land in the 1-7d bucket
					"predicted_price": 100.0,
					"actual_price": 100.0,
					"pct_error": pct_error,
					"evaluated_on": today,
				}
			).insert(ignore_permissions=True)
		frappe.db.commit()

		forecasting._rescore_rolling_mape(TEST_COMMODITY)

		commodity_doc = frappe.get_doc("Commodity", TEST_COMMODITY)
		self.assertAlmostEqual(commodity_doc.mape_1_7d, 5.0, places=2)
		self.assertEqual(commodity_doc.sample_size_1_7d, 3)
		self.assertEqual(commodity_doc.last_evaluated_on, today)

	# -- regenerate --

	def test_regenerate_forecast_creates_thirty_sequential_days_with_recalibrated_bounds(self):
		self._seed_commodity_with_history(TEST_COMMODITY, num_days=60, price=100.0)
		recalibration_factor = frappe.db.get_single_value(
			"Price Suggestion Settings", "interval_recalibration_factor"
		)

		with patch("accreage_mart.pricing.forecasting.Prophet", FakeProphet):
			forecasting._regenerate_forecast(TEST_COMMODITY)

		doc = frappe.get_doc("Price Forecast", TEST_COMMODITY)
		self.assertEqual(len(doc.forecast_days), 30)
		self.assertEqual(doc.forecast_days[0].horizon_days_ahead, 1)
		self.assertEqual(doc.forecast_days[-1].horizon_days_ahead, 30)
		self.assertEqual(
			doc.forecast_days[1].forecast_date - doc.forecast_days[0].forecast_date,
			datetime.timedelta(days=1),
		)

		expected_half_width = (110.0 - 90.0) / 2 * recalibration_factor
		self.assertAlmostEqual(doc.forecast_days[0].predicted_price, 100.0, places=2)
		self.assertAlmostEqual(
			doc.forecast_days[0].lower_bound, 100.0 - expected_half_width, places=2
		)
		self.assertAlmostEqual(
			doc.forecast_days[0].upper_bound, 100.0 + expected_half_width, places=2
		)

	def test_regenerate_forecast_does_nothing_with_too_little_history(self):
		self._seed_commodity_with_history(TEST_COMMODITY, num_days=10)

		with patch("accreage_mart.pricing.forecasting.Prophet", FakeProphet):
			forecasting._regenerate_forecast(TEST_COMMODITY)  # must not raise

		self.assertFalse(frappe.db.exists("Price Forecast", TEST_COMMODITY))

	# -- orchestration --

	def test_refresh_price_forecasts_isolates_per_commodity_failure(self):
		# Deliberately does not mock frappe.log_error - that's a shared, globally-used
		# function (same category of risk as Story 3.4's frappe.db.get_value lesson), and
		# asserting on Error Log's exact field names would be guessing at Frappe internals
		# without verifying them (already wrong twice this session - see Story 3.6/3.7's
		# other fixes). What actually matters here, and is safe to assert precisely: the
		# call never raises, and a failure on one commodity doesn't stop the next one.
		self._seed_commodity_with_history(TEST_COMMODITY, num_days=60)
		self._seed_commodity_with_history(TEST_COMMODITY_OTHER, num_days=60)

		called = []

		def fake_refresh(commodity):
			called.append(commodity)
			if commodity == TEST_COMMODITY:
				raise ValueError("simulated failure")

		with patch(
			"accreage_mart.pricing.forecasting.refresh_commodity_forecast", side_effect=fake_refresh
		):
			tasks.refresh_price_forecasts(commodities=[TEST_COMMODITY, TEST_COMMODITY_OTHER])

		self.assertEqual(sorted(called), sorted([TEST_COMMODITY, TEST_COMMODITY_OTHER]))
