"""Whitelisted bidding and auction-lifecycle endpoints (Epic 07, Stories 7.1-7.2).

Referenced from the frontend as ``accreage_mart.api.auctions.<fn>``.

Guard matrix
------------
==================  =============================  =================================================
Endpoint            Caller                          Notes
==================  =============================  =================================================
get_auction_detail  anyone (guest)                   auction + listing + bid history (newest first) +
                                                       the current highest bid
place_bid           Buyer, account in good standing  rejects a non-live auction, an amount that does
                                                       not exceed the current highest bid (or the
                                                       auction's minimum bid if it has none yet), and
                                                       a bid from the auction's own seller
review_auction      Staff / Admin                    approve (-> scheduled, or straight to live if
                                                       start_time already passed) or reject (reason
                                                       required) a pending auction's listing
cancel_auction      Staff / Admin                    reason required, only while scheduled or live;
                                                       never refunds/voids - nothing is charged at bid
                                                       time
list_seller_auctions  Verified seller                the caller's own auctions (their Auction-type
                                                       listings), newest activity first, paginated
list_buyer_bids     Buyer, account in good standing   every auction the caller has bid on, newest
                                                       activity first, paginated
==================  =============================  =================================================

``place_bid``'s highest-bid check and the ``Bid`` insert run inside one DB-level row lock on
the ``Auction`` record (``for update``), so two near-simultaneous bids on the same auction can
never both pass the "exceeds current highest" check against the same stale value.

``close_expired_auctions`` runs every minute (see ``hooks.py``'s ``scheduler_events``), closing
every live auction past its ``end_time``. It is idempotent: an auction whose seller has already
been sent its closing notification (``auction_sold``/``auction_ended_no_bids``) is skipped on a
later run.
"""

import frappe
from frappe import _
from frappe.utils import flt, now_datetime

from accreage_mart.api import marketplace as mk
from accreage_mart.utils import listing as lu
from accreage_mart.utils.credentials import frontend_url
from accreage_mart.utils.notify import notify
from accreage_mart.utils.registration import require_staff

REVIEW_DECISIONS = ("approve", "reject")
CLOSED_EVENTS = ("auction_sold", "auction_ended_no_bids")


def _auction(name: str):
	if not name or not frappe.db.exists("Auction", name):
		frappe.throw(_("Auction not found."), frappe.DoesNotExistError)
	return frappe.get_doc("Auction", name)


def _require_reason(reason: str | None) -> str:
	reason = (reason or "").strip()
	if not reason:
		frappe.throw(_("A reason is required."))
	return reason


def _distinct_bidders(auction_name: str) -> list[str]:
	return frappe.get_all(
		"Bid",
		filters={"auction": auction_name},
		pluck="bidder",
		distinct=True,
		ignore_permissions=True,
	)


def _listing_of(auction_name: str):
	listing_name = frappe.db.get_value("Listing", {"auction": auction_name}, "name")
	if not listing_name:
		frappe.throw(_("Auction not found."), frappe.DoesNotExistError)
	return frappe.get_doc("Listing", listing_name)


def _bids_of(auction_name: str) -> list[dict]:
	"""Full bid history, newest first."""
	rows = frappe.get_all(
		"Bid",
		filters={"auction": auction_name},
		fields=["name", "auction", "bidder", "amount", "bid_time"],
		order_by="bid_time desc, name desc",
		ignore_permissions=True,
	)
	return [dict(row) for row in rows]


def _highest_bid(auction_name: str) -> dict | None:
	rows = frappe.get_all(
		"Bid",
		filters={"auction": auction_name},
		fields=["name", "auction", "bidder", "amount", "bid_time"],
		order_by="amount desc, bid_time asc, name asc",
		limit=1,
		ignore_permissions=True,
	)
	return rows[0] if rows else None


