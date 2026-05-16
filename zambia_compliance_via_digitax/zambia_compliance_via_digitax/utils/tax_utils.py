from collections import defaultdict

import frappe
from frappe.model.document import Document
from frappe.utils import flt



def calculate_tax(doc: Document) -> dict:
    """
    Orchestrates tax calculation.
    """

    taxes = doc.get("taxes") or []

    if taxes and any(getattr(t, "item_wise_tax_detail", None) for t in taxes):
        return _calculate_from_item_wise_tax_table(doc)

    return _calculate_taxes_by_hierarchy(doc)

def _calculate_from_item_wise_tax_table(doc: "Document") -> dict:

    results = {}
    grouped = defaultdict(lambda: {"tax": 0.0, "taxable": 0.0})

    taxes = doc.get("taxes") or []

    if not taxes:
        return results

    for tax in taxes:

        item_wise = tax.get("item_wise_tax_detail")

        if not item_wise:
            continue

        if isinstance(item_wise, str):
            item_wise = frappe.parse_json(item_wise)

        for item_row, values in item_wise.items():

            tax_rate = flt(values[0]) if len(values) > 0 else 0
            tax_amount = flt(values[1]) if len(values) > 1 else 0

            grouped[item_row]["tax"] += tax_amount

            if tax_rate:
                grouped[item_row]["taxable"] += (tax_amount * 100 / tax_rate)

    for item in doc.items:

        data = grouped.get(item.name)

        if not data:
            continue

        taxable = flt(data["taxable"])

        rate = (data["tax"] / taxable) * 100 if taxable else 0

        results[item.name] = _prepare_tax_entry(
            doc,
            item,
            data["tax"],
            rate
        )

    return results
def _calculate_taxes_by_hierarchy(doc: "Document") -> dict:

    results = {}

    total_net = sum(flt(i.base_net_amount) for i in doc.items)

    template_rate = _get_sales_taxes_template_rate(doc.taxes_and_charges or "")

    has_item_templates = any(i.item_tax_template for i in doc.items)

    total_doc_tax = sum(flt(t.tax_amount) for t in (doc.taxes or []))

    for item in doc.items:

        base_net = flt(item.base_net_amount)

        rate = 0.0

        if item.item_tax_template:
            rate = _get_item_tax_template_rate(item.item_tax_template)

        elif template_rate:
            rate = template_rate

        elif total_net and total_doc_tax:
            rate = (total_doc_tax * base_net / total_net) / base_net * 100 if base_net else 0

        base_tax = (base_net * rate) / 100 if rate else 0

        results[item.name] = _prepare_tax_entry(doc, item, base_tax, rate)

    return results


def _get_sales_taxes_template_rate(template_name: str) -> float:
    """
    Fetches the total combined tax rate from a Sales Taxes and Charges Template.
    """
    if not template_name:
        return 0.0
    rates = frappe.get_all(
        "Sales Taxes and Charges", filters={"parent": template_name}, fields=["rate"]
    )
    return sum(float(r.rate or 0.0) for r in rates)


def _get_item_tax_template_rate(template_name: str) -> float:
    """
    Fetches the total combined tax rate from an Item Tax Template.
    """
    tax_template = frappe.get_doc("Item Tax Template", template_name)
    return (
        sum(float(tax.tax_rate or 0) for tax in tax_template.taxes)
        if tax_template.taxes
        else 0.0
    )
def _prepare_tax_entry(doc, item, base_tax, rate):

    company_currency = frappe.get_cached_value(
        "Company",
        doc.company,
        "default_currency"
    )

    conversion_rate = flt(doc.get("conversion_rate") or 1)

    is_foreign = doc.currency != company_currency

    # ✔ base_tax = company currency always
    base_tax_amount = round(base_tax, 2)

    # ✔ convert downward if foreign
    tax_amount = (
        round(base_tax / conversion_rate, 2)
        if is_foreign and conversion_rate
        else base_tax_amount
    )

    return {
        "tax_amount": tax_amount,
        "base_tax_amount": base_tax_amount,
        "tax_rate": round(rate, 2),
        "taxation_type_code": _determine_taxation_code(item, rate),
    }


def _calculate_document_level_taxes(
    doc: "Document",
    taxes: list
) -> None:
    """
    Fallback document-level tax calculation.
    """

    for item in doc.items:

        total_rate = 0
        total_tax = 0

        for tax in taxes:

            tax_rate = flt(tax.get("rate", 0))

            tax_amount = (
                flt(item.base_net_amount)
                * tax_rate
                / 100
            )

            total_rate += tax_rate
            total_tax += tax_amount

        item.custom_vat_tax_amount = round(total_tax, 2)
        item.custom_tax_rate = round(total_rate, 2)


def _apply_default_tax_from_settings(doc: "Document") -> None:
    """
    Final fallback using system settings.
    """

    settings_name = frappe.get_value(
        "ZRA SIS Settings",
        {"company_name": doc.company},
        "name"
    )

    if not settings_name:
        frappe.throw(
            f"No ZRA SIS Settings found for company {doc.company}"
        )

    settings = frappe.get_doc(
        "ZRA SIS Settings",
        settings_name
    )

    default_rate = flt(settings.default_tax_rate)

    if not default_rate:
        return

    for item in doc.items:

        tax_amount = (
            flt(item.base_net_amount)
            * default_rate
            / 100
        )

        item.custom_vat_tax_amount = round(tax_amount, 2)
        item.custom_tax_rate = round(default_rate, 2)


def _determine_taxation_code(
    item: object,
    rate: float
) -> str:
    """
    Determine VAT category code from:
    1. Item Tax Template metadata
    2. Effective tax rate fallback
    """

    if item.item_tax_template:

        code = frappe.db.get_value(
            "Item Tax Template",
            item.item_tax_template,
            "custom_taxation_type"
        )

        if code:
            return code

    rounded_rate = round(rate)

    if rounded_rate >= 16:
        return "A"

    if rounded_rate >= 8:
        return "E"

    if rounded_rate == 0:
        return "B"

    return "B"



def apply_item_taxes_and_codes(doc: "Document") -> None:

    tax_data_map = calculate_tax(doc)

    for item in doc.items:

        data = tax_data_map.get(item.name)

        if not data:
            continue

        item.tax_amount = data["tax_amount"]
        item.base_tax_amount = data["base_tax_amount"]
        item.tax_rate = data["tax_rate"]
        item.taxation_type_code = data["taxation_type_code"]

        frappe.db.set_value(
            item.doctype,
            item.name,
            {
                "custom_vat_tax_amount": data["tax_amount"],
                "custom_vat_taxable_amount": data["base_tax_amount"],
                "custom_tax_rate": data["tax_rate"],
                "custom_sis_vat_category_code": data["taxation_type_code"],
            },
            update_modified=False,
        )

def after_save(doc: "Document", method: str | None = None) -> None:
    """
    Hook executed after document save.
    Recomputes and applies item-level taxes.
    """

    if not doc.items:
        return

    apply_item_taxes_and_codes(doc)
