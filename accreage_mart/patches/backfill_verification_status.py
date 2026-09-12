"""Story 2.2: backfill the new tri-state verification_status from the existing
verified boolean, so already-verified sellers/buyers don't reappear as "Pending"."""

import frappe


def execute():
	for doctype in ("Buyer Profile", "Seller Profile"):
		if not frappe.db.exists("DocType", doctype):
			continue
		for row in frappe.get_all(doctype, fields=["name", "verified", "verification_status"]):
			if row.verification_status:
				continue
			status = "Approved" if row.verified else "Pending"
			frappe.db.set_value(doctype, row.name, "verification_status", status, update_modified=False)
	frappe.db.commit()
