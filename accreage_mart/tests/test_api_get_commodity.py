import frappe
from frappe.tests.utils import FrappeTestCase

from accreage_mart.api.pricing import get_commodity, list_commodities_overview

TEST_COMMODITY = "Test Get Commodity API"
TEST_COMMODITY_NO_FORECAST = "Test Get Commodity API No Forecast"


class TestApiGetCommodity(FrappeTestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		self._purge()

		frappe.get_doc(
			{
				"doctype": "Commodity",
				"commodity_name": TEST_COMMODITY,
				"harti_category": "Rice",
				"market": "Pettah",
				"unit": "kg",
				"is_active": 1,
				"mape_1_7d": 4.5,
				"mape_8_14d": 6.2,
				"mape_15_30d": 9.1,
				"sample_size_1_7d": 12,
				"sample_size_8_14d": 12,
				"sample_size_15_30d": 12,
				"last_evaluated_on": frappe.utils.getdate(),
			}
		).insert(ignore_permissions=True)

		frappe.get_doc(
			{
				"doctype": "Commodity",
				"commodity_name": TEST_COMMODITY_NO_FORECAST,
				"harti_category": "Rice",
				"market": "Peliyagoda",
			}
		).insert(ignore_permissions=True)

		frappe.get_doc(
			{
				"doctype": "Price Forecast",
				"commodity": TEST_COMMODITY,
				"generated_on": frappe.utils.getdate(),
				"forecast_days": [
					{
						"forecast_date": frappe.utils.add_days(frappe.utils.getdate(), i),
						"horizon_days_ahead": i,
						"predicted_price": 100 + i,
						"lower_bound": 90 + i,
						"upper_bound": 110 + i,
					}
					for i in range(1, 31)
				],
			}
		).insert(ignore_permissions=True)

	def tearDown(self):
		self._purge()
		frappe.set_user("Administrator")

	def _purge(self):
		for name in (TEST_COMMODITY, TEST_COMMODITY_NO_FORECAST):
			if frappe.db.exists("Price Forecast", name):
				frappe.delete_doc("Price Forecast", name, force=True, ignore_permissions=True)
			if frappe.db.exists("Commodity", name):
				frappe.delete_doc("Commodity", name, force=True, ignore_permissions=True)

	# -- list_commodities_overview --

	def test_list_commodities_overview_returns_rows(self):
		rows = list_commodities_overview()
		row = next(r for r in rows if r["name"] == TEST_COMMODITY)
		self.assertEqual(row["harti_category"], "Rice")
		self.assertEqual(row["market"], "Pettah")
		self.assertEqual(row["is_active"], 1)
		self.assertAlmostEqual(row["mape_1_7d"], 4.5, places=2)

	def test_list_commodities_overview_requires_staff_or_admin(self):
		frappe.set_user("Guest")
		try:
			with self.assertRaises(frappe.PermissionError):
				list_commodities_overview()
		finally:
			frappe.set_user("Administrator")

	# -- get_commodity --

	def test_get_commodity_returns_full_data_with_forecast(self):
		result = get_commodity(TEST_COMMODITY)
		self.assertEqual(result["name"], TEST_COMMODITY)
		self.assertEqual(result["harti_category"], "Rice")
		self.assertEqual(result["market"], "Pettah")
		self.assertEqual(result["unit"], "kg")
		self.assertEqual(result["is_active"], 1)
		self.assertAlmostEqual(result["mape_1_7d"], 4.5, places=2)
		self.assertEqual(result["sample_size_1_7d"], 12)
		self.assertEqual(len(result["forecast_days"]), 30)
		self.assertEqual(result["forecast_days"][0]["horizon_days_ahead"], 1)

	def test_get_commodity_no_forecast_yet_returns_empty_list(self):
		result = get_commodity(TEST_COMMODITY_NO_FORECAST)
		self.assertEqual(result["name"], TEST_COMMODITY_NO_FORECAST)
		self.assertEqual(result["forecast_days"], [])

	def test_get_commodity_unknown_raises_cleanly(self):
		with self.assertRaises(frappe.DoesNotExistError):
			get_commodity("Does Not Exist At All")

	def test_get_commodity_requires_staff_or_admin(self):
		frappe.set_user("Guest")
		try:
			with self.assertRaises(frappe.PermissionError):
				get_commodity(TEST_COMMODITY)
		finally:
			frappe.set_user("Administrator")
