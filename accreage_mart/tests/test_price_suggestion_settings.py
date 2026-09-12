import frappe
from frappe.tests.utils import FrappeTestCase

from accreage_mart.setup.install import ensure_price_suggestion_settings


class TestPriceSuggestionSettings(FrappeTestCase):
	def setUp(self):
		frappe.set_user("Administrator")

	def test_defaults_are_seeded(self):
		# before_tests() already calls _setup() (which calls ensure_price_suggestion_settings),
		# so by the time any test runs the singleton should already carry these defaults.
		settings = frappe.get_single("Price Suggestion Settings")
		self.assertEqual(settings.direct_suggestion_mape_threshold, 10)
		self.assertEqual(settings.guidance_mape_threshold, 25)
		self.assertEqual(settings.min_sample_size, 20)
		self.assertEqual(settings.interval_recalibration_factor, 1.5)
		self.assertEqual(settings.forecast_horizon_days, 30)

	def test_reseeding_never_overwrites_an_edited_value(self):
		settings = frappe.get_single("Price Suggestion Settings")
		settings.direct_suggestion_mape_threshold = 12
		settings.save(ignore_permissions=True)
		frappe.db.commit()

		try:
			ensure_price_suggestion_settings()  # simulates a later bench migrate
			reloaded = frappe.get_single("Price Suggestion Settings")
			self.assertEqual(reloaded.direct_suggestion_mape_threshold, 12)
		finally:
			# restore the default so other tests (and the real site) aren't left
			# with this test's edit
			settings = frappe.get_single("Price Suggestion Settings")
			settings.direct_suggestion_mape_threshold = 10
			settings.save(ignore_permissions=True)
			frappe.db.commit()
