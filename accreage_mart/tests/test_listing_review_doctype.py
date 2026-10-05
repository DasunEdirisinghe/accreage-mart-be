import frappe
from frappe.tests.utils import FrappeTestCase

from accreage_mart.tests.listing_fixtures import (
	SELLER_A,
	SELLER_B,
	make_category,
	make_listing,
	make_seller,
	purge,
)
from accreage_mart.utils import listing as lu


class TestListingReviewDoctype(FrappeTestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		purge(SELLER_A, SELLER_B)
		self.seller = make_seller(SELLER_A)
		make_seller(SELLER_B)
		self.listing = make_listing(self.seller, make_category())

	def tearDown(self):
		purge(SELLER_A, SELLER_B)

	def _review(self, **overrides):
		values = {"doctype": "Listing Review", "listing": self.listing.name, "action": "Approved"}
		values.update(overrides)
		with lu.system_write():
			return frappe.get_doc(values).insert(ignore_permissions=True)

	def test_review_is_stored_with_reviewer_and_time_defaults(self):
		review = self._review()
		self.assertTrue(review.name.startswith("LRV-"))
		self.assertEqual(review.reviewer, "Administrator")
		self.assertTrue(review.reviewed_on)

	def test_reject_and_suspend_need_a_reason_approve_does_not(self):
		for action in ("Rejected", "Suspended"):
			with self.assertRaises(frappe.ValidationError):
				self._review(action=action, reason="  ")
		self._review(action="Rejected", reason="Photos are unclear.")
		self._review(action="Approved")

	def test_resubmission_keeps_the_seller_note(self):
		review = self._review(action="Resubmitted", seller_note="Replaced the photos.")
		self.assertEqual(review.seller_note, "Replaced the photos.")

	def test_a_seller_cannot_write_review_records(self):
		frappe.set_user(SELLER_A)
		with self.assertRaises((frappe.PermissionError, frappe.ValidationError)):
			frappe.get_doc(
				{"doctype": "Listing Review", "listing": self.listing.name, "action": "Approved"}
			).insert()

	def test_reviews_cannot_be_edited_or_deleted(self):
		review = self._review(action="Rejected", reason="Missing details.")
		review.reason = "changed"
		with lu.system_write(), self.assertRaises(frappe.ValidationError):
			review.save(ignore_permissions=True)
		with self.assertRaises(frappe.PermissionError):
			frappe.delete_doc("Listing Review", review.name, force=True, ignore_permissions=True)

	def test_only_the_owner_and_staff_can_read_reviews(self):
		review = self._review()
		frappe.set_user(SELLER_B)
		self.assertNotIn(review.name, frappe.get_list("Listing Review", pluck="name"))
		frappe.set_user(SELLER_A)
		self.assertIn(review.name, frappe.get_list("Listing Review", pluck="name"))

	def test_a_listing_with_history_cannot_be_deleted(self):
		self._review()
		with self.assertRaises(frappe.LinkExistsError):
			frappe.delete_doc("Listing", self.listing.name)
