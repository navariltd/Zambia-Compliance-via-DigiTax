from collections import defaultdict

import frappe
from frappe.model.document import Document
from frappe.utils import flt



def calculate_tax(doc: Document) -> dict:
    """
    Orchestrates the tax calculation process by deciding between ERPNext's
    internal tax table or the custom hierarchical resolution engine.
    """
    tax_table = doc.get("item_wise_tax_details", [])

    if tax_table:
        return _calculate_from_item_wise_tax_table(doc)

    return _calculate_taxes_by_hierarchy(doc)


def _calculate_taxes_by_hierarchy(doc: "Document") -> dict:
    """
    Resolves tax rates for each item using a hierarchical priority:
    1. Item Tax Template
    2. Document-level Sales Taxes and Charges Template
    3. Proportional distribution of manual tax entries
    """
    results = {}
    total_net = sum(float(i.base_net_amount or 0) for i in doc.items)
    template_rate = _get_sales_taxes_template_rate(doc.taxes_and_charges)
    has_item_templates = any(i.item_tax_template for i in doc.items)

    for item in doc.items:
        rate = 0.0
        base_net = float(item.base_net_amount or 0.0)

        if item.item_tax_template:
            rate = _get_item_tax_template_rate(item.item_tax_template)
        elif template_rate > 0:
            rate = template_rate
        elif not has_item_templates and doc.get("taxes") and total_net > 0:
            total_doc_tax = sum(float(t.tax_amount or 0) for t in doc.taxes)
            item_tax = total_doc_tax * (base_net / total_net)
            rate = (item_tax / base_net) * 100 if base_net else 0.0

        base_tax = (base_net * rate) / 100.0
        results[item.name] = _prepare_tax_entry(doc, item, base_tax, rate)

    return results

def _calculate_from_item_wise_tax_table(doc: "Document") -> dict:
    """
    Extract and aggregate taxes from ERPNext's
    item-wise tax calculation table.
    """

    results = {}

    grouped = defaultdict(
        lambda: {
            "tax": 0.0,
            "taxable": 0.0
        }
    )

    taxes = doc.get("taxes", []) or []

    if not taxes:
        return results

    # -----------------------------------------
    # Aggregate ERP-calculated tax rows
    # -----------------------------------------
    for tax in taxes:

        item_wise = tax.get("item_wise_tax_detail")

        if not item_wise:
            continue

        # item_wise_tax_detail may be stringified JSON
        if isinstance(item_wise, str):
            item_wise = frappe.parse_json(item_wise)

        for item_row, values in item_wise.items():

            # ERPNext format:
            # [tax_rate, tax_amount]
            tax_rate = flt(values[0]) if len(values) > 0 else 0
            tax_amount = flt(values[1]) if len(values) > 1 else 0

            grouped[item_row]["tax"] += tax_amount

            # derive taxable amount safely
            if tax_rate:
                grouped[item_row]["taxable"] += (
                    tax_amount * 100 / tax_rate
                )

    # -----------------------------------------
    # Normalize per item
    # -----------------------------------------
    for item in doc.items:

        data = grouped.get(item.name)

        if not data:
            continue

        taxable_amount = flt(data["taxable"])

        effective_rate = (
            (data["tax"] / taxable_amount) * 100
            if taxable_amount
            else 0
        )

        results[item.name] = _prepare_tax_entry(
            doc=doc,
            item=item,
            base_tax=data["tax"],
            rate=effective_rate
        )

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
def _prepare_tax_entry(
    doc: "Document",
    item: object,
    base_tax: float,
    rate: float
) -> dict:
    """
    Normalize tax values and handle
    currency conversion.
    """

    conversion_rate = flt(doc.get("conversion_rate", 1.0))

    company_currency = frappe.get_cached_value(
        "Company",
        doc.company,
        "default_currency"
    )

    is_foreign_currency = doc.currency != company_currency

    tax_amount = (
        base_tax / conversion_rate
        if is_foreign_currency
        else base_tax
    )

    return {
        "tax_amount": round(tax_amount, 2),
        "base_tax_amount": round(base_tax, 2),
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
    """
    Applies calculated tax data to document items and updates both
    in-memory values and database records.
    """

    tax_data_map = calculate_tax(doc)

    for item in doc.items:

        data = tax_data_map.get(item.name)

        if not data:
            continue

        # Update in-memory document
        item.tax_amount = data.get("tax_amount", 0)
        item.base_tax_amount = data.get("base_tax_amount", 0)
        item.tax_rate = data.get("tax_rate", 0)
        item.taxation_type_code = data.get("taxation_type_code")

        # Persist to DB
        frappe.db.set_value(
            item.doctype,
            item.name,
            {
                "tax_amount": item.tax_amount,
                "base_tax_amount": item.base_tax_amount,
                "tax_rate": item.tax_rate,
                "taxation_type_code": item.taxation_type_code,
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
