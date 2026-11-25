import frappe
from frappe.tests.utils import FrappeTestCase

from accreage_mart.tests.order_fixtures import (
	BUYER_A,
	USERS,
	make_offer,
	make_order,
	make_proof,
	purge_orders,
	setup_world,
)
from accreage_mart.utils import listing as lu
from accreage_mart.utils import order as ou

ORDER_FIELDS = {
	"listing": "Link",
	"seller": "Link",
	"buyer": "Link",
	"source": "Select",
	"auction": "Link",
	"quantity": "Float",
	"unit": "Data",
	"listed_unit_price": "Currency",
	"agreed_unit_price": "Currency",
	"total_price": "Currency",
	"status": "Select",
	"offer_count": "Int",
	"status_deadline": "Datetime",
	"cancel_requested": "Check",
	"cancel_request_reason": "Small Text",
	"staff_cancel_requested": "Check",
	"staff_cancel_request_reason": "Small Text",
	"needs_staff_review": "Check",
	"has_submitted_proof": "Check",
	"cancel_reason": "Small Text",
	"cancelled_by": "Link",
	"confirmed_on": "Datetime",
	"paid_on": "Datetime",
	"staff_payment_approved_by": "Link",
	"staff_payment_approval_reason": "Small Text",
	"delivered_on": "Datetime",
	"completed_on": "Datetime",
	"invoice_no": "Data",
	"invoice_file": "Attach",
}
OFFER_FIELDS = {
	"order": "Link",
	"offered_by": "Link",
	"party": "Select",
	"unit_price": "Currency",
	"offered_on": "Datetime",
	"outcome": "Select",
}
PROOF_FIELDS = {
	"order": "Link",
	"file": "Attach",
	"amount_paid": "Currency",
	"paid_on": "Date",
	"paid_from_bank": "Data",
	"transfer_reference": "Data",
	"note": "Small Text",
	"status": "Select",
	"submitted_on": "Datetime",
	"reviewed_by": "Link",
	"reviewed_on": "Datetime",
	"reject_category": "Select",
	"reject_reason": "Small Text",
}


class TestOrderDoctypeSchema(FrappeTestCase):
	def assert_fields(self, doctype, expected):
		meta = frappe.get_meta(doctype)
		for fieldname, fieldtype in expected.items():
			field = meta.get_field(fieldname)
			self.assertIsNotNone(field, f"{doctype}.{fieldname} missing")
			self.assertEqual(field.fieldtype, fieldtype, f"{doctype}.{fieldname}")

	def test_order_fields(self):
		self.assert_fields("Order", ORDER_FIELDS)
		meta = frappe.get_meta("Order")
		self.assertEqual(meta.autoname, "ORD-.#####")
		self.assertEqual(meta.get_field("status").options.split("\n"), list(ou.STATUSES))
		self.assertEqual(meta.get_field("source").options.split("\n"), ["Direct", "Auction"])

	def test_order_offer_fields(self):
		self.assert_fields("Order Offer", OFFER_FIELDS)
		self.assertEqual(
			frappe.get_meta("Order Offer").get_field("outcome").options.split("\n"),
			["Open", "Accepted", "Countered", "Declined", "Expired"],
		)

	def test_payment_proof_fields(self):
		self.assert_fields("Payment Proof", PROOF_FIELDS)
		meta = frappe.get_meta("Payment Proof")
		self.assertEqual(meta.get_field("status").options.split("\n"), ["Submitted", "Approved", "Rejected"])
		self.assertEqual(
			[o for o in meta.get_field("reject_category").options.split("\n") if o],
			list(ou.REJECT_CATEGORIES),
		)

	def test_seller_profile_bank_fields_are_hidden_from_plain_roles(self):
		meta = frappe.get_meta("Seller Profile")
		for fieldname in ou.BANK_FIELDS:
			field = meta.get_field(fieldname)
			self.assertIsNotNone(field, f"Seller Profile.{fieldname} missing")
			self.assertEqual(field.fieldtype, "Data")
			# permlevel 1 is granted to Admin/Staff only, so a generic resource read by a
			# buyer or seller never returns bank details.
			self.assertEqual(field.permlevel, 1)
		level1_roles = {p.role for p in meta.permissions if p.permlevel == 1}
		self.assertEqual(level1_roles, {"Admin", "Staff"})


class OrderDoctypeTestCase(FrappeTestCase):
	def setUp(self):
		setup_world(self)

	def tearDown(self):
		purge_orders(*USERS)


