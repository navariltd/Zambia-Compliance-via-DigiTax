"""Sales, credit notes and debit notes to DigiTax Zambia (ZRA Smart Invoice), through the sales ledger.

DigiTax endpoints used (see fixtures/zra_sis_routes.json):
- saveSales       POST /v1/sales             NORMAL, LPO or EXPORT sale
- saveCreditNote  POST /v1/credit-notes      against the original sale_id
- saveDebitNote   POST /v1/debit-notes       against the original sale_id
- selectInvoice   GET  /v1/sales/{sale_id}   any of the above; status pending|failed|completed

The ZRA SIS Sales Ledger is the source of truth. Each entry is one fiscal document attempt: what was sent,
what DigiTax answered and the receipt. The invoice's ZRA fields are only ever copied from its entry.

Rules:
- Selling is never blocked. Submit always succeeds; an invoice that cannot be sent yet gets a Not Ready
  entry listing the problems, and is re-checked when they may be fixed, every hour, and on Retry.
- trader_invoice_number is unique on DigiTax across sales and notes, failed ones included. Sending the
  same number again after a timeout is therefore safe: it either creates the sale once or is refused as a
  duplicate. A sale ZRA marked failed uses its number up, so its retry is a new entry ending in -1, -2...
- Only the current entry of a document holds its document_key (unique), so a document has one live entry.
"""

from __future__ import annotations

import re

import frappe
from frappe import _
from frappe.utils import cint, date_diff, flt, getdate, nowdate

from ..doctype.doctype_names_mapping import SALES_LEDGER_DOCTYPE_NAME, SETTINGS_DOCTYPE_NAME
from . import customers as customer_service
from .common import is_valid_tpin, normalize_tpin
from .items import STATUS_COMPLETED, STATUS_PENDING, is_registered

LEDGER = SALES_LEDGER_DOCTYPE_NAME
SALES_INVOICE = "Sales Invoice"

STATUS_NOT_READY = "Not Ready"
STATUS_QUEUED = "Queued"
STATUS_SENDING = "Sending"
STATUS_PENDING_ZRA = "Pending"
STATUS_COMPLETED_ZRA = "Completed"
STATUS_REJECTED = "Rejected"
STATUS_UNKNOWN = "Unknown"
STATUS_FAILED = "Failed"
STATUS_SUPERSEDED = "Superseded"
STATUS_CANCELLED = "Cancelled"
# The invoice's status when it has no entry (not eligible, or before go-live).
STATUS_NOT_SENT = "Not Sent"

# Nothing reached DigiTax: the invoice can still be cancelled and the entry rebuilt.
UNSENT_STATUSES = (STATUS_NOT_READY, STATUS_QUEUED, STATUS_REJECTED)
# Entries that are, or may be, on DigiTax.
LIVE_STATUSES = (STATUS_QUEUED, STATUS_SENDING, STATUS_PENDING_ZRA, STATUS_COMPLETED_ZRA, STATUS_UNKNOWN)
FINAL_STATUSES = (STATUS_COMPLETED_ZRA, STATUS_SUPERSEDED, STATUS_CANCELLED)

SALE = "Sale"
CREDIT_NOTE = "Credit Note"
DEBIT_NOTE = "Debit Note"
SEND_ROUTES = {SALE: "saveSales", CREDIT_NOTE: "saveCreditNote", DEBIT_NOTE: "saveDebitNote"}

NORMAL = "NORMAL"
LPO = "LPO"
EXPORT = "EXPORT"
# VAT category items must have for these kinds (docs: Sales: Data Attributes, Types of Sales).
KIND_VAT_CATEGORY = {LPO: "C2", EXPORT: "C1"}

PAYMENT_CASH = "01"
PAYMENT_CREDIT = "02"
DEFAULT_REFUND_REASON = "07"  # Other
DEFAULT_DEBIT_REASON = "04"  # Other

ZMW = "ZMW"
# DigiTax refuses sale dates more than this many days from today (Africa/Lusaka).
SALE_DATE_WINDOW_DAYS = 29


