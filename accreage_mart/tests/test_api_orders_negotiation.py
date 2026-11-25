"""Story 8.2 - negotiation, listed-price confirmation and the stock-impact figures."""

from datetime import timedelta

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import get_datetime, now_datetime

from accreage_mart.api import orders as api
from accreage_mart.tests.order_fixtures import (
	BUYER_A,
	BUYER_B,
	SELLER_A,
	SELLER_B,
	STAFF,
	USERS,
	approved_listing,
	delivery_channels,
	expire_deadline,
	make_confirmed_order,
	make_order,
	make_seller,
	order_notifications,
	purge_orders,
	set_bank_details,
	setup_world,
)
from accreage_mart.utils import order as ou


class NegotiationTestCase(FrappeTestCase):
	def setUp(self):
		setup_world(self)
		set_bank_details(self.seller)

	def tearDown(self):
		frappe.set_user("Administrator")
		purge_orders(*USERS)

	def negotiate(self, offer=100, quantity=50) -> str:
		"""A negotiating order with the buyer's opening offer (offer 1 of 5)."""
		frappe.set_user(BUYER_A)
		return api.place_order(self.listing.name, quantity, offer_price=offer)["name"]

	def respond(self, user, order, action, price=None, reason=None):
		frappe.set_user(user)
		return api.respond_to_offer(order, action, price=price, reason=reason)

	def offers(self, order):
		return frappe.get_all(
			"Order Offer",
			filters={"order": order},
			fields=["party", "unit_price", "outcome", "offered_by"],
			order_by="offered_on asc, creation asc",
		)


class TestCounter(NegotiationTestCase):
	def test_seller_counter_closes_the_open_offer_and_opens_a_new_one(self):
		order = self.negotiate(100)
		before = now_datetime()
		result = self.respond(SELLER_A, order, "counter", price=110)

		self.assertEqual(result["status"], ou.NEGOTIATING)
		self.assertEqual(result["offer_count"], 2)
		self.assertEqual(result["total_price"], 50 * 110)
		self.assertEqual(result["action_by"], ou.BUYER)
		deadline = get_datetime(result["status_deadline"])
		self.assertGreaterEqual(deadline, before + timedelta(hours=48) - timedelta(seconds=1))

		offers = self.offers(order)
		self.assertEqual([o.outcome for o in offers], [ou.OFFER_COUNTERED, ou.OFFER_OPEN])
		self.assertEqual(offers[1].party, ou.PARTY_SELLER)
		self.assertEqual(offers[1].offered_by, SELLER_A)
		self.assertEqual(offers[1].unit_price, 110)

	def test_only_one_offer_is_ever_open(self):
		order = self.negotiate(100)
		self.respond(SELLER_A, order, "counter", price=115)
		self.respond(BUYER_A, order, "counter", price=105)
		self.respond(SELLER_A, order, "counter", price=112)
		open_offers = [o for o in self.offers(order) if o.outcome == ou.OFFER_OPEN]
		self.assertEqual(len(open_offers), 1)
		self.assertEqual(open_offers[0].unit_price, 112)

	def test_counter_must_differ_from_the_last_offer(self):
		order = self.negotiate(100)
		with self.assertRaisesRegex(frappe.ValidationError, "differ from the last offer"):
			self.respond(SELLER_A, order, "counter", price=100)
		with self.assertRaisesRegex(frappe.ValidationError, "differ from the last offer"):
			self.respond(SELLER_A, order, "counter", price="100.001")
		self.assertEqual(len(self.offers(order)), 1)

	def test_counter_needs_a_positive_price(self):
		order = self.negotiate(100)
		with self.assertRaisesRegex(frappe.ValidationError, "Enter your counter price"):
			self.respond(SELLER_A, order, "counter")
		for price in (0, -5):
			with self.assertRaisesRegex(frappe.ValidationError, "greater than zero"):
				self.respond(SELLER_A, order, "counter", price=price)

	def test_after_five_offers_only_accept_or_decline(self):
		order = self.negotiate(100)  # 1 (buyer)
		self.respond(SELLER_A, order, "counter", price=118)  # 2
		self.respond(BUYER_A, order, "counter", price=104)  # 3
		self.respond(SELLER_A, order, "counter", price=115)  # 4
		self.respond(BUYER_A, order, "counter", price=108)  # 5
		with self.assertRaisesRegex(frappe.ValidationError, "only accept or decline"):
			self.respond(SELLER_A, order, "counter", price=112)
		result = self.respond(SELLER_A, order, "accept")
		self.assertEqual(result["status"], ou.CONFIRMED)
		self.assertEqual(result["agreed_unit_price"], 108)
		self.assertEqual(result["offer_count"], 5)

	def test_unknown_action_is_refused(self):
		order = self.negotiate(100)
		with self.assertRaisesRegex(frappe.ValidationError, "accept, counter or decline"):
			self.respond(SELLER_A, order, "haggle")


