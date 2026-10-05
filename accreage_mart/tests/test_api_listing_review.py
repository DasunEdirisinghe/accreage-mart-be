from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from accreage_mart.api import listing_review as api
from accreage_mart.api import listings as seller_api
from accreage_mart.api import marketplace
from accreage_mart.tests.listing_fixtures import (
	SELLER_A,
	SELLER_B,
	make_auction_listing,
	make_category,
	make_listing,
	make_seller,
	purge,
	set_status,
	start_auction_now,
)
from accreage_mart.utils import listing as lu
from accreage_mart.utils.profile import create_platform_user

STAFF = "listing.staff@x.lk"
NOTIFY = "accreage_mart.utils.listing_notify"


class ReviewTestCase(FrappeTestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		purge(SELLER_A, SELLER_B, STAFF)
		self.seller = make_seller(SELLER_A)
		self.other = make_seller(SELLER_B)
		self.category = make_category()
		create_platform_user(email=STAFF, full_name="Test Staff", role="Staff", status="active")

	def tearDown(self):
		purge(SELLER_A, SELLER_B, STAFF)

	def as_staff(self):
		frappe.set_user(STAFF)

	def pending(self, **overrides):
		return make_listing(self.seller, self.category, **overrides)

	def token(self, listing):
		return api.get_listing_for_review(listing.name)["expected_modified"]

	def status_of(self, listing):
		return frappe.db.get_value("Listing", listing.name, "status")

	def reviews_of(self, listing):
		return frappe.get_all(
			"Listing Review",
			filters={"listing": listing.name},
			fields=["action", "reason", "reviewer"],
			order_by="creation asc",
		)

	def public_names(self):
		"""What a guest sees on the marketplace for seller A (leaves the session as Guest)."""
		frappe.set_user("Guest")
		items = marketplace.list_marketplace(seller=lu.public_seller_id(SELLER_A))["items"]
		return {item["name"] for item in items}

	def queue(self, **kwargs):
		kwargs.setdefault("search", "ZZ Test")
		return api.list_listings_for_review(**kwargs)


class TestQueue(ReviewTestCase):
	def test_only_staff_can_use_the_review_endpoints(self):
		listing = self.pending()
		for user in (SELLER_A, "Guest"):
			frappe.set_user(user)
			with self.assertRaises((frappe.PermissionError, frappe.AuthenticationError)):
				api.list_listings_for_review()
			with self.assertRaises((frappe.PermissionError, frappe.AuthenticationError)):
				api.get_listing_for_review(listing.name)
			with self.assertRaises((frappe.PermissionError, frappe.AuthenticationError)):
				api.approve_listing(listing.name, "2026-01-01 00:00:00")
		frappe.set_user("Administrator")
		self.assertEqual(self.status_of(listing), "Pending Approval")

	def test_the_queue_holds_both_types_oldest_first(self):
		first = self.pending(title="ZZ Test First")
		auction, _ = make_auction_listing(self.seller, self.category)
		last = self.pending(title="ZZ Test Last")
		set_status(self.pending(title="ZZ Test Live").name, "Approved")
		self.as_staff()
		result = self.queue()
		names = [item["name"] for item in result["items"]]
		self.assertEqual(names[:3], [first.name, auction.name, last.name])
		by_name = {item["name"]: item for item in result["items"]}
		self.assertEqual(by_name[first.name]["selling_type"], "Direct")
		self.assertEqual(by_name[auction.name]["auction"]["min_bid"], 100)
		self.assertFalse(by_name[auction.name]["auction"]["start_passed"])
		self.assertEqual(by_name[first.name]["seller_business_name"], "Test Farms")

	def test_other_statuses_search_counts_and_validation(self):
		live = self.pending(title="ZZ Test Tomatoes")
		set_status(live.name, "Approved")
		self.pending(title="ZZ Test Beans")
		self.as_staff()
		self.assertEqual([i["name"] for i in self.queue(status="Approved")["items"]], [live.name])
		self.assertEqual(len(self.queue(search="beans")["items"]), 1)
		counts = self.queue()["counts"]
		self.assertGreaterEqual(counts["Pending Approval"], 1)
		self.assertGreaterEqual(counts["Approved"], 1)
		with self.assertRaises(frappe.ValidationError):
			api.list_listings_for_review(status="Bogus")

	def test_resubmitted_listings_are_flagged_with_their_earlier_rejections(self):
		listing = self.pending()
		self.as_staff()
		api.reject_listing(listing.name, self.token(listing), "Photos are blurry.")
		frappe.set_user(SELLER_A)
		seller_api.resubmit_listing(listing.name, "New photos.")
		self.as_staff()
		item = self.queue()["items"][0]
		self.assertEqual(
			(item["name"], item["previous_rejections"], item["resubmitted"]), (listing.name, 1, True)
		)


class TestGetListingForReview(ReviewTestCase):
	def test_returns_the_public_view_plus_review_context(self):
		listing = self.pending(latitude=7.29, longitude=80.63, organic=1, certification="SLS 1234")
		with lu.system_write():
			listing.ai_suggested_min, listing.ai_suggested_max = 100, 130
			listing.save()
		self.as_staff()
		result = api.get_listing_for_review(listing.name)
		detail = result["listing"]
		self.assertEqual((detail["title"], detail["certification"]), (listing.title, "SLS 1234"))
		self.assertEqual(detail["status"], "Pending Approval")
		self.assertIn("google.com/maps", detail["map_url"])
		review = result["review"]
		self.assertEqual((review["ai_suggested_min"], review["ai_suggested_max"]), (100, 130))
		self.assertEqual(review["seller"]["email"], SELLER_A)
		self.assertEqual(review["seller"]["business_name"], "Test Farms")
		self.assertEqual(review["seller"]["account_status"], "active")
		self.assertTrue(result["expected_modified"])

	def test_shows_earlier_rejections_and_the_sellers_note(self):
		listing = self.pending()
		self.as_staff()
		api.reject_listing(listing.name, self.token(listing), "Description is too short.")
		frappe.set_user(SELLER_A)
		seller_api.resubmit_listing(listing.name, "Rewrote the description.")
		self.as_staff()
		review = api.get_listing_for_review(listing.name)["review"]
		self.assertEqual([h["action"] for h in review["history"]], ["Resubmitted", "Rejected"])
		self.assertEqual(review["history"][1]["reason"], "Description is too short.")
		self.assertEqual(review["history"][1]["reviewer_name"], "Test Staff")
		self.assertEqual(review["resubmission_note"], "Rewrote the description.")

	def test_shows_the_edit_history(self):
		listing = self.pending(title="ZZ Test Before")
		listing.title = "ZZ Test After"
		listing.save(ignore_version=False)  # Frappe skips versioning under test unless told otherwise
		self.as_staff()
		history = api.get_listing_for_review(listing.name)["review"]["edit_history"]
		changes = [c for entry in history for c in entry["changes"]]
		seen = [(c["field"], c["old"], c["new"]) for c in changes]
		self.assertIn(("Title", "ZZ Test Before", "ZZ Test After"), seen)

	def test_action_flags_follow_the_status(self):
		listing = self.pending()
		self.as_staff()

		def allowed():
			actions = api.get_listing_for_review(listing.name)["actions"]
			return {name: state["allowed"] for name, state in actions.items()}

		self.assertEqual(allowed(), {"approve": True, "reject": True, "suspend": False})
		set_status(listing.name, "Approved")
		self.assertEqual(allowed(), {"approve": False, "reject": False, "suspend": True})
		set_status(listing.name, "Suspended")
		self.assertEqual(allowed(), {"approve": True, "reject": False, "suspend": False})
		set_status(listing.name, "Rejected")
		self.assertEqual(allowed(), {"approve": False, "reject": False, "suspend": False})

	def test_a_stale_auction_cannot_be_approved(self):
		listing, auction = make_auction_listing(self.seller, self.category)
		start_auction_now(auction.name)
		self.as_staff()
		actions = api.get_listing_for_review(listing.name)["actions"]
		self.assertFalse(actions["approve"]["allowed"])
		self.assertIn("start time", actions["approve"]["blocked_reason"])

	def test_unknown_listing(self):
		with self.assertRaises(frappe.DoesNotExistError):
			api.get_listing_for_review("LST-99999999")


class TestApprove(ReviewTestCase):
	def test_approving_publishes_the_listing_and_records_who_and_when(self):
		listing = self.pending()
		frappe.db.set_value("Listing", listing.name, "status_reason", "old reason")
		self.as_staff()
		with patch(f"{NOTIFY}.smtp_configured", return_value=True), patch(
			f"{NOTIFY}.send_templated_email"
		) as send:
			result = api.approve_listing(listing.name, self.token(listing), "  Looks good.  ")
		doc = frappe.get_doc("Listing", listing.name)
		self.assertEqual((result["status"], doc.status), ("Approved", "Approved"))
		self.assertEqual((doc.reviewed_by, doc.status_reason), (STAFF, None))
		self.assertTrue(doc.reviewed_on)
		reviews = self.reviews_of(listing)
		self.assertEqual(
			[(r.action, r.reason, r.reviewer) for r in reviews], [("Approved", "Looks good.", STAFF)]
		)
		self.assertEqual(send.call_args.kwargs["key"], "listing_approved")
		self.assertEqual(send.call_args.kwargs["recipient"], SELLER_A)
		frappe.set_user("Guest")
		self.assertIn(listing.name, self.public_names())

	def test_approving_an_auction_listing_schedules_its_auction(self):
		listing, auction = make_auction_listing(self.seller, self.category)
		self.as_staff()
		api.approve_listing(listing.name, self.token(listing))
		doc = frappe.get_doc("Listing", listing.name)
		detail = marketplace.build_listing_detail(doc, "staff")
		self.assertEqual(detail["auction"]["status"], "scheduled")

	def test_a_stale_version_is_refused(self):
		listing = self.pending()
		self.as_staff()
		token = self.token(listing)
		frappe.set_user("Administrator")
		frappe.db.set_value("Listing", listing.name, "title", "ZZ Test Edited")  # the seller edits meanwhile
		self.as_staff()
		with self.assertRaises(frappe.TimestampMismatchError):
			api.approve_listing(listing.name, token)
		self.assertEqual(self.status_of(listing), "Pending Approval")
		self.assertEqual(self.reviews_of(listing), [])
		with self.assertRaises(frappe.TimestampMismatchError):
			api.approve_listing(listing.name, "")

	def test_an_auction_whose_start_has_passed_cannot_be_approved(self):
		listing, auction = make_auction_listing(self.seller, self.category)
		start_auction_now(auction.name)
		self.as_staff()
		with self.assertRaises(frappe.ValidationError):
			api.approve_listing(listing.name, self.token(listing))
		self.assertEqual(self.status_of(listing), "Pending Approval")

	def test_only_pending_or_suspended_listings_can_be_approved(self):
		listing = self.pending()
		set_status(listing.name, "Approved")
		self.as_staff()
		with self.assertRaises(frappe.ValidationError):
			api.approve_listing(listing.name, self.token(listing))
		frappe.set_user("Administrator")
		set_status(listing.name, "Suspended")
		self.as_staff()
		reinstated = api.approve_listing(listing.name, self.token(listing))
		self.assertEqual(reinstated["status"], "Approved")

	def test_a_failing_email_never_blocks_the_decision(self):
		listing = self.pending()
		self.as_staff()
		with patch(f"{NOTIFY}.smtp_configured", return_value=True), patch(
			f"{NOTIFY}.send_templated_email", side_effect=RuntimeError("smtp down")
		):
			result = api.approve_listing(listing.name, self.token(listing))
		self.assertEqual(result["status"], "Approved")


class TestReject(ReviewTestCase):
	def test_rejecting_needs_a_reason_and_records_it(self):
		listing = self.pending()
		self.as_staff()
		token = self.token(listing)
		for reason in ("", "   ", None):
			with self.assertRaises(frappe.ValidationError):
				api.reject_listing(listing.name, token, reason)
		self.assertEqual(self.status_of(listing), "Pending Approval")

		with patch(f"{NOTIFY}.smtp_configured", return_value=True), patch(
			f"{NOTIFY}.send_templated_email"
		) as send:
			api.reject_listing(listing.name, token, "  Photos are blurry.  ")
		doc = frappe.get_doc("Listing", listing.name)
		self.assertEqual(
			(doc.status, doc.status_reason, doc.reviewed_by), ("Rejected", "Photos are blurry.", STAFF)
		)
		reviews = [(r.action, r.reason) for r in self.reviews_of(listing)]
		self.assertEqual(reviews, [("Rejected", "Photos are blurry.")])
		self.assertEqual(send.call_args.kwargs["key"], "listing_rejected")
		self.assertEqual(send.call_args.kwargs["context"]["reason"], "Photos are blurry.")

	def test_only_a_pending_listing_can_be_rejected(self):
		listing = self.pending()
		set_status(listing.name, "Approved")
		self.as_staff()
		with self.assertRaises(frappe.ValidationError):
			api.reject_listing(listing.name, self.token(listing), "No")

	def test_the_seller_sees_the_reason_and_can_resubmit(self):
		listing = self.pending()
		self.as_staff()
		api.reject_listing(listing.name, self.token(listing), "Description is too short.")
		frappe.set_user(SELLER_A)
		shown = marketplace.get_listing(listing.name)
		self.assertEqual(shown["listing"]["status_reason"], "Description is too short.")
		self.assertEqual(seller_api.resubmit_listing(listing.name)["status"], "Pending Approval")


class TestSuspend(ReviewTestCase):
	def test_suspending_a_published_or_hidden_listing_needs_a_reason(self):
		for status in ("Approved", "Hidden"):
			listing = self.pending()
			set_status(listing.name, status)
			self.as_staff()
			token = self.token(listing)
			with self.assertRaises(frappe.ValidationError):
				api.suspend_listing(listing.name, token, " ")
			with patch(f"{NOTIFY}.smtp_configured", return_value=True), patch(
				f"{NOTIFY}.send_templated_email"
			) as send:
				api.suspend_listing(listing.name, token, "Reported as misleading.")
			doc = frappe.get_doc("Listing", listing.name)
			self.assertEqual((doc.status, doc.status_reason), ("Suspended", "Reported as misleading."))
			self.assertEqual(self.reviews_of(listing)[-1].action, "Suspended")
			self.assertEqual(send.call_args.kwargs["key"], "listing_suspended")
			frappe.set_user("Administrator")

	def test_a_suspended_listing_leaves_the_marketplace(self):
		listing = self.pending()
		set_status(listing.name, "Approved")
		self.as_staff()
		api.suspend_listing(listing.name, self.token(listing), "Misleading.")
		self.assertNotIn(listing.name, self.public_names())

	def test_a_pending_listing_cannot_be_suspended(self):
		listing = self.pending()
		self.as_staff()
		with self.assertRaises(frappe.ValidationError):
			api.suspend_listing(listing.name, self.token(listing), "No")

	def test_a_running_auction_without_bids_can_be_suspended_but_not_with_bids(self):
		listing, auction = make_auction_listing(self.seller, self.category)
		set_status(listing.name, "Approved")
		start_auction_now(auction.name)
		self.as_staff()
		token = self.token(listing)
		with patch("accreage_mart.utils.listing.auction_has_bids", return_value=True):
			with self.assertRaises(frappe.ValidationError):
				api.suspend_listing(listing.name, token, "Stop it.")
			self.assertFalse(api.get_listing_for_review(listing.name)["actions"]["suspend"]["allowed"])
		self.assertEqual(self.status_of(listing), "Approved")
		self.assertEqual(api.suspend_listing(listing.name, token, "Stop it.")["status"], "Suspended")

	def test_a_scheduled_auction_can_be_suspended(self):
		listing, _ = make_auction_listing(self.seller, self.category)
		set_status(listing.name, "Approved")
		self.as_staff()
		result = api.suspend_listing(listing.name, self.token(listing), "Stop it.")
		self.assertEqual(result["status"], "Suspended")
