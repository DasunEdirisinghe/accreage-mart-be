"""Whitelisted read endpoints for listings (Epic 04, Story 4.3).

Referenced from the frontend as ``accreage_mart.api.marketplace.<fn>``.

Guard matrix
------------
=====================  ================  =====================================================
Endpoint               Caller            Notes
=====================  ================  =====================================================
list_marketplace       anyone (guest)    published listings of active sellers; explicit field
                                          allow-list; never exposes seller emails
get_listing            anyone (guest)    one listing; what you see depends on who you are and
                                          the listing's status (see ``get_listing``)
get_public_seller      anyone (guest)    a seller's public card by opaque public id
list_my_listings       verified seller   the caller's own listings, with per-tab counts
=====================  ================  =====================================================

The Seller Profile name is the seller's email, so every public payload carries an opaque
``public_id`` instead (see ``utils.listing.public_seller_id``).
"""

import frappe
from frappe import _
from frappe.utils import cint, flt, now_datetime

from accreage_mart.utils import listing as lu

DEFAULT_PAGE_SIZE = 12
MAX_PAGE_SIZE = 48

SORTS = {
	"newest": "l.creation desc, l.name desc",
	"price_asc": "display_price asc, l.creation desc",
	"price_desc": "display_price desc, l.creation desc",
}

# Seller-facing tabs -> listing statuses.
TABS = {
	"pending": lu.PENDING,
	"live": lu.APPROVED,
	"hidden": lu.HIDDEN,
	"rejected": lu.REJECTED,
	"suspended": lu.SUSPENDED,
	"archived": lu.ARCHIVED,
}

LISTING_FROM = """
	from `tabListing` l
	join `tabSeller Profile` sp on sp.name = l.seller
	join `tabUser` u on u.name = sp.user
	left join `tabAuction` a on a.name = l.auction
	left join `tabCategory` c on c.name = l.category
"""
_COVER = """(select i.image from `tabListing Image` i
	where i.parent = l.name and i.parenttype = 'Listing'
	order by i.is_cover desc, i.idx asc limit 1)"""
LISTING_COLUMNS = f"""
	l.name, l.title, l.selling_type, l.category, c.title as category_title, c.area as category_area,
	l.unit, l.price_per_unit, l.min_order_qty, l.quantity_available, l.low_stock_level,
	l.district, l.location, l.organic, l.status, l.status_reason, l.resubmission_note,
	l.creation, l.modified, sp.name as seller_profile, sp.business_name, sp.trust_score,
	a.min_bid, a.start_time, a.end_time, a.duration_hours,
	if(l.selling_type = 'Direct', l.price_per_unit, a.min_bid) as display_price,
	{_COVER} as cover_image
"""
# Published, by an active verified seller; an auction that has already ended is not offered.
_LIVE = """l.status = 'Approved' and sp.verified = 1 and u.enabled = 1
	and (l.selling_type = 'Direct' or a.end_time > %(now)s)"""


# -- shaping ------------------------------------------------------------------------------------


def page_args(page, page_size) -> tuple[int, int]:
	page = max(1, cint(page) or 1)
	page_size = min(max(1, cint(page_size) or DEFAULT_PAGE_SIZE), MAX_PAGE_SIZE)
	return page, page_size


def _auction_summary(row, listing_status: str) -> dict | None:
	if row.get("selling_type") != lu.AUCTION or not row.get("start_time"):
		return None
	return {
		"min_bid": row.min_bid,
		"start_time": row.start_time,
		"end_time": row.end_time,
		"duration_hours": row.duration_hours,
		"status": lu.derive_auction_status(listing_status, row.start_time, row.end_time),
		"bidding_enabled": lu.BIDDING_ENABLED,
	}


def _card(row) -> dict:
	"""The public marketplace card. An explicit allow-list: nothing is added by accident."""
	return {
		"name": row.name,
		"title": row.title,
		"selling_type": row.selling_type,
		"category": row.category,
		"category_title": row.category_title,
		"unit": row.unit,
		"price_per_unit": row.price_per_unit if row.selling_type == lu.DIRECT else None,
		"quantity_available": row.quantity_available,
		"in_stock": flt(row.quantity_available) > 0,
		"district": row.district,
		"location": row.location,
		"organic": bool(row.organic),
		"cover_image": row.cover_image,
		"created": row.creation,
		"seller": {
			"public_id": lu.public_seller_id(row.seller_profile),
			"business_name": row.business_name,
			"trust_score": row.trust_score,
		},
		"auction": _auction_summary(row, lu.APPROVED),
	}


