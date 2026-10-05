from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import add_to_date, now_datetime

from accreage_mart.api import listings as api
from accreage_mart.tests.listing_fixtures import (
	SELLER_A,
	SELLER_B,
	future,
	make_auction_listing,
	make_category,
	make_listing,
	make_seller,
	purge,
	set_status,
	start_auction_now,
)

NOTIFY = "accreage_mart.utils.listing_notify"


class ApiListingsTestCase(FrappeTestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		purge(SELLER_A, SELLER_B)
		self.seller = make_seller(SELLER_A)
		self.other = make_seller(SELLER_B)
		self.category = make_category()

	def tearDown(self):
		purge(SELLER_A, SELLER_B)

	def as_seller(self, email=SELLER_A):
		frappe.set_user(email)

	def status_of(self, name):
		return frappe.db.get_value("Listing", name, "status")

	def reviews_of(self, name):
		return frappe.get_all(
			"Listing Review",
			filters={"listing": name},
			fields=["action", "seller_note", "reviewer"],
			order_by="creation asc",
		)

	def published(self, **overrides):
		listing = make_listing(self.seller, self.category, **overrides)
		set_status(listing.name, "Approved")
		return listing

	def published_auction(self, **terms):
		listing, auction = make_auction_listing(self.seller, self.category, **terms)
		set_status(listing.name, "Approved")
		return listing, auction

	def create_args(self, **overrides):
		args = {
			"category": self.category,
			"title": "ZZ Test API Carrots",
			"description": "Fresh carrots.",
			"selling_type": "Direct",
			"unit": "kg",
			"quantity_available": 500,
			"district": "Kandy",
			"location": "Kandy market",
			"images": [{"image": "/files/a.png"}],
			"price_per_unit": 120,
		}
		args.update(overrides)
		return args


class TestCreateListing(ApiListingsTestCase):
	def test_creates_a_pending_direct_listing_for_the_caller(self):
		self.as_seller()
		result = api.create_listing(**self.create_args())
		self.assertEqual(result["status"], "Pending Approval")
		self.assertIsNone(result["auction"])
		doc = frappe.get_doc("Listing", result["name"])
		self.assertEqual(doc.seller, self.seller)
		self.assertTrue(doc.submitted_on)
		self.assertEqual(doc.images[0].is_cover, 1)

	def test_only_a_verified_seller_can_create(self):
		frappe.set_user("Guest")
		with self.assertRaises(frappe.AuthenticationError):
			api.create_listing(**self.create_args())
		frappe.set_user("Administrator")
		with self.assertRaises(frappe.PermissionError):
			api.create_listing(**self.create_args())  # staff/admin have no seller profile
		make_seller("listing.unverified@x.lk", verified=False)
		self.addCleanup(purge, "listing.unverified@x.lk")
		frappe.set_user("listing.unverified@x.lk")
		with self.assertRaises(frappe.PermissionError):
			api.create_listing(**self.create_args())

	def test_auction_listing_needs_the_acknowledgement(self):
		self.as_seller()
		args = self.create_args(
			selling_type="Auction", min_bid=100, start_time=str(future(30)), end_time=str(future(42))
		)
		with self.assertRaises(frappe.ValidationError):
			api.create_listing(**args)

	def test_creates_listing_then_auction_and_links_them(self):
		self.as_seller()
		start = future(30)
		result = api.create_listing(
			**self.create_args(
				selling_type="Auction",
				price_per_unit=None,
				min_bid=100,
				start_time=str(start),
				end_time=str(add_to_date(start, hours=12)),
				auction_terms_acknowledged=1,
			)
		)
		self.assertEqual(result["status"], "Pending Approval")
		self.assertEqual(result["auction"]["status"], "pending")
		self.assertEqual(result["auction"]["duration_hours"], 12)
		listing = frappe.get_doc("Listing", result["name"])
		self.assertEqual(listing.auction, result["auction"]["name"])
		self.assertTrue(frappe.db.exists("Auction", listing.auction))

	def test_a_bad_auction_leaves_neither_record_behind(self):
		self.as_seller()
		before = frappe.db.count("Listing", {"title": "ZZ Test API Carrots"})
		with self.assertRaises(frappe.ValidationError):
			api.create_listing(
				**self.create_args(
					selling_type="Auction",
					min_bid=100,
					start_time=str(future(2)),  # inside the 24h gap
					end_time=str(future(14)),
					auction_terms_acknowledged=1,
				)
			)
		self.assertEqual(frappe.db.count("Listing", {"title": "ZZ Test API Carrots"}), before)

	def test_auction_terms_must_match_the_selling_type(self):
		self.as_seller()
		with self.assertRaises(frappe.ValidationError):
			api.create_listing(**self.create_args(selling_type="Auction", auction_terms_acknowledged=1))
		with self.assertRaises(frappe.ValidationError):
			api.create_listing(**self.create_args(min_bid=100))

	def test_a_new_listing_stores_the_price_snapshot(self):
		self.as_seller()
		snapshot = {"min": 100, "max": 130, "fair_value": 120}
		start = future(30)
		with patch("accreage_mart.api.listings.suggestion_snapshot", return_value=snapshot):
			result = api.create_listing(
				**self.create_args(
					selling_type="Auction",
					price_per_unit=None,
					min_bid=100,
					start_time=str(start),
					end_time=str(add_to_date(start, hours=12)),
					auction_terms_acknowledged=1,
				)
			)
		listing = frappe.get_doc("Listing", result["name"])
		self.assertEqual((listing.ai_suggested_min, listing.ai_suggested_max), (100, 130))
		self.assertEqual(frappe.db.get_value("Auction", listing.auction, "ai_fair_value"), 120)


class TestUpdateListing(ApiListingsTestCase):
	def test_editing_a_published_direct_listing_needs_no_re_review(self):
		listing = self.published()
		self.as_seller()
		api.update_listing(listing.name, {"title": "ZZ Test Renamed", "price_per_unit": 150})
		self.assertEqual(self.status_of(listing.name), "Approved")
		self.assertEqual(frappe.db.get_value("Listing", listing.name, "price_per_unit"), 150)

	def test_system_and_unknown_fields_are_rejected(self):
		listing = self.published()
		self.as_seller()
		for values in ({"status": "Approved"}, {"seller": SELLER_B}, {"selling_type": "Auction"}, {"x": 1}):
			with self.assertRaises(frappe.ValidationError):
				api.update_listing(listing.name, values)

	def test_another_sellers_listing_looks_like_it_does_not_exist(self):
		listing = self.published()
		self.as_seller(SELLER_B)
		with self.assertRaises(frappe.DoesNotExistError):
			api.update_listing(listing.name, {"title": "ZZ Test hijack"})

	def test_archived_and_suspended_listings_cannot_be_edited(self):
		for status in ("Archived", "Suspended"):
			listing = self.published()
			set_status(listing.name, status)
			self.as_seller()
			with self.assertRaises(frappe.ValidationError):
				api.update_listing(listing.name, {"title": "ZZ Test nope"})
			frappe.set_user("Administrator")

	def test_images_are_replaced_as_a_set(self):
		listing = self.published()
		self.as_seller()
		api.update_listing(
			listing.name, {"images": [{"image": "/files/x.png"}, {"image": "/files/y.png", "is_cover": 1}]}
		)
		rows = frappe.get_doc("Listing", listing.name).images
		self.assertEqual([(r.image, r.is_cover) for r in rows], [("/files/x.png", 0), ("/files/y.png", 1)])

	def test_field_kinds_must_match_the_selling_type(self):
		direct = self.published()
		auction, _ = self.published_auction()
		self.as_seller()
		with self.assertRaises(frappe.ValidationError):
			api.update_listing(direct.name, {"min_bid": 100})
		with self.assertRaises(frappe.ValidationError):
			api.update_listing(auction.name, {"price_per_unit": 100})

	def test_changing_auction_terms_sends_a_published_listing_back_to_pending(self):
		listing, auction = self.published_auction()
		self.as_seller()
		new_start = future(40)
		result = api.update_listing(
			listing.name,
			{"min_bid": 175, "start_time": new_start, "end_time": add_to_date(new_start, hours=10)},
		)
		self.assertEqual(result["status"], "Pending Approval")
		self.assertEqual(self.status_of(listing.name), "Pending Approval")
		self.assertEqual(frappe.db.get_value("Auction", auction.name, "min_bid"), 175)
		reviews = self.reviews_of(listing.name)
		self.assertEqual([r.action for r in reviews], ["Resubmitted"])
		self.assertEqual(reviews[0].reviewer, SELLER_A)
		self.assertIn("auction terms", reviews[0].seller_note.lower())

	def test_new_terms_must_still_respect_the_24h_gap(self):
		listing, auction = self.published_auction()
		self.as_seller()
		with self.assertRaises(frappe.ValidationError):
			api.update_listing(
				listing.name, {"min_bid": 175, "start_time": future(3), "end_time": future(12)}
			)
		self.assertEqual(self.status_of(listing.name), "Approved")
		self.assertEqual(frappe.db.get_value("Auction", auction.name, "min_bid"), 100)
		self.assertEqual(self.reviews_of(listing.name), [])

	def test_a_bid_only_change_also_needs_review(self):
		listing, _ = self.published_auction()
		self.as_seller()
		api.update_listing(listing.name, {"min_bid": 150})
		self.assertEqual(self.status_of(listing.name), "Pending Approval")

	def test_other_edits_of_a_published_auction_listing_stay_published(self):
		listing, _ = self.published_auction()
		self.as_seller()
		api.update_listing(listing.name, {"description": "Better description"})
		self.assertEqual(self.status_of(listing.name), "Approved")

	def test_a_started_auction_is_locked_to_the_seller(self):
		listing, auction = self.published_auction()
		start_auction_now(auction.name)
		self.as_seller()
		with self.assertRaises(frappe.ValidationError):
			api.update_listing(listing.name, {"min_bid": 150})
		with self.assertRaises(frappe.ValidationError):
			api.update_listing(listing.name, {"quantity_available": 10})
		api.update_listing(listing.name, {"description": "Still editable"})

	def test_pending_auction_terms_stay_pending_without_a_history_row(self):
		listing, _ = make_auction_listing(self.seller, self.category)
		self.as_seller()
		api.update_listing(listing.name, {"min_bid": 150})
		self.assertEqual(self.status_of(listing.name), "Pending Approval")
		self.assertEqual(self.reviews_of(listing.name), [])

	def test_rejected_auction_terms_wait_for_resubmission(self):
		listing, auction = make_auction_listing(self.seller, self.category)
		set_status(listing.name, "Rejected")
		self.as_seller()
		api.update_listing(listing.name, {"min_bid": 150})
		self.assertEqual(self.status_of(listing.name), "Rejected")
		self.assertEqual(frappe.db.get_value("Auction", auction.name, "min_bid"), 150)


class TestVisibility(ApiListingsTestCase):
	def test_action_info_explains_what_is_possible_and_warns(self):
		listing = self.published()
		self.as_seller()
		info = api.get_listing_action_info(listing.name)
		self.assertIsNone(info["hide"]["blocked_reason"])
		self.assertIsNone(info["archive"]["blocked_reason"])
		self.assertTrue(info["unhide"]["blocked_reason"])
		self.assertIn("order", info["warning"])
		self.assertEqual(info["active_order_count"], 0)

	def test_hide_and_archive_need_the_acknowledgement(self):
		listing = self.published()
		self.as_seller()
		with self.assertRaises(frappe.ValidationError):
			api.hide_listing(listing.name)
		with self.assertRaises(frappe.ValidationError):
			api.archive_listing(listing.name)
		self.assertEqual(self.status_of(listing.name), "Approved")

	def test_hide_then_unhide_round_trip(self):
		listing = self.published()
		self.as_seller()
		self.assertEqual(api.hide_listing(listing.name, 1)["status"], "Hidden")
		self.assertEqual(api.unhide_listing(listing.name)["status"], "Approved")

	def test_only_published_listings_can_be_hidden_and_only_hidden_ones_unhidden(self):
		listing = make_listing(self.seller, self.category)  # Pending
		self.as_seller()
		with self.assertRaises(frappe.ValidationError):
			api.hide_listing(listing.name, 1)
		with self.assertRaises(frappe.ValidationError):
			api.unhide_listing(listing.name)

	def test_archive_is_the_sellers_delete_for_most_statuses(self):
		for status in ("Pending Approval", "Approved", "Rejected", "Hidden"):
			listing = make_listing(self.seller, self.category)
			if status != "Pending Approval":
				set_status(listing.name, status)
			self.as_seller()
			self.assertEqual(api.archive_listing(listing.name, 1)["status"], "Archived", status)
			frappe.set_user("Administrator")

	def test_archived_and_suspended_listings_cannot_be_archived_again_from_here(self):
		for status in ("Archived", "Suspended"):
			listing = make_listing(self.seller, self.category)
			set_status(listing.name, status)
			self.as_seller()
			with self.assertRaises(frappe.ValidationError):
				api.archive_listing(listing.name, 1)
			frappe.set_user("Administrator")

	def test_a_live_auction_cannot_be_hidden_or_archived_by_the_seller(self):
		listing, auction = self.published_auction()
		start_auction_now(auction.name)
		self.as_seller()
		with self.assertRaises(frappe.ValidationError):
			api.hide_listing(listing.name, 1)
		with self.assertRaises(frappe.ValidationError):
			api.archive_listing(listing.name, 1)
		self.assertTrue(api.get_listing_action_info(listing.name)["archive"]["blocked_reason"])

	def test_an_auction_can_be_stopped_before_it_starts_and_after_it_ended(self):
		scheduled, _ = self.published_auction()
		ended, auction = self.published_auction()
		frappe.db.set_value(
			"Auction",
			auction.name,
			{"start_time": add_to_date(now_datetime(), hours=-10), "end_time": future(-1)},
			update_modified=False,
		)
		self.as_seller()
		self.assertEqual(api.hide_listing(scheduled.name, 1)["status"], "Hidden")
		self.assertEqual(api.archive_listing(ended.name, 1)["status"], "Archived")

	def test_a_hidden_auction_cannot_come_back_once_its_start_has_passed(self):
		listing, auction = self.published_auction()
		self.as_seller()
		api.hide_listing(listing.name, 1)
		frappe.db.set_value(
			"Auction",
			auction.name,
			{"start_time": add_to_date(now_datetime(), hours=-1), "end_time": future(8)},
			update_modified=False,
		)
		with self.assertRaises(frappe.ValidationError):
			api.unhide_listing(listing.name)


class TestResubmitAndDuplicate(ApiListingsTestCase):
	def test_resubmit_moves_a_rejected_listing_back_to_pending_and_logs_it(self):
		listing = make_listing(self.seller, self.category)
		set_status(listing.name, "Rejected")
		self.as_seller()
		result = api.resubmit_listing(listing.name, "  Replaced the photos.  ")
		self.assertEqual(result["status"], "Pending Approval")
		doc = frappe.get_doc("Listing", listing.name)
		self.assertEqual(doc.resubmission_note, "Replaced the photos.")
		self.assertTrue(doc.submitted_on)
		reviews = self.reviews_of(listing.name)
		self.assertEqual((reviews[0].action, reviews[0].seller_note), ("Resubmitted", "Replaced the photos."))
		self.assertEqual(reviews[0].reviewer, SELLER_A)

	def test_only_a_rejected_listing_can_be_resubmitted(self):
		listing = self.published()
		self.as_seller()
		with self.assertRaises(frappe.ValidationError):
			api.resubmit_listing(listing.name)

	def test_resubmitting_an_auction_rechecks_the_24h_gap(self):
		listing, auction = make_auction_listing(self.seller, self.category)
		set_status(listing.name, "Rejected")
		start_auction_now(auction.name)  # the old start has long gone
		self.as_seller()
		with self.assertRaises(frappe.ValidationError):
			api.resubmit_listing(listing.name)
		self.assertEqual(self.status_of(listing.name), "Rejected")

	def test_resubmitting_a_still_valid_auction_works(self):
		listing, _ = make_auction_listing(self.seller, self.category)
		set_status(listing.name, "Rejected")
		self.as_seller()
		self.assertEqual(api.resubmit_listing(listing.name)["status"], "Pending Approval")

	def test_duplicate_copies_an_archived_direct_listing_as_a_new_pending_one(self):
		source = make_listing(
			self.seller,
			self.category,
			images=[{"image": "/files/a.png"}, {"image": "/files/b.png", "is_cover": 1}],
		)
		set_status(source.name, "Archived")
		self.as_seller()
		result = api.duplicate_listing(source.name)
		self.assertNotEqual(result["name"], source.name)
		self.assertEqual(result["status"], "Pending Approval")
		copy = frappe.get_doc("Listing", result["name"])
		self.assertEqual(copy.title, source.title)
		covers = [(row.image, row.is_cover) for row in copy.images]
		self.assertEqual(covers, [("/files/a.png", 0), ("/files/b.png", 1)])
		self.assertEqual(self.status_of(source.name), "Archived")

	def test_only_archived_listings_can_be_duplicated(self):
		listing = self.published()
		self.as_seller()
		with self.assertRaises(frappe.ValidationError):
			api.duplicate_listing(listing.name)

	def test_duplicating_an_auction_needs_new_times_and_the_acknowledgement(self):
		source, _ = make_auction_listing(self.seller, self.category)
		set_status(source.name, "Archived")
		start = future(30)
		times = {"start_time": str(start), "end_time": str(add_to_date(start, hours=12))}
		self.as_seller()
		with self.assertRaises(frappe.ValidationError):
			api.duplicate_listing(source.name, auction_terms_acknowledged=1)
		with self.assertRaises(frappe.ValidationError):
			api.duplicate_listing(source.name, **times)
		result = api.duplicate_listing(source.name, auction_terms_acknowledged=1, **times)
		self.assertEqual(result["auction"]["min_bid"], 100)
		self.assertNotEqual(result["auction"]["name"], source.auction)


class TestStock(ApiListingsTestCase):
	def test_updates_stock_including_sold_out(self):
		listing = self.published()
		self.as_seller()
		self.assertEqual(api.update_stock(listing.name, 40)["quantity_available"], 40)
		self.assertEqual(api.update_stock(listing.name, 0)["quantity_available"], 0)

	def test_stock_rules(self):
		auction, _ = self.published_auction()
		archived = self.published()
		set_status(archived.name, "Archived")
		direct = self.published()
		self.as_seller()
		with self.assertRaises(frappe.ValidationError):
			api.update_stock(auction.name, 10)
		with self.assertRaises(frappe.ValidationError):
			api.update_stock(archived.name, 10)
		with self.assertRaises(frappe.ValidationError):
			api.update_stock(direct.name, -1)

	def test_one_email_when_stock_first_drops_below_the_level(self):
		listing = self.published(quantity_available=150, low_stock_level=100)
		self.as_seller()
		with patch(f"{NOTIFY}.smtp_configured", return_value=True), patch(
			f"{NOTIFY}.send_templated_email"
		) as send:
			api.update_stock(listing.name, 120)  # still above
			self.assertEqual(send.call_count, 0)
			api.update_stock(listing.name, 80)  # crosses below
			self.assertEqual(send.call_count, 1)
			api.update_stock(listing.name, 60)  # already below
			self.assertEqual(send.call_count, 1)
			api.update_stock(listing.name, 200)  # restocked
			api.update_stock(listing.name, 50)  # below again
			self.assertEqual(send.call_count, 2)
		self.assertEqual(send.call_args.kwargs["key"], "listing_low_stock")
		self.assertEqual(send.call_args.kwargs["recipient"], SELLER_A)

	def test_no_email_without_a_level_or_without_smtp(self):
		no_level = self.published(quantity_available=150)
		with_level = self.published(quantity_available=150, low_stock_level=100)
		self.as_seller()
		with patch(f"{NOTIFY}.smtp_configured", return_value=True), patch(
			f"{NOTIFY}.send_templated_email"
		) as send:
			api.update_stock(no_level.name, 5)
			self.assertEqual(send.call_count, 0)
		with patch(f"{NOTIFY}.smtp_configured", return_value=False), patch(
			f"{NOTIFY}.send_templated_email"
		) as send:
			api.update_stock(with_level.name, 5)
			self.assertEqual(send.call_count, 0)

	def test_a_failing_email_never_blocks_the_stock_update(self):
		listing = self.published(quantity_available=150, low_stock_level=100)
		self.as_seller()
		with patch(f"{NOTIFY}.smtp_configured", return_value=True), patch(
			f"{NOTIFY}.send_templated_email", side_effect=RuntimeError("smtp down")
		):
			result = api.update_stock(listing.name, 10)
		self.assertEqual(result["quantity_available"], 10)

	def test_editing_the_quantity_through_update_listing_also_alerts(self):
		listing = self.published(quantity_available=150, low_stock_level=100)
		self.as_seller()
		with patch(f"{NOTIFY}.smtp_configured", return_value=True), patch(
			f"{NOTIFY}.send_templated_email"
		) as send:
			api.update_listing(listing.name, {"quantity_available": 90})
		self.assertEqual(send.call_count, 1)

	def test_the_low_stock_template_is_seeded_and_renders(self):
		from accreage_mart.setup.install import ensure_email_templates

		ensure_email_templates()
		formatted = frappe.get_doc("Email Template", "listing_low_stock").get_formatted_email(
			{"full_name": "Sam", "title": "Carrots", "quantity": 80, "unit": "kg", "low_stock_level": 100}
		)
		self.assertIn("Carrots", formatted["message"])
		self.assertIn("80", formatted["message"])


class TestNoHardDelete(ApiListingsTestCase):
	def test_there_is_no_seller_delete_endpoint_and_direct_deletes_are_refused(self):
		self.assertFalse(hasattr(api, "delete_listing"))
		listing = self.published()
		self.as_seller()
		with self.assertRaises(frappe.PermissionError):
			frappe.delete_doc("Listing", listing.name)
