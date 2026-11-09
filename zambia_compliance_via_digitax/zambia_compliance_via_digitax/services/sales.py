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

import hashlib
import json
import re
import secrets
import time

import frappe
from frappe import _
from frappe.utils import (
	add_days,
	add_to_date,
	cint,
	date_diff,
	flt,
	get_datetime,
	getdate,
	now_datetime,
	nowdate,
)
from frappe.utils.synchronization import filelock

from ..digitax.client import DigitaxClient, DigitaxError
from ..doctype.doctype_names_mapping import SALES_LEDGER_DOCTYPE_NAME, SETTINGS_DOCTYPE_NAME
from ..utils.qr_utils import generate_and_attach_qr_code
from . import customers as customer_service
from .common import build_callback_url, enqueue_job, is_valid_tpin, normalize_tpin
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

LIST_ROUTES = {SALE: "fetchSales", CREDIT_NOTE: "fetchCreditNotes", DEBIT_NOTE: "fetchDebitNotes"}
# Pages of the newest records searched for a sale DigiTax says already exists (20 per page).
LOOKUP_PAGES = 10

# Error classes
TRANSIENT = "Transient"  # timeout, network, 429 or 5xx: the sale may exist; resending the same number is safe
REJECTED = "Rejected"  # DigiTax refused it (4xx); nothing was created
AUTH = "Auth"  # 401/403: the API key
DUPLICATE = "Duplicate"  # DigiTax already has the number but we could not find the sale

_METHOD = "zambia_compliance_via_digitax.zambia_compliance_via_digitax"
PROCESS_JOB = f"{_METHOD}.services.sales.process_sales_queue"
CHECK_JOB = f"{_METHOD}.services.sales.check_sale_job"
RECHECK_JOB = f"{_METHOD}.services.sales.recheck_not_ready_entries"
CALLBACK_METHOD = f"{_METHOD}.apis.sales.sale_callback"

LOCK_NAME = "zra_sis_sales"
LOCK_TIMEOUT = 300
MAX_PER_RUN = 200
# A Sending entry older than this was interrupted mid-call; DigiTax may or may not have it.
INTERRUPTED_AFTER_MINUTES = 10
# Status checks a job waits for after DigiTax accepts a sale; later ones are the hourly sweep's.
CHECK_DELAYS_SECONDS = (60, 300)
LATER_CHECK_MINUTES = 15
# Invoices without an entry that the hourly sweep still records.
SWEEP_LIMIT = 500


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


# --------------------------------------------------------------------------- #
# Recording invoices (Sales Invoice doc events)
# --------------------------------------------------------------------------- #

CANCELLABLE_STATUSES = (STATUS_NOT_READY, STATUS_QUEUED, STATUS_REJECTED, STATUS_FAILED)
TAXES = ("vat", "ipl", "tl", "excise", "tot")


def on_submit_sales_invoice(invoice) -> None:
	"""Record the invoice in the ledger, in the submit's transaction. Never raises: selling is never
	blocked, and the hourly sweep records any invoice this missed."""
	frappe.db.savepoint("zra_sis_sales_ledger")
	try:
		record_invoice(invoice)
	except Exception:
		frappe.db.rollback(save_point="zra_sis_sales_ledger")
		frappe.log_error(
			title=f"ZRA SIS: could not record {invoice.name} in the sales ledger",
			reference_doctype=SALES_INVOICE,
			reference_name=invoice.name,
		)


def before_cancel_sales_invoice(invoice) -> None:
	"""Cancel the entry if nothing reached ZRA; otherwise the sale stands and needs a credit note."""
	entry = frappe.db.get_value(
		LEDGER,
		{"document_key": get_document_key(SALES_INVOICE, invoice.name)},
		["name", "status"],
		as_dict=True,
		for_update=True,
	)
	if not entry:
		return
	if entry.status in CANCELLABLE_STATUSES:
		frappe.db.set_value(
			LEDGER,
			entry.name,
			{
				"status": STATUS_CANCELLED,
				"document_key": None,
				"next_attempt_at": None,
				"next_check_at": None,
			},
			update_modified=False,
		)
		mirror_to_invoice(entry.name, invoice)  # cancel() writes the in-memory invoice back
		return
	frappe.throw(
		_(
			"This invoice has been sent to ZRA (ledger entry {0}, status {1}). A ZRA sale cannot be cancelled; make a credit note (return) instead."
		).format(entry.name, _(entry.status)),
		title=_("Sent to ZRA"),
	)


