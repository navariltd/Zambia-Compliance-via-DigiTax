import frappe
from frappe.model.document import Document
from frappe.utils import flt


def calculate_tax(doc: "Document") -> None:
    """
    Calculate and assign taxes for invoice items.

    Priority:
    1. Item-level tax templates
    2. Document-level taxes
    3. Default settings fallback

    Also sets VAT category codes.
    """

    taxes = doc.get("taxes", []) or []
    has_item_level_tax = any(
        getattr(item, "item_tax_template", None) for item in doc.items
    )

    # Reset VAT amounts
    for item in doc.items:
        item.custom_vat_tax_amount = 0
        item.custom_tax_rate = 0

    if has_item_level_tax:
        _calculate_item_level_taxes(doc)
    elif taxes:
        _calculate_document_level_taxes(doc, taxes)
    else:
        _apply_default_tax_from_settings(doc)

    _set_taxation_type_codes(doc)


def _calculate_item_level_taxes(doc: "Document") -> None:
    for item in doc.items:
        if not item.item_tax_template:
            continue

        tax_template = frappe.get_doc("Item Tax Template", item.item_tax_template)

        total_rate = 0
        total_tax = 0

        for tax in tax_template.taxes:
            tax_rate = flt(getattr(tax, "tax_rate", 0))
            tax_amount = flt(item.base_net_amount) * tax_rate / 100

            total_rate += tax_rate
            total_tax += tax_amount

        item.custom_vat_tax_amount = total_tax
        item.custom_tax_rate = total_rate


def _calculate_document_level_taxes(doc: "Document", taxes: list) -> None:
    for item in doc.items:
        total_rate = 0
        total_tax = 0

        for tax in taxes:
            tax_rate = flt(tax.get("rate", 0))
            tax_amount = flt(item.base_net_amount) * tax_rate / 100

            total_rate += tax_rate
            total_tax += tax_amount

        item.custom_vat_tax_amount = total_tax
        item.custom_tax_rate = total_rate


def _apply_default_tax_from_settings(doc: "Document") -> None:
    settings_name = frappe.get_value(
        "ZRA SIS Settings",
        {"company_name": doc.company},
        "name"
    )

    if not settings_name:
        frappe.throw(f"No ZRA SIS Settings found for company {doc.company}")

    settings = frappe.get_doc("ZRA SIS Settings", settings_name)

    default_rate = flt(settings.default_tax_rate)
    if not default_rate:
        return

    for item in doc.items:
        item.custom_vat_tax_amount = flt(item.base_net_amount) * default_rate / 100
        item.custom_tax_rate = default_rate


def _set_taxation_type_codes(doc: "Document") -> None:
    for item in doc.items:
        code = None

        # Priority 1: Item Tax Template code
        if item.item_tax_template:
            code = frappe.db.get_value(
                "Item Tax Template",
                item.item_tax_template,
                "custom_taxation_type"
            )

        # Priority 2: Derive from rate
        if not code:
            rate = flt(item.custom_tax_rate)

            if round(rate) == 16:
                code = "A"
            elif round(rate) == 8:
                code = "E"
            elif rate == 0:
                code = "B"
            else:
                code = "B"  # fallback

        item.custom_sis_vat_category_code = code