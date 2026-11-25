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
	ensure_email_templates()
	ensure_seed_admin()
	ensure_demo_users()
	ensure_price_suggestion_settings()
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
			],
			"Notification Log": [
				{
					"fieldname": "custom_event_type",
					"label": "Event Type",
					"fieldtype": "Data",
					"insert_after": "type",
					"in_standard_filter": 1,
					"description": (
						"Accreage Mart event taxonomy, e.g. listing_approved, auction_outbid."
					),
				},
				{
					"fieldname": "custom_delivery_log",
					"label": "Delivery Log",
					"fieldtype": "Table",
					"options": "Notification Delivery Log",
					"insert_after": "custom_event_type",
					"description": "One row per channel (Email/SMS) this notification was dispatched to.",
				},
			],
		},
		ignore_validate=True,
	)


EMAIL_TEMPLATES = {
	"password_reset": {
		"subject": "Reset your Accreage Mart password",
		"response": (
			"<p>Hi {{ full_name }},</p>"
			"<p>We got a request to reset your password. Choose a new one with the button below.</p>"
			"<p>If you didn't ask for this, you can ignore this email.</p>"
		),
	},
	"welcome_member": {
		"subject": "Welcome to Accreage Mart",
		"response": (
			"<p>Hi {{ full_name }},</p>"
			"<p>Your account is ready. Set a password to finish signing up.</p>"
		),
	},
	"staff_invite": {
		"subject": "You've been added to Accreage Mart",
		"response": (
			"<p>Hi {{ full_name }},</p>"
			"<p>An administrator created a staff account for you. Set a password to sign in.</p>"
		),
	},
	"account_approved": {
		"subject": "Your Accreage Mart account is verified",
		"response": (
			"<p>Hi {{ full_name }},</p>"
			"<p>Great news — {{ business_name }} has been verified. Set your password below to sign "
			"in and get started.</p>"
		),
	},
	"account_approved_login": {
		"subject": "Your Accreage Mart account is verified",
		"response": (
			"<p>Hi {{ full_name }},</p>"
			"<p>Great news — {{ business_name }} has been verified. You already set your password, "
			"so you can log in right away.</p>"
		),
	},
	"listing_low_stock": {
		"subject": "Low stock on one of your listings",
		"response": (
			"<p>Hi {{ full_name }},</p>"
			"<p>Your listing <strong>{{ title }}</strong> is running low: {{ quantity }} {{ unit }} left, "
			"below your alert level of {{ low_stock_level }} {{ unit }}.</p>"
			"<p>Update the stock when you have more, or hide the listing if you can't fill new orders.</p>"
		),
	},
	"listing_approved": {
		"subject": "Your listing is now live",
		"response": (
			"<p>Hi {{ full_name }},</p>"
			"<p>Your listing <strong>{{ title }}</strong> was approved and is now live on the "
			"marketplace.</p>"
		),
	},
	"listing_rejected": {
		"subject": "Your listing needs changes",
		"response": (
			"<p>Hi {{ full_name }},</p>"
			"<p>Your listing <strong>{{ title }}</strong> was not approved.</p>"
			"<p><strong>Reason:</strong> {{ reason }}</p>"
			"<p>You can edit the listing and resubmit it for review.</p>"
		),
	},
	"listing_suspended": {
		"subject": "Your listing has been suspended",
		"response": (
			"<p>Hi {{ full_name }},</p>"
			"<p>Your listing <strong>{{ title }}</strong> has been suspended and is no longer "
			"visible on the marketplace.</p>"
			"<p><strong>Reason:</strong> {{ reason }}</p>"
			"<p>Please contact staff if you have questions.</p>"
		),
	},
	"account_rejected": {
		"subject": "Update on your Accreage Mart application",
		"response": (
			"<p>Hi {{ full_name }},</p>"
			"<p>We're unable to verify {{ business_name }} at this time.</p>"
			"<p><strong>Reason:</strong> {{ reason }}</p>"
			"<p>You're welcome to update your details and apply again, or reply to this email with "
			"any questions.</p>"
		),
	},
	"auction_outbid": {
		"subject": "You've been outbid",
		"response": (
			"<p>Hi {{ full_name }},</p>"
			"<p>You were outbid on <strong>{{ title }}</strong>. The new highest bid is "
			"Rs. {{ amount }} per {{ unit }}.</p>"
			"<p>Place a higher bid before the auction ends if you'd like to stay in the running.</p>"
		),
	},
	"auction_rejected": {
		"subject": "Your auction was not approved",
		"response": (
			"<p>Hi {{ full_name }},</p>"
			"<p>Your auction for <strong>{{ title }}</strong> was not approved.</p>"
			"<p><strong>Reason:</strong> {{ reason }}</p>"
		),
	},
	"auction_cancelled_seller": {
		"subject": "Your auction has been cancelled",
		"response": (
			"<p>Hi {{ full_name }},</p>"
			"<p>Your auction for <strong>{{ title }}</strong> has been cancelled.</p>"
			"<p><strong>Reason:</strong> {{ reason }}</p>"
		),
	},
	"auction_cancelled_bidder": {
		"subject": "An auction you bid on was cancelled",
		"response": (
			"<p>Hi {{ full_name }},</p>"
			"<p>The auction for <strong>{{ title }}</strong> that you bid on has been cancelled.</p>"
			"<p><strong>Reason:</strong> {{ reason }}</p>"
		),
	},
	"auction_won": {
		"subject": "You won the auction!",
		"response": (
			"<p>Hi {{ full_name }},</p>"
			"<p>You won the auction for <strong>{{ title }}</strong> with a bid of "
			"Rs. {{ amount }} per {{ unit }}.</p>"
		),
	},
	"auction_sold": {
		"subject": "Your auction has sold",
		"response": (
			"<p>Hi {{ full_name }},</p>"
			"<p>Your auction for <strong>{{ title }}</strong> sold for "
			"Rs. {{ amount }} per {{ unit }}.</p>"
		),
	},
	"auction_ended_no_bids": {
		"subject": "Your auction ended with no bids",
		"response": (
			"<p>Hi {{ full_name }},</p>"
			"<p>Your auction for <strong>{{ title }}</strong> ended with no bids.</p>"
		),
	},
	"order_placed": {
		"subject": "New order {{ order }} for {{ title }}",
		"response": (
			"<p>Hi {{ full_name }},</p>"
			"<p><strong>{{ buyer_name }}</strong> placed order {{ order }} for "
			"{{ quantity }} {{ unit }} of <strong>{{ title }}</strong>.</p>"
			"{% if offer_price %}"
			"<p>They offered <strong>{{ offer_price }}</strong> per {{ unit }} "
			"(your listed price is {{ listed_price }}). Accept, counter or decline the offer "
			"within {{ hours }} hours, or the order is cancelled.</p>"
			"{% else %}"
			"<p>Price: {{ listed_price }} per {{ unit }}, {{ total }} in total. Confirm or decline "
			"the order within {{ hours }} hours, or it is cancelled.</p>"
			"{% endif %}"
		),
	},
	"order_offer_countered": {
		"subject": "New counter-offer on order {{ order }}",
		"response": (
			"<p>Hi {{ full_name }},</p>"
			"<p><strong>{{ actor_name }}</strong> countered on order {{ order }} "
			"({{ quantity }} {{ unit }} of <strong>{{ title }}</strong>) with "
			"<strong>{{ price }}</strong> per {{ unit }} ({{ total }} in total).</p>"
			"<p>Accept, counter or decline within {{ hours }} hours, or the order is cancelled.</p>"
			"{% if final %}<p>That was the last counter-offer allowed: you can now only accept or "
			"decline.</p>{% endif %}"
		),
	},
	"order_offer_accepted": {
		"subject": "Price agreed on order {{ order }}",
		"response": (
			"<p>Hi {{ full_name }},</p>"
			"<p><strong>{{ actor_name }}</strong> accepted <strong>{{ price }}</strong> per {{ unit }} "
			"for order {{ order }} ({{ quantity }} {{ unit }} of <strong>{{ title }}</strong>), "
			"{{ total }} in total.</p>"
			"{% if for_buyer %}<p>Pay the seller within {{ hours }} hours and upload your payment slip "
			"on the order page, or the order is cancelled.</p>"
			"{% else %}<p>The buyer has {{ hours }} hours to pay and upload the payment slip.</p>"
			"{% endif %}"
		),
	},
	"order_offer_declined": {
		"subject": "Offer declined - order {{ order }} cancelled",
		"response": (
			"<p>Hi {{ full_name }},</p>"
			"<p><strong>{{ actor_name }}</strong> declined the offer of {{ price }} per {{ unit }} on "
			"order {{ order }} ({{ quantity }} {{ unit }} of <strong>{{ title }}</strong>), so the order "
			"was cancelled.</p>"
			"{% if reason %}<p>Reason: {{ reason }}</p>{% endif %}"
		),
	},
	"order_confirmed": {
		"subject": "Order {{ order }} confirmed - please pay within {{ hours }} hours",
		"response": (
			"<p>Hi {{ full_name }},</p>"
			"<p><strong>{{ seller_name }}</strong> confirmed order {{ order }} ({{ quantity }} {{ unit }} "
			"of <strong>{{ title }}</strong>) at {{ price }} per {{ unit }}, "
			"<strong>{{ total }}</strong> in total.</p>"
			"<p>Pay the seller within {{ hours }} hours using the bank details on the order page, then "
			"upload your payment slip, or the order is cancelled.</p>"
		),
	},
	"order_declined": {
		"subject": "Order {{ order }} declined",
		"response": (
			"<p>Hi {{ full_name }},</p>"
			"<p><strong>{{ seller_name }}</strong> declined order {{ order }} ({{ quantity }} {{ unit }} "
			"of <strong>{{ title }}</strong>).</p>"
			"{% if reason %}<p>Reason: {{ reason }}</p>{% endif %}"
		),
	},
	"order_cancelled": {
		"subject": "Order {{ order }} cancelled",
		"response": (
			"<p>Hi {{ full_name }},</p>"
			"<p><strong>{{ actor_name }}</strong> cancelled order {{ order }} ({{ quantity }} {{ unit }} "
			"of <strong>{{ title }}</strong>).</p>"
			"{% if reason %}<p>Reason: {{ reason }}</p>{% endif %}"
		),
	},
	"order_cancel_requested": {
		"subject": "Cancellation request on order {{ order }}",
		"response": (
			"<p>Hi {{ full_name }},</p>"
			"<p><strong>{{ buyer_name }}</strong> asked to cancel order {{ order }} "
			"({{ quantity }} {{ unit }} of <strong>{{ title }}</strong>).</p>"
			"<p>Reason: {{ reason }}</p>"
			"<p>Accept or refuse the request on the order page.</p>"
		),
	},
	"order_cancel_request_refused": {
		"subject": "Cancellation request refused - order {{ order }}",
		"response": (
			"<p>Hi {{ full_name }},</p>"
			"<p><strong>{{ seller_name }}</strong> refused your request to cancel order {{ order }} "
			"({{ quantity }} {{ unit }} of <strong>{{ title }}</strong>).</p>"
			"{% if reason %}<p>Reason: {{ reason }}</p>{% endif %}"
			"<p>The order stays open: pay the seller within the {{ hours }}-hour payment window and "
			"upload your payment slip, or the order is cancelled.</p>"
		),
	},
	"order_staff_cancel_requested": {
		"subject": "Request to cancel order {{ order }}",
		"response": (
			"<p>Hi {{ full_name }},</p>"
			"{% if for_staff %}"
			"<p><strong>{{ seller_name }}</strong> asked staff to cancel order {{ order }} "
			"({{ quantity }} {{ unit }} of <strong>{{ title }}</strong>) after a rejected payment slip."
			"</p><p>Reason: {{ reason }}</p>"
			"<p>Review it in the payments queue.</p>"
			"{% else %}"
			"<p><strong>{{ seller_name }}</strong> asked Accreage Mart staff to cancel your order "
			"{{ order }} ({{ quantity }} {{ unit }} of <strong>{{ title }}</strong>).</p>"
			"<p>Reason: {{ reason }}</p>"
			"<p>Staff will review it. You can still upload a new payment slip before the deadline.</p>"
			"{% endif %}"
		),
	},
	"order_staff_cancel_request_refused": {
		"subject": "Staff kept order {{ order }} open",
		"response": (
			"<p>Hi {{ full_name }},</p>"
			"<p>Staff refused your request to cancel order {{ order }} ({{ quantity }} {{ unit }} of "
			"<strong>{{ title }}</strong>).</p>"
			"<p>Reason: {{ reason }}</p>"
		),
	},
	"payment_proof_submitted": {
		"subject": "Payment slip to review - order {{ order }}",
		"response": (
			"<p>Hi {{ full_name }},</p>"
			"<p><strong>{{ buyer_name }}</strong> uploaded a payment slip for order {{ order }} "
			"({{ quantity }} {{ unit }} of <strong>{{ title }}</strong>).</p>"
			"<p>Amount paid: <strong>{{ amount_paid }}</strong> on {{ paid_on }} from {{ paid_from_bank }} "
			"(reference {{ transfer_reference }}).</p>"
			"{% if amount_differs %}<p><strong>This differs from the order total of {{ total }}.</strong></p>"
			"{% endif %}"
			"{% if for_staff %}"
			"<p>The seller reviews it first; you can review it in the payments queue.</p>"
			"{% else %}"
			"<p>Check that the money reached your account, then approve or reject the slip.</p>"
			"{% endif %}"
		),
	},
	"payment_proof_rejected": {
		"subject": "Payment slip rejected - order {{ order }}",
		"response": (
			"<p>Hi {{ full_name }},</p>"
			"{% if for_seller %}"
			"<p>Accreage Mart staff rejected the buyer's payment slip for order {{ order }} "
			"({{ quantity }} {{ unit }} of <strong>{{ title }}</strong>).</p>"
			"{% else %}"
			"<p><strong>{{ reviewer_name }}</strong> rejected your payment slip for order {{ order }} "
			"({{ quantity }} {{ unit }} of <strong>{{ title }}</strong>).</p>"
			"{% endif %}"
			"<p>Why: <strong>{{ reject_category }}</strong></p>"
			"<p>Reason: {{ reason }}</p>"
			"{% if for_seller %}"
			"<p>The buyer has {{ hours }} hours to upload a new slip.</p>"
			"{% else %}"
			"<p>Upload a new payment slip within {{ hours }} hours.</p>"
			"{% endif %}"
		),
	},
	"payment_proof_approved": {
		"subject": "Payment approved - order {{ order }}",
		"response": (
			"<p>Hi {{ full_name }},</p>"
			"{% if for_seller %}"
			"<p>Accreage Mart staff approved the buyer's payment for order {{ order }} "
			"({{ quantity }} {{ unit }} of <strong>{{ title }}</strong>).</p>"
			"{% if reason %}<p>Note: {{ reason }}</p>{% endif %}"
			"<p>Please arrange delivery and mark the order delivered.</p>"
			"{% else %}"
			"<p><strong>{{ reviewer_name }}</strong> approved your payment of <strong>{{ total }}</strong> "
			"for order {{ order }} ({{ quantity }} {{ unit }} of <strong>{{ title }}</strong>).</p>"
			"{% if reason %}<p>Note: {{ reason }}</p>{% endif %}"
			"<p>The seller will now arrange delivery.</p>"
			"{% endif %}"
		),
	},
	"order_delivered": {
		"subject": "Order {{ order }} delivered - please confirm",
		"response": (
			"<p>Hi {{ full_name }},</p>"
			"<p><strong>{{ seller_name }}</strong> marked order {{ order }} "
			"({{ quantity }} {{ unit }} of <strong>{{ title }}</strong>) as delivered.</p>"
			"<p>Please confirm you received the goods within {{ days }} days. If you don't, the order "
			"is completed automatically.</p>"
		),
	},
	"order_completed": {
		"subject": "Order {{ order }} completed - invoice {{ invoice_no }}",
		"response": (
			"<p>Hi {{ full_name }},</p>"
			"<p>Order {{ order }} ({{ quantity }} {{ unit }} of <strong>{{ title }}</strong>, "
			"<strong>{{ total }}</strong>) "
			"{% if auto %}was completed automatically {{ days }} days after delivery, as receipt was not "
			"confirmed.{% else %}was completed: {{ buyer_name }} confirmed receiving the goods.{% endif %}</p>"
			"<p>Invoice {{ invoice_no }} is ready to download on the order page.</p>"
		),
	},
}


