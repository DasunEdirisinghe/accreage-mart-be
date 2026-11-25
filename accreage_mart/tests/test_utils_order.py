from datetime import timedelta

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import add_to_date, get_datetime, now_datetime

from accreage_mart.tests.order_fixtures import (
	BUYER_A,
	BUYER_B,
	SELLER_A,
	SELLER_B,
	USERS,
	make_offer,
	make_order,
	make_proof,
	make_seller,
	purge_orders,
	setup_world,
)
from accreage_mart.utils import listing as lu
from accreage_mart.utils import order as ou


def _order(status, **flags):
	return frappe._dict({"status": status, **flags})


class TestStateMachine(FrappeTestCase):
	def test_happy_path_transitions(self):
		path = [
			(ou.PENDING_CONFIRMATION, ou.CONFIRMED, ou.SELLER),
			(ou.NEGOTIATING, ou.CONFIRMED, ou.BUYER),
			(ou.NEGOTIATING, ou.CONFIRMED, ou.SELLER),
			(ou.CONFIRMED, ou.PAYMENT_REVIEW, ou.BUYER),
			(ou.PAYMENT_REVIEW, ou.PAID, ou.SELLER),
			(ou.PAYMENT_REVIEW, ou.PAID, ou.STAFF),
			(ou.PAYMENT_REVIEW, ou.CONFIRMED, ou.SELLER),
			(ou.PAID, ou.DELIVERED, ou.SELLER),
			(ou.DELIVERED, ou.COMPLETED, ou.BUYER),
			(ou.DELIVERED, ou.COMPLETED, ou.SYSTEM),
		]
		for frm, to, actor in path:
			self.assertTrue(ou.can_transition(_order(frm), to, actor), f"{frm} -> {to} by {actor}")

	def test_placement_and_auction_win(self):
		self.assertTrue(ou.can_transition(_order(None), ou.PENDING_CONFIRMATION, ou.BUYER))
		self.assertTrue(ou.can_transition(_order(None), ou.NEGOTIATING, ou.BUYER))
		self.assertTrue(ou.can_transition(_order(None), ou.CONFIRMED, ou.SYSTEM))
		self.assertFalse(ou.can_transition(_order(None), ou.CONFIRMED, ou.BUYER))

	def test_wrong_actor_is_refused(self):
		refused = [
			(ou.PENDING_CONFIRMATION, ou.CONFIRMED, ou.BUYER),
			(ou.CONFIRMED, ou.PAYMENT_REVIEW, ou.SELLER),
			(ou.PAYMENT_REVIEW, ou.PAID, ou.BUYER),
			(ou.PAID, ou.DELIVERED, ou.BUYER),
			(ou.DELIVERED, ou.COMPLETED, ou.SELLER),
		]
		for frm, to, actor in refused:
			self.assertFalse(ou.can_transition(_order(frm), to, actor), f"{frm} -> {to} by {actor}")

	def test_skipping_steps_and_leaving_terminal_states_is_refused(self):
		self.assertFalse(ou.can_transition(_order(ou.PENDING_CONFIRMATION), ou.PAID, ou.STAFF))
		self.assertFalse(ou.can_transition(_order(ou.CONFIRMED), ou.DELIVERED, ou.SELLER))
		for terminal in ou.TERMINAL_STATUSES:
			self.assertEqual(ou.allowed_transitions(terminal), {})

	def test_cancel_before_confirmation(self):
		for status in (ou.PENDING_CONFIRMATION, ou.NEGOTIATING):
			for actor in (ou.BUYER, ou.SELLER, ou.STAFF, ou.SYSTEM):
				self.assertTrue(ou.can_transition(_order(status), ou.CANCELLED, actor), f"{status} {actor}")

	def test_d13_cancel_in_confirmed_with_no_slip(self):
		order = _order(ou.CONFIRMED, has_submitted_proof=0)
		self.assertFalse(ou.can_transition(order, ou.CANCELLED, ou.BUYER))  # request only (D5)
		self.assertTrue(ou.can_transition(order, ou.CANCELLED, ou.SELLER))
		self.assertTrue(ou.can_transition(order, ou.CANCELLED, ou.STAFF))
		self.assertTrue(ou.can_transition(order, ou.CANCELLED, ou.SYSTEM))

	def test_d13_cancel_in_confirmed_after_a_rejected_slip(self):
		order = _order(ou.CONFIRMED, has_submitted_proof=1)
		self.assertTrue(ou.can_transition(order, ou.CANCELLED, ou.BUYER))
		self.assertFalse(ou.can_transition(order, ou.CANCELLED, ou.SELLER))
		self.assertTrue(ou.can_transition(order, ou.CANCELLED, ou.STAFF))
		self.assertFalse(ou.can_transition(order, ou.CANCELLED, ou.SYSTEM))

	def test_d13_nobody_cancels_under_review_or_after_approval(self):
		for status in (ou.PAYMENT_REVIEW, ou.PAID, ou.DELIVERED):
			for actor in (ou.BUYER, ou.SELLER, ou.STAFF, ou.SYSTEM):
				self.assertFalse(
					ou.can_transition(_order(status, has_submitted_proof=1), ou.CANCELLED, actor),
					f"{status} {actor}",
				)

	def test_staff_approve_payment_only_on_a_flagged_order(self):
		self.assertFalse(ou.can_transition(_order(ou.CONFIRMED), ou.PAID, ou.STAFF))
		self.assertTrue(ou.can_transition(_order(ou.CONFIRMED, needs_staff_review=1), ou.PAID, ou.STAFF))
		self.assertFalse(ou.can_transition(_order(ou.CONFIRMED, needs_staff_review=1), ou.PAID, ou.SELLER))

	def test_assert_transition_raises(self):
		with self.assertRaises(frappe.ValidationError):
			ou.assert_transition(_order(ou.COMPLETED), ou.CANCELLED, ou.STAFF)