def record_invoice(invoice, settings: dict | None = None) -> str | None:
	"""Create the invoice's entry if it belongs in the ledger. Returns the entry name."""
	settings = settings or get_sales_settings(invoice.company)
	if not is_eligible(invoice, settings):
		return None
	current = get_current_entry(SALES_INVOICE, invoice.name)
	if current:
		mirror_to_invoice(current.name, invoice)
		return current.name
	return create_entry(invoice, settings)


def create_entry(
	invoice, settings: dict, trader_invoice_number: str | None = None, previous_entry: str | None = None
) -> str:
	"""Insert a new current entry for the invoice and queue it if it is ready."""
	prepared = build_payload(invoice, settings)
	entry = frappe.get_doc(
		{
			"doctype": LEDGER,
			"trader_invoice_number": trader_invoice_number or invoice.name,
			"document_key": get_document_key(invoice.doctype, invoice.name),
			"reference_doctype": invoice.doctype,
			"reference_name": invoice.name,
			"company": invoice.company,
			"settings": settings.name,
			"posting_date": invoice.posting_date,
			"customer": invoice.customer,
			"currency": invoice.currency,
			"previous_entry": previous_entry,
			"callback_token": secrets.token_urlsafe(24),
		}
	)
	apply_prepared(entry, prepared)
	entry.flags.from_sales_service = True
	entry.insert(ignore_permissions=True)
	mirror_to_invoice(entry.name, invoice)
	if entry.status == STATUS_QUEUED:
		enqueue_queue()
	return entry.name


def apply_prepared(entry, prepared: frappe._dict) -> None:
	"""Copy a freshly built payload onto an entry that has not been sent yet."""
	entry.update(
		{
			"receipt_type": prepared.receipt_type,
			"kind": prepared.kind,
			"original_entry": prepared.original_entry,
			"original_sale_id": prepared.payload.get("sale_id"),
			"reason_code": prepared.reason_code,
			"exchange_rate": prepared.payload.get("exchange_rate"),
			"erpnext_total": prepared.erpnext_total,
			"erpnext_tax": prepared.erpnext_tax,
			"problems": format_problems(prepared.problems),
		}
	)
	entry.set("items", prepared.lines)
	if prepared.problems:
		entry.status = STATUS_NOT_READY
	else:
		entry.status = STATUS_QUEUED
		entry.queued_on = now_datetime()


def format_problems(problems: list) -> str | None:
	return "\n".join(f"• {p}" for p in problems) or None


def update_entry(name: str, status: str | None = None, **values) -> None:
	if status:
		values["status"] = status
	frappe.db.set_value(LEDGER, name, values, update_modified=False)
	mirror_to_invoice(name)


def save_entry(entry) -> None:
	entry.flags.from_sales_service = True
	entry.save(ignore_permissions=True)


# --------------------------------------------------------------------------- #
# Invoice mirror (ledger -> invoice, never the other way)
# --------------------------------------------------------------------------- #


