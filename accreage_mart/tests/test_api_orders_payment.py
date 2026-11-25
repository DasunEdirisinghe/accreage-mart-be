"""Story 8.3 - payment slip upload and review, the staff override for flagged orders (D13) and the
staff payments queue."""

import io
import os
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import add_days, add_to_date, get_datetime, getdate, now_datetime, nowdate
from werkzeug.test import EnvironBuilder

from accreage_mart.api import orders as api
from accreage_mart.tests.listing_fixtures import image_bytes
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
	make_order,
	make_proof,
	make_seller,
	order_notifications,
	purge_orders,
	set_bank_details,
	setup_world,
)
from accreage_mart.utils import listing as lu
from accreage_mart.utils import order as ou

UNCLEAR, NOT_RECEIVED = ou.REJECT_CATEGORIES


def pdf_bytes() -> bytes:
	"""A real one-page PDF (Frappe parses every PDF it stores), unique per call."""
	from pypdf import PdfWriter

	writer = PdfWriter()
	writer.add_blank_page(width=200, height=200)
	writer.add_metadata({"/Title": os.urandom(8).hex()})
	buffer = io.BytesIO()
	writer.write(buffer)
	return buffer.getvalue()


def multipart_request(content: bytes | None, filename: str = "slip.png"):
	data = {"file": (io.BytesIO(content), filename)} if content is not None else {}
	builder = EnvironBuilder(method="POST", base_url="http://acm.localhost", data=data)
	return builder.get_request()


class PaymentTestCase(FrappeTestCase):
	def setUp(self):
		setup_world(self)
		set_bank_details(self.seller)

	def tearDown(self):
		frappe.local.request = None
		frappe.set_user("Administrator")
		purge_orders(*USERS)

	def as_user(self, user, fn, *args, **kwargs):
		frappe.set_user(user)
		try:
			return fn(*args, **kwargs)
		finally:
			frappe.set_user("Administrator")

	def confirmed(self, **overrides):
		"""20 kg at Rs. 120 = Rs. 2,400, waiting for payment."""
		return make_confirmed_order(self.listing, BUYER_A, **overrides).name

	def upload(self, order, content=None, filename="slip.png", user=BUYER_A, **details):
		values = {
			"amount_paid": 2400,
			"paid_on": nowdate(),
			"paid_from_bank": "Sampath Bank",
			"transfer_reference": "TRX-100",
		}
		values.update(details)
		frappe.local.request = multipart_request(image_bytes("PNG") if content is None else content, filename)
		try:
			return self.as_user(user, api.upload_payment_proof, order, **values)
		finally:
			frappe.local.request = None

	def review(self, proof, decision, user=SELLER_A, **kwargs):
		return self.as_user(user, api.review_payment_proof, proof, decision, **kwargs)

	def in_review(self, **overrides):
		"""A payment_review order and its waiting slip."""
		order = self.confirmed(**overrides)
		result = self.upload(order)
		return order, result["payment_proofs"][0]["name"]

	def order(self, name):
		return frappe.get_doc("Order", name)

	def stock(self):
		return frappe.db.get_value("Listing", self.listing.name, "quantity_available")


