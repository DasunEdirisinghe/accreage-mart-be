import frappe
from frappe.tests.utils import FrappeTestCase

from accreage_mart.api.notifications import (
	list_notifications,
	list_notifications_page,
	mark_all_seen,
	mark_notification_seen,
)
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

	def test_list_notifications_page_paginates_only_the_current_users_rows(self):
		for i in range(5):
			notify("listing_approved", USER_A, f"For A {i}", "msg", ["sms"])
		notify("listing_approved", USER_B, "For B", "msg", ["sms"])

		frappe.set_user(USER_A)
		page_1 = list_notifications_page(page=1, page_size=2)
		page_2 = list_notifications_page(page=2, page_size=2)
		page_3 = list_notifications_page(page=3, page_size=2)

		self.assertEqual(page_1["total"], 5)
		self.assertEqual(len(page_1["notification_logs"]), 2)
		self.assertTrue(page_1["has_more"])
		self.assertEqual(len(page_2["notification_logs"]), 2)
		self.assertTrue(page_2["has_more"])
		self.assertEqual(len(page_3["notification_logs"]), 1)
		self.assertFalse(page_3["has_more"])

		seen_names = {row["name"] for row in (*page_1["notification_logs"], *page_2["notification_logs"], *page_3["notification_logs"])}
		self.assertEqual(len(seen_names), 5)

	def test_list_notifications_page_clamps_page_and_page_size(self):
		notify("listing_approved", USER_A, "For A", "msg", ["sms"])

		frappe.set_user(USER_A)
		result = list_notifications_page(page=0, page_size=10000)

		self.assertEqual(result["page"], 1)
		self.assertEqual(result["page_size"], 100)
