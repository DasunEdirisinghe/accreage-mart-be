import frappe
from frappe.tests.utils import FrappeTestCase

from accreage_mart.api.auth import pending_accounts, register_seller, verify_account

SELLER = "approve.seller@x.lk"


class TestApprovals(FrappeTestCase):
	def _purge(self):
		for doctype in ("Seller Profile", "Buyer Profile"):
			if frappe.db.exists(doctype, {"user": SELLER}):
				frappe.delete_doc(doctype, SELLER, force=True, ignore_permissions=True)
		if frappe.db.exists("User", SELLER):
			frappe.delete_doc("User", SELLER, force=True, ignore_permissions=True)

	def setUp(self):
		self._purge()
		register_seller(
			full_name="Approve Seller",
			business_name="Approve Farms",
			email=SELLER,
			mobile="",
			district="Kandy",
		)
		frappe.set_user("Administrator")

	def tearDown(self):
		frappe.set_user("Administrator")
		self._purge()

	def test_pending_lists_the_unverified_seller(self):
		emails = [row["email"] for row in pending_accounts()]
		self.assertIn(SELLER, emails)

	def test_verify_account_flips_the_flag_and_drops_from_pending(self):
		verify_account(SELLER)
		self.assertEqual(frappe.db.get_value("Seller Profile", {"user": SELLER}, "verified"), 1)
		self.assertNotIn(SELLER, [row["email"] for row in pending_accounts()])

	def test_pending_accounts_requires_staff(self):
		frappe.set_user("Guest")
		try:
			with self.assertRaises(frappe.PermissionError):
				pending_accounts()
		finally:
			frappe.set_user("Administrator")
