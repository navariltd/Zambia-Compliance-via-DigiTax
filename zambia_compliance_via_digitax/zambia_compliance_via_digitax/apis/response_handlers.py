import frappe


def handle_update_response(response: dict, branch: str, settings_name: str, **kwargs) -> None:

    try:
        if not response:
            frappe.log_error("ZRA Item Sync", "Empty response received.")
            return

        # Extract response fields
        zra_item_code = response.get("item_code")
        item_name = response.get("item_name", "Unknown Item")
        status = (response.get("status") or "").strip().upper()
        is_stock_item = response.get("is_stock_item", True)
        default_price = round(response.get("default_unit_price", 0.0), 2)
        recommended_price = round(response.get("recommended_retail_price", default_price), 2)
        stock_quantity = response.get("stock_quantity", 0)
        default_uom = response.get("quantity_unit_code") or "Nos"
        country_code = (response.get("origin_nation_code") or "ZM").lower()
        country_link = get_link_value("Country", "code", country_code)

        if not zra_item_code:
            frappe.log_error("ZRA Item Sync", "Missing item_code in response.")
            return

        # Prepare item fields for update or creation
        item_fields = {
            "item_name": item_name,
            "custom_smart_item_code": zra_item_code,
            "custom_smart_status": status,
            "is_sales_item": 1,
            "is_purchase_item": 1,
            "is_stock_item": is_stock_item,
            "valuation_rate": default_price,
            "last_purchase_rate": default_price,
            "standard_rate": recommended_price,
            "custom_smart_item_classification_code": response.get("item_class_code", ""),
            "custom_smart_item_type_code": response.get("item_type_code", ""),
            "custom_smart_origin_country_code": country_code,
            "custom_smart_origin_country_name": country_link or "",
            "custom_smart_packaging_unit_code": response.get("package_unit_code", ""),
            "custom_smart_quantity_unit_code": default_uom,
            "custom_smart_tax_type_code": response.get("vat_category_code", ""),
            "disabled": 0 if response.get("active", True) else 1
        }

        # Check if item exists
        existing_item = frappe.db.get_value(
            "Item",
            {"custom_smart_item_code": zra_item_code},
            "name",
        )

        if existing_item:
            # Update existing item
            item_doc = frappe.get_doc("Item", existing_item)
            for field, value in item_fields.items():
                item_doc.set(field, value)

            if is_stock_item:
                item_doc.set("actual_qty", stock_quantity)

            item_doc.flags.ignore_mandatory = True
            item_doc.save(ignore_permissions=True)
            frappe.db.commit()
        else:
            # Create new item
            item_doc = frappe.get_doc({
                "doctype": "Item",
                "item_code": zra_item_code,
                "item_group": "All Item Groups",
                **item_fields
            })

            if is_stock_item:
                item_doc.set("actual_qty", stock_quantity)

            item_doc.append("uoms", {"uom": default_uom, "conversion_factor": 1})
            item_doc.flags.ignore_mandatory = True
            item_doc.insert(ignore_permissions=True)
            frappe.db.commit()

    except Exception as e:
        frappe.log_error(
            title="ZRA Item Sync Fatal Error",
            message=f"Failed processing item update: {str(e)}",
        )

def item_search_on_success(response: dict, branch: str, settings_name: str, **kwargs) -> None:
 
    try:
        if not response:
            frappe.log_error("ZRA Item Sync", "Empty response received.")
            return

        item_data = response

        # Extract key fields
        zra_item_code = item_data.get("item_code")
        item_name = item_data.get("item_name", "Unknown Item")
        status = (item_data.get("status") or "").strip().upper()  # Normalize
        is_stock_item = item_data.get("is_stock_item", True)
        default_price = round(item_data.get("default_unit_price", 0.0), 2)
        recommended_price = round(item_data.get("recommended_retail_price", default_price), 2)
        stock_quantity = item_data.get("stock_quantity", 0)
        default_uom = item_data.get("quantity_unit_code") or "Nos"
        country_code = (item_data.get("origin_nation_code") or "ZM").lower()
        country_link = get_link_value("Country", "code", country_code)

        if not zra_item_code:
            frappe.log_error("ZRA Item Sync", "Missing item_code in response.")
            return

        # Prepare item field values
        item_fields = {
            "item_name": item_name,
            "custom_smart_item_code": zra_item_code,
            "custom_smart_status": status,
            "is_sales_item": 1,
            "is_purchase_item": 1,
            "is_stock_item": is_stock_item,
            "valuation_rate": default_price,
            "last_purchase_rate": default_price,
            "standard_rate": recommended_price,  # ERPNext field for recommended price
            "custom_smart_item_classification_code": item_data.get("item_class_code", ""),
            "custom_smart_item_type_code": item_data.get("item_type_code", ""),
            "custom_smart_origin_country_code": country_code,
            "custom_smart_origin_country_name": country_link or "",
            "custom_smart_packaging_unit_code": item_data.get("package_unit_code", ""),
            "custom_smart_quantity_unit_code": default_uom,
            "custom_smart_tax_type_code": item_data.get("vat_category_code", ""),
            "disabled": 0  # Ensure the item is active
        }

        # Check if the item exists
        existing_item = frappe.db.get_value(
            "Item",
            {"custom_smart_item_code": zra_item_code},
            "name",
        )

        if existing_item:
            # Update existing item
            item_doc = frappe.get_doc("Item", existing_item)
            for field, value in item_fields.items():
                item_doc.set(field, value)

            # Update stock quantity if item is stock item
            if is_stock_item:
                item_doc.set("actual_qty", stock_quantity)

            # # Ensure the UOM exists
            # if not any(u.uom == default_uom for u in item_doc.uoms):
            #     item_doc.append("uoms", {"uom": default_uom, "conversion_factor": 1})

            item_doc.flags.ignore_mandatory = True
            item_doc.save(ignore_permissions=True)
            frappe.db.commit()

        else:
            # Create a new item
            item_doc = frappe.get_doc({
                "doctype": "Item",
                "item_code": zra_item_code,
                "item_group": "All Item Groups",
                **item_fields
            })

            # Set stock quantity if stock item
            if is_stock_item:
                item_doc.set("actual_qty", stock_quantity)

            item_doc.append("uoms", {"uom": default_uom, "conversion_factor": 1})
            item_doc.flags.ignore_mandatory = True
            item_doc.insert(ignore_permissions=True)
            frappe.db.commit()

    except Exception as e:
        frappe.log_error(
            title="ZRA Item Sync Fatal Error",
            message=f"Unexpected structure or processing failure: {str(e)}",
        )

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
        item_doc.custom__sent_to_digitax = 1
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
		
def get_link_value(doctype, fieldname, value):
	return frappe.db.get_value(doctype, {fieldname: value}, "name")
