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

Guard matrix (Story 8.2)
------------------------
Every write below locks the Order row (``for update``) first, and a buyer/seller action is refused once
``status_deadline`` has passed (D14; staff are not on the clock).

==================================  ==================  ===============================================
Endpoint                            Caller              Notes
==================================  ==================  ===============================================
respond_to_offer                    buyer / seller      negotiating only; only the party who did not
                                                        make the open offer. accept (seller needs bank
                                                        details; quantity re-checked against stock now)
                                                        -> confirmed, 72 h; counter must differ from the
                                                        open offer, refused after 5 offers (a seller
                                                        counter also needs bank details); decline ->
                                                        cancelled
confirm_order / decline_order       seller              pending_confirmation only; confirm needs bank
                                                        details and returns the stock impact
get_stock_impact                    seller / staff      informational, never blocks (D11)
cancel_order                        buyer / seller /    D5 + D13 via ``utils.order.cancel_refusal``;
                                    staff               reason required for seller and staff
request_cancellation                buyer               confirmed, no slip ever submitted, reason
resolve_cancellation_request        seller              accept (cancels) / refuse the buyer's request
request_staff_cancellation          seller              confirmed after a rejected slip, reason;
                                                        notifies staff and the buyer
resolve_staff_cancellation_request  staff / admin       cancel / refuse the seller's request, reason
==================================  ==================  ===============================================

Every order payload carries ``status_deadline``, ``action_by`` (buyer / seller / staff / none) and
``on_expiry`` (cancel / staff_review / auto_complete / null) so both parties can show the same
countdown (D14).
"""

from contextlib import contextmanager

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


# -- shared guards for party actions (Story 8.2) ---------------------------------------------------

REASON_MAX = 1000
PARTY_OF = {ou.BUYER: ou.PARTY_BUYER, ou.SELLER: ou.PARTY_SELLER}


def _lock_order(name: str):
	"""Lock the Order row and return it fresh, so two people acting on the same order at once
	are serialised (same pattern as ``place_bid`` in ``api/auctions.py``)."""
	if not name or not frappe.db.exists("Order", name):
		frappe.throw(_("Order not found."), frappe.DoesNotExistError)
	frappe.db.sql("select name from `tabOrder` where name=%s for update", name)
	return frappe.get_doc("Order", name)


def _actor(order, allowed: tuple[str, ...], refusal: str) -> str:
	"""The caller's role on ``order`` (buyer / seller / staff), which must be in ``allowed``."""
	user = _require_login()
	role = ou.viewer_role(order, user)
	if not role:
		frappe.throw(_("You do not have access to this order."), frappe.PermissionError)
	if role not in allowed:
		frappe.throw(refusal, frappe.PermissionError)
	return role


def _require_status(order, *statuses: str) -> None:
	if order.status not in statuses:
		frappe.throw(
			_("This action is not allowed while the order is {0}.").format(ou.status_label(order.status))
		)


def _party_deadline(order, actor: str) -> None:
	"""D14 - a buyer or seller can't act once the step's deadline has passed, even if the
	scheduled job hasn't processed the order yet. Staff are not on the clock."""
	if actor in (ou.BUYER, ou.SELLER):
		ou.assert_before_deadline(order)


def _reason(reason: str | None, *, required: bool, label: str = "a reason") -> str:
	value = (reason or "").strip()
	if required and not value:
		frappe.throw(_("Please give {0}.").format(_(label)))
	if len(value) > REASON_MAX:
		frappe.throw(_("Please keep the reason under {0} characters.").format(REASON_MAX))
	return value


def _require_bank_details(order) -> None:
	if not ou.has_bank_details(order.seller):
		frappe.throw(_("Add your bank details first, so the buyer knows where to pay."))


@contextmanager
def _atomic(savepoint: str):
	"""Run the wrapped writes as system writes; roll them back together on any error, commit
	on success (which also releases the row lock)."""
	frappe.db.savepoint(savepoint)
	try:
		with lu.system_write():
			yield
	except Exception:
		frappe.db.rollback(save_point=savepoint)
		raise
	frappe.db.commit()


