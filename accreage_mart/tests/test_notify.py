from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from accreage_mart.utils.notify import notify
from accreage_mart.utils.profile import create_platform_user

NOTIFY = "accreage_mart.utils.notify"
USER = "notify.test.user@x.lk"


class TestNotify(FrappeTestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		self._purge()
		create_platform_user(email=USER, full_name="Notify Test User", role="Buyer", status="active")

	def tearDown(self):
		frappe.set_user("Administrator")
		self._purge()

	def _purge(self):
		if frappe.db.exists("User", USER):
			frappe.delete_doc("User", USER, force=True, ignore_permissions=True)

	def test_creates_one_notification_log_row_with_one_delivery_row_per_channel(self):
		with patch(f"{NOTIFY}.smtp_configured", return_value=True), patch(f"{NOTIFY}.send_templated_email"):
			log = notify(
				"listing_approved",
				USER,
				"Your listing is live",
				"Carrots approved.",
				["email", "sms"],
			)

		self.assertEqual(log.doctype, "Notification Log")
		self.assertEqual(log.for_user, USER)
		self.assertEqual(log.custom_event_type, "listing_approved")
		self.assertEqual(len(log.custom_delivery_log), 2)
		channels = {row.channel for row in log.custom_delivery_log}
		self.assertEqual(channels, {"Email", "SMS"})

	def test_document_type_and_name_are_stored_for_click_through(self):
		with patch(f"{NOTIFY}.smtp_configured", return_value=False):
			log = notify(
				"listing_rejected",
				USER,
				"Needs changes",
				"See reason.",
				["email"],
				document_type="Listing",
				document_name="LIST-0001",
			)

		self.assertEqual(log.document_type, "Listing")
		self.assertEqual(log.document_name, "LIST-0001")

	def test_email_channel_sends_via_send_templated_email_when_template_given(self):
		with patch(f"{NOTIFY}.smtp_configured", return_value=True), patch(
			f"{NOTIFY}.send_templated_email"
		) as mock_send:
			log = notify(
				"listing_rejected",
				USER,
				"Needs changes",
				"See reason.",
				["email"],
				email_template="listing_rejected",
				email_context={"reason": "Bad photos"},
			)

		mock_send.assert_called_once()
		self.assertEqual(mock_send.call_args.kwargs["key"], "listing_rejected")
		self.assertEqual(mock_send.call_args.kwargs["context"], {"reason": "Bad photos"})
		row = log.custom_delivery_log[0]
		self.assertEqual(row.status, "Sent")
		self.assertIsNotNone(row.sent_at)

	def test_email_channel_without_template_falls_back_to_plain_sendmail(self):
		with patch(f"{NOTIFY}.smtp_configured", return_value=True), patch("frappe.sendmail") as mock_sendmail:
			log = notify("order_received", USER, "New order", "You have a new order.", ["email"])

		mock_sendmail.assert_called_once()
		self.assertEqual(log.custom_delivery_log[0].status, "Sent")

	def test_email_channel_logs_failed_when_smtp_not_configured(self):
		with patch(f"{NOTIFY}.smtp_configured", return_value=False):
			log = notify("listing_approved", USER, "Title", "Message", ["email"])

		row = log.custom_delivery_log[0]
		self.assertEqual(row.status, "Failed")
		self.assertEqual(row.error, "SMTP not configured")

	def test_sms_channel_always_logs_not_implemented(self):
		log = notify("auction_outbid", USER, "Outbid", "You were outbid.", ["sms"])

		row = log.custom_delivery_log[0]
		self.assertEqual(row.channel, "SMS")
		self.assertEqual(row.status, "Not Implemented")

	def test_unknown_channel_raises(self):
		with self.assertRaises(frappe.ValidationError):
			notify("listing_approved", USER, "Title", "Message", ["carrier-pigeon"])

	def test_email_send_exception_is_logged_on_the_delivery_row_not_raised(self):
		with patch(f"{NOTIFY}.smtp_configured", return_value=True), patch(
			"frappe.sendmail", side_effect=Exception("smtp down")
		):
			log = notify("listing_approved", USER, "Title", "Message", ["email"])

		row = log.custom_delivery_log[0]
		self.assertEqual(row.status, "Failed")
		self.assertEqual(row.error, "smtp down")