class TestDeadlines(FrappeTestCase):
	def test_deadline_for_each_timed_status(self):
		start = get_datetime("2026-11-25 10:00:00")
		self.assertEqual(ou.deadline_for(ou.PENDING_CONFIRMATION, start), start + timedelta(hours=48))
		self.assertEqual(ou.deadline_for(ou.NEGOTIATING, start), start + timedelta(hours=48))
		self.assertEqual(ou.deadline_for(ou.CONFIRMED, start), start + timedelta(hours=72))
		self.assertEqual(ou.deadline_for(ou.DELIVERED, start), start + timedelta(days=7))
		for status in (ou.PAYMENT_REVIEW, ou.PAID, ou.COMPLETED, ou.CANCELLED):
			self.assertIsNone(ou.deadline_for(status, start))

	def test_deadline_info_without_a_negotiation(self):
		deadline = add_to_date(now_datetime(), hours=5)
		cases = [
			(_order(ou.PENDING_CONFIRMATION, status_deadline=deadline), ou.SELLER, ou.ON_EXPIRY_CANCEL, True),
			(_order(ou.CONFIRMED, status_deadline=deadline), ou.BUYER, ou.ON_EXPIRY_CANCEL, True),
			(
				_order(ou.CONFIRMED, status_deadline=deadline, has_submitted_proof=1),
				ou.BUYER,
				ou.ON_EXPIRY_STAFF_REVIEW,
				True,
			),
			(
				_order(ou.CONFIRMED, status_deadline=deadline, has_submitted_proof=1, needs_staff_review=1),
				ou.STAFF,
				None,
				False,
			),
			(_order(ou.PAYMENT_REVIEW), ou.SELLER, None, False),
			(_order(ou.PAID), ou.SELLER, None, False),
			(_order(ou.DELIVERED, status_deadline=deadline), ou.BUYER, ou.ON_EXPIRY_AUTO_COMPLETE, True),
			(_order(ou.COMPLETED), ou.NONE, None, False),
			(_order(ou.CANCELLED), ou.NONE, None, False),
		]
		for order, action_by, on_expiry, has_deadline in cases:
			info = ou.deadline_info(order)
			self.assertEqual(info["action_by"], action_by, order)
			self.assertEqual(info["on_expiry"], on_expiry, order)
			self.assertEqual(info["status_deadline"] is not None, has_deadline, order)

	def test_deadline_passed(self):
		self.assertFalse(ou.deadline_passed(_order(ou.CONFIRMED)))
		self.assertFalse(
			ou.deadline_passed(_order(ou.CONFIRMED, status_deadline=add_to_date(now_datetime(), hours=1)))
		)
		past = _order(ou.CONFIRMED, status_deadline=add_to_date(now_datetime(), minutes=-1))
		self.assertTrue(ou.deadline_passed(past))
		with self.assertRaises(frappe.ValidationError):
			ou.assert_before_deadline(past)


