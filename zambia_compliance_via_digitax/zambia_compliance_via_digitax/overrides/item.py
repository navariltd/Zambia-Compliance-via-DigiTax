import frappe
from frappe.model.document import Document
from ..apis.item import perform_item_registration
from ..utils.smart_api_utils import get_active_smart_settings

# ---------------------------
# Hook: On Update
# ---------------------------
def on_update(doc, method=None):
    # Enqueue background job for each active Smart API setting
    active_settings = get_active_smart_settings()
    
    if not active_settings:
        return

    for setting in active_settings:
        frappe.enqueue(
            "zambia_compliance_via_digitax.zambia_compliance_via_digitax.tasks.tasks.register_item_with_smart",
            item_name=doc.name,
            settings_name=setting.get("name"),
            queue="long",
            
        )


# ---------------------------
# Hook: Validate
# ---------------------------
def validate(doc: Document, method: str = None) -> None:
    """
    Enqueue background job to safely update taxes child table
    based on custom_smart_tax_type_code.
    """
    if not doc.custom_smart_tax_type_code:
        return

    if not doc.has_value_changed("custom_smart_tax_type_code"):
        return

    frappe.enqueue(
        "zambia_compliance_via_digitax.zambia_compliance_via_digitax.tasks.tasks.update_item_taxes",
        item_name=doc.name,
        tax_type=doc.custom_smart_tax_type_code,
        queue="long",
        
    )