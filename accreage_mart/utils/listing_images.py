"""Listing image handling (Epic 04, Story 4.5): content checks, storage, ownership, cleanup.

Uploads are public files with a random name (the original filename is never kept: it can leak
personal information) and are not attached to anything until a listing references them. A daily
job removes uploads that never made it onto a listing.
"""

import io
import uuid

import frappe
from frappe import _
from frappe.utils import add_to_date, now_datetime

MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_PIXELS = 40_000_000
FILE_PREFIX = "lst-"
ORPHAN_AFTER_HOURS = 24

EXTENSIONS = {"jpeg": "jpg", "png": "png", "webp": "webp"}
MIME_TYPES = {"jpeg": "image/jpeg", "png": "image/png", "webp": "image/webp"}


def detect_image_type(content: bytes) -> str | None:
	"""jpeg / png / webp from the file's own bytes, never from its name or declared type."""
	if content.startswith(b"\xff\xd8\xff"):
		return "jpeg"
	if content.startswith(b"\x89PNG\r\n\x1a\n"):
		return "png"
	if content[:4] == b"RIFF" and content[8:12] == b"WEBP":
		return "webp"
	return None


def _verify_decodes(content: bytes, kind: str) -> None:
	"""The bytes must open as a real image of the detected kind, within a sane size."""
	from PIL import Image

	try:
		with Image.open(io.BytesIO(content)) as image:
			actual = (image.format or "").lower()
			width, height = image.size
			too_big = width * height > MAX_PIXELS
			if actual == kind and not too_big:
				image.verify()
	except Exception:
		frappe.throw(_("This file is not a valid image."))
	if actual != kind:
		frappe.throw(_("This file is not a valid image."))
	if too_big:
		frappe.throw(_("This image is too large. Please use one under 40 megapixels."))


def validate_image(content: bytes) -> str:
	"""Throw unless ``content`` is a JPG, PNG or WebP of at most 5 MB. Returns the kind."""
	if not content:
		frappe.throw(_("The file is empty."))
	if len(content) > MAX_IMAGE_BYTES:
		frappe.throw(_("Images can be at most 5 MB."))
	kind = detect_image_type(content)
	if not kind:
		frappe.throw(_("Only JPG, PNG and WebP images are allowed."))
	_verify_decodes(content, kind)
	return kind


def save_listing_image(content: bytes):
	"""Validate and store an upload as a public, unattached file. Returns the File document."""
	kind = validate_image(content)
	return frappe.get_doc(
		{
			"doctype": "File",
			"file_name": f"{FILE_PREFIX}{uuid.uuid4().hex}.{EXTENSIONS[kind]}",
			"content": content,
			"is_private": 0,
		}
	).insert(ignore_permissions=True)


def image_in_use(file_url: str) -> bool:
	return bool(frappe.db.exists("Listing Image", {"image": file_url, "parenttype": "Listing"}))


def check_image_rows(rows: list[dict], seller_profile: str, listing=None) -> None:
	"""Every image on a listing must be a public upload by the seller, an image already on one of
	their listings (a copy or an edit), or already on this listing. External or other people's
	files are refused."""
	seller_user = frappe.db.get_value("Seller Profile", seller_profile, "user")
	already_here = {row.image for row in listing.images} if listing else set()
	own_listings = frappe.get_all("Listing", filters={"seller": seller_profile}, pluck="name")

	for row in rows:
		url = row.get("image") or ""
		if not url.startswith("/files/"):
			frappe.throw(_("Images must be uploaded through the listing form."))
		if url in already_here:
			continue
		if own_listings and frappe.db.exists(
			"Listing Image", {"image": url, "parenttype": "Listing", "parent": ["in", own_listings]}
		):
			continue
		if not frappe.db.exists("File", {"file_url": url, "is_private": 0, "owner": seller_user}):
			frappe.throw(_("One of the images was not uploaded by you. Please upload it again."))


def delete_orphan_images(older_than_hours: int = ORPHAN_AFTER_HOURS) -> int:
	"""Scheduled daily: remove listing uploads nobody attached within the grace period."""
	cutoff = add_to_date(now_datetime(), hours=-older_than_hours)
	removed = 0
	for file in frappe.get_all(
		"File",
		filters=[
			["file_name", "like", f"{FILE_PREFIX}%"],
			["is_private", "=", 0],
			["creation", "<", cutoff],
		],
		fields=["name", "file_url"],
	):
		if not image_in_use(file.file_url):
			frappe.delete_doc("File", file.name, ignore_permissions=True, force=True)
			removed += 1
	return removed
