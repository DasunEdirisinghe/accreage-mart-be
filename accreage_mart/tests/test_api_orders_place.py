from datetime import timedelta
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import get_datetime, now_datetime

from accreage_mart.api import orders as api
from accreage_mart.tests.listing_fixtures import make_auction_listing, set_status
from accreage_mart.tests.order_fixtures import (
	BANK,
	BUYER_A,
	BUYER_B,
	NOTIFY,
	SELLER_A,
	SELLER_B,
	USERS,
	approved_listing,
	make_order,
	make_seller,
	purge_orders,
	set_bank_details,
	setup_world,
)
from accreage_mart.utils import listing as lu
from accreage_mart.utils import order as ou


class OrderApiTestCase(FrappeTestCase):
	def setUp(self):
		setup_world(self)

	def tearDown(self):
		purge_orders(*USERS)


class TestPlaceOrder(OrderApiTestCase):
	def test_listed_price_order_waits_for_seller_confirmation(self):
		frappe.set_user(BUYER_A)
		before = now_datetime()
		result = api.place_order(self.listing.name, 50)

		self.assertRegex(result["name"], r"^ORD-\d{5,}$")
		self.assertEqual(result["status"], ou.PENDING_CONFIRMATION)
		self.assertEqual(result["quantity"], 50)
		self.assertEqual(result["unit"], "kg")
		self.assertEqual(result["listed_unit_price"], 120)
		self.assertEqual(result["total_price"], 6000)
		self.assertFalse(result["agreed_unit_price"])
		self.assertEqual(result["offer_count"], 0)
		self.assertEqual(result["offers"], [])
		self.assertEqual(result["source"], ou.SOURCE_DIRECT)
		self.assertEqual(result["viewer_role"], ou.BUYER)
		self.assertEqual(result["action_by"], ou.SELLER)
		self.assertEqual(result["on_expiry"], ou.ON_EXPIRY_CANCEL)
		deadline = get_datetime(result["status_deadline"])
		self.assertGreaterEqual(deadline, before + timedelta(hours=48) - timedelta(seconds=1))
		self.assertLessEqual(deadline, now_datetime() + timedelta(hours=48))

		order = frappe.get_doc("Order", result["name"])
		self.assertEqual(order.buyer, BUYER_A)
		self.assertEqual(order.seller, self.seller)
		self.assertEqual(order.listing, self.listing.name)

	def test_offer_price_opens_a_negotiation_with_the_first_offer(self):
		frappe.set_user(BUYER_A)
		result = api.place_order(self.listing.name, 50, offer_price=100)

		self.assertEqual(result["status"], ou.NEGOTIATING)
		self.assertEqual(result["listed_unit_price"], 120)
		self.assertEqual(result["total_price"], 5000)
		self.assertEqual(result["offer_count"], 1)
		self.assertEqual(len(result["offers"]), 1)
		offer = result["offers"][0]
		self.assertEqual(offer["party"], ou.PARTY_BUYER)
		self.assertEqual(offer["unit_price"], 100)
		self.assertEqual(offer["outcome"], ou.OFFER_OPEN)
		self.assertEqual(result["action_by"], ou.SELLER)
		self.assertEqual(result["on_expiry"], ou.ON_EXPIRY_CANCEL)
		self.assertTrue(result["status_deadline"])
		self.assertEqual(frappe.db.get_value("Order Offer", offer["name"], "offered_by"), BUYER_A)

	def test_offer_at_or_above_listed_price_is_a_listed_price_order(self):
		frappe.set_user(BUYER_A)
		for price in (120, 150):
			result = api.place_order(self.listing.name, 20, offer_price=price)
			self.assertEqual(result["status"], ou.PENDING_CONFIRMATION)
			self.assertEqual(result["total_price"], 2400)
			self.assertEqual(result["offers"], [])

	def test_listed_price_is_snapshotted(self):
		frappe.set_user(BUYER_A)
		result = api.place_order(self.listing.name, 20)
		frappe.set_user("Administrator")
		frappe.db.set_value("Listing", self.listing.name, "price_per_unit", 999)
		self.assertEqual(frappe.db.get_value("Order", result["name"], "listed_unit_price"), 120)

	def test_rejects_a_listing_that_is_not_approved(self):
		for status in (lu.PENDING, lu.HIDDEN, lu.SUSPENDED, lu.ARCHIVED):
			set_status(self.listing.name, status)
			frappe.set_user(BUYER_A)
			with self.assertRaises(frappe.ValidationError, msg=status):
				api.place_order(self.listing.name, 20)
			frappe.set_user("Administrator")

	def test_rejects_an_auction_listing(self):
		listing, _ = make_auction_listing(self.seller, self.category)
		set_status(listing.name, lu.APPROVED)
		frappe.set_user(BUYER_A)
		with self.assertRaises(frappe.ValidationError):
			api.place_order(listing.name, 20)

	def test_rejects_quantity_below_minimum_or_above_stock(self):
		frappe.set_user(BUYER_A)
		for quantity in (0, -5, 9, 501):
			with self.assertRaises(frappe.ValidationError, msg=quantity):
				api.place_order(self.listing.name, quantity)
		# Both ends are inclusive.
		self.assertEqual(api.place_order(self.listing.name, 10)["quantity"], 10)
		self.assertEqual(api.place_order(self.listing.name, 500)["quantity"], 500)

	def test_rejects_non_positive_offer_price(self):
		frappe.set_user(BUYER_A)
		for price in (0, -10):
			with self.assertRaises(frappe.ValidationError, msg=price):
				api.place_order(self.listing.name, 20, offer_price=price)

	def test_rejects_ordering_your_own_listing(self):
		# A seller who also holds a Buyer account.
		frappe.get_doc("User", self.seller).add_roles("Buyer")
		frappe.set_user(self.seller)
		with self.assertRaises(frappe.PermissionError):
			api.place_order(self.listing.name, 20)

	def test_only_buyers_in_good_standing(self):
		for user in ("Guest", self.seller, self.staff):
			frappe.set_user(user)
			with self.assertRaises((frappe.PermissionError, frappe.AuthenticationError), msg=user):
				api.place_order(self.listing.name, 20)
		frappe.set_user("Administrator")
		frappe.db.set_value("User", BUYER_A, "custom_account_status", "suspended")
		frappe.set_user(BUYER_A)
		with self.assertRaises(frappe.PermissionError):
			api.place_order(self.listing.name, 20)

	def test_unknown_listing(self):
		frappe.set_user(BUYER_A)
		with self.assertRaises(frappe.DoesNotExistError):
			api.place_order("LST-does-not-exist", 20)


