from io import BytesIO

import frappe
import qrcode
from frappe.core.doctype.file.file import create_new_folder

QR_FOLDER = "Home/Smart_QR"


def generate_and_attach_qr_code(url: str, docname: str, doctype: str) -> str | None:
	"""Attach a QR code of a ZRA receipt URL to a document, once. Returns the file URL."""
	if not url:
		return None

	file_name = f"ZRA-QR-{docname}.png"
	existing = frappe.db.get_value(
		"File",
		{"attached_to_doctype": doctype, "attached_to_name": docname, "file_name": file_name},
		"file_url",
	)
	if existing:
		return existing

	qr = qrcode.QRCode(version=1, error_correction=qrcode.constants.ERROR_CORRECT_L, box_size=8, border=2)
	qr.add_data(url)
	qr.make(fit=True)
	buffer = BytesIO()
	qr.make_image(fill_color="black", back_color="white").save(buffer, format="PNG")

	create_new_folder("Smart_QR", "Home")
	file_doc = frappe.get_doc(
		{
			"doctype": "File",
			"file_name": file_name,
			"is_private": 0,
			"attached_to_doctype": doctype,
			"attached_to_name": docname,
			"content": buffer.getvalue(),
			"folder": QR_FOLDER,
		}
	)
	file_doc.save(ignore_permissions=True)
	return file_doc.file_url
