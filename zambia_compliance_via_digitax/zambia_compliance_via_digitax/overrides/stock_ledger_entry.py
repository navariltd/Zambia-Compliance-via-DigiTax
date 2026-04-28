import frappe
import json
from functools import partial

from ..apis.api_processor import process_request

# from ..utils.smart_api_utils import split_user_email
# from ..utils.settings_utils import get_settings


import frappe
from functools import partial


def submit_stock(doc, method=None):
    """
    Triggered on Stock Ledger Entry submit.
    Sends DigiTax stock movement payload.
    """

    if not doc or not doc.name:
        frappe.log_error("Invalid SLE in on_submit", str(doc))
        return

    try:
        qty = doc.actual_qty or 0

        # Skip zero movement
        if qty == 0:
            return

        # Determine action
        action = "ADD" if qty > 0 else "DEDUCT"
        quantity = abs(qty)

        # Get DigiTax Item ID
        item_id = frappe.db.get_value("Item", doc.item_code, "custom_smart_remote_id")
        if not item_id:
            frappe.log_error(f"No DigiTax Item ID for {doc.item_code}", "SLE Submit")
            return

        # Get movement type (IMPORTANT: derive from voucher, not SLE)
        movement_type = get_movement_type_from_voucher(doc)

        # Build payload
        payload = {
            "item_id": item_id,
            "quantity": quantity,
            "action": action,
            "movement_type": movement_type,
        }

        # Async submission
        frappe.enqueue(
            process_request,
            queue="default",
            is_async=True,
            request_data=payload,
            route_key="saveStockItems",
            handler_function=partial(stock_submission_success, document_name=doc.name),
            error_callback=partial(on_error, doctype=doc.doctype, document_name=doc.name),
            request_method="PUT",
            doctype=doc.doctype,
            document_name=doc.name,
        )

    except Exception as e:
        frappe.log_error(f"SLE Submit Failed: {doc.name}", str(e))

def get_movement_type_from_voucher(doc):
    """
    Determine movement type using source document (voucher)
    """

    if doc.voucher_type == "Stock Entry":
        stock_entry_type = frappe.db.get_value(
            "Stock Entry",
            doc.voucher_no,
            "stock_entry_type"
        )

        mapping = {
            "Material Receipt": "04",
            "Material Transfer": "13" if doc.actual_qty < 0 else "04",
            "Manufacture": "05" if doc.actual_qty > 0 else "14",
            "Material Issue": "13",
        }

        return mapping.get(stock_entry_type, "06")

    elif doc.voucher_type == "Purchase Receipt":
        return "01"

    elif doc.voucher_type == "Purchase Invoice":
        return "02"

    elif doc.voucher_type in ("Sales Invoice", "Delivery Note"):
        return "11"

    elif doc.voucher_type == "Stock Reconciliation":
        return "06"

    return "06"

def stock_submission_success(response: dict, document_name: str, **kwargs):
    """Mark document as successfuly submitted."""
    frappe.db.set_value(kwargs.get("doctype", "Stock Ledger Entry"), document_name, "custom_inventory_submitted_successfuly", 1)
    


def on_error(response: dict | str, url=None, doctype=None, document_name=None, **kwargs):
    """Increment submission tries and log errors."""
    if doctype and document_name:
        try:
            current_tries = frappe.db.get_value(doctype, document_name, "custom_submission_tries") or 0
            frappe.db.set_value(doctype, document_name, "custom_submission_tries", current_tries + 1)
            frappe.db.commit()
        except Exception:
            frappe.log_error(f"Failed to increment submission tries for {doctype} {document_name}", frappe.get_traceback())
    # handle_errors(response, route=url, doctype=doctype, document_name=document_name)