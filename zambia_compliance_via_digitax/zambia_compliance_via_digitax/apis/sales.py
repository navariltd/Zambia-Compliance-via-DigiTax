"""Whitelisted ZRA actions for Sales Invoices and their ledger entries. Logic lives in services/sales.py."""

import hmac

import frappe

from ..doctype.doctype_names_mapping import SALES_LEDGER_DOCTYPE_NAME
from ..services import sales as sales_service


def _get_invoice(invoice: str, permission: str):
	doc = frappe.get_doc("Sales Invoice", invoice)
	doc.check_permission(permission)
	return doc


@frappe.whitelist(methods=["POST"])
def send_or_retry(invoice: str) -> dict:
	"""Send the invoice to ZRA, or retry its ledger entry."""
	_get_invoice(invoice, "submit")
	return sales_service.send_or_retry(invoice)


@frappe.whitelist(methods=["POST"])
def check_status(invoice: str) -> dict:
	"""Fetch the invoice's sale from DigiTax (GET /sales/{sale_id})."""
	_get_invoice(invoice, "read")
	entry = sales_service.get_current_entry("Sales Invoice", invoice)
	if not entry:
		frappe.throw(frappe._("This invoice has no ZRA ledger entry."))
	return sales_service.check_entry_status(entry.name)


@frappe.whitelist(methods=["POST"])
def link_sale_id(invoice: str, sale_id: str) -> dict:
	"""Attach a sale found on the DigiTax dashboard to an invoice whose ZRA status is Unknown."""
	_get_invoice(invoice, "submit")
	return sales_service.link_sale_id(invoice, sale_id)


@frappe.whitelist(allow_guest=True, methods=["POST"])
def sale_callback(entry: str | None = None, token: str | None = None, **kwargs) -> None:
	"""DigiTax calls this (event sale.sync) when a sale has synced with ZRA.

	The body is not trusted: the entry and its secret token come from the callback URL we sent, and the
	sale is then re-read from DigiTax.
	"""
	if not entry or not token or not isinstance(entry, str) or not isinstance(token, str):
		return
	expected = frappe.db.get_value(SALES_LEDGER_DOCTYPE_NAME, entry, "callback_token")
	if not expected or not hmac.compare_digest(expected, token):
		return
	sales_service.enqueue_job(sales_service.CHECK_JOB, f"{entry}::callback", queue="short", entry=entry)
