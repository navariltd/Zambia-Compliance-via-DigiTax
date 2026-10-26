"""Whitelisted notice actions for the ZRA SIS Settings form. Logic lives in services/notices.py."""

import frappe

from ..doctype.doctype_names_mapping import SETTINGS_DOCTYPE_NAME
from ..services import notices as notice_service


@frappe.whitelist(methods=["POST"])
def fetch_notices(settings_name: str) -> dict:
	"""Pull ZRA notices for the business (GET /notices)."""
	frappe.get_doc(SETTINGS_DOCTYPE_NAME, settings_name).check_permission("read")
	return notice_service.fetch_notices(settings_name)
