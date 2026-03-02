import frappe
from frappe.model.document import Document



def generate_vsdc_item_payload(item_name: str, bhfid, settings_name: str) -> dict:
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
			"custom_smart_origin_country_code": "class_code",
			"custom_smart_packaging_unit_code": "class_code",
			"custom_smart_quantity_unit_code": "class_code",
			"custom_smart_tax_type_code": "code",
			"custom_smart_insurance_premium_levy": "class_code",
			# "trade_levy_category": "class_code",
			"custom_smart_excise_duty_category_code": "class_code",
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
		# "item_name": item.item_name,
		# "itemStdNm": item.item_name,
		"origin_nation_code": get_code("custom_smart_origin_country_code"),
		"package_unit_code": get_code("custom_smart_packaging_unit_code"),
		"quantity_unit_code": get_code("custom_smart_quantity_unit_code"),
		"vat_category_code": get_code("custom_smart_tax_type_code"),
		"ipl_category_code": get_code("custom_smart_insurance_premium_levy"),
		"tl_category_code": get_code("custom_smart_tourism_levy"),
		"excise_category_code": get_code("custom_smart_excise_duty_category_code"),
		# "btchNo": item.get("batch_number") or None,
		"bar_code": item.get("barcode") or None,
		"default_unit_price": float(item.valuation_rate),
		"tot_category_code": item.get("custom_smart_turn_over_tax_category_code") or None,
		# "manufacturerItemCd": item.get("custom_manufacturer_item_code") or None,
		"recommended_retail_price": float(item.get("standard_rate") or 0),
		# "svcChargeYn": "Y" if item.get("is_service_charge_applicable") else "N",
		# "rentalYn": "Y" if item.get("custom_smart_rental_income_applicable") else "N",
		# "addInfo": item.get("additional_info") or None,
		"stock_quantity": float(item.get("opening_stock") or 0),
		"insurable": "Y" if item.get("custom_smart_insurance_applicable") else "N",
		# "useYn": "Y" if item.disabled == 0 else "N",
		# "regrNm": frappe.session.user,
		# "regrId": frappe.session.user,
		# "modrNm": frappe.session.user,
		# "modrId": frappe.session.user,
	}

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