class TestUpload(PaymentTestCase):
	def test_upload_moves_the_order_to_payment_review(self):
		order = self.confirmed()
		result = self.upload(order, note="Paid at the branch.")
		self.assertEqual(result["status"], ou.PAYMENT_REVIEW)
		self.assertTrue(result["has_submitted_proof"])
		self.assertIsNone(result["status_deadline"])
		self.assertEqual((result["action_by"], result["on_expiry"]), (ou.SELLER, None))

		[proof] = result["payment_proofs"]
		self.assertEqual(proof["status"], ou.PROOF_SUBMITTED)
		self.assertEqual(proof["amount_paid"], 2400)
		self.assertEqual(getdate(proof["paid_on"]), getdate(nowdate()))
		self.assertEqual((proof["paid_from_bank"], proof["transfer_reference"]), ("Sampath Bank", "TRX-100"))
		self.assertEqual(proof["note"], "Paid at the branch.")
		self.assertTrue(proof["amount_matches"])
		self.assertEqual(proof["expected_amount"], 2400)
		self.assertIsNone(proof["reviewer_role"])
		self.assertNotIn("reviewed_by", proof)

	def test_slip_is_a_private_file_attached_to_the_proof(self):
		result = self.upload(self.confirmed(), filename="../Dasun NIC.exe")
		proof = result["payment_proofs"][0]
		self.assertTrue(proof["file"].startswith("/private/files/ppf-"), proof["file"])
		self.assertTrue(proof["file"].endswith(".png"))
		self.assertNotIn("Dasun", proof["file"])
		file = frappe.get_doc("File", {"file_url": proof["file"], "attached_to_name": proof["name"]})
		self.assertEqual(file.is_private, 1)
		self.assertEqual((file.attached_to_doctype, file.attached_to_field), ("Payment Proof", "file"))
		self.assertEqual(file.owner, BUYER_A)

	def test_jpg_png_and_pdf_are_accepted(self):
		for content, extension in (
			(image_bytes("JPEG"), "jpg"),
			(image_bytes("PNG"), "png"),
			(pdf_bytes(), "pdf"),
		):
			order = self.confirmed()
			result = self.upload(order, content)
			self.assertTrue(result["payment_proofs"][0]["file"].endswith(f".{extension}"))

	def test_other_file_types_and_broken_images_are_refused(self):
		order = self.confirmed()
		bad = {
			"WebP image": image_bytes("WEBP"),
			"GIF image": image_bytes("GIF"),
			"text": b"just some text pretending to be a slip",
			"fake PNG": b"\x89PNG\r\n\x1a\n" + os.urandom(64),
			"broken PDF": b"%PDF-1.4\n" + os.urandom(64),
		}
		for label, content in bad.items():
			with self.subTest(label), self.assertRaises(frappe.ValidationError):
				self.upload(order, content, filename="slip.png")
		self.assertEqual(self.order(order).status, ou.CONFIRMED)
		self.assertFalse(frappe.db.exists("Payment Proof", {"order": order}))

	def test_size_limit_is_5_mb(self):
		order = self.confirmed()
		too_big = b"%PDF-1.4\n" + b"0" * ou.MAX_SLIP_BYTES
		with self.assertRaisesRegex(frappe.ValidationError, "5 MB"):
			self.upload(order, too_big)
		# The boundary itself, with the limit lowered to the size of a real PDF.
		pdf = pdf_bytes()
		with (
			patch.object(ou, "MAX_SLIP_BYTES", len(pdf) - 1),
			self.assertRaisesRegex(frappe.ValidationError, "5 MB"),
		):
			self.upload(order, pdf)
		with patch.object(ou, "MAX_SLIP_BYTES", len(pdf)):
			self.assertEqual(self.upload(order, pdf)["status"], ou.PAYMENT_REVIEW)

	def test_a_slip_is_required(self):
		order = self.confirmed()
		with self.assertRaisesRegex(frappe.ValidationError, "attach your payment slip"):
			self.upload(order, b"")
		frappe.local.request = multipart_request(None)
		with self.assertRaisesRegex(frappe.ValidationError, "attach your payment slip"):
			self.as_user(
				BUYER_A,
				api.upload_payment_proof,
				order,
				amount_paid=2400,
				paid_on=nowdate(),
				paid_from_bank="BOC",
				transfer_reference="T1",
			)

	def test_payment_details_are_checked(self):
		order = self.confirmed()
		cases = {
			"amount": {"amount_paid": 0},
			"date missing": {"paid_on": ""},
			"date garbage": {"paid_on": "not a date"},
			"future": {"paid_on": add_days(nowdate(), 1)},
			"before the order": {"paid_on": add_days(nowdate(), -3)},
			"bank": {"paid_from_bank": "  "},
			"reference": {"transfer_reference": ""},
			"long reference": {"transfer_reference": "x" * 141},
			"long note": {"note": "x" * 1001},
		}
		for label, details in cases.items():
			with self.subTest(label), self.assertRaises(frappe.ValidationError):
				self.upload(order, **details)
		self.assertEqual(self.order(order).status, ou.CONFIRMED)
		self.assertFalse(frappe.db.exists("Payment Proof", {"order": order}))

	def test_a_different_amount_is_allowed_but_flagged(self):
		result = self.upload(self.confirmed(), amount_paid=2000)
		self.assertEqual(result["status"], ou.PAYMENT_REVIEW)
		self.assertFalse(result["payment_proofs"][0]["amount_matches"])

	def test_only_the_buyer_can_upload(self):
		order = self.confirmed()
		make_seller(SELLER_B)
		for user in (SELLER_A, STAFF, BUYER_B, SELLER_B):
			with self.subTest(user), self.assertRaises(frappe.PermissionError):
				self.upload(order, user=user)

	def test_only_while_confirmed(self):
		pending = make_order(self.listing, BUYER_A).name
		with self.assertRaisesRegex(frappe.ValidationError, "not allowed"):
			self.upload(pending)
		order, _proof = self.in_review()
		with self.assertRaisesRegex(frappe.ValidationError, "already waiting for review"):
			self.upload(order)
		for status in (ou.PAID, ou.CANCELLED):
			name = self.confirmed(status=status, status_deadline=None)
			with self.subTest(status), self.assertRaises(frappe.ValidationError):
				self.upload(name)

	def test_refused_after_the_deadline(self):
		order = self.confirmed()
		expire_deadline(order)
		with self.assertRaisesRegex(frappe.ValidationError, "deadline"):
			self.upload(order)

	def test_a_flagged_order_still_accepts_a_new_slip_and_clears_the_flag(self):
		order = self.confirmed(has_submitted_proof=1, needs_staff_review=1)
		expire_deadline(order)
		result = self.upload(order)
		self.assertEqual(result["status"], ou.PAYMENT_REVIEW)
		self.assertFalse(result["needs_staff_review"])

	def test_upload_clears_a_pending_cancellation_request_but_keeps_a_staff_request(self):
		order = self.confirmed(
			cancel_requested=1,
			cancel_request_reason="Ordered twice",
			has_submitted_proof=1,
			staff_cancel_requested=1,
			staff_cancel_request_reason="Buyer stopped answering",
		)
		result = self.upload(order)
		self.assertFalse(result["cancel_requested"])
		self.assertIsNone(result["cancel_request_reason"])
		self.assertTrue(result["staff_cancel_requested"])

	def test_has_submitted_proof_is_never_cleared(self):
		order, proof = self.in_review()
		result = self.review(proof, "reject", reject_category=UNCLEAR, reject_reason="Blurry photo")
		self.assertEqual(result["status"], ou.CONFIRMED)
		self.assertTrue(result["has_submitted_proof"])
		self.assertTrue(self.order(order).has_submitted_proof)

	def test_upload_notifies_the_seller_and_staff(self):
		order = self.confirmed()
		self.upload(order, amount_paid=2000)
		rows = order_notifications(order, "payment_proof_submitted")
		users = {r.for_user for r in rows}
		self.assertIn(SELLER_A, users)
		self.assertIn(STAFF, users)
		self.assertNotIn(BUYER_A, users)
		seller_row = next(r for r in rows if r.for_user == SELLER_A)
		self.assertIn("Test Buyers Ltd uploaded a payment slip", seller_row.email_content)
		self.assertIn("TRX-100", seller_row.email_content)
		self.assertIn("differs from the order total", seller_row.email_content)


