import frappe
from frappe.model.document import Document
from frappe.utils import getdate
from datetime import datetime
from urllib.parse import urlparse

from frappe.utils import get_url
from frappe.utils import  now_datetime, add_to_date

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
        "customer_tin": frappe.get_value("Customer", invoice.customer, "tax_id"),
        "customer_name": customer.customer_name,
        "customer_phone": customer.get("mobile_no") or "",
        "customer_id": frappe.get_value("Customer", invoice.customer, "custom_sis_customer_id") or "",
        "trader_invoice_number": invoice.name,
        "payment_type_code": "01",
        "callback_url": build_callback_url(
            "zambia_compliance_via_digitax.zambia_compliance_via_digitax.apis.sales_invoice.invoice_submission_callback"
        ),
        "items": [],
    }

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

def generate_vsdc_item_payload(item_name: str, settings_name: str) -> dict:
	item = frappe.get_doc("Item", item_name)
	def get_code(fieldname: str) -> str | None:
		if not item.get(fieldname):
			return None
		link_doctype = item.meta.get_field(fieldname).options
		link_value = item.get(fieldname)
		field_map = {
			"custom_smart_item_classification_code": "item_cls_cd",
			"custom_smart_item_type_code": "class_code",
			"custom_smart_origin_country_code": "code",
			"custom_smart_packaging_unit_code": "code",
			"custom_smart_quantity_unit_code": "code",
			"custom_smart_tax_type_code": "tax_category_code",
			"custom_smart_insurance_premium_levy": "code",
			# "trade_levy_category": "class_code",
			"custom_smart_excise_duty_category_code": "code",
			# "rental_income_status": "class_code",
			"custom_smart_insurance_applicable": "class_code",
		}

		code_field = field_map.get(fieldname, "code")  # fallback to `code` if unsure
		return frappe.db.get_value(link_doctype, link_value, code_field)
	payload = { 
		"item_class_code": get_code("custom_smart_item_classification_code"),
		"item_type_code": item.custom_smart_item_type_code,
		"item_name": item.item_name,
		"origin_nation_code": get_code("custom_smart_origin_country_code"),
		"package_unit_code": get_code("custom_smart_packaging_unit_code"),
		"quantity_unit_code": get_code("custom_smart_quantity_unit_code"),
		"vat_category_code": get_code("custom_smart_tax_type_code"),
		"ipl_category_code": get_code("custom_smart_insurance_premium_levy") or "",
		"tl_category_code": get_code("custom_smart_tourism_levy") or "",
		"excise_category_code": get_code("custom_smart_excise_duty_category_code") or "",
		"bar_code": item.get("barcode") or "",
		"default_unit_price": float(item.valuation_rate) or 1,
		"tot_category_code": item.get("custom_smart_turn_over_tax_category_code") or "",
		"recommended_retail_price": float(item.get("standard_rate") or 0),
		"stock_quantity": float(item.get("opening_stock") or 0),
		"insurable":item.get("custom_smart_insurance_applicable") == "1",
		"callback_url": build_callback_url(
            "zambia_compliance_via_digitax.zambia_compliance_via_digitax.apis.item.item_registration_callback("
        ),
	
	}
	return payload




def build_callback_url(endpoint: str) -> str:
    """
    Build a full callback URL that works both inside and outside request context.
    """

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
        "callback_url": callback_url or "",
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

def build_customer_payload(doc) -> dict:
    if not doc:
        frappe.throw("Customer document is required")
    payload = {
        "customer_name": doc.customer_name,
        "customer_tin": doc.tax_id,
        "email": doc.email_id or None,
        "is_lpo": bool(getattr(doc, "custom_is_lpo", 0)),
        "phone": doc.mobile_no or "",
        "address": get_customer_address(doc)
    }
    if not payload["customer_name"]:
        frappe.throw("Customer Name is required")
    if not payload["customer_tin"]:
        frappe.throw(f"TIN is required for customer {doc.name}")
    if not payload["phone"]:
        frappe.throw(f"Phone number is required for customer {doc.name}")
    return payload

def get_customer_address(doc) -> str | None:

    address = frappe.db.get_value(
        "Address",
        {
            "link_doctype": "Customer",
            "link_name": doc.name,
            "is_primary_address": 1,
        },
        ["address_line1", "address_line2", "city"],
        as_dict=True,
    )

    if not address:
        return None

    return " ".join(
        part for part in [
            address.address_line1,
            address.address_line2,
            address.city,
        ]
        if part
    )

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
