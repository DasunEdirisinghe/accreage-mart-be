"""Whitelisted staff review endpoints for listings (Epic 04, Story 4.4).

Referenced from the frontend as ``accreage_mart.api.listing_review.<fn>``.

Guard matrix
------------
=========================  ================  =====================================================
Endpoint                   Caller            Notes
=========================  ================  =====================================================
list_listings_for_review   Staff / Admin     the shared queue (Direct and Auction together); one
                                              status at a time, Pending Approval by default
get_listing_for_review     Staff / Admin     the whole listing as buyers will see it, plus review
                                              context and which actions are possible
approve_listing            Staff / Admin     Pending Approval (or Suspended) -> Approved
reject_listing             Staff / Admin     Pending Approval -> Rejected, reason required
suspend_listing            Staff / Admin     Approved / Hidden -> Suspended, reason required
=========================  ================  =====================================================

Every decision names the version staff looked at (``expected_modified``); if the seller changed
the listing since, the decision is refused and staff reload. Each decision writes an immutable
Listing Review row and emails the seller (never blocking the decision).
"""

import frappe
from frappe import _
from frappe.utils import get_datetime, now_datetime

from accreage_mart.api import marketplace as mk
from accreage_mart.utils import listing as lu
from accreage_mart.utils.listing_notify import notify_status_change
from accreage_mart.utils.profile import get_account_status
from accreage_mart.utils.registration import require_staff

MAX_EDIT_HISTORY = 30
MAX_VALUE_LENGTH = 200


# -- helpers --------------------------------------------------------------------------------------


def _get(name: str):
	if not name or not frappe.db.exists("Listing", name):
		frappe.throw(_("Listing not found."), frappe.DoesNotExistError)
	return frappe.get_doc("Listing", name)


def _auction_of(listing):
	return frappe.get_doc("Auction", listing.auction) if listing.auction else None


def _action_state(listing) -> dict:
	"""What staff can do to ``listing`` right now, each with the reason when they can't."""
	auction = _auction_of(listing)
	started = bool(auction) and lu.auction_has_started(auction.start_time)

	approve = None
	if listing.status not in (lu.PENDING, lu.SUSPENDED):
		approve = _("Only a pending or suspended listing can be approved.")
	elif started:
		approve = _("The auction's start time has already passed. Ask the seller to reschedule it.")

	reject = None if listing.status == lu.PENDING else _("Only a pending listing can be rejected.")

	suspend = None
	if listing.status not in (lu.APPROVED, lu.HIDDEN):
		suspend = _("Only a published or hidden listing can be suspended.")
	elif started and lu.auction_has_bids(auction.name):
		suspend = _("This auction has bids and cannot be stopped.")

	return {
		name: {"allowed": reason is None, "blocked_reason": reason}
		for name, reason in (("approve", approve), ("reject", reject), ("suspend", suspend))
	}


def _short(value) -> str | None:
	if value is None:
		return None
	text = str(value)
	return text if len(text) <= MAX_VALUE_LENGTH else text[: MAX_VALUE_LENGTH - 1] + "…"


def _edit_history(listing_name: str) -> list[dict]:
	meta = frappe.get_meta("Listing")
	history = []
	for version in frappe.get_all(
		"Version",
		filters={"ref_doctype": "Listing", "docname": listing_name},
		fields=["owner", "creation", "data"],
		order_by="creation desc",
		limit=MAX_EDIT_HISTORY,
		ignore_permissions=True,
	):
		data = frappe.parse_json(version.data) if version.data else {}
		changes = [
			{"field": meta.get_label(field) or field, "old": _short(old), "new": _short(new)}
			for field, old, new in data.get("changed", [])
		]
		table_changes = sum(len(data.get(key) or []) for key in ("added", "removed", "row_changed"))
		if changes or table_changes:
			history.append(
				{
					"user": version.owner,
					"at": version.creation,
					"changes": changes,
					"table_rows_changed": table_changes,
				}
			)
	return history


