import frappe
from frappe.model.document import Document
from frappe.utils import getdate
from ..utils.tax_utils import calculate_tax
from datetime import datetime



def build_invoice_payload(invoice: "Document", settings_name: str) -> dict:
	

	customer = frappe.get_doc("Customer", invoice.customer)

	# Sale date
	sale_date = datetime.strptime(str(invoice.posting_date), "%Y-%m-%d").date()
	kind_of_sale = "NORMAL"
	if(invoice.tax_category).lower() in ["zero rated", "zero-rated", "zerorated"]:
		kind_of_sale = "EXPORT"
	# Determine sale kind
	elif customer.get("custom_is_lpo") and invoice.po_no:
		kind_of_sale = "LPO"

	if kind_of_sale and invoice.custom_kind_of_sale != kind_of_sale:
		frappe.db.set_value(
        invoice.doctype,
        invoice.name,
        "custom_kind_of_sale",
        kind_of_sale
    )
	invoice.custom_kind_of_sale = kind_of_sale 
	
	

	payload = {
		"kind": invoice.custom_kind_of_sale,
		"sale_date": sale_date.isoformat(),
		"currency_code": invoice.currency,
		"customer_tpin": "",
		"customer_name": "",
		"customer_phone":"",
		"customer_id": "",
		"trader_invoice_number": invoice.name,
		"payment_type_code": "01",
		"items": [],
	}

	# Exchange rate (required for foreign currency)
	if invoice.currency != frappe.defaults.get_global_default("currency"):
		payload["exchange_rate"] = invoice.conversion_rate

	# Export sale
	if invoice.custom_kind_of_sale == "EXPORT":
		payload["destination_country_code"] = invoice.get("custom_destination_country")

	# LPO sale
	if invoice.custom_kind_of_sale == "LPO":
		payload["lpo_number"] = invoice.get("po_no")

	# Discount
	if invoice.get("discount_amount"):
		payload["cash_discount_amount"] = round(invoice.discount_amount, 4)

	if invoice.get("additional_discount_percentage"):
		payload["cash_discount_rate"] = round(
			invoice.additional_discount_percentage / 100, 4
		)

	calculate_tax(invoice)

	
		# Items
	for item in invoice.items:
		# Correct tax field
		tax_amount = float(item.get("custom_vat_tax_amount") or 0)

		# Fetch remote ID
		item_doc = frappe.get_doc("Item", item.item_code)
		remote_id = item_doc.get("custom_smart_remote_id") or item.item_code

		qty = float(item.qty or 0)

		#  Base (net) unit price
		base_unit_price = float(item.get("base_net_rate") or item.rate or 0)

		# Tax per unit
		tax_per_unit = (tax_amount / qty) if qty else 0

		#  FINAL: Tax-inclusive unit price
		unit_price_incl_tax = round(base_unit_price + tax_per_unit, 4)

		# Discount
		discount_percentage = float(item.get("discount_percentage") or 0)
		discount_rate = round(discount_percentage / 100, 4)
		discount_amount = round(float(item.get("discount_amount") or 0), 4)

		#  Total should match tax-inclusive logic
		total_amount = round((unit_price_incl_tax * qty) - discount_amount, 4)

		payload["items"].append(
			{
				"item_id": remote_id,
				"quantity": qty,
				"unit_price": unit_price_incl_tax,
				"total_amount": total_amount,
				"package_unit_quantity": item.get("package_qty") or 1,
				"discount_rate": discount_rate,
				"discount_amount": discount_amount,
			}
		)

	return payload

def generate_vsdc_item_payload(item_name: str, settings_name: str) -> dict:
	item = frappe.get_doc("Item", item_name)

	def get_code(fieldname: str) -> str | None:
		if not item.get(fieldname):
			return None
		link_doctype = item.meta.get_field(fieldname).options
		link_value = item.get(fieldname)

		# map Item field → correct code field in linked Doctype
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

		# Fetch first settings record

	settings = frappe.get_doc("ZRA SIS Settings", settings_name)

	
	
	payload = {
		# "itemCd": item.custom_smart_item_code,  
		"item_class_code": get_code("custom_smart_item_classification_code"),
		"item_type_code": item.custom_smart_item_type_code,
		"item_name": item.item_name,
		# "itemStdNm": item.item_name,
		"origin_nation_code": get_code("custom_smart_origin_country_code"),
		"package_unit_code": get_code("custom_smart_packaging_unit_code"),
		"quantity_unit_code": get_code("custom_smart_quantity_unit_code"),
		"vat_category_code": get_code("custom_smart_tax_type_code"),
		"ipl_category_code": get_code("custom_smart_insurance_premium_levy") or "",
		"tl_category_code": get_code("custom_smart_tourism_levy") or "",
		"excise_category_code": get_code("custom_smart_excise_duty_category_code") or "",
		# "btchNo": item.get("batch_number") or None,
		"bar_code": item.get("barcode") or "",
		"default_unit_price": float(item.valuation_rate) or 1,
		"tot_category_code": item.get("custom_smart_turn_over_tax_category_code") or "",
		# "manufacturerItemCd": item.get("custom_manufacturer_item_code") or None,
		"recommended_retail_price": float(item.get("standard_rate") or 1),
		# "svcChargeYn": "Y" if item.get("is_service_charge_applicable") else "N",
		# "rentalYn": "Y" if item.get("custom_smart_rental_income_applicable") else "N",
		# "addInfo": item.get("additional_info") or None,
		"stock_quantity": float(item.get("opening_stock") or 0),
		"insurable":item.get("custom_smart_insurance_applicable") == "1"
		# "useYn": "Y" if item.disabled == 0 else "N",
		# "regrNm": frappe.session.user,
		# "regrId": frappe.session.user,
		# "modrNm": frappe.session.user,
		# "modrId": frappe.session.user,
	}

	return payload