def _open_offer(order_name: str):
	name = frappe.db.get_value(
		"Order Offer",
		{"order": order_name, "outcome": ou.OFFER_OPEN},
		"name",
		order_by="offered_on desc, creation desc",
	)
	return frappe.get_doc("Order Offer", name) if name else None


def _close_open_offers(order_name: str, outcome: str) -> None:
	for name in frappe.get_all(
		"Order Offer", filters={"order": order_name, "outcome": ou.OFFER_OPEN}, pluck="name"
	):
		offer = frappe.get_doc("Order Offer", name)
		offer.outcome = outcome
		offer.save(ignore_permissions=True)


def _confirm(order, unit_price: float, now) -> None:
	"""Lock the price and start the 72 h payment clock (REQ-12, D4)."""
	order.agreed_unit_price = unit_price
	order.total_price = flt(order.quantity) * unit_price
	order.status = ou.CONFIRMED
	order.confirmed_on = now
	order.status_deadline = ou.deadline_for(ou.CONFIRMED, now)
	order.save(ignore_permissions=True)


def _cancel(order, by_user: str, reason: str) -> None:
	order.status = ou.CANCELLED
	order.status_deadline = None
	order.cancel_reason = reason
	order.cancelled_by = by_user
	# Requests and flags are moot on a cancelled order; the reasons stay as history.
	order.cancel_requested = 0
	order.staff_cancel_requested = 0
	order.needs_staff_review = 0
	order.save(ignore_permissions=True)
	_close_open_offers(order.name, ou.OFFER_DECLINED)


def _with_stock_impact(order, user: str) -> dict:
	"""The order payload plus the D11 stock-impact figures for the seller / staff (None for
	the buyer - stock held by other buyers' orders is the seller's business)."""
	data = _order_detail(order, user)
	data["stock_impact"] = ou.stock_impact(order) if data["viewer_role"] in (ou.SELLER, ou.STAFF) else None
	return data


# -- negotiation (D1, D2) --------------------------------------------------------------------------

ACCEPT = "accept"
COUNTER = "counter"
DECLINE = "decline"