def mirror_to_invoice(entry_name: str, invoice=None) -> None:
	"""Copy the entry's state onto its invoice. Pass `invoice` when the caller holds the document and will
	save it (submit, cancel), so the copy in memory does not overwrite these fields with stale values."""
	entry = frappe.get_doc(LEDGER, entry_name)
	if entry.reference_doctype != SALES_INVOICE:
		return
	if not entry.document_key and entry.status != STATUS_CANCELLED:
		return  # superseded: the newer entry speaks for the invoice

	completed = entry.status == STATUS_COMPLETED_ZRA
	values = {
		"custom_sis_ledger_entry": entry.name,
		"custom_sis_status": entry.status,
		"custom_successfully_submitted": int(completed),
		"custom_sent_to_digitax": int(
			entry.status not in (STATUS_NOT_READY, STATUS_QUEUED, STATUS_CANCELLED)
		),
		"custom_sales_id": entry.sale_id,
		"custom_sale_no": entry.sale_number,
		"custom_submission_status": entry.remote_status,
	}
	if completed:
		values.update(
			{
				"custom_receipt_number": entry.receipt_number,
				"custom_receipt_type_": entry.receipt_type_code,
				"custom_receipt_signature": entry.receipt_signature,
				"custom_internal_data": entry.internal_data,
				"custom_receipt_url": entry.receipt_url,
				"custom_sdc_id": entry.sdc_id,
				"custom_qr_code": entry.qr_code,
				"custom_sale_date": entry.posting_date,
			}
		)
		for tax in TAXES:
			values[f"custom_taxable_amount_{tax}"] = entry.get(f"taxable_amount_{tax}")
			values[f"custom_tax_amount_{tax}"] = entry.get(f"tax_amount_{tax}")
	frappe.db.set_value(SALES_INVOICE, entry.reference_name, values, update_modified=False)
	if invoice is not None:
		invoice.update(values)

	if completed:
		for line in entry.items:
			if line.invoice_row:
				frappe.db.set_value(
					"Sales Invoice Item",
					line.invoice_row,
					{
						**{
							f"custom_{tax}_taxable_amount": line.get(f"{tax}_taxable_amount") for tax in TAXES
						},
						**{f"custom_{tax}_tax_amount": line.get(f"{tax}_tax_amount") for tax in TAXES},
					},
					update_modified=False,
				)

	frappe.publish_realtime(
		"zra_sis_sales_status",
		{"name": entry.reference_name, "status": entry.status},
		doctype=SALES_INVOICE,
		docname=entry.reference_name,
		after_commit=True,
	)


# --------------------------------------------------------------------------- #
# Worker
# --------------------------------------------------------------------------- #


def enqueue_queue() -> None:
	enqueue_job(PROCESS_JOB, "queue", queue="long", timeout=3600)


def process_sales_queue() -> None:
	"""Send Queued entries one at a time, oldest sale first."""
	from .stock import process_stock_queue

	# A sale deducts stock on DigiTax, which refuses it when its stock is short: send pending movements first.
	process_stock_queue()
	mark_interrupted_entries()

	done = set()
	for _i in range(MAX_PER_RUN):
		name = frappe.db.get_value(
			LEDGER,
			{"status": STATUS_QUEUED, "name": ["not in", list(done) or [""]]},
			"name",
			order_by="posting_date asc, creation asc",
		)
		if not name:
			return
		done.add(name)
		process_entry(name)

	enqueue_queue()  # more left; continue in a fresh job


def mark_interrupted_entries() -> None:
	cutoff = add_to_date(now_datetime(), minutes=-INTERRUPTED_AFTER_MINUTES)
	for name in frappe.get_all(
		LEDGER, filters={"status": STATUS_SENDING, "sent_on": ["<", cutoff]}, pluck="name"
	):
		update_entry(
			name,
			STATUS_UNKNOWN,
			error_class=TRANSIENT,
			next_attempt_at=now_datetime(),
			last_error=_("Sending was interrupted.") + " " + may_exist_message(),
		)
	frappe.db.commit()


def may_exist_message() -> str:
	return _(
		"DigiTax may or may not have this sale. It is looked up and, if missing, sent again with the same number, which DigiTax never accepts twice."
	)


def process_entry(name: str) -> None:
	with filelock(LOCK_NAME, timeout=LOCK_TIMEOUT):
		entry = frappe.get_doc(LEDGER, name, for_update=True)
		if entry.status != STATUS_QUEUED:
			frappe.db.commit()
			return
		try:
			send_entry(entry)
		except Exception as e:
			frappe.db.rollback()
			message = str(e) or _("Unexpected error")
			if frappe.db.get_value(LEDGER, name, "status") == STATUS_SENDING:
				# It failed after the entry was committed as Sending, so the sale may be on DigiTax.
				update_entry(
					name,
					STATUS_UNKNOWN,
					error_class=TRANSIENT,
					last_error=f"{message} {may_exist_message()}",
					next_attempt_at=now_datetime(),
				)
			else:
				update_entry(name, STATUS_REJECTED, error_class=REJECTED, last_error=message)
			frappe.log_error(
				title=f"ZRA SIS: sending sale {name} failed", reference_doctype=LEDGER, reference_name=name
			)
		frappe.db.commit()