class OrderUtilsTestCase(FrappeTestCase):
	def setUp(self):
		setup_world(self)

	def tearDown(self):
		purge_orders(*USERS)


class TestNegotiationTurn(OrderUtilsTestCase):
	def test_action_by_is_the_party_who_did_not_make_the_open_offer(self):
		order = make_order(self.listing, BUYER_A, status=ou.NEGOTIATING)
		first = make_offer(order.name, BUYER_A, ou.PARTY_BUYER, 100)
		self.assertEqual(ou.deadline_info(order)["action_by"], ou.SELLER)

		with lu.system_write():
			first.outcome = ou.OFFER_COUNTERED
			first.save(ignore_permissions=True)
		make_offer(order.name, SELLER_A, ou.PARTY_SELLER, 110)
		self.assertEqual(ou.deadline_info(order)["action_by"], ou.BUYER)


class TestViewerRole(OrderUtilsTestCase):
	def test_roles(self):
		order = make_order(self.listing, BUYER_A)
		self.assertEqual(ou.viewer_role(order, BUYER_A), ou.BUYER)
		self.assertEqual(ou.viewer_role(order, SELLER_A), ou.SELLER)
		self.assertEqual(ou.viewer_role(order, self.staff), ou.STAFF)
		self.assertEqual(ou.viewer_role(order, "Administrator"), ou.STAFF)
		self.assertIsNone(ou.viewer_role(order, BUYER_B))
		self.assertIsNone(ou.viewer_role(order, "Guest"))


