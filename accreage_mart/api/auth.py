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
list_accounts             Staff / Admin     real staff/member rows (Story 1.14; Staff read
                                             access added Story 2.3 for the dashboard stats)
set_account_status        Admin / Administrator   active/suspended/deactivated (Story 1.14)
account_applications      Staff / Admin     buyer/seller applications, any status (Story 2.2;
                                             was pending_accounts, Story 1.13 — now returns
                                             Pending/Approved/Rejected in one call)
verify_account            Staff / Admin     approves + mints a set-password link + emails
                                             account_approved (Story 2.2)
reject_account            Staff / Admin     rejects with a reason + emails account_rejected
                                             (Story 2.2)
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
	issue_set_password_link,
	send_onboarding_link,
	send_password_reset,
	should_expose_link,
)
from accreage_mart.utils.email import send_templated_email, smtp_configured
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
def account_applications() -> list[dict]:
	"""Every buyer/seller application with its verification status, for the
	Pending / Approved / Rejected tabs on /admin/accounts. Staff/Admin only."""
	registration.require_staff()

	rows = []
	for doctype, role in (("Buyer Profile", "buyer"), ("Seller Profile", "seller")):
		for name in frappe.get_all(doctype, pluck="name"):
			profile = frappe.get_doc(doctype, name)
			user = frappe.db.get_value(
				"User",
				profile.user,
				["full_name", "mobile_no", "custom_account_status", "creation"],
				as_dict=True,
			)
			if not user or user.custom_account_status == "deactivated":
				continue
			verification_status = (profile.get("verification_status") or "Pending").lower()
			rows.append(
				{
					"email": profile.user,
					"fullName": user.full_name,
					"role": role,
					"businessName": profile.business_name,
					"district": profile.district,
					"mobile": user.mobile_no or "",
					"description": profile.get("description") or "",
					"verificationStatus": verification_status,
					"rejectionReason": profile.get("rejection_reason") or "",
					"since": str(user.creation),
					"reviewedOn": str(profile.modified) if verification_status != "pending" else None,
				}
			)

	rows.sort(key=lambda r: r["since"], reverse=True)
	return rows


def _find_application(email: str) -> tuple[str, str]:
	"""(doctype, docname) of the buyer/seller application for this email, or throws."""
	for doctype in ("Buyer Profile", "Seller Profile"):
		name = frappe.db.get_value(doctype, {"user": email})
		if name:
			return doctype, name
	frappe.throw(_("No application found for this account."))


@frappe.whitelist()
def list_accounts(kind: str = "staff") -> list[dict]:
	"""Real account rows for the admin tables and the shared /admin dashboard
	stat cards. Staff/Admin only (the /admin/staff and /admin/users *pages* stay
	Admin-only via the route guard — this is just the read).

	kind="staff"   -> users holding Staff or Admin
	kind="members" -> users holding Buyer or Seller, with their profile
	"""
	registration.require_staff()

	wanted = {"Staff", "Admin"} if kind == "staff" else {"Buyer", "Seller"}
	rows = []
	seen = set()

	for has_role in frappe.get_all(
		"Has Role", filters={"role": ["in", list(wanted)], "parenttype": "User"}, fields=["parent"]
	):
		email = has_role.parent
		if email in seen or email in ("Guest", "Administrator"):
			continue
		seen.add(email)

		user = frappe.db.get_value(
			"User",
			email,
			["full_name", "custom_account_status", "enabled", "creation"],
			as_dict=True,
		)
		if not user:
			continue

		role = get_primary_role(email)
		profile = get_profile(email, role)
		rows.append(
			{
				"email": email,
				"fullName": user.full_name or email,
				"role": role,
				"status": get_account_status(user),
				"businessName": (profile or {}).get("businessName"),
				"since": str(user.creation),
			}
		)

	rows.sort(key=lambda r: r["since"], reverse=True)
	return rows


@frappe.whitelist(methods=["POST"])
def set_account_status(email: str, status: str) -> dict:
	"""Activate / suspend / deactivate an account. Admin/Administrator only."""
	registration.require_admin()

	if status not in ("active", "suspended", "deactivated"):
		frappe.throw(_("Invalid status."))
	if email in ("Administrator", frappe.session.user):
		frappe.throw(_("You can't change this account's status."), frappe.PermissionError)
	if not frappe.db.exists("User", email):
		frappe.throw(_("No such account."))

	frappe.db.set_value(
		"User",
		email,
		{"custom_account_status": status, "enabled": 1 if status == "active" else 0},
	)
	frappe.db.commit()
	return {"ok": True}


@frappe.whitelist(methods=["POST"])
def verify_account(email: str) -> dict:
	"""Approve a buyer/seller application: flip verified/verification_status, mint a
	fresh set-password link, and email account_approved. Staff/Admin only."""
	registration.require_staff()

	doctype, name = _find_application(email)
	frappe.db.set_value(doctype, name, {"verified": 1, "verification_status": "Approved"})
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

	full_name = frappe.db.get_value("User", email, "full_name") or "there"
	business_name = frappe.db.get_value(doctype, name, "business_name")
	link, expiry_note = issue_set_password_link(email)

	if smtp_configured():
		send_templated_email(
			key="account_approved",
			recipient=email,
			context={"full_name": full_name, "business_name": business_name},
			cta_label="Set my password",
			cta_url=link,
			footer_note=expiry_note,
		)

	return {"ok": True, "dev_link": link if should_expose_link() else None}


@frappe.whitelist(methods=["POST"])
def reject_account(email: str, reason: str) -> dict:
	"""Reject a buyer/seller application with a reason and email account_rejected.
	Staff/Admin only."""
	registration.require_staff()

	reason = (reason or "").strip()
	if not reason:
		frappe.throw(_("A reason is required."))

	doctype, name = _find_application(email)
	frappe.db.set_value(
		doctype,
		name,
		{"verified": 0, "verification_status": "Rejected", "rejection_reason": reason},
	)
	frappe.db.commit()

	full_name = frappe.db.get_value("User", email, "full_name") or "there"
	business_name = frappe.db.get_value(doctype, name, "business_name")

	if smtp_configured():
		send_templated_email(
			key="account_rejected",
			recipient=email,
			context={"full_name": full_name, "business_name": business_name, "reason": reason},
		)

	return {"ok": True}


@frappe.whitelist(allow_guest=True, methods=["POST"])
@rate_limit(key="email", limit=10, seconds=60 * 60)
def account_hint(email: str) -> dict:
	"""Minimal login-screen hint: only whether the address belongs to an account
	still awaiting activation, so we can offer to resend the link on a failed login."""
	user_name = _lookup(email)
	if user_name and frappe.db.get_value("User", user_name, "custom_account_status") == "invited":
		return {"invited": True}
	return {}
