"""In-app notification read / mark-seen endpoints.

Delegates to Frappe's own ``frappe.desk.doctype.notification_log.notification_log``
module rather than re-implementing list/mark-read logic from scratch: its
``get_notification_logs`` already scopes to the current user (via that doctype's
``get_permission_query_conditions``, which filters to ``for_user`` for anyone but
Administrator) and returns our custom fields as ordinary columns; its
``mark_as_read``/``mark_all_as_read`` already implement exactly the single/all
seen-state semantics this epic needs. See handoff open question Q1.
"""

import frappe
from frappe.desk.doctype.notification_log.notification_log import (
	get_notification_logs,
	mark_all_as_read,
	mark_as_read,
)
from frappe.utils import cint

MAX_PAGE_SIZE = 100


@frappe.whitelist()
def list_notifications(limit: int = 20) -> list[dict]:
	"""The current user's notifications, newest first. Used by the bell dropdown."""
	return get_notification_logs(limit=limit)["notification_logs"]


@frappe.whitelist()
def list_notifications_page(page: int = 1, page_size: int = 20) -> dict:
	"""The current user's full notification history, paginated, newest first.

	Used by the "view all" page — unlike ``list_notifications``, this filters by
	``for_user`` explicitly rather than relying on Notification Log's permission query
	conditions, since it also needs an exact count for pagination."""
	page = max(1, cint(page) or 1)
	page_size = min(max(1, cint(page_size) or 20), MAX_PAGE_SIZE)
	filters = {"for_user": frappe.session.user}

	total = frappe.db.count("Notification Log", filters=filters)
	rows = frappe.get_list(
		"Notification Log",
		filters=filters,
		fields=["*"],
		order_by="modified desc",
		limit_start=(page - 1) * page_size,
		limit_page_length=page_size,
	)
	return {
		"notification_logs": rows,
		"page": page,
		"page_size": page_size,
		"total": total,
		"has_more": page * page_size < total,
	}


@frappe.whitelist(methods=["POST"])
def mark_notification_seen(name: str) -> dict:
	mark_as_read(name)
	return {"ok": True}


@frappe.whitelist(methods=["POST"])
def mark_all_seen() -> dict:
	mark_all_as_read()
	return {"ok": True}