@frappe.whitelist(methods=["POST"])
def respond_to_offer(order: str, action: str, price: float | None = None, reason: str | None = None) -> dict:
	"""Answer the open offer on a negotiating order: ``accept``, ``counter`` (with ``price``)
	or ``decline`` (optional ``reason``; cancels the order). Only the party who did not make
	the open offer may answer it."""
	action = (action or "").strip().lower()
	if action not in (ACCEPT, COUNTER, DECLINE):
		frappe.throw(_("Choose accept, counter or decline."))

	doc = _lock_order(order)
	actor = _actor(doc, (ou.BUYER, ou.SELLER), _("Only the buyer and the seller can negotiate the price."))
	user = frappe.session.user
	_require_status(doc, ou.NEGOTIATING)
	_party_deadline(doc, actor)

	open_offer = _open_offer(doc.name)
	if not open_offer:
		frappe.throw(_("There is no open offer to respond to."))
	if open_offer.party == PARTY_OF[actor]:
		frappe.throw(_("Waiting for the other party to respond to your offer."))

	now = now_datetime()
	impact_needed = False
	if action == ACCEPT:
		if actor == ou.SELLER:
			_require_bank_details(doc)
		listing = _lock_listing(doc.listing)
		if flt(doc.quantity) > flt(listing.quantity_available):
			frappe.throw(
				_("Only {0} {1} is left on this listing, less than this order's {2} {1}.").format(
					_qty(listing.quantity_available), doc.unit, _qty(doc.quantity)
				)
			)
		ou.assert_transition(doc, ou.CONFIRMED, actor)
		with _atomic("respond_to_offer"):
			open_offer.outcome = ou.OFFER_ACCEPTED
			open_offer.save(ignore_permissions=True)
			_confirm(doc, flt(open_offer.unit_price), now)
		impact_needed = True
		_notify_offer_accepted(doc, actor, flt(open_offer.unit_price))

	elif action == COUNTER:
		if price in (None, ""):
			frappe.throw(_("Enter your counter price."))
		price = flt(price, 2)
		if price <= 0:
			frappe.throw(_("Your counter price must be greater than zero."))
		if actor == ou.SELLER:
			# The buyer can accept a counter straight into a confirmed order, which needs somewhere
			# to pay; only auction-win orders may exist without the seller's bank details.
			_require_bank_details(doc)
		offers_made = frappe.db.count("Order Offer", {"order": doc.name})
		if offers_made >= ou.MAX_OFFERS:
			frappe.throw(
				_("{0} offers have been made, so you can only accept or decline now.").format(ou.MAX_OFFERS)
			)
		if price == flt(open_offer.unit_price, 2):
			frappe.throw(
				_("Your counter must differ from the last offer ({0}).").format(_rs(open_offer.unit_price))
			)
		with _atomic("respond_to_offer"):
			open_offer.outcome = ou.OFFER_COUNTERED
			open_offer.save(ignore_permissions=True)
			frappe.get_doc(
				{
					"doctype": "Order Offer",
					"order": doc.name,
					"offered_by": user,
					"party": PARTY_OF[actor],
					"unit_price": price,
					"offered_on": now,
					"outcome": ou.OFFER_OPEN,
				}
			).insert(ignore_permissions=True)
			doc.offer_count = offers_made + 1
			doc.total_price = flt(doc.quantity) * price
			doc.status_deadline = ou.deadline_for(ou.NEGOTIATING, now)
			doc.save(ignore_permissions=True)
		_notify_countered(doc, actor, price)

	else:  # decline
		why = _reason(reason, required=False)
		ou.assert_transition(doc, ou.CANCELLED, actor)
		who = "buyer" if actor == ou.BUYER else "seller"
		with _atomic("respond_to_offer"):
			open_offer.outcome = ou.OFFER_DECLINED
			open_offer.save(ignore_permissions=True)
			_cancel(doc, user, f"Offer declined by the {who}." + (f" {why}" if why else ""))
		_notify_offer_declined(doc, actor, flt(open_offer.unit_price), why)

	doc.reload()
	return _with_stock_impact(doc, user) if impact_needed else _order_detail(doc, user)


# -- listed-price confirmation ---------------------------------------------------------------------


@frappe.whitelist(methods=["POST"])
def confirm_order(order: str) -> dict:
	"""The seller accepts a listed-price order: the price locks and the buyer's 72 h payment
	clock starts. Needs the seller's bank details. Returns the stock-impact figures (D11)."""
	doc = _lock_order(order)
	_actor(doc, (ou.SELLER,), _("Only the seller can confirm this order."))
	_require_status(doc, ou.PENDING_CONFIRMATION)
	_party_deadline(doc, ou.SELLER)
	_require_bank_details(doc)
	ou.assert_transition(doc, ou.CONFIRMED, ou.SELLER)
	with _atomic("confirm_order"):
		_confirm(doc, flt(doc.listed_unit_price), now_datetime())
	_notify_confirmed(doc)
	doc.reload()
	return _with_stock_impact(doc, frappe.session.user)


@frappe.whitelist(methods=["POST"])
def decline_order(order: str, reason: str | None = None) -> dict:
	"""The seller turns down a listed-price order (optional ``reason``); the order is cancelled."""
	doc = _lock_order(order)
	_actor(doc, (ou.SELLER,), _("Only the seller can decline this order."))
	_require_status(doc, ou.PENDING_CONFIRMATION)
	_party_deadline(doc, ou.SELLER)
	why = _reason(reason, required=False)
	ou.assert_transition(doc, ou.CANCELLED, ou.SELLER)
	with _atomic("decline_order"):
		_cancel(doc, frappe.session.user, "Declined by the seller." + (f" {why}" if why else ""))
	_notify_declined(doc, why)
	doc.reload()
	return _order_detail(doc, frappe.session.user)


