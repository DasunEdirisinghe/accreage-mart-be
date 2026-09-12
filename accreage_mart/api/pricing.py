"""Whitelisted endpoints for the pricing engine.

Referenced from the frontend as ``accreage_mart.api.pricing.<fn>``.

Guard matrix
------------
==============================  ================  ===================================
Endpoint                        Caller            Notes
==============================  ================  ===================================
get_price_suggestion             authenticated     category -> gated suggestion (Story 3.9;
                                                    not called from anywhere in this epic -
                                                    ready for a later, seller-facing epic)
get_price_history_and_forecast   authenticated     full history + forecast + accuracy,
                                                    for ForecastChart (Story 3.9; same as above)
list_categories                  Staff/Admin       all Category rows, for /admin/categories
                                                    (Story 3.15)
upsert_category                  Staff/Admin       create (no name) or update (name given)
                                                    a Category (Story 3.15)
delete_category                  Staff/Admin       removes a Category - allowed even when
                                                    linked to a commodity (Story 3.15)
list_commodities                 Staff/Admin       read-only commodity list for the Category
                                                    form's commodity picker (Story 3.15)
list_commodities_overview        Staff/Admin       richer commodity list (market, category,
                                                    is_active, last_evaluated_on, mape_1_7d)
                                                    for /admin/commodities (Story 3.16)
get_commodity                    Staff/Admin       one commodity's full fields + its forecast
                                                    days, for /admin/commodities/[id] (Story
                                                    3.16); view-only, reuses
                                                    get_price_history_and_forecast internally
==============================  ================  ===================================
"""

import frappe
from frappe import _

from accreage_mart.utils import registration

CATEGORY_AREAS = {"Fruits", "Vegetables", "Fertilizer", "Tools", "Rice", "Other"}


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


@frappe.whitelist()
def list_categories() -> list:
	"""Every Category row, for the /admin/categories management page. Staff/Admin only."""
	registration.require_staff()
	return frappe.get_all(
		"Category",
		fields=["name", "title", "area", "commodity"],
		order_by="title asc",
	)


@frappe.whitelist()
def upsert_category(title: str, area: str, commodity: str = None, name: str = None) -> dict:
	"""Creates a new Category, or updates the one named by `name` when given. Staff/Admin only.
	This never creates a Commodity - `commodity` must already exist if provided."""
	registration.require_staff()

	if not title or not title.strip():
		frappe.throw(_("Title is required."))
	if area not in CATEGORY_AREAS:
		frappe.throw(_("Invalid area: {0}").format(area))
	if commodity and not frappe.db.exists("Commodity", commodity):
		frappe.throw(_("Unknown commodity: {0}").format(commodity))

	if name:
		if not frappe.db.exists("Category", name):
			frappe.throw(_("Category not found: {0}").format(name), frappe.DoesNotExistError)
		doc = frappe.get_doc("Category", name)
		doc.title = title.strip()
		doc.area = area
		doc.commodity = commodity or None
		doc.save(ignore_permissions=True)
	else:
		doc = frappe.get_doc(
			{
				"doctype": "Category",
				"title": title.strip(),
				"area": area,
				"commodity": commodity or None,
			}
		)
		doc.insert(ignore_permissions=True)

	frappe.db.commit()
	return {"name": doc.name, "title": doc.title, "area": doc.area, "commodity": doc.commodity}


@frappe.whitelist()
def delete_category(name: str) -> dict:
	"""Removes a Category. Deleting one linked to a commodity is allowed - it doesn't touch
	the commodity or its data. Staff/Admin only."""
	registration.require_staff()
	if not frappe.db.exists("Category", name):
		frappe.throw(_("Category not found: {0}").format(name), frappe.DoesNotExistError)
	frappe.delete_doc("Category", name, ignore_permissions=True)
	frappe.db.commit()
	return {"ok": True}


@frappe.whitelist()
def list_commodities(search: str = None) -> list:
	"""Read-only commodity list for the Category form's commodity picker. Staff/Admin only.
	Since Commodity autonames as field:commodity_name, `name` IS the display value already -
	no separate label lookup needed."""
	registration.require_staff()
	filters = {}
	if search:
		filters["commodity_name"] = ["like", f"%{search}%"]
	return frappe.get_all(
		"Commodity",
		filters=filters,
		fields=["name"],
		order_by="name asc",
		limit_page_length=200,
	)


@frappe.whitelist()
def list_commodities_overview() -> list:
	"""Every Commodity row's admin-list fields, for /admin/commodities (Story 3.16). Staff/Admin
	only; this page and get_commodity below are both view-only - no create/edit/delete."""
	registration.require_staff()
	return frappe.get_all(
		"Commodity",
		fields=["name", "harti_category", "market", "is_active", "last_evaluated_on", "mape_1_7d"],
		order_by="name asc",
	)


@frappe.whitelist()
def get_commodity(name: str) -> dict:
	"""One Commodity's full fields plus its current 30-day forecast, for
	/admin/commodities/[id] (Story 3.16). Staff/Admin only, view-only. Reuses
	get_price_history_and_forecast for the forecast_days query rather than duplicating it -
	the history/accuracy parts of that result are discarded since this view doesn't use them."""
	registration.require_staff()
	if not frappe.db.exists("Commodity", name):
		frappe.throw(_("Unknown commodity: {0}").format(name), frappe.DoesNotExistError)

	commodity_doc = frappe.get_doc("Commodity", name)
	forecast = get_price_history_and_forecast(name)

	return {
		"name": commodity_doc.name,
		"harti_category": commodity_doc.harti_category,
		"market": commodity_doc.market,
		"unit": commodity_doc.unit,
		"is_active": commodity_doc.is_active,
		"mape_1_7d": commodity_doc.mape_1_7d,
		"mape_8_14d": commodity_doc.mape_8_14d,
		"mape_15_30d": commodity_doc.mape_15_30d,
		"sample_size_1_7d": commodity_doc.sample_size_1_7d,
		"sample_size_8_14d": commodity_doc.sample_size_8_14d,
		"sample_size_15_30d": commodity_doc.sample_size_15_30d,
		"last_evaluated_on": commodity_doc.last_evaluated_on,
		"forecast_days": forecast["forecast_days"],
	}
