from typing import Literal
import frappe
from datetime import datetime, timedelta
import requests
from frappe.model.document import Document
from frappe.utils import get_datetime
from ..utils.settings_utils import get_settings
from ..apis.api_builder import EndpointsBuilder
from ..apis.api_processor import process_request
from ..doctype.doctype_names_mapping import SETTINGS_DOCTYPE_NAME
from ..utils.payload_utils import (build_invoice_payload, build_note_payload)
from ..utils.settings_utils import get_settings
from ..utils.tax_utils import calculate_tax


def get_timeframe(settings_name: str) -> timedelta:
    settings = get_settings()
    if not settings:
        return timedelta(seconds=86400)
    timeframe = settings.get(
        "stock_information_submission_timeframe", 86400) or 86400
    return timedelta(seconds=timeframe)


def on_submit(doc, method=None):
    # Enqueue background job for each active Smart API setting
    settings = get_settings()
    if not settings.get("sales_auto_submission_enabled") or doc.custom_prevent_sis_submission == 1 or doc.custom_successfully_submitted == 1:
        return
    frappe.enqueue(
        "zambia_compliance_via_digitax.zambia_compliance_via_digitax.apis.sales_invoice.send_invoice_details",
        name=doc.name,

        queue="long",

    )


def generic_invoices_on_submit_override(
    doc: Document, invoice_type: Literal["Sales Invoice", "POS Invoice"]
) -> None:
    """
    Handles sending of Sales, Credit Notes, and now Debit Notes to VSDC.
    All API calls are asynchronous (via frappe.enqueue).
    """

    company_name = doc.company
    settings_doc = get_settings(company_name)
    # frappe.throw(frappe.as_json(settings_doc,indent=2))

    # Skip if prevented or already submitted
    if doc.custom_prevent_sis_submission or getattr(doc, "vsdc_invoice_number", None):
        return
    calculate_tax(doc)

