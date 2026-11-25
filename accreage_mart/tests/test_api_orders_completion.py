"""Story 8.4 - delivery, completion and the order invoice (REQ-9)."""

import io
import re
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import add_to_date, get_datetime, now_datetime

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
	make_seller,
	order_notifications,
	purge_orders,
	set_bank_details,
	setup_world,
)
from accreage_mart.utils import invoice as inv
from accreage_mart.utils import listing as lu
from accreage_mart.utils import order as ou

INVOICE_NO = re.compile(r"^INV-\d{4}-\d{5}$")


def pdf_text(content: bytes) -> str:
	from pypdf import PdfReader

	return "\n".join(page.extract_text() or "" for page in PdfReader(io.BytesIO(content)).pages)


class CompletionTestCase(FrappeTestCase):
	def setUp(self):
		setup_world(self)
		set_bank_details(self.seller)

	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.local.response = frappe._dict()
		purge_orders(*USERS)

	def as_user(self, user, fn, *args, **kwargs):
		frappe.set_user(user)
		try:
			return fn(*args, **kwargs)
		finally:
			frappe.set_user("Administrator")

	def paid(self, **overrides):
		"""20 kg at Rs. 120 = Rs. 2,400, payment approved, waiting for delivery."""
		now = now_datetime()
		values = {
			"status": ou.PAID,
			"status_deadline": None,
			"paid_on": now,
			"has_submitted_proof": 1,
		}
		values.update(overrides)
		return make_confirmed_order(self.listing, BUYER_A, **values).name

	def delivered(self, **overrides):
		now = now_datetime()
		values = {
			"status": ou.DELIVERED,
			"delivered_on": now,
			"status_deadline": ou.deadline_for(ou.DELIVERED, now),
		}
		values.update(overrides)
		return self.paid(**values)

	def completed(self, **overrides):
		order = self.delivered(**overrides)
		self.as_user(BUYER_A, api.confirm_received, order)
		return order

	def order(self, name):
		return frappe.get_doc("Order", name)

	def invoice_files(self, order):
		return frappe.get_all(
			"File",
			filters={"attached_to_doctype": "Order", "attached_to_name": order},
			fields=["name", "file_url", "is_private", "attached_to_field"],
		)

	def download(self, order, user):
		frappe.local.response = frappe._dict()
		self.as_user(user, api.get_invoice, order)
		return frappe.local.response


class TestMarkDelivered(CompletionTestCase):
	def test_the_seller_marks_a_paid_order_delivered_with_a_7_day_deadline(self):
		order = self.paid()
		before = now_datetime()
		result = self.as_user(SELLER_A, api.mark_delivered, order)
		self.assertEqual(result["status"], ou.DELIVERED)
		self.assertTrue(result["delivered_on"])
		deadline = get_datetime(result["status_deadline"])
		self.assertGreaterEqual(deadline, add_to_date(before, days=7, seconds=-5))
		self.assertLessEqual(deadline, add_to_date(now_datetime(), days=7, seconds=5))
		self.assertEqual((result["action_by"], result["on_expiry"]), (ou.BUYER, ou.ON_EXPIRY_AUTO_COMPLETE))

	def test_the_buyer_is_notified_by_email_and_sms(self):
		order = self.paid()
		self.as_user(SELLER_A, api.mark_delivered, order)
		rows = order_notifications(order, "order_delivered")
		self.assertEqual([r.for_user for r in rows], [BUYER_A])
		self.assertIn("within 7 days", rows[0].email_content)
		self.assertEqual(delivery_channels(rows[0].name), {"Email", "SMS"})

	def test_only_the_seller_can_mark_delivered(self):
		order = self.paid()
		for user in (BUYER_A, STAFF):
			with self.assertRaises(frappe.PermissionError):
				self.as_user(user, api.mark_delivered, order)
		make_seller(SELLER_B)
		for user in (BUYER_B, SELLER_B):
			with self.assertRaises(frappe.PermissionError):
				self.as_user(user, api.mark_delivered, order)
		self.assertEqual(self.order(order).status, ou.PAID)

	def test_only_a_paid_order_can_be_marked_delivered(self):
		cases = {
			ou.PENDING_CONFIRMATION: {},
			ou.CONFIRMED: {},
			ou.PAYMENT_REVIEW: {"status_deadline": None},
			ou.CANCELLED: {"status_deadline": None},
		}
		for status, extra in cases.items():
			order = make_confirmed_order(self.listing, BUYER_A, status=status, **extra).name
			with self.assertRaises(frappe.ValidationError, msg=status):
				self.as_user(SELLER_A, api.mark_delivered, order)
			self.assertEqual(self.order(order).status, status)

	def test_marking_delivered_twice_is_refused(self):
		order = self.paid()
		self.as_user(SELLER_A, api.mark_delivered, order)
		with self.assertRaisesRegex(frappe.ValidationError, "already marked delivered"):
			self.as_user(SELLER_A, api.mark_delivered, order)


