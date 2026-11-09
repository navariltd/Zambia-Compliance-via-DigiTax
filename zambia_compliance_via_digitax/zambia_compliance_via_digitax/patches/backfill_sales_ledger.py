"""Give invoices sent by the old sales flow (custom_sales_id set) a ZRA SIS Sales Ledger entry.

Each becomes Pending with its DigiTax sale ID and is due for a status check, so the hourly sweep fetches
DigiTax's real state into the ledger. Credit notes against these sales can then find their original.
"""

import secrets

import frappe
from frappe.utils import now_datetime

from ..doctype.doctype_names_mapping import SALES_LEDGER_DOCTYPE_NAME
from ..services import sales


def execute():
	for name in frappe.get_all(
		"Sales Invoice",
		filters={
			"docstatus": 1,
			"custom_sales_id": ["is", "set"],
			"custom_sis_ledger_entry": ["is", "not set"],
		},
		pluck="name",
		order_by="posting_date asc, creation asc",
	):
		invoice = frappe.get_doc("Sales Invoice", name)
		if frappe.db.exists(SALES_LEDGER_DOCTYPE_NAME, {"sale_id": invoice.custom_sales_id}):
			continue
		if frappe.db.exists(SALES_LEDGER_DOCTYPE_NAME, name):
			continue
		settings = frappe.db.get_value(
			"ZRA SIS Settings", {"company_name": invoice.company, "is_active": 1}, ["name"], as_dict=True
		)
		if not settings:
			continue
		settings.update(sales.get_sales_settings(invoice.company) or {})

		prepared = sales.build_payload(invoice, settings)
		entry = frappe.get_doc(
			{
				"doctype": SALES_LEDGER_DOCTYPE_NAME,
				"trader_invoice_number": name,
				"document_key": sales.get_document_key("Sales Invoice", name),
				"reference_doctype": "Sales Invoice",
				"reference_name": name,
				"company": invoice.company,
				"settings": settings.name,
				"posting_date": invoice.posting_date,
				"customer": invoice.customer,
				"currency": invoice.currency,
				"callback_token": secrets.token_urlsafe(24),
			}
		)
		sales.apply_prepared(entry, prepared)
		entry.update(
			{
				"status": sales.STATUS_PENDING_ZRA,
				"problems": None,
				"sale_id": invoice.custom_sales_id,
				"sale_number": invoice.get("custom_sale_no"),
				"attempts": 1,
				"next_check_at": now_datetime(),
				"last_error": "Sent by the previous sales integration; its state is fetched from DigiTax.",
			}
		)
		entry.flags.from_sales_service = True
		entry.insert(ignore_permissions=True)
		sales.mirror_to_invoice(entry.name)
		frappe.db.commit()