def _my_card(row) -> dict:
	return {
		"name": row.name,
		"title": row.title,
		"status": row.status,
		"selling_type": row.selling_type,
		"category": row.category,
		"category_title": row.category_title,
		"unit": row.unit,
		"price_per_unit": row.price_per_unit if row.selling_type == lu.DIRECT else None,
		"quantity_available": row.quantity_available,
		"low_stock_level": row.low_stock_level,
		"stock_state": lu.stock_state(row.quantity_available, row.low_stock_level)
		if row.selling_type == lu.DIRECT
		else None,
		"district": row.district,
		"cover_image": row.cover_image,
		"status_reason": row.status_reason,
		"resubmission_note": row.resubmission_note,
		"created": row.creation,
		"modified": row.modified,
		"auction": _auction_summary(row, row.status),
	}


def like_pattern(text: str) -> str:
	escaped = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
	return f"%{escaped}%"


# -- marketplace --------------------------------------------------------------------------------


@frappe.whitelist(allow_guest=True)
def list_marketplace(
	category: str | None = None,
	district: str | None = None,
	selling_type: str | None = None,
	organic: int | None = None,
	min_price: float | None = None,
	max_price: float | None = None,
	in_stock: int | None = None,
	search: str | None = None,
	seller: str | None = None,
	sort: str = "newest",
	page: int = 1,
	page_size: int = DEFAULT_PAGE_SIZE,
) -> dict:
	"""Published listings, filtered, searched, sorted and paginated.

	``min_price`` / ``max_price`` apply to the price per unit of a Direct listing and to the
	minimum bid of an Auction. ``seller`` is a seller's public id.
	"""
	if sort not in SORTS:
		frappe.throw(_("Unknown sort order."))
	page, page_size = page_args(page, page_size)

	where = [_LIVE]
	params: dict = {"now": now_datetime()}
	if category:
		where.append("l.category = %(category)s")
		params["category"] = category
	if district:
		where.append("l.district = %(district)s")
		params["district"] = district
	if selling_type:
		if selling_type not in lu.SELLING_TYPES:
			frappe.throw(_("Unknown selling type."))
		where.append("l.selling_type = %(selling_type)s")
		params["selling_type"] = selling_type
	if organic is not None and cint(organic):
		where.append("l.organic = 1")
	if in_stock is not None and cint(in_stock):
		where.append("l.quantity_available > 0")
	if min_price not in (None, ""):
		where.append("if(l.selling_type = 'Direct', l.price_per_unit, a.min_bid) >= %(min_price)s")
		params["min_price"] = flt(min_price)
	if max_price not in (None, ""):
		where.append("if(l.selling_type = 'Direct', l.price_per_unit, a.min_bid) <= %(max_price)s")
		params["max_price"] = flt(max_price)
	if search and search.strip():
		where.append("(l.title like %(search)s or l.description like %(search)s)")
		params["search"] = like_pattern(search.strip())
	if seller:
		profile = lu.seller_profile_from_public_id(seller)
		where.append("l.seller = %(seller)s")
		params["seller"] = profile or ""

	condition = " and ".join(f"({clause})" for clause in where)
	total = frappe.db.sql(f"select count(*) {LISTING_FROM} where {condition}", params)[0][0]
	rows = frappe.db.sql(
		f"select {LISTING_COLUMNS} {LISTING_FROM} where {condition} order by {SORTS[sort]} "
		"limit %(limit)s offset %(offset)s",
		{**params, "limit": page_size, "offset": (page - 1) * page_size},
		as_dict=True,
	)
	return {
		"items": [_card(row) for row in rows],
		"total": total,
		"page": page,
		"page_size": page_size,
		"has_more": page * page_size < total,
	}


# -- one listing --------------------------------------------------------------------------------

