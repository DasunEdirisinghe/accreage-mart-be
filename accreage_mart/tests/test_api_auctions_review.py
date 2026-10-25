from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import add_to_date, now_datetime

from accreage_mart.api import auctions as api
from accreage_mart.tests.listing_fixtures import (
	SELLER_A,
	make_auction_listing,
	make_buyer,
	make_category,
	make_seller,
	purge,
	set_status,
	start_auction_now,
)
from accreage_mart.utils import listing as lu
from accreage_mart.utils.profile import create_platform_user

STAFF = "auction.review.staff@x.lk"
BUYER_A = "auction.review.buyer.a@x.lk"
BUYER_B = "auction.review.buyer.b@x.lk"
NOTIFY = "accreage_mart.utils.notify"


class AuctionReviewTestCase(FrappeTestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		purge(SELLER_A, BUYER_A, BUYER_B, STAFF)
		self.seller = make_seller(SELLER_A)
		self.category = make_category()
		self.buyer_a = make_buyer(BUYER_A)
		self.buyer_b = make_buyer(BUYER_B)
		create_platform_user(email=STAFF, full_name="Test Staff", role="Staff", status="active")

	def tearDown(self):
		frappe.set_user("Administrator")
		purge(SELLER_A, BUYER_A, BUYER_B, STAFF)

	def as_staff(self):
		frappe.set_user(STAFF)

	def pending_auction(self, **overrides):
		return make_auction_listing(self.seller, self.category, **overrides)

	def live_auction(self, **overrides):
		listing, auction = self.pending_auction(**overrides)
		set_status(listing.name, "Approved")
		start_auction_now(auction.name)
		return listing, auction

	def status_of(self, auction):
		doc = frappe.get_doc("Auction", auction.name)
		listing_status = frappe.db.get_value("Listing", {"auction": auction.name}, "status")
		return lu.derive_auction_status(
			listing_status, doc.start_time, doc.end_time, cancelled=doc.cancelled
		)

	def notification_events(self, auction_name, user=None):
		filters = {"document_type": "Auction", "document_name": auction_name}
		if user:
			filters["for_user"] = user
		return frappe.get_all(
			"Notification Log", filters=filters, pluck="custom_event_type", ignore_permissions=True
		)


class TestReviewAuction(AuctionReviewTestCase):
	def test_only_staff_can_review(self):
		listing, auction = self.pending_auction()
		for user in (SELLER_A, "Guest"):
			frappe.set_user(user)
			with self.assertRaises((frappe.PermissionError, frappe.AuthenticationError)):
				api.review_auction(auction.name, "approve")

	def test_approve_schedules_a_pending_auction(self):
		listing, auction = self.pending_auction()
		self.as_staff()
		result = api.review_auction(auction.name, "approve")
		self.assertEqual(result["status"], "scheduled")
		self.assertEqual(frappe.db.get_value("Listing", listing.name, "status"), "Approved")

	def test_approve_moves_straight_to_live_if_start_already_passed(self):
		listing, auction = self.pending_auction()
		start_auction_now(auction.name)
		self.as_staff()
		result = api.review_auction(auction.name, "approve")
		self.assertEqual(result["status"], "live")

	def test_reject_requires_a_reason(self):
		listing, auction = self.pending_auction()
		self.as_staff()
		with self.assertRaises(frappe.ValidationError):
			api.review_auction(auction.name, "reject")

	def test_reject_sets_the_listing_rejected_and_notifies_the_seller(self):
		listing, auction = self.pending_auction()
		self.as_staff()
		with patch(f"{NOTIFY}.smtp_configured", return_value=True), patch(
			f"{NOTIFY}.send_templated_email"
		) as send:
			result = api.review_auction(auction.name, "reject", "Fair value too far off.")
		self.assertEqual(result["status"], "rejected")
		self.assertEqual(frappe.db.get_value("Listing", listing.name, "status"), "Rejected")
		send.assert_called_once()
		self.assertEqual(send.call_args.kwargs["key"], "auction_rejected")
		self.assertEqual(send.call_args.kwargs["recipient"], SELLER_A)
		self.assertEqual(send.call_args.kwargs["context"]["reason"], "Fair value too far off.")
		self.assertEqual(self.notification_events(auction.name, SELLER_A), ["auction_rejected"])

	def test_cancelled_and_cancel_reason_are_system_managed(self):
		_, auction = self.live_auction()
		frappe.set_user(SELLER_A)
		doc = frappe.get_doc("Auction", auction.name)
		doc.cancelled = 1
		doc.cancel_reason = "Trying to self-cancel."
		with self.assertRaises(frappe.ValidationError):
			doc.save(ignore_permissions=True)

	def test_unknown_decision_is_rejected(self):
		listing, auction = self.pending_auction()
		self.as_staff()
		with self.assertRaises(frappe.ValidationError):
			api.review_auction(auction.name, "bogus")

	def test_only_a_pending_auction_can_be_reviewed(self):
		listing, auction = self.live_auction()
		self.as_staff()
		with self.assertRaises(frappe.ValidationError):
			api.review_auction(auction.name, "approve")


class TestCancelAuction(AuctionReviewTestCase):
	def test_only_staff_can_cancel(self):
		_, auction = self.live_auction()
		for user in (SELLER_A, "Guest"):
			frappe.set_user(user)
			with self.assertRaises((frappe.PermissionError, frappe.AuthenticationError)):
				api.cancel_auction(auction.name, "Dispute raised.")

	def test_cancel_requires_a_reason(self):
		_, auction = self.live_auction()
		self.as_staff()
		with self.assertRaises(frappe.ValidationError):
			api.cancel_auction(auction.name, "")

	def test_cancel_is_only_allowed_while_scheduled_or_live(self):
		listing, auction = self.pending_auction()
		self.as_staff()
		with self.assertRaises(frappe.ValidationError):
			api.cancel_auction(auction.name, "Too early.")

		api.review_auction(auction.name, "reject", "Not viable.")
		with self.assertRaises(frappe.ValidationError):
			api.cancel_auction(auction.name, "Already rejected.")

	def test_cancelling_sets_the_reason_and_derived_status(self):
		listing, auction = self.live_auction()
		self.as_staff()
		result = api.cancel_auction(auction.name, "Quality dispute raised by a buyer.")
		self.assertEqual(result["status"], "cancelled")
		doc = frappe.get_doc("Auction", auction.name)
		self.assertTrue(doc.cancelled)
		self.assertEqual(doc.cancel_reason, "Quality dispute raised by a buyer.")
		self.assertEqual(self.status_of(auction), "cancelled")

	def test_cancelling_a_live_auction_blocks_further_bids(self):
		_, auction = self.live_auction()
		frappe.set_user(BUYER_A)
		api.place_bid(auction.name, 150)

		self.as_staff()
		api.cancel_auction(auction.name, "Seller withdrew the lot.")

		frappe.set_user(BUYER_B)
		with self.assertRaises(frappe.ValidationError):
			api.place_bid(auction.name, 200)

	def test_cancel_notifies_the_seller_and_every_bidder(self):
		listing, auction = self.live_auction()
		frappe.set_user(BUYER_A)
		api.place_bid(auction.name, 150)
		frappe.set_user(BUYER_B)
		api.place_bid(auction.name, 200)

		self.as_staff()
		with patch(f"{NOTIFY}.smtp_configured", return_value=True), patch(
			f"{NOTIFY}.send_templated_email"
		) as send:
			api.cancel_auction(auction.name, "Quality dispute.")

		self.assertEqual(send.call_count, 3)
		recipients = {call.kwargs["recipient"] for call in send.call_args_list}
		self.assertEqual(recipients, {SELLER_A, BUYER_A, BUYER_B})
		for call in send.call_args_list:
			self.assertEqual(call.kwargs["context"]["reason"], "Quality dispute.")
		self.assertEqual(
			sorted(self.notification_events(auction.name)),
			sorted(["auction_cancelled"] * 3),
		)


class TestCloseExpiredAuctions(AuctionReviewTestCase):
	def ended_auction(self, **overrides):
		listing, auction = self.pending_auction(**overrides)
		set_status(listing.name, "Approved")
		frappe.db.set_value(
			"Auction",
			auction.name,
			{"start_time": add_to_date(now_datetime(), hours=-7), "end_time": add_to_date(now_datetime(), hours=-1)},
			update_modified=False,
		)
		return listing, auction

	def test_closes_with_a_winner_and_notifies_winner_and_seller_once_each(self):
		listing, auction = self.live_auction()
		frappe.set_user(BUYER_A)
		api.place_bid(auction.name, 150)
		frappe.set_user(BUYER_B)
		api.place_bid(auction.name, 200)
		frappe.db.set_value("Auction", auction.name, "end_time", add_to_date(now_datetime(), hours=-1))

		with patch(f"{NOTIFY}.smtp_configured", return_value=True), patch(
			f"{NOTIFY}.send_templated_email"
		) as send:
			api.close_expired_auctions()
			api.close_expired_auctions()  # idempotent re-run

		self.assertEqual(send.call_count, 2)
		recipients = {call.kwargs["recipient"] for call in send.call_args_list}
		self.assertEqual(recipients, {SELLER_A, BUYER_B})
		self.assertEqual(
			sorted(self.notification_events(auction.name)), sorted(["auction_won", "auction_sold"])
		)

	def test_closes_with_no_bids_and_notifies_only_the_seller_once(self):
		listing, auction = self.ended_auction()

		with patch(f"{NOTIFY}.smtp_configured", return_value=True), patch(
			f"{NOTIFY}.send_templated_email"
		) as send:
			api.close_expired_auctions()
			api.close_expired_auctions()  # idempotent re-run

		send.assert_called_once()
		self.assertEqual(send.call_args.kwargs["recipient"], SELLER_A)
		self.assertEqual(self.notification_events(auction.name), ["auction_ended_no_bids"])

	def test_a_cancelled_auction_is_never_closed(self):
		listing, auction = self.live_auction()
		self.as_staff()
		api.cancel_auction(auction.name, "Withdrawn.")
		frappe.db.set_value("Auction", auction.name, "end_time", add_to_date(now_datetime(), hours=-1))

		with patch(f"{NOTIFY}.smtp_configured", return_value=True), patch(
			f"{NOTIFY}.send_templated_email"
		) as send:
			api.close_expired_auctions()

		self.assertEqual(self.notification_events(auction.name), ["auction_cancelled"])
		send.assert_not_called()
