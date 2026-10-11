"""Seed demo sellers and listings (both types, with some review history).

Run it after the categories are seeded (it is safe to repeat)::

    bench --site acm.localhost execute accreage_mart.setup.seed_listings.run

Dev sites only (``developer_mode``): the demo sellers get the demo password, like the demo
quick sign-in accounts. Rules, so repeating it never duplicates anything:
  * a seller is matched by email, a listing by (seller, title); an existing one is left alone;
  * a listing whose category is missing is skipped and reported (run seed_categories first);
  * review history is written only when the listing is created.

Not hooked into ``migrate``: this is demo data, created only when someone asks for it.
"""

import io

import frappe
from frappe.utils import add_to_date, now_datetime

from accreage_mart.utils import listing as lu

DEMO_PASSWORD = "demo1234"
FILE_PREFIX = "lst-demo-"

# email -> Seller Profile values. The first seller already exists on dev sites (install.py).
SELLERS: dict[str, dict] = {
	"seller@demo.accreagemart.lk": {
		"business_name": "Demo Fresh Farms",
		"district": "Nuwara Eliya",
	},
	"kandy.growers@demo.accreagemart.lk": {
		"business_name": "Kandy Hills Growers",
		"district": "Kandy",
		"location": "Peradeniya",
		"description": "Banana and fruit growers from the Kandy hills.",
	},
	"dambulla.traders@demo.accreagemart.lk": {
		"business_name": "Dambulla Dry Zone Traders",
		"district": "Matale",
		"location": "Dambulla",
		"description": "Dry zone vegetables and chillies, sold in bulk.",
	},
}

# Review history to create with a listing: (action, reason / seller note), oldest first.
# Status is the final status. ``auction`` is (hours until start, hours of bidding) or None.
LISTINGS: tuple[dict, ...] = (
	{
		"seller": "seller@demo.accreagemart.lk",
		"title": "Fresh Carrots, Grade A (Upcountry)",
		"category": "Carrot",
		"price": 150,
		"qty": 1200,
		"unit": "kg",
		"district": "Nuwara Eliya",
		"location": "Nuwara Eliya market",
		"color": (230, 126, 34),
		"status": lu.APPROVED,
		"history": [("Approved", None)],
	},
	{
		"seller": "seller@demo.accreagemart.lk",
		"title": "Organic Cabbage, Certified",
		"category": "Cabbage",
		"price": 120,
		"qty": 60,
		"low_stock": 100,
		"min_order": 20,
		"unit": "kg",
		"district": "Nuwara Eliya",
		"location": "Ragala",
		"organic": "Certified organic by an accredited body (demo certificate)",
		"color": (46, 139, 87),
		"status": lu.APPROVED,
		"history": [
			("Rejected", "The photo is too dark to judge quality. Please add a daylight photo."),
			("Resubmitted", "Added a new daylight photo."),
			("Approved", None),
		],
	},
	{
		"seller": "kandy.growers@demo.accreagemart.lk",
		"title": "Embul Kesel, Fresh Bunches",
		"category": "Embul Kesel",
		"price": 180,
		"qty": 900,
		"unit": "kg",
		"district": "Kandy",
		"location": "Peradeniya",
		"color": (241, 196, 15),
		"status": lu.APPROVED,
		"history": [("Approved", None)],
	},
	{
		"seller": "kandy.growers@demo.accreagemart.lk",
		"title": "Embul Kesel Auction Lot",
		"category": "Embul Kesel",
		"auction": (-3, 30),  # started 3 hours ago, 30 hours of bidding
		"min_bid": 170,
		"qty": 1500,
		"unit": "kg",
		"district": "Kandy",
		"location": "Gampola",
		"color": (212, 172, 13),
		"status": lu.APPROVED,
		"history": [("Approved", None)],
	},
	{
		"seller": "dambulla.traders@demo.accreagemart.lk",
		"title": "Green Chillies, Dry Zone Harvest",
		"category": "Green Chillies",
		"price": 450,
		"qty": 300,
		"unit": "kg",
		"district": "Matale",
		"location": "Dambulla",
		"color": (39, 174, 96),
		"status": lu.APPROVED,
		"history": [("Approved", None)],
	},
	{
		"seller": "dambulla.traders@demo.accreagemart.lk",
		"title": "Big Onions Auction Lot",
		"category": "Other",
		"auction": (48, 24),  # starts in two days
		"min_bid": 200,
		"qty": 2000,
		"unit": "kg",
		"district": "Matale",
		"location": "Dambulla",
		"color": (142, 68, 173),
		"status": lu.PENDING,
		"history": [],
	},
	{
		"seller": "seller@demo.accreagemart.lk",
		"title": "Leeks, Wholesale Bulk",
		"category": "Leeks",
		"price": 200,
		"qty": 400,
		"unit": "kg",
		"district": "Nuwara Eliya",
		"location": "Nuwara Eliya market",
		"color": (88, 214, 141),
		"status": lu.REJECTED,
		"history": [("Rejected", "The quantity does not match the photos. Please check and resubmit.")],
	},
	{
		"seller": "kandy.growers@demo.accreagemart.lk",
		"title": "Papaya, Red Lady",
		"category": "Papaya",
		"price": 140,
		"qty": 250,
		"unit": "kg",
		"district": "Kandy",
		"location": "Peradeniya",
		"color": (255, 127, 80),
		"status": lu.HIDDEN,
		"history": [("Approved", None)],
	},
	{
		"seller": "dambulla.traders@demo.accreagemart.lk",
		"title": "Tomatoes, Hybrid Greenhouse",
		"category": "Tomato",
		"price": 410,
		"qty": 500,
		"unit": "kg",
		"district": "Matale",
		"location": "Dambulla",
		"color": (192, 57, 43),
		"status": lu.SUSPENDED,
		"history": [
			("Approved", None),
			("Suspended", "Buyers reported that the listed grade did not match the delivered goods."),
		],
	},
)