# Plain strings here; translated with _() where they are used (no frappe.local at import time).
_PUBLIC_UNAVAILABLE = {
	"hidden": "This listing is temporarily unavailable.",
	"suspended": "This listing is unavailable.",
	"seller_unavailable": "This listing is unavailable.",
	"archived": "This listing is no longer available.",
}
_ORDER_HOLDER_NOTE = "Your order is not affected."
_OWN_MESSAGES = {
	"pending": "Waiting for staff approval. Buyers can't see this listing yet.",
	"rejected": "This listing was rejected. Review the reason, update it and resubmit.",
	"hidden": "You've hidden this listing. Buyers can't see it on the marketplace.",
	"suspended": "This listing was suspended by staff. Please contact staff.",
	"archived": "This listing is archived.",
	"seller_unavailable": "Your seller account is not active, so this listing is not shown.",
}


def seller_is_active(profile: str) -> bool:
	row = frappe.db.sql(
		"select sp.verified, u.enabled from `tabSeller Profile` sp join `tabUser` u on u.name = sp.user "
		"where sp.name = %s",
		(profile,),
	)
	return bool(row) and bool(row[0][0]) and bool(row[0][1])


def _viewer_kind(listing) -> str:
	user = frappe.session.user
	if user == "Guest":
		return "public"
	if lu.is_staff_or_admin(user):
		return "staff"
	if lu.owns_listing(listing.seller, user):
		return "owner"
	if lu.viewer_has_order(listing.name, user):
		return "order_holder"
	return "public"


def _availability(listing) -> str:
	if listing.status == lu.APPROVED:
		return "available" if seller_is_active(listing.seller) else "seller_unavailable"
	return {
		lu.PENDING: "pending",
		lu.REJECTED: "rejected",
		lu.HIDDEN: "hidden",
		lu.SUSPENDED: "suspended",
		lu.ARCHIVED: "archived",
	}[listing.status]


def _seller_card(profile: str) -> dict:
	row = frappe.db.get_value(
		"Seller Profile", profile, ["business_name", "district", "trust_score", "verified"], as_dict=True
	)
	return {
		"public_id": lu.public_seller_id(profile),
		"business_name": row.business_name,
		"district": row.district,
		"trust_score": row.trust_score,
		"verified": bool(row.verified),
	}


def build_listing_detail(listing, viewer: str) -> dict:
	category = frappe.db.get_value("Category", listing.category, ["title", "area"], as_dict=True)
	images = sorted(listing.images, key=lambda row: (not row.is_cover, row.idx))
	auction = frappe.get_doc("Auction", listing.auction) if listing.auction else None
	detail = {
		"name": listing.name,
		"title": listing.title,
		"description": listing.description,
		"selling_type": listing.selling_type,
		"category": {"name": listing.category, "title": category.title, "area": category.area},
		"unit": listing.unit,
		"price_per_unit": listing.price_per_unit if listing.selling_type == lu.DIRECT else None,
		"min_order_qty": listing.min_order_qty if listing.selling_type == lu.DIRECT else None,
		"quantity_available": listing.quantity_available,
		"in_stock": flt(listing.quantity_available) > 0,
		"district": listing.district,
		"location": listing.location,
		"latitude": listing.latitude or None,
		"longitude": listing.longitude or None,
		"map_url": lu.google_maps_url(listing.latitude, listing.longitude),
		"organic": bool(listing.organic),
		"certification": listing.certification,
		"images": [{"url": row.image, "is_cover": bool(row.is_cover)} for row in images],
		"cover_image": images[0].image if images else None,
		"seller": _seller_card(listing.seller),
		"created": listing.creation,
		"auction": None
		if not auction
		else {
			"min_bid": auction.min_bid,
			"start_time": auction.start_time,
			"end_time": auction.end_time,
			"duration_hours": auction.duration_hours,
			"status": lu.derive_auction_status(listing.status, auction.start_time, auction.end_time),
			"bidding_enabled": lu.BIDDING_ENABLED,
		},
	}
	if viewer in ("owner", "staff"):
		detail.update(
			{
				"status": listing.status,
				"status_reason": listing.status_reason,
				"resubmission_note": listing.resubmission_note,
				"submitted_on": listing.submitted_on,
				"low_stock_level": listing.low_stock_level,
				"stock_state": lu.stock_state(listing.quantity_available, listing.low_stock_level)
				if listing.selling_type == lu.DIRECT
				else None,
			}
		)
	return detail


