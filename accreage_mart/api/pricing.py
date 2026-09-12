"""Whitelisted read endpoints for the pricing engine.

Referenced from the frontend (a later, seller-facing epic) as
``accreage_mart.api.pricing.<fn>``.

Guard matrix
------------
==============================  ================  ===================================
Endpoint                        Caller            Notes
==============================  ================  ===================================
get_price_suggestion             authenticated     category -> gated suggestion (Story 3.9)
get_price_history_and_forecast   authenticated     full history + forecast + accuracy,
                                                    for ForecastChart (Story 3.9)
==============================  ================  ===================================

Neither endpoint is called from anywhere in this epic - both exist, tested, and correct,
ready for whichever later epic wires the seller-facing listing form / dashboard charts.
"""

import frappe
from frappe import _


def _require_authenticated():
	user = frappe.session.user
	if not user or user == "Guest":
		frappe.throw(_("Not authenticated."), frappe.AuthenticationError)


def _forecast_days_for(commodity: str) -> list:
	if not frappe.db.exists("Price Forecast", commodity):
		return []
	forecast_doc = frappe.get_doc("Price Forecast", commodity)
	return [
		{
			"forecast_date": day.forecast_date,
			"horizon_days_ahead": day.horizon_days_ahead,
			"predicted_price": day.predicted_price,
			"lower_bound": day.lower_bound,
			"upper_bound": day.upper_bound,
		}
		for day in forecast_doc.forecast_days
	]


def _tier_for(mape: float, sample_size: int, settings) -> str:
	if sample_size < settings.min_sample_size:
		return "unavailable"
	if mape < settings.direct_suggestion_mape_threshold:
		return "direct"
	if mape < settings.guidance_mape_threshold:
		return "range"
	return "unavailable"


@frappe.whitelist()
def get_price_suggestion(category: str) -> dict:
	"""Resolves a Category to its linked Commodity, gates the result per horizon bucket
	against Price Suggestion Settings, and returns the tier-appropriate data.

	{"available": False, "reason": "no_commodity_linked"} for a Category with no commodity
	link (or an unknown category name) - never an error, since this is called from a seller's
	listing form.

	Otherwise: {"available": True, "commodity_name", "tiers": {"near", "mid", "long"},
	"forecast_days": [...]}. Each tier is "direct" | "range" | "unavailable".
	"""
	_require_authenticated()

	commodity_name = frappe.db.get_value("Category", category, "commodity")
	if not commodity_name:
		return {"available": False, "reason": "no_commodity_linked"}

	commodity = frappe.db.get_value(
		"Commodity",
		commodity_name,
		[
			"mape_1_7d", "mape_8_14d", "mape_15_30d",
			"sample_size_1_7d", "sample_size_8_14d", "sample_size_15_30d",
		],
		as_dict=True,
	)
	settings = frappe.get_single("Price Suggestion Settings")

	tiers = {
		"near": _tier_for(commodity.mape_1_7d, commodity.sample_size_1_7d, settings),
		"mid": _tier_for(commodity.mape_8_14d, commodity.sample_size_8_14d, settings),
		"long": _tier_for(commodity.mape_15_30d, commodity.sample_size_15_30d, settings),
	}

	return {
		"available": True,
		"commodity_name": commodity_name,
		"tiers": tiers,
		"forecast_days": _forecast_days_for(commodity_name),
	}


@frappe.whitelist()
def get_price_history_and_forecast(commodity: str) -> dict:
	"""Full Commodity Price Record history + current Price Forecast + accuracy stats, for
	charting. Raises a clean, catchable DoesNotExistError for an unknown commodity rather
	than a bare 500."""
	_require_authenticated()

	if not frappe.db.exists("Commodity", commodity):
		frappe.throw(_("Unknown commodity: {0}").format(commodity), frappe.DoesNotExistError)

	commodity_doc = frappe.get_doc("Commodity", commodity)

	history = frappe.get_all(
		"Commodity Price Record",
		filters={"commodity": commodity},
		fields=["date", "min_price", "max_price", "average_price", "average_computed"],
		order_by="date asc",
	)

	return {
		"commodity_name": commodity,
		"history": history,
		"forecast_days": _forecast_days_for(commodity),
		"accuracy": {
			"mape_1_7d": commodity_doc.mape_1_7d,
			"mape_8_14d": commodity_doc.mape_8_14d,
			"mape_15_30d": commodity_doc.mape_15_30d,
			"sample_size_1_7d": commodity_doc.sample_size_1_7d,
			"sample_size_8_14d": commodity_doc.sample_size_8_14d,
			"sample_size_15_30d": commodity_doc.sample_size_15_30d,
			"last_evaluated_on": commodity_doc.last_evaluated_on,
		},
	}
