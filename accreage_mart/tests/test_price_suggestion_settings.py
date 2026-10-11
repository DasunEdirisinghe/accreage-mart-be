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
		self.assertEqual(settings.mape_threshold_1_7d, 20)
		self.assertEqual(settings.mape_threshold_8_14d, 20)
		self.assertEqual(settings.mape_threshold_15_30d, 20)
		self.assertEqual(settings.interval_recalibration_factor, 1.5)
		self.assertEqual(settings.forecast_horizon_days, 30)

	def test_reseeding_never_overwrites_an_edited_value(self):
		# Single-field update via db.set_value, not a full get_single()/.save() round trip -
		# the latter risks clobbering the other threshold fields with a stale in-memory
		# snapshot of the document (observed during this story's own verification).
		frappe.db.set_value("Price Suggestion Settings", None, "mape_threshold_1_7d", 12)
		frappe.db.commit()

		try:
			ensure_price_suggestion_settings()  # simulates a later bench migrate
			reloaded = frappe.get_single("Price Suggestion Settings")
			self.assertEqual(reloaded.mape_threshold_1_7d, 12)
		finally:
			# restore the default so other tests (and the real site) aren't left
			# with this test's edit
			frappe.db.set_value("Price Suggestion Settings", None, "mape_threshold_1_7d", 20)
			frappe.db.commit()