@frappe.whitelist(allow_guest=True)
def get_listing(name: str) -> dict:
	"""One listing, as seen by the caller.

	* Staff and the owning seller see it in any status, with its status and review fields.
	* A buyer with an order on it may still open it once it is hidden, suspended or archived,
	  with a message that their order is not affected.
	* Everyone else sees a published listing, a short "unavailable" message for a hidden,
	  suspended or archived one, and "not found" for a pending or rejected one.

	``{"availability", "message", "accepting_orders", "viewer", "listing"}``. ``listing`` is
	None when the viewer only gets the message.
	"""
	if not name or not frappe.db.exists("Listing", name):
		frappe.throw(_("Listing not found."), frappe.DoesNotExistError)
	listing = frappe.get_doc("Listing", name)
	viewer = _viewer_kind(listing)
	availability = _availability(listing)

	if viewer in ("public", "order_holder") and availability in ("pending", "rejected"):
		frappe.throw(_("Listing not found."), frappe.DoesNotExistError)

	accepting = (
		availability == "available"
		and listing.selling_type == lu.DIRECT
		and flt(listing.quantity_available) > 0
	)
	response = {
		"availability": availability,
		"accepting_orders": accepting,
		"viewer": viewer,
		"message": None,
		"listing": None,
	}

	if viewer in ("owner", "staff"):
		message = _OWN_MESSAGES.get(availability)
		response["message"] = _(message) if message else None
		response["listing"] = build_listing_detail(listing, viewer)
	elif availability == "available":
		response["listing"] = build_listing_detail(listing, viewer)
	elif viewer == "order_holder":
		response["message"] = f"{_(_PUBLIC_UNAVAILABLE[availability])} {_(_ORDER_HOLDER_NOTE)}"
		response["listing"] = build_listing_detail(listing, viewer)
	else:
		response["message"] = _(_PUBLIC_UNAVAILABLE[availability])
	return response


# -- a seller's public card ---------------------------------------------------------------------


@frappe.whitelist(allow_guest=True)
def get_public_seller(public_id: str) -> dict:
	profile = lu.seller_profile_from_public_id(public_id)
	if not profile or not seller_is_active(profile):
		frappe.throw(_("Seller not found."), frappe.DoesNotExistError)
	row = frappe.db.get_value(
		"Seller Profile",
		profile,
		["business_name", "district", "description", "trust_score", "verified", "creation"],
		as_dict=True,
	)
	live = frappe.db.sql(
		f"select count(*) {LISTING_FROM} where {_LIVE} and l.seller = %(seller)s",
		{"now": now_datetime(), "seller": profile},
	)[0][0]
	return {
		"public_id": public_id,
		"business_name": row.business_name,
		"district": row.district,
		"description": row.description,
		"trust_score": row.trust_score,
		"verified": bool(row.verified),
		"member_since": row.creation,
		"live_listing_count": live,
	}


# -- the seller's own listings ------------------------------------------------------------------


@frappe.whitelist()
def list_my_listings(
	tab: str | None = None,
	search: str | None = None,
	page: int = 1,
	page_size: int = DEFAULT_PAGE_SIZE,
) -> dict:
	"""The caller's listings, newest change first, with a count per tab for the status tabs."""
	profile = lu.require_seller()
	if tab and tab not in TABS:
		frappe.throw(_("Unknown tab."))
	page, page_size = page_args(page, page_size)

	counts = {key: 0 for key in TABS}
	for status, count in frappe.db.sql(
		"select status, count(*) from `tabListing` where seller = %s group by status", (profile,)
	):
		for key, value in TABS.items():
			if value == status:
				counts[key] = count

	where = ["l.seller = %(seller)s"]
	params: dict = {"seller": profile}
	if tab:
		where.append("l.status = %(status)s")
		params["status"] = TABS[tab]
	if search and search.strip():
		where.append("(l.title like %(search)s or l.description like %(search)s)")
		params["search"] = like_pattern(search.strip())
	condition = " and ".join(f"({clause})" for clause in where)

	total = frappe.db.sql(f"select count(*) {LISTING_FROM} where {condition}", params)[0][0]
	rows = frappe.db.sql(
		f"select {LISTING_COLUMNS} {LISTING_FROM} where {condition} order by l.modified desc, l.name desc "
		"limit %(limit)s offset %(offset)s",
		{**params, "limit": page_size, "offset": (page - 1) * page_size},
		as_dict=True,
	)
	return {
		"items": [_my_card(row) for row in rows],
		"counts": counts,
		"total": total,
		"page": page,
		"page_size": page_size,
		"has_more": page * page_size < total,
	}
