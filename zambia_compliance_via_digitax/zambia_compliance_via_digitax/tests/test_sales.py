"""Offline tests for services/sales.py. DigiTax is never called: requests.request, frappe.enqueue and
frappe.db.commit are mocked.

Needs a site with active ZRA SIS Settings, a registered (COMPLETED) item and a registered customer under it.
"""

from unittest.mock import patch

import frappe
import requests
from frappe.tests import IntegrationTestCase
from frappe.utils import add_days, today

from ..apis import sales as sales_api
from ..doctype.doctype_names_mapping import SALES_LEDGER_DOCTYPE_NAME, SETTINGS_DOCTYPE_NAME
from ..services import sales
from .test_purchases import FakeDigitax
from .test_stock import REQUEST, response

SALE_ID = "sale_TEST0000000000000000000009"


def row(**values):
	defaults = {
		"qty": 1,
		"stock_qty": 0,
		"conversion_factor": 1,
		"price_list_rate": 0,
		"rate": 0,
		"net_amount": 0,
	}
	return frappe._dict({**defaults, **values})


def assert_line_math(test, line):
	test.assertEqual(
		round(line["quantity"] * line["unit_price"] - line["discount_amount"], 4), line["total_amount"]
	)


class TestLineMath(IntegrationTestCase):
	def test_exclusive_tax_with_item_discount(self):
		# 2 x 100 less 10% = 180 net, 16% VAT = 28.8
		line = sales.build_line(row(qty=2, price_list_rate=100, rate=90, net_amount=180), 28.8, False)
		self.assertEqual(line["unit_price"], 116)
		self.assertEqual(line["total_amount"], 208.8)
		self.assertEqual(line["discount_amount"], 23.2)
		assert_line_math(self, line)

	def test_inclusive_tax_without_discount(self):
		line = sales.build_line(row(qty=2, price_list_rate=116, rate=116, net_amount=200), 32, True)
		self.assertEqual((line["unit_price"], line["discount_amount"], line["total_amount"]), (116, 0, 232))

	def test_header_discount_becomes_line_discount(self):
		# 100 less a 10% invoice discount spread into the net amount, plus 16% VAT
		line = sales.build_line(row(qty=1, price_list_rate=100, rate=100, net_amount=90), 14.4, False)
		self.assertEqual(
			(line["unit_price"], line["discount_amount"], line["total_amount"]), (116, 11.6, 104.4)
		)

	def test_sold_above_list_price_has_no_discount(self):
		line = sales.build_line(row(qty=3, price_list_rate=50, rate=60, net_amount=180), 0, False)
		self.assertEqual((line["unit_price"], line["discount_amount"], line["total_amount"]), (60, 0, 180))

	def test_quantities_are_in_stock_units(self):
		# 2 boxes of 12 at 1200 a box
		line = sales.build_line(
			row(qty=2, stock_qty=24, conversion_factor=12, price_list_rate=1200, rate=1200, net_amount=2400),
			384,
			False,
		)
		self.assertEqual((line["quantity"], line["unit_price"], line["total_amount"]), (24, 116, 2784))

	def test_rounding_keeps_the_total_exact(self):
		line = sales.build_line(row(qty=3, price_list_rate=33.33333, rate=30, net_amount=90), 14.4, False)
		assert_line_math(self, line)
		self.assertGreaterEqual(line["discount_amount"], 0)

	def test_returns_are_positive(self):
		line = sales.build_line(
			row(qty=-2, stock_qty=-2, price_list_rate=100, rate=100, net_amount=-200), -32, False
		)
		self.assertEqual((line["quantity"], line["total_amount"]), (2, 232))

	def test_zero_lines_are_left_out(self):
		self.assertIsNone(sales.build_line(row(qty=1, rate=0, net_amount=0), 0, False))

	def test_codes_and_phone(self):
		self.assertEqual(sales.get_code("06 - Mobile Money"), "06")
		self.assertEqual(sales.get_code(None), "")
		self.assertEqual(sales.get_local_phone("+260 971 234 567"), "0971234567")
		self.assertEqual(sales.get_local_phone("0971234567"), "0971234567")
		self.assertEqual(sales.get_local_phone("12345"), "")


