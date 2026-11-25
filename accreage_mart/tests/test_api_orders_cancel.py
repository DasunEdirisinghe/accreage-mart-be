"""Story 8.2 - cancelling orders (D5 + D13) and the cancellation requests."""

from typing import ClassVar

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
	delivery_channels,
	expire_deadline,
	make_confirmed_order,
	make_offer,
	make_order,
	make_seller,
	order_notifications,
	purge_orders,
	set_bank_details,
	setup_world,
)
from accreage_mart.utils import order as ou


class CancelTestCase(FrappeTestCase):
	def setUp(self):
		setup_world(self)
		set_bank_details(self.seller)

	def tearDown(self):
		frappe.set_user("Administrator")
		purge_orders(*USERS)

	def as_user(self, user, fn, *args, **kwargs):
		frappe.set_user(user)
		try:
			return fn(*args, **kwargs)
		finally:
			frappe.set_user("Administrator")

	def pending(self):
		return make_order(self.listing, BUYER_A).name

	def negotiating(self):
		order = make_order(self.listing, BUYER_A, status=ou.NEGOTIATING, offer_count=1)
		make_offer(order.name, BUYER_A, ou.PARTY_BUYER, 100)
		return order.name

	def confirmed(self, **overrides):
		return make_confirmed_order(self.listing, BUYER_A, **overrides).name

	def after_rejected_slip(self, **overrides):
		"""Back in ``confirmed`` after the seller rejected the buyer's slip (D13)."""
		return self.confirmed(has_submitted_proof=1, **overrides)

	def status(self, order):
		return frappe.db.get_value("Order", order, "status")


class TestCancelMatrix(CancelTestCase):
	"""D5 + D13: buyer / seller / staff x the order's state."""

	def test_buyer_cancels_freely_before_the_price_is_agreed(self):
		for order in (self.pending(), self.negotiating()):
			result = self.as_user(BUYER_A, api.cancel_order, order)
			self.assertEqual(result["status"], ou.CANCELLED)
			self.assertEqual(result["cancelled_by"], BUYER_A)
			self.assertEqual(result["cancel_reason"], "Cancelled by the buyer.")
			self.assertIsNone(result["status_deadline"])

	def test_cancelling_a_negotiation_closes_the_open_offer(self):
		order = self.negotiating()
		self.as_user(BUYER_A, api.cancel_order, order)
		outcomes = frappe.get_all("Order Offer", filters={"order": order}, pluck="outcome")
		self.assertEqual(outcomes, [ou.OFFER_DECLINED])

	def test_seller_and_staff_need_a_reason(self):
		for user in (SELLER_A, STAFF):
			order = self.pending()
			with self.assertRaisesRegex(frappe.ValidationError, "give a reason"):
				self.as_user(user, api.cancel_order, order, "  ")
			result = self.as_user(user, api.cancel_order, order, "Cannot supply this week")
			self.assertEqual(result["status"], ou.CANCELLED)
			self.assertEqual(result["cancelled_by"], user)
			self.assertEqual(result["cancel_reason"], "Cannot supply this week")

	def test_seller_and_staff_can_cancel_a_negotiation(self):
		for user in (SELLER_A, STAFF):
			order = self.negotiating()
			self.assertEqual(self.as_user(user, api.cancel_order, order, "No")["status"], ou.CANCELLED)

	def test_confirmed_no_slip_buyer_must_request_instead(self):
		order = self.confirmed()
		with self.assertRaisesRegex(frappe.ValidationError, "cancellation request"):
			self.as_user(BUYER_A, api.cancel_order, order)
		self.assertEqual(self.status(order), ou.CONFIRMED)

	def test_confirmed_no_slip_seller_and_staff_cancel_with_a_reason(self):
		for user in (SELLER_A, STAFF):
			order = self.confirmed()
			with self.assertRaisesRegex(frappe.ValidationError, "give a reason"):
				self.as_user(user, api.cancel_order, order)
			self.assertEqual(
				self.as_user(user, api.cancel_order, order, "Buyer unreachable")["status"], ou.CANCELLED
			)

	def test_after_a_rejected_slip_the_seller_cannot_cancel(self):
		order = self.after_rejected_slip()
		with self.assertRaisesRegex(frappe.ValidationError, "only staff can cancel"):
			self.as_user(SELLER_A, api.cancel_order, order, "Money never arrived")
		self.assertEqual(self.status(order), ou.CONFIRMED)

	def test_after_a_rejected_slip_the_buyer_cancels_directly(self):
		order = self.after_rejected_slip()
		result = self.as_user(BUYER_A, api.cancel_order, order)
		self.assertEqual(result["status"], ou.CANCELLED)
		self.assertEqual(result["cancelled_by"], BUYER_A)

	def test_after_a_rejected_slip_staff_cancel_with_a_reason(self):
		order = self.after_rejected_slip(needs_staff_review=1, staff_cancel_requested=1)
		with self.assertRaisesRegex(frappe.ValidationError, "give a reason"):
			self.as_user(STAFF, api.cancel_order, order)
		result = self.as_user(STAFF, api.cancel_order, order, "No payment arrived after review")
		self.assertEqual(result["status"], ou.CANCELLED)
		self.assertFalse(result["needs_staff_review"])
		self.assertFalse(result["staff_cancel_requested"])

	def test_nobody_cancels_while_a_slip_is_under_review(self):
		order = make_order(self.listing, BUYER_A, status=ou.PAYMENT_REVIEW, has_submitted_proof=1).name
		for user in (BUYER_A, SELLER_A, STAFF):
			with self.assertRaisesRegex(frappe.ValidationError, "being reviewed"):
				self.as_user(user, api.cancel_order, order, "reason")
		self.assertEqual(self.status(order), ou.PAYMENT_REVIEW)

	def test_nobody_cancels_once_payment_is_approved(self):
		for status in (ou.PAID, ou.DELIVERED, ou.COMPLETED):
			order = make_order(self.listing, BUYER_A, status=status, has_submitted_proof=1).name
			for user in (BUYER_A, SELLER_A, STAFF):
				with self.assertRaisesRegex(frappe.ValidationError, "payment has been approved"):
					self.as_user(user, api.cancel_order, order, "reason")

	def test_a_cancelled_order_stays_cancelled(self):
		order = self.pending()
		self.as_user(BUYER_A, api.cancel_order, order)
		with self.assertRaisesRegex(frappe.ValidationError, "already cancelled"):
			self.as_user(STAFF, api.cancel_order, order, "again")

	def test_outsiders_cannot_cancel(self):
		order = self.pending()
		make_seller(SELLER_B)
		for user in (BUYER_B, SELLER_B):
			with self.assertRaises(frappe.PermissionError):
				self.as_user(user, api.cancel_order, order, "reason")

	def test_parties_cannot_cancel_after_the_deadline_but_staff_can(self):
		order = self.pending()
		expire_deadline(order)
		for user in (BUYER_A, SELLER_A):
			with self.assertRaisesRegex(frappe.ValidationError, "deadline"):
				self.as_user(user, api.cancel_order, order, "late")
		self.assertEqual(self.as_user(STAFF, api.cancel_order, order, "Stale")["status"], ou.CANCELLED)

	def test_reason_length_is_capped(self):
		order = self.pending()
		with self.assertRaisesRegex(frappe.ValidationError, "under 1000 characters"):
			self.as_user(SELLER_A, api.cancel_order, order, "x" * 1001)