class TestPlaceOrderNotification(OrderApiTestCase):
	def _notifications(self, order_name):
		return frappe.get_all(
			"Notification Log",
			filters={"document_type": "Order", "document_name": order_name},
			fields=["name", "for_user", "subject", "email_content", "custom_event_type"],
		)

	def test_seller_is_notified_of_a_listed_price_order(self):
		frappe.set_user(BUYER_A)
		with (
			patch(f"{NOTIFY}.smtp_configured", return_value=True),
			patch(f"{NOTIFY}.send_templated_email") as send,
		):
			result = api.place_order(self.listing.name, 50)

		frappe.set_user("Administrator")
		rows = self._notifications(result["name"])
		self.assertEqual(len(rows), 1)
		row = rows[0]
		self.assertEqual(row.for_user, SELLER_A)
		self.assertEqual(row.custom_event_type, "order_placed")
		for text in (result["name"], "ZZ Test Carrots", "50 kg", "Rs. 120.00", "Rs. 6,000.00", "48 hours"):
			self.assertIn(text, row.email_content)

		channels = {
			d.channel: d.status for d in frappe.get_doc("Notification Log", row.name).custom_delivery_log
		}
		self.assertEqual(channels.get("Email"), "Sent")
		self.assertEqual(channels.get("SMS"), "Not Implemented")

		send.assert_called_once()
		kwargs = send.call_args.kwargs
		self.assertEqual(kwargs["key"], "order_placed")
		self.assertEqual(kwargs["recipient"], SELLER_A)
		self.assertTrue(kwargs["cta_url"].endswith(f"/seller/orders/{result['name']}"))

	def test_seller_is_notified_of_an_offer(self):
		frappe.set_user(BUYER_A)
		result = api.place_order(self.listing.name, 50, offer_price=100)
		frappe.set_user("Administrator")
		rows = self._notifications(result["name"])
		self.assertEqual(len(rows), 1)
		self.assertIn("Rs. 100.00", rows[0].email_content)
		self.assertIn("Rs. 120.00", rows[0].email_content)

	def test_template_renders(self):
		from accreage_mart.setup.install import ensure_email_templates

		ensure_email_templates()
		template = frappe.get_doc("Email Template", "order_placed")
		context = {
			"full_name": "Nimal",
			"buyer_name": "Hotel Lanka",
			"order": "ORD-00001",
			"title": "Carrots",
			"quantity": "50",
			"unit": "kg",
			"listed_price": "Rs. 120.00",
			"offer_price": "Rs. 100.00",
			"total": "Rs. 6,000.00",
			"hours": 48,
		}
		formatted = template.get_formatted_email(context)
		self.assertIn("ORD-00001", formatted["subject"])
		self.assertIn("Rs. 100.00", formatted["message"])
		self.assertNotIn("{{", formatted["message"])
		formatted = template.get_formatted_email({**context, "offer_price": None})
		self.assertIn("Rs. 6,000.00", formatted["message"])
		self.assertNotIn("offered", formatted["message"])

	def test_notification_failure_never_blocks_the_order(self):
		frappe.set_user(BUYER_A)
		with patch("accreage_mart.api.orders.notify", side_effect=Exception("boom")):
			result = api.place_order(self.listing.name, 20)
		self.assertTrue(frappe.db.exists("Order", result["name"]))