def _review_history(listing_name: str) -> list[dict]:
	rows = frappe.get_all(
		"Listing Review",
		filters={"listing": listing_name},
		fields=["name", "action", "reason", "seller_note", "reviewer", "reviewed_on"],
		order_by="reviewed_on desc, creation desc",
		ignore_permissions=True,
	)
	for row in rows:
		row["reviewer_name"] = frappe.db.get_value("User", row.reviewer, "full_name") or row.reviewer
	return rows


def _seller_context(listing) -> dict:
	profile = frappe.db.get_value(
		"Seller Profile",
		listing.seller,
		["user", "business_name", "district", "verified", "trust_score", "total_sales", "creation"],
		as_dict=True,
	)
	user = frappe.db.get_value("User", profile.user, ["custom_account_status", "enabled"], as_dict=True)
	return {
		"email": profile.user,
		"business_name": profile.business_name,
		"district": profile.district,
		"verified": bool(profile.verified),
		"trust_score": profile.trust_score,
		"total_sales": profile.total_sales,
		"member_since": profile.creation,
		"account_status": get_account_status(user),
	}


def _require_current(listing, expected_modified: str | None) -> None:
	if not expected_modified or get_datetime(expected_modified) != get_datetime(listing.modified):
		frappe.throw(
			_("This listing changed after you opened it. Reload it and review the latest version."),
			frappe.TimestampMismatchError,
		)


def _require_reason(reason: str | None) -> str:
	reason = (reason or "").strip()
	if not reason:
		frappe.throw(_("A reason is required."))
	return reason


def _decide(listing, action: str, status: str, reason: str | None) -> dict:
	"""Apply a decision: status, reviewer fields and a history row, all or nothing."""
	reviewer = frappe.session.user
	savepoint = "listing_review"
	frappe.db.savepoint(savepoint)
	try:
		with lu.system_write():
			listing.status = status
			listing.status_reason = reason if action in ("Rejected", "Suspended") else None
			listing.reviewed_by = reviewer
			listing.reviewed_on = now_datetime()
			listing.save(ignore_permissions=True)
			frappe.get_doc(
				{
					"doctype": "Listing Review",
					"listing": listing.name,
					"action": action,
					"reason": reason,
					"reviewer": reviewer,
				}
			).insert(ignore_permissions=True)
	except Exception:
		frappe.db.rollback(save_point=savepoint)
		raise

	notify_status_change(listing, action, reason)
	return {"name": listing.name, "status": listing.status, "reviewed_on": listing.reviewed_on}


# -- the queue ------------------------------------------------------------------------------------


