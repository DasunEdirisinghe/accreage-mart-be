"""Story 8.5 - the order-timer job (D4, D13, D14) and auction-win orders (D8, D16)."""

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import add_to_date, get_datetime, now_datetime

from accreage_mart.api import auctions as auctions_api
from accreage_mart.api import orders as api
from accreage_mart.tests.listing_fixtures import make_auction_listing, set_status, start_auction_now
from accreage_mart.tests.order_fixtures import (
	BANK,
	BUYER_A,
	BUYER_B,
	SELLER_A,
	STAFF,
	USERS,
	expire_deadline,
	make_confirmed_order,
	make_offer,
	make_order,
	make_proof,
	order_notifications,
	purge_orders,
	set_bank_details,
	setup_world,
)
from accreage_mart.utils import order as ou


def _fields(name, *fields):
	return frappe.db.get_value("Order", name, list(fields), as_dict=True)


class TimerTestCase(FrappeTestCase):
	def setUp(self):
		setup_world(self)
		set_bank_details(self.seller)

	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.local.response = frappe._dict()
		purge_orders(*USERS)

	def as_user(self, user, fn, *args, **kwargs):
		frappe.set_user(user)
		try:
			return fn(*args, **kwargs)
		finally:
			frappe.set_user("Administrator")

	def recipients(self, order_name, event):
		return sorted(row.for_user for row in order_notifications(order_name, event))

	def assertAbout(self, value, expected, seconds=120):
		self.assertIsNotNone(value)
		delta = abs((get_datetime(value) - get_datetime(expected)).total_seconds())
		self.assertLess(delta, seconds, f"{value} is not close to {expected}")


# -- the timer job ---------------------------------------------------------------------------------


class TestConfirmationAndOfferTimers(TimerTestCase):
	def test_pending_confirmation_past_48h_is_cancelled_and_both_parties_told_why(self):
		order = make_order(self.listing, BUYER_A).name
		expire_deadline(order)

		api.enforce_order_timers()

		row = _fields(order, "status", "status_deadline", "cancel_reason", "cancelled_by")
		self.assertEqual(row.status, ou.CANCELLED)
		self.assertIsNone(row.status_deadline)
		self.assertIsNone(row.cancelled_by)  # nobody: the system did it
		self.assertIn("did not confirm", row.cancel_reason)
		self.assertIn("48 hours", row.cancel_reason)
		self.assertEqual(self.recipients(order, "order_expired"), sorted([BUYER_A, SELLER_A]))
		for log in order_notifications(order, "order_expired"):
			self.assertIn("cancelled automatically", log.subject + log.email_content)

	def test_an_order_still_within_its_deadline_is_left_alone(self):
		order = make_order(self.listing, BUYER_A).name

		api.enforce_order_timers()

		self.assertEqual(_fields(order, "status").status, ou.PENDING_CONFIRMATION)
		self.assertEqual(order_notifications(order), [])

	def test_negotiating_past_48h_expires_the_open_offer_and_cancels(self):
		order = make_order(self.listing, BUYER_A, status=ou.NEGOTIATING, offer_count=1).name
		offer = make_offer(order, BUYER_A, ou.PARTY_BUYER, 100).name
		expire_deadline(order)

		api.enforce_order_timers()

		row = _fields(order, "status", "cancel_reason")
		self.assertEqual(row.status, ou.CANCELLED)
		self.assertIn("did not answer the buyer's offer", row.cancel_reason)
		self.assertEqual(frappe.db.get_value("Order Offer", offer, "outcome"), ou.OFFER_EXPIRED)
		self.assertEqual(self.recipients(order, "order_expired"), sorted([BUYER_A, SELLER_A]))

	def test_a_seller_counter_left_unanswered_names_the_buyer(self):
		order = make_order(self.listing, BUYER_A, status=ou.NEGOTIATING, offer_count=2).name
		make_offer(order, BUYER_A, ou.PARTY_BUYER, 100, outcome=ou.OFFER_COUNTERED)
		make_offer(order, SELLER_A, ou.PARTY_SELLER, 110)
		expire_deadline(order)

		api.enforce_order_timers()

		self.assertIn(
			"buyer did not answer the seller's offer", _fields(order, "cancel_reason").cancel_reason
		)