def send_entry(entry) -> None:
	"""Rebuild, check and send one Queued entry. Call under the sales lock."""
	invoice = frappe.get_doc(entry.reference_doctype, entry.reference_name)
	if invoice.docstatus != 1:
		update_entry(entry.name, STATUS_CANCELLED, document_key=None)
		return

	if entry.error_class in (TRANSIENT, DUPLICATE) and entry.attempts:
		# An earlier send may have reached DigiTax.
		try:
			found = find_remote_sale(entry)
		except DigitaxError as e:
			update_entry(entry.name, STATUS_UNKNOWN, last_error=e.message, next_attempt_at=now_datetime())
			return
		if found:
			apply_remote_sale(entry.name, found)
			return

	settings = get_sales_settings(invoice.company)
	if not settings or settings.name != entry.settings:
		update_entry(
			entry.name,
			STATUS_NOT_READY,
			problems=format_problems(
				[
					_("Send Sales Invoices to ZRA is off in ZRA SIS Settings for company {0}.").format(
						invoice.company
					)
				]
			),
		)
		return

	prepared = build_payload(invoice, settings)
	payload = prepared.payload
	payload["trader_invoice_number"] = entry.trader_invoice_number
	apply_prepared(entry, prepared)
	if prepared.problems:
		save_entry(entry)
		mirror_to_invoice(entry.name)
		return

	payload["callback_url"] = (
		f"{build_callback_url(CALLBACK_METHOD, settings.name)}?entry={entry.name}&token={entry.callback_token}"
	)
	entry.update(
		{
			"status": STATUS_SENDING,
			"request_payload": frappe.as_json(payload),
			"payload_hash": hashlib.sha256(
				json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
			).hexdigest(),
			"attempts": cint(entry.attempts) + 1,
			"sent_on": now_datetime(),
			"last_error": None,
			"next_attempt_at": None,
		}
	)
	save_entry(entry)
	mirror_to_invoice(entry.name)
	# Committed before the call: if this process dies mid-request, the entry is looked up before any resend.
	frappe.db.commit()

	try:
		data = DigitaxClient(settings.name).post(
			SEND_ROUTES[entry.receipt_type], payload, reference_doctype=LEDGER, reference_name=entry.name
		)
	except DigitaxError as e:
		handle_send_error(entry, e, settings)
		return

	if apply_remote_sale(entry.name, data or {}) == STATUS_PENDING_ZRA:
		schedule_status_check(entry.name)


def handle_send_error(entry, e: DigitaxError, settings: dict) -> None:
	code = e.status_code
	if code is None or code == 429 or code >= 500:
		update_entry(
			entry.name,
			STATUS_UNKNOWN,
			error_class=TRANSIENT,
			last_error=f"{e.message} {may_exist_message()}",
			next_attempt_at=now_datetime(),
		)
		return

	if is_duplicate_error(e):
		try:
			found = find_remote_sale(entry)
		except DigitaxError:
			found = None
		if found:
			apply_remote_sale(entry.name, found)
			return
		update_entry(
			entry.name,
			STATUS_UNKNOWN,
			error_class=DUPLICATE,
			last_error=_(
				"DigiTax already has trader invoice number {0} but it was not among its latest sales. Find the sale on the DigiTax dashboard and use Link Sale ID. ({1})"
			).format(entry.trader_invoice_number, e.message),
		)
		return

	retries = cint(settings.get("max_sales_submission_attempts"))
	auth = code in (401, 403)
	message = e.message
	if auth:
		message = _("DigiTax refused the API key ({0}). Check it in ZRA SIS Settings {1}.").format(
			e.message, settings.name
		)
	update_entry(
		entry.name,
		STATUS_REJECTED,
		error_class=AUTH if auth else REJECTED,
		last_error=message,
		next_attempt_at=now_datetime() if cint(entry.attempts) < retries else None,
	)


