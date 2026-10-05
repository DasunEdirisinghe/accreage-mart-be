"""Whitelisted seller write endpoints for listings (Epic 04, Story 4.2).

Referenced from the frontend as ``accreage_mart.api.listings.<fn>``.

Guard matrix
------------
=========================  ================  ===================================================
Endpoint                   Caller            Notes
=========================  ================  ===================================================
create_listing             verified seller   listing -> auction -> link in one savepoint; always
                                              starts "Pending Approval"
update_listing             owner             Direct edits need no re-review; changing auction
                                              terms of a published listing sends it back to
                                              Pending; terms/lot locked once the auction starts
get_listing_action_info    owner             can hide / archive? plus the warning text
hide_listing               owner             Approved -> Hidden (needs acknowledgement)
unhide_listing             owner             Hidden -> Approved
archive_listing            owner             anything but Suspended/Archived -> Archived; this is
                                              the seller's "Delete" (needs acknowledgement)
resubmit_listing           owner             Rejected -> Pending Approval, with a note
duplicate_listing          owner             new listing from an Archived one
update_stock               owner             Direct only; one low-stock email when it crosses
=========================  ================  ===================================================

Sellers never delete listings and never set status directly: every status change here runs inside
``system_write`` after the ownership and state checks. Review decisions (approve / reject /
suspend) are Story 4.4.
"""

from contextlib import nullcontext

import frappe
from frappe import _
from frappe.utils import cint, flt, get_datetime, now_datetime

from accreage_mart.utils import listing as lu
from accreage_mart.utils.listing_images import check_image_rows
from accreage_mart.utils.listing_notify import notify_low_stock
from accreage_mart.utils.listing_snapshot import suggestion_snapshot

DIRECT_ONLY_FIELDS = ("price_per_unit", "min_order_qty", "low_stock_level")
CONTENT_FIELDS = (
	"title",
	"description",
	"category",
	"unit",
	"quantity_available",
	"district",
	"location",
	"latitude",
	"longitude",
	"organic",
	"certification",
	*DIRECT_ONLY_FIELDS,
)
AUCTION_FIELDS = ("min_bid", "start_time", "end_time")
EDITABLE_FIELDS = (*CONTENT_FIELDS, *AUCTION_FIELDS, "images")

AUCTION_TERMS_CHANGED_NOTE = "Auction terms were changed after approval; sent back for review."


# -- guards and helpers -------------------------------------------------------------------------


def _owned_listing(name: str, profile: str):
	# Same answer for "missing" and "someone else's" so listing names can't be probed.
	if not name or frappe.db.get_value("Listing", name, "seller") != profile:
		frappe.throw(_("Listing not found."), frappe.DoesNotExistError)
	return frappe.get_doc("Listing", name)


def _auction_of(listing):
	return frappe.get_doc("Auction", listing.auction) if listing.auction else None


def _as_dict(value) -> dict:
	value = frappe.parse_json(value) if isinstance(value, str) else value
	return dict(value or {})


def _image_rows(images) -> list[dict]:
	rows = []
	for item in frappe.parse_json(images) if isinstance(images, str) else (images or []):
		if isinstance(item, str):
			item = {"image": item}
		rows.append({"image": item.get("image"), "is_cover": cint(item.get("is_cover"))})
	return rows


def _view(listing) -> dict:
	auction = _auction_of(listing)
	return {
		"name": listing.name,
		"status": listing.status,
		"selling_type": listing.selling_type,
		"quantity_available": listing.quantity_available,
		"submitted_on": listing.submitted_on,
		"auction": None
		if not auction
		else {
			"name": auction.name,
			"min_bid": auction.min_bid,
			"start_time": auction.start_time,
			"end_time": auction.end_time,
			"duration_hours": auction.duration_hours,
			"status": lu.derive_auction_status(listing.status, auction.start_time, auction.end_time),
		},
	}


def _stamp_submission(listing, auction=None) -> None:
	"""Record the submission time and refresh the price snapshot (call inside system_write)."""
	listing.submitted_on = now_datetime()
	snapshot = suggestion_snapshot(listing.category, auction.start_time if auction else None)
	listing.ai_suggested_min = snapshot.get("min") or 0
	listing.ai_suggested_max = snapshot.get("max") or 0
	if auction:
		auction.ai_fair_value = snapshot.get("fair_value") or 0


