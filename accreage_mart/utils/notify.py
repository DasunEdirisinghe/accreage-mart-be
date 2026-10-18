"""Central notification dispatcher.

One business event -> one ``Notification Log`` row -> a per-channel delivery attempt,
each attempt logged as a ``Notification Delivery Log`` child row. Email sends for real
via ``send_templated_email``; SMS has no sender yet and always logs ``Not Implemented``.

Every future epic (Auctions, Orders, ...) calls ``notify()`` instead of hand-rolling its
own sender — see ``accreage_mart/utils/listing_notify.py`` for the pattern this
generalizes.
"""

import frappe

from accreage_mart.utils.email import send_templated_email, smtp_configured

CHANNEL_EMAIL = "Email"
CHANNEL_SMS = "SMS"

STATUS_PENDING = "Pending"
STATUS_SENT = "Sent"
STATUS_FAILED = "Failed"
STATUS_NOT_IMPLEMENTED = "Not Implemented"


def notify(
	event: str,
	user: str,
	title: str,
	message: str,
	channels: list[str],
	document_type: str | None = None,
	document_name: str | None = None,
	email_template: str | None = None,
	email_context: dict | None = None,
) -> "frappe.model.document.Document":
	"""Create one ``Notification Log`` row for ``user`` plus one ``Notification Delivery
	Log`` child row per requested channel in ``channels`` ("email"/"sms", case-insensitive).

	``event`` is our own taxonomy string (e.g. "listing_rejected"), stored on
	``custom_event_type``. ``document_type``/``document_name`` are the core Notification
	Log fields used for click-through navigation in the frontend.

	Email channel: sends via ``send_templated_email`` when ``email_template`` is given
	(with ``email_context``), otherwise falls back to a plain ``frappe.sendmail`` of
	``title``/``message``. SMS channel: no sender exists yet, always logs
	``Not Implemented``.

	Never raises on a delivery failure — a failed channel is logged on its delivery row,
	not surfaced to the caller, so a notify() call never blocks the business action that
	triggered it.
	"""
	log = frappe.get_doc(
		{
			"doctype": "Notification Log",
			"for_user": user,
			"subject": title,
			"email_content": message,
			"type": "Alert",
			"document_type": document_type,
			"document_name": document_name,
			"custom_event_type": event,
		}
	)

	for channel in channels:
		log.append("custom_delivery_log", _dispatch(channel, user, title, message, email_template, email_context))

	log.insert(ignore_permissions=True)
	frappe.db.commit()
	return log


def _dispatch(channel, user, title, message, email_template, email_context) -> dict:
	canonical = _canonical_channel(channel)
	if canonical == CHANNEL_SMS:
		return {"channel": CHANNEL_SMS, "status": STATUS_NOT_IMPLEMENTED}
	return _send_email(user, title, message, email_template, email_context)


def _canonical_channel(channel: str) -> str:
	normalized = channel.strip().lower()
	if normalized == "email":
		return CHANNEL_EMAIL
	if normalized == "sms":
		return CHANNEL_SMS
	frappe.throw(f"Unknown notification channel: {channel}")


def _send_email(user, title, message, email_template, email_context) -> dict:
	row = {"channel": CHANNEL_EMAIL, "status": STATUS_PENDING}
	try:
		if not smtp_configured():
			row["status"] = STATUS_FAILED
			row["error"] = "SMTP not configured"
			return row

		recipient = frappe.db.get_value("User", user, "email") or user
		if email_template:
			send_templated_email(key=email_template, recipient=recipient, context=email_context or {})
		else:
			frappe.sendmail(recipients=[recipient], subject=title, message=message, now=True)

		row["status"] = STATUS_SENT
		row["sent_at"] = frappe.utils.now_datetime()
	except Exception as e:
		row["status"] = STATUS_FAILED
		row["error"] = str(e)
		frappe.log_error(title=f"Notification email failed: {title}")
	return row
