import frappe
from frappe.tests.utils import FrappeTestCase

from accreage_mart.api.auth import create_staff, list_accounts, register_buyer, set_account_status

STAFF = "list.staff@x.lk"
BUYER = "list.buyer@x.lk"


class TestAdminAccounts(FrappeTestCase):
	def _purge(self, *emails):
		for email in emails:
			for doctype in ("Buyer Profile", "Seller Profile"):
				if frappe.db.exists(doctype, {"user": email}):
					frappe.delete_doc(doctype, email, force=True, ignore_permissions=True)
			if frappe.db.exists("User", email):
				frappe.delete_doc("User", email, force=True, ignore_permissions=True)

	def setUp(self):
		frappe.set_user("Administrator")
		self._purge(STAFF, BUYER)
		create_staff(full_name="List Staff", email=STAFF, role="Staff")
		register_buyer(
			full_name="List Buyer",
			business_name="List Hotels",
			email=BUYER,
			mobile="",
			district="Colombo",
			buyer_type="Hotel",
		)

	def tearDown(self):
		frappe.set_user("Administrator")
		self._purge(STAFF, BUYER)

	def test_list_accounts_staff_and_members(self):
		staff_emails = [r["email"] for r in list_accounts("staff")]
		self.assertIn(STAFF, staff_emails)
		self.assertNotIn(BUYER, staff_emails)
		self.assertNotIn("Administrator", staff_emails)

		members = list_accounts("members")
		buyer_row = next(r for r in members if r["email"] == BUYER)
		self.assertEqual(buyer_row["role"], "buyer")
		self.assertEqual(buyer_row["businessName"], "List Hotels")

	def test_list_accounts_requires_admin(self):
		frappe.set_user("Guest")
		try:
			with self.assertRaises(frappe.PermissionError):
				list_accounts("staff")
		finally:
			frappe.set_user("Administrator")

	def test_set_account_status(self):
		set_account_status(BUYER, "suspended")
		user = frappe.db.get_value(
			"User", BUYER, ["custom_account_status", "enabled"], as_dict=True
		)
		self.assertEqual(user.custom_account_status, "suspended")
		self.assertEqual(user.enabled, 0)

		set_account_status(BUYER, "active")
		self.assertEqual(frappe.db.get_value("User", BUYER, "enabled"), 1)

	def test_cannot_touch_administrator_or_self(self):
		with self.assertRaises(frappe.PermissionError):
			set_account_status("Administrator", "suspended")
		with self.assertRaises(frappe.PermissionError):
			set_account_status(frappe.session.user, "suspended")
