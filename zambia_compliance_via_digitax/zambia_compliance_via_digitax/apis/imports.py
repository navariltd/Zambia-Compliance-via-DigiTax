"""Whitelisted import actions. Logic lives in services/imports.py."""

import frappe

from ..doctype.doctype_names_mapping import IMPORT_DOCTYPE_NAME, SETTINGS_DOCTYPE_NAME
from ..services import imports as import_service


def _check(name: str, permission: str) -> None:
	frappe.get_doc(IMPORT_DOCTYPE_NAME, name).check_permission(permission)


@frappe.whitelist(methods=["POST"])
def fetch_imports(settings_name: str) -> dict:
	"""Pull import items from DigiTax (GET /imports, all pages)."""
	frappe.get_doc(SETTINGS_DOCTYPE_NAME, settings_name).check_permission("read")
	frappe.has_permission(IMPORT_DOCTYPE_NAME, "write", throw=True)
	return import_service.fetch_imports(settings_name)


@frappe.whitelist(methods=["POST"])
def approve_import(name: str, remarks: str | None = None) -> dict:
	"""Link the import to its Item and approve (PUT link, then PUT review)."""
	_check(name, "write")
	return import_service.approve_import(name, remarks)


@frappe.whitelist(methods=["POST"])
def reject_import(name: str, remarks: str) -> dict:
	"""Reject the import (PUT review with reject = true)."""
	_check(name, "write")
	return import_service.reject_import(name, remarks)