class TestPaymentTimer(TimerTestCase):
	def test_confirmed_past_72h_with_no_slip_ever_is_cancelled(self):
		order = make_confirmed_order(self.listing, BUYER_A).name
		expire_deadline(order)

		api.enforce_order_timers()

		row = _fields(order, "status", "cancel_reason", "needs_staff_review")
		self.assertEqual(row.status, ou.CANCELLED)
		self.assertIn("no payment slip was uploaded within 72 hours", row.cancel_reason)
		self.assertFalse(row.needs_staff_review)
		self.assertEqual(self.recipients(order, "order_expired"), sorted([BUYER_A, SELLER_A]))

	def test_after_a_rejected_slip_the_order_is_flagged_for_staff_not_cancelled(self):
		order = make_confirmed_order(self.listing, BUYER_A, has_submitted_proof=1).name
		make_proof(
			order, status=ou.PROOF_REJECTED, reject_category=ou.REJECT_CATEGORIES[0], reject_reason="Blurry"
		)
		expire_deadline(order)

		api.enforce_order_timers()

		doc = frappe.get_doc("Order", order)
		self.assertEqual(doc.status, ou.CONFIRMED)
		self.assertTrue(doc.needs_staff_review)
		self.assertIsNone(doc.status_deadline)
		self.assertEqual(ou.deadline_info(doc)["action_by"], ou.STAFF)
		self.assertIsNone(ou.deadline_info(doc)["on_expiry"])
		# Every active staff user on the site is told, not only the test one.
		recipients = set(self.recipients(order, "order_payment_flagged"))
		self.assertTrue({BUYER_A, SELLER_A, STAFF} <= recipients, recipients)
		self.assertEqual(recipients - {BUYER_A, SELLER_A}, set(ou.staff_users()))
		self.assertEqual(order_notifications(order, "order_expired"), [])

	def test_a_flagged_order_shows_up_in_the_staff_queue(self):
		order = make_confirmed_order(self.listing, BUYER_A, has_submitted_proof=1).name
		expire_deadline(order)
		api.enforce_order_timers()

		queue = self.as_user(STAFF, api.list_pending_proofs)
		names = [item["order"]["name"] for item in queue["needs_staff_review"]["items"]]
		self.assertIn(order, names)

	def test_a_confirmed_order_without_a_deadline_is_never_touched(self):
		order = make_confirmed_order(self.listing, BUYER_A, status_deadline=None).name

		api.enforce_order_timers()

		row = _fields(order, "status", "needs_staff_review")
		self.assertEqual(row.status, ou.CONFIRMED)
		self.assertFalse(row.needs_staff_review)
		self.assertEqual(order_notifications(order), [])


class TestDeliveryTimer(TimerTestCase):
	def test_delivered_past_7_days_is_completed_with_an_invoice(self):
		now = now_datetime()
		order = make_confirmed_order(
			self.listing,
			BUYER_A,
			status=ou.DELIVERED,
			has_submitted_proof=1,
			paid_on=add_to_date(now, days=-8),
			delivered_on=add_to_date(now, days=-7, minutes=-1),
			status_deadline=ou.deadline_for(ou.DELIVERED, add_to_date(now, days=-7, minutes=-1)),
		).name

		api.enforce_order_timers()

		doc = frappe.get_doc("Order", order)
		self.assertEqual(doc.status, ou.COMPLETED)
		self.assertIsNone(doc.status_deadline)
		self.assertTrue(doc.invoice_no)
		self.assertTrue(doc.invoice_file)
		logs = order_notifications(order, "order_completed")
		self.assertEqual(sorted(log.for_user for log in logs), sorted([BUYER_A, SELLER_A]))
		self.assertIn("completed automatically", logs[0].email_content)


