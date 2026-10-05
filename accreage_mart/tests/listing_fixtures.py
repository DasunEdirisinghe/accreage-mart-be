"""Shared fixtures for the Epic 04 listing tests."""

import io
import os
import uuid

import frappe
from frappe.utils import add_to_date, now_datetime

from accreage_mart.utils import listing as lu
from accreage_mart.utils.profile import create_platform_user

SELLER_A = "listing.seller.a@x.lk"
SELLER_B = "listing.seller.b@x.lk"
CATEGORY_TITLE = "ZZ Listing Test Category"
TITLE = "ZZ Test Carrots"

_created_auctions: list[str] = []
_created_files: list[str] = []


def make_seller(email: str, *, verified: bool = True) -> str:
	"""A Seller user + Seller Profile. Returns the profile name (the email)."""
	create_platform_user(email=email, full_name="Test Seller", role="Seller", status="active")
	frappe.get_doc(
		{
			"doctype": "Seller Profile",
			"user": email,
			"business_name": "Test Farms",
			"district": "Kandy",
			"verified": 1 if verified else 0,
			"verification_status": "Approved" if verified else "Pending",
		}
	).insert(ignore_permissions=True)
	return email


def image_bytes(kind: str = "PNG", size: int = 8) -> bytes:
	"""A small valid image with random pixels, so two calls never share content (Frappe reuses
	the stored file for identical bytes)."""
	from PIL import Image

	image = Image.frombytes("RGB", (size, size), os.urandom(size * size * 3))
	buffer = io.BytesIO()
	image.save(buffer, kind)
	return buffer.getvalue()


def track_file(name: str) -> None:
	_created_files.append(name)


def make_image_file(owner: str) -> str:
	"""A public, unattached image upload owned by ``owner``. Returns its URL."""
	file = frappe.get_doc(
		{
			"doctype": "File",
			"file_name": f"lst-zztest-{uuid.uuid4().hex[:8]}.png",
			"content": image_bytes(),
			"is_private": 0,
		}
	).insert(ignore_permissions=True)
	frappe.db.set_value("File", file.name, "owner", owner, update_modified=False)
	track_file(file.name)
	return file.file_url


def make_category(title: str = CATEGORY_TITLE) -> str:
	existing = frappe.db.get_value("Category", {"title": title})
	if existing:
		return existing
	return frappe.get_doc({"doctype": "Category", "title": title, "area": "Vegetables"}).insert(
		ignore_permissions=True
	).name


def listing_values(seller: str, category: str, **overrides) -> dict:
	values = {
		"doctype": "Listing",
		"title": TITLE,
		"seller": seller,
		"category": category,
		"selling_type": "Direct",
		"description": "Grade A carrots, freshly harvested.",
		"unit": "kg",
		"quantity_available": 500,
		"price_per_unit": 120,
		"district": "Nuwara Eliya",
		"location": "Nuwara Eliya town",
		"images": [{"image": "/files/test-a.png"}],
	}
	values.update(overrides)
	return values


def future(hours: float):
	return add_to_date(now_datetime(), hours=hours)


def auction_values(**overrides) -> dict:
	start = overrides.pop("start_time", future(30))
	values = {
		"doctype": "Auction",
		"min_bid": 100,
		"start_time": start,
		"end_time": add_to_date(start, hours=12),
	}
	values.update(overrides)
	return values


def make_auction(**overrides):
	"""A bare Auction (no listing). Tracked so purge() removes only what the tests created."""
	auction = frappe.get_doc(auction_values(**overrides)).insert(ignore_permissions=True)
	_created_auctions.append(auction.name)
	return auction


def make_listing(seller: str, category: str, **overrides):
	return frappe.get_doc(listing_values(seller, category, **overrides)).insert(ignore_permissions=True)


def make_auction_listing(seller: str, category: str, **auction_overrides):
	"""An Auction listing with its auction created and linked (the create-endpoint order)."""
	listing = make_listing(seller, category, selling_type="Auction", price_per_unit=0)
	auction = make_auction(**auction_overrides)
	with lu.system_write():
		listing.auction = auction.name
		listing.save(ignore_permissions=True)
	return listing, auction


def set_status(listing_name: str, status: str) -> None:
	with lu.system_write():
		doc = frappe.get_doc("Listing", listing_name)
		doc.status = status
		doc.save(ignore_permissions=True)


def start_auction_now(auction_name: str) -> None:
	"""Move an auction's window so it has already started (skips the 24h gap on purpose)."""
	frappe.db.set_value(
		"Auction",
		auction_name,
		{"start_time": add_to_date(now_datetime(), hours=-1), "end_time": future(7)},
		update_modified=False,
	)


def purge(*emails: str) -> None:
	frappe.set_user("Administrator")
	listings = frappe.get_all("Listing", filters={"title": ["like", "ZZ Test%"]}, pluck="name")
	for name in listings:
		frappe.db.delete("Listing Review", {"listing": name})
	for name in listings:
		frappe.delete_doc("Listing", name, force=True, ignore_permissions=True)
	for name in _created_files:
		if frappe.db.exists("File", name):
			frappe.delete_doc("File", name, force=True, ignore_permissions=True)
	_created_files.clear()
	for name in _created_auctions:
		frappe.db.delete("Auction", {"name": name})
	_created_auctions.clear()
	for name in frappe.get_all("Category", filters={"title": ["like", "ZZ Listing Test%"]}, pluck="name"):
		frappe.delete_doc("Category", name, force=True, ignore_permissions=True)
	for email in emails:
		if frappe.db.exists("Seller Profile", {"user": email}):
			frappe.delete_doc("Seller Profile", email, force=True, ignore_permissions=True)
		if frappe.db.exists("User", email):
			frappe.delete_doc("User", email, force=True, ignore_permissions=True)
