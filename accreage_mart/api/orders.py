"""Whitelisted order endpoints (Epic 08).

Referenced from the frontend as ``accreage_mart.api.orders.<fn>``. Status rules live in
``accreage_mart/utils/order.py``; every write here runs inside ``lu.system_write()``.

Guard matrix (Story 8.1)
------------------------
=====================  ===============================  ==============================================
Endpoint               Caller                           Notes
=====================  ===============================  ==============================================
place_order            Buyer, account in good standing  Approved Direct listing only; min_order_qty <=
                                                         quantity <= quantity_available; never the
                                                         caller's own listing. No offer price (or one at
                                                         or above the listed price) -> listed-price order
                                                         (pending_confirmation); a lower positive offer
                                                         -> negotiating with the first Order Offer. 48 h
                                                         deadline either way. Notifies the seller.
get_order              the order's buyer, its seller,   full order + offers + payment proofs; the
                       staff / admin                    seller's bank details once the price is locked
list_buyer_orders      Buyer                            the caller's orders, newest first, status filter
list_seller_orders     Verified seller                  orders on the caller's listings, same shape
list_all_orders        Staff / Admin                    every order, read-only, status filter
get_my_bank_details    Verified seller                  the caller's own Seller Profile bank fields
update_my_bank_details Verified seller                  all four fields required
=====================  ===============================  ==============================================

Every order payload carries ``status_deadline``, ``action_by`` (buyer / seller / staff / none) and
``on_expiry`` (cancel / staff_review / auto_complete / null) so both parties can show the same
countdown (D14).
"""

import frappe
from frappe import _
from frappe.utils import flt, now_datetime

from accreage_mart.api import marketplace as mk
from accreage_mart.utils import listing as lu
from accreage_mart.utils import order as ou
from accreage_mart.utils.credentials import frontend_url
from accreage_mart.utils.notify import notify
from accreage_mart.utils.registration import require_staff

ORDER_LIST_FIELDS = (
	"name",
	"listing",
	"seller",
	"buyer",
	"source",
	"auction",
	"status",
	"status_deadline",
	"offer_count",
	"quantity",
	"unit",
	"listed_unit_price",
	"agreed_unit_price",
	"total_price",
	"cancel_requested",
	"cancel_request_reason",
	"staff_cancel_requested",
	"staff_cancel_request_reason",
	"needs_staff_review",
	"has_submitted_proof",
	"cancel_reason",
	"cancelled_by",
	"confirmed_on",
	"paid_on",
	"delivered_on",
	"completed_on",
	"invoice_no",
	"invoice_file",
	"creation",
	"modified",
)
CHECK_FIELDS = ("cancel_requested", "staff_cancel_requested", "needs_staff_review", "has_submitted_proof")


def _rs(amount) -> str:
	return f"Rs. {flt(amount):,.2f}"


def _qty(value) -> str:
	value = flt(value)
	return f"{value:g}"


# -- placing an order ------------------------------------------------------------------------------


def _lock_listing(name: str):
	"""Lock the Listing row and return it fresh (stock and status as of right now)."""
	if not name or not frappe.db.exists("Listing", name):
		frappe.throw(_("Listing not found."), frappe.DoesNotExistError)
	frappe.db.sql("select name from `tabListing` where name=%s for update", name)
	return frappe.get_doc("Listing", name)


