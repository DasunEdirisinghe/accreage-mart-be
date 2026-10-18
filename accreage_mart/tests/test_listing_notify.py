from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from accreage_mart.tests.listing_fixtures import make_category, make_listing, make_seller, purge
from accreage_mart.utils.listing_notify import notify_low_stock, notify_status_change

SELLER = "listing.notify.seller@x.lk"
NOTIFY = "accreage_mart.utils.notify"


class TestListingNotify(FrappeTestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		purge(SELLER)
		self.seller = make_seller(SELLER)
		self.category = make_category()

	def tearDown(self):
		purge(SELLER)

	def latest_log(self, listing_name: str):
		name = frappe.get_all(
			"Notification Log",
			filters={"document_type": "Listing", "document_name": listing_name},
			order_by="creation desc",
			limit=1,
			pluck="name",
		)
		self.assertTrue(name, "expected a Notification Log row for this listing")
		return frappe.get_doc("Notification Log", name[0])

	def test_status_change_creates_a_notification_log_row_per_action(self):
		cases = [
			("Approved", None, "listing_approved"),
			("Rejected", "Blurry photos.", "listing_rejected"),
			("Suspended", "Reported as misleading.", "listing_suspended"),
		]
		for action, reason, event in cases:
			listing = make_listing(self.seller, self.category)
			with patch(f"{NOTIFY}.smtp_configured", return_value=True), patch(f"{NOTIFY}.send_templated_email"):
				notify_status_change(listing, action, reason)
			log = self.latest_log(listing.name)
			self.assertEqual(log.for_user, SELLER)
			self.assertEqual(log.custom_event_type, event)
			self.assertEqual(log.document_type, "Listing")
			self.assertEqual(log.document_name, listing.name)
			self.assertEqual(len(log.custom_delivery_log), 1)
			self.assertEqual(log.custom_delivery_log[0].channel, "Email")

	def test_low_stock_creates_a_notification_log_row(self):
		listing = make_listing(self.seller, self.category, quantity_available=80, low_stock_level=100)
		with patch(f"{NOTIFY}.smtp_configured", return_value=True), patch(f"{NOTIFY}.send_templated_email"):
			notify_low_stock(listing)
		log = self.latest_log(listing.name)
		self.assertEqual(log.custom_event_type, "listing_low_stock")
		self.assertEqual(log.document_type, "Listing")
		self.assertEqual(log.document_name, listing.name)

	def test_status_change_still_sends_the_same_templated_email(self):
		listing = make_listing(self.seller, self.category)
		with patch(f"{NOTIFY}.smtp_configured", return_value=True), patch(
			f"{NOTIFY}.send_templated_email"
		) as send:
			notify_status_change(listing, "Rejected", "Photos are blurry.")
		self.assertEqual(send.call_args.kwargs["key"], "listing_rejected")
		self.assertEqual(send.call_args.kwargs["recipient"], SELLER)
		self.assertEqual(send.call_args.kwargs["context"]["reason"], "Photos are blurry.")
		self.assertEqual(send.call_args.kwargs["context"]["title"], listing.title)

	def test_low_stock_still_sends_the_same_templated_email(self):
		listing = make_listing(self.seller, self.category, quantity_available=80, low_stock_level=100)
		with patch(f"{NOTIFY}.smtp_configured", return_value=True), patch(
			f"{NOTIFY}.send_templated_email"
		) as send:
			notify_low_stock(listing)
		self.assertEqual(send.call_args.kwargs["key"], "listing_low_stock")
		self.assertEqual(send.call_args.kwargs["recipient"], SELLER)
		self.assertEqual(send.call_args.kwargs["context"]["quantity"], 80)
		self.assertEqual(send.call_args.kwargs["context"]["low_stock_level"], 100)

	def test_no_notification_log_without_a_resolvable_seller_user(self):
		listing = make_listing(self.seller, self.category)
		frappe.db.set_value("Seller Profile", self.seller, "user", None, update_modified=False)
		with patch(f"{NOTIFY}.smtp_configured", return_value=True), patch(f"{NOTIFY}.send_templated_email"):
			notify_status_change(listing, "Approved")
		self.assertEqual(
			frappe.get_all("Notification Log", filters={"document_type": "Listing", "document_name": listing.name}),
			[],
		)

	def test_a_failing_email_never_blocks_the_notification(self):
		listing = make_listing(self.seller, self.category)
		with patch(f"{NOTIFY}.smtp_configured", return_value=True), patch(
			f"{NOTIFY}.send_templated_email", side_effect=RuntimeError("smtp down")
		):
			notify_status_change(listing, "Approved")
		log = self.latest_log(listing.name)
		self.assertEqual(log.custom_delivery_log[0].status, "Failed")
