import frappe
from frappe.tests.utils import FrappeTestCase

from accreage_mart.api.pricing import (
	delete_category,
	list_categories,
	list_commodities,
	upsert_category,
)

TEST_COMMODITY = "Rice - Test Category CRUD"
TEST_CATEGORY_TITLE = "Test Category CRUD Title"
TEST_CATEGORY_TITLE_2 = "Test Category CRUD Title Updated"


class TestApiCategoryCrud(FrappeTestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		self._purge()
		frappe.get_doc(
			{"doctype": "Commodity", "commodity_name": TEST_COMMODITY, "harti_category": "Rice", "market": "Pettah"}
		).insert(ignore_permissions=True)

	def tearDown(self):
		self._purge()
		frappe.set_user("Administrator")

	def _purge(self):
		for title in (TEST_CATEGORY_TITLE, TEST_CATEGORY_TITLE_2):
			for name in frappe.get_all("Category", filters={"title": title}, pluck="name"):
				frappe.delete_doc("Category", name, force=True, ignore_permissions=True)
		if frappe.db.exists("Commodity", TEST_COMMODITY):
			frappe.delete_doc("Commodity", TEST_COMMODITY, force=True, ignore_permissions=True)

	# -- list_categories --

	def test_list_categories_returns_rows(self):
		category = frappe.get_doc(
			{"doctype": "Category", "title": TEST_CATEGORY_TITLE, "area": "Rice", "commodity": TEST_COMMODITY}
		).insert(ignore_permissions=True)

		rows = list_categories()
		row = next(r for r in rows if r["name"] == category.name)
		self.assertEqual(row["title"], TEST_CATEGORY_TITLE)
		self.assertEqual(row["area"], "Rice")
		self.assertEqual(row["commodity"], TEST_COMMODITY)

	def test_list_categories_requires_staff_or_admin(self):
		frappe.set_user("Guest")
		try:
			with self.assertRaises(frappe.PermissionError):
				list_categories()
		finally:
			frappe.set_user("Administrator")

	# -- upsert_category: create vs update --

	def test_upsert_category_creates_new(self):
		result = upsert_category(title=TEST_CATEGORY_TITLE, area="Rice", commodity=TEST_COMMODITY)
		self.assertTrue(frappe.db.exists("Category", result["name"]))
		self.assertEqual(result["title"], TEST_CATEGORY_TITLE)
		self.assertEqual(result["commodity"], TEST_COMMODITY)

	def test_upsert_category_with_name_updates_existing_not_creates_duplicate(self):
		created = upsert_category(title=TEST_CATEGORY_TITLE, area="Rice", commodity=TEST_COMMODITY)

		updated = upsert_category(
			name=created["name"], title=TEST_CATEGORY_TITLE_2, area="Fruits", commodity=None
		)

		self.assertEqual(updated["name"], created["name"])  # same doc, not a new one
		self.assertEqual(updated["title"], TEST_CATEGORY_TITLE_2)
		self.assertEqual(updated["area"], "Fruits")
		self.assertFalse(updated["commodity"])

		matches = frappe.get_all("Category", filters={"title": TEST_CATEGORY_TITLE_2})
		self.assertEqual(len(matches), 1)

	def test_upsert_category_allows_blank_commodity(self):
		result = upsert_category(title=TEST_CATEGORY_TITLE, area="Tools", commodity=None)
		self.assertFalse(result["commodity"])

	def test_upsert_category_rejects_unknown_commodity(self):
		with self.assertRaises(frappe.ValidationError):
			upsert_category(title=TEST_CATEGORY_TITLE, area="Rice", commodity="Does Not Exist At All")

	def test_upsert_category_rejects_missing_title(self):
		with self.assertRaises(frappe.ValidationError):
			upsert_category(title="  ", area="Rice", commodity=None)

	def test_upsert_category_rejects_invalid_area(self):
		with self.assertRaises(frappe.ValidationError):
			upsert_category(title=TEST_CATEGORY_TITLE, area="NotARealArea", commodity=None)

	def test_upsert_category_requires_staff_or_admin(self):
		frappe.set_user("Guest")
		try:
			with self.assertRaises(frappe.PermissionError):
				upsert_category(title=TEST_CATEGORY_TITLE, area="Rice", commodity=None)
		finally:
			frappe.set_user("Administrator")

	# -- delete_category --

	def test_delete_category_removes_it(self):
		created = upsert_category(title=TEST_CATEGORY_TITLE, area="Rice", commodity=None)
		delete_category(created["name"])
		self.assertFalse(frappe.db.exists("Category", created["name"]))

	def test_delete_category_allowed_even_when_linked_to_a_commodity(self):
		created = upsert_category(title=TEST_CATEGORY_TITLE, area="Rice", commodity=TEST_COMMODITY)
		delete_category(created["name"])  # must not raise
		self.assertFalse(frappe.db.exists("Category", created["name"]))
		self.assertTrue(frappe.db.exists("Commodity", TEST_COMMODITY))  # commodity untouched

	def test_delete_category_requires_staff_or_admin(self):
		created = upsert_category(title=TEST_CATEGORY_TITLE, area="Rice", commodity=None)
		frappe.set_user("Guest")
		try:
			with self.assertRaises(frappe.PermissionError):
				delete_category(created["name"])
		finally:
			frappe.set_user("Administrator")

	# -- list_commodities --

	def test_list_commodities_filters_by_search(self):
		names = [r["name"] for r in list_commodities(search="Test Category CRUD")]
		self.assertIn(TEST_COMMODITY, names)

		names_unrelated = [r["name"] for r in list_commodities(search="Zzz Not A Real Commodity")]
		self.assertNotIn(TEST_COMMODITY, names_unrelated)

	def test_list_commodities_requires_staff_or_admin(self):
		frappe.set_user("Guest")
		try:
			with self.assertRaises(frappe.PermissionError):
				list_commodities()
		finally:
			frappe.set_user("Administrator")
