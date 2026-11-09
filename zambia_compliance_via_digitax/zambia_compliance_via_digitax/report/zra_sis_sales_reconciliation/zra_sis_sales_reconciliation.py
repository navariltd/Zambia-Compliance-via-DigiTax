# Copyright (c) 2026, Navari Ltd and contributors
# For license information, please see license.txt

"""Submitted Sales Invoices against their ZRA SIS Sales Ledger entries: what is missing, not yet
fiscalised, or fiscalised with a different amount or tax than ERPNext."""

import frappe
from frappe import _
from frappe.utils import flt, getdate

from ...doctype.doctype_names_mapping import SALES_LEDGER_DOCTYPE_NAME
from ...services import sales

TOLERANCE = 0.01
OPEN_STATUSES = (sales.STATUS_NOT_READY, sales.STATUS_QUEUED, sales.STATUS_SENDING, sales.STATUS_PENDING_ZRA)


def execute(filters=None):
	filters = frappe._dict(filters or {})
	return get_columns(), get_data(filters)


def get_columns():
	return [
		{
			"label": _("Invoice"),
			"fieldname": "invoice",
			"fieldtype": "Link",
			"options": "Sales Invoice",
			"width": 170,
		},
		{"label": _("Date"), "fieldname": "posting_date", "fieldtype": "Date", "width": 100},
		{"label": _("Type"), "fieldname": "receipt_type", "fieldtype": "Data", "width": 100},
		{
			"label": _("Customer"),
			"fieldname": "customer",
			"fieldtype": "Link",
			"options": "Customer",
			"width": 160,
		},
		{
			"label": _("Currency"),
			"fieldname": "currency",
			"fieldtype": "Link",
			"options": "Currency",
			"width": 70,
		},
		{
			"label": _("ERPNext Total"),
			"fieldname": "erpnext_total",
			"fieldtype": "Float",
			"precision": 2,
			"width": 120,
		},
		{
			"label": _("ZRA Total"),
			"fieldname": "zra_total",
			"fieldtype": "Float",
			"precision": 2,
			"width": 120,
		},
		{
			"label": _("Tax Difference"),
			"fieldname": "tax_variance",
			"fieldtype": "Float",
			"precision": 2,
			"width": 110,
		},
		{"label": _("ZRA Status"), "fieldname": "status", "fieldtype": "Data", "width": 100},
		{"label": _("Receipt Number"), "fieldname": "receipt_number", "fieldtype": "Data", "width": 110},
		{
			"label": _("Ledger Entry"),
			"fieldname": "ledger_entry",
			"fieldtype": "Link",
			"options": SALES_LEDGER_DOCTYPE_NAME,
			"width": 170,
		},
		{"label": _("Issue"), "fieldname": "issue", "fieldtype": "Data", "width": 260},
	]


def get_data(filters):
	settings = sales.get_sales_settings(filters.company)
	invoices = frappe.get_all(
		"Sales Invoice",
		filters={
			"company": filters.company,
			"docstatus": 1,
			"posting_date": ["between", [filters.from_date, filters.to_date]],
		},
		fields=[
			"name",
			"posting_date",
			"customer",
			"currency",
			"grand_total",
			"rounding_adjustment",
			"is_return",
			"is_debit_note",
			"is_opening",
			"is_consolidated",
			"custom_prevent_sis_submission",
			"custom_sis_ledger_entry",
		],
		order_by="posting_date asc, name asc",
	)
	entries = {
		e.name: e
		for e in frappe.get_all(
			SALES_LEDGER_DOCTYPE_NAME,
			filters={
				"name": ["in", [i.custom_sis_ledger_entry for i in invoices if i.custom_sis_ledger_entry]]
			},
			fields=[
				"name",
				"status",
				"receipt_number",
				"zra_total",
				"tax_variance",
				"problems",
				"last_error",
			],
		)
	}

	rows = []
	for invoice in invoices:
		entry = entries.get(invoice.custom_sis_ledger_entry)
		erpnext_total = abs(flt(invoice.grand_total) - flt(invoice.rounding_adjustment))
		row = frappe._dict(
			invoice=invoice.name,
			posting_date=invoice.posting_date,
			receipt_type=_(sales.get_receipt_type(invoice)),
			customer=invoice.customer,
			currency=invoice.currency,
			erpnext_total=erpnext_total,
			ledger_entry=entry.name if entry else None,
			status=_(entry.status) if entry else None,
			receipt_number=entry.receipt_number if entry else None,
			zra_total=entry.zra_total if entry else None,
			tax_variance=entry.tax_variance if entry else None,
		)
		row.issue, row.is_open = get_issue(invoice, entry, settings, erpnext_total)
		if filters.only_issues and row.issue == _("OK"):
			continue
		rows.append(row)
	return rows


def get_issue(invoice, entry, settings, erpnext_total) -> tuple[str, bool]:
	"""(what is wrong, whether it is still in progress)."""
	if not entry:
		if is_excluded(invoice, settings):
			return _("OK"), False
		return _("Missing from the ledger"), False
	if entry.status in OPEN_STATUSES:
		detail = entry.problems if entry.status == sales.STATUS_NOT_READY else None
		return (detail or _("Not fiscalised yet")).split("\n")[0], True
	if entry.status != sales.STATUS_COMPLETED_ZRA:
		return (entry.last_error or _(entry.status)).split("\n")[0], False
	if abs(flt(entry.zra_total) - erpnext_total) > TOLERANCE:
		return _("ZRA total differs from the invoice"), False
	if abs(flt(entry.tax_variance)) > TOLERANCE:
		return _("ZRA tax differs from ERPNext's"), False
	return _("OK"), False


def is_excluded(invoice, settings) -> bool:
	"""Invoices that are never sent: not covered by the settings, opening, consolidated or prevented."""
	if (
		not settings
		or invoice.is_opening == "Yes"
		or invoice.is_consolidated
		or invoice.custom_prevent_sis_submission
	):
		return True
	return bool(settings.go_live_date and getdate(invoice.posting_date) < getdate(settings.go_live_date))