class TestTurnTaking(NegotiationTestCase):
	def test_the_party_who_made_the_open_offer_cannot_answer_it(self):
		order = self.negotiate(100)
		for action, price in (("accept", None), ("counter", 90), ("decline", None)):
			with self.assertRaisesRegex(frappe.ValidationError, "other party"):
				self.respond(BUYER_A, order, action, price=price)
		self.respond(SELLER_A, order, "counter", price=110)
		with self.assertRaisesRegex(frappe.ValidationError, "other party"):
			self.respond(SELLER_A, order, "counter", price=112)
		with self.assertRaisesRegex(frappe.ValidationError, "other party"):
			self.respond(SELLER_A, order, "accept")

	def test_outsiders_and_staff_cannot_negotiate(self):
		order = self.negotiate(100)
		make_seller(SELLER_B)
		for user in (BUYER_B, SELLER_B):
			with self.assertRaisesRegex(frappe.PermissionError, "do not have access"):
				self.respond(user, order, "accept")
		with self.assertRaisesRegex(frappe.PermissionError, "Only the buyer and the seller"):
			self.respond(STAFF, order, "accept")

	def test_only_negotiating_orders_take_responses(self):
		frappe.set_user(BUYER_A)
		order = api.place_order(self.listing.name, 20)["name"]
		with self.assertRaisesRegex(frappe.ValidationError, "not allowed while the order is"):
			self.respond(SELLER_A, order, "accept")

	def test_no_response_after_the_deadline(self):
		order = self.negotiate(100)
		expire_deadline(order)
		for action, price in (("accept", None), ("counter", 110), ("decline", None)):
			with self.assertRaisesRegex(frappe.ValidationError, "deadline"):
				self.respond(SELLER_A, order, action, price=price)
		self.assertEqual(frappe.db.get_value("Order", order, "status"), ou.NEGOTIATING)