class TestCancelNotifications(CancelTestCase):
	def test_buyer_cancel_notifies_the_seller(self):
		order = self.pending()
		self.as_user(BUYER_A, api.cancel_order, order)
		rows = order_notifications(order, "order_cancelled")
		self.assertEqual([r.for_user for r in rows], [SELLER_A])
		self.assertIn("Test Buyers Ltd cancelled order", rows[0].email_content)
		self.assertEqual(delivery_channels(rows[0].name), {"Email", "SMS"})

	def test_seller_cancel_notifies_the_buyer_with_the_reason(self):
		order = self.confirmed()
		self.as_user(SELLER_A, api.cancel_order, order, "Flooded fields")
		rows = order_notifications(order, "order_cancelled")
		self.assertEqual([r.for_user for r in rows], [BUYER_A])
		self.assertIn("Flooded fields.", rows[0].email_content)

	def test_staff_cancel_notifies_both_parties(self):
		order = self.after_rejected_slip()
		self.as_user(STAFF, api.cancel_order, order, "Resolved by phone")
		rows = order_notifications(order, "order_cancelled")
		self.assertEqual(sorted(r.for_user for r in rows), sorted([BUYER_A, SELLER_A]))
		self.assertIn("Accreage Mart staff cancelled", rows[0].email_content)


