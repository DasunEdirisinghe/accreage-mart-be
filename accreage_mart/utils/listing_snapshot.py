"""Price-suggestion snapshot stored on a listing at submission (context for staff only).

The seller keeps full control of the price; the snapshot is never shown as a live figure.
"""

from frappe.utils import get_datetime

from accreage_mart.api.pricing import get_price_suggestion


def _bucket(horizon_days: int) -> str:
	if horizon_days <= 7:
		return "near"
	if horizon_days <= 14:
		return "mid"
	return "long"


def suggestion_snapshot(category: str, auction_start=None) -> dict:
	"""``{"min", "max", "fair_value"}`` from the category's forecast, or ``{}`` when there is none.

	min/max come from the nearest forecast day when its accuracy tier allows a suggestion;
	fair_value (auctions only) is the predicted price on the auction's start date, gated the
	same way by that day's horizon bucket.
	"""
	suggestion = get_price_suggestion(category)
	if not suggestion.get("available"):
		return {}

	days = sorted(suggestion.get("forecast_days") or [], key=lambda d: d["horizon_days_ahead"])
	tiers = suggestion["tiers"]
	snapshot: dict = {}

	if days and tiers["near"] != "unavailable":
		nearest = days[0]
		snapshot["min"] = nearest["lower_bound"]
		snapshot["max"] = nearest["upper_bound"]

	if auction_start and days:
		start_date = get_datetime(auction_start).date()
		for day in days:
			if get_datetime(day["forecast_date"]).date() == start_date:
				if tiers[_bucket(day["horizon_days_ahead"])] != "unavailable":
					snapshot["fair_value"] = day["predicted_price"]
				break

	return snapshot

