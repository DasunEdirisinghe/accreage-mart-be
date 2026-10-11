import frappe
from frappe.tests.utils import FrappeTestCase

from accreage_mart.api.pricing import (
	get_price_suggestion_settings,
	update_price_suggestion_settings,
)


class TestApiPriceSuggestionSettingsCrud(FrappeTestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		self._originals = get_price_suggestion_settings()

	def tearDown(self):
		for field, value in self._originals.items():
			frappe.db.set_value("Price Suggestion Settings", None, field, value)
		frappe.db.commit()
		frappe.set_user("Administrator")

	def test_get_returns_the_three_thresholds(self):
		result = get_price_suggestion_settings()
		self.assertEqual(
			set(result.keys()),
			{"mape_threshold_1_7d", "mape_threshold_8_14d", "mape_threshold_15_30d"},
		)

	def test_update_saves_and_returns_the_new_values(self):
		result = update_price_suggestion_settings(
			mape_threshold_1_7d=15, mape_threshold_8_14d=18, mape_threshold_15_30d=22
		)
		self.assertEqual(result["mape_threshold_1_7d"], 15)
		self.assertEqual(result["mape_threshold_8_14d"], 18)
		self.assertEqual(result["mape_threshold_15_30d"], 22)

		reloaded = get_price_suggestion_settings()
		self.assertEqual(reloaded["mape_threshold_1_7d"], 15)
		self.assertEqual(reloaded["mape_threshold_8_14d"], 18)
		self.assertEqual(reloaded["mape_threshold_15_30d"], 22)

	def test_update_does_not_disturb_the_other_two_thresholds(self):
		# Regression: a per-field db.set_value update must never touch the fields it wasn't
		# given, unlike a full get_single()/.save() round trip would risk doing.
		update_price_suggestion_settings(
			mape_threshold_1_7d=11, mape_threshold_8_14d=12, mape_threshold_15_30d=13
		)
		update_price_suggestion_settings(
			mape_threshold_1_7d=99, mape_threshold_8_14d=12, mape_threshold_15_30d=13
		)
		reloaded = get_price_suggestion_settings()
		self.assertEqual(reloaded["mape_threshold_8_14d"], 12)
		self.assertEqual(reloaded["mape_threshold_15_30d"], 13)

	def test_rejects_a_threshold_of_zero(self):
		with self.assertRaises(frappe.ValidationError):
			update_price_suggestion_settings(
				mape_threshold_1_7d=0, mape_threshold_8_14d=20, mape_threshold_15_30d=20
			)

	def test_rejects_a_threshold_above_100(self):
		with self.assertRaises(frappe.ValidationError):
			update_price_suggestion_settings(
				mape_threshold_1_7d=20, mape_threshold_8_14d=101, mape_threshold_15_30d=20
			)

	def test_get_requires_staff_or_admin(self):
		frappe.set_user("Guest")
		try:
			with self.assertRaises(frappe.PermissionError):
				get_price_suggestion_settings()
		finally:
			frappe.set_user("Administrator")

	def test_update_requires_staff_or_admin(self):
		frappe.set_user("Guest")
		try:
			with self.assertRaises(frappe.PermissionError):
				update_price_suggestion_settings(
					mape_threshold_1_7d=20, mape_threshold_8_14d=20, mape_threshold_15_30d=20
				)
		finally:
			frappe.set_user("Administrator")
