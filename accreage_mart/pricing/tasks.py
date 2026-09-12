"""Daily scheduled tasks for the pricing engine. Registered in hooks.py under
scheduler_events.daily (ingest_daily_prices only - everything else in the daily flow is
enqueued from inside it, not separately scheduled).
"""

import datetime

import frappe
import requests

from accreage_mart.pricing.extractor import parse as parse_pdf
from accreage_mart.pricing.harti_client import download_latest_pdf, fetch_real_pdf_index

# Safety bound on how far back a single run will try to catch up - not expected to trigger in
# normal operation (including the very first run after the Story 3.2 backfill, which typically
# only needs to close a gap of days, not months). Guards against a genuinely broken state (e.g.
# Commodity Price Record unexpectedly empty) walking an unbounded date range in one job.
MAX_LOOKBACK_DAYS = 90


def ingest_daily_prices():
	"""Catches up every date from the latest one actually present in Commodity Price Record
	(not from Price Ingestion Log, which is empty on day one) through yesterday. A failure on
	one date never aborts the rest of the run, and a failure in the ingestion machinery itself
	(e.g. the index fetch raising something other than the plain network error it already
	handles) never prevents the forecast refresh from being enqueued - it always runs against
	whatever data currently exists, success or total ingestion failure.
	"""
	try:
		_run_ingestion()
	except Exception as e:
		frappe.log_error(
			title="Price ingestion: total failure",
			message=f"ingest_daily_prices aborted unexpectedly: {e}",
		)

	frappe.enqueue(
		"accreage_mart.pricing.tasks.refresh_price_forecasts",
		queue="long",
	)


def _run_ingestion():
	today = frappe.utils.getdate()
	start_date = _next_date_to_attempt(today)
	end_date = today - datetime.timedelta(days=1)

	if start_date > end_date:
		return

	real_index = fetch_real_pdf_index()
	session = requests.Session()

	current = start_date
	while current <= end_date:
		_ingest_one_date(current, real_index, session)
		current += datetime.timedelta(days=1)


def _next_date_to_attempt(today: datetime.date, max_date=None) -> datetime.date:
	"""max_date is normally looked up from the DB; callers (tests) may pass it directly
	instead, so this function's date arithmetic is testable without monkeypatching the
	shared, globally-used frappe.db.get_value."""
	floor = today - datetime.timedelta(days=MAX_LOOKBACK_DAYS)
	if max_date is None:
		max_date = frappe.db.get_value("Commodity Price Record", {}, "MAX(date)")
	if not max_date:
		return floor
	start = frappe.utils.getdate(max_date) + datetime.timedelta(days=1)
	return max(start, floor)


def _ingest_one_date(d: datetime.date, real_index: dict, session: requests.Session):
	result = download_latest_pdf(d, session=session, real_index=real_index)

	if result["status"] == "not_found":
		# Expected, normal outcome (HARTI simply didn't publish that day) - logged to Price
		# Ingestion Log for the record, but not alarming enough for the Error Log list.
		_log(d, "not_found", 0, result["detail"])
		return

	if result["status"] == "failed":
		frappe.log_error(
			title="Price ingestion: download failure",
			message=f"date={d.isoformat()}: {result['detail']}",
		)
		_log(d, "failed", 0, result["detail"])
		return

	try:
		parsed = parse_pdf(result["content"], d.isoformat())
	except Exception as e:
		frappe.log_error(
			title="Price ingestion: parse failure",
			message=f"date={d.isoformat()}: {e}",
		)
		_log(d, "failed", 0, f"parse error: {e}")
		return

	updated_count = 0
	for rec in parsed["records"]:
		if rec["average_price"] is None:
			continue  # nothing to store - matches the standalone pipeline's "don't interpolate" rule
		_upsert_price_record(rec)
		updated_count += 1

	_log(d, "parsed", updated_count, "; ".join(parsed["warnings"]))


def _upsert_price_record(rec: dict):
	if not frappe.db.exists("Commodity", rec["commodity_name"]):
		frappe.get_doc(
			{
				"doctype": "Commodity",
				"commodity_name": rec["commodity_name"],
				"harti_category": rec.get("harti_category") or "",
				"market": rec["market"],
				"unit": rec.get("unit") or "",
			}
		).insert(ignore_permissions=True)

	record_name = f"{rec['commodity_name']}-{rec['date']}"
	if frappe.db.exists("Commodity Price Record", record_name):
		return  # idempotent - already ingested (e.g. a rerun after a partial earlier failure)

	frappe.get_doc(
		{
			"doctype": "Commodity Price Record",
			"commodity": rec["commodity_name"],
			"date": rec["date"],
			"min_price": rec["min_price"],
			"max_price": rec["max_price"],
			"average_price": rec["average_price"],
			"average_computed": 1 if rec["average_computed"] else 0,
		}
	).insert(ignore_permissions=True)


def _log(d: datetime.date, status: str, commodities_updated_count: int, detail: str):
	name = d.isoformat()
	if frappe.db.exists("Price Ingestion Log", name):
		doc = frappe.get_doc("Price Ingestion Log", name)
		doc.status = status
		doc.commodities_updated_count = commodities_updated_count
		doc.detail = detail
		doc.save(ignore_permissions=True)
	else:
		frappe.get_doc(
			{
				"doctype": "Price Ingestion Log",
				"date": name,
				"status": status,
				"commodities_updated_count": commodities_updated_count,
				"detail": detail,
			}
		).insert(ignore_permissions=True)
	frappe.db.commit()


def refresh_price_forecasts(commodities=None):
	"""Runs the close-out/rescore/regenerate sequence (accreage_mart.pricing.forecasting) for
	every active commodity. One commodity's failure is logged via frappe.log_error and
	skipped - it never blocks the rest.

	commodities is normally looked up from the DB (every active Commodity); tests may pass an
	explicit list instead, so a test run never touches real commodities' forecast/accuracy
	data (same pattern as Story 3.6's bootstrap_all)."""
	from accreage_mart.pricing.forecasting import refresh_commodity_forecast

	if commodities is None:
		commodities = frappe.get_all("Commodity", filters={"is_active": 1}, pluck="name")

	for commodity in commodities:
		try:
			refresh_commodity_forecast(commodity)
		except Exception as e:
			frappe.log_error(
				title="Price forecast refresh failure",
				message=f"commodity={commodity}: {e}",
			)
