"""Set-password / reset-password key handling and the emails that carry the link.

The set-password page lives on the frontend (``<frontend>/set-password?key=...``).
Delivery falls back to returning the link when the site has no outgoing mail
configured, so the flow is usable in development without SMTP.
"""

import frappe
from frappe import _
from frappe.utils import get_datetime, now_datetime
from frappe.utils.password import update_password as _set_user_password

from accreage_mart.utils.email import send_templated_email, smtp_configured

DEFAULT_KEY_EXPIRY_SECONDS = 3600


def frontend_url() -> str:
	return (frappe.conf.get("frontend_url") or "http://localhost:3000").rstrip("/")


def _key_expiry_seconds() -> int:
	value = frappe.db.get_single_value("System Settings", "reset_password_link_expiry_duration")
	try:
		return int(value) if value else DEFAULT_KEY_EXPIRY_SECONDS
	except (TypeError, ValueError):
		return DEFAULT_KEY_EXPIRY_SECONDS


def _issue_key(user_name: str) -> str:
	key = frappe.generate_hash(length=32)
	frappe.db.set_value(
		"User",
		user_name,
		{
			"reset_password_key": key,
			"last_reset_password_key_generated_on": now_datetime(),
		},
		update_modified=False,
	)
	return key


def _link_for(user_name: str) -> str:
	return f"{frontend_url()}/set-password?key={_issue_key(user_name)}"


def _expiry_note() -> str:
	hours = max(1, _key_expiry_seconds() // 3600)
	return f"This link expires in {hours} hour{'s' if hours != 1 else ''}."


def _expose_link() -> bool:
	"""Return the link in the API response when it can't (or might not) be emailed."""
	return not smtp_configured() or bool(frappe.conf.get("developer_mode"))


def issue_set_password_link(user_name: str) -> tuple[str, str]:
	"""Mint a fresh set-password link + its expiry note, without sending any email.

	For flows that build their own message (account approval) rather than the
	generic onboarding/reset copy that send_onboarding_link/send_password_reset use."""
	return _link_for(user_name), _expiry_note()


def should_expose_link() -> bool:
	"""Whether the caller's API response should include the link directly (dev /
	no-SMTP sites) — same rule send_onboarding_link and send_password_reset use."""
	return _expose_link()


def send_onboarding_link(user_name: str, *, staff: bool = False) -> str | None:
	"""Email a set-password link to a freshly created (invited) account. Returns the
	link too on dev / no-SMTP sites so the flow is testable without email."""
	link = _link_for(user_name)
	full_name = frappe.db.get_value("User", user_name, "full_name") or "there"

	if smtp_configured():
		send_templated_email(
			key="staff_invite" if staff else "welcome_member",
			recipient=user_name,
			context={"full_name": full_name},
			cta_label="Set my password",
			cta_url=link,
			footer_note=_expiry_note(),
		)

	return link if _expose_link() else None


def send_password_reset(user_name: str) -> str | None:
	link = _link_for(user_name)
	full_name = frappe.db.get_value("User", user_name, "full_name") or "there"

	if smtp_configured():
		send_templated_email(
			key="password_reset",
			recipient=user_name,
			context={"full_name": full_name},
			cta_label="Choose a new password",
			cta_url=link,
			footer_note=_expiry_note(),
		)

	return link if _expose_link() else None


def is_key_valid(key: str) -> bool:
	"""Whether a set-password key exists and hasn't expired (does not consume it)."""
	if not key:
		return False
	user_name = frappe.db.get_value("User", {"reset_password_key": key})
	if not user_name:
		return False
	generated_on = frappe.db.get_value("User", user_name, "last_reset_password_key_generated_on")
	if generated_on:
		age = (now_datetime() - get_datetime(generated_on)).total_seconds()
		if age > _key_expiry_seconds():
			return False
	return True


def consume_key(key: str, new_password: str) -> str:
	"""Validate the key, set the password, activate the account. Returns the user id."""
	if not key:
		frappe.throw(_("This link is invalid."))

	user_name = frappe.db.get_value("User", {"reset_password_key": key})
	if not user_name:
		frappe.throw(_("This link is invalid or has already been used."))

	generated_on = frappe.db.get_value("User", user_name, "last_reset_password_key_generated_on")
	if generated_on:
		age = (now_datetime() - get_datetime(generated_on)).total_seconds()
		if age > _key_expiry_seconds():
			frappe.throw(_("This link has expired. Please request a new one."))

	validate_password_policy(new_password, user_name)

	_set_user_password(user_name, new_password)
	frappe.db.set_value(
		"User",
		user_name,
		{
			"reset_password_key": "",
			"enabled": 1,
			"custom_account_status": "active",
		},
		update_modified=False,
	)
	frappe.db.commit()
	return user_name


def validate_password_policy(password: str, user_name: str) -> None:
	password = password or ""
	email = user_name.lower()
	local_part = email.split("@")[0]
	full_name = (frappe.db.get_value("User", user_name, "full_name") or "").lower()

	missing = []
	if len(password) < 8:
		missing.append(_("at least 8 characters"))
	if not any(c.isalpha() for c in password):
		missing.append(_("a letter"))
	if not any(c.isdigit() for c in password):
		missing.append(_("a number"))
	if missing:
		frappe.throw(_("Password needs {0}.").format(", ".join(missing)))

	if password.lower() in {email, local_part} or (full_name and password.lower() == full_name):
		frappe.throw(_("Choose a password that isn't your name or email address."))
