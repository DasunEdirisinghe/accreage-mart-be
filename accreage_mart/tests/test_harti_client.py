from datetime import date

import requests
from frappe.tests.utils import FrappeTestCase

from accreage_mart.pricing.harti_client import download_latest_pdf, fetch_real_pdf_index

INDEX_HTML = """
<table>
<tr class="font-13"><td>2026-09-09</td><td>English</td><td>
  <a href="assets/pdf/food_price/daily/eng/2026/September/Vegetables Wholesale Prices (2026.09.09)1.pdf">dl</a>
</td></tr>
<tr class="font-13"><td>2026-09-10</td><td>English</td><td>
  <a href="assets/pdf/food_price/daily/eng/2026/September/Vegetable Pricenew ex1(2026.09.10).pdf">dl</a>
</td></tr>
</table>
"""


class FakeResponse:
	def __init__(self, status_code=200, content=b"", text=""):
		self.status_code = status_code
		self.content = content
		self.text = text

	def raise_for_status(self):
		if self.status_code >= 400:
			raise requests.HTTPError(f"HTTP {self.status_code}")


class FakeSession:
	"""Returns canned responses keyed by URL substring - never touches the network."""

	def __init__(self, responses=None, raise_on=None):
		self.responses = responses or {}
		self.raise_on = raise_on or []
		self.requested_urls = []

	def get(self, url, headers=None, timeout=None):
		self.requested_urls.append(url)
		for substr in self.raise_on:
			if substr in url:
				raise requests.ConnectionError("simulated network failure")
		for substr, response in self.responses.items():
			if substr in url:
				return response
		return FakeResponse(status_code=404)


class TestHartiClient(FrappeTestCase):
	def test_fetch_real_pdf_index_parses_rows(self):
		session = FakeSession(responses={"daily-price.php": FakeResponse(text=INDEX_HTML)})
		index = fetch_real_pdf_index(session=session)

		self.assertEqual(len(index), 2)
		self.assertIn("2026-09-09", index)
		self.assertIn("2026-09-10", index)
		# literal space in the source href becomes %20 in the resolved URL
		self.assertIn("%20", index["2026-09-09"])
		self.assertTrue(index["2026-09-09"].startswith("https://www.harti.gov.lk/"))

	def test_fetch_real_pdf_index_returns_empty_on_fetch_error(self):
		session = FakeSession(raise_on=["daily-price.php"])
		index = fetch_real_pdf_index(session=session)
		self.assertEqual(index, {})

	def test_download_latest_pdf_uses_real_index_url_first(self):
		real_url = "https://www.harti.gov.lk/assets/pdf/food_price/daily/eng/2026/September/real.pdf"
		session = FakeSession(responses={real_url: FakeResponse(content=b"%PDF-1.7 fake content")})

		result = download_latest_pdf(
			date(2026, 9, 9), session=session, real_index={"2026-09-09": real_url}
		)

		self.assertEqual(result["status"], "downloaded")
		self.assertEqual(result["content"], b"%PDF-1.7 fake content")
		self.assertEqual(result["url"], real_url)
		# the real-index URL must be tried before any guessed candidate
		self.assertEqual(session.requested_urls[0], real_url)

	def test_download_latest_pdf_not_found_when_every_candidate_404s(self):
		session = FakeSession()  # everything 404s by default
		result = download_latest_pdf(date(2026, 9, 9), session=session, real_index={})
		self.assertEqual(result["status"], "not_found")
		self.assertIsNone(result["content"])

	def test_download_latest_pdf_failed_on_network_error_with_no_success(self):
		session = FakeSession(raise_on=["harti.gov.lk"])
		result = download_latest_pdf(date(2026, 9, 9), session=session, real_index={}, retries=1)
		self.assertEqual(result["status"], "failed")
		self.assertIsNone(result["content"])
		self.assertTrue(result["detail"])
