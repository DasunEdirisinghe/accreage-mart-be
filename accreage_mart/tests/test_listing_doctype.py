import frappe
from frappe.tests.utils import FrappeTestCase

from accreage_mart.tests.listing_fixtures import (
	SELLER_A,
	SELLER_B,
	listing_values,
	make_auction_listing,
	make_category,
	make_listing,
	make_seller,
	purge,
	set_status,
	start_auction_now,
)
from accreage_mart.utils import listing as lu


class TestListingDoctype(FrappeTestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		purge(SELLER_A, SELLER_B)
		self.seller = make_seller(SELLER_A)
		self.other = make_seller(SELLER_B)
		self.category = make_category()

	def tearDown(self):
		purge(SELLER_A, SELLER_B)

	def _values(self, **overrides):
		return listing_values(self.seller, self.category, **overrides)

	def _insert(self, **overrides):
		return frappe.get_doc(self._values(**overrides)).insert(ignore_permissions=True)

	# -- schema -------------------------------------------------------------------------

	def test_select_options_match_the_shared_constants(self):
		meta = frappe.get_meta("Listing")
		self.assertEqual(tuple(meta.get_field("district").options.split("\n")[1:]), lu.DISTRICTS)
		self.assertEqual(tuple(meta.get_field("unit").options.split("\n")), lu.UNITS)
		self.assertEqual(tuple(meta.get_field("status").options.split("\n")), lu.LISTING_STATUSES)
		self.assertEqual(tuple(meta.get_field("selling_type").options.split("\n")), lu.SELLING_TYPES)

	def test_filter_fields_are_indexed_and_changes_are_tracked(self):
		meta = frappe.get_meta("Listing")
		for fieldname in ("status", "category", "district", "selling_type", "seller"):
			self.assertTrue(meta.get_field(fieldname).search_index, fieldname)
		self.assertTrue(meta.track_changes)
		self.assertTrue(frappe.get_meta("Auction").track_changes)

	# -- create -------------------------------------------------------------------------

	def test_valid_direct_listing_starts_pending_with_defaults(self):
		listing = self._insert()
		self.assertTrue(listing.name.startswith("LST-"))
		self.assertEqual(listing.status, "Pending Approval")
		self.assertEqual(listing.min_order_qty, 1)
		self.assertEqual(listing.images[0].is_cover, 1)

	def test_title_is_required_and_capped_at_140_characters(self):
		with self.assertRaises(frappe.ValidationError):
			self._insert(title="   ")
		with self.assertRaises(frappe.ValidationError):
			self._insert(title="ZZ Test " + "x" * 140)
		self._insert(title="ZZ Test " + "x" * 132)

	def test_description_and_quantity_are_required(self):
		with self.assertRaises(frappe.ValidationError):
			self._insert(description="  ")
		for quantity in (0, -5):
			with self.assertRaises(frappe.ValidationError):
				self._insert(quantity_available=quantity)

	def test_stock_can_drop_to_zero_after_creation_but_not_start_there(self):
		listing = self._insert(min_order_qty=50)
		listing.quantity_available = 0  # sold out; the min-order check does not apply to an empty shelf
		listing.save(ignore_permissions=True)
		listing.quantity_available = -1
		with self.assertRaises(frappe.ValidationError):
			listing.save(ignore_permissions=True)

	def test_direct_requires_a_positive_price(self):
		for price in (0, -1):
			with self.assertRaises(frappe.ValidationError):
				self._insert(price_per_unit=price)

	def test_min_order_qty_is_optional_but_cannot_exceed_the_quantity(self):
		self.assertEqual(self._insert(min_order_qty=0).min_order_qty, 1)
		self.assertEqual(self._insert(min_order_qty=25).min_order_qty, 25)
		with self.assertRaises(frappe.ValidationError):
			self._insert(min_order_qty=501)
		with self.assertRaises(frappe.ValidationError):
			self._insert(min_order_qty=-2)

	def test_low_stock_level_is_optional_and_not_negative(self):
		self.assertEqual(self._insert().low_stock_level, 0)
		self.assertEqual(self._insert(low_stock_level=40).low_stock_level, 40)
		with self.assertRaises(frappe.ValidationError):
			self._insert(low_stock_level=-1)

	def test_only_verified_sellers_can_create(self):
		unverified = make_seller("listing.unverified@x.lk", verified=False)
		self.addCleanup(purge, "listing.unverified@x.lk")
		with self.assertRaises(frappe.ValidationError):
			frappe.get_doc(listing_values(unverified, self.category)).insert(ignore_permissions=True)

	# -- auction type -------------------------------------------------------------------

	def test_auction_listing_clears_direct_only_fields_and_may_be_inserted_without_its_link(self):
		listing = self._insert(selling_type="Auction", price_per_unit=99, min_order_qty=5, low_stock_level=3)
		self.assertEqual(listing.price_per_unit, 0)
		self.assertEqual(listing.min_order_qty, 0)
		self.assertEqual(listing.low_stock_level, 0)
		self.assertFalse(listing.auction)

	def test_auction_listing_must_have_its_auction_on_every_later_save(self):
		listing = self._insert(selling_type="Auction")
		listing.title = "ZZ Test Carrots renamed"
		with self.assertRaises(frappe.ValidationError):
			listing.save(ignore_permissions=True)

	def test_linked_auction_listing_saves(self):
		listing, auction = make_auction_listing(self.seller, self.category)
		listing.reload()
		self.assertEqual(listing.auction, auction.name)
		listing.description = "Updated description"
		listing.save(ignore_permissions=True)

	def test_direct_listing_cannot_have_an_auction(self):
		_, auction = make_auction_listing(self.seller, self.category)
		listing = self._insert()
		with lu.system_write():
			listing.auction = auction.name
			with self.assertRaises(frappe.ValidationError):
				listing.save(ignore_permissions=True)

	def test_selling_type_cannot_change_after_creation(self):
		listing = self._insert()
		listing.selling_type = "Auction"
		with self.assertRaises(frappe.ValidationError):
			listing.save(ignore_permissions=True)

	# -- certification ------------------------------------------------------------------

	def test_organic_requires_certification_text(self):
		with self.assertRaises(frappe.ValidationError):
			self._insert(organic=1, certification="  ")
		self.assertEqual(self._insert(organic=1, certification="SLS 1234").certification, "SLS 1234")

	def test_non_organic_listing_drops_any_certification(self):
		self.assertFalse(self._insert(organic=0, certification="leftover").certification)

	# -- location -----------------------------------------------------------------------

	def test_district_must_come_from_the_list_and_location_is_required(self):
		with self.assertRaises(frappe.ValidationError):
			self._insert(district="Atlantis")
		with self.assertRaises(frappe.ValidationError):
			self._insert(location="  ")

	def test_location_pin_needs_both_coordinates_in_range_or_neither(self):
		self.assertEqual(self._insert().latitude, 0)
		pinned = self._insert(latitude=7.2906, longitude=80.6337)
		self.assertAlmostEqual(pinned.latitude, 7.2906, places=4)
		with self.assertRaises(frappe.ValidationError):
			self._insert(latitude=7.29)
		with self.assertRaises(frappe.ValidationError):
			self._insert(latitude=95, longitude=80)
		with self.assertRaises(frappe.ValidationError):
			self._insert(latitude=7, longitude=200)

	# -- images -------------------------------------------------------------------------

	def test_at_least_one_and_at_most_five_images(self):
		with self.assertRaises(frappe.ValidationError):
			self._insert(images=[])
		six = [{"image": f"/files/{i}.png"} for i in range(6)]
		with self.assertRaises(frappe.ValidationError):
			self._insert(images=six)
		self._insert(images=six[:5])

	def test_cover_defaults_to_the_first_image_and_respects_an_explicit_choice(self):
		pair = [{"image": "/files/a.png"}, {"image": "/files/b.png"}]
		self.assertEqual([row.is_cover for row in self._insert(images=pair).images], [1, 0])
		chosen = [{"image": "/files/a.png"}, {"image": "/files/b.png", "is_cover": 1}]
		self.assertEqual([row.is_cover for row in self._insert(images=chosen).images], [0, 1])

	def test_only_one_cover_image_allowed(self):
		both = [{"image": "/files/a.png", "is_cover": 1}, {"image": "/files/b.png", "is_cover": 1}]
		with self.assertRaises(frappe.ValidationError):
			self._insert(images=both)

	# -- ownership and system fields ----------------------------------------------------

	def test_seller_creates_own_listing_and_seller_is_filled_in(self):
		frappe.set_user(SELLER_A)
		values = self._values()
		del values["seller"]
		listing = frappe.get_doc(values).insert()
		self.assertEqual(listing.seller, self.seller)

	def test_seller_cannot_create_a_listing_for_another_seller(self):
		frappe.set_user(SELLER_A)
		with self.assertRaises(frappe.PermissionError):
			frappe.get_doc(listing_values(self.other, self.category)).insert()

	def test_seller_cannot_start_a_listing_as_approved(self):
		frappe.set_user(SELLER_A)
		with self.assertRaises(frappe.ValidationError):
			frappe.get_doc(self._values(status="Approved")).insert()

	def test_seller_cannot_change_status_or_other_system_fields(self):
		listing = self._insert()
		frappe.set_user(SELLER_A)
		doc = frappe.get_doc("Listing", listing.name)
		doc.status = "Approved"
		with self.assertRaises(frappe.ValidationError):
			doc.save()
		doc = frappe.get_doc("Listing", listing.name)
		doc.status_reason = "I approve myself"
		with self.assertRaises(frappe.ValidationError):
			doc.save()
		doc = frappe.get_doc("Listing", listing.name)
		doc.ai_suggested_min = 1
		with self.assertRaises(frappe.ValidationError):
			doc.save()

	def test_seller_can_edit_own_content_fields(self):
		listing = self._insert()
		frappe.set_user(SELLER_A)
		doc = frappe.get_doc("Listing", listing.name)
		doc.description = "Updated by the owner"
		doc.save()
		self.assertEqual(frappe.db.get_value("Listing", listing.name, "description"), "Updated by the owner")

	def test_system_write_and_administrator_may_change_status(self):
		listing = self._insert()
		set_status(listing.name, "Approved")
		self.assertEqual(frappe.db.get_value("Listing", listing.name, "status"), "Approved")
		doc = frappe.get_doc("Listing", listing.name)
		doc.status = "Hidden"
		doc.save()  # Administrator
		self.assertEqual(frappe.db.get_value("Listing", listing.name, "status"), "Hidden")

	def test_another_seller_cannot_read_write_or_list_the_listing(self):
		listing = self._insert()
		frappe.set_user(SELLER_B)
		doc = frappe.get_doc("Listing", listing.name)
		with self.assertRaises(frappe.PermissionError):
			doc.check_permission("read")
		doc.title = "ZZ Test hijacked"
		with self.assertRaises(frappe.PermissionError):
			doc.save()
		self.assertNotIn(listing.name, frappe.get_list("Listing", pluck="name"))

		frappe.set_user(SELLER_A)
		self.assertIn(listing.name, frappe.get_list("Listing", pluck="name"))

	def test_staff_can_read_every_listing(self):
		listing = self._insert()
		frappe.set_user("Administrator")
		self.assertIn(listing.name, frappe.get_list("Listing", pluck="name", limit_page_length=0))

	def test_a_seller_cannot_delete_a_listing_but_administrator_can(self):
		listing = self._insert()
		frappe.set_user(SELLER_A)
		with self.assertRaises(frappe.PermissionError):
			frappe.delete_doc("Listing", listing.name)
		frappe.set_user("Administrator")
		frappe.delete_doc("Listing", listing.name)
		self.assertFalse(frappe.db.exists("Listing", listing.name))

	def test_deleting_a_listing_removes_its_auction(self):
		listing, auction = make_auction_listing(self.seller, self.category)
		frappe.delete_doc("Listing", listing.name)
		self.assertFalse(frappe.db.exists("Auction", auction.name))

	# -- started auction lock -----------------------------------------------------------

	def test_lot_is_locked_once_a_published_auction_has_started(self):
		listing, auction = make_auction_listing(self.seller, self.category)
		set_status(listing.name, "Approved")
		doc = frappe.get_doc("Listing", listing.name)
		doc.quantity_available = 400
		doc.save(ignore_permissions=True)  # not started yet: allowed

		start_auction_now(auction.name)
		doc = frappe.get_doc("Listing", listing.name)
		doc.quantity_available = 300
		with self.assertRaises(frappe.ValidationError):
			doc.save(ignore_permissions=True)
		doc = frappe.get_doc("Listing", listing.name)
		doc.description = "Description edits stay allowed"
		doc.save(ignore_permissions=True)