class TestReadOrders(OrderApiTestCase):
	def setUp(self):
		super().setUp()
		make_seller(SELLER_B)
		self.other_listing = approved_listing(SELLER_B, self.category)
		frappe.set_user(BUYER_A)
		self.mine = api.place_order(self.listing.name, 20)["name"]
		self.mine_offer = api.place_order(self.listing.name, 30, offer_price=100)["name"]
		frappe.set_user(BUYER_B)
		self.theirs = api.place_order(self.other_listing.name, 40)["name"]
		frappe.set_user("Administrator")

	def test_get_order_for_buyer_seller_and_staff(self):
		for user, role in ((BUYER_A, ou.BUYER), (SELLER_A, ou.SELLER), (self.staff, ou.STAFF)):
			frappe.set_user(user)
			result = api.get_order(self.mine)
			self.assertEqual(result["name"], self.mine)
			self.assertEqual(result["viewer_role"], role)
			for key in ("status_deadline", "action_by", "on_expiry", "offers", "payment_proofs"):
				self.assertIn(key, result)

	def test_get_order_refuses_anyone_else(self):
		for user in (BUYER_B, SELLER_B):
			frappe.set_user(user)
			with self.assertRaises(frappe.PermissionError, msg=user):
				api.get_order(self.mine)
		frappe.set_user("Guest")
		with self.assertRaises(frappe.AuthenticationError):
			api.get_order(self.mine)
		frappe.set_user(BUYER_A)
		with self.assertRaises(frappe.DoesNotExistError):
			api.get_order("ORD-99999999")

	def test_payload_never_leaks_emails_to_parties(self):
		frappe.set_user(SELLER_A)
		result = api.get_order(self.mine)
		self.assertNotIn("email", result["buyer"])
		self.assertNotIn(SELLER_A, str(result["seller"]))
		self.assertEqual(result["seller"]["public_id"], lu.public_seller_id(SELLER_A))
		frappe.set_user(self.staff)
		self.assertEqual(api.get_order(self.mine)["buyer"]["email"], BUYER_A)

	def test_bank_details_only_once_the_price_is_locked(self):
		set_bank_details(self.seller)
		frappe.set_user(BUYER_A)
		result = api.get_order(self.mine)
		self.assertIsNone(result["seller_bank_details"])
		self.assertFalse(result["payment_details_pending"])

		frappe.db.set_value("Order", self.mine, "status", ou.CONFIRMED)
		result = api.get_order(self.mine)
		self.assertEqual(result["seller_bank_details"], BANK)
		self.assertFalse(result["payment_details_pending"])

	def test_payment_details_pending_without_bank_details(self):
		frappe.db.set_value("Order", self.mine, "status", ou.CONFIRMED)
		frappe.set_user(BUYER_A)
		result = api.get_order(self.mine)
		self.assertIsNone(result["seller_bank_details"])
		self.assertTrue(result["payment_details_pending"])

	def test_list_buyer_orders_is_scoped_and_newest_first(self):
		frappe.set_user(BUYER_A)
		result = api.list_buyer_orders()
		self.assertEqual([i["name"] for i in result["items"]], [self.mine_offer, self.mine])
		self.assertEqual(result["total"], 2)
		self.assertFalse(result["has_more"])
		for key in ("status_deadline", "action_by", "on_expiry", "listing", "seller", "buyer"):
			self.assertIn(key, result["items"][0])
		self.assertEqual(result["items"][0]["listing"]["title"], "ZZ Test Carrots")

		frappe.set_user(BUYER_B)
		self.assertEqual([i["name"] for i in api.list_buyer_orders()["items"]], [self.theirs])

	def test_list_seller_orders_is_scoped(self):
		frappe.set_user(SELLER_A)
		self.assertEqual([i["name"] for i in api.list_seller_orders()["items"]], [self.mine_offer, self.mine])
		frappe.set_user(SELLER_B)
		self.assertEqual([i["name"] for i in api.list_seller_orders()["items"]], [self.theirs])

	def test_status_filter(self):
		frappe.set_user(BUYER_A)
		self.assertEqual(
			[i["name"] for i in api.list_buyer_orders(status=ou.NEGOTIATING)["items"]], [self.mine_offer]
		)
		both = api.list_buyer_orders(status=f"{ou.NEGOTIATING},{ou.PENDING_CONFIRMATION}")
		self.assertEqual(both["total"], 2)
		both = api.list_buyer_orders(status=f'["{ou.NEGOTIATING}", "{ou.PENDING_CONFIRMATION}"]')
		self.assertEqual(both["total"], 2)
		self.assertEqual(api.list_buyer_orders(status=ou.COMPLETED)["total"], 0)
		with self.assertRaises(frappe.ValidationError):
			api.list_buyer_orders(status="shipped")

	def test_pagination(self):
		frappe.set_user(BUYER_A)
		first = api.list_buyer_orders(page=1, page_size=1)
		self.assertEqual(len(first["items"]), 1)
		self.assertTrue(first["has_more"])
		second = api.list_buyer_orders(page=2, page_size=1)
		self.assertEqual(second["items"][0]["name"], self.mine)
		self.assertFalse(second["has_more"])

	def test_list_role_guards(self):
		frappe.set_user(SELLER_A)
		with self.assertRaises(frappe.PermissionError):
			api.list_buyer_orders()
		frappe.set_user(BUYER_A)
		with self.assertRaises(frappe.PermissionError):
			api.list_seller_orders()

	def test_list_all_orders_is_staff_only(self):
		for user in (BUYER_A, SELLER_A):
			frappe.set_user(user)
			with self.assertRaises(frappe.PermissionError, msg=user):
				api.list_all_orders()
		frappe.set_user(self.staff)
		names = [i["name"] for i in api.list_all_orders(page_size=48)["items"]]
		for name in (self.mine, self.mine_offer, self.theirs):
			self.assertIn(name, names)
		filtered = [i["name"] for i in api.list_all_orders(status=ou.NEGOTIATING, page_size=48)["items"]]
		self.assertIn(self.mine_offer, filtered)
		self.assertNotIn(self.mine, filtered)