def _log_resubmission(listing_name: str, note: str | None) -> None:
	with lu.system_write():
		frappe.get_doc(
			{
				"doctype": "Listing Review",
				"listing": listing_name,
				"action": "Resubmitted",
				"seller_note": note,
				"reviewer": frappe.session.user,
			}
		).insert(ignore_permissions=True)


def _require_acknowledged(acknowledged) -> None:
	if not cint(acknowledged):
		frappe.throw(_("Please confirm that you have read the warning."))


def _maybe_notify_low_stock(listing, old_quantity: float) -> None:
	level = flt(listing.low_stock_level)
	if level > 0 and flt(old_quantity) >= level > flt(listing.quantity_available):
		notify_low_stock(listing)


def _block_reason(listing, action: str) -> str | None:
	"""Why ``action`` ("hide" | "archive" | "unhide") is not possible right now, or None."""
	status = listing.status
	if action == "hide" and status != lu.APPROVED:
		return _("Only a published listing can be hidden.")
	if action == "unhide" and status != lu.HIDDEN:
		return _("Only a hidden listing can be shown again.")
	if action == "archive" and status in (lu.ARCHIVED, lu.SUSPENDED):
		return (
			_("This listing is already archived.")
			if status == lu.ARCHIVED
			else _("This listing is suspended. Please contact staff.")
		)

	if listing.selling_type == lu.AUCTION and listing.auction:
		auction = _auction_of(listing)
		if action == "unhide":
			if lu.auction_has_started(auction.start_time):
				return _("The start time of this auction has passed. Archive it and create a new one.")
		elif status in (lu.APPROVED, lu.HIDDEN) and lu.auction_is_live(auction.start_time, auction.end_time):
			return _(
				"This auction has started and cannot be stopped from here. Please contact staff."
			)
	return None


def _set_status(listing, status: str) -> None:
	with lu.system_write():
		listing.status = status
		listing.save()


# -- create ---------------------------------------------------------------------------------------


def _create(profile: str, values: dict, auction_terms: dict, acknowledged) -> dict:
	selling_type = values.get("selling_type")
	if selling_type == lu.AUCTION:
		_require_acknowledged_auction(acknowledged)
		missing = [field for field in AUCTION_FIELDS if not auction_terms.get(field)]
		if missing:
			frappe.throw(_("Auction listings need: {0}.").format(", ".join(missing)))
	elif any(auction_terms.values()):
		frappe.throw(_("Auction terms are only for Auction listings."))

	check_image_rows(values.get("images") or [], profile)

	savepoint = "create_listing"
	frappe.db.savepoint(savepoint)
	try:
		listing = frappe.get_doc({"doctype": "Listing", "seller": profile, **values}).insert()
		auction = None
		if selling_type == lu.AUCTION:
			with lu.system_write():
				auction = frappe.get_doc({"doctype": "Auction", **auction_terms}).insert(
					ignore_permissions=True
				)
		with lu.system_write():
			_stamp_submission(listing, auction)
			if auction:
				# Ownership is already checked; the auction has no listing link yet, so skip the
				# link-based permission lookup for this one internal write.
				auction.save(ignore_permissions=True)
				listing.auction = auction.name
			listing.save()
	except Exception:
		frappe.db.rollback(save_point=savepoint)
		raise
	return _view(listing)


def _require_acknowledged_auction(acknowledged) -> None:
	if not cint(acknowledged):
		frappe.throw(
			_(
				"Please confirm that once an auction has started it cannot be stopped without "
				"contacting staff, and that with any bids placed it cannot be stopped at all."
			)
		)


