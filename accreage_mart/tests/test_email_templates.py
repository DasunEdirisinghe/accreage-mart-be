from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from accreage_mart.setup.install import EMAIL_TEMPLATES, ensure_email_templates
from accreage_mart.utils.email import send_templated_email


class TestEmailTemplates(FrappeTestCase):
	def test_all_expected_templates_exist(self):
		ensure_email_templates()
		for name in EMAIL_TEMPLATES:
			self.assertTrue(frappe.db.exists("Email Template", name), f"missing template: {name}")

	def test_ensure_email_templates_is_idempotent_and_preserves_edits(self):
		ensure_email_templates()
		frappe.db.set_value("Email Template", "welcome_member", "subject", "Custom edited subject")

		ensure_email_templates()  # a second run must not clobber the edit

		self.assertEqual(
			frappe.db.get_value("Email Template", "welcome_member", "subject"),
			"Custom edited subject",
		)

	def test_template_renders_placeholders(self):
		ensure_email_templates()
		template = frappe.get_doc("Email Template", "account_approved")
		formatted = template.get_formatted_email({"full_name": "Jane Doe", "business_name": "Jane Farms"})

		self.assertIn("Jane Doe", formatted["message"])
		self.assertIn("Jane Farms", formatted["message"])
		self.assertNotIn("{{", formatted["message"])
		self.assertNotIn("{{", formatted["subject"])

	def test_send_templated_email_includes_rendered_content_and_cta(self):
		ensure_email_templates()

		with patch("frappe.sendmail") as mock_sendmail:
			send_templated_email(
				key="password_reset",
				recipient="someone@example.com",
				context={"full_name": "Jane Doe"},
				cta_label="Choose a new password",
				cta_url="https://example.com/set-password?key=abc",
				footer_note="This link expires in 1 hour.",
			)

		self.assertEqual(mock_sendmail.call_count, 1)
		kwargs = mock_sendmail.call_args.kwargs
		self.assertEqual(kwargs["recipients"], ["someone@example.com"])
		self.assertEqual(kwargs["subject"], "Reset your Accreage Mart password")
		self.assertIn("Jane Doe", kwargs["message"])
		self.assertIn("https://example.com/set-password?key=abc", kwargs["message"])
		self.assertIn("Choose a new password", kwargs["message"])
		self.assertIn("This link expires in 1 hour.", kwargs["message"])
		# Regression: `content=` would silently replace the HTML `message` outright
		# (frappe.sendmail does `message = content or message`), which is exactly
		# the bug that made every branded email send as plain text. Must never
		# be passed alongside `message`.
		self.assertNotIn("content", kwargs)

	def test_send_templated_email_without_cta_omits_the_button(self):
		ensure_email_templates()

		with patch("frappe.sendmail") as mock_sendmail:
			send_templated_email(
				key="account_rejected",
				recipient="someone@example.com",
				context={"full_name": "Jane Doe", "business_name": "Jane Farms", "reason": "Missing details"},
			)

		kwargs = mock_sendmail.call_args.kwargs
		self.assertIn("Missing details", kwargs["message"])
		self.assertNotIn("If the button doesn't work", kwargs["message"])
