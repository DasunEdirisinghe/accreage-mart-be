"""Whitelisted bidding endpoints (Epic 07, Story 7.1).

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
==================  =============================  =================================================

``place_bid``'s highest-bid check and the ``Bid`` insert run inside one DB-level row lock on
the ``Auction`` record (``for update``), so two near-simultaneous bids on the same auction can
never both pass the "exceeds current highest" check against the same stale value.
"""

import frappe
from frappe import _
from frappe.utils import flt

from accreage_mart.utils import listing as lu
from accreage_mart.utils.credentials import frontend_url
from accreage_mart.utils.notify import notify


def _auction(name: str):
	if not name or not frappe.db.exists("Auction", name):
		frappe.throw(_("Auction not found."), frappe.DoesNotExistError)
	return frappe.get_doc("Auction", name)


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
	status = lu.derive_auction_status(listing.status, doc.start_time, doc.end_time)
	return {
		"auction": {
			"name": doc.name,
			"min_bid": doc.min_bid,
			"start_time": doc.start_time,
			"end_time": doc.end_time,
			"status": status,
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

	status = lu.derive_auction_status(listing.status, doc.start_time, doc.end_time)
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