def _highest_bid_locked(auction_name: str) -> dict | None:
	"""Lock the Auction row, then read the current highest bid for it as of right now.

	The lock on the Auction row is what makes the highest-bid check and the Bid insert that
	follows atomic for a given auction: a second call for the same auction blocks here until
	the first one commits or rolls back. Both queries use ``for update`` so they read the
	latest committed data rather than a transaction's earlier (possibly stale) snapshot.
	"""
	frappe.db.sql("select name from `tabAuction` where name=%s for update", auction_name)
	rows = frappe.db.sql(
		"""select bidder, amount from `tabBid` where auction=%s
		order by amount desc, bid_time asc, name asc limit 1 for update""",
		auction_name,
		as_dict=True,
	)
	return rows[0] if rows else None


@frappe.whitelist()
def get_auction_detail(auction: str) -> dict:
	"""The auction, its listing, full bid history (newest first) and the current highest bid."""
	doc = _auction(auction)
	listing = _listing_of(doc.name)
	status = lu.derive_auction_status(
		listing.status, doc.start_time, doc.end_time, cancelled=doc.cancelled
	)
	return {
		"auction": {
			"name": doc.name,
			"min_bid": doc.min_bid,
			"start_time": doc.start_time,
			"end_time": doc.end_time,
			"status": status,
			"cancel_reason": doc.cancel_reason if doc.cancelled else None,
		},
		"listing": {
			"name": listing.name,
			"title": listing.title,
			"unit": listing.unit,
			"quantity_available": listing.quantity_available,
			"seller_business_name": frappe.db.get_value("Seller Profile", listing.seller, "business_name"),
			"seller": lu.public_seller_id(listing.seller),
		},
		"bids": _bids_of(doc.name),
		"highest_bid": _highest_bid(doc.name),
	}


@frappe.whitelist(methods=["POST"])
def place_bid(auction: str, amount: float) -> dict:
	"""Place a bid as the logged-in buyer. Validated and inserted under a DB-level lock."""
	user = lu.require_buyer()
	doc = _auction(auction)
	listing = _listing_of(doc.name)

	if lu.owns_listing(listing.seller, user):
		frappe.throw(_("You cannot bid on your own auction."), frappe.PermissionError)

	status = lu.derive_auction_status(
		listing.status, doc.start_time, doc.end_time, cancelled=doc.cancelled
	)
	if status != "live":
		frappe.throw(_("This auction is not currently live."))

	highest = _highest_bid_locked(doc.name)
	floor = flt(highest.amount) if highest else flt(doc.min_bid)
	if flt(amount) <= floor:
		frappe.throw(
			_("Your bid must exceed the current {0} of {1}.").format(
				_("highest bid") if highest else _("minimum bid"), floor
			)
		)

	with lu.system_write():
		bid = frappe.get_doc(
			{"doctype": "Bid", "auction": doc.name, "bidder": user, "amount": amount}
		).insert(ignore_permissions=True)
	frappe.db.commit()

	if highest and highest.bidder != user:
		_notify_outbid(listing, doc, highest.bidder, amount)

	return {
		"name": bid.name,
		"auction": bid.auction,
		"bidder": bid.bidder,
		"amount": bid.amount,
		"bid_time": bid.bid_time,
	}


def _notify_outbid(listing, auction, outbid_user: str, new_amount) -> None:
	"""Never blocks place_bid: a failure here is logged, not raised."""
	try:
		amount = flt(new_amount)
		notify(
			"auction_outbid",
			outbid_user,
			"You've been outbid",
			f"You were outbid on {listing.title}. The new highest bid is Rs. {amount} per {listing.unit}.",
			["email"],
			document_type="Auction",
			document_name=auction.name,
			email_template="auction_outbid",
			email_context={
				"full_name": frappe.db.get_value("User", outbid_user, "full_name") or "there",
				"title": listing.title,
				"amount": amount,
				"unit": listing.unit,
			},
			email_cta_label="View auction",
			email_cta_url=f"{frontend_url()}/auctions/{auction.name}",
		)
	except Exception:
		frappe.log_error(title="Auction outbid notification failed")


