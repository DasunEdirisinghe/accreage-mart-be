"""Whitelisted authentication / account endpoints.

Referenced from the frontend as ``accreage_mart.api.auth.<fn>`` (see
src/lib/methods.ts).

Guard matrix
------------
========================  ================  ===================================
Endpoint                  Caller            Notes
========================  ================  ===================================
get_user_info             authenticated     current user + primary role + profile
register_buyer            guest             creates invited User + Buyer Profile (1.9)
register_seller           guest             creates invited User + Seller Profile (1.10)
set_password              guest             key-based, single use (Story 1.8)
check_reset_key           guest             is a set-password link still valid (1.8)
account_hint              guest             is an address a pending account (1.8)
request_password_reset    guest             generic response, rate-limited (1.8)
resend_activation         guest             only for status "invited" (Story 1.8)
create_staff              Admin / Administrator   emails an invite (Story 1.11)
pending_accounts          Staff / Admin     unverified buyers/sellers (Story 1.13)
verify_account            Staff / Admin     flips profile.verified (Story 1.13)
========================  ================  ===================================

Native Frappe ``login`` / ``logout`` handle the session itself; the frontend calls
them directly.
"""

import frappe
from frappe import _
from frappe.rate_limiter import rate_limit

from accreage_mart.utils.credentials import (
	consume_key,
	is_key_valid,
	send_onboarding_link,
	send_password_reset,
)
from accreage_mart.utils.profile import get_account_status, get_primary_role, get_profile
from accreage_mart.utils import registration

# Always the same, regardless of whether the address is known (no account enumeration).
_GENERIC_OK = {"ok": True}


@frappe.whitelist()
def get_user_info() -> dict:
	"""Current user, primary role, account status, and buyer/seller profile.

	Shape matches what src/lib/current-user-info.ts expects. Never errors for
	Administrator, a role-less user, or a user without a profile.
	"""
	user = frappe.session.user
	if not user or user == "Guest":
		frappe.throw(_("Not authenticated."), frappe.AuthenticationError)

	user_doc = frappe.get_doc("User", user)
	role = get_primary_role(user)
	profile = get_profile(user, role)

	return {
		"user": {
			"id": user_doc.name,
			"email": user_doc.email,
			"fullName": user_doc.full_name or user_doc.first_name or user_doc.name,
			"phone": user_doc.phone or user_doc.mobile_no or "",
		},
		"role": role,
		"status": get_account_status(user_doc),
		"verified": bool(profile.get("verified")) if profile else False,
		"profile": profile,
	}


def _lookup(email: str) -> str | None:
	return frappe.db.get_value("User", {"email": (email or "").strip().lower()})


@frappe.whitelist(allow_guest=True, methods=["POST"])
@rate_limit(key="email", limit=5, seconds=60 * 60)
def request_password_reset(email: str) -> dict:
	"""Email a link to set a new password. Generic response either way. An account
	still in "invited" gets the onboarding link instead of a reset link."""
	response = dict(_GENERIC_OK)
	user_name = _lookup(email)
	if user_name:
		status = frappe.db.get_value("User", user_name, "custom_account_status")
		link = (
			send_onboarding_link(user_name)
			if status == "invited"
			else send_password_reset(user_name)
		)
		if link:  # dev: no SMTP configured
			response["dev_link"] = link
	return response


@frappe.whitelist(allow_guest=True, methods=["POST"])
@rate_limit(key="email", limit=5, seconds=60 * 60)
def resend_activation(email: str) -> dict:
	"""Re-send the set-password link for an account still in "invited"."""
	response = dict(_GENERIC_OK)
	user_name = _lookup(email)
	if user_name and frappe.db.get_value("User", user_name, "custom_account_status") == "invited":
		link = send_onboarding_link(user_name)
		if link:
			response["dev_link"] = link
	return response