class TestPermissionHooks(OrderUtilsTestCase):
	def setUp(self):
		super().setUp()
		make_seller(SELLER_B)
		self.order = make_order(self.listing, BUYER_A)
		self.offer = make_offer(self.order.name, BUYER_A, ou.PARTY_BUYER, 100)
		self.proof = make_proof(self.order.name)

	def tearDown(self):
		purge_orders(*USERS)

	def test_order_read_access(self):
		for user in (BUYER_A, SELLER_A, self.staff):
			self.assertIsNone(ou.order_has_permission(self.order, "read", user), user)
		for user in (BUYER_B, SELLER_B):
			self.assertFalse(ou.order_has_permission(self.order, "read", user), user)

	def test_order_writes_need_the_system(self):
		for user in (BUYER_A, SELLER_A, self.staff):
			for ptype in ("create", "write", "delete"):
				self.assertFalse(ou.order_has_permission(self.order, ptype, user), f"{user} {ptype}")
		with lu.system_write():
			self.assertIsNone(ou.order_has_permission(self.order, "write", SELLER_A))

	def test_offer_and_proof_read_access(self):
		for doc in (self.offer, self.proof):
			for user in (BUYER_A, SELLER_A, self.staff):
				self.assertIsNone(ou.order_child_has_permission(doc, "read", user), f"{doc.doctype} {user}")
			for user in (BUYER_B, SELLER_B):
				self.assertFalse(ou.order_child_has_permission(doc, "read", user), f"{doc.doctype} {user}")
			self.assertFalse(ou.order_child_has_permission(doc, "write", BUYER_A))

	def test_frappe_has_permission_uses_the_hooks(self):
		self.assertTrue(frappe.has_permission("Order", "read", self.order, user=BUYER_A))
		self.assertFalse(frappe.has_permission("Order", "read", self.order, user=BUYER_B))
		self.assertTrue(frappe.has_permission("Payment Proof", "read", self.proof, user=SELLER_A))
		self.assertFalse(frappe.has_permission("Payment Proof", "read", self.proof, user=SELLER_B))

	def test_list_queries_are_scoped(self):
		expectations = {
			BUYER_A: 1,
			SELLER_A: 1,
			BUYER_B: 0,
			SELLER_B: 0,
		}
		for user, count in expectations.items():
			frappe.set_user(user)
			for doctype in ("Order", "Order Offer", "Payment Proof"):
				names = frappe.get_list(doctype, filters={"name": ["like", "%"]}, pluck="name")
				mine = [n for n in names if n in (self.order.name, self.offer.name, self.proof.name)]
				self.assertEqual(len(mine), count, f"{user} {doctype}")
		frappe.set_user(self.staff)
		self.assertIn(self.order.name, frappe.get_list("Order", pluck="name"))
		frappe.set_user("Administrator")

	def test_query_conditions_for_guests_and_staff(self):
		self.assertEqual(ou.order_query_conditions("Guest"), "1=0")
		self.assertEqual(ou.order_query_conditions(self.staff), "")
		self.assertEqual(ou.payment_proof_query_conditions("Guest"), "1=0")


class TestListingOrderHelpers(OrderUtilsTestCase):
	def test_viewer_has_order(self):
		self.assertFalse(lu.viewer_has_order(self.listing.name, BUYER_A))
		order = make_order(self.listing, BUYER_A)
		self.assertTrue(lu.viewer_has_order(self.listing.name, BUYER_A))
		self.assertFalse(lu.viewer_has_order(self.listing.name, BUYER_B))
		self.assertFalse(lu.viewer_has_order(self.listing.name, "Guest"))

		frappe.db.set_value("Order", order.name, "status", ou.COMPLETED)
		self.assertTrue(lu.viewer_has_order(self.listing.name, BUYER_A))
		frappe.db.set_value("Order", order.name, "status", ou.CANCELLED)
		self.assertFalse(lu.viewer_has_order(self.listing.name, BUYER_A))

	def test_count_active_orders(self):
		self.assertEqual(lu.count_active_orders(self.listing.name), 0)
		for status in ou.STATUSES:
			make_order(self.listing, BUYER_A, status=status)
		self.assertEqual(lu.count_active_orders(self.listing.name), len(ou.STATUSES) - 2)
		self.assertEqual(len(ou.ACTIVE_STATUSES), 6)
		self.assertNotIn(ou.CANCELLED, ou.ACTIVE_STATUSES)
		self.assertNotIn(ou.COMPLETED, ou.ACTIVE_STATUSES)


class TestBankDetailsHelpers(OrderUtilsTestCase):
	def test_has_bank_details_needs_all_four(self):
		from accreage_mart.tests.order_fixtures import set_bank_details

		self.assertFalse(ou.has_bank_details(self.seller))
		set_bank_details(self.seller, bank_branch="")
		self.assertFalse(ou.has_bank_details(self.seller))
		set_bank_details(self.seller)
		self.assertTrue(ou.has_bank_details(self.seller))

	def test_normalize_account_number(self):
		self.assertEqual(ou.normalize_account_number(" 0012-3456 78 "), "0012345678")
		self.assertEqual(ou.normalize_account_number(None), "")