# -- review / cancel (Story 7.2) ------------------------------------------------------------------


@frappe.whitelist(methods=["POST"])
def review_auction(auction: str, decision: str, reason: str | None = None) -> dict:
	"""Approve or reject a pending auction's listing. Approve publishes it immediately -
	``scheduled`` before its start time, or straight to ``live`` if that has already passed.
	Reject requires a reason."""
	require_staff()
	if decision not in REVIEW_DECISIONS:
		frappe.throw(_("Unknown decision."))
	doc = _auction(auction)
	listing = _listing_of(doc.name)
	if listing.status != lu.PENDING:
		frappe.throw(_("Only a pending auction can be reviewed."))

	reason = _require_reason(reason) if decision == "reject" else ((reason or "").strip() or None)
	action = "Approved" if decision == "approve" else "Rejected"
	reviewer = frappe.session.user

	savepoint = "auction_review"
	frappe.db.savepoint(savepoint)
	try:
		with lu.system_write():
			listing.status = lu.APPROVED if decision == "approve" else lu.REJECTED
			listing.status_reason = reason if decision == "reject" else None
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
	frappe.db.commit()

	status = lu.derive_auction_status(listing.status, doc.start_time, doc.end_time, cancelled=doc.cancelled)
	if decision == "reject":
		_notify_rejected(listing, doc, reason)
	return {"name": doc.name, "listing": listing.name, "status": status}


@frappe.whitelist(methods=["POST"])
def cancel_auction(auction: str, reason: str) -> dict:
	"""Staff/admin cancel a scheduled or live auction, with a required reason. Purely a status
	+ notification concern - never refunds or voids a bid, since nothing is charged at bid
	time."""
	require_staff()
	reason = _require_reason(reason)
	doc = _auction(auction)
	listing = _listing_of(doc.name)
	status = lu.derive_auction_status(listing.status, doc.start_time, doc.end_time, cancelled=doc.cancelled)
	if status not in ("scheduled", "live"):
		frappe.throw(_("Only a scheduled or live auction can be cancelled."))

	savepoint = "auction_cancel"
	frappe.db.savepoint(savepoint)
	try:
		with lu.system_write():
			doc.cancelled = 1
			doc.cancel_reason = reason
			doc.save(ignore_permissions=True)
	except Exception:
		frappe.db.rollback(save_point=savepoint)
		raise
	frappe.db.commit()

	_notify_cancelled(listing, doc, reason)
	return {"name": doc.name, "status": "cancelled"}


def _notify_rejected(listing, auction, reason: str) -> None:
	"""Never blocks review_auction: a failure here is logged, not raised."""
	try:
		seller_user = lu.seller_user_of(listing.seller)
		if not seller_user:
			return
		notify(
			"auction_rejected",
			seller_user,
			"Your auction was not approved",
			f"Your auction for {listing.title} was not approved. Reason: {reason}",
			["email"],
			document_type="Auction",
			document_name=auction.name,
			email_template="auction_rejected",
			email_context={
				"full_name": frappe.db.get_value("User", seller_user, "full_name") or "there",
				"title": listing.title,
				"reason": reason,
			},
			email_cta_label="View auction",
			email_cta_url=f"{frontend_url()}/auctions/{auction.name}",
		)
	except Exception:
		frappe.log_error(title="Auction rejected notification failed")