def is_duplicate_error(e: DigitaxError) -> bool:
	message = (e.message or "").lower()
	if "duplicate" in message:
		return True
	return "trader_invoice_number" in message and any(
		word in message for word in ("exist", "unique", "already", "used")
	)


def find_remote_sale(entry) -> dict | None:
	"""Look for the entry's trader_invoice_number among DigiTax's latest records of its kind."""
	client = DigitaxClient(entry.settings)
	for record in client.iterate(
		LIST_ROUTES[entry.receipt_type],
		max_pages=LOOKUP_PAGES,
		reference_doctype=LEDGER,
		reference_name=entry.name,
	):
		if record.get("trader_invoice_number") == entry.trader_invoice_number:
			return record
	return None


# --------------------------------------------------------------------------- #
# DigiTax results
# --------------------------------------------------------------------------- #


def apply_remote_sale(name: str, data: dict) -> str:
	"""Store DigiTax's record of the sale on the entry and mirror it. Returns the entry's new status."""
	remote_status = (data.get("status") or "").lower()
	values = {
		"sale_id": data.get("id"),
		"sale_number": data.get("sale_number"),
		"remote_status": remote_status,
		"receipt_type_code": data.get("receipt_type_code"),
		"remote_created_at": data.get("created_at"),
		"response_payload": frappe.as_json(data),
		"last_checked_on": now_datetime(),
		"last_error": None,
		"error_class": None,
		"next_attempt_at": None,
		"next_check_at": None,
	}

	if remote_status == "completed":
		status = STATUS_COMPLETED_ZRA
		summary = data.get("sales_tax_summary") or {}
		for tax in TAXES:
			values[f"taxable_amount_{tax}"] = flt(summary.get(f"taxable_amount_{tax}"))
			values[f"tax_amount_{tax}"] = flt(summary.get(f"tax_amount_{tax}"))
		zra_tax = round(sum(values[f"tax_amount_{tax}"] for tax in TAXES), 4)
		zra_total = sum(flt(line.get("total_amount")) for line in data.get("item_list") or [])
		values.update(
			{
				"receipt_number": str(data.get("receipt_number") or ""),
				"sdc_id": data.get("sdc_id"),
				"internal_data": data.get("internal_data"),
				"receipt_signature": data.get("receipt_signature"),
				"receipt_url": data.get("receipt_url"),
				"completed_on": now_datetime(),
				"zra_tax": zra_tax,
				"zra_total": round(zra_total - flt(data.get("cash_discount_amount")), 4),
				"tax_variance": round(zra_tax - flt(frappe.db.get_value(LEDGER, name, "erpnext_tax")), 4),
			}
		)
		if data.get("receipt_url"):
			values["qr_code"] = generate_and_attach_qr_code(data["receipt_url"], name, LEDGER)
	elif remote_status == "failed":
		status = STATUS_FAILED
		values["last_error"] = _(
			"ZRA marked this sale failed. DigiTax gives no reason; check the items and customer, then Retry, which sends it again under a new number."
		)
	else:
		status = STATUS_PENDING_ZRA
		values["next_check_at"] = add_to_date(now_datetime(), minutes=LATER_CHECK_MINUTES)

	values["status"] = status
	frappe.db.set_value(LEDGER, name, values, update_modified=False)
	if status == STATUS_COMPLETED_ZRA:
		apply_line_taxes(name, data.get("item_list") or [])
	mirror_to_invoice(name)
	if status == STATUS_COMPLETED_ZRA:
		enqueue_job(RECHECK_JOB, "all", queue="long")  # notes waiting for this sale
	return status


