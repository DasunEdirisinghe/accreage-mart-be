import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import now_datetime

from accreage_mart.utils import listing as lu


class ListingReview(Document):
	"""One immutable row per decision (approve / reject / suspend / resubmit)."""

	def validate(self):
		if not self.is_new():
			frappe.throw(_("Review history cannot be edited."))
		if not lu.can_system_write():
			frappe.throw(_("Review records are written by the system only."), frappe.PermissionError)
		if self.action in ("Rejected", "Suspended") and not (self.reason or "").strip():
			frappe.throw(_("A reason is required."))
		self.reviewer = self.reviewer or frappe.session.user
		self.reviewed_on = self.reviewed_on or now_datetime()

	def on_trash(self):
		frappe.throw(_("Review history cannot be deleted."), frappe.PermissionError)