class TestProofAccess(PaymentTestCase):
	def test_proof_and_file_readable_only_by_the_parties_and_staff(self):
		make_seller(SELLER_B)
		order, proof_name = self.in_review()
		proof = frappe.get_doc("Payment Proof", proof_name)
		file = frappe.get_doc(
			"File", {"attached_to_doctype": "Payment Proof", "attached_to_name": proof_name}
		)
		for user, allowed in (
			(BUYER_A, True),
			(SELLER_A, True),
			(STAFF, True),
			(BUYER_B, False),
			(SELLER_B, False),
		):
			with self.subTest(user):
				self.assertEqual(
					bool(frappe.has_permission("Payment Proof", "read", doc=proof, user=user)), allowed
				)
				self.assertEqual(bool(frappe.has_permission("File", "read", doc=file, user=user)), allowed)

	def test_list_queries_hide_other_peoples_proofs(self):
		self.in_review()
		listed = self.as_user(BUYER_B, frappe.get_list, "Payment Proof", pluck="name")
		self.assertEqual(listed, [])

	def test_a_reviewed_proof_is_read_only_and_undeletable(self):
		order, proof_name = self.in_review()
		self.review(proof_name, "approve")
		proof = frappe.get_doc("Payment Proof", proof_name)
		proof.note = "changed"
		with lu.system_write(), self.assertRaisesRegex(frappe.ValidationError, "cannot be changed"):
			proof.save(ignore_permissions=True)
		with self.assertRaises(frappe.PermissionError):
			frappe.delete_doc("Payment Proof", proof_name, ignore_permissions=True)
		self.assertTrue(frappe.db.exists("Payment Proof", proof_name))