@frappe.whitelist()
def get_stock_impact(order: str) -> dict:
	"""Stock now, stock held by the listing's other price-agreed orders, this order's quantity and
	the projected stock once all of them are paid - for the seller (or staff) before they commit
	stock (D11). Informational only: nothing is ever blocked on it."""
	doc = _get_order_doc(order)
	_actor(doc, (ou.SELLER, ou.STAFF), _("Only the seller can see the stock impact."))
	return ou.stock_impact(doc)


# -- cancellation (D5, D13) ------------------------------------------------------------------------


@frappe.whitelist(methods=["POST"])
def cancel_order(order: str, reason: str | None = None) -> dict:
	"""Cancel an order, following D5 + D13:

	- before the price is agreed: the buyer freely; the seller or staff with a reason;
	- ``confirmed`` with no slip ever submitted: the seller or staff with a reason (the buyer sends a
	  cancellation request instead);
	- ``confirmed`` after a rejected slip: the buyer, or staff with a reason (the seller asks staff);
	- while a slip is under review, or once payment is approved: nobody.
	"""
	doc = _lock_order(order)
	actor = _actor(doc, (ou.BUYER, ou.SELLER, ou.STAFF), _("You can't cancel this order."))
	refusal = ou.cancel_refusal(doc, actor)
	if refusal:
		frappe.throw(refusal)
	_party_deadline(doc, actor)
	why = _reason(reason, required=actor != ou.BUYER)
	default = {ou.BUYER: "Cancelled by the buyer.", ou.SELLER: "Cancelled by the seller.", ou.STAFF: ""}
	with _atomic("cancel_order"):
		_cancel(doc, frappe.session.user, why or default[actor])
	_notify_cancelled(doc, actor, doc.cancel_reason)
	doc.reload()
	return _order_detail(doc, frappe.session.user)


@frappe.whitelist(methods=["POST"])
def request_cancellation(order: str, reason: str) -> dict:
	"""The buyer asks the seller to cancel a ``confirmed`` order on which no payment slip was
	ever submitted (D5). The order stays as it is until the seller answers."""
	doc = _lock_order(order)
	_actor(doc, (ou.BUYER,), _("Only the buyer can ask to cancel this order."))
	if doc.status in (ou.PENDING_CONFIRMATION, ou.NEGOTIATING):
		frappe.throw(_("The price isn't agreed yet, so you can cancel this order directly."))
	_require_status(doc, ou.CONFIRMED)
	if doc.has_submitted_proof:
		frappe.throw(_("You have submitted a payment slip, so you can cancel this order directly."))
	if doc.cancel_requested:
		frappe.throw(_("You have already asked the seller to cancel this order."))
	_party_deadline(doc, ou.BUYER)
	why = _reason(reason, required=True, label="a reason for cancelling")
	with _atomic("request_cancellation"):
		doc.cancel_requested = 1
		doc.cancel_request_reason = why
		doc.save(ignore_permissions=True)
	_notify_cancel_requested(doc, why)
	doc.reload()
	return _order_detail(doc, frappe.session.user)


@frappe.whitelist(methods=["POST"])
def resolve_cancellation_request(order: str, decision: str, reason: str | None = None) -> dict:
	"""The seller answers the buyer's cancellation request: ``accept`` cancels the order,
	``refuse`` (optional ``reason``) keeps it and clears the request."""
	decision = (decision or "").strip().lower()
	if decision not in (ACCEPT, "refuse"):
		frappe.throw(_("Choose accept or refuse."))
	doc = _lock_order(order)
	_actor(doc, (ou.SELLER,), _("Only the seller can answer a cancellation request."))
	_require_status(doc, ou.CONFIRMED)
	if not doc.cancel_requested:
		frappe.throw(_("There is no cancellation request on this order."))
	_party_deadline(doc, ou.SELLER)
	user = frappe.session.user
	if decision == ACCEPT:
		refusal = ou.cancel_refusal(doc, ou.SELLER)
		if refusal:
			frappe.throw(refusal)
		with _atomic("resolve_cancellation_request"):
			_cancel(doc, user, f"Cancelled at the buyer's request: {doc.cancel_request_reason}")
		_notify_cancelled(doc, ou.SELLER, doc.cancel_reason)
	else:
		why = _reason(reason, required=False)
		with _atomic("resolve_cancellation_request"):
			doc.cancel_requested = 0
			doc.cancel_request_reason = None
			doc.save(ignore_permissions=True)
		_notify_cancel_request_refused(doc, why)
	doc.reload()
	return _order_detail(doc, user)


