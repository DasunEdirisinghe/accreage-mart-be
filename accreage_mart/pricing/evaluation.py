"""Bootstrap accuracy evaluation: seeds each commodity's initial per-horizon MAPE via the
interim report's own rolling-origin cross-validation protocol (Section 3.1.3) - 6 cutoffs, 45
days apart, each fitting on data up to the cutoff and forecasting 30 days ahead - so accuracy
numbers exist from day one instead of a 20-30 day cold start on the daily self-scoring loop
(Story 3.7).

Run once, after the Story 3.2 backfill, via bench execute:
	bench --site <site> execute accreage_mart.pricing.evaluation.bootstrap_all

Not scheduled. Expect this to take a while for all 65 commodities (up to 6 Prophet fits each).
"""

import statistics

import frappe
import pandas as pd
from prophet import Prophet

CUTOFF_SPACING_DAYS = 45
NUM_CUTOFFS = 6
FORECAST_HORIZON_DAYS = 30

# A cutoff needs at least this much prior history to fit anything meaningful. Matches the
# order of magnitude of the sufficiency workbook's own "date span" suitability floor.
MIN_HISTORY_BEFORE_CUTOFF_DAYS = 180

# Fewer usable cutoffs than this and the result isn't a meaningful cross-validation - skip the
# commodity entirely rather than seed a score from 1 lucky/unlucky fit.
MIN_USABLE_CUTOFFS = 2

HORIZON_BUCKETS = {
	"1_7d": range(1, 8),
	"8_14d": range(8, 15),
	"15_30d": range(15, 31),
}


def _load_history(commodity: str) -> pd.DataFrame:
	rows = frappe.get_all(
		"Commodity Price Record",
		filters={"commodity": commodity},
		fields=["date as ds", "average_price as y"],
		order_by="date asc",
	)
	# frappe.get_all() returns frappe._dict rows; passing those directly to pd.DataFrame()
	# trips numpy's array conversion on some pandas versions ("invalid __array_struct__") -
	# plain dicts avoid it.
	df = pd.DataFrame([dict(r) for r in rows])
	if df.empty:
		return df
	df["ds"] = pd.to_datetime(df["ds"])
	return df


def _cutoff_dates(df: pd.DataFrame) -> list:
	"""Up to 6 cutoffs, 45 days apart, each with enough prior history to fit on and a full
	30-day window of following actuals to evaluate against. Fewer than 6 come back if the
	series is too short for all of them - that's fine, bootstrap_accuracy() just uses whatever
	usable cutoffs exist (down to MIN_USABLE_CUTOFFS)."""
	last_date = df["ds"].max()
	first_date = df["ds"].min()

	last_cutoff = last_date - pd.Timedelta(days=FORECAST_HORIZON_DAYS)
	candidates = sorted(last_cutoff - pd.Timedelta(days=CUTOFF_SPACING_DAYS * k) for k in range(NUM_CUTOFFS))

	return [c for c in candidates if c - first_date >= pd.Timedelta(days=MIN_HISTORY_BEFORE_CUTOFF_DAYS)]


def bootstrap_accuracy(commodity: str) -> dict:
	"""Runs the rolling-origin protocol for one commodity and writes its mape_*/sample_size_*/
	last_evaluated_on fields. Returns a dict describing what happened - {"skipped": True, ...}
	if there wasn't enough history for even MIN_USABLE_CUTOFFS cutoffs."""
	df = _load_history(commodity)
	if df.empty:
		return {"skipped": True, "reason": "no history"}

	cutoffs = _cutoff_dates(df)
	if len(cutoffs) < MIN_USABLE_CUTOFFS:
		return {
			"skipped": True,
			"reason": f"only {len(cutoffs)} usable cutoff(s), need >= {MIN_USABLE_CUTOFFS}",
		}

	bucket_errors = {name: [] for name in HORIZON_BUCKETS}

	for cutoff in cutoffs:
		train = df[df["ds"] <= cutoff]
		actuals = df[(df["ds"] > cutoff) & (df["ds"] <= cutoff + pd.Timedelta(days=FORECAST_HORIZON_DAYS))]
		if actuals.empty or len(train) < 30:
			continue

		model = Prophet(changepoint_prior_scale=0.05, seasonality_mode="additive", interval_width=0.90)
		model.fit(train[["ds", "y"]])

		future = pd.DataFrame(
			{"ds": pd.date_range(cutoff + pd.Timedelta(days=1), periods=FORECAST_HORIZON_DAYS)}
		)
		forecast = model.predict(future)

		merged = actuals.merge(forecast[["ds", "yhat"]], on="ds", how="inner")
		for _, row in merged.iterrows():
			if pd.isna(row["y"]) or row["y"] == 0:
				continue
			horizon = (row["ds"] - cutoff).days
			pct_error = abs(row["y"] - row["yhat"]) / row["y"] * 100
			for bucket_name, day_range in HORIZON_BUCKETS.items():
				if horizon in day_range:
					bucket_errors[bucket_name].append(pct_error)
					break

	updates = {}
	for bucket_name, errors in bucket_errors.items():
		if errors:
			updates[f"mape_{bucket_name}"] = round(statistics.median(errors), 2)
			updates[f"sample_size_{bucket_name}"] = len(errors)
		else:
			updates[f"sample_size_{bucket_name}"] = 0
	updates["last_evaluated_on"] = frappe.utils.today()

	frappe.db.set_value("Commodity", commodity, updates)
	frappe.db.commit()

	return {"skipped": False, **updates}


def bootstrap_all(commodities=None):
	"""commodities is normally looked up from the DB (every active Commodity); tests may pass
	an explicit list instead, so a test run never touches real commodities' accuracy fields."""
	if commodities is None:
		commodities = frappe.get_all("Commodity", filters={"is_active": 1}, pluck="name")
	seeded, skipped = 0, []

	for commodity in commodities:
		result = bootstrap_accuracy(commodity)
		if result.get("skipped"):
			skipped.append((commodity, result.get("reason")))
		else:
			seeded += 1
		print(f"  {commodity}: {'skipped - ' + result['reason'] if result.get('skipped') else 'seeded'}")

	print(f"Bootstrap accuracy done: {seeded} commodities seeded, {len(skipped)} skipped.")
	return {"seeded": seeded, "skipped": skipped}