class TestAccept(NegotiationTestCase):
	def test_seller_accept_locks_the_price_and_starts_the_payment_clock(self):
		order = self.negotiate(100)
		before = now_datetime()
		result = self.respond(SELLER_A, order, "accept")

		self.assertEqual(result["status"], ou.CONFIRMED)
		self.assertEqual(result["agreed_unit_price"], 100)
		self.assertEqual(result["total_price"], 5000)
		self.assertTrue(result["confirmed_on"])
		self.assertEqual(result["action_by"], ou.BUYER)
		self.assertEqual(result["on_expiry"], ou.ON_EXPIRY_CANCEL)
		deadline = get_datetime(result["status_deadline"])
		self.assertGreaterEqual(deadline, before + timedelta(hours=72) - timedelta(seconds=1))
		self.assertLessEqual(deadline, now_datetime() + timedelta(hours=72))
		self.assertEqual([o.outcome for o in self.offers(order)], [ou.OFFER_ACCEPTED])
		self.assertEqual(result["stock_impact"]["this_order"], 50)

	def test_buyer_accepts_the_sellers_counter(self):
		order = self.negotiate(100)
		self.respond(SELLER_A, order, "counter", price=110)
		result = self.respond(BUYER_A, order, "accept")
		self.assertEqual(result["status"], ou.CONFIRMED)
		self.assertEqual(result["agreed_unit_price"], 110)
		self.assertEqual(result["total_price"], 5500)
		self.assertIsNone(result["stock_impact"])  # stock figures are the seller's business
		self.assertEqual(result["seller_bank_details"]["account_number"], "0012345678")

	def test_seller_accept_needs_bank_details(self):
		frappe.db.set_value("Seller Profile", self.seller, "account_number", None)
		order = self.negotiate(100)
		with self.assertRaisesRegex(frappe.ValidationError, "Add your bank details first"):
			self.respond(SELLER_A, order, "accept")
		self.assertEqual(frappe.db.get_value("Order", order, "status"), ou.NEGOTIATING)

	def test_seller_counter_needs_bank_details_too(self):
		"""A counter can be accepted straight into a confirmed order, which needs somewhere to pay."""
		frappe.db.set_value("Seller Profile", self.seller, "bank_branch", None)
		order = self.negotiate(100)
		with self.assertRaisesRegex(frappe.ValidationError, "Add your bank details first"):
			self.respond(SELLER_A, order, "counter", price=110)
		# Declining needs no bank details.
		self.assertEqual(self.respond(SELLER_A, order, "decline")["status"], ou.CANCELLED)

	def test_accept_rechecks_quantity_against_current_stock(self):
		order = self.negotiate(100, quantity=50)
		frappe.db.set_value("Listing", self.listing.name, "quantity_available", 30)
		with self.assertRaisesRegex(frappe.ValidationError, "Only 30 kg is left"):
			self.respond(SELLER_A, order, "accept")
		self.assertEqual(frappe.db.get_value("Order", order, "status"), ou.NEGOTIATING)
		self.assertEqual([o.outcome for o in self.offers(order)], [ou.OFFER_OPEN])

		frappe.db.set_value("Listing", self.listing.name, "quantity_available", 50)
		self.assertEqual(self.respond(SELLER_A, order, "accept")["status"], ou.CONFIRMED)

	def test_price_cannot_change_after_accept(self):
		order = self.negotiate(100)
		self.respond(SELLER_A, order, "accept")
		with self.assertRaisesRegex(frappe.ValidationError, "not allowed while the order is"):
			self.respond(BUYER_A, order, "counter", price=90)


class TestDecline(NegotiationTestCase):
	def test_decline_cancels_the_order(self):
		order = self.negotiate(100)
		result = self.respond(SELLER_A, order, "decline", reason="Too low")
		self.assertEqual(result["status"], ou.CANCELLED)
		self.assertIsNone(result["status_deadline"])
		self.assertEqual(result["cancelled_by"], SELLER_A)
		self.assertIn("declined by the seller", result["cancel_reason"])
		self.assertIn("Too low", result["cancel_reason"])
		self.assertEqual([o.outcome for o in self.offers(order)], [ou.OFFER_DECLINED])

	def test_buyer_declines_a_counter(self):
		order = self.negotiate(100)
		self.respond(SELLER_A, order, "counter", price=119)
		result = self.respond(BUYER_A, order, "decline")
		self.assertEqual(result["status"], ou.CANCELLED)
		self.assertEqual(result["cancelled_by"], BUYER_A)
		self.assertEqual([o.outcome for o in self.offers(order)], [ou.OFFER_COUNTERED, ou.OFFER_DECLINED])


