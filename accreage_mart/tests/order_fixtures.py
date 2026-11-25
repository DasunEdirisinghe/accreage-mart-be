"""Shared fixtures for the Epic 08 order tests (built on the Epic 04 listing fixtures)."""

import frappe
from frappe.utils import add_to_date, now_datetime

from accreage_mart.tests.listing_fixtures import (
	SELLER_A,
	SELLER_B,
	make_buyer,
	make_category,
	make_listing,
	make_seller,
	purge,
	set_status,
)
from accreage_mart.utils import listing as lu
from accreage_mart.utils import order as ou

BUYER_A = "order.buyer.a@x.lk"
BUYER_B = "order.buyer.b@x.lk"
STAFF = "order.staff@x.lk"
USERS = (SELLER_A, SELLER_B, BUYER_A, BUYER_B, STAFF)
NOTIFY = "accreage_mart.utils.notify"

BANK = {
	"bank_name": "Bank of Ceylon",
	"bank_branch": "Kandy",
	"account_name": "Test Farms",
	"account_number": "0012345678",
}


def make_staff(email: str = STAFF) -> str:
	from accreage_mart.utils.profile import create_platform_user

	create_platform_user(email=email, full_name="Test Staff", role="Staff", status="active")
	return email


def set_bank_details(seller_profile: str, **values) -> None:
	frappe.db.set_value("Seller Profile", seller_profile, {**BANK, **values}, update_modified=False)


def approved_listing(seller: str, category: str, **overrides):
	"""An approved Direct listing: 500 kg at Rs. 120, min order 10 kg unless overridden."""
	values = {"min_order_qty": 10, **overrides}
	listing = make_listing(seller, category, **values)
	set_status(listing.name, lu.APPROVED)
	return frappe.get_doc("Listing", listing.name)


def make_order(listing, buyer: str, **overrides):
	"""An Order inserted directly (no API), for state-machine / permission tests."""
	values = {
		"doctype": "Order",
		"listing": listing.name,
		"seller": listing.seller,
		"buyer": buyer,
		"source": ou.SOURCE_DIRECT,
		"status": ou.PENDING_CONFIRMATION,
		"status_deadline": ou.deadline_for(ou.PENDING_CONFIRMATION),
		"quantity": 20,
		"unit": listing.unit,
		"listed_unit_price": listing.price_per_unit,
		"total_price": 20 * listing.price_per_unit,
	}
	values.update(overrides)
	with lu.system_write():
		return frappe.get_doc(values).insert(ignore_permissions=True)


def make_offer(order_name: str, user: str, party: str, unit_price: float, **overrides):
	values = {
		"doctype": "Order Offer",
		"order": order_name,
		"offered_by": user,
		"party": party,
		"unit_price": unit_price,
		"offered_on": now_datetime(),
		"outcome": ou.OFFER_OPEN,
	}
	values.update(overrides)
	with lu.system_write():
		return frappe.get_doc(values).insert(ignore_permissions=True)


def make_proof(order_name: str, **overrides):
	values = {
		"doctype": "Payment Proof",
		"order": order_name,
		"file": "/private/files/test-slip.png",
		"amount_paid": 2400,
		"paid_on": now_datetime().date(),
		"paid_from_bank": "Sampath Bank",
		"transfer_reference": "TRX-1",
	}
	values.update(overrides)
	with lu.system_write():
		return frappe.get_doc(values).insert(ignore_permissions=True)


def make_confirmed_order(listing, buyer: str, **overrides):
	"""A ``confirmed`` order (price agreed at the listed price, 72 h payment clock running)."""
	values = {
		"status": ou.CONFIRMED,
		"status_deadline": ou.deadline_for(ou.CONFIRMED),
		"agreed_unit_price": listing.price_per_unit,
		"confirmed_on": now_datetime(),
	}
	values.update(overrides)
	return make_order(listing, buyer, **values)


def order_notifications(order_name: str, event: str | None = None) -> list:
	"""Notification Log rows about ``order_name`` (optionally one event type), oldest first."""
	filters = {"document_type": "Order", "document_name": order_name}
	if event:
		filters["custom_event_type"] = event
	return frappe.get_all(
		"Notification Log",
		filters=filters,
		fields=["name", "for_user", "subject", "email_content", "custom_event_type"],
		order_by="creation asc",
	)


def delivery_channels(log_name: str) -> set[str]:
	return {row.channel for row in frappe.get_doc("Notification Log", log_name).custom_delivery_log}


def expire_deadline(order_name: str) -> None:
	"""Move the order's deadline into the past (the scheduled job hasn't run yet)."""
	frappe.db.set_value(
		"Order", order_name, "status_deadline", add_to_date(now_datetime(), minutes=-1), update_modified=False
	)


def purge_orders(*emails: str) -> None:
	"""Remove every order on the test listings (and their offers, proofs and notifications),
	then the listings and users via the listing fixtures' purge."""
	frappe.set_user("Administrator")
	listings = frappe.get_all("Listing", filters={"title": ["like", "ZZ Test%"]}, pluck="name")
	orders = frappe.get_all("Order", filters={"listing": ["in", listings or [""]]}, pluck="name")
	if orders:
		proofs = frappe.get_all("Payment Proof", filters={"order": ["in", orders]}, pluck="name")
		if proofs:
			# Slip files (and their bytes on disk) attached to the test proofs.
			for name in frappe.get_all(
				"File",
				filters={"attached_to_doctype": "Payment Proof", "attached_to_name": ["in", proofs]},
				pluck="name",
			):
				try:
					frappe.delete_doc("File", name, force=True, ignore_permissions=True)
				except Exception:
					frappe.db.delete("File", {"name": name})
			frappe.db.delete(
				"Comment", {"reference_doctype": "Payment Proof", "reference_name": ["in", proofs]}
			)
		frappe.db.delete("Comment", {"reference_doctype": "Order", "reference_name": ["in", orders]})
		frappe.db.delete("Order Offer", {"order": ["in", orders]})
		frappe.db.delete("Payment Proof", {"order": ["in", orders]})
		frappe.db.delete("Notification Log", {"document_type": "Order", "document_name": ["in", orders]})
		frappe.db.delete("Order", {"name": ["in", orders]})
	purge(*emails)


def setup_world(test):
	"""Seller A, an approved listing, two buyers and a staff user."""
	frappe.set_user("Administrator")
	purge_orders(*USERS)
	test.seller = make_seller(SELLER_A)
	test.category = make_category()
	test.buyer_a = make_buyer(BUYER_A)
	test.buyer_b = make_buyer(BUYER_B)
	test.staff = make_staff()
	test.listing = approved_listing(test.seller, test.category)


__all__ = [
	"BANK",
	"BUYER_A",
	"BUYER_B",
	"NOTIFY",
	"SELLER_A",
	"SELLER_B",
	"STAFF",
	"USERS",
	"approved_listing",
	"delivery_channels",
	"expire_deadline",
	"make_confirmed_order",
	"make_offer",
	"make_order",
	"make_proof",
	"make_seller",
	"make_staff",
	"order_notifications",
	"purge_orders",
	"set_bank_details",
	"setup_world",
]
