import frappe
from frappe.tests.utils import FrappeTestCase

TEST_COMMODITY = "Test Forecast Carrot"


class TestPriceForecastDoctype(FrappeTestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		self._purge()
		frappe.get_doc(
			{"doctype": "Commodity", "commodity_name": TEST_COMMODITY, "market": "Peliyagoda"}
		).insert(ignore_permissions=True)

	def tearDown(self):
		self._purge()
		frappe.set_user("Administrator")

	def _purge(self):
		if frappe.db.exists("Price Forecast", TEST_COMMODITY):
			frappe.delete_doc("Price Forecast", TEST_COMMODITY, force=True, ignore_permissions=True)
		if frappe.db.exists("Commodity", TEST_COMMODITY):
			frappe.delete_doc("Commodity", TEST_COMMODITY, force=True, ignore_permissions=True)

	def _make_forecast_doc(self, n_days=30, base_price=100.0):
		doc = frappe.get_doc(
			{
				"doctype": "Price Forecast",
				"commodity": TEST_COMMODITY,
				"generated_on": frappe.utils.today(),
				"forecast_days": [
					{
						"forecast_date": frappe.utils.add_days(frappe.utils.today(), i),
						"horizon_days_ahead": i,
						"predicted_price": base_price + i,
						"lower_bound": base_price + i - 10,
						"upper_bound": base_price + i + 10,
					}
					for i in range(1, n_days + 1)
				],
			}
		)
		return doc

	def test_creates_one_doc_per_commodity_with_thirty_days(self):
		doc = self._make_forecast_doc().insert(ignore_permissions=True)
		self.assertEqual(doc.name, TEST_COMMODITY)
		self.assertEqual(len(doc.forecast_days), 30)
		self.assertEqual(doc.forecast_days[0].horizon_days_ahead, 1)
		self.assertEqual(doc.forecast_days[-1].horizon_days_ahead, 30)

	def test_second_insert_for_same_commodity_raises_duplicate_entry(self):
		self._make_forecast_doc().insert(ignore_permissions=True)
		with self.assertRaises(frappe.DuplicateEntryError):
			self._make_forecast_doc().insert(ignore_permissions=True)

	def test_correct_update_pattern_replaces_forecast_days_in_place(self):
		# The intended usage (Story 3.7): load the existing doc and overwrite its child
		# table, rather than trying to insert a second Price Forecast for the commodity.
		self._make_forecast_doc(base_price=100.0).insert(ignore_permissions=True)

		existing = frappe.get_doc("Price Forecast", TEST_COMMODITY)
		existing.forecast_days = []
		for i in range(1, 31):
			existing.append(
				"forecast_days",
				{
					"forecast_date": frappe.utils.add_days(frappe.utils.today(), i),
					"horizon_days_ahead": i,
					"predicted_price": 200.0 + i,
					"lower_bound": 190.0 + i,
					"upper_bound": 210.0 + i,
				},
			)
		existing.save(ignore_permissions=True)

		reloaded = frappe.get_doc("Price Forecast", TEST_COMMODITY)
		self.assertEqual(len(reloaded.forecast_days), 30)
		self.assertEqual(reloaded.forecast_days[0].predicted_price, 201.0)

		# still exactly one Price Forecast for this commodity - update, not a duplicate
		self.assertEqual(frappe.db.count("Price Forecast", {"commodity": TEST_COMMODITY}), 1)