@frappe.whitelist(methods=["POST"])
def create_listing(
	category: str,
	title: str,
	description: str,
	selling_type: str,
	unit: str,
	quantity_available: float,
	district: str,
	location: str,
	images,
	price_per_unit: float | None = None,
	min_order_qty: float | None = None,
	low_stock_level: float | None = None,
	latitude: float | None = None,
	longitude: float | None = None,
	organic: int = 0,
	certification: str | None = None,
	min_bid: float | None = None,
	start_time: str | None = None,
	end_time: str | None = None,
	auction_terms_acknowledged: int = 0,
) -> dict:
	profile = lu.require_seller()
	values = {
		"category": category,
		"title": title,
		"description": description,
		"selling_type": selling_type,
		"unit": unit,
		"quantity_available": quantity_available,
		"district": district,
		"location": location,
		"images": _image_rows(images),
		"price_per_unit": price_per_unit,
		"min_order_qty": min_order_qty,
		"low_stock_level": low_stock_level,
		"latitude": latitude,
		"longitude": longitude,
		"organic": cint(organic),
		"certification": certification,
	}
	terms = {"min_bid": min_bid, "start_time": start_time, "end_time": end_time}
	return _create(profile, values, terms, auction_terms_acknowledged)


# -- update ---------------------------------------------------------------------------------------


def _terms_changed(auction, values: dict) -> bool:
	if "min_bid" in values and flt(values["min_bid"]) != flt(auction.min_bid):
		return True
	for field in ("start_time", "end_time"):
		if field in values and get_datetime(values[field]) != get_datetime(auction.get(field)):
			return True
	return False


@frappe.whitelist(methods=["POST"])
def update_listing(name: str, values) -> dict:
	profile = lu.require_seller()
	listing = _owned_listing(name, profile)
	values = _as_dict(values)

	unknown = sorted(set(values) - set(EDITABLE_FIELDS))
	if unknown:
		frappe.throw(_("These fields cannot be changed: {0}.").format(", ".join(unknown)))
	if listing.status == lu.ARCHIVED:
		frappe.throw(_("Archived listings cannot be edited. Duplicate it instead."))
	if listing.status == lu.SUSPENDED:
		frappe.throw(_("This listing is suspended. Please contact staff."))

	is_auction = listing.selling_type == lu.AUCTION
	auction = _auction_of(listing)
	if is_auction:
		wrong = [field for field in DIRECT_ONLY_FIELDS if field in values]
		if wrong:
			frappe.throw(_("Auction listings do not have: {0}.").format(", ".join(wrong)))
	elif any(field in values for field in AUCTION_FIELDS):
		frappe.throw(_("Auction terms are only for Auction listings."))

	terms_changed = bool(auction) and _terms_changed(auction, values)
	published = listing.status in (lu.APPROVED, lu.HIDDEN)
	if terms_changed and published and lu.auction_has_started(auction.start_time):
		frappe.throw(
			_("The auction terms cannot be changed once the auction has started. Please contact staff.")
		)

	old_quantity = flt(listing.quantity_available)
	for field in CONTENT_FIELDS:
		if field in values:
			listing.set(field, values[field])
	if "images" in values:
		rows = _image_rows(values["images"])
		check_image_rows(rows, profile, listing)
		listing.set("images", [])
		for row in rows:
			listing.append("images", row)

	# New terms on a Pending/published listing go (back) to review; on a Rejected one they wait for
	# the seller's resubmission. Re-stamping writes system fields, hence system_write.
	restamp = terms_changed and listing.status != lu.REJECTED
	sent_back = terms_changed and published

	savepoint = "update_listing"
	frappe.db.savepoint(savepoint)
	try:
		with lu.system_write() if restamp else nullcontext():
			if terms_changed:
				if restamp:
					lu.validate_start_gap(values.get("start_time", auction.start_time))
				for field in AUCTION_FIELDS:
					if field in values:
						auction.set(field, values[field])
				if sent_back:
					listing.status = lu.PENDING
				if restamp:
					_stamp_submission(listing, auction)
				auction.save()
			listing.save()
		if sent_back:
			_log_resubmission(listing.name, AUCTION_TERMS_CHANGED_NOTE)
	except Exception:
		frappe.db.rollback(save_point=savepoint)
		raise

	_maybe_notify_low_stock(listing, old_quantity)
	return _view(listing)


# -- hide / unhide / archive ----------------------------------------------------------------------