def _ensure_seller(email: str, values: dict) -> None:
	if not frappe.db.exists("User", email):
		from frappe.utils.password import update_password

		user = frappe.new_doc("User")
		user.email = email
		user.first_name = values["business_name"]
		user.user_type = "System User"
		user.enabled = 1
		user.send_welcome_email = 0
		user.flags.no_welcome_mail = True
		user.insert(ignore_permissions=True)
		user.add_roles("Seller", "System Manager")
		user.db_set("custom_account_status", "active")
		update_password(email, DEMO_PASSWORD)
	if not frappe.db.exists("Seller Profile", {"user": email}):
		frappe.get_doc(
			{
				"doctype": "Seller Profile",
				"user": email,
				"verified": 1,
				"verification_status": "Approved",
				**values,
			}
		).insert(ignore_permissions=True)


def _image_url(slug: str, color: tuple[int, int, int]) -> str:
	"""A small public colour block, created once per listing (no photos ship with the app)."""
	from PIL import Image

	file_name = f"{FILE_PREFIX}{slug}.png"
	existing = frappe.db.get_value("File", {"file_name": file_name, "is_private": 0}, "file_url")
	if existing:
		return existing
	buffer = io.BytesIO()
	Image.new("RGB", (240, 180), color).save(buffer, "PNG")
	file = frappe.get_doc(
		{"doctype": "File", "file_name": file_name, "content": buffer.getvalue(), "is_private": 0}
	).insert(ignore_permissions=True)
	return file.file_url


def _slug(title: str) -> str:
	return "".join(ch if ch.isalnum() else "-" for ch in title.lower()).strip("-")[:60]


