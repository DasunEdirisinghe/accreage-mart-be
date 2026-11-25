"""Order state machine, deadline rules and access hooks (Epic 08).

Every status change an order can go through lives in :data:`TRANSITIONS`, keyed by
``(from_status, to_status)`` with the actors allowed to drive it. The D13 "cancelling after a
payment slip" rules are the one place where the answer also depends on the order's flags, so
they sit in :func:`can_transition` rather than in the table. The API (``api/orders.py``) asks
:func:`assert_transition` before every change and writes inside :func:`accreage_mart.utils.listing.system_write`.

Deadlines (D14): an order carries one ``status_deadline`` for its current status. :func:`deadline_info`
says who must act before it (``action_by``) and what happens when it passes (``on_expiry``), so both
parties can show the same countdown.

Every platform user also carries ``System Manager`` (see ``utils/profile.py``), so the
``has_permission`` / ``permission_query_conditions`` hooks below - registered in ``hooks.py`` - are
what keep one buyer or seller away from another party's orders and payment slips.
"""

import re

import frappe
from frappe import _
from frappe.utils import add_to_date, flt, get_datetime, now_datetime

from accreage_mart.utils import listing as lu

# -- statuses --------------------------------------------------------------------------------------

PENDING_CONFIRMATION = "pending_confirmation"
NEGOTIATING = "negotiating"
CONFIRMED = "confirmed"
PAYMENT_REVIEW = "payment_review"
PAID = "paid"
DELIVERED = "delivered"
COMPLETED = "completed"
CANCELLED = "cancelled"
STATUSES = (
	PENDING_CONFIRMATION,
	NEGOTIATING,
	CONFIRMED,
	PAYMENT_REVIEW,
	PAID,
	DELIVERED,
	COMPLETED,
	CANCELLED,
)
TERMINAL_STATUSES = (COMPLETED, CANCELLED)
# "Active" = still being worked on: drives the listing hide/archive warning.
ACTIVE_STATUSES = tuple(s for s in STATUSES if s not in TERMINAL_STATUSES)
# Price is locked from here on (REQ-12); the buyer may see the seller's bank details.
PRICE_LOCKED_STATUSES = (CONFIRMED, PAYMENT_REVIEW, PAID, DELIVERED, COMPLETED)

SOURCE_DIRECT = "Direct"
SOURCE_AUCTION = "Auction"

PARTY_BUYER = "Buyer"
PARTY_SELLER = "Seller"

OFFER_OPEN = "Open"
OFFER_ACCEPTED = "Accepted"
OFFER_COUNTERED = "Countered"
OFFER_DECLINED = "Declined"
OFFER_EXPIRED = "Expired"
MAX_OFFERS = 5

PROOF_SUBMITTED = "Submitted"
PROOF_APPROVED = "Approved"
PROOF_REJECTED = "Rejected"
REJECT_CATEGORIES = ("Slip unclear or incomplete", "Payment not received")

# -- actors ----------------------------------------------------------------------------------------

BUYER = "buyer"
SELLER = "seller"
STAFF = "staff"
SYSTEM = "system"  # the scheduler / auction close
NONE = "none"

# -- deadlines (D4, D14) ---------------------------------------------------------------------------

CONFIRM_HOURS = 48  # pending_confirmation, and each open offer while negotiating
PAYMENT_HOURS = 72  # confirmed -> slip upload
DELIVERY_CONFIRM_DAYS = 7  # delivered -> buyer confirms receipt

ON_EXPIRY_CANCEL = "cancel"
ON_EXPIRY_STAFF_REVIEW = "staff_review"
ON_EXPIRY_AUTO_COMPLETE = "auto_complete"

# -- transitions -----------------------------------------------------------------------------------

# (from, to) -> who may drive it. Placement (None -> ...) is listed for completeness.
TRANSITIONS: dict[tuple[str | None, str], frozenset[str]] = {
	(None, PENDING_CONFIRMATION): frozenset({BUYER}),
	(None, NEGOTIATING): frozenset({BUYER}),
	(None, CONFIRMED): frozenset({SYSTEM}),  # auction win (D8)
	(PENDING_CONFIRMATION, CONFIRMED): frozenset({SELLER}),
	(PENDING_CONFIRMATION, CANCELLED): frozenset({BUYER, SELLER, STAFF, SYSTEM}),
	(NEGOTIATING, CONFIRMED): frozenset({BUYER, SELLER}),  # accept; turn order checked by the API
	(NEGOTIATING, CANCELLED): frozenset({BUYER, SELLER, STAFF, SYSTEM}),
	(CONFIRMED, PAYMENT_REVIEW): frozenset({BUYER}),
	(CONFIRMED, PAID): frozenset({STAFF}),  # staff approve a payment on a flagged order (D13)
	(CONFIRMED, CANCELLED): frozenset({BUYER, SELLER, STAFF, SYSTEM}),  # narrowed by D13 below
	(PAYMENT_REVIEW, PAID): frozenset({SELLER, STAFF}),
	(PAYMENT_REVIEW, CONFIRMED): frozenset({SELLER, STAFF}),  # slip rejected
	(PAID, DELIVERED): frozenset({SELLER}),
	(DELIVERED, COMPLETED): frozenset({BUYER, SYSTEM}),
}