def apply_line_taxes(name: str, remote_lines: list) -> None:
	"""Per-line taxes from DigiTax's item_list, matched by position and checked by item ID."""
	unused = list(remote_lines)
	for idx, line in enumerate(
		frappe.get_all(
			"ZRA SIS Sales Ledger Item",
			filters={"parent": name, "parenttype": LEDGER},
			fields=["name", "item_id"],
			order_by="idx asc",
		)
	):
		remote = remote_lines[idx] if idx < len(remote_lines) else None
		if not remote or remote.get("item_id") != line.item_id or remote not in unused:
			remote = next((r for r in unused if r.get("item_id") == line.item_id), None)
		if not remote:
			continue
		unused.remove(remote)
		frappe.db.set_value(
			"ZRA SIS Sales Ledger Item",
			line.name,
			{
				**{f"{tax}_taxable_amount": flt(remote.get(f"{tax}_taxable_amount")) for tax in TAXES},
				**{f"{tax}_tax_amount": flt(remote.get(f"{tax}_tax_amount")) for tax in TAXES},
			},
			update_modified=False,
		)


def schedule_status_check(name: str, attempt: int = 0) -> None:
	"""DigiTax syncs with ZRA in the background. Check back shortly, in case its callback never reaches us
	(e.g. a localhost site); after CHECK_DELAYS_SECONDS the hourly sweep keeps checking."""
	if attempt >= len(CHECK_DELAYS_SECONDS):
		return
	delay = CHECK_DELAYS_SECONDS[attempt]
	enqueue_job(
		CHECK_JOB,
		f"{name}::{attempt}",
		queue="long",
		timeout=delay + 300,
		entry=name,
		check_after=str(add_to_date(now_datetime(), seconds=delay)),
		attempt=attempt,
	)


def check_sale_job(entry: str, check_after: str | None = None, attempt: int | None = None) -> None:
	"""Wait until `check_after`, then fetch the sale if it is still pending."""
	if check_after:
		wait = (get_datetime(check_after) - now_datetime()).total_seconds()
		if wait > 0:
			time.sleep(wait)
		frappe.db.rollback()  # fresh read: the callback may have settled it meanwhile

	if frappe.db.get_value(LEDGER, entry, "status") != STATUS_PENDING_ZRA:
		return
	try:
		result = check_entry_status(entry)
	except Exception:
		frappe.log_error(
			title=f"ZRA SIS: status check of {entry} failed", reference_doctype=LEDGER, reference_name=entry
		)
		return
	frappe.db.commit()
	if result.get("status") == STATUS_PENDING_ZRA and attempt is not None:
		schedule_status_check(entry, attempt + 1)


def check_entry_status(name: str) -> dict:
	"""GET /sales/{sale_id}. Returns {"ok", "status"} or {"ok": False, "message"}."""
	entry = frappe.db.get_value(LEDGER, name, ["name", "sale_id", "settings", "status"], as_dict=True)
	if not entry or not entry.sale_id:
		frappe.throw(_("Ledger entry {0} has no DigiTax sale ID yet.").format(name))
	try:
		data = DigitaxClient(entry.settings).get(
			"selectInvoice",
			path_params={"sale_id": entry.sale_id},
			reference_doctype=LEDGER,
			reference_name=name,
		)
	except DigitaxError as e:
		frappe.db.set_value(
			LEDGER, name, {"last_error": e.message, "last_checked_on": now_datetime()}, update_modified=False
		)
		return {"ok": False, "message": e.message}
	return {"ok": True, "status": apply_remote_sale(name, data or {})}


# --------------------------------------------------------------------------- #
# Manual actions
# --------------------------------------------------------------------------- #


