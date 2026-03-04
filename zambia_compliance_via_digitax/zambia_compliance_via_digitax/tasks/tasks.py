import frappe
from frappe.model.document import Document
from ..apis.item import perform_item_registration


@frappe.whitelist()
def register_item_with_smart(item_name: str, settings_name: str):
    """
    Safely call perform_item_registration in a background job.
    """
    try:
        item_doc = frappe.get_doc("Item", item_name)

        # Skip registration if already registered
        if getattr(item_doc, "custom_item_registered", 0):
            frappe.logger().info(f"[SMART] Item {item_name} already registered. Skipping.")
            return

        from ..apis.item import perform_item_registration

        perform_item_registration(
            doc=item_doc,
            settings_name=settings_name
        )

    except Exception as e:
        # Log the error but do NOT re-raise, prevents infinite retries
        frappe.log_error(
            frappe.get_traceback(),
            f"[SMART] Failed Smart registration for {item_name} | Settings: {settings_name}"
        )
        frappe.logger().error(f"[SMART] Registration job failed for {item_name} but will not retry: {str(e)}")
        return
@frappe.whitelist()
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