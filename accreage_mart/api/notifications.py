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


@frappe.whitelist()
def list_notifications(limit: int = 20) -> list[dict]:
	"""The current user's notifications, newest first."""
	return get_notification_logs(limit=limit)["notification_logs"]


@frappe.whitelist(methods=["POST"])
def mark_notification_seen(name: str) -> dict:
	mark_as_read(name)
	return {"ok": True}


@frappe.whitelist(methods=["POST"])
def mark_all_seen() -> dict:
	mark_all_as_read()
	return {"ok": True}
