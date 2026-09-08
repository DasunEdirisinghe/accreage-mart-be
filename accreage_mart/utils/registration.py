"""Account creation for the three onboarding paths.

Buyer and seller sign themselves up; staff/admin are provisioned by an admin. All
three create a disabled "invited" User and email a set-password link (Story 1.8).
"""

import frappe
from frappe import _

from accreage_mart.utils.credentials import send_onboarding_link
from accreage_mart.utils.profile import create_platform_user

BUYER_TYPES = ("Hotel", "Supermarket", "Exporter", "Processor", "Other")


def _ok(link: str | None) -> dict:
	return {"ok": True, "dev_link": link} if link else {"ok": True}


def _districts() -> set[str]:
	options = frappe.get_meta("Buyer Profile").get_field("district").options or ""
	return {line.strip() for line in options.split("\n") if line.strip()}


def _handle_existing(email: str) -> dict | None:
	"""Resend the link for a still-invited account; reject an active one."""
	existing = frappe.db.get_value(
		"User", {"email": email}, ["name", "custom_account_status"], as_dict=True
	)
	if not existing:
		return None
	if existing.custom_account_status == "invited":
		return _ok(send_onboarding_link(existing.name))
	frappe.throw(
		_("An account with this email already exists — try signing in instead."),
		frappe.DuplicateEntryError,
	)


def _require(field: str, value: str):
	if not (value or "").strip():
		frappe.throw(_("{0} is required.").format(field))


def register_buyer(
	*, full_name: str, business_name: str, email: str, mobile: str, district: str, buyer_type: str
) -> dict:
	email = (email or "").strip().lower()
	if (existing := _handle_existing(email)) is not None:
		return existing

	_require("Full name", full_name)
	_require("Business name", business_name)
	if buyer_type not in BUYER_TYPES:
		frappe.throw(_("Choose a valid buyer type."))
	if district not in _districts():
		frappe.throw(_("Choose a valid district."))

	user = create_platform_user(email=email, full_name=full_name, role="Buyer", mobile=mobile)
	profile = frappe.new_doc("Buyer Profile")
	profile.update(
		{
			"user": user.name,
			"business_name": business_name,
			"buyer_type": buyer_type,
			"district": district,
			"verified": 0,
		}
	)
	profile.insert(ignore_permissions=True)

	link = send_onboarding_link(user.name)
	frappe.db.commit()
	return _ok(link)


def register_seller(
	*,
	full_name: str,
	business_name: str,
	email: str,
	mobile: str,
	district: str,
	description: str = "",
) -> dict:
	email = (email or "").strip().lower()
	if (existing := _handle_existing(email)) is not None:
		return existing

	_require("Full name", full_name)
	_require("Business name", business_name)
	if district not in _districts():
		frappe.throw(_("Choose a valid district."))

	user = create_platform_user(email=email, full_name=full_name, role="Seller", mobile=mobile)
	profile = frappe.new_doc("Seller Profile")
	profile.update(
		{
			"user": user.name,
			"business_name": business_name,
			"district": district,
			"description": description or "",
			"trust_score": 0,
			"verified": 0,
			"total_sales": 0,
		}
	)
	profile.insert(ignore_permissions=True)

	link = send_onboarding_link(user.name)
	frappe.db.commit()
	return _ok(link)


def require_admin():
	if frappe.session.user == "Administrator" or "Admin" in set(frappe.get_roles(frappe.session.user)):
		return
	frappe.throw(_("Only an administrator can do this."), frappe.PermissionError)


def create_staff(*, full_name: str, email: str, role: str) -> dict:
	require_admin()
	email = (email or "").strip().lower()
	_require("Full name", full_name)
	if role not in ("Staff", "Admin"):
		frappe.throw(_("Role must be Staff or Admin."))
	if frappe.db.exists("User", email):
		frappe.throw(_("An account with this email already exists."), frappe.DuplicateEntryError)

	user = create_platform_user(email=email, full_name=full_name, role=role)
	link = send_onboarding_link(user.name, staff=True)
	frappe.db.commit()
	return {"ok": True, "email": user.name, **({"dev_link": link} if link else {})}