# --------------------------------------------------------------------------- #
# Settings and eligibility
# --------------------------------------------------------------------------- #


def get_sales_settings(company: str) -> dict | None:
	"""Active settings for the company with "Send Sales Invoices to ZRA" on, or None."""
	if not company:
		return None
	settings = frappe.get_all(
		SETTINGS_DOCTYPE_NAME,
		filters={"company_name": company, "is_active": 1, "sales_auto_submission_enabled": 1},
		fields=["name", "go_live_date", "default_payment_type_code", "max_sales_submission_attempts"],
		limit=1,
	)
	return settings[0] if settings else None


def get_receipt_type(invoice) -> str:
	if invoice.get("is_debit_note"):
		return DEBIT_NOTE
	if invoice.get("is_return"):
		return CREDIT_NOTE
	return SALE


def is_eligible(invoice, settings: dict | None) -> bool:
	"""Whether a submitted invoice belongs in the ledger. Consolidated POS invoices are left out: their POS
	Invoices are the sales (POS Invoice is not supported yet)."""
	if not settings or invoice.docstatus != 1:
		return False
	if invoice.get("is_opening") == "Yes" or invoice.get("custom_prevent_sis_submission"):
		return False
	if invoice.get("is_consolidated"):
		return False
	return not (
		settings.get("go_live_date") and getdate(invoice.posting_date) < getdate(settings.go_live_date)
	)


def get_document_key(doctype: str, name: str) -> str:
	return f"{doctype}::{name}"


def get_current_entry(doctype: str, name: str, fields: list | str = "name"):
	"""The document's live entry (the one holding its document_key), or None."""
	return frappe.db.get_value(
		LEDGER, {"document_key": get_document_key(doctype, name)}, fields, as_dict=True
	)


# --------------------------------------------------------------------------- #
# Payload
# --------------------------------------------------------------------------- #


def get_code(value: str | None) -> str:
	"""Select options are "01 - Cash"; DigiTax wants "01"."""
	return (value or "").split(" ", 1)[0].strip()


def build_payload(invoice, settings: dict) -> frappe._dict:
	"""Build what to send for a submitted invoice and collect every reason it cannot be sent yet.

	Returns {"payload", "lines", "problems", "receipt_type", "kind", "original_entry", "reason_code",
	"erpnext_total", "erpnext_tax"}. Nothing here throws for data problems: they go in `problems`.
	"""
	receipt_type = get_receipt_type(invoice)
	result = frappe._dict(
		receipt_type=receipt_type,
		kind=None,
		original_entry=None,
		reason_code=None,
		problems=[],
		lines=[],
		payload={},
		erpnext_total=0.0,
		erpnext_tax=0.0,
	)

	original_lines = {}
	if receipt_type == SALE:
		build_sale_header(invoice, settings, result)
	else:
		original_lines = build_note_header(invoice, receipt_type, result)

	build_lines(invoice, settings.name, result, original_lines)
	return result


def build_sale_header(invoice, settings: dict, result: frappe._dict) -> None:
	problems = result.problems
	kind = invoice.get("custom_kind_of_sale") or NORMAL
	result.kind = kind

	payload = {
		"kind": kind,
		"sale_date": str(getdate(invoice.posting_date)),
		"currency_code": invoice.currency,
		"trader_invoice_number": invoice.name,
		"payment_type_code": get_payment_type_code(invoice, settings),
	}

	days = abs(date_diff(nowdate(), invoice.posting_date))
	if days > SALE_DATE_WINDOW_DAYS:
		problems.append(
			_(
				"The posting date is {0} days from today. DigiTax only accepts sales dated within {1} days, so this invoice cannot be sent; contact ZRA."
			).format(days, SALE_DATE_WINDOW_DAYS)
		)

	exchange_rate = get_exchange_rate(invoice)
	if exchange_rate:
		payload["exchange_rate"] = exchange_rate

	add_customer_fields(invoice, settings.name, kind, payload, problems)

	if kind == LPO:
		if invoice.get("po_no"):
			payload["lpo_number"] = invoice.po_no.strip()
		else:
			problems.append(
				_("LPO sales need the customer's purchase order number in Customer's Purchase Order.")
			)
	elif kind == EXPORT:
		country = invoice.get("custom_destination_country")
		if not country or country == "ZM":
			problems.append(_("Export sales need a Destination Country other than Zambia."))
		else:
			payload["destination_country_code"] = country

	result.payload = payload


