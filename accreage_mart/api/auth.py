"""Whitelisted authentication / account endpoints.

Referenced from the frontend as ``accreage_mart.api.auth.<fn>`` (see
src/lib/methods.ts).

Guard matrix
------------
========================  ================  ===================================
Endpoint                  Caller            Notes
========================  ================  ===================================
get_user_info             authenticated     current user + primary role + profile
register_buyer            guest             rate-limited (Story 1.9)
register_seller           guest             rate-limited (Story 1.10)
set_password              guest             key-based, single use (Story 1.8)
request_password_reset    guest             generic response, rate-limited (1.8)
resend_activation         guest             only for status "invited" (Story 1.8)
create_staff              Admin / Administrator   emails an invite (Story 1.11)
verify_account            Staff / Admin     flips profile.verified (Story 1.13)
========================  ================  ===================================

Native Frappe ``login`` / ``logout`` handle the session itself; the frontend calls
them directly.
"""

import frappe
from frappe import _

from accreage_mart.utils.profile import get_account_status, get_primary_role, get_profile


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