def send_or_retry(invoice_name: str) -> dict:
	"""Send an invoice that has no entry, or retry the one it has. Returns {"ok", "status", "message"}."""
	invoice = frappe.get_doc(SALES_INVOICE, invoice_name)
	if invoice.docstatus != 1:
		frappe.throw(_("Submit the invoice first."))
	settings = get_sales_settings(invoice.company)
	if not settings:
		frappe.throw(
			_("Turn on Send Sales Invoices to ZRA in the ZRA SIS Settings of company {0} first.").format(
				invoice.company
			)
		)
	if not is_eligible(invoice, settings):
		frappe.throw(
			_(
				"This invoice is not sent to ZRA: it is an opening or consolidated invoice, has Prevent SIS Submission ticked, or is dated before the Go-Live Date."
			)
		)

	entry = get_current_entry(SALES_INVOICE, invoice.name, ["name", "status"])
	if not entry:
		name = create_entry(invoice, settings)
	elif entry.status == STATUS_FAILED:
		name = supersede(entry.name, invoice, settings)
	elif entry.status == STATUS_NOT_READY:
		name = entry.name
		recheck_entry(name)
	elif entry.status in (STATUS_REJECTED, STATUS_UNKNOWN, STATUS_QUEUED):
		name = entry.name
		update_entry(name, STATUS_QUEUED, queued_on=now_datetime(), next_attempt_at=None)
		enqueue_queue()
	else:
		return {
			"ok": False,
			"status": entry.status,
			"message": _("ZRA status is {0}; nothing to retry.").format(_(entry.status)),
		}

	status, problems = frappe.db.get_value(LEDGER, name, ["status", "problems"])
	return {"ok": status != STATUS_NOT_READY, "status": status, "entry": name, "message": problems}


def supersede(old_name: str, invoice, settings: dict) -> str:
	"""Replace a sale ZRA marked failed. Its number is used up on DigiTax, so the new entry gets the next one."""
	frappe.db.set_value(
		LEDGER, old_name, {"status": STATUS_SUPERSEDED, "document_key": None}, update_modified=False
	)
	count = frappe.db.count(LEDGER, {"reference_doctype": invoice.doctype, "reference_name": invoice.name})
	number = f"{invoice.name}-{count}"
	while frappe.db.exists(LEDGER, number):
		count += 1
		number = f"{invoice.name}-{count}"
	return create_entry(invoice, settings, trader_invoice_number=number, previous_entry=old_name)


def recheck_entry(name: str) -> str:
	"""Rebuild a Not Ready entry; queue it when nothing is in the way any more. Returns the status."""
	entry = frappe.get_doc(LEDGER, name)
	if entry.status != STATUS_NOT_READY:
		return entry.status
	invoice = frappe.get_doc(entry.reference_doctype, entry.reference_name)
	if invoice.docstatus != 1:
		update_entry(name, STATUS_CANCELLED, document_key=None)
		return STATUS_CANCELLED

	settings = get_sales_settings(invoice.company)
	if not settings or settings.name != entry.settings:
		return entry.status
	previous_problems = entry.problems
	apply_prepared(entry, build_payload(invoice, settings))
	if entry.status == STATUS_NOT_READY and entry.problems == previous_problems:
		return entry.status  # nothing changed; don't write a new version every hour
	save_entry(entry)
	mirror_to_invoice(name)
	if entry.status == STATUS_QUEUED:
		enqueue_queue()
	return entry.status


def recheck_not_ready_entries() -> None:
	"""Re-check every Not Ready entry, e.g. after an item or customer is registered or a sale completes."""
	for name in frappe.get_all(
		LEDGER, filters={"status": STATUS_NOT_READY}, pluck="name", order_by="creation asc", limit=SWEEP_LIMIT
	):
		try:
			recheck_entry(name)
			frappe.db.commit()
		except Exception:
			frappe.db.rollback()
			frappe.log_error(
				title=f"ZRA SIS: re-checking {name} failed", reference_doctype=LEDGER, reference_name=name
			)


def enqueue_recheck() -> None:
	"""Called when something an entry may wait for changes (an item completes, a customer registers)."""
	if frappe.db.exists(LEDGER, {"status": STATUS_NOT_READY}):
		enqueue_job(RECHECK_JOB, "all", queue="long")