class TestReview(PaymentTestCase):
	def test_seller_approves_paid_and_stock_drops(self):
		order, proof = self.in_review()
		result = self.review(proof, "approve")
		self.assertEqual(result["status"], ou.PAID)
		self.assertIsNotNone(result["paid_on"])
		self.assertIsNone(result["status_deadline"])
		self.assertEqual(result["payment_proofs"][0]["status"], ou.PROOF_APPROVED)
		self.assertEqual(result["payment_proofs"][0]["reviewer_role"], ou.SELLER)
		self.assertEqual(
			result["stock_update"],
			{"unit": "kg", "stock_before": 500, "quantity": 20, "stock_after": 480, "shortfall": 0},
		)
		self.assertEqual(self.stock(), 480)
		stored = frappe.get_doc("Payment Proof", proof)
		self.assertEqual(stored.reviewed_by, SELLER_A)
		self.assertIsNotNone(stored.reviewed_on)

	def test_stock_is_floored_at_zero_and_the_shortfall_reported(self):
		order, proof = self.in_review()
		frappe.db.set_value("Listing", self.listing.name, "quantity_available", 5)
		result = self.review(proof, "approve")
		self.assertEqual(result["status"], ou.PAID)
		self.assertEqual(result["stock_update"]["stock_after"], 0)
		self.assertEqual(result["stock_update"]["shortfall"], 15)
		self.assertEqual(self.stock(), 0)

	def test_reject_needs_a_category_and_a_reason(self):
		order, proof = self.in_review()
		for kwargs in (
			{"reject_reason": "Blurry"},
			{"reject_category": "Looks fake", "reject_reason": "Blurry"},
			{"reject_category": UNCLEAR},
			{"reject_category": UNCLEAR, "reject_reason": "   "},
		):
			with self.subTest(kwargs), self.assertRaises(frappe.ValidationError):
				self.review(proof, "reject", **kwargs)
		self.assertEqual(self.order(order).status, ou.PAYMENT_REVIEW)

	def test_reject_returns_to_confirmed_with_a_fresh_72_hours(self):
		order = self.confirmed(status_deadline=add_to_date(now_datetime(), hours=2))
		proof = self.upload(order)["payment_proofs"][0]["name"]
		result = self.review(
			proof, "reject", reject_category=NOT_RECEIVED, reject_reason="Nothing in my account"
		)
		self.assertEqual(result["status"], ou.CONFIRMED)
		left = get_datetime(result["status_deadline"]) - now_datetime()
		self.assertGreater(left.total_seconds(), (ou.PAYMENT_HOURS - 1) * 3600)
		self.assertEqual((result["action_by"], result["on_expiry"]), (ou.BUYER, ou.ON_EXPIRY_STAFF_REVIEW))
		[slip] = result["payment_proofs"]
		self.assertEqual(slip["status"], ou.PROOF_REJECTED)
		self.assertEqual(
			(slip["reject_category"], slip["reject_reason"]), (NOT_RECEIVED, "Nothing in my account")
		)
		self.assertEqual(self.stock(), 500)
		self.assertIsNone(result["stock_update"])

	def test_buyer_can_reupload_after_a_rejection(self):
		order, proof = self.in_review()
		self.review(proof, "reject", reject_category=UNCLEAR, reject_reason="Blurry")
		result = self.upload(order, transfer_reference="TRX-200")
		self.assertEqual(result["status"], ou.PAYMENT_REVIEW)
		self.assertEqual(
			[p["status"] for p in result["payment_proofs"]], [ou.PROOF_SUBMITTED, ou.PROOF_REJECTED]
		)

	def test_first_decision_wins(self):
		order, proof = self.in_review()
		self.review(proof, "approve")
		with self.assertRaisesRegex(frappe.ValidationError, "Already reviewed"):
			self.review(proof, "approve", user=STAFF)
		with self.assertRaisesRegex(frappe.ValidationError, "Already reviewed"):
			self.review(proof, "reject", user=STAFF, reject_category=UNCLEAR, reject_reason="x")
		self.assertEqual(self.stock(), 480)  # taken off once

	def test_who_may_review(self):
		make_seller(SELLER_B)
		order, proof = self.in_review()
		for user in (BUYER_A, BUYER_B, SELLER_B):
			with self.subTest(user), self.assertRaises(frappe.PermissionError):
				self.review(proof, "approve", user=user)
		result = self.review(proof, "reject", user=STAFF, reject_category=UNCLEAR, reject_reason="Blurry")
		self.assertEqual(result["payment_proofs"][0]["reviewer_role"], ou.STAFF)

	def test_decision_and_proof_are_validated(self):
		order, proof = self.in_review()
		with self.assertRaises(frappe.ValidationError):
			self.review(proof, "maybe")
		with self.assertRaises(frappe.DoesNotExistError):
			self.review("PPF-99999999", "approve")

	def test_approval_notifies_the_buyer(self):
		order, proof = self.in_review()
		self.review(proof, "approve")
		rows = order_notifications(order, "payment_proof_approved")
		self.assertEqual([r.for_user for r in rows], [BUYER_A])
		self.assertIn("approved your payment", rows[0].email_content)
		self.assertEqual(delivery_channels(rows[0].name), {"Email", "SMS"})

	def test_staff_approval_also_tells_the_seller(self):
		order, proof = self.in_review()
		self.review(proof, "approve", user=STAFF)
		rows = order_notifications(order, "payment_proof_approved")
		self.assertEqual(sorted(r.for_user for r in rows), sorted([BUYER_A, SELLER_A]))

	def test_rejection_notifies_the_buyer_with_category_and_reason(self):
		order, proof = self.in_review()
		self.review(proof, "reject", reject_category=UNCLEAR, reject_reason="The reference is cut off")
		rows = order_notifications(order, "payment_proof_rejected")
		self.assertEqual([r.for_user for r in rows], [BUYER_A])
		self.assertIn(UNCLEAR, rows[0].email_content)
		self.assertIn("The reference is cut off.", rows[0].email_content)