def build_note_payload(doc, settings_name, note_type="credit", callback_url=None):
    """
    Build payload for Credit Note or Debit Note dynamically.

    note_type: "credit" | "debit"
    """

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
            "source_field": "return_against",  # adjust if needed
        },
    }

    config = field_map.get(note_type)
    if not config:
        frappe.throw(f"Unsupported note type: {note_type}")

    # Get original invoice
    original_invoice = frappe.get_doc("Sales Invoice", doc.get(config["source_field"]))

    payload = {
        config["date_field"]: getdate(doc.posting_date).strftime("%Y-%m-%d"),
        "sale_id": original_invoice.custom_sales_id,
        config["reason_field"]: doc.get("return_reason") or config["default_reason"],
        "trader_invoice_number": doc.name,
        "callback_url": callback_url or "",
        "items": [],
    }

    # Build items (shared logic)
    for item in doc.items:
        unit_price = float(item.rate or 0)
        quantity = int(round(abs(item.qty) or 0))
        discount_amount = float(item.discount_amount or 0)
        discount_rate = float(item.discount_percentage or 0)

        total_amount = round(abs((unit_price * quantity) - discount_amount), 2)
        package_unit_quantity = float(item.get("package_qty") or 1)

        payload["items"].append({
            "item_id": item.get("custom_sis_item_id") or item.item_code,
            "quantity": quantity,
            "unit_price": unit_price,
            "total_amount": total_amount,
            "package_unit_quantity": package_unit_quantity,
            "discount_rate": discount_rate,
            "discount_amount": discount_amount,
        })

    return payload

def generate_custom_item_code_smart(doc: Document) -> str:
	"""
	Generate smart item code in fixed format:
	    CC T PP QQ CCCC SSSSSSS

	    CC  = Country (2 chars)
	    T   = Item type (1 char)
	    PP  = Packaging unit (2 chars)
	    QQ  = Qty unit (2 chars)
	    CCCC = Classification code (4 chars, padded)
	    SSSSSSS = Running sequence (7 digits)
	"""

	# --- Extract fields ---
	country = (doc.get("custom_smart_origin_country_code") or "").upper().strip()[:2]
	item_type = (doc.get("custom_smart_item_type_code") or "").upper().strip()[:1]
	pkg_unit = (doc.get("custom_smart_packaging_unit_code") or "").upper().strip()[:2]
	qty_unit = (doc.get("custom_smart_quantity_unit_code") or "").upper().strip()[:2]
	class_code = (doc.get("custom_smart_item_classification_code") or "").strip()

	# --- Enforce padding ---
	country = country.ljust(2)
	item_type = item_type.ljust(1)
	pkg_unit = pkg_unit.ljust(2)
	qty_unit = qty_unit.ljust(
		2,
	)
	class_code = class_code.zfill(4)  # always 4 digits

	# --- Build prefix ---
	prefix = f"{country}{item_type}{pkg_unit}{qty_unit}{class_code}"

	# --- Determine suffix ---
	if doc.get("custom_smart_item_code"):
		suffix = doc.custom_smart_item_code[-7:]
	else:
		last = frappe.db.sql(
			"""
            SELECT custom_smart_item_code
            FROM `tabItem`
            WHERE custom_smart_item_classification_code = %s
              AND custom_smart_item_code IS NOT NULL
            ORDER BY CAST(RIGHT(custom_smart_item_code, 7) AS UNSIGNED) DESC
            LIMIT 1
            """,
			(doc.custom_smart_item_classification_code,),
		)

		if last:
			last_code = last[0][0]
			try:
				suffix = str(int(last_code[-7:]) + 1).zfill(7)
			except:
				suffix = "0000001"
		else:
			suffix = "0000001"

	# --- Final smart code ---
	new_code = f"{prefix}{suffix}"

	# Save
	doc.db_set("custom_smart_item_code", new_code, update_modified=False)

	frappe.logger().info(f"[SIS] Generated SIS Code: {new_code}")

	return new_code

import frappe


def build_customer_payload(doc) -> dict:
    """
    Build DigiTax customer payload.

    Expected output:
    {
        "customer_name": str,
        "customer_tin": str,
        "email": str | None,
        "is_lpo": bool,
        "phone": str,
        "address": str | None
    }
    """

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

    # ----------------------------
    # REQUIRED FIELD VALIDATION
    # ----------------------------
    if not payload["customer_name"]:
        frappe.throw("Customer Name is required")

    if not payload["customer_tin"]:
        frappe.throw(f"TIN is required for customer {doc.name}")

    if not payload["phone"]:
        frappe.throw(f"Phone number is required for customer {doc.name}")

    return payload

def get_customer_address(doc) -> str | None:
    """
    Fetch primary address for customer
    """

    address = frappe.db.get_value(
        "Address",
        {
            "link_doctype": "Customer",
            "link_name": doc.name,
            "is_primary_address": 1
        },
        ["address_line1", "address_line2", "city"],
        as_dict=True
    )

    if not address:
        return None

    return " ".join(filter(None, [
        address.address_line1,
        address.address_line2,
        address.city
    ]))