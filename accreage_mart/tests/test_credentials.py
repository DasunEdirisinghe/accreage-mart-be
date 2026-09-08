import frappe
from frappe.tests.utils import FrappeTestCase

from accreage_mart.api.auth import request_password_reset, resend_activation, set_password
from accreage_mart.utils.credentials import consume_key, validate_password_policy
from accreage_mart.utils.profile import create_platform_user

INVITEE = "credflow.test@accreagemart.lk"


class TestCredentials(FrappeTestCase):
	def setUp(self):
		for email in (INVITEE,):
			if frappe.db.exists("User", email):
				frappe.delete_doc("User", email, force=True, ignore_permissions=True)
		create_platform_user(email=INVITEE, full_name="Cred Flow", role="Buyer")

	def tearDown(self):
		if frappe.db.exists("User", INVITEE):
			frappe.delete_doc("User", INVITEE, force=True, ignore_permissions=True)

	def _issue_key(self) -> str:
		res = request_password_reset(INVITEE)
		self.assertTrue(res["ok"])
		# Delivery mechanism (email vs dev_link) depends on site SMTP config; the
		# key itself always lands on the User doc.
		key = frappe.db.get_value("User", INVITEE, "reset_password_key")
		self.assertTrue(key)
		return key

	def test_request_reset_is_generic_for_unknown_email(self):
		res = request_password_reset("nobody@nowhere.lk")
		self.assertEqual(res, {"ok": True})

	def test_set_password_activates_the_account(self):
		key = self._issue_key()
		out = set_password(key, "Harvest2026")
		self.assertEqual(out["email"], INVITEE)

		user = frappe.get_doc("User", INVITEE)
		self.assertEqual(user.enabled, 1)
		self.assertEqual(user.custom_account_status, "active")
		self.assertFalse(user.reset_password_key)

	def test_key_is_single_use(self):
		key = self._issue_key()
		set_password(key, "Harvest2026")
		with self.assertRaises(frappe.ValidationError):
			set_password(key, "Harvest2026")

	def test_password_policy(self):
		with self.assertRaises(frappe.ValidationError):
			validate_password_policy("Har2", INVITEE)  # too short
		with self.assertRaises(frappe.ValidationError):
			validate_password_policy("abcdefgh", INVITEE)  # no number
		with self.assertRaises(frappe.ValidationError):
			validate_password_policy("12345678", INVITEE)  # no letter
		with self.assertRaises(frappe.ValidationError):
			validate_password_policy("credflow.test", INVITEE)  # equals local part
		validate_password_policy("Harvest2026", INVITEE)  # fine

	def test_resend_activation_only_for_invited(self):
		res = resend_activation(INVITEE)
		self.assertTrue(res["ok"])
		key = frappe.db.get_value("User", INVITEE, "reset_password_key")
		self.assertTrue(key)

		# Activate, then resend should be a no-op that issues no new key.
		set_password(key, "Harvest2026")
		frappe.db.set_value("User", INVITEE, "reset_password_key", "")
		resend_activation(INVITEE)
		self.assertFalse(frappe.db.get_value("User", INVITEE, "reset_password_key"))

	def test_consume_key_rejects_unknown_key(self):
		with self.assertRaises(frappe.ValidationError):
			consume_key("not-a-real-key", "Harvest2026")