class SalesTestCase(IntegrationTestCase):
	def setUp(self):
		settings = frappe.get_all(
			SETTINGS_DOCTYPE_NAME, filters={"is_active": 1}, fields=["name", "company_name"], limit=1
		)
		if not settings:
			self.skipTest("No active ZRA SIS Settings on this site")
		self.settings = settings[0].name
		self.company = settings[0].company_name
		self.item = frappe.db.get_value(
			"Item",
			{
				"custom_smart_status": "COMPLETED",
				"custom_sis_settings": self.settings,
				"custom_smart_tax_type_code": "A",
			},
			["name", "custom_smart_remote_id"],
			as_dict=True,
		)
		self.customer = frappe.db.get_value(
			"Customer",
			{"custom_sis_customer_id": ["is", "set"], "custom_is_lpo": 0},
			["name", "custom_sis_customer_id"],
			as_dict=True,
		)
		if not self.item or not self.customer:
			self.skipTest("Needs a COMPLETED item in VAT category A and a registered customer")
		frappe.db.set_value("Customer", self.customer.name, "custom_sis_settings", self.settings)
		frappe.db.set_value(
			SETTINGS_DOCTYPE_NAME, self.settings, {"sales_auto_submission_enabled": 1, "go_live_date": None}
		)
		self.tax_template = frappe.db.get_value(
			"Sales Taxes and Charges Template", {"company": self.company}, "name"
		)

		self.patches = [
			patch.object(frappe.db, "commit"),
			patch("frappe.enqueue"),
			patch(
				"zambia_compliance_via_digitax.zambia_compliance_via_digitax.services.stock.process_stock_queue"
			),
		]
		for p in self.patches:
			p.start()

	def tearDown(self):
		for p in self.patches:
			p.stop()
		frappe.db.rollback()

	def get_settings(self):
		return sales.get_sales_settings(self.company)

	def invoice(self, insert=True, **values):
		doc = frappe.get_doc(
			{
				"doctype": "Sales Invoice",
				"company": self.company,
				"customer": self.customer.name,
				"posting_date": today(),
				"set_posting_time": 1,
				"items": [{"item_code": self.item.name, "qty": 2, "rate": 100}],
				"taxes_and_charges": self.tax_template,
				**values,
			}
		)
		doc.set_missing_values()
		if self.tax_template and not doc.taxes:
			doc.set_taxes()
		doc.calculate_taxes_and_totals()
		if insert:
			doc.insert()
		return doc

	def completed_entry(self, invoice, sale_id="sale_TEST0000000000000000000001"):
		"""A Completed ledger entry for an invoice, as if DigiTax had accepted it."""
		prepared = sales.build_payload(invoice, self.get_settings())
		entry = frappe.get_doc(
			{
				"doctype": SALES_LEDGER_DOCTYPE_NAME,
				"trader_invoice_number": invoice.name,
				"document_key": sales.get_document_key("Sales Invoice", invoice.name),
				"reference_doctype": "Sales Invoice",
				"reference_name": invoice.name,
				"receipt_type": sales.SALE,
				"kind": prepared.kind,
				"status": sales.STATUS_COMPLETED_ZRA,
				"sale_id": sale_id,
				"items": prepared.lines,
			}
		)
		entry.flags.from_sales_service = True
		entry.insert(ignore_permissions=True)
		return entry


