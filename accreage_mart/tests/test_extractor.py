from pathlib import Path

from frappe.tests.utils import FrappeTestCase

from accreage_mart.pricing.extractor import parse, parse_pettah_page

FIXTURE_PDF = Path(__file__).parent / "fixtures" / "2024-01-01.pdf"


class TestExtractor(FrappeTestCase):
	def test_real_pdf_matches_known_correct_extraction(self):
		# Cross-checked against the standalone pipeline's already-verified
		# price_data/pettah_rice_nadu_1.json and
		# price_data/peliyagoda_up_country_vegetable_tomato.json for 2024-01-01,
		# and the real batch_process_pdfs.py run's reported "59 items" for this date.
		pdf_bytes = FIXTURE_PDF.read_bytes()
		result = parse(pdf_bytes, "2024-01-01")

		self.assertEqual(result["warnings"], [])
		self.assertEqual(len(result["records"]), 59)

		by_name = {r["commodity_name"]: r for r in result["records"]}

		nadu1 = by_name["Rice - Nadu 1"]
		self.assertEqual(nadu1["market"], "Pettah")
		self.assertEqual(nadu1["min_price"], 210.0)
		self.assertEqual(nadu1["max_price"], 215.0)
		self.assertEqual(nadu1["average_price"], 212.5)
		self.assertFalse(nadu1["average_computed"])

		tomato = by_name["Up Country Vegetable - Tomato"]
		self.assertEqual(tomato["market"], "Peliyagoda")
		self.assertEqual(tomato["min_price"], 550.0)
		self.assertEqual(tomato["max_price"], 600.0)
		self.assertEqual(tomato["average_price"], 575.0)
		self.assertTrue(tomato["average_computed"])

		# ignored items never appear in the output
		self.assertNotIn("Dried Chillies - Local", by_name)
		self.assertNotIn("Imported Rice - Raw red", by_name)

	def test_stray_decimal_before_range_is_not_guessed(self):
		# Documented ambiguous case: a rogue decimal value renders before the real range.
		# The parser must not silently absorb the wrong numbers - name only, null price.
		text = "Rice (Rs/kg)\nSamba 1 - 246.67 240.00 - 246.00 243.33"
		records = parse_pettah_page(text, "2024-01-01")
		samba = next(r for r in records if r["item"] == "Samba 1")
		self.assertIsNone(samba["min_price"])
		self.assertIsNone(samba["max_price"])
		self.assertIsNone(samba["average_price"])

	def test_missing_range_dash_is_not_guessed(self):
		# Documented ambiguous case: the range dash is missing entirely.
		text = "Rice (Rs/kg)\nNadu 2 210.00 215.00 212.50"
		records = parse_pettah_page(text, "2024-01-01")
		nadu2 = next(r for r in records if r["item"] == "Nadu 2")
		self.assertIsNone(nadu2["min_price"])
		self.assertIsNone(nadu2["max_price"])
		self.assertIsNone(nadu2["average_price"])

	def test_collision_prone_item_resolves_to_disambiguated_commodity_name(self):
		# "Imported" alone is not unique across categories (see pricing/naming.py) - confirm
		# the same raw item text under two different categories produces two distinct,
		# correctly-disambiguated commodity_names, not a collision.
		text_onion = "Onion\nImported 100.00 - 110.00 105.00"
		text_big_onion = "Big Onion\nImported 150.00 - 160.00 155.00"

		onion_records = parse_pettah_page(text_onion, "2024-01-01")
		big_onion_records = parse_pettah_page(text_big_onion, "2024-01-01")

		self.assertEqual(onion_records[0]["item"], "Imported")
		self.assertEqual(onion_records[0]["category"], "Onion")
		self.assertEqual(big_onion_records[0]["item"], "Imported")
		self.assertEqual(big_onion_records[0]["category"], "Big Onion")

		from accreage_mart.pricing.naming import commodity_name_for

		name_onion = commodity_name_for(onion_records[0]["item"], onion_records[0]["category"])
		name_big_onion = commodity_name_for(big_onion_records[0]["item"], big_onion_records[0]["category"])
		self.assertEqual(name_onion, "Onion - Imported")
		self.assertEqual(name_big_onion, "Big Onion - Imported")
		self.assertNotEqual(name_onion, name_big_onion)
