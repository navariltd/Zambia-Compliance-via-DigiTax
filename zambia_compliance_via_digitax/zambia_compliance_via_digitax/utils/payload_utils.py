import frappe
from frappe.model.document import Document


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
