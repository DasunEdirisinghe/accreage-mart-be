"""Fetch HARTI daily wholesale-price PDFs by date, as bytes - never writes to disk.

Ported from the standalone 06_price_dataset project's download_pdfs.py, including the
real-index-first fix (this project's own session) for HARTI's inconsistent filename templates:
the site's own daily-price.php listing page pairs each date with its real PDF link directly, so
that's tried first; the older candidate-URL guessing (month-folder x filename-template
combinations) is the fallback for a date the live index doesn't have.
"""

import re
import time
from datetime import date as date_cls
from urllib.parse import urljoin

import requests

SITE_ROOT = "https://www.harti.gov.lk/"
INDEX_PAGE_URL = "https://www.harti.gov.lk/daily-price.php"
BASE = "https://www.harti.gov.lk/assets/pdf/food_price/daily/eng/{year}/{folder}/{filename}"

MONTH_NAMES = {
	1: "January", 2: "February", 3: "March", 4: "April",
	5: "May", 6: "June", 7: "July", 8: "August",
	9: "September", 10: "October", 11: "November", 12: "December",
}

# Folder names known to hold PDFs whose actual date is NOT in that month (retroactive/batched
# uploads). Tried as a fallback after the date's own month folder.
FALLBACK_FOLDER_NAMES = ["December", "February"]

# The filename template changed starting this date (inclusive).
NEW_FORMAT_START = date_cls(2026, 4, 1)

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; harti-price-collector/1.0; research use)"}

ROW_RE = re.compile(r'<tr class="font-13">(.*?)</tr>', re.S)
ROW_DATE_RE = re.compile(r"<td>(\d{4}-\d{2}-\d{2})</td>")
ROW_LINK_RE = re.compile(r'href="([^"]+\.pdf)"')


def candidate_filenames(d: date_cls) -> list:
	old = f"daily_{d.day:02d}-{d.month:02d}-{d.year}.pdf"
	new = f"Vegetable%20Pricenew%20ex1({d.year}.{d.month:02d}.{d.day:02d}).pdf"
	return [new, old] if d >= NEW_FORMAT_START else [old, new]


def candidate_folders(d: date_cls) -> list:
	primary = MONTH_NAMES[d.month]
	fallbacks = [f for f in FALLBACK_FOLDER_NAMES if f != primary]
	return [primary] + fallbacks


def candidate_urls(d: date_cls) -> list:
	"""Ordered list of plausible URLs for a date, most likely first."""
	urls = []
	for folder in candidate_folders(d):
		for filename in candidate_filenames(d):
			urls.append(BASE.format(year=d.year, folder=folder, filename=filename))
	return urls


def fetch_real_pdf_index(session: requests.Session = None, timeout: int = 30) -> dict:
	"""Fetch the live HARTI daily-price listing page and return a {date_iso: absolute_pdf_url}
	map built from its actual table rows - ground truth, not a guessed filename pattern.

	Returns {} on any fetch error so callers fall back to candidate_urls() guessing exactly as
	before - this is a first-try improvement, not a replacement for the fallback.
	"""
	session = session or requests.Session()
	try:
		resp = session.get(INDEX_PAGE_URL, headers=HEADERS, timeout=timeout)
		resp.raise_for_status()
	except requests.RequestException:
		return {}

	index = {}
	for row in ROW_RE.findall(resp.text):
		date_match = ROW_DATE_RE.search(row)
		link_match = ROW_LINK_RE.search(row)
		if not date_match or not link_match:
			continue
		# Real hrefs use literal spaces, not %20 - encode the same way candidate URLs do
		# (parens left as-is, the server accepts them unencoded there too).
		href = link_match.group(1).replace(" ", "%20")
		index[date_match.group(1)] = urljoin(SITE_ROOT, href)
	return index


def download_latest_pdf(
	d: date_cls, session: requests.Session = None, real_index: dict = None,
	timeout: int = 30, retries: int = 2,
) -> dict:
	"""Fetch one date's PDF as bytes - the real-index URL (if known) is tried first, then each
	guessed candidate URL in turn. Never writes to disk.

	Returns {"status": "downloaded"|"not_found"|"failed", "content": bytes|None,
	"url": str|None, "detail": str}.
	"""
	session = session or requests.Session()
	real_url = (real_index or {}).get(d.isoformat())
	urls = ([real_url] if real_url else []) + candidate_urls(d)

	tried = []
	last_error = ""

	for url in urls:
		tried.append(url)
		resp = None

		for attempt in range(1, retries + 1):
			try:
				resp = session.get(url, headers=HEADERS, timeout=timeout)
			except requests.RequestException as e:
				last_error = str(e)
				resp = None
				time.sleep(1.5 * attempt)
				continue
			break

		if resp is None:
			continue  # couldn't get a response for this candidate at all, try next

		if resp.status_code == 200 and resp.content[:4] == b"%PDF":
			return {"status": "downloaded", "content": resp.content, "url": url, "detail": ""}

		if resp.status_code != 404:
			last_error = f"HTTP {resp.status_code}"

	if last_error:
		return {
			"status": "failed", "content": None,
			"url": tried[-1] if tried else None, "detail": last_error,
		}

	return {
		"status": "not_found", "content": None, "url": None,
		"detail": f"no PDF found under any of {len(tried)} candidate URLs",
	}
