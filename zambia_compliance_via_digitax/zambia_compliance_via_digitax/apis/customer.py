import frappe
import json
from functools import partial

from .api_processor import process_request
from ..utils.payload_utils import build_customer_payload


@frappe.whitelist()
def perform_customer_registration(doc, settings_name=None):

    """
    Register Customer to DigiTax (ZRA SIS)

    :param doc: Customer doc (dict or JSON string)
    :param settings_name: Smart Setup name (company-specific config)
    """

    # ---------------- Parse डॉक ----------------
    if isinstance(doc, str):
        doc = json.loads(doc)

    if isinstance(doc, dict):
        doc = frappe.get_doc("Customer", doc.get("name"))

    if not doc:
        frappe.throw("Invalid Customer document")

    try:
        # ---------------- Build Payload ----------------
        payload = build_customer_payload(doc)

        # ---------------- Enqueue Request ----------------
        frappe.enqueue(
            process_request,
            queue="default",
            is_async=True,
            request_data=payload,
            route_key="saveCustomer",   # DigiTax route
            settings_name=settings_name,
            request_method="POST",

            # Success / Error handlers
            handler_function=partial(
                customer_registration_success,
                document_name=doc.name,
                settings_name=settings_name
            ),
            error_callback=partial(
                customer_registration_error,
                doctype="Customer",
                document_name=doc.name
            ),

            doctype="Customer",
            document_name=doc.name,
        )

    except Exception as e:
        frappe.log_error(
            f"Customer Registration Failed: {doc.name}",
            frappe.get_traceback()
        )
        frappe.throw(f"Customer registration failed: {str(e)}")

def customer_registration_success(response: dict, document_name: str, settings_name=None, **kwargs):
    """
    Handle successful customer registration and store DigiTax ID
    """

    try:
        # ---------------- Extract Response ----------------
        customer_id = response.get("id")

        update_values = {
            "custom_registered_successfuly": 1,
        }

        if customer_id:
            update_values["custom_sis_customer_id"] = customer_id

        # ---------------- Update Customer ----------------
        frappe.db.set_value("Customer", document_name, update_values)
        frappe.db.commit()

        
        frappe.publish_realtime(
            event="doc_update",
            message={
                "doctype": "Customer",
                "name": document_name
            }
        )

    except Exception:
        frappe.log_error(
            f"Failed to update customer after registration: {document_name}",
            frappe.get_traceback()
        )

def customer_registration_error(response, doctype=None, document_name=None, **kwargs):
    """
    Handle failed registration attempts
    """

    if doctype and document_name:
        try:
            current_tries = frappe.db.get_value(
                doctype,
                document_name,
                "custom_submission_tries"
            ) or 0

            frappe.db.set_value(
                doctype,
                document_name,
                "custom_submission_tries",
                current_tries + 1
            )
            frappe.db.commit()

        except Exception:
            frappe.log_error(
                f"Failed to increment tries for {doctype} {document_name}",
                frappe.get_traceback()
            )

    frappe.log_error("Customer Registration Error", str(response))