class TestTimerJob(TimerTestCase):
	def test_one_pass_handles_every_timer_and_a_rerun_changes_nothing(self):
		pending = make_order(self.listing, BUYER_A).name
		unpaid = make_confirmed_order(self.listing, BUYER_B).name
		flagged = make_confirmed_order(self.listing, BUYER_A, has_submitted_proof=1).name
		for name in (pending, unpaid, flagged):
			expire_deadline(name)

		api.enforce_order_timers()
		counts = {name: len(order_notifications(name)) for name in (pending, unpaid, flagged)}
		modified = {name: _fields(name, "modified").modified for name in (pending, unpaid, flagged)}
		api.enforce_order_timers()  # idempotent re-run

		self.assertEqual(_fields(pending, "status").status, ou.CANCELLED)
		self.assertEqual(_fields(unpaid, "status").status, ou.CANCELLED)
		self.assertTrue(_fields(flagged, "needs_staff_review").needs_staff_review)
		for name in (pending, unpaid, flagged):
			self.assertEqual(len(order_notifications(name)), counts[name])
			self.assertEqual(_fields(name, "modified").modified, modified[name])

	def test_one_failing_order_does_not_stop_the_others(self):
		broken = make_order(self.listing, BUYER_A).name
		fine = make_order(self.listing, BUYER_B).name
		expire_deadline(broken)
		expire_deadline(fine)
		frappe.db.commit()  # the job rolls back after a failure; keep the fixtures
		real = api.expire_order

		def flaky(name):
			if name == broken:
				raise RuntimeError("boom")
			return real(name)

		with patch.object(api, "expire_order", side_effect=flaky):
			api.enforce_order_timers()

		self.assertEqual(_fields(broken, "status").status, ou.PENDING_CONFIRMATION)
		self.assertEqual(_fields(fine, "status").status, ou.CANCELLED)

	def test_expire_order_rechecks_the_deadline_under_the_lock(self):
		order = make_order(self.listing, BUYER_A).name  # deadline still 48 h away

		self.assertIsNone(api.expire_order(order))
		self.assertEqual(_fields(order, "status").status, ou.PENDING_CONFIRMATION)

	def test_statuses_without_a_timer_are_ignored_even_with_a_stale_deadline(self):
		order = make_confirmed_order(
			self.listing, BUYER_A, status=ou.PAYMENT_REVIEW, has_submitted_proof=1
		).name
		expire_deadline(order)

		api.enforce_order_timers()

		self.assertEqual(_fields(order, "status").status, ou.PAYMENT_REVIEW)

	def test_the_job_is_scheduled_every_five_minutes(self):
		from accreage_mart import hooks

		self.assertIn(
			"accreage_mart.api.orders.enforce_order_timers",
			hooks.scheduler_events["cron"]["*/5 * * * *"],
		)


# -- auction-win orders (D8, D16) ------------------------------------------------------------------


class AuctionOrderTestCase(TimerTestCase):
	def closed_auction(self, bids=((BUYER_A, 150), (BUYER_B, 200))):
		"""An auction on Seller A's listing with ``bids`` placed, now past its end time."""
		listing, auction = make_auction_listing(self.seller, self.category)
		set_status(listing.name, "Approved")
		start_auction_now(auction.name)
		for user, amount in bids:
			self.as_user(user, auctions_api.place_bid, auction.name, amount)
		frappe.db.set_value("Auction", auction.name, "end_time", add_to_date(now_datetime(), hours=-1))
		return frappe.get_doc("Listing", listing.name), auction

	def auction_orders(self, auction_name):
		return frappe.get_all("Order", filters={"auction": auction_name}, pluck="name")