@frappe.whitelist(methods=["POST"])
def place_order(listing: str, quantity: float, offer_price: float | None = None) -> dict:
	"""Order an approved Direct listing as the logged-in buyer.

	Without ``offer_price`` (or with one at or above the listed price) the order waits for the
	seller to confirm at the listed price. A lower ``offer_price`` opens a price negotiation with
	the buyer's offer as the first ``Order Offer``.
	"""
	user = lu.require_buyer("place orders")
	doc = _lock_listing(listing)

	if lu.owns_listing(doc.seller, user):
		frappe.throw(_("You cannot order your own listing."), frappe.PermissionError)
	if doc.selling_type != lu.DIRECT:
		frappe.throw(_("Auction lots are sold through bidding only."))
	if doc.status != lu.APPROVED or not mk.seller_is_active(doc.seller):
		frappe.throw(_("This listing is not available to order."))

	quantity = flt(quantity)
	min_qty = flt(doc.min_order_qty)
	if quantity <= 0:
		frappe.throw(_("Quantity must be greater than zero."))
	if min_qty and quantity < min_qty:
		frappe.throw(_("The minimum order is {0} {1}.").format(_qty(min_qty), doc.unit))
	if quantity > flt(doc.quantity_available):
		frappe.throw(_("Only {0} {1} is available.").format(_qty(doc.quantity_available), doc.unit))

	listed = flt(doc.price_per_unit)
	offer = None
	if offer_price not in (None, ""):
		offer = flt(offer_price)
		if offer <= 0:
			frappe.throw(_("Your offer price must be greater than zero."))
		if offer >= listed:
			offer = None  # at or above the listed price: just an order at the listed price

	status = ou.NEGOTIATING if offer else ou.PENDING_CONFIRMATION
	ou.assert_transition({"status": None}, status, ou.BUYER)
	now = now_datetime()

	savepoint = "place_order"
	frappe.db.savepoint(savepoint)
	try:
		with lu.system_write():
			order = frappe.get_doc(
				{
					"doctype": "Order",
					"listing": doc.name,
					"seller": doc.seller,
					"buyer": user,
					"source": ou.SOURCE_DIRECT,
					"status": status,
					"status_deadline": ou.deadline_for(status, now),
					"quantity": quantity,
					"unit": doc.unit,
					"listed_unit_price": listed,
					"total_price": quantity * (offer or listed),
					"offer_count": 1 if offer else 0,
				}
			).insert(ignore_permissions=True)
			if offer:
				frappe.get_doc(
					{
						"doctype": "Order Offer",
						"order": order.name,
						"offered_by": user,
						"party": ou.PARTY_BUYER,
						"unit_price": offer,
						"offered_on": now,
						"outcome": ou.OFFER_OPEN,
					}
				).insert(ignore_permissions=True)
	except Exception:
		frappe.db.rollback(save_point=savepoint)
		raise
	frappe.db.commit()

	_notify_order_placed(order, doc, offer)
	return _order_detail(order, user)


def _buyer_display_name(user: str) -> str:
	return (
		frappe.db.get_value("Buyer Profile", {"user": user}, "business_name")
		or frappe.db.get_value("User", user, "full_name")
		or "A buyer"
	)


def _notify_order_placed(order, listing, offer) -> None:
	"""Never blocks place_order: a failure here is logged, not raised."""
	try:
		seller_user = lu.seller_user_of(order.seller)
		if not seller_user:
			return
		buyer_name = _buyer_display_name(order.buyer)
		quantity = f"{_qty(order.quantity)} {order.unit}"
		if offer:
			title = "New price offer on your listing"
			message = (
				f"{buyer_name} wants {quantity} of {listing.title} and offered {_rs(offer)} per "
				f"{order.unit} (your price is {_rs(order.listed_unit_price)}). Order {order.name}: accept, "
				f"counter or decline within {ou.CONFIRM_HOURS} hours."
			)
		else:
			title = "New order to confirm"
			message = (
				f"{buyer_name} ordered {quantity} of {listing.title} at {_rs(order.listed_unit_price)} "
				f"per {order.unit} ({_rs(order.total_price)} in total). Order {order.name}: confirm or "
				f"decline within {ou.CONFIRM_HOURS} hours."
			)
		notify(
			"order_placed",
			seller_user,
			title,
			message,
			["email", "sms"],
			document_type="Order",
			document_name=order.name,
			email_template="order_placed",
			email_context={
				"full_name": frappe.db.get_value("User", seller_user, "full_name") or "there",
				"buyer_name": buyer_name,
				"order": order.name,
				"title": listing.title,
				"quantity": _qty(order.quantity),
				"unit": order.unit,
				"listed_price": _rs(order.listed_unit_price),
				"offer_price": _rs(offer) if offer else None,
				"total": _rs(order.total_price),
				"hours": ou.CONFIRM_HOURS,
			},
			email_cta_label="View order",
			email_cta_url=f"{frontend_url()}/seller/orders/{order.name}",
		)
	except Exception:
		frappe.log_error(title="Order placed notification failed")