@frappe.whitelist(methods=["POST"])
def request_staff_cancellation(order: str, reason: str) -> dict:
	"""After a rejected payment slip the seller can't cancel on their own (D13); they ask staff,
	with a reason. Staff and the buyer are notified."""
	doc = _lock_order(order)
	_actor(doc, (ou.SELLER,), _("Only the seller can ask staff to cancel this order."))
	_require_status(doc, ou.CONFIRMED)
	if not doc.has_submitted_proof:
		frappe.throw(_("No payment slip was submitted, so you can cancel this order yourself."))
	if doc.staff_cancel_requested:
		frappe.throw(_("You have already asked staff to cancel this order."))
	_party_deadline(doc, ou.SELLER)
	why = _reason(reason, required=True, label="a reason for cancelling")
	with _atomic("request_staff_cancellation"):
		doc.staff_cancel_requested = 1
		doc.staff_cancel_request_reason = why
		doc.save(ignore_permissions=True)
	_notify_staff_cancel_requested(doc, why)
	doc.reload()
	return _order_detail(doc, frappe.session.user)


@frappe.whitelist(methods=["POST"])
def resolve_staff_cancellation_request(order: str, decision: str, reason: str) -> dict:
	"""Staff answer a seller's request to cancel: ``cancel`` ends the order, ``refuse`` keeps it
	open. A reason is required either way and goes to the seller (and the buyer on cancel)."""
	decision = (decision or "").strip().lower()
	if decision not in ("cancel", "refuse"):
		frappe.throw(_("Choose cancel or refuse."))
	require_staff()
	doc = _lock_order(order)
	_require_status(doc, ou.CONFIRMED)
	if not doc.staff_cancel_requested:
		frappe.throw(_("There is no request to cancel this order."))
	why = _reason(reason, required=True)
	user = frappe.session.user
	if decision == "cancel":
		ou.assert_transition(doc, ou.CANCELLED, ou.STAFF)
		with _atomic("resolve_staff_cancellation_request"):
			_cancel(doc, user, why)
		_notify_cancelled(doc, ou.STAFF, why)
	else:
		with _atomic("resolve_staff_cancellation_request"):
			doc.staff_cancel_requested = 0
			doc.staff_cancel_request_reason = None
			doc.save(ignore_permissions=True)
		_notify_staff_cancel_request_refused(doc, why)
	doc.reload()
	return _order_detail(doc, user)


# -- notifications (Story 8.2) ---------------------------------------------------------------------

STAFF_DISPLAY_NAME = "Accreage Mart staff"


def _seller_display_name(profile: str) -> str:
	return frappe.db.get_value("Seller Profile", profile, "business_name") or "The seller"


def _actor_display_name(order, actor: str) -> str:
	if actor == ou.BUYER:
		return _buyer_display_name(order.buyer)
	if actor == ou.SELLER:
		return _seller_display_name(order.seller)
	return STAFF_DISPLAY_NAME


def _order_path(order, user: str) -> str:
	role = ou.viewer_role(order, user)
	if role == ou.BUYER:
		return f"/buyer/orders/{order.name}"
	if role == ou.SELLER:
		return f"/seller/orders/{order.name}"
	return "/admin/payments"