@frappe.whitelist()
def list_listings_for_review(
	status: str = lu.PENDING,
	search: str | None = None,
	page: int = 1,
	page_size: int = mk.DEFAULT_PAGE_SIZE,
) -> dict:
	"""Listings of one status, both types together. Pending ones are oldest first."""
	require_staff()
	if status not in lu.LISTING_STATUSES:
		frappe.throw(_("Unknown status."))
	page, page_size = mk.page_args(page, page_size)

	where = ["l.status = %(status)s"]
	params: dict = {"status": status}
	if search and search.strip():
		where.append("(l.title like %(search)s or l.description like %(search)s)")
		params["search"] = mk.like_pattern(search.strip())
	condition = " and ".join(f"({clause})" for clause in where)
	order = "l.submitted_on asc, l.name asc" if status == lu.PENDING else "l.modified desc, l.name desc"

	total = frappe.db.sql(f"select count(*) {mk.LISTING_FROM} where {condition}", params)[0][0]
	rows = frappe.db.sql(
		f"""select {mk.LISTING_COLUMNS},
			(select count(*) from `tabListing Review` r
				where r.listing = l.name and r.action = 'Rejected') as previous_rejections,
			(select count(*) from `tabListing Review` r
				where r.listing = l.name and r.action = 'Resubmitted') as resubmissions,
			l.submitted_on
		{mk.LISTING_FROM} where {condition} order by {order} limit %(limit)s offset %(offset)s""",
		{**params, "limit": page_size, "offset": (page - 1) * page_size},
		as_dict=True,
	)

	counts = {value: 0 for value in lu.LISTING_STATUSES}
	for row_status, count in frappe.db.sql("select status, count(*) from `tabListing` group by status"):
		counts[row_status] = count

	now = now_datetime()
	items = []
	for row in rows:
		auction = None
		if row.selling_type == lu.AUCTION and row.start_time:
			auction = {
				"min_bid": row.min_bid,
				"start_time": row.start_time,
				"end_time": row.end_time,
				"start_passed": get_datetime(row.start_time) <= now,
			}
		items.append(
			{
				"name": row.name,
				"title": row.title,
				"status": row.status,
				"selling_type": row.selling_type,
				"category_title": row.category_title,
				"unit": row.unit,
				"price_per_unit": row.price_per_unit if row.selling_type == lu.DIRECT else None,
				"quantity_available": row.quantity_available,
				"cover_image": row.cover_image,
				"seller_business_name": row.business_name,
				"submitted_on": row.submitted_on,
				"modified": row.modified,
				"previous_rejections": row.previous_rejections,
				"resubmitted": bool(row.resubmissions),
				"auction": auction,
			}
		)
	return {
		"items": items,
		"counts": counts,
		"total": total,
		"page": page,
		"page_size": page_size,
		"has_more": page * page_size < total,
	}


# -- one listing, in full -------------------------------------------------------------------------


@frappe.whitelist()
def get_listing_for_review(name: str) -> dict:
	"""The listing exactly as buyers will see it, plus what staff need to decide."""
	require_staff()
	listing = _get(name)
	auction = _auction_of(listing)
	return {
		"listing": mk.build_listing_detail(listing, "staff"),
		"expected_modified": str(listing.modified),
		"review": {
			"submitted_on": listing.submitted_on,
			"ai_suggested_min": listing.ai_suggested_min or None,
			"ai_suggested_max": listing.ai_suggested_max or None,
			"ai_fair_value": (auction.ai_fair_value or None) if auction else None,
			"resubmission_note": listing.resubmission_note,
			"reviewed_by": listing.reviewed_by,
			"reviewed_on": listing.reviewed_on,
			"seller": _seller_context(listing),
			"history": _review_history(listing.name),
			"edit_history": _edit_history(listing.name),
		},
		"actions": _action_state(listing),
	}


# -- decisions ------------------------------------------------------------------------------------


def _target(name: str, expected_modified: str | None, action: str):
	require_staff()
	listing = _get(name)
	_require_current(listing, expected_modified)
	state = _action_state(listing)[action]
	if not state["allowed"]:
		frappe.throw(state["blocked_reason"])
	return listing


@frappe.whitelist(methods=["POST"])
def approve_listing(name: str, expected_modified: str, note: str | None = None) -> dict:
	"""Publish a pending listing (or reinstate a suspended one). Its auction, if any, goes with it."""
	listing = _target(name, expected_modified, "approve")
	return _decide(listing, "Approved", lu.APPROVED, (note or "").strip() or None)


@frappe.whitelist(methods=["POST"])
def reject_listing(name: str, expected_modified: str, reason: str | None = None) -> dict:
	require_staff()
	reason = _require_reason(reason)
	listing = _target(name, expected_modified, "reject")
	return _decide(listing, "Rejected", lu.REJECTED, reason)


@frappe.whitelist(methods=["POST"])
def suspend_listing(name: str, expected_modified: str, reason: str | None = None) -> dict:
	require_staff()
	reason = _require_reason(reason)
	listing = _target(name, expected_modified, "suspend")
	return _decide(listing, "Suspended", lu.SUSPENDED, reason)
