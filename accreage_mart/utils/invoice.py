"""Order invoices (Epic 08, Story 8.4 - REQ-9).

When an order reaches ``completed`` it gets an invoice number (``INV-.YYYY.-.#####``) and, exactly
once, a PDF rendered from the app's ``Order Invoice`` Print Format, stored as a private File attached
to the Order (``invoice_file``). Frappe's File permission then defers to the Order's
``has_permission`` hook, so only the buyer, the seller and staff can open it.

Phase 1: no tax lines, no logo.
"""

import frappe
from frappe import _
from frappe.model.naming import make_autoname
from frappe.utils import flt, format_datetime, get_datetime

from accreage_mart.utils import listing as lu
from accreage_mart.utils import order as ou

PRINT_FORMAT = "Order Invoice"
INVOICE_SERIES = "INV-.YYYY.-.#####"


def new_invoice_no() -> str:
	return make_autoname(INVOICE_SERIES, "Order")


def _money(amount) -> str:
	return f"Rs. {flt(amount):,.2f}"


def _qty(value) -> str:
	return f"{flt(value):g}"


def invoice_context(doc) -> dict:
	"""Everything the ``Order Invoice`` Print Format shows, for ``doc`` (an Order).

	Registered as a Jinja method in ``hooks.py`` so the template stays free of queries.
	"""
	seller = (
		frappe.db.get_value(
			"Seller Profile", doc.seller, ["business_name", "location", "district", "user"], as_dict=True
		)
		or frappe._dict()
	)
	buyer_profile = (
		frappe.db.get_value(
			"Buyer Profile", {"user": doc.buyer}, ["business_name", "location", "district"], as_dict=True
		)
		or frappe._dict()
	)
	unit_price = flt(doc.agreed_unit_price) or flt(doc.listed_unit_price)
	issued = doc.completed_on or doc.modified
	return {
		"invoice_no": doc.invoice_no,
		"invoice_date": format_datetime(get_datetime(issued), "d MMMM yyyy") if issued else "",
		"order_no": doc.name,
		"source": "Auction" if doc.source == ou.SOURCE_AUCTION else "Direct order",
		"seller": {
			"business_name": seller.business_name or "",
			"contact": frappe.db.get_value("User", seller.user, "full_name") if seller.user else "",
			"address": ", ".join(p for p in (seller.location, seller.district) if p),
		},
		"buyer": {
			"business_name": buyer_profile.business_name or "",
			"contact": frappe.db.get_value("User", doc.buyer, "full_name") or "",
			"address": ", ".join(p for p in (buyer_profile.location, buyer_profile.district) if p),
		},
		"item": frappe.db.get_value("Listing", doc.listing, "title") or doc.listing,
		"quantity": _qty(doc.quantity),
		"unit": doc.unit or "",
		"unit_price": _money(unit_price),
		"total": _money(doc.total_price),
		"paid_on": format_datetime(get_datetime(doc.paid_on), "d MMMM yyyy") if doc.paid_on else "",
		"delivered_on": format_datetime(get_datetime(doc.delivered_on), "d MMMM yyyy")
		if doc.delivered_on
		else "",
	}


def render_invoice_html(order) -> str:
	"""The ``Order Invoice`` Print Format's template rendered for ``order``, as a standalone page.

	Rendered directly rather than through Frappe's print view: the print view links the desk's print
	stylesheet by URL, which wkhtmltopdf must then fetch from the site's own host name - that fails
	wherever the server can't reach itself (e.g. ``acm.localhost`` inside the dev container). The
	template carries its own styles, so the invoice needs no network at all."""
	print_format = frappe.get_cached_doc("Print Format", PRINT_FORMAT)
	body = frappe.render_template(print_format.html, {"doc": order})
	return (
		'<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">'
		f"<title>{frappe.utils.escape_html(order.invoice_no or order.name)}</title>"
		f"</head><body>{body}</body></html>"
	)


def render_invoice_pdf(order) -> bytes:
	"""The ``Order Invoice`` Print Format of ``order`` as A4 PDF bytes. A system render: the caller
	has already checked who may complete or download the order."""
	from frappe.utils.pdf import get_pdf

	print_format = frappe.get_cached_doc("Print Format", PRINT_FORMAT)
	options = {"page-size": "A4", "encoding": "UTF-8"}
	for side in ("top", "bottom", "left", "right"):
		options[f"margin-{side}"] = f"{flt(print_format.get(f'margin_{side}')) or 15}mm"
	return get_pdf(render_invoice_html(order), options)


def attach_invoice(order) -> str:
	"""Render the invoice PDF and attach it to ``order`` (a completed, row-locked Order with an
	``invoice_no``) as a private File; sets and saves ``invoice_file``. Returns the file URL.

	Exactly once: an order that already has ``invoice_file`` is returned untouched. The caller runs
	this inside a system write (``api.orders._atomic``)."""
	if order.invoice_file:
		return order.invoice_file
	if order.status != ou.COMPLETED or not order.invoice_no:
		frappe.throw(_("An invoice is made only for a completed order."))

	content = render_invoice_pdf(order)
	ou._verify_pdf(content)  # Frappe's File runs the same pypdf check; fail with a clear message
	file = frappe.get_doc(
		{
			"doctype": "File",
			"file_name": f"{order.invoice_no}.pdf",
			"content": content,
			"is_private": 1,
			"attached_to_doctype": "Order",
			"attached_to_name": order.name,
			"attached_to_field": "invoice_file",
		}
	).insert(ignore_permissions=True)
	# Frappe lets a File's owner write/delete it whatever the attached document says, so the invoice
	# is owned by the system, never by the buyer whose click completed the order. (insert() always
	# stamps the session user as owner, hence the update afterwards.)
	frappe.db.set_value("File", file.name, "owner", "Administrator", update_modified=False)
	order.invoice_file = file.file_url
	with lu.system_write():
		order.save(ignore_permissions=True)
	return file.file_url


def invoice_file_doc(order):
	"""The File row behind ``order.invoice_file``, or None."""
	if not order.invoice_file:
		return None
	name = frappe.db.get_value(
		"File",
		{"file_url": order.invoice_file, "attached_to_doctype": "Order", "attached_to_name": order.name},
		"name",
	)
	return frappe.get_doc("File", name) if name else None