class TestSalePayload(SalesTestCase):
	def test_normal_sale(self):
		invoice = self.invoice()
		prepared = sales.build_payload(invoice, self.get_settings())
		self.assertEqual(prepared.problems, [])
		payload = prepared.payload
		self.assertEqual(payload["kind"], "NORMAL")
		self.assertEqual(payload["trader_invoice_number"], invoice.name)
		self.assertEqual(payload["customer_id"], self.customer.custom_sis_customer_id)
		self.assertNotIn("customer_tpin", payload)
		self.assertNotIn("exchange_rate", payload)
		self.assertEqual(payload["payment_type_code"], sales.PAYMENT_CREDIT)  # unpaid
		[line] = payload["items"]
		self.assertEqual(line["item_id"], self.item.custom_smart_remote_id)
		assert_line_math(self, line)
		self.assertAlmostEqual(prepared.erpnext_total, invoice.grand_total - invoice.rounding_adjustment, 2)

	def test_unregistered_item_is_a_problem_not_an_error(self):
		other = frappe.db.get_value(
			"Item", {"custom_smart_remote_id": ["is", "not set"], "is_sales_item": 1}, "name"
		)
		if not other:
			self.skipTest("No unregistered item")
		invoice = self.invoice(items=[{"item_code": other, "qty": 1, "rate": 10}])
		prepared = sales.build_payload(invoice, self.get_settings())
		self.assertEqual(len(prepared.problems), 1)
		self.assertIn(other, prepared.problems[0])

	def test_old_sale_date_is_a_problem(self):
		invoice = self.invoice(insert=False, posting_date=add_days(today(), -40))
		prepared = sales.build_payload(invoice, self.get_settings())
		self.assertTrue(any("40 days" in p for p in prepared.problems))

	def test_lpo_needs_an_lpo_customer_and_c2_items(self):
		invoice = self.invoice(insert=False, custom_kind_of_sale="LPO")
		problems = sales.build_payload(invoice, self.get_settings()).problems
		self.assertEqual(len(problems), 3)  # purchase order number, LPO customer, C2 item

	def test_export_needs_a_destination(self):
		invoice = self.invoice(insert=False, custom_kind_of_sale="EXPORT")
		problems = sales.build_payload(invoice, self.get_settings()).problems
		self.assertTrue(any("Destination Country" in p for p in problems))

	def test_payment_type(self):
		invoice = self.invoice(insert=False, custom_sis_payment_type="06 - Mobile Money")
		self.assertEqual(sales.build_payload(invoice, self.get_settings()).payload["payment_type_code"], "06")

	def test_eligibility(self):
		settings = self.get_settings()
		invoice = self.invoice(insert=False)
		invoice.docstatus = 1
		self.assertTrue(sales.is_eligible(invoice, settings))
		invoice.custom_prevent_sis_submission = 1
		self.assertFalse(sales.is_eligible(invoice, settings))
		invoice.custom_prevent_sis_submission = 0
		settings.go_live_date = add_days(today(), 1)
		self.assertFalse(sales.is_eligible(invoice, settings))
		self.assertFalse(sales.is_eligible(invoice, None))


class TestNotePayload(SalesTestCase):
	def return_of(self, original, qty=-1, insert=True, **values):
		doc = frappe.get_doc(
			{
				"doctype": "Sales Invoice",
				"company": self.company,
				"customer": original.customer,
				"is_return": 1,
				"return_against": original.name,
				"posting_date": today(),
				"set_posting_time": 1,
				"items": [{"item_code": self.item.name, "qty": qty, "rate": 100}],
				"taxes_and_charges": original.taxes_and_charges,
				**values,
			}
		)
		doc.set_missing_values()
		if original.taxes_and_charges and not doc.taxes:
			doc.set_taxes()
		doc.calculate_taxes_and_totals()
		if insert:
			doc.insert()
		return doc

	def submitted_original(self):
		original = self.invoice()
		original.db_set("docstatus", 1)
		return original

	def test_credit_note_waits_for_the_original(self):
		original = self.submitted_original()
		note = self.return_of(original)
		prepared = sales.build_payload(note, self.get_settings())
		self.assertEqual(prepared.receipt_type, sales.CREDIT_NOTE)
		self.assertTrue(any("Waiting for the original" in p for p in prepared.problems))

	def test_credit_note_payload(self):
		original = self.submitted_original()
		entry = self.completed_entry(original)
		note = self.return_of(original, custom_sis_refund_reason="03 - Damaged Goods")
		prepared = sales.build_payload(note, self.get_settings())
		self.assertEqual(prepared.problems, [])
		payload = prepared.payload
		self.assertEqual(payload["sale_id"], entry.sale_id)
		self.assertEqual(payload["refund_reason_code"], "03")
		self.assertEqual(payload["return_date"], today())
		[line] = payload["items"]
		self.assertEqual(line["quantity"], 1)
		self.assertIsInstance(line["quantity"], int)
		assert_line_math(self, line)
		self.assertEqual(prepared.original_entry, entry.name)

	def test_credit_note_cannot_exceed_the_sale(self):
		original = self.submitted_original()
		entry = self.completed_entry(original)
		first = self.return_of(original, qty=-2)
		first_prepared = sales.build_payload(first, self.get_settings())
		self.assertEqual(first_prepared.problems, [])
		# Record the first note as sent
		note_entry = frappe.get_doc(
			{
				"doctype": SALES_LEDGER_DOCTYPE_NAME,
				"trader_invoice_number": first.name,
				"reference_doctype": "Sales Invoice",
				"reference_name": first.name,
				"receipt_type": sales.CREDIT_NOTE,
				"original_entry": entry.name,
				"status": sales.STATUS_PENDING_ZRA,
				"items": first_prepared.lines,
			}
		)
		note_entry.flags.from_sales_service = True
		note_entry.insert(ignore_permissions=True)

		second = self.return_of(original, qty=-1)
		problems = sales.build_payload(second, self.get_settings()).problems
		self.assertTrue(any("only 2 were sold" in p for p in problems))

	def test_fractional_quantities_are_a_problem(self):
		original = self.submitted_original()
		self.completed_entry(original)
		note = self.return_of(original, qty=-0.5, insert=False)  # ERPNext refuses fractions of Nos
		problems = sales.build_payload(note, self.get_settings()).problems
		self.assertTrue(any("whole quantities" in p for p in problems))