def _notify_cancelled(listing, auction, reason: str) -> None:
	"""Notifies the seller and every bidder. Never blocks cancel_auction."""
	seller_user = lu.seller_user_of(listing.seller)
	recipients = {seller_user} if seller_user else set()
	recipients |= set(_distinct_bidders(auction.name))

	for user in recipients:
		is_seller = user == seller_user
		try:
			notify(
				"auction_cancelled",
				user,
				"Auction cancelled",
				(
					f"Your auction for {listing.title} was cancelled. Reason: {reason}"
					if is_seller
					else f"The auction for {listing.title} that you bid on was cancelled. Reason: {reason}"
				),
				["email"],
				document_type="Auction",
				document_name=auction.name,
				email_template="auction_cancelled_seller" if is_seller else "auction_cancelled_bidder",
				email_context={
					"full_name": frappe.db.get_value("User", user, "full_name") or "there",
					"title": listing.title,
					"reason": reason,
				},
				email_cta_label="View auction",
				email_cta_url=f"{frontend_url()}/auctions/{auction.name}",
			)
		except Exception:
			frappe.log_error(title="Auction cancelled notification failed")


# -- scheduled closing (Story 7.2) ----------------------------------------------------------------


def close_expired_auctions() -> None:
	"""Scheduled every minute (see ``hooks.py``). Closes every live auction past its
	``end_time``: notifies the winning bidder and the seller, or just the seller if there were
	no bids. Idempotent - an auction already notified of its outcome is skipped."""
	rows = frappe.db.sql(
		"""select a.name as auction from `tabAuction` a
		join `tabListing` l on l.auction = a.name
		where l.status = %(approved)s and a.cancelled = 0 and a.end_time <= %(now)s""",
		{"approved": lu.APPROVED, "now": now_datetime()},
		as_dict=True,
	)
	for row in rows:
		try:
			_close_one(row.auction)
		except Exception:
			frappe.log_error(title=f"close_expired_auctions failed for {row.auction}")


def _close_one(auction_name: str) -> None:
	if frappe.db.exists(
		"Notification Log",
		{
			"document_type": "Auction",
			"document_name": auction_name,
			"custom_event_type": ["in", CLOSED_EVENTS],
		},
	):
		return  # already closed and notified on a previous run

	doc = _auction(auction_name)
	listing = _listing_of(auction_name)
	highest = _highest_bid(auction_name)
	if highest:
		_notify_closed_with_winner(listing, doc, highest)
	else:
		_notify_closed_no_bids(listing, doc)


def _notify_closed_with_winner(listing, auction, highest: dict) -> None:
	"""Notifies the winning buyer, then the seller (the seller notification is the idempotency
	marker checked by ``_close_one`` - it is always the last of the two to be sent)."""
	amount = flt(highest["amount"])
	winner = highest["bidder"]
	try:
		notify(
			"auction_won",
			winner,
			"You won the auction!",
			f"You won the auction for {listing.title} with a bid of Rs. {amount} per {listing.unit}.",
			["email"],
			document_type="Auction",
			document_name=auction.name,
			email_template="auction_won",
			email_context={
				"full_name": frappe.db.get_value("User", winner, "full_name") or "there",
				"title": listing.title,
				"amount": amount,
				"unit": listing.unit,
			},
			email_cta_label="View auction",
			email_cta_url=f"{frontend_url()}/auctions/{auction.name}",
		)
	except Exception:
		frappe.log_error(title="Auction won notification failed")

	seller_user = lu.seller_user_of(listing.seller)
	if not seller_user:
		return
	try:
		notify(
			"auction_sold",
			seller_user,
			"Your auction has sold",
			f"Your auction for {listing.title} sold for Rs. {amount} per {listing.unit}.",
			["email"],
			document_type="Auction",
			document_name=auction.name,
			email_template="auction_sold",
			email_context={
				"full_name": frappe.db.get_value("User", seller_user, "full_name") or "there",
				"title": listing.title,
				"amount": amount,
				"unit": listing.unit,
			},
			email_cta_label="View auction",
			email_cta_url=f"{frontend_url()}/auctions/{auction.name}",
		)
	except Exception:
		frappe.log_error(title="Auction sold notification failed")


