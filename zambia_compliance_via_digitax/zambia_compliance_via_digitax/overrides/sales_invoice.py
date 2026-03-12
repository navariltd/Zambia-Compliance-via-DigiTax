from typing import Literal

import frappe
from frappe.model.document import Document

from ..utils.settings_utils import get_settings
from ..apis.api_builder import EndpointsBuilder
from ..apis.api_processor import process_request
from ..doctype.doctype_names_mapping import SETTINGS_DOCTYPE_NAME
from ..utils.payload_utils import build_invoice_payload

@frappe.whitelist()
def send_invoice_details(name: str) -> None:
	"""Manual trigger to push a Sales Invoice to Crystal VSDC."""
	doc = frappe.get_doc("Sales Invoice", name)

	# Skip opening entries
	if doc.is_opening == "Yes":
		return

	generic_invoices_on_submit_override(doc, "Sales Invoice")




def generic_invoices_on_submit_override(
    doc: Document, invoice_type: Literal["Sales Invoice", "POS Invoice"]
) -> None:
    """
    Handles sending of Sales, Credit Notes, and now Debit Notes to VSDC.
    All API calls are asynchronous (via frappe.enqueue).
    """

    company_name = doc.company
    settings_doc = get_settings(company_name)

    # Skip if prevented or already submitted
    if doc.custom_prevent_sis_submission or getattr(doc, "vsdc_invoice_number", None):
        return

   
    # =============== NORMAL SALES INVOICE SUBMISSION ==================
  
    payload = build_invoice_payload(doc, settings_doc.name)

    frappe.enqueue(
        process_request,
        queue="default",
        is_async=True,
        request_data=payload,
        route_key="saveSales",
        handler_function=handle_sales_submission_success,
        request_method="POST",
        document_name=doc.name,
        doctype=invoice_type,
       
        error_callback=sales_information_submission_on_error,
    )

def handle_sales_submission_success():
     pass

def sales_information_submission_on_error(
	response: dict | str | None,
	url: str | None,
	doctype: str | None,
	document_name: str | None,
	payload: dict | None,
	settings_name: str | None,
):
	frappe.log_error(
		title="Sales Submission Failed",
		message=f"Failed sending invoice {document_name} of {doctype}\n"
		f"URL: {url}\n"
		f"Settings: {settings_name}\n"
		f"Payload: {payload}\n"
		f"Response: {response}",
	)