@frappe.whitelist(allow_guest=True, methods=["POST"])
@rate_limit(key="key", limit=10, seconds=60 * 60)
def set_password(key: str, new_password: str) -> dict:
	"""Set the password behind an emailed key and activate the account. Single use."""
	user_name = consume_key(key, new_password)
	return {"ok": True, "email": user_name}


@frappe.whitelist(allow_guest=True)
@rate_limit(key="key", limit=30, seconds=60 * 60)
def check_reset_key(key: str) -> dict:
	"""Whether a set-password link is still usable — lets the page show the right
	state before the person fills anything in."""
	return {"valid": is_key_valid(key)}


@frappe.whitelist(allow_guest=True, methods=["POST"])
@rate_limit(key="email", limit=5, seconds=60 * 60)
def register_buyer(
	full_name: str, business_name: str, email: str, mobile: str, district: str, buyer_type: str
) -> dict:
	return registration.register_buyer(
		full_name=full_name,
		business_name=business_name,
		email=email,
		mobile=mobile,
		district=district,
		buyer_type=buyer_type,
	)


@frappe.whitelist(allow_guest=True, methods=["POST"])
@rate_limit(key="email", limit=5, seconds=60 * 60)
def register_seller(
	full_name: str, business_name: str, email: str, mobile: str, district: str, description: str = ""
) -> dict:
	return registration.register_seller(
		full_name=full_name,
		business_name=business_name,
		email=email,
		mobile=mobile,
		district=district,
		description=description,
	)


@frappe.whitelist(methods=["POST"])
def create_staff(full_name: str, email: str, role: str) -> dict:
	"""Admin provisions a Staff/Admin account and emails an invite. Admin only."""
	return registration.create_staff(full_name=full_name, email=email, role=role)


@frappe.whitelist()
def pending_accounts() -> list[dict]:
	"""Buyers and sellers whose profile hasn't been staff-verified. Staff/Admin only."""
	registration.require_staff()

	rows = []
	for doctype, role in (("Buyer Profile", "buyer"), ("Seller Profile", "seller")):
		for name in frappe.get_all(doctype, filters={"verified": 0}, pluck="name"):
			profile = frappe.get_doc(doctype, name)
			user = frappe.db.get_value(
				"User", profile.user, ["full_name", "custom_account_status", "creation"], as_dict=True
			)
			if not user or user.custom_account_status == "deactivated":
				continue
			rows.append(
				{
					"email": profile.user,
					"fullName": user.full_name,
					"role": role,
					"businessName": profile.business_name,
					"district": profile.district,
					"status": user.custom_account_status,
					"since": str(user.creation),
				}
			)
	return rows


@frappe.whitelist(methods=["POST"])
def verify_account(email: str) -> dict:
	"""Mark a buyer/seller profile as verified and notify them. Staff/Admin only."""
	registration.require_staff()

	for doctype in ("Buyer Profile", "Seller Profile"):
		name = frappe.db.get_value(doctype, {"user": email})
		if not name:
			continue
		frappe.db.set_value(doctype, name, "verified", 1)
		frappe.get_doc(
			{
				"doctype": "Notification Log",
				"subject": "Your Accreage Mart account is verified",
				"for_user": email,
				"type": "Alert",
				"email_content": "Your business has been verified — you now have full access.",
			}
		).insert(ignore_permissions=True)
		frappe.db.commit()
		return {"ok": True}

	frappe.throw(_("No buyer or seller profile found for this account."))


@frappe.whitelist(allow_guest=True, methods=["POST"])
@rate_limit(key="email", limit=10, seconds=60 * 60)
def account_hint(email: str) -> dict:
	"""Minimal login-screen hint: only whether the address belongs to an account
	still awaiting activation, so we can offer to resend the link on a failed login."""
	user_name = _lookup(email)
	if user_name and frappe.db.get_value("User", user_name, "custom_account_status") == "invited":
		return {"invited": True}
	return {}
