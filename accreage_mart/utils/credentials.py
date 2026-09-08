"""Set-password / reset-password key handling and the emails that carry the link.

The set-password page lives on the frontend (``<frontend>/set-password?key=...``).
Delivery falls back to returning the link when the site has no outgoing mail
configured, so the flow is usable in development without SMTP.
"""

import frappe
from frappe import _
from frappe.utils import get_datetime, now_datetime
from frappe.utils.password import update_password as _set_user_password

from accreage_mart.utils.email import send_branded_email, smtp_configured

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


def send_onboarding_link(user_name: str, *, staff: bool = False) -> str | None:
	"""Set-password link for a freshly created (invited) account. Returns the link
	when SMTP isn't configured, else sends the email and returns None."""
	link = _link_for(user_name)
	if not smtp_configured():
		return link

	full_name = frappe.db.get_value("User", user_name, "full_name") or "there"
	send_branded_email(
		recipient=user_name,
		subject="Set your Accreage Mart password",
		heading="You've been added to Accreage Mart" if staff else "Welcome to Accreage Mart",
		body_lines=[
			f"Hi {full_name},",
			(
				"An administrator created a staff account for you. "
				"Set a password to sign in."
				if staff
				else "Your account is ready. Set a password to finish signing up."
			),
		],
		cta_label="Set my password",
		cta_url=link,
		footer_note=_expiry_note(),
	)
	return None


def send_password_reset(user_name: str) -> str | None:
	link = _link_for(user_name)
	if not smtp_configured():
		return link

	full_name = frappe.db.get_value("User", user_name, "full_name") or "there"
	send_branded_email(
		recipient=user_name,
		subject="Reset your Accreage Mart password",
		heading="Reset your password",
		body_lines=[
			f"Hi {full_name},",
			"We got a request to reset your password. Choose a new one with the button below.",
			"If you didn't ask for this, you can ignore this email.",
		],
		cta_label="Choose a new password",
		cta_url=link,
		footer_note=_expiry_note(),
	)
	return None


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
