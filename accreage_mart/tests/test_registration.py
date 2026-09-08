import frappe
from frappe.tests.utils import FrappeTestCase

from accreage_mart.api.auth import create_staff, register_buyer, register_seller


class TestRegistration(FrappeTestCase):
	def _cleanup(self, *emails):
		for email in emails:
			if frappe.db.exists("User", email):
				frappe.delete_doc("User", email, force=True, ignore_permissions=True)

	def test_register_buyer_creates_invited_user_and_profile(self):
		email = "reg.buyer@x.lk"
		self._cleanup(email)
		self.addCleanup(self._cleanup, email)

		res = register_buyer(
			full_name="Reg Buyer",
			business_name="Reg Hotels",
			email=email,
			mobile="+94 77 111 2222",
			district="Colombo",
			buyer_type="Hotel",
		)
		self.assertTrue(res["ok"])

		user = frappe.get_doc("User", email)
		self.assertEqual(user.enabled, 0)
		self.assertEqual(user.custom_account_status, "invited")
		roles = frappe.get_roles(email)
		self.assertIn("Buyer", roles)
		self.assertIn("System Manager", roles)

		profile = frappe.get_doc("Buyer Profile", {"user": email})
		self.assertEqual(profile.buyer_type, "Hotel")
		self.assertEqual(profile.verified, 0)

	def test_register_buyer_rejects_bad_buyer_type(self):
		email = "reg.buyer2@x.lk"
		self._cleanup(email)
		self.addCleanup(self._cleanup, email)
		with self.assertRaises(frappe.ValidationError):
			register_buyer(
				full_name="X",
				business_name="Y",
				email=email,
				mobile="",
				district="Colombo",
				buyer_type="Airline",
			)

	def test_duplicate_active_email_rejected(self):
		email = "reg.dup@x.lk"
		self._cleanup(email)
		self.addCleanup(self._cleanup, email)

		register_buyer(
			full_name="Dup",
			business_name="Dup Co",
			email=email,
			mobile="",
			district="Colombo",
			buyer_type="Hotel",
		)
		frappe.db.set_value("User", email, {"enabled": 1, "custom_account_status": "active"})

		with self.assertRaises(frappe.DuplicateEntryError):
			register_seller(
				full_name="Dup", business_name="Dup Co", email=email, mobile="", district="Colombo"
			)

	def test_duplicate_invited_email_resends_without_error(self):
		email = "reg.inv@x.lk"
		self._cleanup(email)
		self.addCleanup(self._cleanup, email)

		register_buyer(
			full_name="Inv",
			business_name="Inv Co",
			email=email,
			mobile="",
			district="Colombo",
			buyer_type="Hotel",
		)
		res = register_buyer(
			full_name="Inv",
			business_name="Inv Co",
			email=email,
			mobile="",
			district="Colombo",
			buyer_type="Hotel",
		)
		self.assertTrue(res["ok"])

	def test_register_seller_profile_defaults(self):
		email = "reg.seller@x.lk"
		self._cleanup(email)
		self.addCleanup(self._cleanup, email)

		register_seller(
			full_name="Reg Seller",
			business_name="Reg Farms",
			email=email,
			mobile="",
			district="Kandy",
			description="A demo farm.",
		)
		profile = frappe.get_doc("Seller Profile", {"user": email})
		self.assertEqual(profile.verified, 0)
		self.assertEqual(profile.trust_score, 0)

	def test_create_staff_requires_admin(self):
		frappe.set_user("Guest")
		try:
			with self.assertRaises(frappe.PermissionError):
				create_staff(full_name="S", email="staff.x@x.lk", role="Staff")
		finally:
			frappe.set_user("Administrator")

	def test_create_staff_as_admin(self):
		email = "new.staff@x.lk"
		self._cleanup(email)
		self.addCleanup(self._cleanup, email)
		frappe.set_user("Administrator")

		res = create_staff(full_name="New Staff", email=email, role="Staff")
		self.assertEqual(res["email"], email)
		self.assertIn("Staff", frappe.get_roles(email))
		self.assertEqual(frappe.db.get_value("User", email, "custom_account_status"), "invited")
