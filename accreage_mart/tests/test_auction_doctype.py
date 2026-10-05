import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import add_to_date, now_datetime

from accreage_mart.tests.listing_fixtures import (
	SELLER_A,
	SELLER_B,
	auction_values,
	future,
	make_auction,
	make_auction_listing,
	make_category,
	make_seller,
	purge,
	set_status,
	start_auction_now,
)
from accreage_mart.utils import listing as lu


class TestAuctionDoctype(FrappeTestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		purge(SELLER_A, SELLER_B)
		self.seller = make_seller(SELLER_A)
		self.other = make_seller(SELLER_B)
		self.category = make_category()

	def tearDown(self):
		purge(SELLER_A, SELLER_B)

	# -- terms --------------------------------------------------------------------------

	def test_valid_auction_gets_a_name_and_computed_duration(self):
		auction = make_auction()
		self.assertTrue(auction.name.startswith("AUC-"))
		self.assertEqual(auction.duration_hours, 12)

	def test_min_bid_must_be_positive(self):
		for bid in (0, -10):
			with self.assertRaises(frappe.ValidationError):
				make_auction(min_bid=bid)

	def test_end_must_be_after_start(self):
		start = future(30)
		with self.assertRaises(frappe.ValidationError):
			make_auction(start_time=start, end_time=start)
		with self.assertRaises(frappe.ValidationError):
			make_auction(start_time=start, end_time=add_to_date(start, hours=-2))

	def test_duration_must_be_between_6_and_48_hours(self):
		start = future(30)
		with self.assertRaises(frappe.ValidationError):
			make_auction(start_time=start, end_time=add_to_date(start, hours=5, minutes=59))
		with self.assertRaises(frappe.ValidationError):
			make_auction(start_time=start, end_time=add_to_date(start, hours=48, minutes=1))
		shortest = make_auction(start_time=start, end_time=add_to_date(start, hours=6))
		longest = make_auction(start_time=start, end_time=add_to_date(start, hours=48))
		self.assertEqual((shortest.duration_hours, longest.duration_hours), (6, 48))

	def test_start_must_be_at_least_24_hours_ahead(self):
		with self.assertRaises(frappe.ValidationError):
			make_auction(start_time=future(23))
		with self.assertRaises(frappe.ValidationError):
			make_auction(start_time=future(-2))
		make_auction(start_time=add_to_date(now_datetime(), hours=24, minutes=5))

	def test_gap_is_rechecked_when_the_start_time_is_edited(self):
		auction = make_auction()
		auction.start_time = future(2)
		auction.end_time = future(10)
		with self.assertRaises(frappe.ValidationError):
			auction.save(ignore_permissions=True)

	# -- system fields ------------------------------------------------------------------

	def test_ai_fair_value_is_system_managed(self):
		frappe.set_user(SELLER_A)
		with self.assertRaises(frappe.ValidationError):
			frappe.get_doc(auction_values(ai_fair_value=250)).insert(ignore_permissions=True)
		frappe.set_user("Administrator")
		self.assertEqual(make_auction(ai_fair_value=250).ai_fair_value, 250)

	# -- started lock -------------------------------------------------------------------

	def test_terms_are_locked_once_a_published_auction_has_started(self):
		listing, auction = make_auction_listing(self.seller, self.category)
		set_status(listing.name, "Approved")

		doc = frappe.get_doc("Auction", auction.name)
		doc.min_bid = 150
		doc.save(ignore_permissions=True)  # not started yet

		start_auction_now(auction.name)
		doc = frappe.get_doc("Auction", auction.name)
		doc.min_bid = 175
		with self.assertRaises(frappe.ValidationError):
			doc.save(ignore_permissions=True)

	def test_terms_stay_editable_while_the_listing_is_still_pending(self):
		listing, auction = make_auction_listing(self.seller, self.category)
		start_auction_now(auction.name)  # stale pending auction: seller may still fix its terms
		doc = frappe.get_doc("Auction", auction.name)
		doc.min_bid = 150
		doc.save(ignore_permissions=True)
		self.assertEqual(frappe.db.get_value("Auction", auction.name, "min_bid"), 150)

	# -- access -------------------------------------------------------------------------

	def test_only_the_owner_and_staff_see_an_auction(self):
		_, auction = make_auction_listing(self.seller, self.category)
		frappe.set_user(SELLER_B)
		self.assertNotIn(auction.name, frappe.get_list("Auction", pluck="name"))
		with self.assertRaises(frappe.PermissionError):
			frappe.get_doc("Auction", auction.name).check_permission("write")
		frappe.set_user(SELLER_A)
		self.assertIn(auction.name, frappe.get_list("Auction", pluck="name"))

	# -- derived status -----------------------------------------------------------------

	def test_derived_status_follows_listing_status_and_clock(self):
		start, end = future(10), future(22)
		derive = lu.derive_auction_status
		self.assertEqual(derive("Pending Approval", start, end), "pending")
		self.assertEqual(derive("Rejected", start, end), "rejected")
		self.assertEqual(derive("Approved", start, end), "scheduled")
		self.assertEqual(derive("Approved", start, end, now=add_to_date(start, hours=1)), "live")
		self.assertEqual(derive("Approved", start, end, now=add_to_date(end, hours=1)), "ended")
		self.assertEqual(derive("Hidden", start, end), "hidden")
		self.assertEqual(derive("Suspended", start, end), "suspended")
		self.assertEqual(derive("Archived", start, end), "archived")
