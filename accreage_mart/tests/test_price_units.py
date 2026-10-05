from frappe.tests.utils import FrappeTestCase

from accreage_mart.pricing.units import price_unit_label


class TestPriceUnitLabel(FrappeTestCase):
	def test_reads_the_unit_from_the_source_unit_column(self):
		self.assertEqual(price_unit_label("Rs/Egg"), "egg")
		self.assertEqual(price_unit_label("Rs/kg"), "kg")

	def test_reads_it_from_the_item_or_category_name_when_the_column_is_empty(self):
		self.assertEqual(price_unit_label("", "Banana - Ambul(Rs/Kg)"), "kg")
		self.assertEqual(
			price_unit_label(None, "Other Fruits (Rs/Fruit) - Avocado", "Other Fruits (Rs/Fruit)"), "fruit"
		)
		self.assertEqual(price_unit_label("", "Banana - Anamalu (Rs/Fruits)"), "fruit")

	def test_the_first_text_with_a_unit_wins(self):
		self.assertEqual(price_unit_label("Rs/Egg", "Rice - Nadu (Rs/kg)"), "egg")

	def test_defaults_to_kg(self):
		self.assertEqual(price_unit_label("", "Up Country Vegetable - Carrot", None), "kg")
		self.assertEqual(price_unit_label(), "kg")

	def test_spacing_and_case_do_not_matter(self):
		self.assertEqual(price_unit_label("RS / EGGS"), "egg")
