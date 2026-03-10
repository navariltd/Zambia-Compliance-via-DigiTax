import json
from datetime import datetime

import frappe
from frappe import _
from frappe.utils.background_jobs import enqueue
from ..utils.payload_utils import generate_vsdc_item_payload
from ..utils.settings_utils import get_settings
from ..utils.payload_utils import (
	generate_custom_item_code_smart,
)
from .response_handlers import (item_search_on_success,handle_update_response,handle_registration_response)
from ..apis.api_processor import process_request
from ..utils.routes_utils import get_route_path


@frappe.whitelist()
def update_item(doc, method=None, settings_name=None, branch=None) -> dict | None:
	"""Update Item details in Digitax API."""
	import json

	# Convert string from JS to dict
	if isinstance(doc, str):
		doc = json.loads(doc)

	# Extract item name
	if isinstance(doc, dict):
		docname = doc.get("name")
	else:
		docname = getattr(doc, "name", None)

	if not docname:
		frappe.throw("No Item name provided for update.")

	item = frappe.get_doc("Item", docname)

	if not is_item_eligible_for_registration(item):
		return None

	# Build payload matching Digitax API
	payload = {
		"item_id":item.get("custom_smart_remote_id"),
		"item_name": item.item_name,
		
		"default_unit_price": item.valuation_rate or 0,
		"recommended_retail_price": item.standard_rate or 0
	}

	# Item ID stored in custom field (adjust if different)
	

	

	frappe.enqueue(
		process_request,
		queue="default",
		is_async=True,
		request_data=payload,
		route_key="updateItem",
		handler_function=handle_update_response,
		request_method="PUT",
		doctype="Item",
		document_name=item.name,
	)

	return {"queued": True, "item": item.name}

@frappe.whitelist()
def perform_item_registration(
	doc,
	settings_name: str | None = None,
	branch: str | None = None,
	branch_code: str | None = None,
	method: str | None = None,
) -> dict | None:
	"""
	Register an Item with the Smart Invoice System (ZRA SIS).
	Item lookup logic has been intentionally removed.
	"""

	# Handle both dict and JSON string inputs
	if isinstance(doc, str):
		doc = json.loads(doc)

	if isinstance(doc, dict):
		docname = doc.get("name")
	else:
		docname = getattr(doc, "name", None)

	if not docname:
		frappe.throw(_("No Item name provided for registration."))

	item = frappe.get_doc("Item", docname)

	# Fetch settings
	settings = get_settings(settings_name)
	if not settings:
		frappe.throw(_("No active ZRA SIS API Settings found."))

	settings_name = settings.get("name")

	# Validate eligibility
	if not is_item_eligible_for_registration(item):
		frappe.msgprint(_("Item not eligible for registration."), alert=True)
		return None

	# Validate required fields
	missing_fields = validate_required_fields(item)
	if missing_fields:
		frappe.msgprint(
			_("Missing required fields: {0}").format(", ".join(missing_fields)),
			alert=True,
		)
		return None

	# Update Item Tax Templates if VAT category changed
	if hasattr(item, "has_value_changed"):
		is_tax_type_changed = item.has_value_changed("custom_smart_tax_type_code")
	else:
		is_tax_type_changed = True

	if item.custom_smart_tax_type_code and is_tax_type_changed:
		relevant_tax_templates = frappe.get_all(
			"Item Tax Template",
			filters={"custom_taxation_type": item.custom_smart_tax_type_code},
			fields=["name"],
		)

		if relevant_tax_templates:
			item.set("taxes", [])
			for template in relevant_tax_templates:
				item.append(
					"taxes",
					{"item_tax_template": template.name},
				)

	# Generate Smart Item Code if missing
	if not item.custom_smart_item_code:
		generate_and_set_smart_code(item)

	item.save(ignore_permissions=True)

	# Enqueue direct registration (no lookup)
	enqueue(
		"zambia_compliance_via_digitax.zambia_compliance_via_digitax.apis.item._process_item_registration",
		queue="default",
		job_name=f"[SMART] Register item {item.name}",
		timeout=300,
		item_name=item.name,
		branch=branch,
		branch_code=branch_code,
		settings_name=settings_name,
	)

	return {
		"queued": True,
		"item": item.name,
		"message": _("Item registration has been queued for Smart Invoice System."),
	}


@frappe.whitelist()
def fetch_item_details(item_id: str,settings_name: str = None) -> None:
	"""Fetch Item details from Smart Zambia API."""
	settings = get_settings(settings_name)
	if not settings:
		frappe.throw(_("No active Smart API Settings found"))


	payload = {
		"item_id": item_id,
	}

	frappe.enqueue(
		process_request,
		queue="default",
		is_async=True,
		request_data=payload,
		route_key="selectItem",
		handler_function=item_search_on_success,
		request_method="GET",
		doctype="Item",
		settings_name=settings["name"],
	)
	return {"queued": True, "item": item_id}




def _process_item_registration(
	item_name: str,
	settings_name: str,
	branch: str | None = None,
	branch_code: str | None = None,
):
	"""
	Background job:
	Registers item to Smart Invoice System (ZRA SIS)
	for one or multiple branches.
	"""

	try:
		item = frappe.get_doc("Item", item_name)

		settings = get_settings(settings_name)
		if not settings:
			frappe.log_error(
				title="[SMART] Missing Settings",
				message=f"No Smart API Settings found for {settings_name}",
			)
			return

		settings_name = settings.get("name")


		
		# Generate payload
		request_data = generate_vsdc_item_payload(
				item.name,
			
				settings_name,
			)
		response = process_request(
				doctype="Item",
				request_data=request_data,
				route_key="saveItem",
				handler_function=handle_registration_response,
				request_method="POST",
				# branch=branch_name,
				settings_name=settings_name,
				 document_name=item.name, 
			)

		frappe.logger().info(
				f"[SMART] Response for item {item.name} "
				# f"branch {branch_name}: {response}"
			)

	except Exception:
		frappe.log_error(
			title="[SMART] Item Registration Failed",
			message=frappe.get_traceback(),
		)
	
def validate_required_fields(item) -> list:
	"""Validate required fields for item registration"""
	required_fields = [
		"custom_smart_item_classification_code",
		"custom_smart_item_type_code",
		"custom_smart_origin_country_code",
		"custom_smart_packaging_unit_code",
		"custom_smart_quantity_unit_code",
		"custom_smart_tax_type_code",
	]
	return [field for field in required_fields if not item.get(field)]



def generate_and_set_smart_code(item) -> None:
	"""Generate and set Smart code for item"""
	item.custom_smart_item_code = generate_custom_item_code_smart(item)
	frappe.db.set_value("Item", item.name, "custom_smart_item_code", item.custom_smart_item_code)
	frappe.db.commit()


def is_item_eligible_for_registration(item) -> bool:
	"""Check if item can be registered in ZRA SIS."""
	return not (item.get("custom_prevent_smart_registration") or item.disabled)
