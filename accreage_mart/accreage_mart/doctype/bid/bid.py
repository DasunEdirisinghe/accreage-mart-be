import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import now_datetime

from accreage_mart.utils import listing as lu


class Bid(Document):
	"""One immutable bid row. Written only by ``api.auctions.place_bid``."""

	def validate(self):
		if not self.is_new():
			frappe.throw(_("A bid cannot be changed once placed."))
		if not lu.can_system_write():
			frappe.throw(_("Bids are written by the system only."), frappe.PermissionError)
		self.bid_time = self.bid_time or now_datetime()

	def on_trash(self):
		frappe.throw(_("A bid cannot be deleted."), frappe.PermissionError)