def link_sale_id(invoice_name: str, sale_id: str) -> dict:
	"""Attach a sale found on the DigiTax dashboard to an entry whose send outcome is unknown."""
	entry = get_current_entry(
		SALES_INVOICE, invoice_name, ["name", "status", "settings", "trader_invoice_number"]
	)
	if not entry or entry.status != STATUS_UNKNOWN:
		frappe.throw(_("Only an invoice whose ZRA status is Unknown can be linked to a DigiTax sale."))
	sale_id = (sale_id or "").strip()
	if not sale_id.startswith("sale_"):
		frappe.throw(_("Enter the DigiTax sale ID. It starts with sale_."))
	other = frappe.db.get_value(LEDGER, {"sale_id": sale_id, "name": ["!=", entry.name]}, "name")
	if other:
		frappe.throw(_("DigiTax sale {0} already belongs to ledger entry {1}.").format(sale_id, other))

	try:
		data = DigitaxClient(entry.settings).get(
			"selectInvoice",
			path_params={"sale_id": sale_id},
			reference_doctype=LEDGER,
			reference_name=entry.name,
		)
	except DigitaxError as e:
		frappe.throw(e.message, title=_("DigiTax Error"))
	if (data or {}).get("trader_invoice_number") != entry.trader_invoice_number:
		frappe.throw(
			_("DigiTax sale {0} has trader invoice number {1}, not {2}.").format(
				sale_id, (data or {}).get("trader_invoice_number") or "-", entry.trader_invoice_number
			)
		)

	status = apply_remote_sale(entry.name, data)
	if status == STATUS_PENDING_ZRA:
		schedule_status_check(entry.name)
	return {"ok": True, "status": status}


# --------------------------------------------------------------------------- #
# Hourly sweep
# --------------------------------------------------------------------------- #


def sweep() -> None:
	"""Safety net: record invoices the submit missed, retry what is due, and check pending sales."""
	mark_interrupted_entries()
	for company in frappe.get_all(
		SETTINGS_DOCTYPE_NAME,
		filters={"is_active": 1, "sales_auto_submission_enabled": 1},
		pluck="company_name",
	):
		ensure_ledger_entries(company)
	requeue_due_entries()
	check_due_entries()
	recheck_not_ready_entries()
	if frappe.db.exists(LEDGER, {"status": STATUS_QUEUED}):
		enqueue_queue()


def ensure_ledger_entries(company: str) -> None:
	settings = get_sales_settings(company)
	if not settings:
		return
	since = add_days(nowdate(), -SALE_DATE_WINDOW_DAYS)
	if settings.go_live_date and getdate(settings.go_live_date) > getdate(since):
		since = settings.go_live_date
	for name in frappe.get_all(
		SALES_INVOICE,
		filters={
			"company": company,
			"docstatus": 1,
			"posting_date": [">=", since],
			"custom_sis_ledger_entry": ["is", "not set"],
			"is_opening": ["!=", "Yes"],
			"custom_prevent_sis_submission": 0,
			"is_consolidated": 0,
		},
		pluck="name",
		order_by="posting_date asc, creation asc",
		limit=SWEEP_LIMIT,
	):
		try:
			record_invoice(frappe.get_doc(SALES_INVOICE, name), settings)
			frappe.db.commit()
		except Exception:
			frappe.db.rollback()
			frappe.log_error(
				title=f"ZRA SIS: could not record {name} in the sales ledger",
				reference_doctype=SALES_INVOICE,
				reference_name=name,
			)


def requeue_due_entries() -> None:
	now = now_datetime()
	for name in frappe.get_all(
		LEDGER,
		filters={"status": ["in", [STATUS_UNKNOWN, STATUS_REJECTED]], "next_attempt_at": ["<=", now]},
		pluck="name",
		limit=SWEEP_LIMIT,
	):
		update_entry(name, STATUS_QUEUED, queued_on=now, next_attempt_at=None)
	frappe.db.commit()


def check_due_entries() -> None:
	for name in frappe.get_all(
		LEDGER,
		filters={"status": STATUS_PENDING_ZRA, "next_check_at": ["<=", now_datetime()]},
		pluck="name",
		limit=SWEEP_LIMIT,
	):
		try:
			check_entry_status(name)
			frappe.db.commit()
		except Exception:
			frappe.db.rollback()
			frappe.log_error(
				title=f"ZRA SIS: status check of {name} failed", reference_doctype=LEDGER, reference_name=name
			)
