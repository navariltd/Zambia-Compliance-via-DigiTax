import frappe
from datetime import datetime, timedelta
from frappe.model.document import Document
from ..apis.item import perform_item_registration
from ..overrides.stock_ledger_entry import submit_stock
from ..overrides.sales_invoice import on_submit
from ..apis.sales_invoice import send_invoice_details
from ..utils.settings_utils import get_settings


def get_timeframe(setting_field: str, default_seconds: int = 86400) -> timedelta:
    settings = get_settings()
    if not settings:
        return timedelta(seconds=default_seconds)

    timeframe = settings.get(setting_field, default_seconds) or default_seconds
    return timedelta(seconds=timeframe)

def send_stock_information(*args, **kwargs) -> None:
    settings = get_settings()
    if not settings.get("stock_auto_submission_enabled"):
        return

    timeframe_ago = datetime.now() - get_timeframe("stock_information_submission_timeframe")

    all_stock_ledger_entries: list[Document] = frappe.get_all(
        "Stock Ledger Entry",
        {
            "docstatus": 1,
            "custom_inventory_submitted_successfuly": 0,
            "creation": [">=", timeframe_ago],
        },
       
    )

    for entry in all_stock_ledger_entries:
        doc = frappe.get_doc("Stock Ledger Entry", entry.name, for_update=False)

        max_tries = settings.max_stock_submission_attempts or 3
        
        if doc.custom_submission_tries and int(doc.custom_submission_tries) >= max_tries:
            continue

        try:
            submit_stock(doc, method=None)

        except TypeError:
            continue
def send_sales_invoice_information(*args, **kwargs) -> None:
    settings = get_settings()

    if not settings.get("sales_invoice_auto_submission_enabled"):
        return

    frappe.logger().info("Sales Invoice Auto Submission Triggered")

    timeframe_ago = datetime.now() - get_timeframe("sales_information_submission_timeframe")

    all_sales_invoices = frappe.get_all(
        "Sales Invoice",
        {
            "docstatus": 1,
            "custom_successfully_submitted": 0,
            "creation": [">=", timeframe_ago],
        },
        pluck="name"
    )

    frappe.logger().info(f"Found {len(all_sales_invoices)} invoices to process")

    for invoice_name in all_sales_invoices:
        doc = frappe.get_doc("Sales Invoice", invoice_name, for_update=False)

        max_tries = settings.max_sales_invoice_submission_attempts or 3

        if doc.custom_submission_tries and int(doc.custom_submission_tries) >= max_tries:
            continue

        try:
            send_invoice_details(doc.name)

        except Exception:
            frappe.log_error(
                frappe.get_traceback(),
                f"[SMART] Sales Invoice submission failed: {doc.name}"
            )
            continue
def register_item_with_smart(item_name: str, settings_name: str, **kwargs):
    """
    Safely call perform_item_registration in a background job.
    """
    try:
        item_doc = frappe.get_doc("Item", item_name)

        # Skip registration if already registered
        if getattr(item_doc, "custom_item_registered", 0):
            frappe.logger().info(
                f"[SMART] Item {item_name} already registered. Skipping."
            )
            return

        # Retry guard
        max_tries = 3
        if item_doc.custom_submission_tries and int(item_doc.custom_submission_tries) >= max_tries:
            frappe.logger().warning(
                f"[SMART] Skipping {item_name} - max retries ({max_tries}) reached"
            )
            return

        try:
            perform_item_registration(
                doc=item_doc,
                settings_name=settings_name
            )

        except Exception as e:
            # Log the error but do NOT re-raise (prevents infinite retries)
            frappe.log_error(
                frappe.get_traceback(),
                f"[SMART] Failed Smart registration for {item_name} | Settings: {settings_name}"
            )
            frappe.logger().error(
                f"[SMART] Registration job failed for {item_name} but will not retry: {str(e)}"
            )
            return

    except Exception:
        frappe.log_error(
            frappe.get_traceback(),
            f"[SMART] Unexpected failure in register_item_with_smart for {item_name}"
        )
        return

def update_item_taxes(item_name: str, tax_type: str):
    try:
        item_doc = frappe.get_doc("Item", item_name)

        relevant_templates = frappe.get_all(
            "Item Tax Template",
            ["name"],
            {"custom_taxation_type": tax_type},
        )

        if not relevant_templates:
            frappe.logger().info(f"[SMART] No tax templates for {item_name} with {tax_type}")
            return

        item_doc.taxes = []
        for template in relevant_templates:
            item_doc.append("taxes", {"item_tax_template": template.name})

        item_doc.save(ignore_permissions=True)
        frappe.db.commit()

        frappe.logger().info(f"[SMART] Updated taxes for Item {item_name} based on {tax_type}")

    except Exception as e:
        frappe.log_error(
            frappe.get_traceback(),
            f"[SMART] Failed to update taxes for {item_name} | Tax type: {tax_type}"
        )
        frappe.logger().error(f"[SMART] Tax update job failed for {item_name} but will not retry: {str(e)}")
        return