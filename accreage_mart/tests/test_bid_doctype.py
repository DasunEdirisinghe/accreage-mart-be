import frappe
from frappe.tests.utils import FrappeTestCase

from accreage_mart.tests.listing_fixtures import (
	SELLER_A,
	make_auction_listing,
	make_category,
	make_seller,
	purge,
	set_status,
	start_auction_now,
)
from accreage_mart.utils import listing as lu


class TestBidDoctype(FrappeTestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		purge(SELLER_A)
		self.seller = make_seller(SELLER_A)
		self.category = make_category()
		self.listing, self.auction = make_auction_listing(self.seller, self.category)
		set_status(self.listing.name, "Approved")
		start_auction_now(self.auction.name)

	def tearDown(self):
		frappe.set_user("Administrator")
		purge(SELLER_A)

	def _bid(self, **overrides):
		values = {"doctype": "Bid", "auction": self.auction.name, "bidder": SELLER_A, "amount": 150}
		values.update(overrides)
		return frappe.get_doc(values)

	def test_a_bid_cannot_be_inserted_without_system_write(self):
		frappe.set_user(SELLER_A)
		with self.assertRaises(frappe.PermissionError):
			self._bid().insert(ignore_permissions=True)

	def test_a_bid_is_system_insertable_and_gets_a_bid_time(self):
		with lu.system_write():
			bid = self._bid().insert(ignore_permissions=True)
		self.assertTrue(bid.name.startswith("BID-"))
		self.assertIsNotNone(bid.bid_time)

	def test_a_bid_cannot_be_edited_once_placed(self):
		with lu.system_write():
			bid = self._bid().insert(ignore_permissions=True)
		bid.amount = 200
		with self.assertRaises(frappe.ValidationError):
			bid.save(ignore_permissions=True)

	def test_a_bid_cannot_be_deleted(self):
		with lu.system_write():
			bid = self._bid().insert(ignore_permissions=True)
		with self.assertRaises(frappe.PermissionError):
			frappe.delete_doc("Bid", bid.name, ignore_permissions=True)
