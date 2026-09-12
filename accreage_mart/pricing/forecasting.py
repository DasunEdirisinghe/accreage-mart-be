"""Per-commodity forecast refresh: close out realized predictions, rescore the rolling MAPE,
regenerate the 30-day forecast, then refresh the commodity's CSV mirror. Called once per
commodity, in that order, by accreage_mart.pricing.tasks.refresh_price_forecasts(), which owns
the per-commodity try/except failure isolation - functions here let exceptions propagate to
that caller.
"""

import statistics

import frappe
import pandas as pd
from prophet import Prophet

from accreage_mart.pricing import csv_mirror

FORECAST_HORIZON_DAYS = 30

# How many of the most recent realized predictions per bucket the rolling MAPE is computed
# from, so it stays current rather than averaging in ancient history forever.
TRAILING_WINDOW = 90

HORIZON_BUCKETS = {
	"1_7d": range(1, 8),
	"8_14d": range(8, 15),
	"15_30d": range(15, 31),
}


def refresh_commodity_forecast(commodity: str):
	"""Runs all four steps, in order, for one commodity."""
	_close_out_realized_predictions(commodity)
	_rescore_rolling_mape(commodity)
	_regenerate_forecast(commodity)
	csv_mirror.generate_for_commodity(commodity)


def _close_out_realized_predictions(commodity: str):
	"""Scores every current Price Forecast Day whose target date has now happened and has a
	real price recorded, writing one Forecast Accuracy Log row each - but only once per
	(commodity, forecast_date), so a rerun never double-counts."""
	if not frappe.db.exists("Price Forecast", commodity):
		return  # nothing forecast yet for this commodity (e.g. its very first run)

	forecast_doc = frappe.get_doc("Price Forecast", commodity)
	today = frappe.utils.getdate()
	wrote_any = False

	for day in forecast_doc.forecast_days:
		if day.forecast_date > today:
			continue  # target date hasn't happened yet

		log_name = f"{commodity}-{day.forecast_date}"
		if frappe.db.exists("Forecast Accuracy Log", log_name):
			continue  # already scored on a previous run

		actual = frappe.db.get_value(
			"Commodity Price Record", f"{commodity}-{day.forecast_date}", "average_price"
		)
		if actual is None or actual == 0:
			continue  # real price not ingested yet, or can't divide by a zero actual

		pct_error = abs(actual - day.predicted_price) / actual * 100
		frappe.get_doc(
			{
				"doctype": "Forecast Accuracy Log",
				"commodity": commodity,
				"forecast_date": day.forecast_date,
				"horizon_days_ahead": day.horizon_days_ahead,
				"predicted_price": day.predicted_price,
				"actual_price": actual,
				"pct_error": pct_error,
				"evaluated_on": today,
			}
		).insert(ignore_permissions=True)
		wrote_any = True

	if wrote_any:
		frappe.db.commit()


def _rescore_rolling_mape(commodity: str):
	"""Recomputes each horizon bucket's median MAPE from its trailing TRAILING_WINDOW
	Forecast Accuracy Log rows. A bucket with zero rows is left untouched (not reset to 0) -
	only Story 3.6's bootstrap or an earlier run's score should ever occupy it."""
	updates = {}
	for bucket_name, day_range in HORIZON_BUCKETS.items():
		rows = frappe.get_all(
			"Forecast Accuracy Log",
			filters={"commodity": commodity, "horizon_days_ahead": ["in", list(day_range)]},
			fields=["pct_error"],
			order_by="evaluated_on desc",
			limit_page_length=TRAILING_WINDOW,
		)
		errors = [r["pct_error"] for r in rows]
		if errors:
			updates[f"mape_{bucket_name}"] = round(statistics.median(errors), 2)
			updates[f"sample_size_{bucket_name}"] = len(errors)

	if updates:
		updates["last_evaluated_on"] = frappe.utils.today()
		frappe.db.set_value("Commodity", commodity, updates)
		frappe.db.commit()


def _regenerate_forecast(commodity: str):
	"""Refits Prophet on the commodity's full history and wholesale-overwrites its 30-day
	Price Forecast. Does nothing if there isn't enough history to fit anything meaningful yet
	(the same commodity may still be waiting on Story 3.6's bootstrap, or be brand new)."""
	rows = frappe.get_all(
		"Commodity Price Record",
		filters={"commodity": commodity},
		fields=["date as ds", "average_price as y"],
		order_by="date asc",
	)
	# frappe.get_all() rows are frappe._dict - pd.DataFrame() on those directly can raise
	# "invalid __array_struct__" on some pandas versions (see pricing/evaluation.py).
	df = pd.DataFrame([dict(r) for r in rows])
	if df.empty or len(df) < 30:
		return

	df["ds"] = pd.to_datetime(df["ds"])

	model = Prophet(changepoint_prior_scale=0.05, seasonality_mode="additive", interval_width=0.90)
	model.fit(df[["ds", "y"]])

	future = pd.DataFrame(
		{"ds": pd.date_range(df["ds"].max() + pd.Timedelta(days=1), periods=FORECAST_HORIZON_DAYS)}
	)
	forecast = model.predict(future)

	recalibration_factor = (
		frappe.db.get_single_value("Price Suggestion Settings", "interval_recalibration_factor") or 1.0
	)

	if frappe.db.exists("Price Forecast", commodity):
		doc = frappe.get_doc("Price Forecast", commodity)
		doc.forecast_days = []
	else:
		doc = frappe.get_doc({"doctype": "Price Forecast", "commodity": commodity})

	doc.generated_on = frappe.utils.today()

	for i, row in enumerate(forecast.itertuples(), start=1):
		center = row.yhat
		# Widen the ORIGINAL band around its true center (yhat), rather than scaling
		# yhat_lower/yhat_upper independently - Prophet's interval isn't guaranteed
		# perfectly symmetric around yhat, and this keeps predicted_price exactly centered
		# in the shown range regardless.
		half_width = (row.yhat_upper - row.yhat_lower) / 2 * recalibration_factor
		doc.append(
			"forecast_days",
			{
				"forecast_date": row.ds.date(),
				"horizon_days_ahead": i,
				"predicted_price": round(center, 2),
				"lower_bound": round(center - half_width, 2),
				"upper_bound": round(center + half_width, 2),
			},
		)

	doc.save(ignore_permissions=True)
	frappe.db.commit()