class TestCancellationRequest(CancelTestCase):
	def test_buyer_asks_the_seller_to_cancel(self):
		order = self.confirmed()
		result = self.as_user(BUYER_A, api.request_cancellation, order, "Ordered by mistake")
		self.assertEqual(result["status"], ou.CONFIRMED)
		self.assertTrue(result["cancel_requested"])
		self.assertEqual(result["cancel_request_reason"], "Ordered by mistake")

		rows = order_notifications(order, "order_cancel_requested")
		self.assertEqual([r.for_user for r in rows], [SELLER_A])
		self.assertIn("Ordered by mistake.", rows[0].email_content)

	def test_request_needs_a_reason_and_is_sent_once(self):
		order = self.confirmed()
		with self.assertRaisesRegex(frappe.ValidationError, "reason for cancelling"):
			self.as_user(BUYER_A, api.request_cancellation, order, "")
		self.as_user(BUYER_A, api.request_cancellation, order, "Changed plans")
		with self.assertRaisesRegex(frappe.ValidationError, "already asked"):
			self.as_user(BUYER_A, api.request_cancellation, order, "Again")

	def test_request_only_where_the_buyer_cannot_cancel_directly(self):
		with self.assertRaisesRegex(frappe.ValidationError, "cancel this order directly"):
			self.as_user(BUYER_A, api.request_cancellation, self.pending(), "x")
		with self.assertRaisesRegex(frappe.ValidationError, "cancel this order directly"):
			self.as_user(BUYER_A, api.request_cancellation, self.after_rejected_slip(), "x")
		review = make_order(self.listing, BUYER_A, status=ou.PAYMENT_REVIEW, has_submitted_proof=1).name
		with self.assertRaisesRegex(frappe.ValidationError, "not allowed while the order is"):
			self.as_user(BUYER_A, api.request_cancellation, review, "x")

	def test_only_the_buyer_requests(self):
		order = self.confirmed()
		for user in (SELLER_A, STAFF):
			with self.assertRaises(frappe.PermissionError):
				self.as_user(user, api.request_cancellation, order, "x")

	def test_request_refused_after_the_deadline(self):
		order = self.confirmed()
		expire_deadline(order)
		with self.assertRaisesRegex(frappe.ValidationError, "deadline"):
			self.as_user(BUYER_A, api.request_cancellation, order, "x")

	def test_seller_accepts_the_request(self):
		order = self.confirmed(cancel_requested=1, cancel_request_reason="Wrong item")
		result = self.as_user(SELLER_A, api.resolve_cancellation_request, order, "accept")
		self.assertEqual(result["status"], ou.CANCELLED)
		self.assertEqual(result["cancelled_by"], SELLER_A)
		self.assertIn("Wrong item", result["cancel_reason"])
		self.assertFalse(result["cancel_requested"])
		rows = order_notifications(order, "order_cancelled")
		self.assertEqual([r.for_user for r in rows], [BUYER_A])

	def test_seller_refuses_the_request(self):
		order = self.confirmed(cancel_requested=1, cancel_request_reason="Wrong item")
		result = self.as_user(SELLER_A, api.resolve_cancellation_request, order, "refuse", "Already packed")
		self.assertEqual(result["status"], ou.CONFIRMED)
		self.assertFalse(result["cancel_requested"])
		self.assertFalse(result["cancel_request_reason"])
		rows = order_notifications(order, "order_cancel_request_refused")
		self.assertEqual([r.for_user for r in rows], [BUYER_A])
		self.assertIn("Already packed.", rows[0].email_content)

	def test_resolve_guards(self):
		order = self.confirmed()
		with self.assertRaisesRegex(frappe.ValidationError, "no cancellation request"):
			self.as_user(SELLER_A, api.resolve_cancellation_request, order, "accept")
		frappe.db.set_value("Order", order, {"cancel_requested": 1, "cancel_request_reason": "x"})
		with self.assertRaisesRegex(frappe.ValidationError, "accept or refuse"):
			self.as_user(SELLER_A, api.resolve_cancellation_request, order, "maybe")
		for user in (BUYER_A, STAFF):
			with self.assertRaises(frappe.PermissionError):
				self.as_user(user, api.resolve_cancellation_request, order, "accept")
		expire_deadline(order)
		with self.assertRaisesRegex(frappe.ValidationError, "deadline"):
			self.as_user(SELLER_A, api.resolve_cancellation_request, order, "accept")


