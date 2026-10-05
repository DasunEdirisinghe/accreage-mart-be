"""Shared constants, access rules and helpers for the Listing / Auction DocTypes (Epic 04).

Every platform user also carries the ``System Manager`` role (see ``utils/profile.py``), so DocType
permission rows alone cannot keep a seller away from another seller's listing, or from the
status field. The controllers and the ``has_permission`` / ``permission_query_conditions`` hooks in
this module enforce those rules instead. The whitelisted API (Stories 4.2+) wraps its own writes in
:func:`system_write` to set system-managed fields (status, review fields, AI snapshots).
"""

import hashlib
from contextlib import contextmanager

import frappe
from frappe import _
from frappe.utils import add_to_date, flt, get_datetime, now_datetime

DISTRICTS = (
	"Colombo",
	"Gampaha",
	"Kalutara",
	"Kandy",
	"Matale",
	"Nuwara Eliya",
	"Galle",
	"Matara",
	"Hambantota",
	"Jaffna",
	"Kilinochchi",
	"Mannar",
	"Vavuniya",
	"Mullaitivu",
	"Batticaloa",
	"Ampara",
	"Trincomalee",
	"Kurunegala",
	"Puttalam",
	"Anuradhapura",
	"Polonnaruwa",
	"Badulla",
	"Monaragala",
	"Ratnapura",
	"Kegalle",
)
UNITS = ("kg", "nut", "bundle", "piece", "bag", "litre", "dozen")

PENDING = "Pending Approval"
APPROVED = "Approved"
REJECTED = "Rejected"
HIDDEN = "Hidden"
SUSPENDED = "Suspended"
ARCHIVED = "Archived"
LISTING_STATUSES = (PENDING, APPROVED, REJECTED, HIDDEN, SUSPENDED, ARCHIVED)

DIRECT = "Direct"
AUCTION = "Auction"
SELLING_TYPES = (DIRECT, AUCTION)

REVIEW_ACTIONS = ("Approved", "Rejected", "Suspended", "Resubmitted")

TITLE_MAX = 140
MAX_IMAGES = 5
MIN_AUCTION_HOURS = 6
MAX_AUCTION_HOURS = 48
MIN_START_GAP_HOURS = 24

# Once an auction has started in one of these listing statuses, its terms and lot are locked.
STARTED_LOCK_STATUSES = (APPROVED, HIDDEN, SUSPENDED)

# Fields only the system (the API, or Administrator) may write.
LISTING_SYSTEM_FIELDS = (
	"status",
	"status_reason",
	"reviewed_by",
	"reviewed_on",
	"submitted_on",
	"ai_suggested_min",
	"ai_suggested_max",
	"auction",
)
AUCTION_SYSTEM_FIELDS = ("ai_fair_value",)


@contextmanager
def system_write():
	"""Allow the wrapped code to write system-managed fields and review records."""
	previous = frappe.flags.get("listing_system_write")
	frappe.flags.listing_system_write = True
	try:
		yield
	finally:
		frappe.flags.listing_system_write = previous


def can_system_write(user: str | None = None) -> bool:
	return bool(frappe.flags.get("listing_system_write")) or (user or frappe.session.user) == "Administrator"


def is_staff_or_admin(user: str | None = None) -> bool:
	user = user or frappe.session.user
	if user == "Administrator":
		return True
	return bool({"Staff", "Admin"} & set(frappe.get_roles(user)))


def is_admin(user: str | None = None) -> bool:
	user = user or frappe.session.user
	return user == "Administrator" or "Admin" in frappe.get_roles(user)


def seller_profile_of(user: str | None = None) -> str | None:
	"""Seller Profile name (the user's email) for ``user``, or None."""
	return frappe.db.get_value("Seller Profile", {"user": user or frappe.session.user})


def require_seller() -> str:
	"""Seller Profile name of the caller, who must be a verified seller."""
	user = frappe.session.user
	if not user or user == "Guest":
		frappe.throw(_("Not authenticated."), frappe.AuthenticationError)
	profile = seller_profile_of(user)
	if not profile or not frappe.db.get_value("Seller Profile", profile, "verified"):
		frappe.throw(_("Only verified sellers can manage listings."), frappe.PermissionError)
	return profile


def seller_user_of(seller_profile: str | None) -> str | None:
	if not seller_profile:
		return None
	return frappe.db.get_value("Seller Profile", seller_profile, "user")


def owns_listing(listing_seller: str | None, user: str | None = None) -> bool:
	user = user or frappe.session.user
	return bool(listing_seller) and seller_user_of(listing_seller) == user


# Flipped on by the auction epic, which adds bidding.
BIDDING_ENABLED = False


def public_seller_id(seller_profile: str) -> str:
	"""Opaque public id for a seller. The Seller Profile name is the user's email, which must
	never appear in a public response or URL."""
	digest = hashlib.sha256(f"{frappe.local.site}:{seller_profile}".encode()).hexdigest()
	return digest[:16]


def seller_profile_from_public_id(public_id: str) -> str | None:
	if not public_id:
		return None
	for profile in frappe.get_all("Seller Profile", pluck="name"):
		if public_seller_id(profile) == public_id:
			return profile
	return None


def stock_state(quantity, low_stock_level) -> str:
	if flt(quantity) <= 0:
		return "out_of_stock"
	if flt(low_stock_level) > 0 and flt(quantity) < flt(low_stock_level):
		return "low_stock"
	return "in_stock"


