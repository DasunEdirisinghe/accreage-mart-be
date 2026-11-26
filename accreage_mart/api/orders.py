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

Guard matrix (Story 8.3)
------------------------
=======================  ==================  ==================================================
Endpoint                 Caller              Notes
=======================  ==================  ==================================================
upload_payment_proof     buyer               confirmed only, before the deadline (a flagged order
                                             has none); multipart ``file`` JPG/PNG/PDF <= 5 MB,
                                             checked by content, stored private and attached to
                                             the proof; amount, date, bank, reference required
                                             -> payment_review. Notifies the seller and staff
review_payment_proof     seller / staff      the Submitted slip of a payment_review order; first
                                             decision wins ("Already reviewed"). reject needs a
                                             category + reason -> confirmed, fresh 72 h; approve
                                             -> paid, stock - quantity floored at 0 (D11)
approve_flagged_payment  staff / admin       confirmed + needs_staff_review (D13): staff decide
                                             the payment arrived -> paid, same stock rule;
                                             reason required
list_pending_proofs      staff / admin       the payments queue, oldest first
get_order_stats          seller / staff      counts by status + sales; seller scope or platform
=======================  ==================  ==================================================

Guard matrix (Story 8.4)
------------------------
=================  ======================  ==============================================================
Endpoint           Caller                  Notes
=================  ======================  ==============================================================
mark_delivered     seller                  paid only -> delivered; the buyer gets 7 days to confirm
confirm_received   buyer                   delivered only, before the 7-day deadline -> completed;
                                           invoice number INV-.YYYY.-.##### + PDF from the "Order
                                           Invoice" Print Format, attached once as a private File
get_invoice        buyer / seller / staff  completed only; downloads the PDF (made on demand if the
                                           render at completion failed)
=================  ======================  ==============================================================

Timers and auction-win orders (Story 8.5)
-----------------------------------------
``enforce_order_timers`` (scheduler, every 5 minutes) applies each expired deadline once, under the
Order row lock: 48 h pending_confirmation / open offer -> cancelled (offer Expired); 72 h confirmed ->
cancelled if no slip was ever submitted, else ``needs_staff_review`` + staff, buyer and seller notified
(D13); 7 days delivered -> completed with the invoice. ``create_auction_order`` (called by
``api.auctions._close_one``) makes the winner's confirmed order - full lot, winning bid, one per
auction. An order whose seller has no bank details gets no payment clock (D16) until
``update_my_bank_details`` starts it (``start_pending_payment_clocks``).

``complete_order(doc, actor)`` is the shared completion step (buyer confirm, and the 8.5 auto-complete
with actor ``system``).

