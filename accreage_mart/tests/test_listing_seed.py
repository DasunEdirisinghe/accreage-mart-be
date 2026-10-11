import frappe
from frappe.tests.utils import FrappeTestCase

from accreage_mart.setup import seed_listings as seed
from accreage_mart.utils import listing as lu


def _ensure_categories() -> None:
	for title in {spec["category"] for spec in seed.LISTINGS}:
		if not frappe.db.exists("Category", {"title": title}):
			frappe.get_doc({"doctype": "Category", "title": title, "area": "Vegetables"}).insert(
				ignore_permissions=True
			)


def _count() -> int:
	return frappe.db.count("Listing", {"seller": ["in", list(seed.SELLERS)]})


class TestSeedListings(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.conf.developer_mode = 1
		frappe.set_user("Administrator")
		_ensure_categories()

	@classmethod
	def tearDownClass(cls):
		frappe.conf.developer_mode = 0
		frappe.db.rollback()
		super().tearDownClass()

	def setUp(self):
		frappe.db.savepoint("seed_test")

	def tearDown(self):
		frappe.db.rollback(save_point="seed_test")

	def test_spec_is_consistent(self):
		titles = [(spec["seller"], spec["title"]) for spec in seed.LISTINGS]
		self.assertEqual(len(titles), len(set(titles)))
		self.assertTrue({spec["seller"] for spec in seed.LISTINGS} <= set(seed.SELLERS))
		types = {bool(spec.get("auction")) for spec in seed.LISTINGS}
		self.assertEqual(types, {True, False})  # both listing types
		statuses = {spec["status"] for spec in seed.LISTINGS}
		self.assertTrue({lu.APPROVED, lu.PENDING, lu.REJECTED, lu.HIDDEN, lu.SUSPENDED} <= statuses)

	def test_creates_everything_with_history_and_is_safe_to_repeat(self):
		before = _count()
		first = seed.run(commit=False)
		self.assertEqual(first["skipped"], [])
		self.assertEqual(len(first["created"]), len(seed.LISTINGS))
		self.assertEqual(_count(), before + len(seed.LISTINGS))

		for email in seed.SELLERS:
			self.assertTrue(frappe.db.get_value("Seller Profile", {"user": email}, "verified"))

		second = seed.run(commit=False)
		self.assertEqual(second["created"], [])
		self.assertEqual(len(second["existing"]), len(seed.LISTINGS))
		self.assertEqual(_count(), before + len(seed.LISTINGS))

	def test_statuses_history_and_auctions(self):
		seed.run(commit=False)
		cabbage = frappe.db.get_value("Listing", {"title": "Organic Cabbage, Certified"})
		actions = frappe.get_all(
			"Listing Review", filters={"listing": cabbage}, order_by="creation asc, name asc", pluck="action"
		)
		self.assertEqual(actions, ["Rejected", "Resubmitted", "Approved"])
		self.assertEqual(frappe.db.get_value("Listing", cabbage, "status"), lu.APPROVED)

		tomatoes = frappe.db.get_value("Listing", {"title": "Tomatoes, Hybrid Greenhouse"})
		self.assertEqual(frappe.db.get_value("Listing", tomatoes, "status"), lu.SUSPENDED)
		self.assertTrue(frappe.db.get_value("Listing", tomatoes, "status_reason"))

		for spec in seed.LISTINGS:
			name = frappe.db.get_value("Listing", {"title": spec["title"], "seller": spec["seller"]})
			self.assertEqual(frappe.db.get_value("Listing", name, "status"), spec["status"])
			if spec.get("auction"):
				self.assertTrue(frappe.db.get_value("Listing", name, "auction"))
			else:
				self.assertFalse(frappe.db.get_value("Listing", name, "auction"))

	def test_missing_category_is_skipped_not_fatal(self):
		spec = seed.LISTINGS[0]
		frappe.db.set_value("Category", {"title": spec["category"]}, "title", "ZZ Renamed Seed Category")
		result = seed.run(commit=False)
		self.assertTrue(any(spec["title"] in line for line in result["skipped"]))
		self.assertEqual(len(result["created"]), len(seed.LISTINGS) - sum(
			1 for s in seed.LISTINGS if s["category"] == spec["category"]
		))

	def test_does_nothing_off_developer_sites(self):
		frappe.conf.developer_mode = 0
		try:
			before = _count()
			result = seed.run(commit=False)
			self.assertEqual(result["created"], [])
			self.assertEqual(_count(), before)
		finally:
			frappe.conf.developer_mode = 1