def viewer_has_order(listing_name: str, user: str) -> bool:
	"""Whether ``user`` has an order (placed or in progress, or completed) on the listing. Such a
	buyer may still open a hidden / suspended / archived listing. Wired by the orders epic."""
	return False


def google_maps_url(latitude, longitude) -> str | None:
	if not flt(latitude) or not flt(longitude):
		return None
	return f"https://www.google.com/maps?q={flt(latitude)},{flt(longitude)}"


def derive_auction_status(listing_status: str, start_time, end_time, now=None) -> str:
	"""Auction status is never stored: it follows the listing status and the clock.

	``pending`` / ``rejected`` mirror the listing; an approved listing is ``scheduled`` before the
	start, ``live`` between start and end, ``ended`` after. Hidden / suspended / archived listings
	report that listing status in lower case.
	"""
	if listing_status == PENDING:
		return "pending"
	if listing_status == REJECTED:
		return "rejected"
	if listing_status == APPROVED:
		now = get_datetime(now) if now else now_datetime()
		if now < get_datetime(start_time):
			return "scheduled"
		if now < get_datetime(end_time):
			return "live"
		return "ended"
	return listing_status.lower()


def auction_has_started(start_time, now=None) -> bool:
	now = get_datetime(now) if now else now_datetime()
	return now >= get_datetime(start_time)


def auction_is_live(start_time, end_time, now=None) -> bool:
	"""Running right now: started and not yet ended."""
	now = get_datetime(now) if now else now_datetime()
	return get_datetime(start_time) <= now < get_datetime(end_time)


def auction_has_bids(auction_name: str) -> bool:
	"""Bids arrive with the auction epic; until then no auction has any."""
	return False


def count_active_orders(listing_name: str) -> int:
	"""Orders placed or in progress against a listing. Wired up by the orders epic."""
	return 0


HIDE_ARCHIVE_WARNING = (
	"This listing will no longer appear on the marketplace. Buyers who have already ordered it, "
	"or have an order in progress, can still see it through their order, and those orders must "
	"still be completed."
)


def validate_start_gap(start_time, reference=None) -> None:
	"""Start must be at least MIN_START_GAP_HOURS after ``reference`` (default: now)."""
	reference = get_datetime(reference) if reference else now_datetime()
	earliest = add_to_date(reference, hours=MIN_START_GAP_HOURS)
	if get_datetime(start_time) < earliest:
		frappe.throw(
			_("The auction must start at least {0} hours after you submit it for review.").format(
				MIN_START_GAP_HOURS
			)
		)


def validate_auction_window(start_time, end_time) -> float:
	"""Return the duration in hours; throw unless MIN <= duration <= MAX and end is after start."""
	start, end = get_datetime(start_time), get_datetime(end_time)
	if end <= start:
		frappe.throw(_("The auction end time must be after its start time."))
	hours = (end - start).total_seconds() / 3600
	if hours < MIN_AUCTION_HOURS:
		frappe.throw(_("An auction must run for at least {0} hours.").format(MIN_AUCTION_HOURS))
	if hours > MAX_AUCTION_HOURS:
		frappe.throw(_("An auction can run for at most {0} hours (2 days).").format(MAX_AUCTION_HOURS))
	return round(hours, 2)


# -- access hooks (see hooks.py) ----------------------------------------------------------------


def listing_has_permission(doc, ptype=None, user=None, debug=False):
	"""Deny anyone who is not the owning seller or staff/admin. None = no objection."""
	user = user or frappe.session.user
	if is_staff_or_admin(user):
		return None
	if ptype == "create":
		return None if "Seller" in frappe.get_roles(user) else False
	return None if owns_listing(doc.get("seller"), user) else False


def auction_has_permission(doc, ptype=None, user=None, debug=False):
	user = user or frappe.session.user
	if is_staff_or_admin(user):
		return None
	if ptype == "create":
		return None if "Seller" in frappe.get_roles(user) else False
	seller = frappe.db.get_value("Listing", {"auction": doc.get("name")}, "seller")
	return None if owns_listing(seller, user) else False


def review_has_permission(doc, ptype=None, user=None, debug=False):
	"""Reviews are written by the system only; the listing's owner and staff may read them."""
	user = user or frappe.session.user
	if ptype in ("create", "write", "delete"):
		return None if can_system_write(user) else False
	if is_staff_or_admin(user):
		return None
	seller = frappe.db.get_value("Listing", doc.get("listing"), "seller")
	return None if owns_listing(seller, user) else False


def listing_query_conditions(user=None, doctype=None):
	user = user or frappe.session.user
	if is_staff_or_admin(user):
		return ""
	profile = seller_profile_of(user)
	if not profile:
		return "1=0"
	return f"`tabListing`.`seller` = {frappe.db.escape(profile)}"


def auction_query_conditions(user=None, doctype=None):
	user = user or frappe.session.user
	if is_staff_or_admin(user):
		return ""
	profile = seller_profile_of(user)
	if not profile:
		return "1=0"
	return (
		"`tabAuction`.`name` in "
		f"(select `auction` from `tabListing` where `seller` = {frappe.db.escape(profile)})"
	)


def review_query_conditions(user=None, doctype=None):
	user = user or frappe.session.user
	if is_staff_or_admin(user):
		return ""
	profile = seller_profile_of(user)
	if not profile:
		return "1=0"
	return (
		"`tabListing Review`.`listing` in "
		f"(select `name` from `tabListing` where `seller` = {frappe.db.escape(profile)})"
	)