class TestConfirmReceived(CompletionTestCase):
	def test_the_buyer_completes_a_delivered_order(self):
		order = self.delivered()
		result = self.as_user(BUYER_A, api.confirm_received, order)
		self.assertEqual(result["status"], ou.COMPLETED)
		self.assertTrue(result["completed_on"])
		self.assertIsNone(result["status_deadline"])
		self.assertEqual((result["action_by"], result["on_expiry"]), (ou.NONE, None))
		self.assertRegex(result["invoice_no"], INVOICE_NO)
		self.assertTrue(result["invoice_file"])

	def test_only_the_buyer_can_confirm_receipt(self):
		order = self.delivered()
		make_seller(SELLER_B)
		for user in (SELLER_A, STAFF, BUYER_B, SELLER_B):
			with self.assertRaises(frappe.PermissionError, msg=user):
				self.as_user(user, api.confirm_received, order)
		self.assertEqual(self.order(order).status, ou.DELIVERED)

	def test_only_a_delivered_order_can_be_confirmed(self):
		order = self.paid()
		with self.assertRaises(frappe.ValidationError):
			self.as_user(BUYER_A, api.confirm_received, order)
		self.assertEqual(self.order(order).status, ou.PAID)

	def test_confirming_after_the_7_day_deadline_is_refused(self):
		order = self.delivered()
		expire_deadline(order)
		with self.assertRaisesRegex(frappe.ValidationError, "deadline"):
			self.as_user(BUYER_A, api.confirm_received, order)
		doc = self.order(order)
		self.assertEqual(doc.status, ou.DELIVERED)
		self.assertFalse(doc.invoice_no)

	def test_confirming_twice_is_refused_and_makes_no_second_invoice(self):
		order = self.completed()
		first = self.order(order)
		with self.assertRaisesRegex(frappe.ValidationError, "already completed"):
			self.as_user(BUYER_A, api.confirm_received, order)
		again = self.order(order)
		self.assertEqual((again.invoice_no, again.invoice_file), (first.invoice_no, first.invoice_file))
		self.assertEqual(len(self.invoice_files(order)), 1)

	def test_both_parties_are_notified_with_the_invoice_number(self):
		order = self.completed()
		invoice_no = self.order(order).invoice_no
		rows = order_notifications(order, "order_completed")
		self.assertEqual(sorted(r.for_user for r in rows), sorted([BUYER_A, SELLER_A]))
		for row in rows:
			self.assertIn(invoice_no, row.email_content)
			self.assertIn("confirmed receiving the goods", row.email_content)
			self.assertEqual(delivery_channels(row.name), {"Email", "SMS"})


class TestInvoice(CompletionTestCase):
	def test_the_invoice_is_a_private_pdf_attached_to_the_order_once(self):
		order = self.completed()
		doc = self.order(order)
		[file] = self.invoice_files(order)
		self.assertEqual(file.file_url, doc.invoice_file)
		self.assertTrue(file.file_url.startswith("/private/files/"))
		self.assertEqual((file.is_private, file.attached_to_field), (1, "invoice_file"))
		content = frappe.get_doc("File", file.name).get_content()
		self.assertTrue(content.startswith(b"%PDF-"))
		ou._verify_pdf(content)  # parses, no scripts

		# Any later attempt is a no-op: exactly one invoice per order.
		api.ensure_invoice(order)
		with lu.system_write():
			inv.attach_invoice(self.order(order))
		self.assertEqual(len(self.invoice_files(order)), 1)
		self.assertEqual(self.order(order).invoice_file, doc.invoice_file)

	def test_invoice_numbers_follow_the_inv_year_series(self):
		first = self.order(self.completed()).invoice_no
		second = self.order(self.completed()).invoice_no
		year = str(now_datetime().year)
		for number in (first, second):
			self.assertRegex(number, INVOICE_NO)
			self.assertTrue(number.startswith(f"INV-{year}-"))
		self.assertEqual(int(second.rsplit("-", 1)[1]), int(first.rsplit("-", 1)[1]) + 1)

	def test_the_invoice_shows_the_order_figures_and_no_tax(self):
		order = self.completed()
		doc = self.order(order)
		html = inv.render_invoice_html(doc)
		for expected in (
			doc.invoice_no,
			order,
			"Test Farms",  # seller business
			"Test Buyers Ltd",  # buyer business
			self.listing.title,
			">20<",
			"kg",
			"Rs. 120.00",
			"Rs. 2,400.00",
		):
			self.assertIn(expected, html)
		self.assertNotRegex(html.lower(), r"\btax\b|\bvat\b")

		text = pdf_text(frappe.get_doc("File", {"file_url": doc.invoice_file}).get_content())
		for expected in (doc.invoice_no, order, "Rs. 2,400.00", "Rs. 120.00"):
			self.assertIn(expected, text)

	def test_a_negotiated_order_invoices_the_agreed_price(self):
		order = self.completed(agreed_unit_price=100, total_price=2000)
		text = pdf_text(frappe.get_doc("File", {"file_url": self.order(order).invoice_file}).get_content())
		self.assertIn("Rs. 100.00", text)
		self.assertIn("Rs. 2,000.00", text)
		self.assertNotIn("Rs. 120.00", text)

	def test_the_invoice_cannot_be_changed_once_set(self):
		order = self.order(self.completed())
		order.invoice_no = "INV-1999-00001"
		with lu.system_write(), self.assertRaisesRegex(frappe.ValidationError, "invoice cannot be changed"):
			order.save(ignore_permissions=True)

	def test_a_failed_render_still_completes_and_the_invoice_is_made_on_download(self):
		order = self.delivered()
		with patch.object(inv, "render_invoice_pdf", side_effect=RuntimeError("wkhtmltopdf down")):
			result = self.as_user(BUYER_A, api.confirm_received, order)
		self.assertEqual(result["status"], ou.COMPLETED)
		self.assertRegex(result["invoice_no"], INVOICE_NO)
		self.assertFalse(result["invoice_file"])
		self.assertEqual(self.invoice_files(order), [])

		response = self.download(order, BUYER_A)
		self.assertTrue(response.filecontent.startswith(b"%PDF-"))
		doc = self.order(order)
		self.assertEqual(doc.invoice_no, result["invoice_no"])  # the number never changes
		self.assertEqual(len(self.invoice_files(order)), 1)

	def test_the_system_can_complete_an_order_for_the_8_5_auto_complete(self):
		order = self.delivered()
		doc = api._lock_order(order)
		api.complete_order(doc, ou.SYSTEM)
		doc = self.order(order)
		self.assertEqual(doc.status, ou.COMPLETED)
		self.assertTrue(doc.invoice_file)
		rows = order_notifications(order, "order_completed")
		self.assertEqual(len(rows), 2)
		self.assertIn("completed automatically", rows[0].email_content)

	def test_complete_order_refuses_anything_but_delivered(self):
		order = self.paid()
		with self.assertRaises(frappe.ValidationError):
			api.complete_order(api._lock_order(order), ou.SYSTEM)
		self.assertEqual(self.order(order).status, ou.PAID)