class TestOrderDoc(OrderDoctypeTestCase):
	def test_named_ord_series(self):
		order = make_order(self.listing, BUYER_A)
		self.assertRegex(order.name, r"^ORD-\d{5,}$")

	def test_written_by_the_system_only(self):
		frappe.set_user(BUYER_A)
		with self.assertRaises(frappe.PermissionError):
			frappe.get_doc(
				{
					"doctype": "Order",
					"listing": self.listing.name,
					"seller": self.listing.seller,
					"buyer": BUYER_A,
					"status": ou.PENDING_CONFIRMATION,
					"quantity": 20,
					"unit": "kg",
				}
			).insert(ignore_permissions=True)

	def test_rejects_non_positive_quantity(self):
		with self.assertRaises(frappe.ValidationError):
			make_order(self.listing, BUYER_A, quantity=0)

	def test_auction_order_must_link_its_auction(self):
		with self.assertRaises(frappe.ValidationError):
			make_order(self.listing, BUYER_A, source=ou.SOURCE_AUCTION)

	def test_price_and_quantity_lock_once_confirmed(self):
		order = make_order(self.listing, BUYER_A, status=ou.CONFIRMED, agreed_unit_price=120)
		with lu.system_write():
			order.quantity = 30
			with self.assertRaises(frappe.ValidationError):
				order.save(ignore_permissions=True)
			order.reload()
			order.agreed_unit_price = 100
			with self.assertRaises(frappe.ValidationError):
				order.save(ignore_permissions=True)

	def test_price_may_change_before_confirmation(self):
		order = make_order(self.listing, BUYER_A, status=ou.NEGOTIATING)
		with lu.system_write():
			order.total_price = 2000
			order.save(ignore_permissions=True)
		self.assertEqual(frappe.db.get_value("Order", order.name, "total_price"), 2000)


class TestOrderOfferDoc(OrderDoctypeTestCase):
	def test_append_only(self):
		order = make_order(self.listing, BUYER_A, status=ou.NEGOTIATING)
		offer = make_offer(order.name, BUYER_A, ou.PARTY_BUYER, 100)
		self.assertTrue(offer.offered_on)
		with lu.system_write():
			offer.unit_price = 90
			with self.assertRaises(frappe.ValidationError):
				offer.save(ignore_permissions=True)

	def test_outcome_moves_once(self):
		order = make_order(self.listing, BUYER_A, status=ou.NEGOTIATING)
		offer = make_offer(order.name, BUYER_A, ou.PARTY_BUYER, 100)
		with lu.system_write():
			offer.outcome = ou.OFFER_COUNTERED
			offer.save(ignore_permissions=True)
			offer.outcome = ou.OFFER_ACCEPTED
			with self.assertRaises(frappe.ValidationError):
				offer.save(ignore_permissions=True)

	def test_written_by_the_system_only_and_never_deleted(self):
		order = make_order(self.listing, BUYER_A, status=ou.NEGOTIATING)
		frappe.set_user(BUYER_A)
		with self.assertRaises(frappe.PermissionError):
			frappe.get_doc(
				{
					"doctype": "Order Offer",
					"order": order.name,
					"offered_by": BUYER_A,
					"party": ou.PARTY_BUYER,
					"unit_price": 90,
				}
			).insert(ignore_permissions=True)
		frappe.set_user("Administrator")
		offer = make_offer(order.name, BUYER_A, ou.PARTY_BUYER, 100)
		with self.assertRaises(frappe.PermissionError):
			frappe.delete_doc("Order Offer", offer.name, ignore_permissions=True)


class TestPaymentProofDoc(OrderDoctypeTestCase):
	def test_editable_while_submitted_then_read_only(self):
		order = make_order(self.listing, BUYER_A, status=ou.PAYMENT_REVIEW)
		proof = make_proof(order.name)
		self.assertEqual(proof.status, ou.PROOF_SUBMITTED)
		self.assertTrue(proof.submitted_on)
		with lu.system_write():
			proof.status = ou.PROOF_REJECTED
			proof.reject_category = ou.REJECT_CATEGORIES[0]
			proof.reject_reason = "Blurry"
			proof.save(ignore_permissions=True)
			proof.reject_reason = "Changed my mind"
			with self.assertRaises(frappe.ValidationError):
				proof.save(ignore_permissions=True)

	def test_never_deleted(self):
		order = make_order(self.listing, BUYER_A, status=ou.PAYMENT_REVIEW)
		proof = make_proof(order.name)
		with self.assertRaises(frappe.PermissionError):
			frappe.delete_doc("Payment Proof", proof.name, ignore_permissions=True)

	def test_written_by_the_system_only(self):
		order = make_order(self.listing, BUYER_A, status=ou.CONFIRMED)
		frappe.set_user(BUYER_A)
		with self.assertRaises(frappe.PermissionError):
			make_proof_unsafe(order.name)


def make_proof_unsafe(order_name: str):
	return frappe.get_doc(
		{
			"doctype": "Payment Proof",
			"order": order_name,
			"file": "/private/files/x.png",
			"amount_paid": 1,
			"paid_on": "2026-11-25",
			"paid_from_bank": "X",
			"transfer_reference": "Y",
		}
	).insert(ignore_permissions=True)
