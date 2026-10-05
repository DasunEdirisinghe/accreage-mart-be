import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import flt

from accreage_mart.utils import listing as lu


class Listing(Document):
	def before_validate(self):
		# A seller creating a listing through the generic API doesn't have to name themselves.
		if self.is_new() and not self.seller:
			self.seller = lu.seller_profile_of()

	def validate(self):
		self._guard_access_and_system_fields()
		self._validate_seller_verified()
		self._validate_basics()
		self._validate_selling_type()
		self._validate_location()
		self._validate_certification()
		self._validate_images()
		self._guard_started_auction_lot()

	def on_trash(self):
		if not lu.is_admin():
			frappe.throw(
				_("Listings cannot be deleted. Archive the listing instead."), frappe.PermissionError
			)

	def after_delete(self):
		if self.auction and frappe.db.exists("Auction", self.auction):
			frappe.delete_doc("Auction", self.auction, force=True, ignore_permissions=True)

	# -- guards -----------------------------------------------------------------------------

	def _guard_access_and_system_fields(self):
		system_write = lu.can_system_write()

		if self.is_new():
			if not lu.is_staff_or_admin() and not lu.owns_listing(self.seller):
				frappe.throw(
					_("You can only create listings for your own seller account."), frappe.PermissionError
				)
			if self.status != lu.PENDING and not system_write:
				frappe.throw(_("A new listing always starts as Pending Approval."))
			for fieldname in lu.LISTING_SYSTEM_FIELDS:
				if fieldname != "status" and self.get(fieldname) and not system_write:
					frappe.throw(_("{0} is managed by the system.").format(self.meta.get_label(fieldname)))
			return

		if not lu.is_staff_or_admin() and not lu.owns_listing(self.seller) and not system_write:
			frappe.throw(_("You can only change your own listings."), frappe.PermissionError)

		for fieldname in lu.LISTING_SYSTEM_FIELDS:
			if self.has_value_changed(fieldname) and not system_write:
				frappe.throw(_("{0} is managed by the system.").format(self.meta.get_label(fieldname)))

	def _validate_seller_verified(self):
		if not self.is_new():
			return
		if not self.seller or not frappe.db.get_value("Seller Profile", self.seller, "verified"):
			frappe.throw(_("Only verified sellers can create listings."))

	# -- field rules ------------------------------------------------------------------------

	def _validate_basics(self):
		self.title = (self.title or "").strip()
		if not self.title:
			frappe.throw(_("Title is required."))
		if len(self.title) > lu.TITLE_MAX:
			frappe.throw(_("Title can be at most {0} characters.").format(lu.TITLE_MAX))
		if not (self.description or "").strip():
			frappe.throw(_("Description is required."))
		if flt(self.quantity_available) < 0 or (self.is_new() and not flt(self.quantity_available)):
			frappe.throw(_("Quantity available must be greater than zero."))

	def _validate_selling_type(self):
		if self.selling_type == lu.DIRECT:
			if self.auction:
				frappe.throw(_("A Direct listing cannot have an auction."))
			if flt(self.price_per_unit) <= 0:
				frappe.throw(_("Price per unit must be greater than zero."))
			if flt(self.min_order_qty) < 0:
				frappe.throw(_("Minimum order quantity cannot be negative."))
			if not flt(self.min_order_qty):
				self.min_order_qty = 1
			if flt(self.quantity_available) and flt(self.min_order_qty) > flt(self.quantity_available):
				frappe.throw(_("Minimum order quantity cannot be more than the available quantity."))
			if flt(self.low_stock_level) < 0:
				frappe.throw(_("Low stock level cannot be negative."))
			self.low_stock_level = flt(self.low_stock_level)
			return

		# Auction: the whole lot goes to the winner, so the Direct-only fields do not apply.
		self.price_per_unit = 0
		self.min_order_qty = 0
		self.low_stock_level = 0
		# The auction record is created right after the listing (create endpoint), so a brand-new
		# listing may not have its link yet; every later save must.
		if not self.is_new() and not self.auction:
			frappe.throw(_("An Auction listing must have an auction."))

	def _validate_location(self):
		self.location = (self.location or "").strip()
		if not self.location:
			frappe.throw(_("Location is required."))
		if self.district not in lu.DISTRICTS:
			frappe.throw(_("Please choose a district from the list."))

		latitude, longitude = flt(self.latitude), flt(self.longitude)
		if not latitude and not longitude:
			self.latitude = self.longitude = 0
			return
		if not latitude or not longitude:
			frappe.throw(_("The location pin needs both latitude and longitude."))
		if not -90 <= latitude <= 90 or not -180 <= longitude <= 180:
			frappe.throw(_("The location pin is outside the valid range."))

	def _validate_certification(self):
		if self.organic:
			self.certification = (self.certification or "").strip()
			if not self.certification:
				frappe.throw(_("Certification details are required for organic produce."))
		else:
			self.certification = None

	def _validate_images(self):
		rows = self.images or []
		if not rows:
			frappe.throw(_("Add at least one image."))
		if len(rows) > lu.MAX_IMAGES:
			frappe.throw(_("A listing can have at most {0} images.").format(lu.MAX_IMAGES))
		covers = [row for row in rows if row.is_cover]
		if len(covers) > 1:
			frappe.throw(_("Choose only one cover image."))
		if not covers:
			rows[0].is_cover = 1  # a single image (or no explicit choice) is the cover by default

	def _guard_started_auction_lot(self):
		"""Lot size and unit are locked once an auction has started."""
		if self.is_new() or self.selling_type != lu.AUCTION or not self.auction:
			return
		previous = self.get_doc_before_save()
		if not previous or previous.status not in lu.STARTED_LOCK_STATUSES:
			return
		start_time = frappe.db.get_value("Auction", self.auction, "start_time")
		if not start_time or not lu.auction_has_started(start_time):
			return
		if self.has_value_changed("quantity_available") or self.has_value_changed("unit"):
			frappe.throw(_("The lot cannot be changed once the auction has started."))