class TestLedgerIsReadOnly(SalesTestCase):
	def test_entries_cannot_be_edited_or_deleted(self):
		entry = frappe.get_doc(SALES_LEDGER_DOCTYPE_NAME, self.completed_entry(self.invoice()).name)
		entry.receipt_number = "123"
		self.assertRaises(frappe.ValidationError, entry.save, ignore_permissions=True)
		self.assertRaises(
			frappe.ValidationError, frappe.delete_doc, SALES_LEDGER_DOCTYPE_NAME, entry.name, force=True
		)


class SendingTestCase(SalesTestCase):
	def sale(self, invoice, status="pending", **values):
		data = {
			"id": SALE_ID,
			"trader_invoice_number": invoice.name,
			"sale_number": "SAL26-TEST",
			"receipt_type_code": "S",
			"status": status,
			"item_list": [],
		}
		if status == "completed":
			data.update(
				{
					"receipt_number": 77,
					"sdc_id": "SDC-1",
					"internal_data": "INTERNAL",
					"receipt_signature": "SIGNATURE",
					"receipt_url": "https://sandbox.zra.org.zm/receipt/77",
					"sales_tax_summary": {"taxable_amount_vat": 200, "tax_amount_vat": 32},
					"item_list": [
						{
							"item_id": self.item.custom_smart_remote_id,
							"total_amount": 232,
							"vat_taxable_amount": 200,
							"vat_tax_amount": 32,
						}
					],
				}
			)
		data.update(values)
		return data

	def submitted(self, **values):
		invoice = self.invoice(**values)
		invoice.submit()
		return invoice

	def entry(self, invoice):
		return frappe.get_doc(
			SALES_LEDGER_DOCTYPE_NAME,
			frappe.db.get_value("Sales Invoice", invoice.name, "custom_sis_ledger_entry"),
		)

	def run_queue(self, routes):
		fake = FakeDigitax(routes)
		with patch(REQUEST, side_effect=fake):
			sales.process_sales_queue()
		return fake


