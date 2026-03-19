import frappe
from .api_processor import process_request
from frappe.utils import get_datetime
from ..overrides.sales_invoice import generic_invoices_on_submit_override
from ..utils.qr_utils import generate_and_attach_qr_code


@frappe.whitelist()
def send_invoice_details(name: str) -> None:
	"""Manual trigger to push a Sales Invoice to Crystal VSDC."""
	doc = frappe.get_doc("Sales Invoice", name)

	# Skip opening entries
	if doc.is_opening == "Yes":
		return

	generic_invoices_on_submit_override(doc, "Sales Invoice")



@frappe.whitelist()
def get_invoice_details(
	document_name: str,
	invoice_type: str = "Sales Invoice",
	settings_name: str = None,
	company: str = None,
	**kwargs,
):
	
	# ---  Debug Logging: confirm arguments ---
	frappe.log_error(
		title="VSDC Invoice Details Debug",
		message=f"""
         get_vsdc_invoice_details() called with:
        - document_name: {document_name}
        - invoice_type: {invoice_type}
        - settings_name: {settings_name}
        - company: {company}
        - kwargs: {kwargs}
        """,
	)

	# --- Optional: handle 'kwargs' wrapping (if job was enqueued as string path) ---
	if not document_name and "kwargs" in kwargs:
		inner = kwargs.get("kwargs") or {}
		document_name = inner.get("document_name")
		invoice_type = inner.get("invoice_type", invoice_type)
		settings_name = inner.get("settings_name", settings_name)
		company = inner.get("company", company)

		frappe.log_error(
			title="VSDC Invoice Details (Recovered from kwargs)",
			message=f"Recovered args from kwargs → document_name={document_name}, invoice_type={invoice_type}, settings_name={settings_name}",
		)

	# --- Sanity check ---
	if not document_name:
		frappe.throw(" Missing document_name in get_vsdc_invoice_details()")

	# Fetch invoice
	invoice = frappe.get_doc(invoice_type, document_name)


	# Build payload
	payload = {
		
		"sale_id": invoice.custom_sales_id,
	}

	# Define success handler for response
	def on_success(response, **_):
		if not response:
			frappe.throw("Empty response from ZRA Smart Invoice system.")
	
		update_invoice_info(
			response=response,
			document_name=invoice.name,
			doctype=invoice.doctype,
		)

		frappe.msgprint(f"Invoice details synced successfully with ZRA for {invoice.name}")

	# Define error handler
	def on_error(response=None, **kwargs):
		frappe.log_error(title="VSDC API Error", message=f"Failed to fetch VSDC invoice details: {response}")

	# Use process_request to send the request
	process_request(
		request_data=payload,
		route_key="SelectInvoice",
		handler_function=on_success,
		request_method="GET",
		doctype=invoice_type,
		settings_name=settings_name,
		company=company or invoice.company,
		error_callback=on_error,
		document_name=document_name,
	)
	


def update_invoice_info(
	response: dict,
	document_name: str,
	doctype: str = "Sales Invoice",
	settings_name: str | None = None,
	**kwargs,
) -> None:
	"""
	Updates a Sales Invoice or Credit Note document with details from
	the Crystal VSDC (ZRA Smart Invoice) response.
	"""
	try:
		process_invoice_response(response, document_name, doctype)
		frappe.msgprint(f"ZRA Smart Invoice data synced for {document_name}")
	except Exception:
		frappe.log_error(frappe.get_traceback(), "Crystal VSDC Update Failed")
		frappe.throw("Failed to update document from ZRA response.")


def process_invoice_response(response: dict, document_name: str, doctype: str) -> None:
	"""
	Common handler to process ZRA Smart Invoice response
	and update ERPNext document fields.
	"""
	try:
		if not response:
			frappe.throw("Empty response received from ZRA Smart Invoice API.")

	

	
		data = response

		if not response:
			frappe.throw(f"Unexpected response format: {frappe.as_json(response)}")

		updates = {
			**map_vsdc_fields(data, document_name, doctype),
		}
		# Optional: capture error fields if failed
		# if not response.get("IsSuccess"):
		#     updates.update({
		#         "custom_submission_status": "Failed",
		#         "custom_zra_error": response.get("ErrorMessage"),
		#     })

		frappe.db.set_value(doctype, document_name, updates)
		frappe.db.commit()
		frappe.publish_realtime("refresh_form", document_name)
	except Exception as e:
		frappe.log_error(f"Invoice Update", str(e))
		frappe.throw(f"Failed to auto-submit to Digitax VSDC: {e}")




def map_vsdc_fields(data: dict, docname: str, doctype: str) -> dict:
	"""
	Map Digitax VSDC (ZRA Smart Invoice) response data
	to ERPNext custom fields.
	"""
	if not data:
		return {}

	qr_url = data.get("receipt_url")

	image_url = generate_and_attach_qr_code(qr_url, docname, doctype) if qr_url else None

	return {
		
       
        "custom_receipt_number": data.get("receipt_number"),
        "custom_internal_data": data.get("internal_data"),
        "custom_receipt_signature": data.get("receipt_signature"),
        "custom_receipt_url": data.get("receipt_url"),
         "custom_qr_code": image_url,
        "custom_submission_status": data.get("status"),
        "custom_sale_date": data.get("sale_date"),

	}