def _send(event: str, user: str | None, order, title: str, message: str, context: dict, sms: bool = False):
	"""One notification about ``order`` to ``user``. Never raises: a failure is logged, not
	surfaced, so it never undoes the action that triggered it."""
	if not user:
		return
	try:
		title_of_listing = frappe.db.get_value("Listing", order.listing, "title")
		notify(
			event,
			user,
			title,
			message,
			["email", "sms"] if sms else ["email"],
			document_type="Order",
			document_name=order.name,
			email_template=event,
			email_context={
				"full_name": frappe.db.get_value("User", user, "full_name") or "there",
				"order": order.name,
				"title": title_of_listing,
				"quantity": _qty(order.quantity),
				"unit": order.unit,
				**context,
			},
			email_cta_label="View order",
			email_cta_url=f"{frontend_url()}{_order_path(order, user)}",
		)
	except Exception:
		frappe.log_error(title=f"Order notification failed: {event}")


def _sentence(text: str | None) -> str:
	"""``text`` ending in a full stop (reasons are typed by people and often don't)."""
	text = (text or "").strip()
	return text if not text or text[-1] in ".!?" else f"{text}."


def _other_party_user(order, actor: str) -> str | None:
	return lu.seller_user_of(order.seller) if actor == ou.BUYER else order.buyer


def _what(order) -> str:
	title = frappe.db.get_value("Listing", order.listing, "title")
	return f"{_qty(order.quantity)} {order.unit} of {title}"


def _notify_countered(order, actor: str, price: float) -> None:
	name = _actor_display_name(order, actor)
	final = frappe.db.count("Order Offer", {"order": order.name}) >= ou.MAX_OFFERS
	message = (
		f"{name} countered on order {order.name} ({_what(order)}) with {_rs(price)} per {order.unit} "
		f"({_rs(order.total_price)} in total). Accept, counter or decline within {ou.CONFIRM_HOURS} hours, "
		"or the order is cancelled."
	)
	if final:
		message += f" That was offer {ou.MAX_OFFERS} of {ou.MAX_OFFERS}: you can now only accept or decline."
	_send(
		"order_offer_countered",
		_other_party_user(order, actor),
		order,
		f"New counter-offer on order {order.name}",
		message,
		{
			"actor_name": name,
			"price": _rs(price),
			"total": _rs(order.total_price),
			"hours": ou.CONFIRM_HOURS,
			"final": final,
		},
	)


def _payment_next_step(for_buyer: bool) -> str:
	if for_buyer:
		return (
			f"Pay the seller within {ou.PAYMENT_HOURS} hours and upload your payment slip, "
			"or the order is cancelled."
		)
	return f"The buyer has {ou.PAYMENT_HOURS} hours to pay and upload the payment slip."


def _notify_offer_accepted(order, actor: str, price: float) -> None:
	name = _actor_display_name(order, actor)
	recipient = _other_party_user(order, actor)
	for_buyer = actor == ou.SELLER
	_send(
		"order_offer_accepted",
		recipient,
		order,
		f"Price agreed on order {order.name}",
		f"{name} accepted {_rs(price)} per {order.unit} for order {order.name} ({_what(order)}), "
		f"{_rs(order.total_price)} in total. {_payment_next_step(for_buyer)}",
		{
			"actor_name": name,
			"price": _rs(price),
			"total": _rs(order.total_price),
			"hours": ou.PAYMENT_HOURS,
			"for_buyer": for_buyer,
		},
		sms=True,
	)


def _notify_offer_declined(order, actor: str, price: float, reason: str) -> None:
	name = _actor_display_name(order, actor)
	message = (
		f"{name} declined the offer of {_rs(price)} per {order.unit} on order {order.name} "
		f"({_what(order)}), so the order was cancelled."
	)
	if reason:
		message += f" Reason: {_sentence(reason)}"
	_send(
		"order_offer_declined",
		_other_party_user(order, actor),
		order,
		f"Offer declined - order {order.name} cancelled",
		message,
		{"actor_name": name, "price": _rs(price), "reason": reason or None},
		sms=True,
	)