def _notify_closed_no_bids(listing, auction) -> None:
	"""Notifies the seller only; also the idempotency marker checked by ``_close_one``."""
	seller_user = lu.seller_user_of(listing.seller)
	if not seller_user:
		return
	try:
		notify(
			"auction_ended_no_bids",
			seller_user,
			"Your auction ended with no bids",
			f"Your auction for {listing.title} ended with no bids.",
			["email"],
			document_type="Auction",
			document_name=auction.name,
			email_template="auction_ended_no_bids",
			email_context={
				"full_name": frappe.db.get_value("User", seller_user, "full_name") or "there",
				"title": listing.title,
			},
			email_cta_label="View auction",
			email_cta_url=f"{frontend_url()}/auctions/{auction.name}",
		)
	except Exception:
		frappe.log_error(title="Auction ended-no-bids notification failed")


# -- listing endpoints for the frontend (Story 7.3) -----------------------------------------------


@frappe.whitelist()
def list_seller_auctions(page: int = 1, page_size: int = mk.DEFAULT_PAGE_SIZE) -> dict:
	"""The caller's own auctions (their Auction-type listings), newest activity first."""
	profile = lu.require_seller()
	page, page_size = mk.page_args(page, page_size)

	where = {"seller": profile, "selling_type": lu.AUCTION}
	total = frappe.db.count("Listing", where)
	listings = frappe.get_all(
		"Listing",
		filters=where,
		fields=["name", "title", "unit", "auction", "status"],
		order_by="modified desc, name desc",
		limit_page_length=page_size,
		limit_start=(page - 1) * page_size,
		ignore_permissions=True,
	)
	items = []
	for row in listings:
		doc = frappe.get_doc("Auction", row.auction)
		items.append(
			{
				"name": row.auction,
				"listing": {"name": row.name, "title": row.title, "unit": row.unit},
				"min_bid": doc.min_bid,
				"start_time": doc.start_time,
				"end_time": doc.end_time,
				"status": lu.derive_auction_status(row.status, doc.start_time, doc.end_time, cancelled=doc.cancelled),
				"cancel_reason": doc.cancel_reason if doc.cancelled else None,
				"bid_count": len(_bids_of(row.auction)),
				"highest_bid": _highest_bid(row.auction),
			}
		)
	return {
		"items": items,
		"total": total,
		"page": page,
		"page_size": page_size,
		"has_more": page * page_size < total,
	}


@frappe.whitelist()
def list_buyer_bids(page: int = 1, page_size: int = mk.DEFAULT_PAGE_SIZE) -> dict:
	"""Every auction the caller has bid on, newest activity first."""
	user = lu.require_buyer()
	page, page_size = mk.page_args(page, page_size)

	total = frappe.db.sql(
		"select count(distinct auction) from `tabBid` where bidder=%s", user, as_dict=False
	)[0][0]
	rows = frappe.db.sql(
		"""select auction, max(bid_time) as last_bid_time from `tabBid` where bidder=%s
		group by auction order by last_bid_time desc limit %s offset %s""",
		(user, page_size, (page - 1) * page_size),
		as_dict=True,
	)

	items = []
	for row in rows:
		doc = _auction(row.auction)
		listing = _listing_of(row.auction)
		highest = _highest_bid(row.auction)
		mine = frappe.get_all(
			"Bid",
			filters={"auction": row.auction, "bidder": user},
			fields=["amount", "bid_time"],
			order_by="amount desc, bid_time asc",
			limit=1,
			ignore_permissions=True,
		)
		items.append(
			{
				"name": row.auction,
				"listing": {"name": listing.name, "title": listing.title, "unit": listing.unit},
				"status": lu.derive_auction_status(listing.status, doc.start_time, doc.end_time, cancelled=doc.cancelled),
				"cancel_reason": doc.cancel_reason if doc.cancelled else None,
				"end_time": doc.end_time,
				"my_highest_bid": mine[0] if mine else None,
				"highest_bid": highest,
				"leading": bool(highest) and highest["bidder"] == user,
			}
		)
	return {
		"items": items,
		"total": total,
		"page": page,
		"page_size": page_size,
		"has_more": page * page_size < total,
	}