def allowed_transitions(from_status: str | None) -> dict[str, frozenset[str]]:
	"""``{to_status: actors}`` reachable from ``from_status`` according to the table."""
	return {to: actors for (frm, to), actors in TRANSITIONS.items() if frm == from_status}


def can_transition(order, to_status: str, actor: str) -> bool:
	"""Whether ``actor`` may move ``order`` (a doc or dict with ``status`` and the D13 flags)
	to ``to_status``.

	D13 - once a slip has been submitted (``has_submitted_proof``), a ``confirmed`` order can be
	ended only by the buyer or staff: never by the seller alone, never by the 72 h timer. Before
	any slip, the buyer cannot cancel a ``confirmed`` order directly - they send a cancellation
	request the seller accepts (the seller then cancels).
	"""
	from_status = order.get("status")
	actors = TRANSITIONS.get((from_status, to_status))
	if not actors or actor not in actors:
		return False
	if (from_status, to_status) == (CONFIRMED, CANCELLED):
		submitted = bool(order.get("has_submitted_proof"))
		if actor in (SELLER, SYSTEM):
			return not submitted
		if actor == BUYER:
			return submitted
	if (from_status, to_status) == (CONFIRMED, PAID):
		return bool(order.get("needs_staff_review"))
	return True


def assert_transition(order, to_status: str, actor: str) -> None:
	if not can_transition(order, to_status, actor):
		frappe.throw(
			_("This action is not allowed while the order is {0}.").format(status_label(order.get("status")))
		)


STATUS_LABELS = {
	PENDING_CONFIRMATION: "waiting for the seller to confirm",
	NEGOTIATING: "in price negotiation",
	CONFIRMED: "waiting for payment",
	PAYMENT_REVIEW: "waiting for the payment slip review",
	PAID: "paid",
	DELIVERED: "delivered",
	COMPLETED: "completed",
	CANCELLED: "cancelled",
}


def status_label(status: str | None) -> str:
	return STATUS_LABELS.get(status, status or "")


# -- deadlines -------------------------------------------------------------------------------------


def deadline_for(status: str, start=None):
	"""The ``status_deadline`` a freshly entered ``status`` gets, or None if it has no timer."""
	start = get_datetime(start) if start else now_datetime()
	if status in (PENDING_CONFIRMATION, NEGOTIATING):
		return add_to_date(start, hours=CONFIRM_HOURS)
	if status == CONFIRMED:
		return add_to_date(start, hours=PAYMENT_HOURS)
	if status == DELIVERED:
		return add_to_date(start, days=DELIVERY_CONFIRM_DAYS)
	return None


def open_offer_party(order_name: str) -> str | None:
	"""``Buyer`` / ``Seller`` - who made the currently open offer, if any."""
	return frappe.db.get_value(
		"Order Offer", {"order": order_name, "outcome": OFFER_OPEN}, "party", order_by="offered_on desc"
	)


def deadline_info(order) -> dict:
	"""``status_deadline``, ``action_by`` (buyer/seller/staff/none) and ``on_expiry``
	(cancel/staff_review/auto_complete/None) for the order's current status (D14)."""
	status = order.get("status")
	deadline = order.get("status_deadline")
	if status == PENDING_CONFIRMATION:
		return _deadline(deadline, SELLER, ON_EXPIRY_CANCEL)
	if status == NEGOTIATING:
		last = open_offer_party(order.get("name")) if order.get("name") else None
		return _deadline(deadline, SELLER if last != PARTY_SELLER else BUYER, ON_EXPIRY_CANCEL)
	if status == CONFIRMED:
		if order.get("needs_staff_review"):
			# The 72 h passed after a rejected slip: staff decide; nobody is on a clock.
			return _deadline(None, STAFF, None)
		on_expiry = ON_EXPIRY_STAFF_REVIEW if order.get("has_submitted_proof") else ON_EXPIRY_CANCEL
		return _deadline(deadline, BUYER, on_expiry)
	if status == PAYMENT_REVIEW:
		return _deadline(None, SELLER, None)
	if status == PAID:
		return _deadline(None, SELLER, None)
	if status == DELIVERED:
		return _deadline(deadline, BUYER, ON_EXPIRY_AUTO_COMPLETE)
	return _deadline(None, NONE, None)


