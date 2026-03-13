from typing import Literal

import frappe
from frappe.model.document import Document
from frappe.utils import get_datetime
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
        handler_function=sales_information_submission_on_success,
        request_method="POST",
        document_name=doc.name,
        doctype=invoice_type,
       
        error_callback=sales_information_submission_on_error,
    )

def sales_information_submission_on_success(
    response: dict, document_name: str, doctype: str, settings_name: str, **kwargs
) -> None:
    """
    Callback executed after a successful Sales Invoice submission to ZRA Smart Invoice.
    Updates the ERPNext document with ZRA response details and triggers reconciliation.
    """
    if not response:
        frappe.throw("Empty response from ZRA Smart Invoice system.")

    # Debug logging
    frappe.log_error(frappe.as_json(response), "ZRA Response Debug")

    # Extract response fields
    result_data = response  # response itself contains the invoice object
    updates = {
        "custom_successfully_submitted": 1,
        "custom_sales_id": result_data.get("id"),
        # "custom_trader_invoice_number": result_data.get("trader_invoice_number"),
        "custom_sale_no": result_data.get("sale_number"),
        # "custom_invoice_kind": result_data.get("kind"),
        "custom_receipt_type_": result_data.get("receipt_type_code"),
        "custom_receipt_number": result_data.get("receipt_number"),
        # "custom_lpo_number": result_data.get("lpo_number"),
        # "custom_destination_country": result_data.get("destination_country_code"),
        # "custom_currency_code": result_data.get("currency_code"),
        # "custom_exchange_rate": result_data.get("exchange_rate"),
        "custom_submission_status": result_data.get("status"),
        "custom_sale_date": result_data.get("sale_date"),
        
        # "custom_cash_discount_rate": result_data.get("cash_discount_rate"),
        # "custom_cash_discount_amount": result_data.get("cash_discount_amount"),
    }

    # Update tax summary
    tax_summary = result_data.get("sales_tax_summary", {})
    updates.update({
        "custom_taxable_amount_vat": tax_summary.get("taxable_amount_vat"),
        "custom_taxable_amount_ipl": tax_summary.get("taxable_amount_ipl"),
        "custom_taxable_amount_tl": tax_summary.get("taxable_amount_tl"),
        "custom_taxable_amount_excise": tax_summary.get("taxable_amount_excise"),
        "custom_taxable_amount_tot": tax_summary.get("taxable_amount_tot"),
        "custom_tax_amount_vat": tax_summary.get("tax_amount_vat"),
        "custom_tax_amount_ipl": tax_summary.get("tax_amount_ipl"),
        "custom_tax_amount_tl": tax_summary.get("tax_amount_tl"),
        "custom_tax_amount_excise": tax_summary.get("tax_amount_excise"),
        "custom_tax_amount_tot": tax_summary.get("tax_amount_tot"),
    })

    if result_data.get("created_at"):
        updates["custom_created_at"] = get_datetime(result_data.get("created_at"))
    # Update ERPNext document
    frappe.db.set_value(doctype, document_name, updates)
    frappe.db.commit()
    frappe.publish_realtime("refresh_form", document_name)

    item_list = result_data.get("item_list", [])

    invoice = frappe.get_doc("Sales Invoice", document_name)

    for item in item_list:
        row = next((r for r in invoice.items if r.custom_sis_item_id == item.get("item_id")), None)
        if not row:
            # fallback by item_code
            row = next((r for r in invoice.items if r.item_code == item.get("item_code")), None)
        if not row:
            frappe.logger().warning(f"Could not match item {item.get('item_code')} in invoice {document_name}")
            continue

        row.custom_vat_taxable_amount = item.get("vat_taxable_amount")
        row.custom_vat_tax_amount = item.get("vat_tax_amount")
        row.custom_ipl_taxable_amount = item.get("ipl_taxable_amount")
        row.custom_ipl_tax_amount = item.get("ipl_tax_amount")
        row.custom_tl_taxable_amount = item.get("tl_taxable_amount")
        row.custom_tl_tax_amount = item.get("tl_tax_amount")
        row.custom_excise_taxable_amount = item.get("excise_taxable_amount")
        row.custom_excise_tax_amount = item.get("excise_tax_amount")
        row.custom_tot_taxable_amount = item.get("tot_taxable_amount")
        row.custom_tot_tax_amount = item.get("tot_tax_amount")

    invoice.save(ignore_permissions=True)

    # Enqueue background fetch for reconciliation
    # frappe.enqueue(
    #     get_vsdc_invoice_details,
    #     queue="long",
    #     document_name=document_name,
    #     invoice_type=doctype,
    #     settings_name=settings_name,
    # )

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
