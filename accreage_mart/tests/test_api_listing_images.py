import io

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import add_to_date, now_datetime
from werkzeug.test import EnvironBuilder

from accreage_mart.api import listing_images as api
from accreage_mart.api import listings as listings_api
from accreage_mart.tests.listing_fixtures import (
	SELLER_A,
	SELLER_B,
	image_bytes,
	make_category,
	make_image_file,
	make_listing,
	make_seller,
	purge,
	track_file,
)
from accreage_mart.utils import listing_images as images

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def multipart_request(content: bytes | None = None, filename: str = "photo.png"):
	"""A real werkzeug request, the way the endpoint receives an upload."""
	data = {"file": (io.BytesIO(content), filename)} if content is not None else {}
	builder = EnvironBuilder(method="POST", base_url="http://accreage-mart.localhost", data=data)
	return builder.get_request()


class ImageTestCase(FrappeTestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		purge(SELLER_A, SELLER_B)
		self.seller = make_seller(SELLER_A)
		self.other = make_seller(SELLER_B)
		self.category = make_category()

	def tearDown(self):
		frappe.local.request = None
		purge(SELLER_A, SELLER_B)

	def upload(self, content: bytes, filename="photo.png", user=SELLER_A) -> dict:
		"""Call the endpoint as ``user`` with ``content`` in the multipart ``file`` field."""
		frappe.set_user(user)
		frappe.local.request = multipart_request(content, filename)
		try:
			result = api.upload_listing_image()
		finally:
			frappe.local.request = None
			frappe.set_user("Administrator")
		track_file(result["file"])
		return result


class TestUpload(ImageTestCase):
	def test_stores_each_allowed_type_as_a_public_unattached_file(self):
		allowed = (
			("PNG", "png", "image/png"),
			("JPEG", "jpg", "image/jpeg"),
			("WEBP", "webp", "image/webp"),
		)
		for kind, extension, mime in allowed:
			content = image_bytes(kind)
			result = self.upload(content)
			self.assertTrue(result["url"].startswith("/files/lst-"), result)
			self.assertTrue(result["url"].endswith(f".{extension}"))
			self.assertEqual((result["content_type"], result["size"]), (mime, len(content)))
			file = frappe.get_doc("File", result["file"])
			self.assertEqual((file.is_private, file.owner), (0, SELLER_A))
			self.assertFalse(file.attached_to_doctype)
			self.assertEqual(file.get_content(), content)

	def test_the_original_filename_and_claimed_extension_are_ignored(self):
		result = self.upload(image_bytes("PNG"), filename="../../Dasun NIC front.exe")
		self.assertTrue(result["url"].endswith(".png"))
		self.assertNotIn("Dasun", result["url"])
		self.assertNotIn("exe", result["url"])

	def test_rejects_anything_that_is_not_a_real_allowed_image(self):
		bad = {
			"empty": b"",
			"text named png": b"just some text, not an image",
			"html": b"<html><script>alert(1)</script></html>",
			"svg": b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>',
			"gif": b"GIF89a" + b"\x00" * 64,
			"png header only": PNG_MAGIC + b"\x00" * 32,
			"truncated jpeg": b"\xff\xd8\xff\xe0" + b"\x00" * 16,
			"riff but not webp": b"RIFF\x00\x00\x00\x00WAVEfmt " + b"\x00" * 16,
		}
		for label, content in bad.items():
			with self.assertRaises(frappe.ValidationError, msg=label):
				self.upload(content)

	def test_rejects_files_over_5_megabytes(self):
		with self.assertRaises(frappe.ValidationError):
			self.upload(PNG_MAGIC + b"\x00" * images.MAX_IMAGE_BYTES)

	def test_rejects_images_with_too_many_pixels(self):
		from PIL import Image

		buffer = io.BytesIO()
		Image.new("1", (10000, 10000)).save(buffer, "PNG")  # 100 megapixels, tiny on disk
		self.assertLess(len(buffer.getvalue()), images.MAX_IMAGE_BYTES)
		with self.assertRaises(frappe.ValidationError):
			self.upload(buffer.getvalue())

	def test_a_request_without_a_file_is_refused(self):
		frappe.set_user(SELLER_A)
		frappe.local.request = multipart_request()
		with self.assertRaises(frappe.ValidationError):
			api.upload_listing_image()

	def test_only_verified_sellers_can_upload(self):
		with self.assertRaises(frappe.AuthenticationError):
			self.upload(image_bytes(), user="Guest")
		with self.assertRaises(frappe.PermissionError):
			self.upload(image_bytes(), user="Administrator")
		make_seller("listing.unverified@x.lk", verified=False)
		self.addCleanup(purge, "listing.unverified@x.lk")
		with self.assertRaises(frappe.PermissionError):
			self.upload(image_bytes(), user="listing.unverified@x.lk")


class TestAttachingImages(ImageTestCase):
	def args(self, images_):
		return {
			"category": self.category,
			"title": "ZZ Test Photo Carrots",
			"description": "Fresh.",
			"selling_type": "Direct",
			"unit": "kg",
			"quantity_available": 100,
			"district": "Kandy",
			"location": "Kandy",
			"images": images_,
			"price_per_unit": 100,
		}

	def create(self, images_, user=SELLER_A):
		frappe.set_user(user)
		return listings_api.create_listing(**self.args(images_))

	def test_a_listing_can_use_the_sellers_own_uploads_and_picks_the_cover(self):
		first, second = self.upload(image_bytes())["url"], self.upload(image_bytes())["url"]
		result = self.create([{"image": first}, {"image": second, "is_cover": 1}])
		rows = frappe.get_doc("Listing", result["name"]).images
		self.assertEqual([(r.image, r.is_cover) for r in rows], [(first, 0), (second, 1)])

	def test_a_single_image_is_the_cover(self):
		url = self.upload(image_bytes())["url"]
		rows = frappe.get_doc("Listing", self.create([{"image": url}])["name"]).images
		self.assertEqual([r.is_cover for r in rows], [1])

	def test_at_least_one_and_at_most_five_images(self):
		with self.assertRaises(frappe.ValidationError):
			self.create([])
		six = [{"image": self.upload(image_bytes())["url"]} for _ in range(6)]
		with self.assertRaises(frappe.ValidationError):
			self.create(six)
		self.create(six[:5])

	def test_another_sellers_upload_and_external_urls_are_refused(self):
		theirs = self.upload(image_bytes(), user=SELLER_B)["url"]
		refused = (
			theirs,
			"https://example.com/tracker.png",
			"/private/files/secret.png",
			"/files/nope.png",
			"",
		)
		for url in refused:
			with self.assertRaises(frappe.ValidationError, msg=url):
				self.create([{"image": url}])

	def test_the_same_check_applies_when_editing(self):
		listing = make_listing(self.seller, self.category)
		theirs = self.upload(image_bytes(), user=SELLER_B)["url"]
		mine = self.upload(image_bytes())["url"]
		frappe.set_user(SELLER_A)
		with self.assertRaises(frappe.ValidationError):
			listings_api.update_listing(listing.name, {"images": [{"image": theirs}]})
		listings_api.update_listing(listing.name, {"images": [{"image": mine}]})
		self.assertEqual(frappe.get_doc("Listing", listing.name).images[0].image, mine)

	def test_an_image_already_on_the_sellers_own_listing_can_be_reused(self):
		source = make_listing(self.seller, self.category, images=[{"image": "/files/legacy.png"}])
		result = self.create([{"image": source.images[0].image}])
		self.assertEqual(frappe.get_doc("Listing", result["name"]).images[0].image, "/files/legacy.png")


class TestDiscardAndCleanup(ImageTestCase):
	def test_a_seller_can_discard_their_own_unused_upload(self):
		result = self.upload(image_bytes())
		frappe.set_user(SELLER_A)
		self.assertEqual(api.discard_listing_image(result["url"]), {"ok": True})
		self.assertFalse(frappe.db.exists("File", result["file"]))

	def test_an_image_on_a_listing_cannot_be_discarded(self):
		url = make_image_file(SELLER_A)
		make_listing(self.seller, self.category, images=[{"image": url}])
		frappe.set_user(SELLER_A)
		with self.assertRaises(frappe.ValidationError):
			api.discard_listing_image(url)

	def test_nobody_can_discard_someone_elses_upload(self):
		result = self.upload(image_bytes(), user=SELLER_A)
		frappe.set_user(SELLER_B)
		with self.assertRaises(frappe.DoesNotExistError):
			api.discard_listing_image(result["url"])
		with self.assertRaises(frappe.DoesNotExistError):
			api.discard_listing_image("/files/does-not-exist.png")
		self.assertTrue(frappe.db.exists("File", result["file"]))

	def test_the_cleanup_removes_only_old_unattached_listing_uploads(self):
		def aged(url_owner, hours_old):
			url = make_image_file(url_owner)
			name = frappe.db.get_value("File", {"file_url": url})
			frappe.db.set_value(
				"File", name, "creation", add_to_date(now_datetime(), hours=-hours_old), update_modified=False
			)
			return name, url

		old_unused, _ = aged(SELLER_A, 48)
		recent_unused, _ = aged(SELLER_A, 2)
		old_used, used_url = aged(SELLER_A, 48)
		make_listing(self.seller, self.category, images=[{"image": used_url}])
		unrelated = frappe.get_doc(
			{"doctype": "File", "file_name": "ZZ unrelated.png", "content": image_bytes(), "is_private": 0}
		).insert(ignore_permissions=True)
		track_file(unrelated.name)
		frappe.db.set_value(
			"File", unrelated.name, "creation", add_to_date(now_datetime(), days=-9), update_modified=False
		)

		removed = images.delete_orphan_images()

		self.assertGreaterEqual(removed, 1)
		self.assertFalse(frappe.db.exists("File", old_unused))
		self.assertTrue(frappe.db.exists("File", recent_unused))
		self.assertTrue(frappe.db.exists("File", old_used))
		self.assertTrue(frappe.db.exists("File", unrelated.name))

	def test_the_cleanup_is_scheduled_daily(self):
		daily = frappe.get_hooks("scheduler_events")["daily"]
		self.assertIn("accreage_mart.utils.listing_images.delete_orphan_images", daily)