def _deadline(deadline, action_by: str, on_expiry: str | None) -> dict:
	return {"status_deadline": deadline, "action_by": action_by, "on_expiry": on_expiry}


def deadline_passed(order, now=None) -> bool:
	deadline = order.get("status_deadline")
	if not deadline:
		return False
	now = get_datetime(now) if now else now_datetime()
	return now > get_datetime(deadline)


def assert_before_deadline(order) -> None:
	"""D14 - every party action is refused once the deadline has passed, even before the
	scheduled job has processed the order."""
	if deadline_passed(order):
		frappe.throw(_("The deadline for this step has passed."))


# -- roles on an order -----------------------------------------------------------------------------


def viewer_role(order, user: str | None = None) -> str | None:
	"""``buyer`` / ``seller`` / ``staff`` for ``user`` on ``order``, or None for anyone else.
	A party to the order wins over staff (a staff member who also buys sees it as the buyer)."""
	user = user or frappe.session.user
	if user and user != "Guest":
		if order.get("buyer") == user:
			return BUYER
		if lu.owns_listing(order.get("seller"), user):
			return SELLER
		if lu.is_staff_or_admin(user):
			return STAFF
	return None


# -- seller bank details (D10) ---------------------------------------------------------------------

BANK_FIELDS = ("bank_name", "bank_branch", "account_name", "account_number")
ACCOUNT_NUMBER_RE = re.compile(r"^\d{5,20}$")


def bank_details_of(seller_profile: str) -> dict:
	row = frappe.db.get_value("Seller Profile", seller_profile, list(BANK_FIELDS), as_dict=True) or {}
	return {field: row.get(field) or None for field in BANK_FIELDS}


def has_bank_details(seller_profile: str) -> bool:
	return all(bank_details_of(seller_profile).values())


def normalize_account_number(value: str | None) -> str:
	"""Digits only; spaces and dashes people type are dropped."""
	return re.sub(r"[\s-]", "", value or "")


# -- listing helpers (used by utils/listing.py) ----------------------------------------------------


def buyer_has_order(listing_name: str, user: str) -> bool:
	"""Any order by ``user`` on the listing that was not cancelled."""
	if not user or user == "Guest":
		return False
	return bool(
		frappe.db.exists("Order", {"listing": listing_name, "buyer": user, "status": ["!=", CANCELLED]})
	)


def active_order_count(listing_name: str) -> int:
	return frappe.db.count("Order", {"listing": listing_name, "status": ["in", ACTIVE_STATUSES]})


# -- stock impact (D11) ----------------------------------------------------------------------------

# Price agreed but stock not yet subtracted (that happens on payment approval, D7).
STOCK_HOLDING_STATUSES = (CONFIRMED, PAYMENT_REVIEW)
# Stock already subtracted for these.
STOCK_DEDUCTED_STATUSES = (PAID, DELIVERED, COMPLETED)


def stock_impact(order) -> dict:
	"""What the listing's stock looks like if every price-agreed order on it is paid (D11).

	``held_by_others`` is the quantity of the listing's *other* ``confirmed`` /
	``payment_review`` orders. ``this_order_pending`` says whether this order's quantity is still
	to come off the stock (False once it was paid, or if it was cancelled). ``projected`` can go
	negative; ``shortfall`` is how far below zero it goes. Never used to block anything.
	"""
	listing = frappe.db.get_value(
		"Listing", order.get("listing"), ["quantity_available", "unit"], as_dict=True
	)
	stock_now = flt(listing.quantity_available) if listing else 0
	held = frappe.get_all(
		"Order",
		filters={
			"listing": order.get("listing"),
			"status": ["in", STOCK_HOLDING_STATUSES],
			"name": ["!=", order.get("name") or ""],
		},
		pluck="quantity",
	)
	held_total = sum(flt(q) for q in held)
	this_order = flt(order.get("quantity"))
	pending = order.get("status") not in (*STOCK_DEDUCTED_STATUSES, CANCELLED)
	projected = stock_now - held_total - (this_order if pending else 0)
	return {
		"unit": order.get("unit") or (listing.unit if listing else None),
		"stock_now": stock_now,
		"held_by_others": held_total,
		"held_order_count": len(held),
		"this_order": this_order,
		"this_order_pending": pending,
		"projected": projected,
		"shortfall": max(0.0, -projected),
	}


