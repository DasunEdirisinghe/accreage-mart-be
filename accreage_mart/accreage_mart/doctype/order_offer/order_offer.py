import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import now_datetime

from accreage_mart.utils import listing as lu
from accreage_mart.utils import order as ou


class OrderOffer(Document):
	"""One price offer in an order's negotiation. Append-only: once made, only its ``outcome``
	moves on (Open -> Accepted / Countered / Declined / Expired), and only through the API."""

	def validate(self):
		if not lu.can_system_write():
			frappe.throw(_("Offers are written by the system only."), frappe.PermissionError)
		self.offered_on = self.offered_on or now_datetime()
		if self.is_new():
			return
		before = self.get_doc_before_save()
		if not before:
			return
		for field in ("order", "offered_by", "party", "unit_price", "offered_on"):
			if self.get(field) != before.get(field):
				frappe.throw(_("An offer cannot be changed once made."))
		if before.outcome != ou.OFFER_OPEN and self.outcome != before.outcome:
			frappe.throw(_("This offer has already been closed."))

	def on_trash(self):
		frappe.throw(_("An offer cannot be deleted."), frappe.PermissionError)
