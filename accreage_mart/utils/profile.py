"""Identity helpers: primary role derivation, profile lookup, platform user creation.

The frontend models one primary role per user (Buyer | Seller | Staff | Admin) plus a
public/guest state. A user may hold several Frappe roles (everyone also carries
``System Manager`` — see the epic notes), so ``get_primary_role`` collapses them to the
single role the frontend routes on.
"""

import frappe
from frappe import _

# Highest wins. Mirrors the frontend Role union.
ROLE_PRECEDENCE = ("Admin", "Staff", "Seller", "Buyer")
PLATFORM_ROLES = ROLE_PRECEDENCE
FRONTEND_ROLE = {"Admin": "admin", "Staff": "staff", "Seller": "seller", "Buyer": "buyer"}

ACCOUNT_STATUSES = ("invited", "active", "suspended", "deactivated")


def get_primary_role(user: str) -> str | None:
	"""Return "admin" | "staff" | "seller" | "buyer", or None for a role-less user."""
	if user == "Administrator":
		return "admin"

	roles = set(frappe.get_roles(user))
	for role in ROLE_PRECEDENCE:
		if role in roles:
			return FRONTEND_ROLE[role]
	return None


def get_account_status(user_doc) -> str:
	status = user_doc.get("custom_account_status")
	if status in ACCOUNT_STATUSES:
		return status
	# No explicit status stored — infer from `enabled`.
	return "active" if user_doc.enabled else "invited"


def get_profile(user: str, role: str | None = None) -> dict | None:
	"""The buyer/seller profile as the shape src/lib/types.ts expects, or None."""
	role = role or get_primary_role(user)

	if role == "buyer":
		name = frappe.db.get_value("Buyer Profile", {"user": user})
		if not name:
			return None
		doc = frappe.get_doc("Buyer Profile", name)
		return {
			"id": doc.name,
			"userId": user,
			"businessName": doc.business_name,
			"buyerType": doc.buyer_type,
			"location": doc.location,
			"district": doc.district,
			"verified": bool(doc.verified),
		}

	if role == "seller":
		name = frappe.db.get_value("Seller Profile", {"user": user})
		if not name:
			return None
		doc = frappe.get_doc("Seller Profile", name)
		return {
			"id": doc.name,
			"userId": user,
			"businessName": doc.business_name,
			"location": doc.location,
			"district": doc.district,
			"description": doc.description,
			"trustScore": doc.trust_score,
			"verified": bool(doc.verified),
			"totalSales": doc.total_sales,
		}

	return None


def create_platform_user(
	*,
	email: str,
	full_name: str,
	role: str,
	mobile: str | None = None,
	status: str = "invited",
):
	"""Create a System User carrying ``role`` + ``System Manager``.

	Used by the registration and staff-provisioning endpoints. The account starts
	disabled and in "invited" until the person sets a password via the emailed link
	(Story 1.8). No welcome email is sent here.
	"""
	if role not in PLATFORM_ROLES:
		frappe.throw(_("Unknown role: {0}").format(role))

	if frappe.db.exists("User", email):
		frappe.throw(_("An account with this email already exists."), frappe.DuplicateEntryError)

	user = frappe.new_doc("User")
	user.email = email
	user.first_name = full_name
	user.mobile_no = mobile
	user.phone = mobile
	user.user_type = "System User"
	user.send_welcome_email = 0
	user.enabled = 1 if status == "active" else 0
	user.flags.no_welcome_mail = True
	user.insert(ignore_permissions=True)

	user.add_roles(role, "System Manager")
	user.db_set("custom_account_status", status)
	return user
