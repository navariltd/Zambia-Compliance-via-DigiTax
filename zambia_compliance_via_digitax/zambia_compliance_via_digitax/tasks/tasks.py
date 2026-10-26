import frappe
from datetime import datetime, timedelta
from frappe.model.document import Document
from ..overrides.sales_invoice import on_submit
from ..apis.sales_invoice import send_invoice_details
from ..utils.settings_utils import get_settings


def get_timeframe(setting_field: str, default_seconds: int = 86400) -> timedelta:
    settings = get_settings()
    if not settings:
        return timedelta(seconds=default_seconds)

    timeframe = settings.get(setting_field, default_seconds) or default_seconds
    return timedelta(seconds=timeframe)

def send_sales_invoice_information(*args, **kwargs) -> None:
    settings = get_settings()

    if not settings or not settings.get("sales_auto_submission_enabled"):
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

        max_tries = settings.get("max_sales_submission_attempts") or 3

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
