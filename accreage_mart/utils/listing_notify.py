"""Listing emails. Sending never blocks or fails the action that triggered it."""

import frappe

from accreage_mart.utils import listing as lu
from accreage_mart.utils.email import send_templated_email, smtp_configured

STATUS_TEMPLATES = {
	"Approved": "listing_approved",
	"Rejected": "listing_rejected",
	"Suspended": "listing_suspended",
}


def _send(listing, key: str, extra: dict) -> None:
	try:
		if not smtp_configured():
			return
		seller_user = lu.seller_user_of(listing.seller)
		if not seller_user:
			return
		send_templated_email(
			key=key,
			recipient=seller_user,
			context={
				"full_name": frappe.db.get_value("User", seller_user, "full_name") or "there",
				"title": listing.title,
				**extra,
			},
		)
	except Exception:
		frappe.log_error(title=f"Listing email failed: {key}")


def notify_low_stock(listing) -> None:
	"""Email the seller once, when stock has just crossed below their low-stock level."""
	_send(
		listing,
		"listing_low_stock",
		{
			"quantity": listing.quantity_available,
			"unit": listing.unit,
			"low_stock_level": listing.low_stock_level,
		},
	)


def notify_status_change(listing, action: str, reason: str | None = None) -> None:
	"""Email the seller about a staff decision: ``action`` is Approved, Rejected or Suspended."""
	_send(listing, STATUS_TEMPLATES[action], {"reason": reason or ""})
