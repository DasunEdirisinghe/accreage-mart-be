import frappe
from frappe.tests.utils import FrappeTestCase

from accreage_mart.api.pricing import get_price_history_and_forecast, get_price_suggestion

TEST_COMMODITY_GOOD = "Test Pricing API Good"
TEST_COMMODITY_BAD = "Test Pricing API Bad"
TEST_COMMODITY_ZERO_SAMPLE = "Test Pricing API Zero Sample"
TEST_CATEGORY_LINKED = "Test Pricing API Category Linked"
TEST_CATEGORY_UNLINKED = "Test Pricing API Category Unlinked"


class TestApiPricing(FrappeTestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		self._purge()

		self.settings = frappe.get_single("Price Suggestion Settings")
		self.threshold_1_7d = self.settings.mape_threshold_1_7d
		self.threshold_8_14d = self.settings.mape_threshold_8_14d
		self.threshold_15_30d = self.settings.mape_threshold_15_30d

		# get_price_suggestion/get_price_history_and_forecast are pure reads - nothing here
		# calls frappe.db.commit(), so no test-isolation leak risk (unlike Stories 3.2/3.4/3.6/3.7).
		# last_evaluated_on must be set for a tier to ever be trusted - Frappe's Float columns
		# default an unset mape_* to 0.0, never NULL, so last_evaluated_on (nullable Date) is
		# the only reliable "has this commodity ever actually been scored" signal.
		frappe.get_doc(
			{
				"doctype": "Commodity",
				"commodity_name": TEST_COMMODITY_GOOD,
				"market": "Peliyagoda",
				"last_evaluated_on": frappe.utils.nowdate(),
				"mape_1_7d": self.threshold_1_7d - 1,  # below its horizon's threshold
				"mape_8_14d": self.threshold_8_14d + 5,  # above its horizon's threshold
				"mape_15_30d": self.threshold_15_30d + 10,  # above its horizon's threshold
			}
		).insert(ignore_permissions=True)

		# Regression fixture for the bug this story fixes: a brand-new commodity with no
		# accumulated sample size at all, but a genuinely good MAPE, must now get a suggestion -
		# gating is MAPE-only, sample size is no longer consulted anywhere in pricing.py.
		frappe.get_doc(
			{
				"doctype": "Commodity",
				"commodity_name": TEST_COMMODITY_ZERO_SAMPLE,
				"market": "Peliyagoda",
				"last_evaluated_on": frappe.utils.nowdate(),
				"mape_1_7d": self.threshold_1_7d - 1,
				"sample_size_1_7d": 0,
			}
		).insert(ignore_permissions=True)

		# Category autonames as a random hash, not its title - the API takes a docname
		# (Frappe Link convention), so tests must call it with the ID insert() returns, not
		# the title constant used to create the fixture.
		self.category_linked_name = frappe.get_doc(
			{"doctype": "Category", "title": TEST_CATEGORY_LINKED, "area": "Vegetables", "commodity": TEST_COMMODITY_GOOD}
		).insert(ignore_permissions=True).name
		self.category_unlinked_name = frappe.get_doc(
			{"doctype": "Category", "title": TEST_CATEGORY_UNLINKED, "area": "Tools"}
		).insert(ignore_permissions=True).name

	def tearDown(self):
		self._purge()
		frappe.set_user("Administrator")

	def _purge(self):
		for title in (TEST_CATEGORY_LINKED, TEST_CATEGORY_UNLINKED):
			for name in frappe.get_all("Category", filters={"title": title}, pluck="name"):
				frappe.delete_doc("Category", name, force=True, ignore_permissions=True)
		for name in (TEST_COMMODITY_GOOD, TEST_COMMODITY_BAD, TEST_COMMODITY_ZERO_SAMPLE):
			if frappe.db.exists("Commodity", name):
				frappe.delete_doc("Commodity", name, force=True, ignore_permissions=True)

	def test_category_with_no_commodity_is_unavailable(self):
		result = get_price_suggestion(self.category_unlinked_name)
		self.assertEqual(result, {"available": False, "reason": "no_commodity_linked"})

	def test_unknown_category_is_unavailable_not_an_error(self):
		result = get_price_suggestion("Does Not Exist At All")
		self.assertEqual(result, {"available": False, "reason": "no_commodity_linked"})

	def test_tiers_reflect_each_horizon_s_own_mape_threshold(self):
		result = get_price_suggestion(self.category_linked_name)
		self.assertTrue(result["available"])
		self.assertEqual(result["commodity_name"], TEST_COMMODITY_GOOD)
		self.assertEqual(result["tiers"]["near"], "direct")  # mape_1_7d below its threshold
		self.assertEqual(result["tiers"]["mid"], "unavailable")  # mape_8_14d above its threshold
		self.assertEqual(result["tiers"]["long"], "unavailable")  # mape_15_30d above its threshold

	def test_disabled_commodity_is_unavailable_with_its_own_reason(self):
		# Story 5.2: staff can turn a commodity's suggestions off (Commodity.is_active) without
		# deleting any data. This must be distinguishable from "no_commodity_linked" so the
		# seller sees an accurate message, not "there's no price reference for this category".
		frappe.db.set_value("Commodity", TEST_COMMODITY_GOOD, "is_active", 0)
		try:
			result = get_price_suggestion(self.category_linked_name)
			self.assertEqual(result, {"available": False, "reason": "disabled"})
		finally:
			frappe.db.set_value("Commodity", TEST_COMMODITY_GOOD, "is_active", 1)

	def test_never_evaluated_commodity_stays_unavailable_despite_a_zero_mape(self):
		# Frappe's Float columns default an unset mape_* to 0.0, never NULL - a commodity that
		# has literally never been scored (last_evaluated_on is empty) must not be read as a
		# perfect 0% score. This is the regression this story's fix (last_evaluated_on gating)
		# exists for; without it every brand-new Commodity would show "direct" for every horizon.
		commodity = frappe.get_doc(
			{"doctype": "Commodity", "commodity_name": "Test Pricing API Never Evaluated", "market": "Peliyagoda"}
		).insert(ignore_permissions=True)
		category = frappe.get_doc(
			{"doctype": "Category", "title": "Test Pricing API Never Evaluated Category", "area": "Vegetables", "commodity": commodity.name}
		).insert(ignore_permissions=True)
		try:
			result = get_price_suggestion(category.name)
			self.assertEqual(result["tiers"], {"near": "unavailable", "mid": "unavailable", "long": "unavailable"})
		finally:
			frappe.delete_doc("Category", category.name, force=True, ignore_permissions=True)
			frappe.delete_doc("Commodity", commodity.name, force=True, ignore_permissions=True)

	def test_zero_sample_size_no_longer_blocks_a_good_mape(self):
		# This is the exact bug the epic fixes: a brand-new commodity with sample_size 0 but a
		# genuinely good MAPE must show a suggestion - sample size is not consulted anymore.
		category = frappe.get_doc(
			{"doctype": "Category", "title": "Test Pricing API Zero Sample Category", "area": "Vegetables", "commodity": TEST_COMMODITY_ZERO_SAMPLE}
		).insert(ignore_permissions=True)
		try:
			result = get_price_suggestion(category.name)
			self.assertEqual(result["tiers"]["near"], "direct")
		finally:
			frappe.delete_doc("Category", category.name, force=True, ignore_permissions=True)

	def test_suggestion_says_what_the_prices_are_per(self):
		result = get_price_suggestion(self.category_linked_name)
		# nothing in the fixture names a unit, so it is the kg default
		self.assertEqual(result["price_unit"], "kg")

		frappe.db.set_value("Commodity", TEST_COMMODITY_GOOD, "unit", "Rs/Egg")
		self.assertEqual(get_price_suggestion(self.category_linked_name)["price_unit"], "egg")

	def test_get_price_history_and_forecast_returns_expected_shape(self):
		result = get_price_history_and_forecast(TEST_COMMODITY_GOOD)
		self.assertEqual(result["commodity_name"], TEST_COMMODITY_GOOD)
		self.assertEqual(result["history"], [])  # no Commodity Price Record rows seeded
		self.assertEqual(result["forecast_days"], [])  # no Price Forecast seeded
		self.assertAlmostEqual(result["accuracy"]["mape_1_7d"], self.threshold_1_7d - 1, places=2)

	def test_get_price_history_and_forecast_unknown_commodity_raises_cleanly(self):
		with self.assertRaises(frappe.DoesNotExistError):
			get_price_history_and_forecast("Does Not Exist At All")

	def test_requires_authentication(self):
		frappe.set_user("Guest")
		try:
			with self.assertRaises(frappe.AuthenticationError):
				get_price_suggestion(self.category_linked_name)
			with self.assertRaises(frappe.AuthenticationError):
				get_price_history_and_forecast(TEST_COMMODITY_GOOD)
		finally:
			frappe.set_user("Administrator")