# -- payloads --------------------------------------------------------------------------------------


def _listing_summary(listing_name: str) -> dict:
	row = frappe.db.get_value("Listing", listing_name, ["name", "title", "unit", "status"], as_dict=True)
	if not row:
		return {"name": listing_name, "title": None, "unit": None, "cover_image": None}
	cover = frappe.db.get_value(
		"Listing Image",
		{"parent": listing_name, "parenttype": "Listing"},
		"image",
		order_by="is_cover desc, idx asc",
	)
	return {"name": row.name, "title": row.title, "unit": row.unit, "cover_image": cover}


def _seller_summary(profile: str) -> dict:
	business_name = frappe.db.get_value("Seller Profile", profile, "business_name")
	return {"public_id": lu.public_seller_id(profile), "business_name": business_name}


def _buyer_summary(user: str, role: str | None) -> dict:
	summary = {
		"full_name": frappe.db.get_value("User", user, "full_name"),
		"business_name": frappe.db.get_value("Buyer Profile", {"user": user}, "business_name"),
	}
	if role == ou.STAFF:
		summary["email"] = user
	return summary


def _order_summary(row, viewer: str) -> dict:
	"""The list-row shape; ``get_order`` extends it. ``row`` is a doc or a dict of
	ORDER_LIST_FIELDS."""
	role = ou.viewer_role(row, viewer)
	data = {field: row.get(field) for field in ORDER_LIST_FIELDS}
	for field in CHECK_FIELDS:
		data[field] = bool(data[field])
	data.pop("seller")
	data.pop("buyer")
	data["listing"] = _listing_summary(row.get("listing"))
	data["seller"] = _seller_summary(row.get("seller"))
	data["buyer"] = _buyer_summary(row.get("buyer"), role)
	data["viewer_role"] = role
	data.update(ou.deadline_info(row))
	return data


def _offers(order_name: str) -> list[dict]:
	rows = frappe.get_all(
		"Order Offer",
		filters={"order": order_name},
		fields=["name", "party", "unit_price", "offered_on", "outcome"],
		order_by="offered_on asc, creation asc",
		ignore_permissions=True,
	)
	return [dict(row) for row in rows]


def _payment_proofs(order_name: str) -> list[dict]:
	rows = frappe.get_all(
		"Payment Proof",
		filters={"order": order_name},
		fields=[
			"name",
			"file",
			"status",
			"submitted_on",
			"amount_paid",
			"paid_on",
			"paid_from_bank",
			"transfer_reference",
			"note",
			"reviewed_on",
			"reject_category",
			"reject_reason",
		],
		order_by="submitted_on desc, creation desc",
		ignore_permissions=True,
	)
	return [dict(row) for row in rows]


def _order_detail(order, viewer: str) -> dict:
	data = _order_summary(order, viewer)
	data["offers"] = _offers(order.name)
	data["payment_proofs"] = _payment_proofs(order.name)

	# D10 - the buyer sees where to pay once the price is locked; never before, never publicly.
	locked = order.status in ou.PRICE_LOCKED_STATUSES
	has_bank = ou.has_bank_details(order.seller)
	data["seller_bank_details"] = ou.bank_details_of(order.seller) if locked and has_bank else None
	data["payment_details_pending"] = locked and not has_bank
	return data


def _get_order_doc(name: str):
	if not name or not frappe.db.exists("Order", name):
		frappe.throw(_("Order not found."), frappe.DoesNotExistError)
	return frappe.get_doc("Order", name)


def _require_login() -> str:
	user = frappe.session.user
	if not user or user == "Guest":
		frappe.throw(_("Not authenticated."), frappe.AuthenticationError)
	return user


@frappe.whitelist()
def get_order(name: str) -> dict:
	"""One order with its offers and payment proofs - for its buyer, its seller or staff."""
	user = _require_login()
	order = _get_order_doc(name)
	if not ou.viewer_role(order, user):
		frappe.throw(_("You do not have access to this order."), frappe.PermissionError)
	return _order_detail(order, user)