def _notify_confirmed(order) -> None:
	name = _seller_display_name(order.seller)
	_send(
		"order_confirmed",
		order.buyer,
		order,
		f"Order {order.name} confirmed",
		f"{name} confirmed order {order.name} ({_what(order)}) at {_rs(order.agreed_unit_price)} per "
		f"{order.unit}, {_rs(order.total_price)} in total. {_payment_next_step(True)}",
		{
			"seller_name": name,
			"price": _rs(order.agreed_unit_price),
			"total": _rs(order.total_price),
			"hours": ou.PAYMENT_HOURS,
		},
		sms=True,
	)


def _notify_declined(order, reason: str) -> None:
	name = _seller_display_name(order.seller)
	message = f"{name} declined order {order.name} ({_what(order)})."
	if reason:
		message += f" Reason: {_sentence(reason)}"
	_send(
		"order_declined",
		order.buyer,
		order,
		f"Order {order.name} declined",
		message,
		{"seller_name": name, "reason": reason or None},
		sms=True,
	)


def _notify_cancelled(order, actor: str, reason: str) -> None:
	"""The other party hears about it; when staff cancel, both parties do."""
	name = _actor_display_name(order, actor)
	if actor == ou.STAFF:
		recipients = [order.buyer, lu.seller_user_of(order.seller)]
	else:
		recipients = [_other_party_user(order, actor)]
	message = f"{name} cancelled order {order.name} ({_what(order)})."
	if reason:
		message += f" Reason: {_sentence(reason)}"
	for user in recipients:
		_send(
			"order_cancelled",
			user,
			order,
			f"Order {order.name} cancelled",
			message,
			{"actor_name": name, "reason": reason or None},
			sms=True,
		)


def _notify_cancel_requested(order, reason: str) -> None:
	name = _buyer_display_name(order.buyer)
	_send(
		"order_cancel_requested",
		lu.seller_user_of(order.seller),
		order,
		f"Cancellation request on order {order.name}",
		f"{name} asked to cancel order {order.name} ({_what(order)}). Reason: {_sentence(reason)} "
		"Accept or refuse the request on the order page.",
		{"buyer_name": name, "reason": reason},
	)


def _notify_cancel_request_refused(order, reason: str) -> None:
	name = _seller_display_name(order.seller)
	message = (
		f"{name} refused your request to cancel order {order.name} ({_what(order)}). "
		f"The order stays open: {_payment_next_step(True)}"
	)
	if reason:
		message += f" Reason: {_sentence(reason)}"
	_send(
		"order_cancel_request_refused",
		order.buyer,
		order,
		f"Cancellation request refused - order {order.name}",
		message,
		{"seller_name": name, "reason": reason or None, "hours": ou.PAYMENT_HOURS},
	)


def _notify_staff_cancel_requested(order, reason: str) -> None:
	name = _seller_display_name(order.seller)
	context = {"seller_name": name, "reason": reason}
	for user in ou.staff_users():
		_send(
			"order_staff_cancel_requested",
			user,
			order,
			f"Seller asks staff to cancel order {order.name}",
			f"{name} asked staff to cancel order {order.name} ({_what(order)}) after a rejected payment "
			f"slip. Reason: {_sentence(reason)} Review it in the payments queue.",
			{**context, "for_staff": True},
		)
	_send(
		"order_staff_cancel_requested",
		order.buyer,
		order,
		f"The seller asked staff to cancel order {order.name}",
		f"{name} asked Accreage Mart staff to cancel order {order.name} ({_what(order)}). "
		f"Reason: {_sentence(reason)} Staff will review it. "
		"You can still upload a new payment slip before the deadline.",
		{**context, "for_staff": False},
	)


def _notify_staff_cancel_request_refused(order, reason: str) -> None:
	_send(
		"order_staff_cancel_request_refused",
		lu.seller_user_of(order.seller),
		order,
		f"Staff kept order {order.name} open",
		f"Staff refused your request to cancel order {order.name} ({_what(order)}). Reason: {_sentence(reason)}",
		{"reason": reason},
	)