def add_customer_fields(invoice, settings_name: str, kind: str, payload: dict, problems: list) -> None:
	"""customer_id when the customer is registered under these settings, else the TPIN (DigiTax finds or
	creates the customer by it), else the name only (walk-in). DigiTax ignores customers on exports."""
	customer = frappe.db.get_value(
		"Customer",
		invoice.customer,
		[
			"name",
			"customer_name",
			"tax_id",
			"mobile_no",
			"custom_sis_customer_id",
			"custom_sis_settings",
			"custom_is_lpo",
		],
		as_dict=True,
	)
	if not customer:
		return

	payload["customer_name"] = invoice.customer_name or customer.customer_name
	registered = customer_service.is_registered(customer) and customer.custom_sis_settings == settings_name

	if kind == LPO and not (registered and customer.custom_is_lpo):
		problems.append(
			_(
				"LPO sales need a customer registered with ZRA as an LPO customer. Register {0} with Is LPO ticked."
			).format(customer.name)
		)

	if registered:
		payload["customer_id"] = customer.custom_sis_customer_id
		return

	tpin = normalize_tpin(invoice.get("tax_id") or customer.tax_id)
	if tpin and kind != EXPORT:
		if is_valid_tpin(tpin):
			payload["customer_tpin"] = tpin
		else:
			problems.append(
				_("The customer's TPIN {0} is not 10 digits. Fix it on the customer or remove it.").format(
					tpin
				)
			)

	phone = get_local_phone(customer.mobile_no)
	if phone and "customer_tpin" in payload:
		payload["customer_phone"] = phone


def get_local_phone(phone: str | None) -> str:
	"""POST /sales wants exactly 10 characters (0971234567), unlike POST /customers."""
	digits = re.sub(r"\D", "", phone or "")
	if len(digits) == 12 and digits.startswith("260"):
		digits = "0" + digits[3:]
	return digits if len(digits) == 10 and digits.startswith("0") else ""


def get_payment_type_code(invoice, settings: dict) -> str:
	if invoice.get("custom_sis_payment_type"):
		return get_code(invoice.custom_sis_payment_type)
	if invoice.get("is_pos"):
		for payment in invoice.get("payments") or []:
			if flt(payment.amount) and payment.mode_of_payment:
				code = frappe.get_cached_value(
					"Mode of Payment", payment.mode_of_payment, "custom_sis_payment_type_code"
				)
				if code:
					return get_code(code)
	if flt(invoice.outstanding_amount) > 0:
		return PAYMENT_CREDIT
	return get_code(settings.get("default_payment_type_code")) or PAYMENT_CASH


def get_exchange_rate(invoice) -> float | None:
	"""Rate from the invoice currency to ZMW, or None for ZMW invoices. When no rate is known it is left out
	and DigiTax's answer says so, rather than holding the sale back here."""
	if invoice.currency == ZMW:
		return None
	company_currency = frappe.get_cached_value("Company", invoice.company, "default_currency")
	if company_currency == ZMW:
		return flt(invoice.conversion_rate) or None
	rate = frappe.db.get_value(
		"Currency Exchange",
		{"from_currency": invoice.currency, "to_currency": ZMW, "date": ["<=", invoice.posting_date]},
		"exchange_rate",
		order_by="date desc",
	)
	return flt(rate) or None