class TestAuctionWinOrder(AuctionOrderTestCase):
	def test_closing_creates_one_confirmed_order_for_the_full_lot_at_the_winning_bid(self):
		listing, auction = self.closed_auction()

		auctions_api.close_expired_auctions()

		names = self.auction_orders(auction.name)
		self.assertEqual(len(names), 1)
		doc = frappe.get_doc("Order", names[0])
		self.assertEqual(doc.buyer, BUYER_B)
		self.assertEqual(doc.seller, self.seller)
		self.assertEqual(doc.listing, listing.name)
		self.assertEqual(doc.source, ou.SOURCE_AUCTION)
		self.assertEqual(doc.status, ou.CONFIRMED)
		self.assertEqual(doc.quantity, listing.quantity_available)
		self.assertEqual(doc.unit, listing.unit)
		self.assertEqual(doc.agreed_unit_price, 200)
		self.assertEqual(doc.total_price, listing.quantity_available * 200)
		self.assertAbout(doc.confirmed_on, now_datetime())
		self.assertAbout(doc.status_deadline, add_to_date(now_datetime(), hours=72))
		info = ou.deadline_info(doc)
		self.assertEqual((info["action_by"], info["on_expiry"]), (ou.BUYER, ou.ON_EXPIRY_CANCEL))
		self.assertEqual(self.recipients(doc.name, "order_auction_won"), sorted([BUYER_B, SELLER_A]))

	def test_closing_is_idempotent_never_two_orders_per_auction(self):
		_, auction = self.closed_auction()

		auctions_api.close_expired_auctions()
		auctions_api.close_expired_auctions()
		order, created = api.create_auction_order(auction.name, BUYER_A, 999)

		self.assertFalse(created)
		self.assertEqual(self.auction_orders(auction.name), [order.name])
		self.assertEqual(order.buyer, BUYER_B)
		self.assertEqual(len(order_notifications(order.name, "order_auction_won")), 2)

	def test_the_order_is_created_even_if_the_close_notifications_fail(self):
		_, auction = self.closed_auction()

		with patch.object(auctions_api, "notify", side_effect=Exception("smtp down")):
			auctions_api.close_expired_auctions()
		auctions_api.close_expired_auctions()  # next minute: notifications retried, no second order

		self.assertEqual(len(self.auction_orders(auction.name)), 1)

	def test_an_auction_with_no_bids_creates_no_order(self):
		_, auction = self.closed_auction(bids=())

		auctions_api.close_expired_auctions()

		self.assertEqual(self.auction_orders(auction.name), [])

	def test_the_winner_sees_the_order_with_bank_details(self):
		_, auction = self.closed_auction()
		auctions_api.close_expired_auctions()
		name = self.auction_orders(auction.name)[0]

		data = self.as_user(BUYER_B, api.get_order, name)

		self.assertEqual(data["source"], ou.SOURCE_AUCTION)
		self.assertFalse(data["payment_details_pending"])
		self.assertEqual(data["seller_bank_details"]["account_number"], BANK["account_number"])
		self.assertEqual(data["action_by"], ou.BUYER)