class TestNegotiationNotifications(NegotiationTestCase):
	def test_counter_notifies_the_other_party(self):
		order = self.negotiate(100)
		self.respond(SELLER_A, order, "counter", price=110)
		frappe.set_user("Administrator")
		rows = order_notifications(order, "order_offer_countered")
		self.assertEqual([r.for_user for r in rows], [BUYER_A])
		for text in (order, "Test Farms", "Rs. 110.00", "Rs. 5,500.00", "48 hours"):
			self.assertIn(text, rows[0].email_content)

		self.respond(BUYER_A, order, "counter", price=105)
		frappe.set_user("Administrator")
		rows = order_notifications(order, "order_offer_countered")
		self.assertEqual(rows[-1].for_user, SELLER_A)
		self.assertIn("Test Buyers Ltd", rows[-1].email_content)

	def test_fifth_offer_tells_the_other_party_only_accept_or_decline_remain(self):
		order = self.negotiate(100)
		self.respond(SELLER_A, order, "counter", price=118)
		self.respond(BUYER_A, order, "counter", price=104)
		self.respond(SELLER_A, order, "counter", price=115)
		self.respond(BUYER_A, order, "counter", price=108)
		frappe.set_user("Administrator")
		last = order_notifications(order, "order_offer_countered")[-1]
		self.assertEqual(last.for_user, SELLER_A)
		self.assertIn("only accept or decline", last.email_content)

	def test_accept_notifies_the_other_party_with_sms_requested(self):
		order = self.negotiate(100)
		self.respond(SELLER_A, order, "accept")
		frappe.set_user("Administrator")
		rows = order_notifications(order, "order_offer_accepted")
		self.assertEqual([r.for_user for r in rows], [BUYER_A])
		self.assertIn("72 hours", rows[0].email_content)
		self.assertEqual(delivery_channels(rows[0].name), {"Email", "SMS"})

	def test_decline_notifies_the_other_party(self):
		order = self.negotiate(100)
		self.respond(SELLER_A, order, "decline", reason="Too low")
		frappe.set_user("Administrator")
		rows = order_notifications(order, "order_offer_declined")
		self.assertEqual([r.for_user for r in rows], [BUYER_A])
		self.assertIn("Too low.", rows[0].email_content)


class TestConfirmAndDecline(NegotiationTestCase):
	def place(self, quantity=50) -> str:
		frappe.set_user(BUYER_A)
		return api.place_order(self.listing.name, quantity)["name"]

	def test_seller_confirms_at_the_listed_price(self):
		order = self.place()
		before = now_datetime()
		frappe.set_user(SELLER_A)
		result = api.confirm_order(order)
		self.assertEqual(result["status"], ou.CONFIRMED)
		self.assertEqual(result["agreed_unit_price"], 120)
		self.assertEqual(result["total_price"], 6000)
		self.assertTrue(result["confirmed_on"])
		deadline = get_datetime(result["status_deadline"])
		self.assertGreaterEqual(deadline, before + timedelta(hours=72) - timedelta(seconds=1))
		self.assertEqual(result["stock_impact"]["stock_now"], 500)

		frappe.set_user("Administrator")
		rows = order_notifications(order, "order_confirmed")
		self.assertEqual([r.for_user for r in rows], [BUYER_A])
		self.assertIn("Rs. 6,000.00", rows[0].email_content)
		self.assertEqual(delivery_channels(rows[0].name), {"Email", "SMS"})

	def test_confirm_needs_bank_details(self):
		frappe.db.set_value("Seller Profile", self.seller, "bank_name", "")
		order = self.place()
		frappe.set_user(SELLER_A)
		with self.assertRaisesRegex(frappe.ValidationError, "Add your bank details first"):
			api.confirm_order(order)
		self.assertEqual(frappe.db.get_value("Order", order, "status"), ou.PENDING_CONFIRMATION)

	def test_only_the_seller_confirms_or_declines(self):
		order = self.place()
		for user in (BUYER_A, STAFF):
			frappe.set_user(user)
			with self.assertRaises(frappe.PermissionError):
				api.confirm_order(order)
			with self.assertRaises(frappe.PermissionError):
				api.decline_order(order)

	def test_confirm_only_from_pending_confirmation(self):
		order = self.negotiate(100)
		frappe.set_user(SELLER_A)
		with self.assertRaisesRegex(frappe.ValidationError, "not allowed while the order is"):
			api.confirm_order(order)
		with self.assertRaisesRegex(frappe.ValidationError, "not allowed while the order is"):
			api.decline_order(order)

	def test_confirm_or_decline_refused_after_the_deadline(self):
		order = self.place()
		expire_deadline(order)
		frappe.set_user(SELLER_A)
		with self.assertRaisesRegex(frappe.ValidationError, "deadline"):
			api.confirm_order(order)
		with self.assertRaisesRegex(frappe.ValidationError, "deadline"):
			api.decline_order(order)

	def test_decline_cancels_and_notifies_the_buyer(self):
		order = self.place()
		frappe.set_user(SELLER_A)
		result = api.decline_order(order, reason="Sold out at the market")
		self.assertEqual(result["status"], ou.CANCELLED)
		self.assertEqual(result["cancelled_by"], SELLER_A)
		self.assertIn("Sold out at the market", result["cancel_reason"])
		frappe.set_user("Administrator")
		rows = order_notifications(order, "order_declined")
		self.assertEqual([r.for_user for r in rows], [BUYER_A])
		self.assertIn("Sold out at the market.", rows[0].email_content)


