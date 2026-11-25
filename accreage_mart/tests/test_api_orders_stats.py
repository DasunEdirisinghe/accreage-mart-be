"""Story 8.3 - get_order_stats: order counts by status and sales (D12)."""

import frappe
from frappe.tests.utils import FrappeTestCase

from accreage_mart.api import orders as api
from accreage_mart.tests.order_fixtures import (
	BUYER_A,
	BUYER_B,
	SELLER_A,
	SELLER_B,
	STAFF,
	USERS,
	approved_listing,
	make_confirmed_order,
	make_order,
	make_seller,
	purge_orders,
	setup_world,
)
from accreage_mart.utils import order as ou


class StatsTestCase(FrappeTestCase):
	def setUp(self):
		setup_world(self)
		self.other_seller = make_seller(SELLER_B)
		self.other_listing = approved_listing(self.other_seller, self.category)

	def tearDown(self):
		frappe.set_user("Administrator")
		purge_orders(*USERS)

	def as_user(self, user, fn, *args, **kwargs):
		frappe.set_user(user)
		try:
			return fn(*args, **kwargs)
		finally:
			frappe.set_user("Administrator")

	def seed(self, listing):
		"""One order per interesting status on ``listing``; each is 20 kg x Rs. 120 = Rs. 2,400
		unless noted."""
		make_order(listing, BUYER_A)  # pending_confirmation
		make_order(listing, BUYER_A, status=ou.NEGOTIATING)
		make_confirmed_order(listing, BUYER_A)
		make_confirmed_order(listing, BUYER_A, status=ou.PAYMENT_REVIEW, status_deadline=None)
		make_confirmed_order(listing, BUYER_B, status=ou.PAID, status_deadline=None)
		make_confirmed_order(listing, BUYER_B, status=ou.DELIVERED)
		make_confirmed_order(listing, BUYER_A, status=ou.COMPLETED, status_deadline=None)
		make_confirmed_order(
			listing, BUYER_B, status=ou.COMPLETED, status_deadline=None, quantity=10, total_price=1200
		)
		make_order(listing, BUYER_B, status=ou.CANCELLED, status_deadline=None)


class TestSellerStats(StatsTestCase):
	def test_counts_and_sales_for_the_callers_listings_only(self):
		self.seed(self.listing)
		self.seed(self.other_listing)  # another seller's orders never leak in
		stats = self.as_user(SELLER_A, api.get_order_stats)
		self.assertEqual(stats["scope"], "seller")
		self.assertEqual(
			stats["counts"],
			{
				ou.PENDING_CONFIRMATION: 1,
				ou.NEGOTIATING: 1,
				ou.CONFIRMED: 1,
				ou.PAYMENT_REVIEW: 1,
				ou.PAID: 1,
				ou.DELIVERED: 1,
				ou.COMPLETED: 2,
				ou.CANCELLED: 1,
			},
		)
		self.assertEqual(stats["total_orders"], 9)
		self.assertEqual(stats["active_orders"], 6)
		self.assertEqual(stats["awaiting_payment_review"], 1)
		self.assertEqual(stats["completed_orders"], 2)
		self.assertEqual(stats["completed_sales"], 3600)
		self.assertEqual(stats["paid_orders"], 4)
		self.assertEqual(stats["paid_value"], 2400 + 2400 + 3600)

	def test_a_seller_with_no_orders_gets_zeros(self):
		stats = self.as_user(SELLER_A, api.get_order_stats)
		self.assertEqual(set(stats["counts"]), set(ou.STATUSES))
		self.assertTrue(all(v == 0 for v in stats["counts"].values()))
		self.assertEqual((stats["total_orders"], stats["completed_sales"]), (0, 0))

	def test_a_seller_cannot_ask_for_the_platform(self):
		with self.assertRaises(frappe.PermissionError):
			self.as_user(SELLER_A, api.get_order_stats, "platform")

	def test_buyers_have_no_order_stats(self):
		with self.assertRaises(frappe.PermissionError):
			self.as_user(BUYER_A, api.get_order_stats)

	def test_unknown_scope_is_refused(self):
		with self.assertRaises(frappe.ValidationError):
			self.as_user(SELLER_A, api.get_order_stats, "galaxy")


class TestPlatformStats(StatsTestCase):
	def test_staff_see_every_seller(self):
		before = self.as_user(STAFF, api.get_order_stats)
		self.seed(self.listing)
		self.seed(self.other_listing)
		after = self.as_user(STAFF, api.get_order_stats)
		self.assertEqual(after["scope"], "platform")
		for status in ou.STATUSES:
			expected = 4 if status == ou.COMPLETED else 2
			self.assertEqual(after["counts"][status] - before["counts"][status], expected, status)
		self.assertEqual(after["total_orders"] - before["total_orders"], 18)
		self.assertEqual(after["completed_sales"] - before["completed_sales"], 7200)

	def test_staff_who_sell_can_ask_for_their_own_figures(self):
		frappe.get_doc("User", SELLER_A).add_roles("Staff")
		self.seed(self.listing)
		self.seed(self.other_listing)
		self.assertEqual(self.as_user(SELLER_A, api.get_order_stats)["scope"], "platform")
		own = self.as_user(SELLER_A, api.get_order_stats, "seller")
		self.assertEqual((own["scope"], own["total_orders"]), ("seller", 9))