Every order payload carries ``status_deadline``, ``action_by`` (buyer / seller / staff / none) and
``on_expiry`` (cancel / staff_review / auto_complete / null) so both parties can show the same
countdown (D14).
"""

import uuid
from contextlib import contextmanager

import frappe
from frappe import _
from frappe.query_builder.functions import Count, Sum
from frappe.utils import cint, flt, getdate, now_datetime, nowdate

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
	"staff_payment_approved_by",
	"staff_payment_approval_reason",
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
	# A locking read sees the latest committed row even inside an older REPEATABLE READ snapshot.
	return frappe.get_doc("Listing", name, for_update=True)


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
	if role != ou.STAFF:
		data.pop("staff_payment_approved_by")  # parties see the reason, not which staff member
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


PROOF_FIELDS = (
	"name",
	"order",
	"file",
	"status",
	"submitted_on",
	"amount_paid",
	"paid_on",
	"paid_from_bank",
	"transfer_reference",
	"note",
	"reviewed_by",
	"reviewed_on",
	"reject_category",
	"reject_reason",
)


def _proof_payload(row, order) -> dict:
	"""One slip as the parties see it: ``reviewer_role`` (seller / staff) instead of the reviewer's
	user id, and ``amount_matches`` so the reviewer sees a paid amount that differs from the
	order total (highlighted, never blocked - D10)."""
	data = {field: row.get(field) for field in PROOF_FIELDS}
	data["reviewer_role"] = ou.reviewer_role(row, order)
	data.pop("reviewed_by")
	data["expected_amount"] = flt(order.get("total_price"))
	data["amount_matches"] = flt(row.get("amount_paid"), 2) == flt(order.get("total_price"), 2)
	return data


def _payment_proofs(order) -> list[dict]:
	rows = frappe.get_all(
		"Payment Proof",
		filters={"order": order.get("name")},
		fields=list(PROOF_FIELDS),
		order_by="submitted_on desc, creation desc",
		ignore_permissions=True,
	)
	return [_proof_payload(row, order) for row in rows]


def _order_detail(order, viewer: str) -> dict:
	data = _order_summary(order, viewer)
	data["offers"] = _offers(order.name)
	data["payment_proofs"] = _payment_proofs(order)

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
	start_pending_payment_clocks(profile)
	return _bank_payload(profile)


# -- shared guards for party actions (Story 8.2) ---------------------------------------------------

REASON_MAX = 1000
PARTY_OF = {ou.BUYER: ou.PARTY_BUYER, ou.SELLER: ou.PARTY_SELLER}


def _lock_order(name: str):
	"""Lock the Order row and return it fresh, so two people acting on the same order at once
	are serialised (same pattern as ``place_bid`` in ``api/auctions.py``)."""
	if not name or not frappe.db.exists("Order", name):
		frappe.throw(_("Order not found."), frappe.DoesNotExistError)
	# ``for_update`` loads the row with a locking read, so the doc reflects whatever a concurrent
	# request committed while we waited for the lock (a plain read could return the transaction's
	# older REPEATABLE READ snapshot).
	return frappe.get_doc("Order", name, for_update=True)


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


def payment_deadline(seller_profile: str, now):
	"""The 72 h payment clock for a freshly ``confirmed`` order - or None while the seller has no
	bank details (D16: only auction-win orders can get there; the clock starts in
	``update_my_bank_details``)."""
	return ou.deadline_for(ou.CONFIRMED, now) if ou.has_bank_details(seller_profile) else None


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


# -- payment proof upload and review (Story 8.3) ---------------------------------------------------

SLIP_FIELD = "file"
APPROVE = "approve"
REJECT = "reject"
NOTE_MAX = 1000


def _uploaded_slip() -> bytes:
	"""The multipart ``file`` field, read one byte past the limit so an oversized slip is caught
	without loading all of it."""
	upload = (frappe.request.files if frappe.request else {}).get(SLIP_FIELD)
	if not upload:
		frappe.throw(_("Please attach your payment slip."))
	return upload.stream.read(ou.MAX_SLIP_BYTES + 1)


def _payment_details(order, amount_paid, paid_on, paid_from_bank, transfer_reference, note) -> dict:
	"""The details the buyer types next to the slip (D10), checked and trimmed."""
	amount = flt(amount_paid, 2)
	if amount <= 0:
		frappe.throw(_("Enter the amount you paid."))
	if paid_on in (None, ""):
		frappe.throw(_("Enter the date you paid."))
	try:
		date = getdate(paid_on)
	except Exception:
		frappe.throw(_("Enter a valid payment date."))
	if date > getdate(nowdate()):
		frappe.throw(_("The payment date can't be in the future."))
	if date < getdate(order.creation):
		frappe.throw(_("The payment date can't be before the order was placed."))

	bank = (paid_from_bank or "").strip()
	reference = (transfer_reference or "").strip()
	if not bank:
		frappe.throw(_("Enter the bank you paid from."))
	if not reference:
		frappe.throw(_("Enter the transfer reference from your bank slip."))
	if len(bank) > ou.DETAIL_MAX:
		frappe.throw(_("The bank name is too long."))
	if len(reference) > ou.DETAIL_MAX:
		frappe.throw(_("The transfer reference is too long."))
	note = (note or "").strip()
	if len(note) > NOTE_MAX:
		frappe.throw(_("Please keep the note under {0} characters.").format(NOTE_MAX))
	return {
		"amount_paid": amount,
		"paid_on": date,
		"paid_from_bank": bank,
		"transfer_reference": reference,
		"note": note or None,
	}


@frappe.whitelist(methods=["POST"])
def upload_payment_proof(
	order: str,
	amount_paid: float | None = None,
	paid_on: str | None = None,
	paid_from_bank: str | None = None,
	transfer_reference: str | None = None,
	note: str | None = None,
) -> dict:
	"""The buyer's bank slip (multipart field ``file``: JPG, PNG or PDF up to 5 MB) plus the
	payment details, on a ``confirmed`` order. The slip is stored as a private file only the buyer,
	the seller and staff can open; the order moves to ``payment_review``."""
	doc = _lock_order(order)
	_actor(doc, (ou.BUYER,), _("Only the buyer can upload a payment slip."))
	if doc.status == ou.PAYMENT_REVIEW:
		frappe.throw(_("Your payment slip is already waiting for review."))
	_require_status(doc, ou.CONFIRMED)
	if not doc.needs_staff_review:
		# A flagged order (D13) has no clock any more: the buyer may still send a new slip.
		_party_deadline(doc, ou.BUYER)
	details = _payment_details(doc, amount_paid, paid_on, paid_from_bank, transfer_reference, note)
	content = _uploaded_slip()
	kind = ou.validate_slip(content)
	ou.assert_transition(doc, ou.PAYMENT_REVIEW, ou.BUYER)

	now = now_datetime()
	with _atomic("upload_payment_proof"):
		# A random name: the buyer's own filename can carry personal information.
		file = frappe.get_doc(
			{
				"doctype": "File",
				"file_name": f"{ou.SLIP_FILE_PREFIX}{uuid.uuid4().hex}.{ou.SLIP_EXTENSIONS[kind]}",
				"content": content,
				"is_private": 1,
			}
		).insert(ignore_permissions=True)
		proof = frappe.get_doc(
			{
				"doctype": "Payment Proof",
				"order": doc.name,
				"file": file.file_url,
				"status": ou.PROOF_SUBMITTED,
				"submitted_on": now,
				**details,
			}
		).insert(ignore_permissions=True)
		# Attached to the proof, the private file is readable exactly by who may read the proof
		# (Frappe's File permission defers to the attached document). Frappe also lets a File's
		# owner write/delete it whatever the attached document says, so the slip is owned by the
		# system, never the buyer (D13). insert() always stamps the session user, hence here.
		frappe.db.set_value(
			"File",
			file.name,
			{
				"attached_to_doctype": "Payment Proof",
				"attached_to_name": proof.name,
				"attached_to_field": "file",
				"owner": "Administrator",
			},
			update_modified=False,
		)
		doc.status = ou.PAYMENT_REVIEW
		doc.status_deadline = None
		doc.has_submitted_proof = 1  # never cleared (D13)
		doc.needs_staff_review = 0
		# A pending buyer cancellation request is moot once a slip is in. A seller's request to
		# staff stays: staff still see it if this slip is rejected too.
		doc.cancel_requested = 0
		doc.cancel_request_reason = None
		doc.save(ignore_permissions=True)
	_notify_proof_submitted(doc, proof)
	doc.reload()
	return _order_detail(doc, frappe.session.user)


def _reject_category(value: str | None) -> str:
	value = (value or "").strip()
	for category in ou.REJECT_CATEGORIES:
		if value.lower() == category.lower():
			return category
	frappe.throw(_("Choose why the slip is rejected: {0}.").format(" or ".join(ou.REJECT_CATEGORIES)))


def _mark_paid(order, now) -> dict:
	"""Move a locked order to ``paid`` and take its quantity off the listing's stock under the
	Listing row lock (Order row first, then Listing - the same order as accept). Stock is floored
	at 0 and never blocks the approval (D11). Returns the stock change for the response."""
	listing = _lock_listing(order.listing)
	before = flt(listing.quantity_available)
	after, shortfall = ou.stock_after_payment(before, order.quantity)
	# set_value, not listing.save(): a seller-side listing rule (e.g. min order above the stock
	# left) must never block a payment approval.
	frappe.db.set_value("Listing", listing.name, "quantity_available", after)

	order.status = ou.PAID
	order.paid_on = now
	order.status_deadline = None
	# Flags and staff requests are moot once the payment is approved; reasons stay as history.
	order.needs_staff_review = 0
	order.staff_cancel_requested = 0
	order.cancel_requested = 0
	order.save(ignore_permissions=True)
	return {
		"listing": listing.name,
		"unit": order.unit,
		"stock_before": before,
		"quantity": flt(order.quantity),
		"stock_after": after,
		"shortfall": shortfall,
		"low_stock_level": flt(listing.low_stock_level),
	}


def _after_stock_drop(stock_update: dict) -> None:
	"""Low-stock alert to the seller when the approval took the listing under its level. Never
	raises: the approval is already committed."""
	try:
		level = stock_update["low_stock_level"]
		if level > 0 and stock_update["stock_before"] >= level > stock_update["stock_after"]:
			from accreage_mart.utils.listing_notify import notify_low_stock

			notify_low_stock(frappe.get_doc("Listing", stock_update["listing"]))
	except Exception:
		frappe.log_error(title="Low stock notification after payment approval failed")


def _paid_payload(order, user: str, stock_update: dict | None) -> dict:
	data = _order_detail(order, user)
	if stock_update:
		stock_update = {k: v for k, v in stock_update.items() if k not in ("listing", "low_stock_level")}
	data["stock_update"] = stock_update
	return data


@frappe.whitelist(methods=["POST"])
def review_payment_proof(
	proof: str, decision: str, reject_category: str | None = None, reject_reason: str | None = None
) -> dict:
	"""The seller (or staff, as a fallback) approves or rejects the waiting slip (D6). The first
	decision wins: both reviewers lock the order row, and the second sees "Already reviewed".

	- ``reject`` needs ``reject_category`` (see ``REJECT_CATEGORIES``) and ``reject_reason``; the
	  order goes back to ``confirmed`` with a fresh 72 h for a new slip.
	- ``approve`` moves the order to ``paid`` and takes its quantity off the listing's stock,
	  floored at 0; the response's ``stock_update`` reports any shortfall (D11).
	"""
	decision = (decision or "").strip().lower()
	if decision not in (APPROVE, REJECT):
		frappe.throw(_("Choose approve or reject."))
	order_name = frappe.db.get_value("Payment Proof", proof, "order") if proof else None
	if not order_name:
		frappe.throw(_("Payment slip not found."), frappe.DoesNotExistError)

	doc = _lock_order(order_name)
	actor = _actor(doc, (ou.SELLER, ou.STAFF), _("Only the seller or staff can review a payment slip."))
	slip = frappe.get_doc("Payment Proof", proof, for_update=True)
	if slip.status != ou.PROOF_SUBMITTED:
		frappe.throw(_("Already reviewed: this payment slip was {0}.").format(slip.status.lower()))
	_require_status(doc, ou.PAYMENT_REVIEW)
	_party_deadline(doc, actor)
	user = frappe.session.user
	now = now_datetime()

	if decision == REJECT:
		category = _reject_category(reject_category)
		why = _reason(reject_reason, required=True, label="a reason for rejecting the slip")
		ou.assert_transition(doc, ou.CONFIRMED, actor)
		with _atomic("review_payment_proof"):
			slip.update(
				{
					"status": ou.PROOF_REJECTED,
					"reviewed_by": user,
					"reviewed_on": now,
					"reject_category": category,
					"reject_reason": why,
				}
			)
			slip.save(ignore_permissions=True)
			doc.status = ou.CONFIRMED
			doc.status_deadline = payment_deadline(doc.seller, now)
			doc.save(ignore_permissions=True)
		_notify_proof_rejected(doc, actor, category, why)
		doc.reload()
		return _paid_payload(doc, user, None)

	ou.assert_transition(doc, ou.PAID, actor)
	with _atomic("review_payment_proof"):
		slip.update({"status": ou.PROOF_APPROVED, "reviewed_by": user, "reviewed_on": now})
		slip.save(ignore_permissions=True)
		stock_update = _mark_paid(doc, now)
	_after_stock_drop(stock_update)
	_notify_payment_approved(doc, actor)
	doc.reload()
	return _paid_payload(doc, user, stock_update)


@frappe.whitelist(methods=["POST"])
def approve_flagged_payment(order: str, reason: str) -> dict:
	"""D13 - a rejected slip was not replaced within 72 h, so the order is flagged for staff.
	Staff who find the payment did arrive approve it here: the order moves to ``paid`` with the
	same stock rule as a slip approval. A reason is required; it is stored on the order
	(``staff_payment_approved_by`` / ``staff_payment_approval_reason``, unchangeable once set) and sent
	to both parties. (The rejected slips stay rejected - reviewed proofs never change.)"""
	require_staff()
	doc = _lock_order(order)
	_require_status(doc, ou.CONFIRMED)
	if not doc.needs_staff_review:
		frappe.throw(_("This order is not waiting for a staff decision."))
	why = _reason(reason, required=True, label="a reason for approving the payment")
	ou.assert_transition(doc, ou.PAID, ou.STAFF)
	user = frappe.session.user
	with _atomic("approve_flagged_payment"):
		doc.staff_payment_approved_by = user
		doc.staff_payment_approval_reason = why
		stock_update = _mark_paid(doc, now_datetime())  # saves the order with the approval
	_after_stock_drop(stock_update)
	_notify_payment_approved(doc, ou.STAFF, reason=why)
	doc.reload()
	return _paid_payload(doc, user, stock_update)


# -- staff payments queue (Story 8.3, used by 8.8) -------------------------------------------------

QUEUE_LIMIT = 100
QUEUE_LIMIT_MAX = 500


def _latest_proof(order) -> dict | None:
	row = frappe.get_all(
		"Payment Proof",
		filters={"order": order.get("name")},
		fields=list(PROOF_FIELDS),
		order_by="submitted_on desc, creation desc",
		limit_page_length=1,
		ignore_permissions=True,
	)
	return _proof_payload(row[0], order) if row else None


def _queue_orders(filters: dict, limit: int, viewer: str) -> dict:
	total = frappe.db.count("Order", filters)
	rows = frappe.get_all(
		"Order",
		filters=filters,
		fields=list(ORDER_LIST_FIELDS),
		order_by="modified asc, name asc",  # oldest first
		limit_page_length=limit,
		ignore_permissions=True,
	)
	items = [{"order": _order_summary(row, viewer), "latest_proof": _latest_proof(row)} for row in rows]
	return {"items": items, "total": total}


@frappe.whitelist()
def list_pending_proofs(limit: int = QUEUE_LIMIT) -> dict:
	"""The staff payments queue, oldest first:

	- ``awaiting_review`` - slips waiting for a decision (staff may review them, D6);
	- ``needs_staff_review`` - confirmed orders whose rejected slip was not replaced in time (D13):
	  ``approve_flagged_payment`` or ``cancel_order``;
	- ``staff_cancel_requests`` - sellers asking staff to cancel after a rejected slip:
	  ``resolve_staff_cancellation_request``.
	"""
	require_staff()
	viewer = frappe.session.user
	limit = max(1, min(cint(limit) or QUEUE_LIMIT, QUEUE_LIMIT_MAX))

	waiting = frappe.get_all(
		"Order",
		filters={"status": ou.PAYMENT_REVIEW},
		fields=list(ORDER_LIST_FIELDS),
		ignore_permissions=True,
	)
	by_name = {row.name: row for row in waiting}
	proofs = frappe.get_all(
		"Payment Proof",
		filters={"status": ou.PROOF_SUBMITTED, "order": ["in", list(by_name) or [""]]},
		fields=list(PROOF_FIELDS),
		order_by="submitted_on asc, creation asc",
		ignore_permissions=True,
	)
	awaiting = [
		{
			"proof": _proof_payload(proof, by_name[proof.order]),
			"order": _order_summary(by_name[proof.order], viewer),
		}
		for proof in proofs[:limit]
	]

	flagged = _queue_orders({"status": ou.CONFIRMED, "needs_staff_review": 1}, limit, viewer)
	requests = _queue_orders({"status": ou.CONFIRMED, "staff_cancel_requested": 1}, limit, viewer)
	return {
		"awaiting_review": {"items": awaiting, "total": len(proofs)},
		"needs_staff_review": flagged,
		"staff_cancel_requests": requests,
		"counts": {
			"awaiting_review": len(proofs),
			"needs_staff_review": flagged["total"],
			"staff_cancel_requests": requests["total"],
		},
	}


# -- order stats (D12) -----------------------------------------------------------------------------

SCOPE_SELLER = "seller"
SCOPE_PLATFORM = "platform"
SALE_STATUSES = (ou.PAID, ou.DELIVERED, ou.COMPLETED)  # payment approved


@frappe.whitelist()
def get_order_stats(scope: str | None = None) -> dict:
	"""Order counts by status and sales figures (D12). ``scope`` is ``seller`` (orders on the
	caller's listings) or ``platform`` (every order, staff only); by default staff get the platform
	and sellers their own.

	``completed_sales`` / ``completed_orders`` count ``completed`` orders only (the sales figure);
	``paid_value`` adds orders whose payment is approved but not yet completed.
	"""
	user = _require_login()
	staff = lu.is_staff_or_admin(user)
	scope = (scope or "").strip().lower() or (SCOPE_PLATFORM if staff else SCOPE_SELLER)
	if scope not in (SCOPE_SELLER, SCOPE_PLATFORM):
		frappe.throw(_("Choose the seller or platform scope."))

	order = frappe.qb.DocType("Order")
	query = (
		frappe.qb.from_(order)
		.select(order.status, Count(order.name).as_("orders"), Sum(order.total_price).as_("value"))
		.groupby(order.status)
	)
	if scope == SCOPE_PLATFORM:
		if not staff:
			frappe.throw(_("Only staff can see platform-wide order figures."), frappe.PermissionError)
	else:
		profile = lu.seller_profile_of(user)
		if not profile or not frappe.db.get_value("Seller Profile", profile, "verified"):
			frappe.throw(_("Only verified sellers have order figures."), frappe.PermissionError)
		query = query.where(order.seller == profile)

	rows = {row.status: row for row in query.run(as_dict=True)}
	counts = {status: cint(rows[status].orders) if status in rows else 0 for status in ou.STATUSES}

	def value_of(*statuses) -> float:
		return sum(flt(rows[s].value) for s in statuses if s in rows)

	return {
		"scope": scope,
		"counts": counts,
		"total_orders": sum(counts.values()),
		"active_orders": sum(counts[s] for s in ou.ACTIVE_STATUSES),
		"awaiting_payment_review": counts[ou.PAYMENT_REVIEW],
		"completed_orders": counts[ou.COMPLETED],
		"completed_sales": value_of(ou.COMPLETED),
		"paid_orders": sum(counts[s] for s in SALE_STATUSES),
		"paid_value": value_of(*SALE_STATUSES),
	}


# -- notifications (Story 8.3) ---------------------------------------------------------------------


def _slip_summary(proof, order) -> str:
	summary = (
		f"{_rs(proof.amount_paid)} paid on {frappe.utils.formatdate(proof.paid_on)} from {proof.paid_from_bank} "
		f"(reference {proof.transfer_reference})."
	)
	if flt(proof.amount_paid, 2) != flt(order.total_price, 2):
		summary += f" This differs from the order total of {_rs(order.total_price)}."
	return summary


def _notify_proof_submitted(order, proof) -> None:
	name = _buyer_display_name(order.buyer)
	differs = flt(proof.amount_paid, 2) != flt(order.total_price, 2)
	context = {
		"buyer_name": name,
		"amount_paid": _rs(proof.amount_paid),
		"paid_on": frappe.utils.formatdate(proof.paid_on),
		"paid_from_bank": proof.paid_from_bank,
		"transfer_reference": proof.transfer_reference,
		"total": _rs(order.total_price),
		"amount_differs": differs,
	}
	base = f"{name} uploaded a payment slip for order {order.name} ({_what(order)}): {_slip_summary(proof, order)}"
	_send(
		"payment_proof_submitted",
		lu.seller_user_of(order.seller),
		order,
		f"Payment slip to review - order {order.name}",
		f"{base} Check that the money reached your account, then approve or reject the slip.",
		{**context, "for_staff": False},
	)
	for user in ou.staff_users():
		_send(
			"payment_proof_submitted",
			user,
			order,
			f"New payment slip - order {order.name}",
			f"{base} The seller reviews it first; you can review it in the payments queue.",
			{**context, "for_staff": True},
		)


def _notify_proof_rejected(order, actor: str, category: str, reason: str) -> None:
	who = "Accreage Mart staff" if actor == ou.STAFF else _seller_display_name(order.seller)
	context = {
		"reviewer_name": who,
		"reject_category": category,
		"reason": reason,
		"hours": ou.PAYMENT_HOURS,
	}
	_send(
		"payment_proof_rejected",
		order.buyer,
		order,
		f"Payment slip rejected - order {order.name}",
		f"{who} rejected your payment slip for order {order.name} ({_what(order)}). "
		f"Why: {category}. {_sentence(reason)} Upload a new slip within {ou.PAYMENT_HOURS} hours.",
		{**context, "for_seller": False},
	)
	if actor == ou.STAFF:
		_send(
			"payment_proof_rejected",
			lu.seller_user_of(order.seller),
			order,
			f"Staff rejected the payment slip - order {order.name}",
			f"Accreage Mart staff rejected the buyer's payment slip for order {order.name} ({_what(order)}). "
			f"Why: {category}. {_sentence(reason)} The buyer has {ou.PAYMENT_HOURS} hours to upload a new one.",
			{**context, "for_seller": True},
		)


def _notify_payment_approved(order, actor: str, reason: str | None = None) -> None:
	"""To the buyer; to the seller too when staff made the call (the seller must now deliver)."""
	who = "Accreage Mart staff" if actor == ou.STAFF else _seller_display_name(order.seller)
	context = {"reviewer_name": who, "total": _rs(order.total_price), "reason": reason or None}
	message = (
		f"{who} approved your payment of {_rs(order.total_price)} for order {order.name} ({_what(order)})."
	)
	if reason:
		message += f" Note: {_sentence(reason)}"
	_send(
		"payment_proof_approved",
		order.buyer,
		order,
		f"Payment approved - order {order.name}",
		f"{message} The seller will now arrange delivery.",
		{**context, "for_seller": False},
		sms=True,
	)
	if actor == ou.STAFF:
		seller_message = (
			f"Accreage Mart staff approved the buyer's payment for order {order.name} ({_what(order)})."
		)
		if reason:
			seller_message += f" Note: {_sentence(reason)}"
		_send(
			"payment_proof_approved",
			lu.seller_user_of(order.seller),
			order,
			f"Payment approved by staff - order {order.name}",
			f"{seller_message} Please arrange delivery and mark the order delivered.",
			{**context, "for_seller": True},
			sms=True,
		)


# -- delivery, completion and invoice (Story 8.4) --------------------------------------------------


@frappe.whitelist(methods=["POST"])
def mark_delivered(order: str) -> dict:
	"""The seller says the goods were handed over (only once the payment is approved, ``paid``).
	The buyer then has 7 days to confirm receipt, after which the order completes by itself."""
	doc = _lock_order(order)
	_actor(doc, (ou.SELLER,), _("Only the seller can mark this order delivered."))
	if doc.status == ou.DELIVERED:
		frappe.throw(_("This order is already marked delivered."))
	_require_status(doc, ou.PAID)
	_party_deadline(doc, ou.SELLER)
	ou.assert_transition(doc, ou.DELIVERED, ou.SELLER)
	now = now_datetime()
	with _atomic("mark_delivered"):
		doc.status = ou.DELIVERED
		doc.delivered_on = now
		doc.status_deadline = ou.deadline_for(ou.DELIVERED, now)
		doc.save(ignore_permissions=True)
	_notify_delivered(doc)
	doc.reload()
	return _order_detail(doc, frappe.session.user)


@frappe.whitelist(methods=["POST"])
def confirm_received(order: str) -> dict:
	"""The buyer confirms the goods arrived: the order is ``completed`` and its invoice is made."""
	doc = _lock_order(order)
	_actor(doc, (ou.BUYER,), _("Only the buyer can confirm receipt of this order."))
	if doc.status == ou.COMPLETED:
		frappe.throw(_("This order is already completed."))
	_require_status(doc, ou.DELIVERED)
	_party_deadline(doc, ou.BUYER)
	complete_order(doc, ou.BUYER)
	doc.reload()
	return _order_detail(doc, frappe.session.user)


def complete_order(doc, actor: str) -> None:
	"""Move a row-locked ``delivered`` order to ``completed`` and make its invoice.

	``actor`` is ``buyer`` (confirm_received) or ``system`` (the 7-day auto-complete, Story 8.5 - it
	should lock the order with ``_lock_order`` and re-check the status/deadline first). Commits.

	The status change and the invoice number commit together. The PDF is rendered right after, in
	its own step: if rendering fails, the order still completes (the failure is logged) and the
	invoice is made on the first ``get_invoice`` instead - never twice (``ensure_invoice``).
	Both parties are notified."""
	ou.assert_transition(doc, ou.COMPLETED, actor)
	from accreage_mart.utils import invoice as inv

	with _atomic("complete_order"):
		doc.status = ou.COMPLETED
		doc.completed_on = now_datetime()
		doc.status_deadline = None
		if not doc.invoice_no:
			doc.invoice_no = inv.new_invoice_no()
		doc.save(ignore_permissions=True)
	try:
		ensure_invoice(doc.name)
	except Exception:
		frappe.log_error(title=f"Invoice for order {doc.name} failed")
	doc.reload()
	_notify_completed(doc, actor)


def ensure_invoice(order_name: str):
	"""The completed order's invoice PDF, made now if it doesn't exist yet - exactly once: the
	Order row is locked and ``invoice_file`` re-checked under the lock. Commits. Returns the
	(locked, fresh) Order."""
	from accreage_mart.utils import invoice as inv

	doc = _lock_order(order_name)
	if doc.invoice_file:
		frappe.db.commit()  # release the row lock
		return doc
	with _atomic("ensure_invoice"):
		inv.attach_invoice(doc)
	return doc


@frappe.whitelist()
def get_invoice(order: str):
	"""Download the order's invoice PDF - for its buyer, its seller and staff only, once the order
	is completed."""
	from accreage_mart.utils import invoice as inv

	doc = _get_order_doc(order)
	_actor(doc, (ou.BUYER, ou.SELLER, ou.STAFF), _("You do not have access to this invoice."))
	if doc.status != ou.COMPLETED:
		frappe.throw(_("The invoice is ready once the order is completed."))
	if not doc.invoice_file:
		doc = ensure_invoice(doc.name)
	file = inv.invoice_file_doc(doc)
	if not file:
		frappe.throw(_("The invoice file is missing. Please contact staff."), frappe.DoesNotExistError)
	frappe.local.response.filename = f"{doc.invoice_no}.pdf"
	frappe.local.response.filecontent = file.get_content()
	frappe.local.response.type = "download"
	frappe.local.response.content_type = "application/pdf"


# -- notifications (Story 8.4) ---------------------------------------------------------------------


def _notify_delivered(order) -> None:
	name = _seller_display_name(order.seller)
	days = ou.DELIVERY_CONFIRM_DAYS
	_send(
		"order_delivered",
		order.buyer,
		order,
		f"Order {order.name} delivered",
		f"{name} marked order {order.name} ({_what(order)}) as delivered. Please confirm you received the "
		f"goods within {days} days; if you don't, the order is completed automatically.",
		{"seller_name": name, "days": days},
		sms=True,
	)


def _notify_completed(order, actor: str) -> None:
	"""Both parties: the order is completed and the invoice is on the order page."""
	auto = actor == ou.SYSTEM
	buyer_name = _buyer_display_name(order.buyer)
	seller_name = _seller_display_name(order.seller)
	if auto:
		how = (
			f"was completed automatically {ou.DELIVERY_CONFIRM_DAYS} days after delivery, "
			"as receipt was not confirmed"
		)
	else:
		how = f"was completed: {buyer_name} confirmed receiving the goods"
	base = f"Order {order.name} ({_what(order)}, {_rs(order.total_price)}) {how}."
	context = {
		"auto": auto,
		"buyer_name": buyer_name,
		"seller_name": seller_name,
		"total": _rs(order.total_price),
		"invoice_no": order.invoice_no,
		"days": ou.DELIVERY_CONFIRM_DAYS,
	}
	invoice = f" Invoice {order.invoice_no} is ready to download on the order page."
	_send(
		"order_completed",
		order.buyer,
		order,
		f"Order {order.name} completed",
		base + invoice,
		{**context, "for_seller": False},
		sms=True,
	)
	_send(
		"order_completed",
		lu.seller_user_of(order.seller),
		order,
		f"Order {order.name} completed",
		base + invoice,
		{**context, "for_seller": True},
		sms=True,
	)


# -- timers and auction-win orders (Story 8.5) -----------------------------------------------------

# Statuses that carry a timer the job enforces (payment_review and paid never do).
TIMED_STATUSES = (ou.PENDING_CONFIRMATION, ou.NEGOTIATING, ou.CONFIRMED, ou.DELIVERED)


def enforce_order_timers() -> None:
	"""Scheduled every 5 minutes (see ``hooks.py``). One pass over every order whose
	``status_deadline`` has passed (D4, D13, D14):

	- ``pending_confirmation`` / ``negotiating`` past 48 h -> cancelled (an open offer -> Expired);
	- ``confirmed`` past 72 h -> cancelled if no slip was ever submitted, otherwise flagged
	  ``needs_staff_review`` (no clock any more; staff approve a payment or cancel) - D13;
	- ``delivered`` past 7 days -> completed with its invoice.

	Idempotent: each order is handled under its row lock after re-checking status and deadline, and
	every outcome clears or replaces the deadline, so a re-run (or an overlapping run) finds nothing to
	do. An order with no deadline (e.g. an auction-win order waiting for the seller's bank details,
	D16) is never touched. One order's failure is logged and never stops the others."""
	names = frappe.get_all(
		"Order",
		filters={"status": ["in", TIMED_STATUSES], "status_deadline": ["<", now_datetime()]},
		pluck="name",
		order_by="status_deadline asc",
	)
	for name in names:
		try:
			expire_order(name)
		except Exception:
			frappe.db.rollback()
			frappe.log_error(title=f"Order timer failed for {name}")


def expire_order(name: str) -> str | None:
	"""Apply the expiry rule to one order if its deadline really passed. Returns what was done
	(``cancelled`` / ``flagged`` / ``completed``) or None. Commits."""
	doc = _lock_order(name)
	now = now_datetime()
	if doc.status not in TIMED_STATUSES or not ou.deadline_passed(doc, now):
		frappe.db.commit()  # someone acted (or the job already ran) while we waited: release the lock
		return None

	if doc.status == ou.DELIVERED:
		complete_order(doc, ou.SYSTEM)
		return "completed"

	if doc.status == ou.CONFIRMED and doc.has_submitted_proof:
		if doc.needs_staff_review:
			frappe.db.commit()
			return None
		with _atomic("expire_order"):
			doc.needs_staff_review = 1
			doc.status_deadline = None
			doc.save(ignore_permissions=True)
		_notify_payment_flagged(doc)
		return "flagged"

	why = _expiry_reason(doc)
	ou.assert_transition(doc, ou.CANCELLED, ou.SYSTEM)
	with _atomic("expire_order"):
		if doc.status == ou.NEGOTIATING:
			_close_open_offers(doc.name, ou.OFFER_EXPIRED)
		_cancel(doc, None, f"Cancelled automatically: {why}.")
	_notify_expired(doc, why)
	return "cancelled"


def _expiry_reason(order) -> str:
	if order.status == ou.PENDING_CONFIRMATION:
		return f"the seller did not confirm the order within {ou.CONFIRM_HOURS} hours"
	if order.status == ou.NEGOTIATING:
		party = ou.open_offer_party(order.name)
		if party == ou.PARTY_BUYER:
			return f"the seller did not answer the buyer's offer within {ou.CONFIRM_HOURS} hours"
		if party == ou.PARTY_SELLER:
			return f"the buyer did not answer the seller's offer within {ou.CONFIRM_HOURS} hours"
		return f"the price negotiation got no answer within {ou.CONFIRM_HOURS} hours"
	return f"no payment slip was uploaded within {ou.PAYMENT_HOURS} hours"


def create_auction_order(auction_name: str, winner: str, amount: float):
	"""D8 - the winner's ``confirmed`` order when an auction closes: the full lot (the listing's
	``quantity_available`` at close), the winning bid as the unit price, ``source = Auction``.

	Idempotent: the Auction row is locked and an existing order for it is returned instead of a
	second one. Without the seller's bank details the order gets no payment clock (D16).
	Returns ``(order, created)``. Commits."""
	frappe.get_doc("Auction", auction_name, for_update=True)  # serialise overlapping closers
	existing = frappe.db.get_value("Order", {"auction": auction_name}, "name")
	if existing:
		frappe.db.commit()
		return frappe.get_doc("Order", existing), False

	listing_name = frappe.db.get_value("Listing", {"auction": auction_name}, "name")
	listing = _lock_listing(listing_name)
	ou.assert_transition({"status": None}, ou.CONFIRMED, ou.SYSTEM)
	now = now_datetime()
	quantity = flt(listing.quantity_available)
	price = flt(amount)
	with _atomic("create_auction_order"):
		order = frappe.get_doc(
			{
				"doctype": "Order",
				"listing": listing.name,
				"seller": listing.seller,
				"buyer": winner,
				"source": ou.SOURCE_AUCTION,
				"auction": auction_name,
				"status": ou.CONFIRMED,
				"status_deadline": payment_deadline(listing.seller, now),
				"quantity": quantity,
				"unit": listing.unit,
				# An auction listing has no listed price; the lot sold at the winning bid.
				"listed_unit_price": price,
				"agreed_unit_price": price,
				"total_price": quantity * price,
				"offer_count": 0,
				"confirmed_on": now,
			}
		).insert(ignore_permissions=True)
	_notify_auction_order(order)
	return order, True


def start_pending_payment_clocks(seller_profile: str) -> list[str]:
	"""D16 - the seller just saved complete bank details: every ``confirmed`` order of theirs that was
	waiting for them (no deadline, not flagged for staff) gets its 72 h payment clock now, and each
	buyer is told where to pay. Returns the order names. Commits per order."""
	if not ou.has_bank_details(seller_profile):
		return []
	names = frappe.get_all(
		"Order",
		filters={
			"seller": seller_profile,
			"status": ou.CONFIRMED,
			"needs_staff_review": 0,
			"status_deadline": ["is", "not set"],
		},
		pluck="name",
		order_by="creation asc",
	)
	started = []
	for name in names:
		doc = _lock_order(name)
		if doc.status != ou.CONFIRMED or doc.status_deadline or doc.needs_staff_review:
			frappe.db.commit()
			continue
		with _atomic("start_payment_clock"):
			doc.status_deadline = ou.deadline_for(ou.CONFIRMED, now_datetime())
			doc.save(ignore_permissions=True)
		_notify_payment_clock_started(doc)
		started.append(name)
	return started


# -- notifications (Story 8.5) ---------------------------------------------------------------------


def _notify_expired(order, why: str) -> None:
	"""Both parties: the order was cancelled because its time ran out."""
	message = f"Order {order.name} ({_what(order)}) was cancelled automatically: {why}."
	for user in (order.buyer, lu.seller_user_of(order.seller)):
		_send(
			"order_expired",
			user,
			order,
			f"Order {order.name} cancelled - time ran out",
			message,
			{"reason": why},
			sms=True,
		)


def _notify_payment_flagged(order) -> None:
	"""D13 - the payment time ran out after a rejected slip: staff, the buyer and the seller."""
	hours = ou.PAYMENT_HOURS
	what = _what(order)
	for user in ou.staff_users():
		_send(
			"order_payment_flagged",
			user,
			order,
			f"Order {order.name} needs staff review",
			f"Order {order.name} ({what}) needs review: the buyer's payment slip was rejected and no new "
			f"slip arrived within {hours} hours. Approve a payment or cancel the order in the payments queue.",
			{"for_role": ou.STAFF, "hours": hours},
		)
	_send(
		"order_payment_flagged",
		order.buyer,
		order,
		f"Order {order.name} passed to staff for review",
		f"The {hours}-hour payment time on order {order.name} ({what}) ran out after your payment slip was "
		"rejected. The order is not cancelled: Accreage Mart staff will review it and either approve your "
		"payment or cancel the order. You can still upload a new payment slip.",
		{"for_role": ou.BUYER, "hours": hours},
		sms=True,
	)
	_send(
		"order_payment_flagged",
		lu.seller_user_of(order.seller),
		order,
		f"Order {order.name} passed to staff for review",
		f"The buyer's {hours}-hour payment time on order {order.name} ({what}) ran out after the payment "
		"slip was rejected. Accreage Mart staff will now review the order and either approve the payment "
		"or cancel it.",
		{"for_role": ou.SELLER, "hours": hours},
	)


def _notify_auction_order(order) -> None:
	"""The winner and the seller: the auction's order exists. Without the seller's bank details the
	seller is asked to add them and the buyer is told the payment time starts once they do (D16)."""
	pending = not order.status_deadline
	hours = ou.PAYMENT_HOURS
	price = f"{_rs(order.agreed_unit_price)} per {order.unit}"
	base = f"Order {order.name} was created for the winning bid: {_what(order)} at {price}, {_rs(order.total_price)} in total."
	context = {
		"price": _rs(order.agreed_unit_price),
		"total": _rs(order.total_price),
		"hours": hours,
		"payment_pending": pending,
	}
	if pending:
		buyer_next = (
			f"The seller is adding their bank details. Your {hours}-hour payment time starts once they do, "
			"and we'll tell you where to pay."
		)
		seller_next = (
			f"Add your bank details on your profile so the buyer can pay you. The buyer's {hours}-hour "
			"payment time starts once you save them."
		)
	else:
		buyer_next = _payment_next_step(True) + " The seller's bank details are on the order page."
		seller_next = _payment_next_step(False)
	_send(
		"order_auction_won",
		order.buyer,
		order,
		f"Your auction order {order.name}",
		f"{base} {buyer_next}",
		{**context, "for_seller": False},
		sms=True,
	)
	_send(
		"order_auction_won",
		lu.seller_user_of(order.seller),
		order,
		f"Add your bank details - order {order.name}" if pending else f"Auction order {order.name} created",
		f"{base} {seller_next}",
		{**context, "for_seller": True},
		sms=pending,
	)


def _notify_payment_clock_started(order) -> None:
	"""D16 - the buyer of a waiting auction order: where to pay, and the 72 h now running."""
	bank = ou.bank_details_of(order.seller)
	name = _seller_display_name(order.seller)
	where = (
		f"{bank['account_name']}, account {bank['account_number']}, {bank['bank_name']} "
		f"({bank['bank_branch']} branch)"
	)
	_send(
		"order_payment_details_added",
		order.buyer,
		order,
		f"You can now pay for order {order.name}",
		f"{name} added their bank details for order {order.name} ({_what(order)}). Pay "
		f"{_rs(order.total_price)} to {where}, then upload your payment slip within {ou.PAYMENT_HOURS} "
		"hours, or the order is cancelled.",
		{
			"seller_name": name,
			"total": _rs(order.total_price),
			"hours": ou.PAYMENT_HOURS,
			**bank,
		},
		sms=True,
	)