class TestGetInvoice(CompletionTestCase):
	def test_buyer_seller_and_staff_can_download_it(self):
		order = self.completed()
		doc = self.order(order)
		stored = frappe.get_doc("File", {"file_url": doc.invoice_file}).get_content()
		for user in (BUYER_A, SELLER_A, STAFF):
			response = self.download(order, user)
			self.assertEqual(response.type, "download", user)
			self.assertEqual(response.filename, f"{doc.invoice_no}.pdf")
			self.assertEqual(response.content_type, "application/pdf")
			self.assertEqual(response.filecontent, stored)

	def test_nobody_else_can_download_it(self):
		order = self.completed()
		make_seller(SELLER_B)
		for user in (BUYER_B, SELLER_B):
			with self.assertRaises(frappe.PermissionError, msg=user):
				self.download(order, user)
		with self.assertRaises(frappe.AuthenticationError):
			self.download(order, "Guest")

	def test_the_invoice_file_itself_is_private_to_the_parties(self):
		from frappe.core.doctype.file.file import has_permission

		order = self.completed()
		file = frappe.get_doc("File", {"file_url": self.order(order).invoice_file})
		self.assertEqual(file.owner, "Administrator")
		for user, allowed in ((BUYER_A, True), (SELLER_A, True), (STAFF, True), (BUYER_B, False)):
			self.assertEqual(bool(has_permission(file, "read", user=user)), allowed, user)
		# Nobody but the system may change or delete it.
		for user in (BUYER_A, SELLER_A, STAFF):
			for ptype in ("write", "delete"):
				self.assertFalse(has_permission(file, ptype, user=user), f"{user} {ptype}")

	def test_there_is_no_invoice_before_completion(self):
		order = self.delivered()
		with self.assertRaisesRegex(frappe.ValidationError, "once the order is completed"):
			self.download(order, BUYER_A)
		self.assertEqual(self.invoice_files(order), [])


class TestEmailTemplates(FrappeTestCase):
	def test_the_story_templates_are_seeded_and_render(self):
		from accreage_mart.setup.install import ensure_email_templates

		ensure_email_templates()
		delivered = frappe.get_doc("Email Template", "order_delivered").get_formatted_email(
			{"full_name": "Sam", "seller_name": "Green Farms", "order": "ORD-1", "days": 7, "title": "Leeks"}
		)
		self.assertIn("Green Farms", delivered["message"])
		self.assertIn("7 days", delivered["message"])
		completed = frappe.get_doc("Email Template", "order_completed").get_formatted_email(
			{
				"full_name": "Sam",
				"order": "ORD-1",
				"quantity": "20",
				"unit": "kg",
				"title": "Leeks",
				"total": "Rs. 2,400.00",
				"invoice_no": "INV-2026-00001",
				"auto": True,
				"days": 7,
			}
		)
		self.assertIn("INV-2026-00001", completed["subject"])
		self.assertIn("automatically", completed["message"])
		self.assertNotIn("{{", completed["message"])
