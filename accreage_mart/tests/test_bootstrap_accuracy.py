import datetime
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from accreage_mart.pricing import evaluation

TEST_COMMODITY_SHORT = "Test Bootstrap Short History"
TEST_COMMODITY_LONG = "Test Bootstrap Long History"

# A constant 5.0% error, every day, every cutoff - makes the expected median MAPE exactly
# computable per bucket without needing a real Prophet fit (slow, and not what this test is
# meant to verify - the cutoff/bucketing/aggregation logic is).
FAKE_ERROR_PCT = 5.0
CONSTANT_PRICE = 100.0


class FakeProphet:
	"""Stands in for prophet.Prophet - fit() is a no-op, predict() always returns
	CONSTANT_PRICE * 1.05 for every requested date, a fixed 5% error against the constant
	CONSTANT_PRICE actuals seeded below."""

	def __init__(self, *args, **kwargs):
		pass

	def fit(self, df):
		return self

	def predict(self, future):
		out = future.copy()
		out["yhat"] = CONSTANT_PRICE * (1 + FAKE_ERROR_PCT / 100)
		return out


class TestBootstrapAccuracy(FrappeTestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		self._purge()

	def tearDown(self):
		self._purge()
		frappe.set_user("Administrator")

	def _purge(self):
		# bootstrap_accuracy() calls frappe.db.commit() internally, which breaks
		# FrappeTestCase's usual auto-rollback - see test_backfill.py's _purge for the full
		# explanation. This cleanup must commit its own deletes explicitly.
		for name in (TEST_COMMODITY_SHORT, TEST_COMMODITY_LONG):
			for rec in frappe.get_all("Commodity Price Record", filters={"commodity": name}, pluck="name"):
				frappe.delete_doc("Commodity Price Record", rec, force=True, ignore_permissions=True)
			if frappe.db.exists("Commodity", name):
				frappe.delete_doc("Commodity", name, force=True, ignore_permissions=True)
		frappe.db.commit()

	def _seed_daily_history(self, commodity, num_days, price=CONSTANT_PRICE):
		frappe.get_doc(
			{"doctype": "Commodity", "commodity_name": commodity, "market": "Peliyagoda"}
		).insert(ignore_permissions=True)

		today = frappe.utils.getdate()
		for i in range(num_days):
			d = today - datetime.timedelta(days=num_days - i)
			frappe.get_doc(
				{
					"doctype": "Commodity Price Record",
					"commodity": commodity,
					"date": d,
					"min_price": price - 5,
					"max_price": price + 5,
					"average_price": price,
					"average_computed": 0,
				}
			).insert(ignore_permissions=True)
		frappe.db.commit()

	def test_skips_commodity_with_no_history(self):
		frappe.get_doc(
			{"doctype": "Commodity", "commodity_name": TEST_COMMODITY_SHORT, "market": "Peliyagoda"}
		).insert(ignore_permissions=True)

		result = evaluation.bootstrap_accuracy(TEST_COMMODITY_SHORT)
		self.assertTrue(result["skipped"])
		self.assertEqual(result["reason"], "no history")

	def test_skips_commodity_with_too_short_history_for_two_cutoffs(self):
		# 60 days is nowhere near enough for even one 180-day-prior-history cutoff.
		self._seed_daily_history(TEST_COMMODITY_SHORT, num_days=60)

		result = evaluation.bootstrap_accuracy(TEST_COMMODITY_SHORT)
		self.assertTrue(result["skipped"])

		commodity_doc = frappe.get_doc("Commodity", TEST_COMMODITY_SHORT)
		# Frappe Float fields default to 0.0, not None, when never set - not the "null" the
		# original story text assumed; the field is simply untouched, which for a Float
		# means 0.0.
		self.assertEqual(commodity_doc.mape_1_7d, 0.0)
		self.assertEqual(commodity_doc.sample_size_1_7d, 0)

	def test_computes_bucketed_median_mape_with_mocked_prophet(self):
		# 450 days gives all 6 cutoffs enough prior history (>= 180 days each).
		self._seed_daily_history(TEST_COMMODITY_LONG, num_days=450)

		with patch("accreage_mart.pricing.evaluation.Prophet", FakeProphet):
			result = evaluation.bootstrap_accuracy(TEST_COMMODITY_LONG)

		self.assertFalse(result["skipped"])
		self.assertAlmostEqual(result["mape_1_7d"], FAKE_ERROR_PCT, places=2)
		self.assertAlmostEqual(result["mape_8_14d"], FAKE_ERROR_PCT, places=2)
		self.assertAlmostEqual(result["mape_15_30d"], FAKE_ERROR_PCT, places=2)

		# 6 usable cutoffs x 7/7/16 days per bucket (1-7 / 8-14 / 15-30), all with real
		# actuals present (daily, gap-free synthetic series).
		self.assertEqual(result["sample_size_1_7d"], 6 * 7)
		self.assertEqual(result["sample_size_8_14d"], 6 * 7)
		self.assertEqual(result["sample_size_15_30d"], 6 * 16)

		commodity_doc = frappe.get_doc("Commodity", TEST_COMMODITY_LONG)
		self.assertAlmostEqual(commodity_doc.mape_1_7d, FAKE_ERROR_PCT, places=2)
		self.assertEqual(commodity_doc.sample_size_1_7d, 6 * 7)
		self.assertEqual(commodity_doc.last_evaluated_on, frappe.utils.getdate())

	def test_bootstrap_all_skips_and_seeds_correctly(self):
		# Explicit commodities list - bootstrap_all() must NEVER be called with its default
		# (every active Commodity) inside a test, since this dev DB already holds the real
		# 65-commodity backfill; doing so would overwrite their real accuracy fields with
		# fake mocked-Prophet data.
		self._seed_daily_history(TEST_COMMODITY_SHORT, num_days=60)
		self._seed_daily_history(TEST_COMMODITY_LONG, num_days=450)

		with patch("accreage_mart.pricing.evaluation.Prophet", FakeProphet):
			result = evaluation.bootstrap_all(commodities=[TEST_COMMODITY_SHORT, TEST_COMMODITY_LONG])

		skipped_names = [name for name, _ in result["skipped"]]
		self.assertIn(TEST_COMMODITY_SHORT, skipped_names)
		self.assertEqual(result["seeded"], 1)
