"""Idempotent site setup for the auth model.

Runs on ``after_install`` (fresh installs) and ``after_migrate`` (every
``bench migrate``) so an already-installed site picks the changes up too.
"""

import frappe

from accreage_mart.utils.profile import PLATFORM_ROLES

SEED_ADMIN_EMAIL = "admin@accreagemart.lk"

DEMO_PASSWORD = "demo1234"  # noqa: S105 — dev-only demo accounts
DEMO_USERS = (
	{"email": "buyer@demo.accreagemart.lk", "name": "Demo Buyer", "role": "Buyer"},
	{"email": "seller@demo.accreagemart.lk", "name": "Demo Seller", "role": "Seller"},
	{"email": "staff@demo.accreagemart.lk", "name": "Demo Staff", "role": "Staff"},
	{"email": "admin@demo.accreagemart.lk", "name": "Demo Admin", "role": "Admin"},
)


def after_install():
	_setup()


def after_migrate():
	_setup()


def before_tests():
	"""Make sure the auth model is in place before the suite runs, regardless of
	whether ``bench migrate`` was run first."""
	_setup()


def _setup():
	ensure_roles()
	ensure_custom_fields()
	ensure_seed_admin()
	ensure_demo_users()
	frappe.db.commit()


def ensure_roles():
	for role in PLATFORM_ROLES:
		if not frappe.db.exists("Role", role):
			frappe.get_doc(
				{
					"doctype": "Role",
					"role_name": role,
					"desk_access": 1,
				}
			).insert(ignore_permissions=True)


def ensure_custom_fields():
	from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

	create_custom_fields(
		{
			"User": [
				{
					"fieldname": "custom_account_status",
					"label": "Account Status",
					"fieldtype": "Select",
					# No stored default — a blank value is inferred from `enabled`
					# (see accreage_mart.utils.profile.get_account_status). A column
					# default of "active" gets backfilled onto invited users by
					# ``bench migrate``, which would silently activate them.
					"options": "\ninvited\nactive\nsuspended\ndeactivated",
					"insert_after": "enabled",
					"in_standard_filter": 1,
					"description": "Accreage Mart account lifecycle state.",
				}
			]
		},
		ignore_validate=True,
	)


def ensure_seed_admin():
	"""Seed one named Admin so a fresh site has someone who can provision staff.

	Created disabled — it gets in through the password-reset / set-password flow
	(Story 1.8). Frappe's built-in ``Administrator`` also works as an admin.
	"""
	if frappe.db.exists("User", SEED_ADMIN_EMAIL):
		return

	user = frappe.new_doc("User")
	user.email = SEED_ADMIN_EMAIL
	user.first_name = "Accreage"
	user.last_name = "Admin"
	user.user_type = "System User"
	user.enabled = 0
	user.send_welcome_email = 0
	user.flags.no_welcome_mail = True
	user.insert(ignore_permissions=True)
	user.add_roles("Admin", "System Manager")
	frappe.db.set_value("User", SEED_ADMIN_EMAIL, "custom_account_status", "invited")


def ensure_demo_users():
	"""One active account per role for the demo quick sign-in cards.

	Dev sites only (``developer_mode``). Password is ``demo1234``. The buyer and
	seller also get a verified profile so the seller isn't held at the pending gate.
	"""
	if not frappe.conf.get("developer_mode"):
		return

	from frappe.utils.password import update_password

	for spec in DEMO_USERS:
		if frappe.db.exists("User", spec["email"]):
			continue
		user = frappe.new_doc("User")
		user.email = spec["email"]
		user.first_name = spec["name"]
		user.user_type = "System User"
		user.enabled = 1
		user.send_welcome_email = 0
		user.flags.no_welcome_mail = True
		user.insert(ignore_permissions=True)
		user.add_roles(spec["role"], "System Manager")
		user.db_set("custom_account_status", "active")
		update_password(spec["email"], DEMO_PASSWORD)

	_ensure_demo_profile(
		"buyer@demo.accreagemart.lk",
		"Buyer Profile",
		{
			"business_name": "Demo Hotels (Pvt) Ltd",
			"buyer_type": "Hotel",
			"location": "Colombo 03",
			"district": "Colombo",
			"verified": 1,
		},
	)
	_ensure_demo_profile(
		"seller@demo.accreagemart.lk",
		"Seller Profile",
		{
			"business_name": "Demo Fresh Farms",
			"location": "Nuwara Eliya",
			"district": "Nuwara Eliya",
			"description": "Demo seller account.",
			"trust_score": 4.5,
			"verified": 1,
		},
	)


def _ensure_demo_profile(user: str, doctype: str, fields: dict):
	if not frappe.db.exists("User", user) or frappe.db.exists(doctype, {"user": user}):
		return
	doc = frappe.new_doc(doctype)
	doc.user = user
	doc.update(fields)
	doc.insert(ignore_permissions=True)