class TestStockImpact(NegotiationTestCase):
	def setUp(self):
		super().setUp()
		# Listing: 500 kg. Holding stock: 300 confirmed + 100 under slip review (another listing's
		# order and the non-holding statuses must not count).
		make_confirmed_order(self.listing, BUYER_B, quantity=300, total_price=36000)
		make_order(self.listing, BUYER_B, quantity=100, status=ou.PAYMENT_REVIEW, total_price=12000)
		make_order(self.listing, BUYER_B, quantity=40, total_price=4800)  # pending_confirmation
		make_order(self.listing, BUYER_B, quantity=70, status=ou.PAID, total_price=8400)
		make_order(self.listing, BUYER_B, quantity=60, status=ou.CANCELLED, total_price=7200)
		make_seller(SELLER_B)
		other = approved_listing(SELLER_B, self.category)
		make_confirmed_order(other, BUYER_B, quantity=200, total_price=24000)
		frappe.set_user(BUYER_A)
		self.order = api.place_order(self.listing.name, 150)["name"]

	def test_figures(self):
		frappe.set_user(SELLER_A)
		impact = api.get_stock_impact(self.order)
		self.assertEqual(impact["unit"], "kg")
		self.assertEqual(impact["stock_now"], 500)
		self.assertEqual(impact["held_by_others"], 400)
		self.assertEqual(impact["held_order_count"], 2)
		self.assertEqual(impact["this_order"], 150)
		self.assertTrue(impact["this_order_pending"])
		self.assertEqual(impact["projected"], -50)
		self.assertEqual(impact["shortfall"], 50)

	def test_confirm_is_never_blocked_and_returns_the_same_figures(self):
		frappe.set_user(SELLER_A)
		expected = api.get_stock_impact(self.order)
		result = api.confirm_order(self.order)
		self.assertEqual(result["status"], ou.CONFIRMED)
		self.assertEqual(result["stock_impact"], expected)

	def test_no_shortfall(self):
		frappe.db.set_value("Listing", self.listing.name, "quantity_available", 1000)
		frappe.set_user(SELLER_A)
		impact = api.get_stock_impact(self.order)
		self.assertEqual(impact["projected"], 450)
		self.assertEqual(impact["shortfall"], 0)

	def test_paid_order_is_already_off_the_stock(self):
		frappe.db.set_value("Order", self.order, "status", ou.PAID)
		frappe.set_user(SELLER_A)
		impact = api.get_stock_impact(self.order)
		self.assertFalse(impact["this_order_pending"])
		self.assertEqual(impact["projected"], 100)

	def test_who_can_see_it(self):
		frappe.set_user(STAFF)
		self.assertEqual(api.get_stock_impact(self.order)["stock_now"], 500)
		for user in (BUYER_A, BUYER_B):
			frappe.set_user(user)
			with self.assertRaises(frappe.PermissionError):
				api.get_stock_impact(self.order)
