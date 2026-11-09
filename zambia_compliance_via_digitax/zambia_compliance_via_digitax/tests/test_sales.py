"""Offline tests for services/sales.py. DigiTax is never called: requests.request, frappe.enqueue and
frappe.db.commit are mocked.

Needs a site with active ZRA SIS Settings, a registered (COMPLETED) item and a registered customer under it.
"""

from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import add_days, today

from ..doctype.doctype_names_mapping import SALES_LEDGER_DOCTYPE_NAME, SETTINGS_DOCTYPE_NAME
from ..services import sales


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

		self.patches = [patch.object(frappe.db, "commit"), patch("frappe.enqueue")]
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