# ================= CREDIT NOTE =================
    if doc.is_return and doc.return_against:
        payload = build_note_payload(
            doc, settings_doc.name, note_type="credit")
        route_key = "saveCreditNote"
   # ================= CREDIT NOTE =================
    elif hasattr(doc, "is_debit_note") and doc.is_debit_note:
        payload = build_note_payload(doc, settings_doc.name, note_type="debit")
        route_key = "saveDebitNote"
    # =============== NORMAL SALES INVOICE SUBMISSION ==================
    else:
        payload = build_invoice_payload(doc, settings_doc.name)
        route_key = "saveSales"

    # frappe.throw(frappe.as_json(payload, indent=2))
    process_request(
        request_data=payload,
        route_key=route_key,
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
        "custom_sent_to_digitax": 1,
        "custom_sales_id": result_data.get("id"),
        "custom_sale_no": result_data.get("sale_number"),
        "custom_receipt_type_": result_data.get("receipt_type_code"),
        "custom_receipt_number": result_data.get("receipt_number"),
        "custom_submission_status": result_data.get("status"),
        "custom_sale_date": result_data.get("sale_date"),
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

    item_list = result_data.get("item_list", [])

    invoice = frappe.get_doc("Sales Invoice", document_name)

    for item in item_list:
        row = next(
            (r for r in invoice.items if r.custom_sis_item_id == item.get("item_id")), None)
        if not row:
            row = next((r for r in invoice.items if r.item_code == item.get("item_code")), None)
        if not row:
            frappe.logger().warning(
                f"Could not match item {item.get('item_code')} in invoice {document_name}")
            continue

        frappe.db.set_value("Sales Invoice Item", row.name, {
            "custom_vat_taxable_amount": item.get("vat_taxable_amount"),
            "custom_vat_tax_amount": item.get("vat_tax_amount"),
            "custom_ipl_taxable_amount": item.get("ipl_taxable_amount"),
            "custom_ipl_tax_amount": item.get("ipl_tax_amount"),
            "custom_tl_taxable_amount": item.get("tl_taxable_amount"),
            "custom_tl_tax_amount": item.get("tl_tax_amount"),
            "custom_excise_taxable_amount": item.get("excise_taxable_amount"),
            "custom_excise_tax_amount": item.get("excise_tax_amount"),
            "custom_tot_taxable_amount": item.get("tot_taxable_amount"),
            "custom_tot_tax_amount": item.get("tot_tax_amount"),
        })

    # Update ERPNext document (moved out of the item loop so it only runs once)
    frappe.db.set_value(doctype, document_name, updates)
    frappe.publish_realtime(
        "refresh_form",
        {"name": document_name},
        doctype=doctype,
        docname=document_name
    )

    # Hand reconciliation off to a background worker. The delay and retry logic
    # live inside the job itself, so the request thread returns immediately.
    frappe.enqueue(
        _fetch_invoice_details_with_retry,
        queue="short",
        enqueue_after_commit=True,  # only fires once this transaction is committed
        document_name=document_name,
        invoice_type=doctype,
        settings_name=settings_name,
    )


PENDING_STATUSES = {"pending", "processing", "submitted"}  # adjust to match your actual status values


def _fetch_invoice_details_with_retry(
    document_name: str,
    invoice_type: str,
    settings_name: str,
    attempt: int = 1,
    max_attempts: int = 6,
    base_delay_seconds: int = 2,
) -> None:
    """
    Runs in the background worker (not the request thread). The transaction is
    often still "pending" on ZRA's side right after submission, so this polls:
    wait briefly, fetch the latest details, check whether the status has moved
    past pending, and if not, re-enqueue itself with backoff. Gives up after
    max_attempts and logs so it doesn't poll forever.
    """
    import time
    from ..apis.sales_invoice import get_invoice_details

    # Backoff: 2s, 4s, 8s, 16s, 32s, 64s ... capped by max_attempts
    delay = base_delay_seconds * (2 ** (attempt - 1))
    time.sleep(delay)

    try:
        get_invoice_details(
            document_name=document_name,
            invoice_type=invoice_type,
            settings_name=settings_name,
        )
    except Exception:
        frappe.log_error(
            frappe.get_traceback(),
            f"ZRA get_invoice_details call failed (attempt {attempt}, {document_name})",
        )
        current_status = None  # treat as unresolved, fall through to retry logic below
    else:
        current_status = frappe.db.get_value(
            invoice_type, document_name, "custom_submission_status"
        )

    still_pending = (current_status or "").strip().lower() in PENDING_STATUSES

    if not still_pending:
        # Status has moved on (approved/rejected/whatever terminal state ZRA uses) — done.
        return

    if attempt >= max_attempts:
        frappe.log_error(
            f"Status for {document_name} still '{current_status}' after {attempt} attempts — giving up.",
            "ZRA get_invoice_details still pending",
        )
        return

    frappe.enqueue(
        _fetch_invoice_details_with_retry,
        queue="short",
        document_name=document_name,
        invoice_type=invoice_type,
        settings_name=settings_name,
        attempt=attempt + 1,
        max_attempts=max_attempts,
        base_delay_seconds=base_delay_seconds,
    )



def sales_information_submission_on_error(
    response: dict | str | None = None,
    url: str | None = None,
    doctype: str | None = None,
    document_name: str | None = None,
    payload: dict | None = None,
    settings_name: str | None = None,
    error=None,
    **kwargs,
):
    # Fallbacks in case kwargs are used instead of explicit args
    doctype = doctype or kwargs.get("doctype")
    document_name = document_name or kwargs.get("document_name")

    # Detect network-related errors
    is_network_error = isinstance(
        error,
        (
            requests.exceptions.ConnectionError,
            requests.exceptions.Timeout,
            requests.exceptions.ConnectTimeout,
        ),
    )

    # Extra safety: string-based detection (some errors are wrapped)
    error_str = str(error).lower() if error else ""
    network_keywords = ["connection", "timeout", "temporarily unavailable"]

    if is_network_error or any(k in error_str for k in network_keywords):
        frappe.logger().error(
            f"[DIGITAX] Network error for {document_name}: {error}"
        )
        return

    # Only update if we have valid identifiers
    if doctype and document_name:
        # Get current retry count (default to 0 if not set)
        current_tries = frappe.db.get_value(
            doctype,
            document_name,
            "custom_submission_tries"
        ) or 0

        # Increment retry count
        frappe.db.set_value(
            doctype,
            document_name,
            "custom_submission_tries",
            current_tries + 1,
        )

        frappe.db.set_value(
            doctype,
            document_name,
            "custom_sent_to_digitax",
            1,
        )

    else:
        frappe.logger().warning(
            f"[DIGITAX] Missing doctype or document_name. "
            f"doctype={doctype}, document_name={document_name}"
        )

    # Log full error details
    frappe.log_error(
        title="Sales Submission Failed",
        message=(
            f"Failed sending invoice {document_name} of {doctype}\n"
            f"URL: {url}\n"
            f"Settings: {settings_name}\n"
            f"Payload: {payload}\n"
            f"Response: {response}\n"
            f"Error: {error}"
        ),
    )
