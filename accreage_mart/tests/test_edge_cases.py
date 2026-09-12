"""Story 3.13: edge cases. Two of the four listed cases already have dedicated coverage
elsewhere and are deliberately NOT duplicated here:
  - "Category with commodity=null returns no_commodity_linked, never a 500" -
    apps/accreage_mart/accreage_mart/tests/test_api_pricing.py::test_category_with_no_commodity_is_unavailable
  - "null/ambiguous source price is never guessed" -
    apps/accreage_mart/accreage_mart/tests/test_extractor.py::test_stray_decimal_before_range_is_not_guessed
    and test_missing_range_dash_is_not_guessed
Re-testing the exact same assertions here would just be two places to keep in sync for the
same behavior. This file covers the two cases that genuinely aren't exercised end to end
anywhere yet: a too-short-history commodity across the full refresh -> suggestion path, and
Price Suggestion Settings taking effect immediately with no caching layer.
"""

import datetime
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from accreage_mart.api.pricing import get_price_suggestion
from accreage_mart.pricing import forecasting

TEST_CATEGORY = "Rice"
TEST_ITEM = "Test Edge Case"
TEST_COMMODITY = f"{TEST_CATEGORY} - {TEST_ITEM}"
TEST_CATEGORY_DOC_TITLE = "Test Edge Case Category"

TEST_COMMODITY_THRESHOLD = f"{TEST_CATEGORY} - Test Edge Case Threshold"
TEST_CATEGORY_DOC_TITLE_THRESHOLD = "Test Edge Case Threshold Category"


class TestEdgeCases(FrappeTestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		self._purge()

	def tearDown(self):
		self._purge()
		frappe.set_user("Administrator")

	def _purge(self):
		# csv_mirror.generate_for_commodity() (Story 3.10, wired as forecasting.py's 4th step)
		# calls frappe.db.commit() unconditionally, even when regenerate itself did nothing -
		# breaks FrappeTestCase's usual auto-rollback, same gotcha every other pricing test
		# with real commit-calling code in its path has hit. This cleanup commits its own
		# deletes explicitly.
		for commodity in (TEST_COMMODITY, TEST_COMMODITY_THRESHOLD):
			for title in (TEST_CATEGORY_DOC_TITLE, TEST_CATEGORY_DOC_TITLE_THRESHOLD):
				for name in frappe.get_all("Category", filters={"title": title}, pluck="name"):
					frappe.delete_doc("Category", name, force=True, ignore_permissions=True)
			for doctype in ("Forecast Accuracy Log", "Commodity Price Record"):
				for rec in frappe.get_all(doctype, filters={"commodity": commodity}, pluck="name"):
					frappe.delete_doc(doctype, rec, force=True, ignore_permissions=True)
			for f in frappe.get_all(
				"File", filters={"attached_to_doctype": "Commodity", "attached_to_name": commodity}, pluck="name"
			):
				frappe.delete_doc("File", f, force=True, ignore_permissions=True)
			if frappe.db.exists("Price Forecast", commodity):
				frappe.delete_doc("Price Forecast", commodity, force=True, ignore_permissions=True)
			if frappe.db.exists("Commodity", commodity):
				frappe.delete_doc("Commodity", commodity, force=True, ignore_permissions=True)
		frappe.db.commit()

	# -- case: a commodity with too little history for a meaningful Prophet fit --

	def test_too_short_history_commodity_stays_unavailable_end_to_end(self):
		frappe.get_doc(
			{"doctype": "Commodity", "commodity_name": TEST_COMMODITY, "harti_category": TEST_CATEGORY, "market": "Pettah"}
		).insert(ignore_permissions=True)

		today = frappe.utils.getdate()
		for i in range(10):  # well under the 30-day floor _regenerate_forecast requires
			d = today - datetime.timedelta(days=10 - i)
			frappe.get_doc(
				{
					"doctype": "Commodity Price Record", "commodity": TEST_COMMODITY, "date": d,
					"min_price": 95.0, "max_price": 105.0, "average_price": 100.0, "average_computed": 0,
				}
			).insert(ignore_permissions=True)
		frappe.db.commit()

		category = frappe.get_doc(
			{"doctype": "Category", "title": TEST_CATEGORY_DOC_TITLE, "area": "Rice", "commodity": TEST_COMMODITY}
		).insert(ignore_permissions=True)

		# Prophet must never even be constructed for this commodity - proves the short-circuit
		# happens before any fit is attempted, not just that no exception surfaces.
		with patch(
			"accreage_mart.pricing.forecasting.Prophet",
			side_effect=AssertionError("Prophet should never be called for too-short history"),
		):
			forecasting.refresh_commodity_forecast(TEST_COMMODITY)  # must not raise

		self.assertFalse(frappe.db.exists("Price Forecast", TEST_COMMODITY))

		result = get_price_suggestion(category.name)
		self.assertTrue(result["available"])  # the commodity exists and is linked
		self.assertEqual(result["tiers"], {"near": "unavailable", "mid": "unavailable", "long": "unavailable"})
		self.assertEqual(result["forecast_days"], [])

	# -- case: Price Suggestion Settings changes take effect immediately, no caching --

	def test_settings_change_takes_effect_immediately_no_caching(self):
		# Read the CURRENT guidance threshold first and derive both the commodity's MAPE and
		# the new threshold from it, rather than hardcoding 30/35 against an assumed default of
		# 25 - keeps this test correct regardless of what an earlier test left Settings at.
		settings = frappe.get_single("Price Suggestion Settings")
		original_guidance = settings.guidance_mape_threshold
		mape_above_current_guidance = original_guidance + 5
		new_guidance_above_that = mape_above_current_guidance + 10

		frappe.get_doc(
			{
				"doctype": "Commodity", "commodity_name": TEST_COMMODITY_THRESHOLD,
				"harti_category": TEST_CATEGORY, "market": "Pettah",
				"mape_1_7d": mape_above_current_guidance, "sample_size_1_7d": 50,
			}
		).insert(ignore_permissions=True)
		category = frappe.get_doc(
			{
				"doctype": "Category", "title": TEST_CATEGORY_DOC_TITLE_THRESHOLD,
				"area": "Rice", "commodity": TEST_COMMODITY_THRESHOLD,
			}
		).insert(ignore_permissions=True)

		before = get_price_suggestion(category.name)
		self.assertEqual(before["tiers"]["near"], "unavailable")  # mape > current guidance threshold

		try:
			settings.guidance_mape_threshold = new_guidance_above_that
			settings.save(ignore_permissions=True)
			frappe.db.commit()

			after = get_price_suggestion(category.name)  # same process, no restart, no cache clear
			self.assertEqual(after["tiers"]["near"], "range")
		finally:
			settings = frappe.get_single("Price Suggestion Settings")
			settings.guidance_mape_threshold = original_guidance
			settings.save(ignore_permissions=True)
			frappe.db.commit()
