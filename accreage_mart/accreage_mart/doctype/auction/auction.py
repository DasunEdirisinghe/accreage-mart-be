import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import flt, get_datetime

from accreage_mart.utils import listing as lu

TERM_FIELDS = ("min_bid", "start_time", "end_time")
DATETIME_FIELDS = ("start_time", "end_time")


class Auction(Document):
	def validate(self):
		if flt(self.min_bid) <= 0:
			frappe.throw(_("The minimum bid must be greater than zero."))
		self.duration_hours = lu.validate_auction_window(self.start_time, self.end_time)

		self._guard_system_fields()
		self._guard_started_lock()
		if self.is_new() or self._changed("start_time"):
			lu.validate_start_gap(self.start_time)

	def _changed(self, fieldname: str) -> bool:
		previous = self.get_doc_before_save()
		if previous is None:
			return True
		old, new = previous.get(fieldname), self.get(fieldname)
		if fieldname in DATETIME_FIELDS:
			return get_datetime(old) != get_datetime(new)
		return flt(old) != flt(new)

	def _guard_system_fields(self):
		if lu.can_system_write():
			return
		changed = bool(flt(self.ai_fair_value)) if self.is_new() else self._changed("ai_fair_value")
		if changed:
			frappe.throw(_("{0} is managed by the system.").format(self.meta.get_label("ai_fair_value")))

	def _guard_started_lock(self):
		"""Once a published auction has started its terms are locked for everyone."""
		if self.is_new() or not any(self._changed(field) for field in TERM_FIELDS):
			return
		status = frappe.db.get_value("Listing", {"auction": self.name}, "status")
		previous = self.get_doc_before_save()
		if status in lu.STARTED_LOCK_STATUSES and lu.auction_has_started(previous.start_time):
			frappe.throw(_("The auction terms cannot be changed once the auction has started."))
