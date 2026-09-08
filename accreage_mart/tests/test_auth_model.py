import frappe
from frappe.tests.utils import FrappeTestCase

from accreage_mart.setup.install import SEED_ADMIN_EMAIL
from accreage_mart.utils.profile import (
	PLATFORM_ROLES,
	create_platform_user,
	get_account_status,
	get_primary_role,
	get_profile,
)


class TestAuthModel(FrappeTestCase):
	def test_platform_roles_exist(self):
		for role in PLATFORM_ROLES:
			self.assertTrue(frappe.db.exists("Role", role), f"Role {role} missing")

	def test_account_status_custom_field_exists(self):
		self.assertTrue(frappe.db.exists("Custom Field", "User-custom_account_status"))

	def test_profile_doctypes_exist(self):
		self.assertTrue(frappe.db.exists("DocType", "Buyer Profile"))
		self.assertTrue(frappe.db.exists("DocType", "Seller Profile"))

	def test_seed_admin_created_and_invited(self):
		self.assertTrue(frappe.db.exists("User", SEED_ADMIN_EMAIL))
		self.assertEqual(
			frappe.db.get_value("User", SEED_ADMIN_EMAIL, "custom_account_status"), "invited"
		)
		self.assertIn("Admin", frappe.get_roles(SEED_ADMIN_EMAIL))

	def test_administrator_maps_to_admin(self):
		self.assertEqual(get_primary_role("Administrator"), "admin")

	def test_role_precedence(self):
		email = "precedence.test@accreagemart.lk"
		if frappe.db.exists("User", email):
			frappe.delete_doc("User", email, force=True)
		user = create_platform_user(email=email, full_name="Precedence Test", role="Seller")
		user.add_roles("Staff")
		# Staff outranks Seller.
		self.assertEqual(get_primary_role(email), "staff")
		frappe.delete_doc("User", email, force=True)

	def test_role_less_user_has_no_primary_role(self):
		email = "roleless.test@accreagemart.lk"
		if frappe.db.exists("User", email):
			frappe.delete_doc("User", email, force=True)
		user = frappe.new_doc("User")
		user.email = email
		user.first_name = "Roleless"
		user.flags.no_welcome_mail = True
		user.insert(ignore_permissions=True)
		self.assertIsNone(get_primary_role(email))
		frappe.delete_doc("User", email, force=True)

	def test_create_platform_user_shape(self):
		email = "buyer.model.test@accreagemart.lk"
		if frappe.db.exists("User", email):
			frappe.delete_doc("User", email, force=True)

		user = create_platform_user(
			email=email, full_name="Buyer Model", role="Buyer", mobile="+94 77 000 0000"
		)
		self.assertEqual(user.user_type, "System User")
		self.assertEqual(user.enabled, 0)
		roles = frappe.get_roles(email)
		self.assertIn("Buyer", roles)
		self.assertIn("System Manager", roles)
		self.assertEqual(get_account_status(user), "invited")

		with self.assertRaises(frappe.DuplicateEntryError):
			create_platform_user(email=email, full_name="Dup", role="Buyer")

		frappe.delete_doc("User", email, force=True)

	def test_get_user_info_for_administrator(self):
		frappe.set_user("Administrator")
		try:
			from accreage_mart.api.auth import get_user_info

			info = get_user_info()
			self.assertEqual(info["role"], "admin")
			self.assertIn("status", info)
			self.assertIsNone(info["profile"])
			self.assertFalse(info["verified"])
		finally:
			frappe.set_user("Administrator")

	def test_get_profile_returns_none_without_profile(self):
		self.assertIsNone(get_profile("Administrator", "admin"))
