import frappe
from frappe.tests.utils import FrappeTestCase

from accreage_mart.api.pricing import get_price_history_and_forecast, get_price_suggestion

TEST_COMMODITY_GOOD = "Test Pricing API Good"
TEST_COMMODITY_BAD = "Test Pricing API Bad"
TEST_COMMODITY_LOW_SAMPLE = "Test Pricing API Low Sample"
TEST_CATEGORY_LINKED = "Test Pricing API Category Linked"
TEST_CATEGORY_UNLINKED = "Test Pricing API Category Unlinked"


class TestApiPricing(FrappeTestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		self._purge()

		self.settings = frappe.get_single("Price Suggestion Settings")
		self.direct_threshold = self.settings.direct_suggestion_mape_threshold
		self.guidance_threshold = self.settings.guidance_mape_threshold
		self.min_sample_size = self.settings.min_sample_size

		# get_price_suggestion/get_price_history_and_forecast are pure reads - nothing here
		# calls frappe.db.commit(), so no test-isolation leak risk (unlike Stories 3.2/3.4/3.6/3.7).
		frappe.get_doc(
			{
				"doctype": "Commodity",
				"commodity_name": TEST_COMMODITY_GOOD,
				"market": "Peliyagoda",
				"mape_1_7d": self.direct_threshold - 1,  # below direct threshold
				"sample_size_1_7d": self.min_sample_size + 5,
				"mape_8_14d": self.guidance_threshold - 1,  # between direct and guidance
				"sample_size_8_14d": self.min_sample_size + 5,
				"mape_15_30d": self.guidance_threshold + 10,  # above guidance
				"sample_size_15_30d": self.min_sample_size + 5,
			}
		).insert(ignore_permissions=True)

		frappe.get_doc(
			{
				"doctype": "Commodity",
				"commodity_name": TEST_COMMODITY_LOW_SAMPLE,
				"market": "Peliyagoda",
				"mape_1_7d": self.direct_threshold - 1,  # otherwise a "direct"-worthy score...
				"sample_size_1_7d": self.min_sample_size - 1,  # ...but too few samples to trust
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
		for name in (TEST_COMMODITY_GOOD, TEST_COMMODITY_BAD, TEST_COMMODITY_LOW_SAMPLE):
			if frappe.db.exists("Commodity", name):
				frappe.delete_doc("Commodity", name, force=True, ignore_permissions=True)

	def test_category_with_no_commodity_is_unavailable(self):
		result = get_price_suggestion(self.category_unlinked_name)
		self.assertEqual(result, {"available": False, "reason": "no_commodity_linked"})

	def test_unknown_category_is_unavailable_not_an_error(self):
		result = get_price_suggestion("Does Not Exist At All")
		self.assertEqual(result, {"available": False, "reason": "no_commodity_linked"})

	def test_tiers_reflect_mape_thresholds_and_sample_size(self):
		result = get_price_suggestion(self.category_linked_name)
		self.assertTrue(result["available"])
		self.assertEqual(result["commodity_name"], TEST_COMMODITY_GOOD)
		self.assertEqual(result["tiers"]["near"], "direct")  # below direct threshold
		self.assertEqual(result["tiers"]["mid"], "range")  # between direct and guidance
		self.assertEqual(result["tiers"]["long"], "unavailable")  # above guidance

	def test_low_sample_size_overrides_a_good_mape(self):
		category = frappe.get_doc(
			{"doctype": "Category", "title": "Test Pricing API Low Sample Category", "area": "Vegetables", "commodity": TEST_COMMODITY_LOW_SAMPLE}
		).insert(ignore_permissions=True)
		try:
			result = get_price_suggestion(category.name)
			self.assertEqual(result["tiers"]["near"], "unavailable")
		finally:
			frappe.delete_doc("Category", category.name, force=True, ignore_permissions=True)

	def test_get_price_history_and_forecast_returns_expected_shape(self):
		result = get_price_history_and_forecast(TEST_COMMODITY_GOOD)
		self.assertEqual(result["commodity_name"], TEST_COMMODITY_GOOD)
		self.assertEqual(result["history"], [])  # no Commodity Price Record rows seeded
		self.assertEqual(result["forecast_days"], [])  # no Price Forecast seeded
		self.assertAlmostEqual(result["accuracy"]["mape_1_7d"], self.direct_threshold - 1, places=2)

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