class TestBankDetailsEndpoints(OrderApiTestCase):
	def test_seller_reads_and_updates_their_own(self):
		frappe.set_user(SELLER_A)
		empty = api.get_my_bank_details()
		self.assertFalse(empty["complete"])
		self.assertIsNone(empty["account_number"])

		saved = api.update_my_bank_details(" Bank of Ceylon ", "Kandy", "Test Farms", "0012-3456 78")
		self.assertEqual(saved, {**BANK, "complete": True})
		self.assertEqual(api.get_my_bank_details(), {**BANK, "complete": True})
		self.assertTrue(ou.has_bank_details(self.seller))

	def test_all_four_fields_required_and_account_number_digits(self):
		frappe.set_user(SELLER_A)
		with self.assertRaises(frappe.ValidationError):
			api.update_my_bank_details("Bank of Ceylon", "", "Test Farms", "0012345678")
		with self.assertRaises(frappe.ValidationError):
			api.update_my_bank_details("Bank of Ceylon", "Kandy", "Test Farms", "12AB34")
		with self.assertRaises(frappe.ValidationError):
			api.update_my_bank_details("Bank of Ceylon", "Kandy", "Test Farms", "123")
		frappe.set_user("Administrator")
		self.assertFalse(ou.has_bank_details(self.seller))

	def test_sellers_only(self):
		for user in ("Guest", BUYER_A):
			frappe.set_user(user)
			with self.assertRaises((frappe.PermissionError, frappe.AuthenticationError), msg=user):
				api.get_my_bank_details()
			with self.assertRaises((frappe.PermissionError, frappe.AuthenticationError), msg=user):
				api.update_my_bank_details("B", "Br", "N", "0012345678")

	def test_update_touches_only_the_callers_profile(self):
		make_seller(SELLER_B)
		frappe.set_user(SELLER_B)
		api.update_my_bank_details("Sampath Bank", "Galle", "Other Farms", "99887766")
		frappe.set_user("Administrator")
		self.assertFalse(ou.has_bank_details(SELLER_A))
		self.assertTrue(ou.has_bank_details(SELLER_B))

	def test_generic_resource_read_hides_bank_details_from_a_buyer(self):
		set_bank_details(self.seller)
		frappe.set_user(BUYER_A)
		doc = frappe.get_doc("Seller Profile", self.seller)
		doc.apply_fieldlevel_read_permissions()
		for field in ou.BANK_FIELDS:
			self.assertFalse(doc.get(field), field)


class TestListingWiring(OrderApiTestCase):
	def test_active_order_count_reaches_the_listing_action_info(self):
		from accreage_mart.api import listings as listings_api

		make_order(self.listing, BUYER_A)
		make_order(self.listing, BUYER_A, status=ou.CANCELLED)
		frappe.set_user(SELLER_A)
		info = listings_api.get_listing_action_info(self.listing.name)
		self.assertEqual(info["active_order_count"], 1)
