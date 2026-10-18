import frappe
from frappe.tests.utils import FrappeTestCase

from accreage_mart.api.notifications import list_notifications, mark_all_seen, mark_notification_seen
from accreage_mart.utils.notify import notify
from accreage_mart.utils.profile import create_platform_user

USER_A = "notify.api.user.a@x.lk"
USER_B = "notify.api.user.b@x.lk"


class TestApiNotifications(FrappeTestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		self._purge()
		create_platform_user(email=USER_A, full_name="Notify Api User A", role="Buyer", status="active")
		create_platform_user(email=USER_B, full_name="Notify Api User B", role="Buyer", status="active")

	def tearDown(self):
		frappe.set_user("Administrator")
		self._purge()

	def _purge(self):
		for doctype in ("Notification Log",):
			for name in frappe.get_all(doctype, filters={"for_user": ["in", [USER_A, USER_B]]}, pluck="name"):
				frappe.delete_doc(doctype, name, force=True, ignore_permissions=True)
		for email in (USER_A, USER_B):
			if frappe.db.exists("User", email):
				frappe.delete_doc("User", email, force=True, ignore_permissions=True)

	def test_list_notifications_only_returns_the_current_users_rows(self):
		notify("listing_approved", USER_A, "For A", "msg", ["sms"])
		notify("listing_approved", USER_B, "For B", "msg", ["sms"])

		frappe.set_user(USER_A)
		rows = list_notifications()

		self.assertEqual(len(rows), 1)
		self.assertEqual(rows[0]["subject"], "For A")

	def test_mark_notification_seen_marks_that_row_read(self):
		log = notify("listing_approved", USER_A, "For A", "msg", ["sms"])

		frappe.set_user(USER_A)
		mark_notification_seen(log.name)

		self.assertEqual(frappe.db.get_value("Notification Log", log.name, "read"), 1)

	def test_mark_all_seen_marks_every_unread_row_for_the_current_user(self):
		first = notify("listing_approved", USER_A, "First", "msg", ["sms"])
		second = notify("listing_rejected", USER_A, "Second", "msg", ["sms"])
		notify("listing_approved", USER_B, "For B", "msg", ["sms"])

		frappe.set_user(USER_A)
		mark_all_seen()

		self.assertEqual(frappe.db.get_value("Notification Log", first.name, "read"), 1)
		self.assertEqual(frappe.db.get_value("Notification Log", second.name, "read"), 1)
		frappe.set_user("Administrator")
		self.assertEqual(
			frappe.db.get_value(
				"Notification Log", {"for_user": USER_B}, "read"
			),
			0,
		)
