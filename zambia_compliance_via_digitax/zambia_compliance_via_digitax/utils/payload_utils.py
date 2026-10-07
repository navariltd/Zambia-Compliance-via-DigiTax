import frappe
from frappe.model.document import Document
from frappe.utils import getdate
from datetime import datetime
from urllib.parse import urlparse

from frappe.utils import get_url
from frappe.utils import  now_datetime, add_to_date

from .settings_utils import get_settings

from datetime import datetime


def build_invoice_payload(invoice: "Document", settings_name: str) -> dict:
    customer = frappe.get_doc("Customer", invoice.customer)

    sale_date = datetime.strptime(
        str(invoice.posting_date), "%Y-%m-%d"
    ).date()

    # ---------------------------
    # Sale type
    # ---------------------------
    kind_of_sale = "NORMAL"
    tax_category = (invoice.tax_category or "").lower()

    if tax_category in ["zero rated", "zero-rated", "zerorated"]:
        kind_of_sale = "EXPORT"
    elif customer.get("custom_is_lpo") and invoice.po_no:
        kind_of_sale = "LPO"

    if invoice.custom_kind_of_sale != kind_of_sale:
        frappe.db.set_value(
            invoice.doctype,
            invoice.name,
            "custom_kind_of_sale",
            kind_of_sale,
            update_modified=False,
        )

    invoice.custom_kind_of_sale = kind_of_sale

    # ---------------------------
    # Currency handling
    # ---------------------------
    currency = invoice.currency

    company_currency = frappe.get_value(
        "Company",
        invoice.company,
        "default_currency",
    )

    conversion_rate = 1
    rate_field = "net_rate"
    tax_field = "custom_vat_tax_amount"

    if currency != company_currency:
        conversion_rate, used_rate = get_zmw_conversion_rate(
            currency=currency,
            company_currency=company_currency,
            posting_date=invoice.posting_date,
        )

        if used_rate != "net":
            rate_field = "base_net_rate"
            tax_field = "base_tax_amount"

    # ---------------------------
    # Payload base
    # ---------------------------
    payload = {
        "kind": invoice.custom_kind_of_sale,
        "sale_date": sale_date.isoformat(),
        "currency_code": currency,
        "customer_name": customer.customer_name,
        "trader_invoice_number": invoice.name,
        "payment_type_code": "01",
        "callback_url": build_callback_url(
            "zambia_compliance_via_digitax.zambia_compliance_via_digitax.apis.sales_invoice.invoice_submission_callback",
            settings_name,
        ),
        "items": [],
    }

    # Optional customer fields: DigiTax rejects null values, so only send them when set
    optional_customer_fields = {
        "customer_tpin": customer.get("tax_id"),
        "customer_phone": customer.get("mobile_no"),
        "customer_id": customer.get("custom_sis_customer_id"),
    }
    for field, value in optional_customer_fields.items():
        if value:
            payload[field] = str(value).strip()

    # ---------------------------
    # Extra fields
    # ---------------------------
    if currency != company_currency:
        payload["exchange_rate"] = invoice.conversion_rate

    if kind_of_sale == "EXPORT":
        payload["destination_country_code"] = invoice.get("custom_destination_country")

    if kind_of_sale == "LPO":
        payload["lpo_number"] = invoice.get("po_no")

    if invoice.get("discount_amount"):
        payload["cash_discount_amount"] = round(float(invoice.discount_amount or 0), 4)

    if invoice.get("additional_discount_percentage"):
        payload["cash_discount_rate"] = round(
            float(invoice.additional_discount_percentage or 0) / 100,
            4,
        )

    # ---------------------------
    # ITEM BUILD (FIXED TAX INCLUSIVITY)
    # ---------------------------
    items = []

    for item in invoice.items:
        qty = float(item.qty or 0)

        if not qty:
            continue

        base_unit_price = float(item.get(rate_field) or 0)

        # total tax on item
        total_tax = float(item.get(tax_field) or 0)

        # per-unit tax
        unit_tax = total_tax / qty if qty else 0

       
        unit_price_inclusive = round(
            (base_unit_price + unit_tax) * conversion_rate,
            4,
        )

        # total inclusive
        total_amount = round(
            (unit_price_inclusive * qty)
            - float(item.get("discount_amount") or 0),
            4,
        )

        items.append(
            {
                "item_id": (
                    frappe.get_value(
                        "Item",
                        item.item_code,
                        "custom_smart_remote_id",
                    )
                    or item.item_code
                ),
                "quantity": qty,
                "unit_price": unit_price_inclusive,
                "total_amount": total_amount,
                "package_unit_quantity": item.get("package_qty") or 1,
                "discount_rate": round(
                    float(item.get("discount_percentage") or 0) / 100,
                    4,
                ),
                "discount_amount": float(item.get("discount_amount") or 0),
            }
        )

    payload["items"] = items

    return payload