# -- cancellation (D5, D13) ------------------------------------------------------------------------


def cancel_refusal(order, actor: str) -> str | None:
	"""Why ``actor`` may not cancel ``order`` right now, in plain words, or None if they may.
	Mirrors :func:`can_transition` for ``-> cancelled`` with a message per D5 / D13 case."""
	if can_transition(order, CANCELLED, actor):
		return None
	status = order.get("status")
	if status == CANCELLED:
		return _("This order is already cancelled.")
	if status == PAYMENT_REVIEW:
		return _("This order can't be cancelled while its payment slip is being reviewed.")
	if status in STOCK_DEDUCTED_STATUSES:
		return _("This order can't be cancelled once its payment has been approved.")
	if status == CONFIRMED:
		if actor == BUYER:
			return _(
				"The price is agreed, so ask the seller to cancel instead (send a cancellation request)."
			)
		if actor == SELLER:
			return _(
				"The buyer has submitted a payment slip for this order, so only staff can cancel it. "
				"Ask staff to cancel instead."
			)
	return _("You can't cancel this order.")


# -- staff recipients ------------------------------------------------------------------------------


def staff_users() -> list[str]:
	"""Enabled, active Staff and Admin users (who get staff-queue notifications)."""
	from accreage_mart.utils.profile import get_account_status

	names = frappe.get_all(
		"Has Role",
		filters={"role": ["in", ["Staff", "Admin"]], "parenttype": "User"},
		pluck="parent",
		distinct=True,
	)
	names = [n for n in names if n not in ("Administrator", "Guest")]
	if not names:
		return []
	rows = frappe.get_all(
		"User",
		filters={"name": ["in", names], "enabled": 1},
		fields=["name", "enabled", "custom_account_status"],
	)
	return sorted(row.name for row in rows if get_account_status(row) == "active")


# -- access hooks (see hooks.py) -------------------------------------------------------------------

WRITE_PTYPES = ("create", "write", "delete", "submit", "cancel", "amend")


def _party_to(order_name: str | None, user: str) -> bool:
	if not order_name:
		return False
	row = frappe.db.get_value("Order", order_name, ["buyer", "seller"], as_dict=True)
	if not row:
		return False
	return row.buyer == user or lu.owns_listing(row.seller, user)


def order_has_permission(doc, ptype=None, user=None, debug=False):
	"""Orders are written only through the API. The buyer, the seller and staff may read."""
	user = user or frappe.session.user
	if ptype in WRITE_PTYPES:
		return None if lu.can_system_write(user) else False
	if lu.is_staff_or_admin(user):
		return None
	if doc.get("buyer") == user or lu.owns_listing(doc.get("seller"), user):
		return None
	return False


def order_child_has_permission(doc, ptype=None, user=None, debug=False):
	"""Order Offer / Payment Proof: written only through the API; readable by the order's
	buyer, its seller and staff."""
	user = user or frappe.session.user
	if ptype in WRITE_PTYPES:
		return None if lu.can_system_write(user) else False
	if lu.is_staff_or_admin(user):
		return None
	return None if _party_to(doc.get("order"), user) else False


def _order_scope_sql(user: str) -> str:
	clauses = [f"`tabOrder`.`buyer` = {frappe.db.escape(user)}"]
	profile = lu.seller_profile_of(user)
	if profile:
		clauses.append(f"`tabOrder`.`seller` = {frappe.db.escape(profile)}")
	return "(" + " or ".join(clauses) + ")"


def order_query_conditions(user=None, doctype=None):
	user = user or frappe.session.user
	if lu.is_staff_or_admin(user):
		return ""
	if not user or user == "Guest":
		return "1=0"
	return _order_scope_sql(user)


def _child_query_conditions(table: str, user: str | None) -> str:
	user = user or frappe.session.user
	if lu.is_staff_or_admin(user):
		return ""
	if not user or user == "Guest":
		return "1=0"
	return f"`{table}`.`order` in (select `name` from `tabOrder` where {_order_scope_sql(user)})"


def order_offer_query_conditions(user=None, doctype=None):
	return _child_query_conditions("tabOrder Offer", user)


def payment_proof_query_conditions(user=None, doctype=None):
	return _child_query_conditions("tabPayment Proof", user)