class TestSending(SendingTestCase):
	def test_submit_with_problems_is_not_blocked(self):
		other = frappe.db.get_value(
			"Item", {"custom_smart_remote_id": ["is", "not set"], "is_sales_item": 1}, "name"
		)
		if not other:
			self.skipTest("No unregistered item")
		invoice = self.submitted(items=[{"item_code": other, "qty": 1, "rate": 10}])
		self.assertEqual(invoice.docstatus, 1)
		entry = self.entry(invoice)
		self.assertEqual(entry.status, sales.STATUS_NOT_READY)
		self.assertIn(other, entry.problems)
		self.assertEqual(frappe.db.get_value("Sales Invoice", invoice.name, "custom_sis_status"), "Not Ready")

	def test_sale_is_sent_once_and_completed(self):
		invoice = self.submitted()
		entry = self.entry(invoice)
		self.assertEqual(entry.status, sales.STATUS_QUEUED)
		self.assertTrue(
			[c for c in frappe.enqueue.call_args_list if c.args and c.args[0] == sales.PROCESS_JOB]
		)

		fake = self.run_queue({("POST", "/sales"): response(201, self.sale(invoice))})
		self.run_queue({})  # a second run sends nothing
		[body] = fake.sent("POST", "/sales")
		self.assertEqual(body["trader_invoice_number"], invoice.name)
		self.assertIn(f"entry={entry.name}&token=", body["callback_url"])
		entry.reload()
		self.assertEqual(
			(entry.status, entry.sale_id, entry.attempts), (sales.STATUS_PENDING_ZRA, SALE_ID, 1)
		)
		self.assertTrue(entry.payload_hash)
		self.assertTrue([c for c in frappe.enqueue.call_args_list if c.args and c.args[0] == sales.CHECK_JOB])

		fake = FakeDigitax({("GET", f"/sales/{SALE_ID}"): response(200, self.sale(invoice, "completed"))})
		with patch(REQUEST, side_effect=fake):
			sales.check_sale_job(entry.name)
		entry.reload()
		self.assertEqual(entry.status, sales.STATUS_COMPLETED_ZRA)
		self.assertEqual((entry.receipt_number, entry.sdc_id, entry.zra_tax), ("77", "SDC-1", 32))
		self.assertTrue(entry.qr_code)
		self.assertEqual(entry.items[0].vat_tax_amount, 32)
		invoice.reload()
		self.assertEqual(invoice.custom_sis_status, "Completed")
		self.assertEqual(
			(invoice.custom_receipt_number, invoice.custom_sdc_id, invoice.custom_successfully_submitted),
			("77", "SDC-1", 1),
		)
		self.assertEqual(invoice.items[0].custom_vat_tax_amount, 32)

	def test_timeout_is_looked_up_before_resending(self):
		invoice = self.submitted()
		with patch(REQUEST, side_effect=requests.exceptions.Timeout()):
			sales.process_sales_queue()
		entry = self.entry(invoice)
		self.assertEqual((entry.status, entry.error_class), (sales.STATUS_UNKNOWN, sales.TRANSIENT))

		sales.requeue_due_entries()
		fake = self.run_queue(
			{("GET", "/sales"): response(200, {"data": [self.sale(invoice, "completed")], "pagination": {}})}
		)
		self.assertEqual(fake.sent("POST", "/sales"), [])  # found it, so not sent again
		self.assertEqual(self.entry(invoice).status, sales.STATUS_COMPLETED_ZRA)

	def test_duplicate_answer_finds_the_sale(self):
		invoice = self.submitted()
		fake = self.run_queue(
			{
				("POST", "/sales"): response(
					400, {"message": "trader_invoice_number already exists", "code": "400"}
				),
				("GET", "/sales"): response(200, {"data": [self.sale(invoice)], "pagination": {}}),
			}
		)
		self.assertEqual(len(fake.sent("POST", "/sales")), 1)
		self.assertEqual(self.entry(invoice).status, sales.STATUS_PENDING_ZRA)

	def test_rejected_sale_is_retried_with_the_same_number(self):
		invoice = self.submitted()
		self.run_queue(
			{("POST", "/sales"): response(400, {"message": "quantity exceeds stock", "code": "400"})}
		)
		entry = self.entry(invoice)
		self.assertEqual((entry.status, entry.error_class), (sales.STATUS_REJECTED, sales.REJECTED))
		self.assertIn("quantity exceeds stock", entry.last_error)

		self.assertTrue(sales.send_or_retry(invoice.name)["ok"])
		fake = self.run_queue({("POST", "/sales"): response(201, self.sale(invoice))})
		[body] = fake.sent("POST", "/sales")
		self.assertEqual(body["trader_invoice_number"], invoice.name)
		self.assertEqual(self.entry(invoice).attempts, 2)

	def test_failed_sale_is_retried_under_a_new_number(self):
		invoice = self.submitted()
		self.run_queue({("POST", "/sales"): response(201, self.sale(invoice, "failed"))})
		failed = self.entry(invoice)
		self.assertEqual(failed.status, sales.STATUS_FAILED)

		sales.send_or_retry(invoice.name)
		retry = self.entry(invoice)
		self.assertEqual(retry.trader_invoice_number, f"{invoice.name}-1")
		self.assertEqual(retry.previous_entry, failed.name)
		self.assertEqual(
			frappe.db.get_value(SALES_LEDGER_DOCTYPE_NAME, failed.name, "status"), sales.STATUS_SUPERSEDED
		)
		fake = self.run_queue(
			{
				("POST", "/sales"): response(
					201,
					self.sale(
						invoice, id="sale_TEST0000000000000000000010", trader_invoice_number=retry.name
					),
				)
			}
		)
		self.assertEqual(fake.sent("POST", "/sales")[0]["trader_invoice_number"], f"{invoice.name}-1")

	def test_callback_needs_the_token(self):
		invoice = self.submitted()
		entry = self.entry(invoice)
		frappe.enqueue.reset_mock()
		sales_api.sale_callback(entry=entry.name, token="wrong")
		self.assertFalse(frappe.enqueue.called)
		sales_api.sale_callback(entry=entry.name, token=entry.callback_token, id="anything")
		self.assertEqual(frappe.enqueue.call_args.args[0], sales.CHECK_JOB)
		self.assertEqual(frappe.enqueue.call_args.kwargs["entry"], entry.name)

	def test_cancel_before_sending_but_not_after(self):
		queued = self.submitted()
		queued.cancel()
		self.assertEqual(self.entry(queued).status, sales.STATUS_CANCELLED)
		self.assertFalse(self.entry(queued).document_key)

		sent = self.submitted()
		self.run_queue({("POST", "/sales"): response(201, self.sale(sent))})
		self.assertRaises(frappe.ValidationError, sent.cancel)


