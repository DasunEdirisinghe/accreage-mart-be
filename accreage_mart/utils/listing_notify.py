"""Listing notifications. Sending never blocks or fails the action that triggered it."""

import frappe

from accreage_mart.utils import listing as lu
from accreage_mart.utils.credentials import frontend_url
from accreage_mart.utils.notify import notify

STATUS_TEMPLATES = {
	"Approved": "listing_approved",
	"Rejected": "listing_rejected",
	"Suspended": "listing_suspended",
}

STATUS_TITLES = {
	"Approved": "Your listing is now live",
	"Rejected": "Your listing needs changes",
	"Suspended": "Your listing has been suspended",
}


def _notify(listing, event: str, title: str, message: str, extra: dict) -> None:
	try:
		seller_user = lu.seller_user_of(listing.seller)
		if not seller_user:
			return
		notify(
			event,
			seller_user,
			title,
			message,
			["email"],
			document_type="Listing",
			document_name=listing.name,
			email_template=event,
			email_context={
				"full_name": frappe.db.get_value("User", seller_user, "full_name") or "there",
				"title": listing.title,
				**extra,
			},
			email_cta_label="View listing",
			email_cta_url=f"{frontend_url()}/marketplace/{listing.name}",
		)
	except Exception:
		frappe.log_error(title=f"Listing notification failed: {event}")


def notify_low_stock(listing) -> None:
	"""Notify the seller once, when stock has just crossed below their low-stock level."""
	_notify(
		listing,
		"listing_low_stock",
		"Low stock on one of your listings",
		f"{listing.title} is running low: {listing.quantity_available} {listing.unit} left, "
		f"below your alert level of {listing.low_stock_level} {listing.unit}.",
		{
			"quantity": listing.quantity_available,
			"unit": listing.unit,
			"low_stock_level": listing.low_stock_level,
		},
	)


def notify_status_change(listing, action: str, reason: str | None = None) -> None:
	"""Notify the seller about a staff decision: ``action`` is Approved, Rejected or Suspended."""
	event = STATUS_TEMPLATES[action]
	message = f"{listing.title} has been {action.lower()}."
	if reason:
		message += f" Reason: {reason}"
	_notify(listing, event, STATUS_TITLES[action], message, {"reason": reason or ""})