def build_callback_url(endpoint: str, settings_name: str | None = None) -> str:
    """
    Build a full callback URL that works both inside and outside request context.

    For sandbox settings with a Callback Base URL (e.g. an ngrok tunnel to a
    localhost site), that base is used. Otherwise the site's own URL is used.
    """

    settings = get_settings(settings_name)
    if (
        settings
        and settings.get("environment") == "Sandbox"
        and settings.get("callback_base_url")
    ):
        return f"{settings.callback_base_url.rstrip('/')}/api/method/{endpoint}"

    base_url = get_url()

    parsed_url = urlparse(base_url)

    # Optional cleanup for localhost / IP cases
    if parsed_url.hostname:
        if (
            parsed_url.hostname == "localhost"
            or parsed_url.hostname.replace(".", "").isdigit()
        ):
            base_url = f"{parsed_url.scheme}://{parsed_url.netloc}"

    return f"{base_url}/api/method/{endpoint}"

def build_note_payload(doc, settings_name, note_type="credit", callback_url=None):
    # Map dynamic fields
    field_map = {
        "credit": {
            "date_field": "return_date",
            "reason_field": "refund_reason_code",
            "default_reason": "01",
            "source_field": "return_against",
        },
        "debit": {
            "date_field": "debit_date",
            "reason_field": "debit_reason_code",
            "default_reason": "01",
            "source_field": "return_against",
        },
    }

    config = field_map.get(note_type)
    if not config:
        frappe.throw(f"Unsupported note type: {note_type}")

    # Get original invoice
    original_invoice = frappe.get_doc(
        "Sales Invoice", doc.get(config["source_field"])
    )

    payload = {
        config["date_field"]: getdate(doc.posting_date).strftime("%Y-%m-%d"),
        "sale_id": original_invoice.custom_sales_id,
        config["reason_field"]: doc.get("return_reason") or config["default_reason"],
        "trader_invoice_number": doc.name,
        "callback_url": callback_url or build_callback_url(
            "zambia_compliance_via_digitax.zambia_compliance_via_digitax.apis.sales_invoice.invoice_submission_callback",
            settings_name,
        ),
        "items": [],
    }
    payload["items"] = [
    {
        "item_id": item.get("custom_sis_item_id"),
        "quantity": abs(int(item.qty or 0)),
        "unit_price": round(
            float(item.get("base_net_rate") or item.rate or 0)
            + (float(item.get("custom_vat_tax_amount") or 0) / float(item.qty or 1)),
            4
        ),
        "total_amount": round(
            (
                float(item.get("base_net_rate") or item.rate or 0)
                + (float(item.get("custom_vat_tax_amount") or 0) / float(item.qty or 1))
            )
            * abs(float(item.qty or 0))
            - float(item.get("discount_amount") or 0),
            4
        ),
        "package_unit_quantity": item.get("package_qty") or 1,
        "discount_rate": round(float(item.get("discount_percentage") or 0) / 100, 4),
        "discount_amount": float(item.get("discount_amount") or 0),
    }
    for item in doc.items
]
    return payload

def get_zmw_conversion_rate(currency, company_currency, posting_date=None):
 
    if not posting_date:
        posting_date = frappe.utils.nowdate()

    def get_rate(frm, to):
        return frappe.db.get_value(
            "Currency Exchange",
            {
                "from_currency": frm,
                "to_currency": to,
                "date": ["<=", posting_date],
                "for_selling": 1,
            },
            "exchange_rate",
            order_by="date desc",
        )

    rate = get_rate(currency, "ZMW")
    if rate:
        return rate, "net"

    rate = get_rate(company_currency, "ZMW")
    if rate:
        return rate, "base"
    frappe.throw(
        f"No exchange rate found to ZMW for {currency} or {company_currency} on {posting_date}"
    )
