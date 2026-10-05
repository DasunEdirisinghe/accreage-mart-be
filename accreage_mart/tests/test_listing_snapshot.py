from unittest.mock import patch

from frappe.tests.utils import FrappeTestCase
from frappe.utils import add_to_date, now_datetime

from accreage_mart.utils.listing_snapshot import suggestion_snapshot

TARGET = "accreage_mart.utils.listing_snapshot.get_price_suggestion"


def _day(offset: int, predicted: float = 120, lower: float = 100, upper: float = 140) -> dict:
	return {
		"forecast_date": add_to_date(now_datetime(), days=offset).date(),
		"horizon_days_ahead": offset,
		"predicted_price": predicted,
		"lower_bound": lower,
		"upper_bound": upper,
	}


class TestSuggestionSnapshot(FrappeTestCase):
	def test_no_suggestion_gives_an_empty_snapshot(self):
		with patch(TARGET, return_value={"available": False, "reason": "no_commodity_linked"}):
			self.assertEqual(suggestion_snapshot("Any"), {})

	def test_untrusted_near_tier_gives_no_range(self):
		result = {
			"available": True,
			"tiers": {"near": "unavailable", "mid": "range", "long": "range"},
			"forecast_days": [_day(1)],
		}
		with patch(TARGET, return_value=result):
			self.assertNotIn("min", suggestion_snapshot("Any"))

	def test_range_comes_from_the_nearest_day(self):
		result = {
			"available": True,
			"tiers": {"near": "direct", "mid": "range", "long": "range"},
			"forecast_days": [_day(3, lower=90, upper=130), _day(1, lower=100, upper=140)],
		}
		with patch(TARGET, return_value=result):
			snapshot = suggestion_snapshot("Any")
		self.assertEqual((snapshot["min"], snapshot["max"]), (100, 140))
		self.assertNotIn("fair_value", snapshot)

	def test_fair_value_is_the_prediction_on_the_auction_start_date(self):
		days = [_day(1), _day(2, predicted=111), _day(3, predicted=125)]
		tiers = {"near": "direct", "mid": "range", "long": "range"}
		result = {"available": True, "tiers": tiers, "forecast_days": days}
		with patch(TARGET, return_value=result):
			snapshot = suggestion_snapshot("Any", auction_start=add_to_date(now_datetime(), days=2))
		self.assertEqual(snapshot["fair_value"], 111)

	def test_fair_value_is_skipped_when_that_horizon_is_untrusted(self):
		days = [_day(1), _day(9, predicted=130)]
		tiers = {"near": "direct", "mid": "unavailable", "long": "range"}
		result = {"available": True, "tiers": tiers, "forecast_days": days}
		with patch(TARGET, return_value=result):
			snapshot = suggestion_snapshot("Any", auction_start=add_to_date(now_datetime(), days=9))
		self.assertNotIn("fair_value", snapshot)
