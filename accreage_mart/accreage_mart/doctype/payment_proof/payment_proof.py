import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import now_datetime

from accreage_mart.utils import listing as lu
from accreage_mart.utils import order as ou


class PaymentProof(Document):
	"""A buyer's bank slip plus the payment details they entered. Written only through the API;
	read-only once reviewed and never deletable (D13 audit trail)."""

	def validate(self):
		if not lu.can_system_write():
			frappe.throw(_("Payment proofs are written by the system only."), frappe.PermissionError)
		self.submitted_on = self.submitted_on or now_datetime()
		if self.is_new():
			return
		before = self.get_doc_before_save()
		if before and before.status != ou.PROOF_SUBMITTED:
			frappe.throw(_("A reviewed payment proof cannot be changed."))

	def on_trash(self):
		frappe.throw(_("A payment proof cannot be deleted."), frappe.PermissionError)