class TestStaffCancellationRequest(CancelTestCase):
	def test_seller_asks_staff_after_a_rejected_slip(self):
		order = self.after_rejected_slip()
		result = self.as_user(SELLER_A, api.request_staff_cancellation, order, "Money never arrived")
		self.assertEqual(result["status"], ou.CONFIRMED)
		self.assertTrue(result["staff_cancel_requested"])
		self.assertEqual(result["staff_cancel_request_reason"], "Money never arrived")

		rows = order_notifications(order, "order_staff_cancel_requested")
		recipients = {r.for_user for r in rows}
		self.assertIn(STAFF, recipients)
		self.assertIn(BUYER_A, recipients)
		self.assertNotIn(SELLER_A, recipients)
		buyer_row = next(r for r in rows if r.for_user == BUYER_A)
		self.assertIn("Money never arrived.", buyer_row.email_content)

	def test_staff_users_are_active_staff_and_admins(self):
		users = ou.staff_users()
		self.assertIn(STAFF, users)
		for user in (BUYER_A, SELLER_A, "Administrator"):
			self.assertNotIn(user, users)
		frappe.db.set_value("User", STAFF, "enabled", 0)
		self.assertNotIn(STAFF, ou.staff_users())

	def test_request_guards(self):
		with self.assertRaisesRegex(frappe.ValidationError, "cancel this order yourself"):
			self.as_user(SELLER_A, api.request_staff_cancellation, self.confirmed(), "x")
		order = self.after_rejected_slip()
		with self.assertRaisesRegex(frappe.ValidationError, "reason for cancelling"):
			self.as_user(SELLER_A, api.request_staff_cancellation, order, " ")
		for user in (BUYER_A, STAFF):
			with self.assertRaises(frappe.PermissionError):
				self.as_user(user, api.request_staff_cancellation, order, "x")
		self.as_user(SELLER_A, api.request_staff_cancellation, order, "x")
		with self.assertRaisesRegex(frappe.ValidationError, "already asked staff"):
			self.as_user(SELLER_A, api.request_staff_cancellation, order, "x")

	def test_request_refused_after_the_deadline(self):
		order = self.after_rejected_slip()
		expire_deadline(order)
		with self.assertRaisesRegex(frappe.ValidationError, "deadline"):
			self.as_user(SELLER_A, api.request_staff_cancellation, order, "x")

	def test_staff_cancel_on_request(self):
		order = self.after_rejected_slip(staff_cancel_requested=1, staff_cancel_request_reason="x")
		result = self.as_user(STAFF, api.resolve_staff_cancellation_request, order, "cancel", "Checked bank")
		self.assertEqual(result["status"], ou.CANCELLED)
		self.assertEqual(result["cancelled_by"], STAFF)
		self.assertEqual(result["cancel_reason"], "Checked bank")
		rows = order_notifications(order, "order_cancelled")
		self.assertEqual(sorted(r.for_user for r in rows), sorted([BUYER_A, SELLER_A]))

	def test_staff_refuse_the_request(self):
		order = self.after_rejected_slip(staff_cancel_requested=1, staff_cancel_request_reason="x")
		result = self.as_user(STAFF, api.resolve_staff_cancellation_request, order, "refuse", "Paid in full")
		self.assertEqual(result["status"], ou.CONFIRMED)
		self.assertFalse(result["staff_cancel_requested"])
		rows = order_notifications(order, "order_staff_cancel_request_refused")
		self.assertEqual([r.for_user for r in rows], [SELLER_A])
		self.assertIn("Paid in full", rows[0].email_content)

	def test_staff_resolve_guards(self):
		order = self.after_rejected_slip()
		with self.assertRaisesRegex(frappe.ValidationError, "no request"):
			self.as_user(STAFF, api.resolve_staff_cancellation_request, order, "cancel", "x")
		frappe.db.set_value("Order", order, "staff_cancel_requested", 1)
		with self.assertRaisesRegex(frappe.ValidationError, "give a reason"):
			self.as_user(STAFF, api.resolve_staff_cancellation_request, order, "refuse", "")
		with self.assertRaisesRegex(frappe.ValidationError, "cancel or refuse"):
			self.as_user(STAFF, api.resolve_staff_cancellation_request, order, "maybe", "x")
		for user in (BUYER_A, SELLER_A):
			with self.assertRaises(frappe.PermissionError):
				self.as_user(user, api.resolve_staff_cancellation_request, order, "cancel", "x")


class TestOrderEmailTemplates(FrappeTestCase):
	CONTEXT: ClassVar[dict] = {
		"full_name": "Nimal",
		"order": "ORD-00001",
		"title": "Carrots",
		"quantity": "50",
		"unit": "kg",
		"actor_name": "Test Farms",
		"seller_name": "Test Farms",
		"buyer_name": "Hotel Lanka",
		"price": "Rs. 110.00",
		"total": "Rs. 5,500.00",
		"hours": 72,
		"reason": "Too low",
		"final": True,
		"for_buyer": True,
		"for_staff": True,
	}
	EVENTS = (
		"order_offer_countered",
		"order_offer_accepted",
		"order_offer_declined",
		"order_confirmed",
		"order_declined",
		"order_cancelled",
		"order_cancel_requested",
		"order_cancel_request_refused",
		"order_staff_cancel_requested",
		"order_staff_cancel_request_refused",
	)

	def test_every_story_8_2_template_renders(self):
		from accreage_mart.setup.install import ensure_email_templates

		ensure_email_templates()
		for event in self.EVENTS:
			for flags in ({}, {"final": False, "for_buyer": False, "for_staff": False, "reason": None}):
				template = frappe.get_doc("Email Template", event)
				formatted = template.get_formatted_email({**self.CONTEXT, **flags})
				self.assertIn("ORD-00001", formatted["subject"], event)
				self.assertIn("ORD-00001", formatted["message"], event)
				self.assertNotIn("{{", formatted["message"], event)
				self.assertNotIn("{%", formatted["message"], event)