class TestFlaggedPayment(PaymentTestCase):
	def flagged(self):
		order = self.confirmed(has_submitted_proof=1, needs_staff_review=1)
		make_proof(order, status=ou.PROOF_REJECTED, reject_category=UNCLEAR, reject_reason="Blurry")
		return order

	def test_staff_approve_a_flagged_order(self):
		order = self.flagged()
		result = self.as_user(STAFF, api.approve_flagged_payment, order, "Seller confirmed by phone")
		self.assertEqual(result["status"], ou.PAID)
		self.assertFalse(result["needs_staff_review"])
		self.assertEqual(result["stock_update"]["stock_after"], 480)
		self.assertEqual(self.stock(), 480)
		self.assertEqual(result["staff_payment_approved_by"], STAFF)
		self.assertEqual(result["staff_payment_approval_reason"], "Seller confirmed by phone")
		rows = order_notifications(order, "payment_proof_approved")
		self.assertEqual(sorted(r.for_user for r in rows), sorted([BUYER_A, SELLER_A]))

	def test_parties_see_the_reason_but_not_which_staff_member(self):
		order = self.flagged()
		self.as_user(STAFF, api.approve_flagged_payment, order, "Seller confirmed by phone")
		for user in (BUYER_A, SELLER_A):
			view = self.as_user(user, api.get_order, order)
			self.assertEqual(view["staff_payment_approval_reason"], "Seller confirmed by phone")
			self.assertNotIn("staff_payment_approved_by", view)

	def test_the_approval_record_cannot_be_changed(self):
		order = self.flagged()
		self.as_user(STAFF, api.approve_flagged_payment, order, "Seller confirmed by phone")
		doc = self.order(order)
		doc.staff_payment_approval_reason = "Something else"
		with lu.system_write(), self.assertRaisesRegex(frappe.ValidationError, "cannot be changed"):
			doc.save(ignore_permissions=True)
		self.assertEqual(
			frappe.db.get_value("Order", order, "staff_payment_approval_reason"), "Seller confirmed by phone"
		)

	def test_guards(self):
		order = self.flagged()
		for user in (SELLER_A, BUYER_A):
			with self.subTest(user), self.assertRaises(frappe.PermissionError):
				self.as_user(user, api.approve_flagged_payment, order, "x")
		with self.assertRaises(frappe.ValidationError):
			self.as_user(STAFF, api.approve_flagged_payment, order, "  ")
		not_flagged = self.confirmed(has_submitted_proof=1)
		with self.assertRaisesRegex(frappe.ValidationError, "not waiting for a staff decision"):
			self.as_user(STAFF, api.approve_flagged_payment, not_flagged, "x")


