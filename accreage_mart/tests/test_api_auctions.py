import threading
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

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

BUYER_A = "auction.buyer.a@x.lk"
BUYER_B = "auction.buyer.b@x.lk"
NOTIFY = "accreage_mart.utils.notify"


class AuctionApiTestCase(FrappeTestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		purge(SELLER_A, BUYER_A, BUYER_B)
		self.seller = make_seller(SELLER_A)
		self.category = make_category()
		self.buyer_a = make_buyer(BUYER_A)
		self.buyer_b = make_buyer(BUYER_B)

	def tearDown(self):
		frappe.set_user("Administrator")
		purge(SELLER_A, BUYER_A, BUYER_B)

	def live_auction(self, **overrides):
		"""A live auction (approved listing, window already started)."""
		listing, auction = make_auction_listing(self.seller, self.category, **overrides)
		set_status(listing.name, "Approved")
		start_auction_now(auction.name)
		return listing, auction


class TestGetAuctionDetail(AuctionApiTestCase):
	def test_returns_listing_bid_history_newest_first_and_highest_bid(self):
		listing, auction = self.live_auction()
		frappe.set_user(BUYER_A)
		api.place_bid(auction.name, 150)
		frappe.set_user(BUYER_B)
		api.place_bid(auction.name, 200)

		frappe.set_user("Guest")
		detail = api.get_auction_detail(auction.name)
		self.assertEqual(detail["auction"]["status"], "live")
		self.assertEqual(detail["listing"]["name"], listing.name)
		self.assertEqual([b["amount"] for b in detail["bids"]], [200, 150])
		self.assertEqual(detail["highest_bid"]["amount"], 200)
		self.assertEqual(detail["highest_bid"]["bidder"], BUYER_B)

	def test_no_bids_yet_has_no_highest_bid(self):
		_, auction = self.live_auction()
		detail = api.get_auction_detail(auction.name)
		self.assertEqual(detail["bids"], [])
		self.assertIsNone(detail["highest_bid"])

	def test_unknown_auction(self):
		with self.assertRaises(frappe.DoesNotExistError):
			api.get_auction_detail("AUC-99999999")


class TestPlaceBid(AuctionApiTestCase):
	def test_only_a_logged_in_buyer_in_good_standing_can_bid(self):
		_, auction = self.live_auction()

		for user in ("Guest", self.seller):
			frappe.set_user(user)
			with self.assertRaises((frappe.PermissionError, frappe.AuthenticationError)):
				api.place_bid(auction.name, 150)

		frappe.set_user("Administrator")
		frappe.db.set_value("User", BUYER_A, "custom_account_status", "suspended")
		frappe.set_user(BUYER_A)
		with self.assertRaises(frappe.PermissionError):
			api.place_bid(auction.name, 150)

	def test_rejects_a_non_live_auction(self):
		# Not approved yet: still "pending".
		_, auction = make_auction_listing(self.seller, self.category)
		frappe.set_user(BUYER_A)
		with self.assertRaises(frappe.ValidationError):
			api.place_bid(auction.name, 150)

	def test_rejects_an_amount_that_does_not_exceed_the_floor(self):
		_, auction = self.live_auction()  # min_bid 100
		frappe.set_user(BUYER_A)
		with self.assertRaises(frappe.ValidationError):
			api.place_bid(auction.name, 100)
		api.place_bid(auction.name, 150)

		frappe.set_user(BUYER_B)
		with self.assertRaises(frappe.ValidationError):
			api.place_bid(auction.name, 150)
		with self.assertRaises(frappe.ValidationError):
			api.place_bid(auction.name, 140)

	def test_rejects_self_bidding_by_the_seller(self):
		_, auction = self.live_auction()
		# Simulate a seller who also holds a Buyer account - the role gate alone wouldn't
		# cover this, so the self-bid guard must.
		frappe.get_doc("User", self.seller).add_roles("Buyer")
		frappe.get_doc(
			{
				"doctype": "Buyer Profile",
				"user": self.seller,
				"business_name": "Seller As Buyer",
				"buyer_type": "Hotel",
				"district": "Colombo",
				"verified": 1,
			}
		).insert(ignore_permissions=True)

		frappe.set_user(self.seller)
		with self.assertRaises(frappe.PermissionError):
			api.place_bid(auction.name, 150)

	def test_the_first_bid_never_notifies_anyone(self):
		_, auction = self.live_auction()
		frappe.set_user(BUYER_A)
		with patch(f"{NOTIFY}.smtp_configured", return_value=True), patch(
			f"{NOTIFY}.send_templated_email"
		) as send:
			api.place_bid(auction.name, 150)
		send.assert_not_called()

	def test_a_successful_bid_that_unseats_the_previous_highest_bidder_notifies_them(self):
		listing, auction = self.live_auction()
		frappe.set_user(BUYER_A)
		api.place_bid(auction.name, 150)

		frappe.set_user(BUYER_B)
		with patch(f"{NOTIFY}.smtp_configured", return_value=True), patch(
			f"{NOTIFY}.send_templated_email"
		) as send:
			result = api.place_bid(auction.name, 200)

		self.assertEqual((result["bidder"], float(result["amount"])), (BUYER_B, 200.0))
		send.assert_called_once()
		self.assertEqual(send.call_args.kwargs["key"], "auction_outbid")
		self.assertEqual(send.call_args.kwargs["recipient"], BUYER_A)
		self.assertEqual(send.call_args.kwargs["context"]["title"], listing.title)
		self.assertEqual(send.call_args.kwargs["context"]["amount"], 200)

		log_name = frappe.db.get_value(
			"Notification Log", {"for_user": BUYER_A, "custom_event_type": "auction_outbid"}, "name"
		)
		log = frappe.get_doc("Notification Log", log_name)
		self.assertEqual((log.document_type, log.document_name), ("Auction", auction.name))


class TestPlaceBidConcurrency(AuctionApiTestCase):
	def test_two_near_simultaneous_bids_only_the_valid_higher_one_persists(self):
		"""Two buyers bid the same amount at nearly the same time. Without the DB-level lock
		both could read the same stale "current highest" and both get accepted - a double
		accept where the second bid doesn't actually exceed the (by-then) real highest. With
		the lock, exactly one of the two calls succeeds and the other is rejected."""
		_, auction = self.live_auction()

		# Two real DB connections need to see this data, so it has to actually be committed -
		# other tests rely on the class-level rollback instead of a real commit.
		frappe.db.commit()

		site = frappe.local.site
		sites_path = frappe.local.sites_path
		barrier = threading.Barrier(2)
		results = {}

		def worker(user, amount, key):
			frappe.init(site=site, sites_path=sites_path)
			frappe.connect()
			try:
				frappe.set_user(user)
				barrier.wait(timeout=5)
				try:
					results[key] = ("ok", api.place_bid(auction.name, amount))
				except Exception as exc:
					results[key] = ("error", exc)
			finally:
				frappe.destroy()

		threads = [
			threading.Thread(target=worker, args=(BUYER_A, 150, "a")),
			threading.Thread(target=worker, args=(BUYER_B, 150, "b")),
		]
		for t in threads:
			t.start()
		for t in threads:
			t.join(timeout=15)

		try:
			outcomes = [results.get("a"), results.get("b")]
			self.assertTrue(all(outcomes), "both threads should have finished")
			self.assertEqual(sum(1 for o in outcomes if o[0] == "ok"), 1)
			self.assertEqual(sum(1 for o in outcomes if o[0] == "error"), 1)

			# A fresh read on the primary connection: commit() starts a new transaction so
			# this doesn't see a stale pre-worker snapshot.
			frappe.db.commit()
			bids = frappe.get_all(
				"Bid", filters={"auction": auction.name}, fields=["bidder", "amount"], ignore_permissions=True
			)
			self.assertEqual(len(bids), 1)
			self.assertEqual(float(bids[0].amount), 150.0)
		finally:
			# This test committed real rows for two independent DB connections to see, so
			# cleanup must be committed too - the usual rely-on-final-rollback isolation
			# doesn't apply once anything has actually been committed.
			purge(SELLER_A, BUYER_A, BUYER_B)
			frappe.db.commit()
