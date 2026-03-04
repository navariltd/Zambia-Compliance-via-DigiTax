import frappe
def handle_registration_response(
    response,
    request_data=None,
    document_name=None,
    doctype=None,
    payload=None,
    settings_name=None,
    branch=None,
    bhfid=None,
):
    """
    Handles DigiTax ZRA SIS item registration response.
    Saves returned `item_code` directly to Item.custom_smart_item_code.
    """

    frappe.logger().info(
        f"[SMART] Registration Response: {frappe.as_json(response)}"
    )

    try:
        if not response:
            frappe.log_error(
                "[SMART] Empty response received",
                "Item Registration Handler",
            )
            return

        # Extract returned values from DigiTax
        zra_item_code = response.get("item_code")
        remote_id = response.get("id")
        status = response.get("status")

        if not zra_item_code:
            frappe.log_error(
                "[SMART] item_code missing in response",
                "Item Registration Handler",
            )
            return

        if not document_name:
            frappe.log_error(
                "[SMART] document_name missing — cannot update Item",
                "Item Registration Handler",
            )
            return

        # Load the ERPNext Item
        item_doc = frappe.get_doc("Item", document_name)

        # Set returned Smart Item Code
        item_doc.custom_smart_item_code = zra_item_code
        item_doc.custom_smart_remote_id = remote_id
        item_doc.custom_smart_status = status
        item_doc.custom_item_registered = 1
        item_doc.custom_active = 1

        item_doc.save(ignore_permissions=True)
        frappe.db.commit()

        frappe.logger().info(
            f"[SMART] Item {item_doc.name} registered successfully "
            f"→ ZRA Code: {zra_item_code} | Status: {status}"
        )

    except Exception:
        frappe.log_error(
            frappe.get_traceback(),
            "[SMART] Failed to process DigiTax registration response",
        )