class TestPendingQueue(PaymentTestCase):
	def test_queue_lists_the_three_kinds_oldest_first(self):
		first, first_proof = self.in_review()
		second, second_proof = self.in_review()
		flagged = self.confirmed(has_submitted_proof=1, needs_staff_review=1)
		requested = self.confirmed(
			has_submitted_proof=1, staff_cancel_requested=1, staff_cancel_request_reason="Buyer vanished"
		)
		self.confirmed()  # plain confirmed order: not in the queue

		queue = self.as_user(STAFF, api.list_pending_proofs)
		awaiting = [item["proof"]["name"] for item in queue["awaiting_review"]["items"]]
		self.assertEqual(awaiting[-2:], [first_proof, second_proof])
		item = next(i for i in queue["awaiting_review"]["items"] if i["proof"]["name"] == first_proof)
		self.assertEqual(item["order"]["name"], first)
		self.assertEqual(item["proof"]["expected_amount"], 2400)

		self.assertIn(flagged, [i["order"]["name"] for i in queue["needs_staff_review"]["items"]])
		self.assertIn(requested, [i["order"]["name"] for i in queue["staff_cancel_requests"]["items"]])
		self.assertNotIn(requested, [i["order"]["name"] for i in queue["needs_staff_review"]["items"]])
		self.assertEqual(
			queue["counts"],
			{
				"awaiting_review": queue["awaiting_review"]["total"],
				"needs_staff_review": queue["needs_staff_review"]["total"],
				"staff_cancel_requests": queue["staff_cancel_requests"]["total"],
			},
		)

	def test_reviewed_slips_leave_the_queue(self):
		order, proof = self.in_review()
		self.review(proof, "approve")
		queue = self.as_user(STAFF, api.list_pending_proofs)
		self.assertNotIn(proof, [i["proof"]["name"] for i in queue["awaiting_review"]["items"]])

	def test_staff_only(self):
		for user in (SELLER_A, BUYER_A):
			with self.subTest(user), self.assertRaises(frappe.PermissionError):
				self.as_user(user, api.list_pending_proofs)
