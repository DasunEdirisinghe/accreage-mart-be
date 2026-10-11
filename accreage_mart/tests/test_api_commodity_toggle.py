import frappe
from frappe.tests.utils import FrappeTestCase

from accreage_mart.api.pricing import set_commodity_active

TEST_COMMODITY = "Test Commodity Toggle"


class TestApiCommodityToggle(FrappeTestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		self._purge()
		frappe.get_doc(
			{"doctype": "Commodity", "commodity_name": TEST_COMMODITY, "market": "Pettah"}
		).insert(ignore_permissions=True)

	def tearDown(self):
		self._purge()
		frappe.set_user("Administrator")

	def _purge(self):
		if frappe.db.exists("Commodity", TEST_COMMODITY):
			frappe.delete_doc("Commodity", TEST_COMMODITY, force=True, ignore_permissions=True)

	def test_turns_a_commodity_off_and_back_on(self):
		# Commodity.is_active defaults to 1 - confirm the fixture starts there before toggling.
		self.assertEqual(frappe.db.get_value("Commodity", TEST_COMMODITY, "is_active"), 1)

		result = set_commodity_active(TEST_COMMODITY, False)
		self.assertFalse(result["is_active"])
		self.assertEqual(frappe.db.get_value("Commodity", TEST_COMMODITY, "is_active"), 0)

		result = set_commodity_active(TEST_COMMODITY, True)
		self.assertTrue(result["is_active"])
		self.assertEqual(frappe.db.get_value("Commodity", TEST_COMMODITY, "is_active"), 1)

	def test_rejects_unknown_commodity(self):
		with self.assertRaises(frappe.DoesNotExistError):
			set_commodity_active("Does Not Exist At All", False)

	def test_requires_staff_or_admin(self):
		frappe.set_user("Guest")
		try:
			with self.assertRaises(frappe.PermissionError):
				set_commodity_active(TEST_COMMODITY, False)
		finally:
			frappe.set_user("Administrator")
