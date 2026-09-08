"""Idempotent site setup for the auth model.

Runs on ``after_install`` (fresh installs) and ``after_migrate`` (every
``bench migrate``) so an already-installed site picks the changes up too.
"""

import frappe

from accreage_mart.utils.profile import PLATFORM_ROLES

SEED_ADMIN_EMAIL = "admin@accreagemart.lk"


def after_install():
	_setup()


def after_migrate():
	_setup()


def _setup():
	ensure_roles()
	ensure_custom_fields()
	ensure_seed_admin()
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
					"options": "invited\nactive\nsuspended\ndeactivated",
					"default": "active",
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

	Created in "invited" — it onboards through the same emailed set-password link as
	everyone else. Frappe's built-in ``Administrator`` also works as an admin.
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
	user.db_set("custom_account_status", "invited")
