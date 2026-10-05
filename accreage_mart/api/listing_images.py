"""Whitelisted listing image endpoints (Epic 04, Story 4.5).

Referenced from the frontend as ``accreage_mart.api.listing_images.<fn>``.

Guard matrix
------------
=======================  ================  ====================================================
Endpoint                 Caller            Notes
=======================  ================  ====================================================
upload_listing_image     verified seller   multipart field ``file``; JPG/PNG/WebP up to 5 MB,
                                            checked by content; stored public and unattached;
                                            returns the URL to send to create/update_listing
discard_listing_image    owner of the      deletes an upload that is not on any listing
                         upload
=======================  ================  ====================================================

An upload is only attached by ``create_listing`` / ``update_listing`` / ``duplicate_listing``,
which check that every image is the seller's own (see ``utils.listing_images.check_image_rows``).
Unattached uploads are removed after 24 hours by a daily job.
"""

import frappe
from frappe import _

from accreage_mart.utils import listing as lu
from accreage_mart.utils import listing_images as images

FIELD = "file"


def _uploaded_bytes() -> bytes:
	upload = (frappe.request.files if frappe.request else {}).get(FIELD)
	if not upload:
		frappe.throw(_("No file was uploaded."))
	# Read one byte past the limit so an oversized file is caught without loading all of it.
	return upload.stream.read(images.MAX_IMAGE_BYTES + 1)


@frappe.whitelist(methods=["POST"])
def upload_listing_image() -> dict:
	lu.require_seller()
	content = _uploaded_bytes()
	file = images.save_listing_image(content)
	return {
		"url": file.file_url,
		"file": file.name,
		"size": len(content),
		"content_type": images.MIME_TYPES[images.detect_image_type(content)],
	}


@frappe.whitelist(methods=["POST"])
def discard_listing_image(url: str) -> dict:
	"""Remove an upload the seller changed their mind about. Images on a listing stay."""
	lu.require_seller()
	user = frappe.session.user
	names = frappe.get_all(
		"File", filters={"file_url": url, "is_private": 0, "owner": user}, pluck="name"
	)
	if not names:
		frappe.throw(_("Image not found."), frappe.DoesNotExistError)
	if images.image_in_use(url):
		frappe.throw(_("This image is used by a listing. Remove it from the listing instead."))
	for name in names:
		frappe.delete_doc("File", name, ignore_permissions=True, force=True)
	return {"ok": True}