class TestAuctionOrderWithoutBankDetails(AuctionOrderTestCase):
	def setUp(self):
		super().setUp()
		frappe.db.set_value(
			"Seller Profile", self.seller, {field: None for field in ou.BANK_FIELDS}, update_modified=False
		)

	def win(self):
		_, auction = self.closed_auction()
		auctions_api.close_expired_auctions()
		return self.auction_orders(auction.name)[0]

	def test_the_order_is_created_with_no_payment_clock_and_the_seller_is_asked_for_bank_details(self):
		name = self.win()

		doc = frappe.get_doc("Order", name)
		self.assertEqual(doc.status, ou.CONFIRMED)
		self.assertIsNone(doc.status_deadline)
		info = ou.deadline_info(doc)
		self.assertEqual(
			(info["status_deadline"], info["action_by"], info["on_expiry"]), (None, ou.SELLER, None)
		)

		data = self.as_user(BUYER_B, api.get_order, name)
		self.assertTrue(data["payment_details_pending"])
		self.assertIsNone(data["seller_bank_details"])
		self.assertIsNone(data["status_deadline"])
		self.assertEqual(data["action_by"], ou.SELLER)

		seller_log = [
			log for log in order_notifications(name, "order_auction_won") if log.for_user == SELLER_A
		]
		self.assertEqual(len(seller_log), 1)
		self.assertIn("bank details", seller_log[0].subject)
		buyer_log = [log for log in order_notifications(name, "order_auction_won") if log.for_user == BUYER_B]
		self.assertIn("starts once they do", buyer_log[0].email_content)

	def test_the_timer_job_never_cancels_it(self):
		name = self.win()

		api.enforce_order_timers()

		self.assertEqual(_fields(name, "status").status, ou.CONFIRMED)

	def test_saving_bank_details_starts_the_72h_and_tells_the_buyer_where_to_pay(self):
		name = self.win()

		self.as_user(SELLER_A, api.update_my_bank_details, **BANK)

		doc = frappe.get_doc("Order", name)
		self.assertAbout(doc.status_deadline, add_to_date(now_datetime(), hours=72))
		info = ou.deadline_info(doc)
		self.assertEqual((info["action_by"], info["on_expiry"]), (ou.BUYER, ou.ON_EXPIRY_CANCEL))
		logs = order_notifications(name, "order_payment_details_added")
		self.assertEqual([log.for_user for log in logs], [BUYER_B])
		self.assertIn(BANK["account_number"], logs[0].email_content)
		data = self.as_user(BUYER_B, api.get_order, name)
		self.assertFalse(data["payment_details_pending"])

		# Saving again later restarts nothing and tells nobody twice.
		deadline = doc.status_deadline
		self.as_user(SELLER_A, api.update_my_bank_details, **{**BANK, "bank_branch": "Galle"})
		self.assertEqual(
			get_datetime(_fields(name, "status_deadline").status_deadline), get_datetime(deadline)
		)
		self.assertEqual(len(order_notifications(name, "order_payment_details_added")), 1)

	def test_saving_bank_details_leaves_other_orders_alone(self):
		name = self.win()
		flagged = make_confirmed_order(
			self.listing, BUYER_A, status_deadline=None, has_submitted_proof=1, needs_staff_review=1
		).name
		running = make_confirmed_order(self.listing, BUYER_A).name
		running_deadline = _fields(running, "status_deadline").status_deadline

		self.as_user(SELLER_A, api.update_my_bank_details, **BANK)

		self.assertIsNone(_fields(flagged, "status_deadline").status_deadline)
		self.assertEqual(_fields(running, "status_deadline").status_deadline, running_deadline)
		self.assertTrue(_fields(name, "status_deadline").status_deadline)

	def test_a_slip_rejected_before_bank_details_exist_does_not_start_a_clock(self):
		name = self.win()
		frappe.db.set_value("Order", name, {"status": ou.PAYMENT_REVIEW, "has_submitted_proof": 1})
		proof = make_proof(name, status=ou.PROOF_SUBMITTED).name

		self.as_user(
			STAFF,
			api.review_payment_proof,
			proof,
			"reject",
			reject_category=ou.REJECT_CATEGORIES[1],
			reject_reason="Not received yet.",
		)

		row = _fields(name, "status", "status_deadline")
		self.assertEqual(row.status, ou.CONFIRMED)
		self.assertIsNone(row.status_deadline)


class TestOrderTimerEmailTemplates(FrappeTestCase):
	def test_new_templates_render_for_every_variant(self):
		from accreage_mart.setup.install import ensure_email_templates

		ensure_email_templates()
		base = {"full_name": "Jane", "order": "ORD-1", "title": "Rice", "quantity": "20", "unit": "kg"}
		variants = {
			"order_expired": [{"reason": "no payment slip was uploaded within 72 hours"}],
			"order_payment_flagged": [
				{"for_role": role, "hours": 72} for role in ("staff", "buyer", "seller")
			],
			"order_auction_won": [
				{
					"for_seller": s,
					"payment_pending": p,
					"price": "Rs. 1.00",
					"total": "Rs. 20.00",
					"hours": 72,
				}
				for s in (True, False)
				for p in (True, False)
			],
			"order_payment_details_added": [
				{"seller_name": "Farm", "total": "Rs. 20.00", "hours": 72, **BANK}
			],
		}
		for name, contexts in variants.items():
			template = frappe.get_doc("Email Template", name)
			for context in contexts:
				formatted = template.get_formatted_email({**base, **context})
				for part in ("subject", "message"):
					self.assertTrue(formatted[part].strip(), f"{name} {context} {part} is empty")
					self.assertNotIn("{{", formatted[part])
					self.assertNotIn("{%", formatted[part])
