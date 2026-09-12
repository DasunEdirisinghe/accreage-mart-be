import frappe
from frappe.tests.utils import FrappeTestCase

from accreage_mart.api.auth import account_applications, register_seller, reject_account, verify_account

SELLER = "approve.seller@x.lk"


class TestApprovals(FrappeTestCase):
	def _purge(self):
		for doctype in ("Seller Profile", "Buyer Profile"):
			if frappe.db.exists(doctype, {"user": SELLER}):
				frappe.delete_doc(doctype, SELLER, force=True, ignore_permissions=True)
		if frappe.db.exists("User", SELLER):
			frappe.delete_doc("User", SELLER, force=True, ignore_permissions=True)

	def _row(self):
		return next(row for row in account_applications() if row["email"] == SELLER)

	def setUp(self):
		self._purge()
		register_seller(
			full_name="Approve Seller",
			business_name="Approve Farms",
			email=SELLER,
			mobile="0771234567",
			district="Kandy",
		)
		frappe.set_user("Administrator")

	def tearDown(self):
		frappe.set_user("Administrator")
		self._purge()

	def test_application_lists_the_seller_as_pending_with_registration_fields(self):
		row = self._row()
		self.assertEqual(row["verificationStatus"], "pending")
		self.assertEqual(row["businessName"], "Approve Farms")
		self.assertEqual(row["mobile"], "0771234567")
		self.assertEqual(row["district"], "Kandy")
		self.assertIsNone(row["reviewedOn"])

	def test_verify_account_approves_and_mints_a_set_password_link(self):
		out = verify_account(SELLER)
		self.assertTrue(out["ok"])

		self.assertEqual(frappe.db.get_value("Seller Profile", {"user": SELLER}, "verified"), 1)
		row = self._row()
		self.assertEqual(row["verificationStatus"], "approved")
		self.assertIsNotNone(row["reviewedOn"])
		# Dev sites (no SMTP) expose the set-password link directly.
		self.assertTrue(out.get("dev_link"))
		self.assertTrue(frappe.db.get_value("User", SELLER, "reset_password_key"))

	def test_reject_account_requires_a_reason(self):
		with self.assertRaises(frappe.ValidationError):
			reject_account(SELLER, "")

	def test_reject_account_records_the_reason_and_leaves_unverified(self):
		out = reject_account(SELLER, "Business registration documents did not match.")
		self.assertTrue(out["ok"])

		self.assertEqual(frappe.db.get_value("Seller Profile", {"user": SELLER}, "verified"), 0)
		row = self._row()
		self.assertEqual(row["verificationStatus"], "rejected")
		self.assertEqual(row["rejectionReason"], "Business registration documents did not match.")
		self.assertIsNotNone(row["reviewedOn"])

	def test_account_applications_requires_staff(self):
		frappe.set_user("Guest")
		try:
			with self.assertRaises(frappe.PermissionError):
				account_applications()
		finally:
			frappe.set_user("Administrator")

	def test_verify_and_reject_require_staff(self):
		frappe.set_user("Guest")
		try:
			with self.assertRaises(frappe.PermissionError):
				verify_account(SELLER)
			with self.assertRaises(frappe.PermissionError):
				reject_account(SELLER, "no")
		finally:
			frappe.set_user("Administrator")

	def test_demo_accounts_are_approved_not_pending(self):
		# Regression: ensure_demo_users() sets verified=1 directly (bypassing
		# verify_account), so it must also set verification_status explicitly —
		# otherwise the demo seller/buyer show up as "pending" on /admin/accounts.
		from accreage_mart.setup.install import ensure_demo_users

		ensure_demo_users()
		rows = {row["email"]: row for row in account_applications()}
		for email in ("buyer@demo.accreagemart.lk", "seller@demo.accreagemart.lk"):
			if email in rows:  # only present when developer_mode is on
				self.assertEqual(rows[email]["verificationStatus"], "approved")