def build_note_header(invoice, receipt_type: str, result: frappe._dict) -> dict:
	"""Header of a credit or debit note. Returns the original entry's lines by item code, so lines use the
	same DigiTax item IDs as the sale."""
	problems = result.problems
	if receipt_type == CREDIT_NOTE:
		result.reason_code = get_code(invoice.get("custom_sis_refund_reason")) or DEFAULT_REFUND_REASON
		payload = {
			"return_date": str(getdate(invoice.posting_date)),
			"refund_reason_code": result.reason_code,
		}
	else:
		result.reason_code = get_code(invoice.get("custom_sis_debit_reason")) or DEFAULT_DEBIT_REASON
		payload = {"debit_date": str(getdate(invoice.posting_date)), "debit_reason_code": result.reason_code}
	payload["trader_invoice_number"] = invoice.name
	result.payload = payload

	if not invoice.get("return_against"):
		problems.append(_("Set Return Against to the original invoice; ZRA notes must refer to a sale."))
		return {}

	original = get_current_entry(SALES_INVOICE, invoice.return_against, ["name", "status", "sale_id", "kind"])
	if not original or original.status != STATUS_COMPLETED_ZRA or not original.sale_id:
		problems.append(
			_(
				"Waiting for the original invoice {0} to be fiscalised on ZRA. This note is sent once it is."
			).format(invoice.return_against)
		)
		return {}

	result.original_entry = original.name
	result.kind = original.kind
	payload["sale_id"] = original.sale_id

	original_lines = {}
	for line in frappe.get_all(
		"ZRA SIS Sales Ledger Item",
		filters={"parent": original.name, "parenttype": LEDGER},
		fields=["item_code", "item_id", "quantity"],
		order_by="idx asc",
	):
		entry = original_lines.setdefault(line.item_code, frappe._dict(item_id=line.item_id, quantity=0))
		entry.quantity += flt(line.quantity)
	return original_lines


def get_line_taxes(invoice) -> dict:
	"""Tax per invoice row in the invoice currency, from v16's item_wise_tax_details (company currency)."""
	conversion_rate = flt(invoice.conversion_rate) or 1
	tax_rows = {tax.name: tax for tax in invoice.get("taxes") or []}
	line_tax = {}
	for detail in invoice.get("item_wise_tax_details") or []:
		tax = tax_rows.get(detail.tax_row)
		if tax and tax.get("category") != "Valuation":
			sign = -1 if tax.get("add_deduct_tax") == "Deduct" else 1
			line_tax[detail.item_row] = (
				line_tax.get(detail.item_row, 0) + sign * flt(detail.amount) / conversion_rate
			)
	return line_tax


def build_line(row, line_tax: float, tax_inclusive: bool) -> dict | None:
	"""One DigiTax line, in the invoice currency, per stock unit (DigiTax counts stock in the item's unit).

	ZRA wants the tax-inclusive price before discounts and the line's discount, with
	total_amount == quantity * unit_price - discount_amount to 4 decimals. The total is ERPNext's net
	amount plus the line's tax, so the receipt matches the invoice; the header discount is already spread
	into the net amount by ERPNext, so it ends up in the line discount. Returns None for zero lines.
	"""
	qty = abs(flt(row.stock_qty) or flt(row.qty))
	total = abs(flt(row.net_amount) + line_tax)
	if not qty or not total:
		return None

	factor = flt(row.conversion_factor) or 1
	list_price = max(flt(row.price_list_rate), flt(row.rate)) / factor
	if not tax_inclusive and flt(row.net_amount):
		list_price *= total / abs(flt(row.net_amount))  # add the line's tax rate

	unit_price = round(list_price, 4)
	total_amount = round(total, 4)
	discount_amount = round(qty * unit_price - total_amount, 4)
	if discount_amount <= 0:
		# Sold above the list price (or rounding): no discount, price = total / qty.
		discount_amount = 0
		unit_price = round(total / qty, 4)
		total_amount = round(qty * unit_price, 4)

	return {
		"quantity": round(qty, 4),
		"unit_price": unit_price,
		"total_amount": total_amount,
		"package_unit_quantity": round(abs(flt(row.get("package_qty"))) or 1, 4),
		"discount_rate": 0,
		"discount_amount": discount_amount,
	}