# -- lists -----------------------------------------------------------------------------------------


def _status_filter(status) -> list[str] | None:
	"""``status`` may be one status, a comma-separated list, or a JSON list."""
	if status in (None, "", []):
		return None
	if isinstance(status, str):
		status = frappe.parse_json(status) if status.strip().startswith("[") else status.split(",")
	values = [str(s).strip() for s in status if str(s).strip()]
	unknown = [s for s in values if s not in ou.STATUSES]
	if unknown:
		frappe.throw(_("Unknown order status: {0}").format(", ".join(unknown)))
	return values or None


def _list(filters: dict, status, page, page_size, viewer: str) -> dict:
	statuses = _status_filter(status)
	if statuses:
		filters = {**filters, "status": ["in", statuses]}
	page, page_size = mk.page_args(page, page_size)
	total = frappe.db.count("Order", filters)
	rows = frappe.get_all(
		"Order",
		filters=filters,
		fields=list(ORDER_LIST_FIELDS),
		order_by="creation desc, name desc",
		limit_start=(page - 1) * page_size,
		limit_page_length=page_size,
		ignore_permissions=True,
	)
	return {
		"items": [_order_summary(row, viewer) for row in rows],
		"total": total,
		"page": page,
		"page_size": page_size,
		"has_more": page * page_size < total,
	}


@frappe.whitelist()
def list_buyer_orders(status=None, page: int = 1, page_size: int = mk.DEFAULT_PAGE_SIZE) -> dict:
	"""The caller's own orders as a buyer, newest first."""
	user = lu.require_buyer("view buyer orders")
	return _list({"buyer": user}, status, page, page_size, user)


@frappe.whitelist()
def list_seller_orders(status=None, page: int = 1, page_size: int = mk.DEFAULT_PAGE_SIZE) -> dict:
	"""Orders on the caller's listings, newest first."""
	profile = lu.require_seller()
	return _list({"seller": profile}, status, page, page_size, frappe.session.user)


@frappe.whitelist()
def list_all_orders(status=None, page: int = 1, page_size: int = mk.DEFAULT_PAGE_SIZE) -> dict:
	"""Every order on the platform, read-only, for staff/admin."""
	require_staff()
	return _list({}, status, page, page_size, frappe.session.user)


# -- seller bank details (D10) ---------------------------------------------------------------------


def _bank_payload(profile: str) -> dict:
	details = ou.bank_details_of(profile)
	return {**details, "complete": all(details.values())}


@frappe.whitelist()
def get_my_bank_details() -> dict:
	"""The caller's own bank details (where buyers pay them)."""
	return _bank_payload(lu.require_seller())


@frappe.whitelist(methods=["POST"])
def update_my_bank_details(bank_name: str, bank_branch: str, account_name: str, account_number: str) -> dict:
	"""Set the caller's own bank details. All four fields are required."""
	profile = lu.require_seller()
	values = {
		"bank_name": (bank_name or "").strip(),
		"bank_branch": (bank_branch or "").strip(),
		"account_name": (account_name or "").strip(),
		"account_number": ou.normalize_account_number(account_number),
	}
	labels = {
		"bank_name": _("Bank name"),
		"bank_branch": _("Branch"),
		"account_name": _("Account name"),
		"account_number": _("Account number"),
	}
	missing = [labels[field] for field, value in values.items() if not value]
	if missing:
		frappe.throw(_("Please fill in: {0}.").format(", ".join(missing)))
	if not ou.ACCOUNT_NUMBER_RE.match(values["account_number"]):
		frappe.throw(_("The account number should be 5 to 20 digits."))
	for field in ("bank_name", "bank_branch", "account_name"):
		if len(values[field]) > 140:
			frappe.throw(_("{0} is too long.").format(labels[field]))

	doc = frappe.get_doc("Seller Profile", profile)
	doc.update(values)
	doc.save(ignore_permissions=True)
	frappe.db.commit()
	return _bank_payload(profile)