def _create_listing(spec: dict, category: str) -> str:
	values = {
		"doctype": "Listing",
		"title": spec["title"],
		"seller": spec["seller"],
		"category": category,
		"selling_type": lu.AUCTION if spec.get("auction") else lu.DIRECT,
		"description": f"{spec['title']}. Demo listing, sorted and packed for bulk buyers.",
		"unit": spec["unit"],
		"quantity_available": spec["qty"],
		"price_per_unit": 0 if spec.get("auction") else spec["price"],
		"min_order_qty": spec.get("min_order", 0),
		"low_stock_level": spec.get("low_stock", 0),
		"district": spec["district"],
		"location": spec["location"],
		"images": [{"image": _image_url(_slug(spec["title"]), spec["color"])}],
	}
	if spec.get("organic"):
		values["organic"] = 1
		values["certification"] = spec["organic"]
	listing = frappe.get_doc(values).insert(ignore_permissions=True)

	if spec.get("auction"):
		starts_in, hours = spec["auction"]
		# Created with a valid future window; a running auction is moved back afterwards (the 24h
		# start gap only applies to new auctions, and this is demo data).
		start = add_to_date(now_datetime(), hours=max(starts_in, 30))
		auction = frappe.get_doc(
			{
				"doctype": "Auction",
				"min_bid": spec["min_bid"],
				"start_time": start,
				"end_time": add_to_date(start, hours=hours),
			}
		).insert(ignore_permissions=True)
		real_start = add_to_date(now_datetime(), hours=starts_in)
		frappe.db.set_value(
			"Auction",
			auction.name,
			{"start_time": real_start, "end_time": add_to_date(real_start, hours=hours)},
			update_modified=False,
		)
		with lu.system_write():
			listing.auction = auction.name
			listing.save(ignore_permissions=True)

	with lu.system_write():
		listing.submitted_on = now_datetime()
		listing.status = spec["status"]
		for action, text in spec["history"]:
			frappe.get_doc(
				{
					"doctype": "Listing Review",
					"listing": listing.name,
					"action": action,
					"reason": text if action in ("Rejected", "Suspended") else None,
					"seller_note": text if action == "Resubmitted" else None,
					"reviewer": spec["seller"] if action == "Resubmitted" else "Administrator",
				}
			).insert(ignore_permissions=True)
		if spec["status"] != lu.PENDING:
			listing.reviewed_by = "Administrator"
			listing.reviewed_on = now_datetime()
		if spec["status"] in (lu.REJECTED, lu.SUSPENDED):
			listing.status_reason = spec["history"][-1][1]
		listing.save(ignore_permissions=True)
	return listing.name


def run(commit: bool = True) -> dict[str, list[str]]:
	"""Create what is missing and return ``{"created": [...], "existing": [...], "skipped": [...]}``.

	``bench execute`` does not commit on its own, so this commits at the end. Tests pass
	``commit=False`` to stay inside their own transaction."""
	summary: dict[str, list[str]] = {"created": [], "existing": [], "skipped": []}
	if not frappe.conf.get("developer_mode"):
		summary["skipped"].append("not a developer site: demo data is not created")
		_print(summary)
		return summary

	previous_user = frappe.session.user
	frappe.set_user("Administrator")
	try:
		for email, values in SELLERS.items():
			_ensure_seller(email, values)
		for spec in LISTINGS:
			label = f"{spec['title']} ({spec['seller']})"
			if frappe.db.exists("Listing", {"title": spec["title"], "seller": spec["seller"]}):
				summary["existing"].append(label)
				continue
			category = frappe.db.get_value("Category", {"title": spec["category"]})
			if not category:
				summary["skipped"].append(f"{label}: category '{spec['category']}' is missing")
				continue
			_create_listing(spec, category)
			summary["created"].append(label)
	finally:
		frappe.set_user(previous_user)

	if commit:
		frappe.db.commit()
	_print(summary)
	return summary


def _print(summary: dict[str, list[str]]) -> None:
	for key in ("created", "existing", "skipped"):
		print(f"{key}: {len(summary[key])}")
		for line in summary[key]:
			print(f"  - {line}")
