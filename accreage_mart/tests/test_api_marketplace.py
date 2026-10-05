import json
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import add_to_date, now_datetime

from accreage_mart.api import marketplace as api
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
from accreage_mart.utils import listing as lu


class MarketplaceTestCase(FrappeTestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		purge(SELLER_A, SELLER_B)
		self.seller = make_seller(SELLER_A)
		self.other = make_seller(SELLER_B)
		self.category = make_category()
		self.pid = lu.public_seller_id(SELLER_A)

	def tearDown(self):
		purge(SELLER_A, SELLER_B)

	def live(self, seller=None, status="Approved", **overrides):
		listing = make_listing(seller or self.seller, self.category, **overrides)
		if status != "Pending Approval":
			set_status(listing.name, status)
		return listing

	def names(self, **filters):
		"""Names returned for seller A (isolates the tests from other data on the dev site)."""
		filters.setdefault("seller", self.pid)
		return {item["name"] for item in api.list_marketplace(**filters)["items"]}


class TestListMarketplace(MarketplaceTestCase):
	def test_only_published_listings_are_listed(self):
		live = self.live()
		for status in ("Pending Approval", "Rejected", "Hidden", "Suspended", "Archived"):
			self.live(status=status)
		self.assertEqual(self.names(), {live.name})

	def test_guests_can_browse(self):
		live = self.live()
		frappe.set_user("Guest")
		self.assertEqual(self.names(), {live.name})

	def test_listings_of_an_inactive_seller_disappear(self):
		self.live()
		frappe.db.set_value("User", SELLER_A, "enabled", 0)
		self.assertEqual(self.names(), set())
		frappe.db.set_value("User", SELLER_A, "enabled", 1)
		self.assertEqual(len(self.names()), 1)

	def test_auctions_show_until_they_end(self):
		scheduled, _ = make_auction_listing(self.seller, self.category)
		running, running_auction = make_auction_listing(self.seller, self.category)
		ended, ended_auction = make_auction_listing(self.seller, self.category)
		for listing in (scheduled, running, ended):
			set_status(listing.name, "Approved")
		start_auction_now(running_auction.name)
		frappe.db.set_value(
			"Auction",
			ended_auction.name,
			{"start_time": add_to_date(now_datetime(), hours=-10), "end_time": future(-1)},
			update_modified=False,
		)
		items = {i["name"]: i for i in api.list_marketplace(seller=self.pid)["items"]}
		self.assertEqual(set(items), {scheduled.name, running.name})
		self.assertEqual(items[scheduled.name]["auction"]["status"], "scheduled")
		self.assertEqual(items[running.name]["auction"]["status"], "live")
		self.assertFalse(items[running.name]["auction"]["bidding_enabled"])
		self.assertIsNone(items[running.name]["price_per_unit"])

	def test_the_card_is_an_allow_list_and_never_leaks_the_sellers_email(self):
		self.live()
		result = api.list_marketplace(seller=self.pid)
		card = result["items"][0]
		expected = (
			"name title selling_type category category_title unit price_per_unit quantity_available "
			"in_stock district location organic cover_image created seller auction"
		).split()
		self.assertEqual(set(card), set(expected))
		self.assertEqual(set(card["seller"]), {"public_id", "business_name", "trust_score"})
		self.assertNotIn(SELLER_A, json.dumps(result, default=str))
		self.assertEqual(card["cover_image"], "/files/test-a.png")

	def test_filters(self):
		kandy = self.live(
			title="ZZ Test Kandy", district="Kandy", price_per_unit=100, organic=1, certification="SLS"
		)
		galle = self.live(title="ZZ Test Galle", district="Galle", price_per_unit=300)
		empty = self.live(title="ZZ Test Empty", district="Galle", price_per_unit=200)
		frappe.db.set_value("Listing", empty.name, "quantity_available", 0)
		auction, _ = make_auction_listing(self.seller, self.category)  # min bid 100
		set_status(auction.name, "Approved")

		self.assertEqual(self.names(district="Kandy"), {kandy.name})
		self.assertEqual(self.names(organic=1), {kandy.name})
		self.assertEqual(self.names(selling_type="Auction"), {auction.name})
		self.assertEqual(self.names(selling_type="Direct"), {kandy.name, galle.name, empty.name})
		self.assertEqual(self.names(in_stock=1), {kandy.name, galle.name, auction.name})
		self.assertEqual(self.names(min_price=150), {galle.name, empty.name})
		self.assertEqual(self.names(max_price=100), {kandy.name, auction.name})  # auction uses its min bid
		self.assertEqual(self.names(min_price=150, max_price=250), {empty.name})
		everything = {kandy.name, galle.name, empty.name, auction.name}
		self.assertEqual(self.names(category=self.category), everything)
		self.assertEqual(self.names(category="no-such-category"), set())

	def test_search_matches_title_and_description_and_treats_wildcards_literally(self):
		a = self.live(title="ZZ Test Red Onions", description="Sweet and crisp")
		b = self.live(title="ZZ Test 100% Pure Honey", description="Jungle flower")
		self.assertEqual(self.names(search="onion"), {a.name})
		self.assertEqual(self.names(search="crisp"), {a.name})
		self.assertEqual(self.names(search="100%"), {b.name})
		self.assertEqual(self.names(search="%"), {b.name})
		self.assertEqual(self.names(search="_"), set())

	def test_the_seller_filter_is_by_public_id(self):
		mine = self.live()
		theirs = self.live(seller=self.other)
		self.assertEqual(self.names(), {mine.name})
		self.assertEqual(self.names(seller=lu.public_seller_id(SELLER_B)), {theirs.name})
		self.assertEqual(self.names(seller="not-a-real-id"), set())

	def test_sorting(self):
		cheap = self.live(title="ZZ Test Cheap", price_per_unit=50)
		mid = self.live(title="ZZ Test Mid", price_per_unit=150)
		dear = self.live(title="ZZ Test Dear", price_per_unit=400)

		def order(sort):
			return [i["name"] for i in api.list_marketplace(seller=self.pid, sort=sort)["items"]]

		self.assertEqual(order("price_asc"), [cheap.name, mid.name, dear.name])
		self.assertEqual(order("price_desc"), [dear.name, mid.name, cheap.name])
		self.assertEqual(order("newest"), [dear.name, mid.name, cheap.name])
		with self.assertRaises(frappe.ValidationError):
			api.list_marketplace(sort="cheapest")

	def test_pagination(self):
		for i in range(5):
			self.live(title=f"ZZ Test Item {i}")
		first = api.list_marketplace(seller=self.pid, page_size=2)
		self.assertEqual((first["total"], len(first["items"]), first["has_more"]), (5, 2, True))
		last = api.list_marketplace(seller=self.pid, page_size=2, page=3)
		self.assertEqual((len(last["items"]), last["has_more"]), (1, False))
		self.assertEqual(api.list_marketplace(seller=self.pid, page_size=1000)["page_size"], 48)
		self.assertEqual(api.list_marketplace(seller=self.pid, page=0)["page"], 1)

	def test_unknown_selling_type_is_rejected(self):
		with self.assertRaises(frappe.ValidationError):
			api.list_marketplace(selling_type="Barter")


class TestGetListing(MarketplaceTestCase):
	def test_a_published_listing_is_fully_visible_to_the_public(self):
		listing = self.live(
			latitude=7.29,
			longitude=80.63,
			images=[{"image": "/files/a.png"}, {"image": "/files/b.png", "is_cover": 1}],
		)
		frappe.set_user("Guest")
		result = api.get_listing(listing.name)
		self.assertEqual((result["availability"], result["viewer"]), ("available", "public"))
		self.assertTrue(result["accepting_orders"])
		self.assertIsNone(result["message"])
		detail = result["listing"]
		self.assertEqual(detail["cover_image"], "/files/b.png")
		self.assertEqual([i["url"] for i in detail["images"]], ["/files/b.png", "/files/a.png"])
		self.assertIn("google.com/maps?q=7.29,80.63", detail["map_url"])
		self.assertEqual(detail["category"]["name"], self.category)
		self.assertNotIn("status", detail)
		self.assertNotIn("low_stock_level", detail)
		self.assertNotIn(SELLER_A, json.dumps(result, default=str))

	def test_pending_and_rejected_look_like_they_do_not_exist_to_the_public(self):
		for status in ("Pending Approval", "Rejected"):
			listing = self.live(status=status)
			frappe.set_user("Guest")
			with self.assertRaises(frappe.DoesNotExistError):
				api.get_listing(listing.name)
			frappe.set_user(SELLER_B)
			with self.assertRaises(frappe.DoesNotExistError):
				api.get_listing(listing.name)
			frappe.set_user("Administrator")

	def test_missing_listing(self):
		with self.assertRaises(frappe.DoesNotExistError):
			api.get_listing("LST-99999999")

	def test_hidden_suspended_and_archived_give_the_public_only_a_message(self):
		expected = {
			"Hidden": ("hidden", "temporarily unavailable"),
			"Suspended": ("suspended", "unavailable"),
			"Archived": ("archived", "no longer available"),
		}
		for status, (availability, text) in expected.items():
			listing = self.live(status=status)
			frappe.set_user("Guest")
			result = api.get_listing(listing.name)
			self.assertEqual(result["availability"], availability)
			self.assertIn(text, result["message"])
			self.assertIsNone(result["listing"])
			self.assertFalse(result["accepting_orders"])
			frappe.set_user("Administrator")

	def test_a_listing_of_an_inactive_seller_is_unavailable(self):
		listing = self.live()
		frappe.db.set_value("User", SELLER_A, "enabled", 0)
		frappe.set_user("Guest")
		result = api.get_listing(listing.name)
		self.assertEqual(result["availability"], "seller_unavailable")
		self.assertIsNone(result["listing"])

	def test_the_owner_sees_every_status_with_a_message_and_private_fields(self):
		pending = self.live(status="Pending Approval", low_stock_level=100, quantity_available=50)
		self.assertIn("Waiting for staff approval", self._as(SELLER_A, pending)["message"])
		result = self._as(SELLER_A, pending)
		self.assertEqual(result["viewer"], "owner")
		self.assertEqual(result["listing"]["status"], "Pending Approval")
		self.assertEqual(result["listing"]["stock_state"], "low_stock")
		hidden = self.live(status="Hidden")
		self.assertIn("hidden this listing", self._as(SELLER_A, hidden)["message"])
		rejected = self.live(status="Rejected")
		self.assertEqual(self._as(SELLER_A, rejected)["availability"], "rejected")

	def test_staff_see_everything(self):
		pending = self.live(status="Pending Approval")
		result = self._as("Administrator", pending)
		self.assertEqual(result["viewer"], "staff")
		self.assertEqual(result["listing"]["status"], "Pending Approval")

	def test_another_seller_is_just_the_public(self):
		hidden = self.live(status="Hidden")
		result = self._as(SELLER_B, hidden)
		self.assertEqual(result["viewer"], "public")
		self.assertIsNone(result["listing"])

	def test_a_buyer_with_an_order_can_still_open_a_hidden_listing(self):
		for status, text in (("Hidden", "temporarily unavailable"), ("Archived", "no longer available")):
			listing = self.live(status=status)
			with patch("accreage_mart.utils.listing.viewer_has_order", return_value=True):
				result = self._as(SELLER_B, listing)
			self.assertEqual(result["viewer"], "order_holder")
			self.assertIn(text, result["message"])
			self.assertIn("Your order is not affected", result["message"])
			self.assertEqual(result["listing"]["name"], listing.name)
			self.assertFalse(result["accepting_orders"])

	def test_an_order_does_not_reveal_a_pending_listing(self):
		listing = self.live(status="Pending Approval")
		with patch("accreage_mart.utils.listing.viewer_has_order", return_value=True):
			frappe.set_user(SELLER_B)
			with self.assertRaises(frappe.DoesNotExistError):
				api.get_listing(listing.name)

	def test_auction_detail_carries_the_terms_and_no_orders(self):
		listing, _ = make_auction_listing(self.seller, self.category)
		set_status(listing.name, "Approved")
		result = self._as("Guest", listing)
		auction = result["listing"]["auction"]
		self.assertEqual((auction["min_bid"], auction["status"]), (100, "scheduled"))
		self.assertFalse(auction["bidding_enabled"])
		self.assertFalse(result["accepting_orders"])
		self.assertIsNone(result["listing"]["price_per_unit"])

	def test_a_sold_out_listing_is_visible_but_not_orderable(self):
		listing = self.live()
		frappe.db.set_value("Listing", listing.name, "quantity_available", 0)
		result = self._as("Guest", listing)
		self.assertEqual(result["availability"], "available")
		self.assertFalse(result["listing"]["in_stock"])
		self.assertFalse(result["accepting_orders"])

	def _as(self, user, listing):
		frappe.set_user(user)
		try:
			return api.get_listing(listing.name)
		finally:
			frappe.set_user("Administrator")


class TestListMyListings(MarketplaceTestCase):
	def test_lists_only_the_callers_listings_with_tab_counts(self):
		self.live(status="Pending Approval")
		self.live(status="Approved")
		self.live(status="Approved")
		self.live(status="Rejected")
		theirs = self.live(seller=self.other)
		frappe.set_user(SELLER_A)
		result = api.list_my_listings()
		self.assertEqual(result["total"], 4)
		self.assertEqual(
			result["counts"],
			{"pending": 1, "live": 2, "hidden": 0, "rejected": 1, "suspended": 0, "archived": 0},
		)
		self.assertNotIn(theirs.name, {i["name"] for i in result["items"]})

	def test_filters_by_tab_and_search(self):
		a = self.live(title="ZZ Test Tomatoes")
		self.live(title="ZZ Test Beans", status="Hidden")
		frappe.set_user(SELLER_A)
		self.assertEqual({i["name"] for i in api.list_my_listings(tab="live")["items"]}, {a.name})
		self.assertEqual(len(api.list_my_listings(tab="hidden")["items"]), 1)
		self.assertEqual({i["name"] for i in api.list_my_listings(search="tomato")["items"]}, {a.name})
		with self.assertRaises(frappe.ValidationError):
			api.list_my_listings(tab="everything")

	def test_cards_carry_status_reason_stock_state_and_auction_summary(self):
		low = self.live(low_stock_level=100, quantity_available=50)
		gone = self.live()
		frappe.db.set_value("Listing", gone.name, "quantity_available", 0)
		fine = self.live(low_stock_level=10, quantity_available=50)
		auction, _ = make_auction_listing(self.seller, self.category)
		frappe.db.set_value("Listing", auction.name, {"status_reason": "Blurry photos"})
		frappe.set_user(SELLER_A)
		items = {i["name"]: i for i in api.list_my_listings()["items"]}
		self.assertEqual(items[low.name]["stock_state"], "low_stock")
		self.assertEqual(items[gone.name]["stock_state"], "out_of_stock")
		self.assertEqual(items[fine.name]["stock_state"], "in_stock")
		self.assertIsNone(items[auction.name]["stock_state"])
		self.assertEqual(items[auction.name]["status_reason"], "Blurry photos")
		self.assertEqual(items[auction.name]["auction"]["status"], "pending")

	def test_only_a_verified_seller_may_call_it(self):
		with self.assertRaises(frappe.PermissionError):
			api.list_my_listings()  # Administrator has no seller profile
		frappe.set_user("Guest")
		with self.assertRaises(frappe.AuthenticationError):
			api.list_my_listings()


class TestPublicSeller(MarketplaceTestCase):
	def test_public_card_has_no_email_and_counts_live_listings(self):
		self.live()
		self.live(status="Hidden")
		frappe.set_user("Guest")
		card = api.get_public_seller(self.pid)
		self.assertEqual(card["business_name"], "Test Farms")
		self.assertEqual(card["live_listing_count"], 1)
		self.assertNotIn(SELLER_A, json.dumps(card, default=str))

	def test_unknown_or_inactive_sellers_are_not_found(self):
		with self.assertRaises(frappe.DoesNotExistError):
			api.get_public_seller("nope")
		frappe.db.set_value("User", SELLER_A, "enabled", 0)
		with self.assertRaises(frappe.DoesNotExistError):
			api.get_public_seller(self.pid)

	def test_public_ids_are_opaque_stable_and_reversible(self):
		self.assertNotIn("@", self.pid)
		self.assertEqual(self.pid, lu.public_seller_id(SELLER_A))
		self.assertNotEqual(self.pid, lu.public_seller_id(SELLER_B))
		self.assertEqual(lu.seller_profile_from_public_id(self.pid), SELLER_A)
		self.assertIsNone(lu.seller_profile_from_public_id(""))


class TestListingCategories(MarketplaceTestCase):
	def test_guests_get_name_title_and_area_only(self):
		frappe.set_user("Guest")
		rows = api.list_listing_categories()
		row = next(r for r in rows if r["name"] == self.category)
		self.assertEqual(set(row), {"name", "title", "area"})
		self.assertEqual((row["title"], row["area"]), ("ZZ Listing Test Category", "Vegetables"))

	def test_the_list_is_sorted_by_title(self):
		make_category("ZZ Listing Test B")
		make_category("ZZ Listing Test A")
		titles = [r["title"] for r in api.list_listing_categories()]
		self.assertLess(titles.index("ZZ Listing Test A"), titles.index("ZZ Listing Test B"))


class TestMyListingsFilters(MarketplaceTestCase):
	def test_filters_by_selling_type_and_hides_archived(self):
		direct = self.live(title="ZZ Test Direct")
		archived = self.live(title="ZZ Test Gone", status="Archived")
		auction, _ = make_auction_listing(self.seller, self.category)
		frappe.set_user(SELLER_A)
		names = lambda **kw: {i["name"] for i in api.list_my_listings(**kw)["items"]}  # noqa: E731
		self.assertEqual(names(selling_type="Direct"), {direct.name, archived.name})
		self.assertEqual(names(selling_type="Direct", exclude_archived=1), {direct.name})
		self.assertEqual(names(selling_type="Auction"), {auction.name})
		self.assertEqual(names(exclude_archived=1), {direct.name, auction.name})
		with self.assertRaises(frappe.ValidationError):
			api.list_my_listings(selling_type="Barter")

	def test_tab_counts_ignore_the_filters(self):
		self.live(status="Archived")
		self.live()
		frappe.set_user(SELLER_A)
		result = api.list_my_listings(selling_type="Direct", exclude_archived=1)
		self.assertEqual((result["total"], result["counts"]["archived"], result["counts"]["live"]), (1, 1, 1))


class TestListingHistory(MarketplaceTestCase):
	def _review(self, listing, action, **values):
		with lu.system_write():
			frappe.get_doc(
				{"doctype": "Listing Review", "listing": listing.name, "action": action, **values}
			).insert(ignore_permissions=True)

	def test_the_owner_sees_decisions_newest_first_without_staff_identities(self):
		listing = self.live(status="Pending Approval")
		self._review(listing, "Rejected", reason="Photos are blurry.")
		self._review(listing, "Resubmitted", seller_note="New photos.")
		frappe.set_user(SELLER_A)
		history = api.get_listing_history(listing.name)
		self.assertEqual([h["action"] for h in history], ["Resubmitted", "Rejected"])
		self.assertEqual([h["by"] for h in history], ["you", "staff"])
		self.assertEqual(history[1]["reason"], "Photos are blurry.")
		self.assertEqual(history[0]["seller_note"], "New photos.")
		self.assertNotIn("reviewer", history[0])
		self.assertNotIn("Administrator", json.dumps(history, default=str))

	def test_a_listing_with_no_history_gives_an_empty_list(self):
		listing = self.live()
		frappe.set_user(SELLER_A)
		self.assertEqual(api.get_listing_history(listing.name), [])

	def test_another_sellers_listing_is_not_found(self):
		listing = self.live()
		frappe.set_user(SELLER_B)
		with self.assertRaises(frappe.DoesNotExistError):
			api.get_listing_history(listing.name)
		with self.assertRaises(frappe.DoesNotExistError):
			api.get_listing_history("LST-99999999")

	def test_only_a_verified_seller_may_ask(self):
		with self.assertRaises(frappe.PermissionError):
			api.get_listing_history("anything")