@frappe.whitelist()
def get_listing_action_info(name: str) -> dict:
	"""What the seller UI needs before showing the hide / archive dialogs."""
	listing = _owned_listing(name, lu.require_seller())
	return {
		"hide": {"blocked_reason": _block_reason(listing, "hide")},
		"archive": {"blocked_reason": _block_reason(listing, "archive")},
		"unhide": {"blocked_reason": _block_reason(listing, "unhide")},
		"warning": lu.HIDE_ARCHIVE_WARNING,
		"active_order_count": lu.count_active_orders(listing.name),
	}


def _change_visibility(name: str, action: str, status: str, acknowledged=None) -> dict:
	listing = _owned_listing(name, lu.require_seller())
	reason = _block_reason(listing, action)
	if reason:
		frappe.throw(reason)
	if action in ("hide", "archive"):
		_require_acknowledged(acknowledged)
	_set_status(listing, status)
	return _view(listing)


@frappe.whitelist(methods=["POST"])
def hide_listing(name: str, acknowledged: int = 0) -> dict:
	return _change_visibility(name, "hide", lu.HIDDEN, acknowledged)


@frappe.whitelist(methods=["POST"])
def unhide_listing(name: str) -> dict:
	return _change_visibility(name, "unhide", lu.APPROVED)


@frappe.whitelist(methods=["POST"])
def archive_listing(name: str, acknowledged: int = 0) -> dict:
	return _change_visibility(name, "archive", lu.ARCHIVED, acknowledged)


# -- resubmit / duplicate -------------------------------------------------------------------------


@frappe.whitelist(methods=["POST"])
def resubmit_listing(name: str, note: str | None = None) -> dict:
	listing = _owned_listing(name, lu.require_seller())
	if listing.status != lu.REJECTED:
		frappe.throw(_("Only a rejected listing can be resubmitted."))

	auction = _auction_of(listing)
	if auction:
		lu.validate_start_gap(auction.start_time)

	savepoint = "resubmit_listing"
	frappe.db.savepoint(savepoint)
	try:
		with lu.system_write():
			listing.status = lu.PENDING
			listing.resubmission_note = (note or "").strip() or None
			_stamp_submission(listing, auction)
			if auction:
				auction.save()
			listing.save()
		_log_resubmission(listing.name, listing.resubmission_note)
	except Exception:
		frappe.db.rollback(save_point=savepoint)
		raise
	return _view(listing)


@frappe.whitelist(methods=["POST"])
def duplicate_listing(
	name: str,
	auction_terms_acknowledged: int = 0,
	min_bid: float | None = None,
	start_time: str | None = None,
	end_time: str | None = None,
) -> dict:
	"""A fresh Pending listing copied from an archived one. An auction needs new start/end times."""
	profile = lu.require_seller()
	source = _owned_listing(name, profile)
	if source.status != lu.ARCHIVED:
		frappe.throw(_("Only an archived listing can be duplicated."))

	values = {field: source.get(field) for field in CONTENT_FIELDS}
	values["selling_type"] = source.selling_type
	values["images"] = [{"image": row.image, "is_cover": row.is_cover} for row in source.images]

	terms = {"min_bid": None, "start_time": None, "end_time": None}
	if source.selling_type == lu.AUCTION:
		old = _auction_of(source)
		terms = {"min_bid": min_bid or old.min_bid, "start_time": start_time, "end_time": end_time}
	return _create(profile, values, terms, auction_terms_acknowledged)


# -- stock ----------------------------------------------------------------------------------------


@frappe.whitelist(methods=["POST"])
def update_stock(name: str, quantity_available: float) -> dict:
	listing = _owned_listing(name, lu.require_seller())
	if listing.selling_type == lu.AUCTION:
		frappe.throw(_("The quantity of an auction lot is fixed."))
	if listing.status in (lu.ARCHIVED, lu.SUSPENDED):
		frappe.throw(_("This listing's stock cannot be changed right now."))
	if flt(quantity_available) < 0:
		frappe.throw(_("Quantity cannot be negative."))

	old_quantity = flt(listing.quantity_available)
	listing.quantity_available = flt(quantity_available)
	listing.save()
	_maybe_notify_low_stock(listing, old_quantity)
	return _view(listing)
