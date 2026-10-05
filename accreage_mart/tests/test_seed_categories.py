from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from accreage_mart.setup import seed_categories as seed

PREFIX = "ZZ Seed Test"
BANANA = f"{PREFIX} Banana - Ambul(Rs/Kg)"
CARROT = f"{PREFIX} Veg - Carrot"
OTHER = f"{PREFIX} Veg - Other"

SMALL = (
	(f"{PREFIX} Embul", "Fruits", BANANA),
	(f"{PREFIX} Carrot", "Vegetables", CARROT),
	(f"{PREFIX} Missing", "Vegetables", f"{PREFIX} Veg - Nothing Like This"),
	(f"{PREFIX} Coconut", "Other", None),
)


class TestFindCommodity(FrappeTestCase):
	NAMES = ["Banana - Ambul(Rs/Kg)", "Up Country Vegetable - Carrot", "Up Country Vegetable - Beet root",
		"Up Country Vegetable - Beet Root(N'Eliya)", "Rice - Nadu 1"]  # fmt: skip

	def test_exact_names_match(self):
		self.assertEqual(seed.find_commodity("Rice - Nadu 1", self.NAMES), "Rice - Nadu 1")

	def test_case_spacing_and_punctuation_are_ignored(self):
		self.assertEqual(seed.find_commodity("banana - ambul (rs/kg)", self.NAMES), "Banana - Ambul(Rs/Kg)")
		shouted = seed.find_commodity("UP COUNTRY VEGETABLE  -  CARROT", self.NAMES)
		self.assertEqual(shouted, "Up Country Vegetable - Carrot")

	def test_falls_back_to_all_words_and_prefers_the_shortest(self):
		self.assertEqual(seed.find_commodity("Beet root", self.NAMES), "Up Country Vegetable - Beet root")
		self.assertEqual(seed.find_commodity("Vegetable Carrot", self.NAMES), "Up Country Vegetable - Carrot")

	def test_no_match_and_no_hint(self):
		self.assertIsNone(seed.find_commodity("Pineapple", self.NAMES))
		self.assertIsNone(seed.find_commodity(None, self.NAMES))
		self.assertIsNone(seed.find_commodity("", self.NAMES))


class TestCategoryList(FrappeTestCase):
	def test_titles_are_unique_and_areas_are_valid(self):
		titles = [title for title, _, _ in seed.CATEGORIES]
		self.assertEqual(len(titles), len(set(titles)))
		valid = set(frappe.get_meta("Category").get_field("area").options.split("\n"))
		self.assertTrue({area for _, area, _ in seed.CATEGORIES} <= valid)

	def test_there_is_a_catch_all_other_category_without_a_price_link(self):
		other = [row for row in seed.CATEGORIES if row[0] == "Other"]
		self.assertEqual(other, [("Other", "Other", None)])

	def test_priced_hints_follow_the_backfill_naming(self):
		for title, _, hint in seed.CATEGORIES:
			if hint:
				self.assertIn(" - ", hint, title)


class TestRun(FrappeTestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		self._purge()
		for name in (BANANA, CARROT, OTHER):
			frappe.get_doc(
				{
					"doctype": "Commodity",
					"commodity_name": name,
					"market": "Peliyagoda",
					"harti_category": "Test",
				}
			).insert(ignore_permissions=True)

	def tearDown(self):
		self._purge()

	def _purge(self):
		for name in frappe.get_all("Category", filters={"title": ["like", f"{PREFIX}%"]}, pluck="name"):
			frappe.delete_doc("Category", name, force=True, ignore_permissions=True)
		for name in (BANANA, CARROT, OTHER):
			if frappe.db.exists("Commodity", name):
				frappe.delete_doc("Commodity", name, force=True, ignore_permissions=True)

	def _run(self):
		with patch.object(seed, "CATEGORIES", SMALL):
			return seed.run(commit=False)

	def _category(self, title):
		return frappe.db.get_value("Category", {"title": title}, ["name", "area", "commodity"], as_dict=True)

	def test_creates_the_categories_and_links_their_commodities(self):
		summary = self._run()
		self.assertEqual(len(summary["created"]), 4)
		embul = self._category(f"{PREFIX} Embul")
		self.assertEqual((embul.area, embul.commodity), ("Fruits", BANANA))
		self.assertEqual(self._category(f"{PREFIX} Carrot").commodity, CARROT)

	def test_a_category_with_no_commodity_stays_unlinked_on_purpose(self):
		self._run()
		self.assertFalse(self._category(f"{PREFIX} Coconut").commodity)

	def test_a_commodity_that_cannot_be_found_is_reported_and_the_category_still_created(self):
		summary = self._run()
		self.assertTrue(any("Missing" in line for line in summary["commodity_not_found"]))
		self.assertFalse(self._category(f"{PREFIX} Missing").commodity)

	def test_running_twice_creates_nothing_new(self):
		self._run()
		before = frappe.db.count("Category", {"title": ["like", f"{PREFIX}%"]})
		summary = self._run()
		self.assertEqual(summary["created"], [])
		self.assertEqual(len(summary["already_present"]), 4)
		self.assertEqual(frappe.db.count("Category", {"title": ["like", f"{PREFIX}%"]}), before)

	def test_links_an_existing_category_that_has_no_commodity(self):
		frappe.get_doc({"doctype": "Category", "title": f"{PREFIX} Embul", "area": "Fruits"}).insert(
			ignore_permissions=True
		)
		summary = self._run()
		self.assertIn(f"{PREFIX} Embul", summary["linked_existing"])
		self.assertEqual(self._category(f"{PREFIX} Embul").commodity, BANANA)

	def test_never_replaces_a_link_staff_chose(self):
		frappe.get_doc(
			{"doctype": "Category", "title": f"{PREFIX} Embul", "area": "Fruits", "commodity": OTHER}
		).insert(ignore_permissions=True)
		self._run()
		self.assertEqual(self._category(f"{PREFIX} Embul").commodity, OTHER)
