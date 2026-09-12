import frappe
from frappe.tests.utils import FrappeTestCase

COMMODITY = "Test Carrot"
CATEGORY_A = "Test Carrot - Up Country"
CATEGORY_B = "Test Carrot - Organic"


class TestCommodityCategory(FrappeTestCase):
	def _purge(self):
		for title in (CATEGORY_A, CATEGORY_B):
			for name in frappe.get_all("Category", filters={"title": title}, pluck="name"):
				frappe.delete_doc("Category", name, force=True, ignore_permissions=True)
		if frappe.db.exists("Commodity", COMMODITY):
			frappe.delete_doc("Commodity", COMMODITY, force=True, ignore_permissions=True)

	def setUp(self):
		frappe.set_user("Administrator")
		self._purge()

	def tearDown(self):
		frappe.set_user("Administrator")
		self._purge()

	def test_commodity_autonames_on_name(self):
		doc = frappe.get_doc(
			{
				"doctype": "Commodity",
				"commodity_name": COMMODITY,
				"harti_category": "Up Country Vegetable",
				"market": "Peliyagoda",
				"unit": "Rs/kg",
			}
		).insert()
		self.assertEqual(doc.name, COMMODITY)
		self.assertEqual(doc.is_active, 1)
		self.assertIsNone(doc.mape_1_7d)
		self.assertEqual(doc.sample_size_1_7d, 0)

	def test_two_categories_can_share_one_commodity(self):
		frappe.get_doc(
			{
				"doctype": "Commodity",
				"commodity_name": COMMODITY,
				"market": "Peliyagoda",
			}
		).insert()

		cat_a = frappe.get_doc(
			{"doctype": "Category", "title": CATEGORY_A, "area": "Vegetables", "commodity": COMMODITY}
		).insert()
		cat_b = frappe.get_doc(
			{"doctype": "Category", "title": CATEGORY_B, "area": "Vegetables", "commodity": COMMODITY}
		).insert()

		self.assertEqual(cat_a.commodity, COMMODITY)
		self.assertEqual(cat_b.commodity, COMMODITY)
		self.assertNotEqual(cat_a.name, cat_b.name)

		linked = frappe.get_all("Category", filters={"commodity": COMMODITY}, pluck="name")
		self.assertEqual(len(linked), 2)

	def test_category_with_no_commodity_saves_cleanly(self):
		doc = frappe.get_doc(
			{"doctype": "Category", "title": CATEGORY_A, "area": "Tools"}
		).insert()
		self.assertFalse(doc.commodity)

	def test_write_requires_staff_or_admin(self):
		frappe.set_user("Guest")
		try:
			with self.assertRaises(frappe.PermissionError):
				frappe.get_doc(
					{"doctype": "Category", "title": CATEGORY_A, "area": "Other"}
				).insert()
		finally:
			frappe.set_user("Administrator")