def ensure_email_templates():
	"""Seed the transactional email templates, editable from the desk afterwards.

	Never overwrites an existing template — an admin's content edits survive
	``bench migrate`` (same idempotency rule as ``ensure_roles``)."""
	for name, fields in EMAIL_TEMPLATES.items():
		if frappe.db.exists("Email Template", name):
			continue
		frappe.get_doc(
			{
				"doctype": "Email Template",
				"name": name,
				"subject": fields["subject"],
				"response": fields["response"],
			}
		).insert(ignore_permissions=True)


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
			"verification_status": "Approved",
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
			"verification_status": "Approved",
		},
	)

	# Self-heal: demo profiles created before verification_status existed (or by any
	# other path that sets verified=1 directly) should read "Approved", not "Pending".
	for doctype in ("Buyer Profile", "Seller Profile"):
		frappe.db.set_value(
			doctype,
			{"verified": 1, "verification_status": ["!=", "Approved"]},
			"verification_status",
			"Approved",
		)


def ensure_price_suggestion_settings():
	"""Seed Price Suggestion Settings defaults once per field. Never overwrites an
	already-configured value - same idempotency rule as ensure_email_templates: if an admin
	already saved a threshold, bench migrate must not reset it. Checked and repaired
	independently per field (not gated on just one) so a field left at a falsy value by any
	other code path gets re-seeded rather than silently staying wrong forever."""
	doc = frappe.get_single("Price Suggestion Settings")
	changed = False
	for fieldname, default in (
		("mape_threshold_1_7d", 20),
		("mape_threshold_8_14d", 20),
		("mape_threshold_15_30d", 20),
		("interval_recalibration_factor", 1.5),
		("forecast_horizon_days", 30),
	):
		if not doc.get(fieldname):
			doc.set(fieldname, default)
			changed = True
	if changed:
		doc.save(ignore_permissions=True)


def _ensure_demo_profile(user: str, doctype: str, fields: dict):
	if not frappe.db.exists("User", user) or frappe.db.exists(doctype, {"user": user}):
		return
	doc = frappe.new_doc(doctype)
	doc.user = user
	doc.update(fields)
	doc.insert(ignore_permissions=True)