def build_lines(invoice, settings_name: str, result: frappe._dict, original_lines: dict) -> None:
	problems = result.problems
	is_note = result.receipt_type != SALE
	required_category = KIND_VAT_CATEGORY.get(result.kind) if not is_note else None
	tax_inclusive = any(tax.get("included_in_print_rate") for tax in invoice.get("taxes") or [])
	line_taxes = get_line_taxes(invoice)
	already_credited = (
		get_credited_quantities(result.original_entry, invoice.name)
		if result.receipt_type == CREDIT_NOTE
		else {}
	)

	for row in invoice.items:
		item = frappe.db.get_value(
			"Item",
			row.item_code,
			[
				"name",
				"custom_smart_remote_id",
				"custom_smart_status",
				"custom_sis_settings",
				"custom_smart_tax_type_code",
			],
			as_dict=True,
		)
		if not item or not is_registered(item) or item.custom_sis_settings != settings_name:
			problems.append(_("Row {0}: item {1} is not registered with ZRA.").format(row.idx, row.item_code))
			continue
		if item.custom_smart_status == STATUS_PENDING:
			problems.append(
				_(
					"Row {0}: item {1} is still PENDING on ZRA. The invoice is sent once ZRA completes it."
				).format(row.idx, row.item_code)
			)
			continue
		if item.custom_smart_status != STATUS_COMPLETED:
			problems.append(_("Row {0}: item {1} is not COMPLETED on ZRA.").format(row.idx, row.item_code))
			continue
		if required_category and item.custom_smart_tax_type_code != required_category:
			problems.append(
				_("Row {0}: {1} sales need items in VAT category {2}; item {3} is {4}.").format(
					row.idx,
					result.kind,
					required_category,
					row.item_code,
					item.custom_smart_tax_type_code or "-",
				)
			)

		line_tax = line_taxes.get(row.name, 0)
		line = build_line(row, line_tax, tax_inclusive)
		if not line:
			continue  # DigiTax refuses zero amounts; free lines are left off the receipt

		item_id = item.custom_smart_remote_id
		if is_note and original_lines:
			original = original_lines.get(row.item_code)
			if not original:
				problems.append(
					_("Row {0}: item {1} is not on the original sale.").format(row.idx, row.item_code)
				)
				continue
			item_id = original.item_id
			if line["quantity"] != cint(line["quantity"]):
				problems.append(
					_("Row {0}: ZRA notes take whole quantities only; {1} has {2}.").format(
						row.idx, row.item_code, f"{line['quantity']:g}"
					)
				)
			line["quantity"] = cint(line["quantity"])
			if result.receipt_type == CREDIT_NOTE:
				credited = already_credited.get(row.item_code, 0) + line["quantity"]
				already_credited[row.item_code] = credited
				if credited > original.quantity:
					problems.append(
						_("Row {0}: {1} would be credited {2} in total but only {3} were sold.").format(
							row.idx, row.item_code, f"{credited:g}", f"{original.quantity:g}"
						)
					)

		line["item_id"] = item_id
		result.payload.setdefault("items", []).append(line)
		result.lines.append({"invoice_row": row.name, "item_code": row.item_code, **line})
		result.erpnext_tax += abs(line_tax)
		result.erpnext_total += abs(flt(row.net_amount) + line_tax)

	result.payload.setdefault("items", [])
	if not result.lines and not problems:
		problems.append(_("There is nothing to send: every line has a zero amount."))
	result.erpnext_total = round(result.erpnext_total, 4)
	result.erpnext_tax = round(result.erpnext_tax, 4)


def get_credited_quantities(original_entry: str | None, exclude_document: str) -> dict:
	"""Quantity per item already credited (or on its way) against the original sale, by other notes."""
	if not original_entry:
		return {}
	entries = frappe.get_all(
		LEDGER,
		filters={
			"original_entry": original_entry,
			"receipt_type": CREDIT_NOTE,
			"status": ["in", LIVE_STATUSES],
			"reference_name": ["!=", exclude_document],
		},
		pluck="name",
	)
	if not entries:
		return {}
	credited = {}
	for line in frappe.get_all(
		"ZRA SIS Sales Ledger Item",
		filters={"parent": ["in", entries], "parenttype": LEDGER},
		fields=["item_code", "quantity"],
	):
		credited[line.item_code] = credited.get(line.item_code, 0) + flt(line.quantity)
	return credited
