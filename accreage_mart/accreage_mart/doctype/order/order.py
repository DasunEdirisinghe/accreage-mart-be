import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import flt

from accreage_mart.utils import listing as lu
from accreage_mart.utils import order as ou


class Order(Document):
	"""One order for one listing. Written only through ``api.orders`` (status changes follow the
	state machine in ``utils/order.py``)."""

	def validate(self):
		if not lu.can_system_write():
			frappe.throw(_("Orders are changed by the system only."), frappe.PermissionError)
		if flt(self.quantity) <= 0:
			frappe.throw(_("Quantity must be greater than zero."))
		if self.source == ou.SOURCE_AUCTION and not self.auction:
			frappe.throw(_("An auction order must link its auction."))
		if not self.is_new():
			self._guard_locked_price()

	def _guard_locked_price(self):
		"""REQ-12 - quantity and the agreed price never change once the order is confirmed."""
		before = self.get_doc_before_save()
		if not before or before.status not in ou.PRICE_LOCKED_STATUSES:
			return
		for field in ("quantity", "agreed_unit_price", "total_price", "listing", "buyer", "seller"):
			if self.get(field) != before.get(field):
				frappe.throw(_("The order's {0} cannot change once it is confirmed.").format(field))

	def on_trash(self):
		if not lu.can_system_write():
			frappe.throw(_("An order cannot be deleted."), frappe.PermissionError)