class TestSendingNotes(SendingTestCase):
	def note(self, original, **values):
		doc = frappe.get_doc(
			{
				"doctype": "Sales Invoice",
				"company": self.company,
				"customer": original.customer,
				"is_return": 1,
				"return_against": original.name,
				"posting_date": today(),
				"set_posting_time": 1,
				"items": [{"item_code": self.item.name, "qty": -1, "rate": 100}],
				"taxes_and_charges": original.taxes_and_charges,
				**values,
			}
		)
		doc.set_missing_values()
		if original.taxes_and_charges and not doc.taxes:
			doc.set_taxes()
		doc.calculate_taxes_and_totals()
		doc.insert()
		doc.submit()
		return doc

	def complete(self, invoice):
		self.run_queue({("POST", "/sales"): response(201, self.sale(invoice, "completed"))})
		self.assertEqual(self.entry(invoice).status, sales.STATUS_COMPLETED_ZRA)

	def test_credit_note_waits_then_is_sent(self):
		original = self.submitted()
		note = self.note(original, custom_sis_refund_reason="01 - Wrong Product")
		entry = self.entry(note)
		self.assertEqual((entry.status, entry.receipt_type), (sales.STATUS_NOT_READY, sales.CREDIT_NOTE))

		self.complete(original)  # completing the original queues a re-check of waiting notes
		self.assertTrue(
			[c for c in frappe.enqueue.call_args_list if c.args and c.args[0] == sales.RECHECK_JOB]
		)
		sales.recheck_not_ready_entries()
		entry.reload()
		self.assertEqual(entry.status, sales.STATUS_QUEUED)
		self.assertEqual(entry.original_entry, self.entry(original).name)

		note_sale = self.sale(note, id="sale_TEST0000000000000000000011", receipt_type_code="R")
		fake = self.run_queue({("POST", "/credit-notes"): response(201, note_sale)})
		[body] = fake.sent("POST", "/credit-notes")
		self.assertEqual(body["sale_id"], SALE_ID)
		self.assertEqual(body["refund_reason_code"], "01")
		self.assertEqual(body["trader_invoice_number"], note.name)
		self.assertEqual(body["items"][0]["quantity"], 1)
		self.assertNotIn("kind", body)
		self.assertEqual(self.entry(note).status, sales.STATUS_PENDING_ZRA)

	def test_debit_note(self):
		original = self.submitted()
		self.complete(original)
		note = self.note(
			original,
			is_return=0,
			is_debit_note=1,
			custom_sis_debit_reason="02 - Wrong Invoice Amount",
			items=[{"item_code": self.item.name, "qty": 1, "rate": 10}],
		)
		entry = self.entry(note)
		self.assertEqual((entry.status, entry.receipt_type), (sales.STATUS_QUEUED, sales.DEBIT_NOTE))

		note_sale = self.sale(note, id="sale_TEST0000000000000000000012", receipt_type_code="D")
		fake = self.run_queue({("POST", "/debit-notes"): response(201, note_sale)})
		[body] = fake.sent("POST", "/debit-notes")
		self.assertEqual(
			(body["sale_id"], body["debit_reason_code"], body["debit_date"]), (SALE_ID, "02", today())
		)
		assert_line_math(self, body["items"][0])

	def test_note_is_looked_up_among_credit_notes(self):
		original = self.submitted()
		self.complete(original)
		note = self.note(original)
		with patch(REQUEST, side_effect=requests.exceptions.Timeout()):
			sales.process_sales_queue()
		sales.requeue_due_entries()
		note_sale = self.sale(note, id="sale_TEST0000000000000000000013", status="completed")
		fake = self.run_queue(
			{("GET", "/credit-notes"): response(200, {"data": [note_sale], "pagination": {}})}
		)
		self.assertEqual(fake.sent("POST", "/credit-notes"), [])
		self.assertEqual(self.entry(note).status, sales.STATUS_COMPLETED_ZRA)
