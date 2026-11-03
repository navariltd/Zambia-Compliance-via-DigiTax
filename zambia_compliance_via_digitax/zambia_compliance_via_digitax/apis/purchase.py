"""Whitelisted purchase actions. Logic lives in services/purchases.py."""

import frappe

from ..doctype.doctype_names_mapping import PURCHASE_DOCTYPE_NAME, SETTINGS_DOCTYPE_NAME
from ..services import purchases as purchase_service


def _check(name: str, permission: str) -> None:
	frappe.get_doc(PURCHASE_DOCTYPE_NAME, name).check_permission(permission)


@frappe.whitelist(methods=["POST"])
def fetch_purchases(settings_name: str) -> dict:
	"""Pull purchases from DigiTax (GET /purchases, all pages)."""
	frappe.get_doc(SETTINGS_DOCTYPE_NAME, settings_name).check_permission("read")
	frappe.has_permission(PURCHASE_DOCTYPE_NAME, "write", throw=True)
	return purchase_service.fetch_purchases(settings_name)


@frappe.whitelist(methods=["POST"])
def refresh_purchase(name: str) -> dict:
	"""Re-read the purchase from DigiTax (GET /purchases/{id})."""
	_check(name, "write")
	return purchase_service.refresh_purchase(name)


@frappe.whitelist(methods=["POST"])
def approve_purchase(name: str) -> dict:
	"""Link the lines to our items and approve (PUT link-items, then PUT review)."""
	_check(name, "write")
	return purchase_service.approve_purchase(name)


@frappe.whitelist(methods=["POST"])
def reject_purchase(name: str, comment: str) -> dict:
	"""Reject the purchase (PUT review with reject = true)."""
	_check(name, "write")
	return purchase_service.reject_purchase(name, comment)


@frappe.whitelist(methods=["POST"])
def create_supplier(name: str) -> str:
	"""Create the ERPNext Supplier from the purchase's DigiTax supplier."""
	_check(name, "write")
	frappe.has_permission("Supplier", "create", throw=True)
	return purchase_service.create_supplier(name)


@frappe.whitelist(methods=["POST"])
def create_purchase_invoice(name: str) -> str:
	"""Create a draft Purchase Invoice from an approved purchase."""
	_check(name, "write")
	frappe.has_permission("Purchase Invoice", "create", throw=True)
	return purchase_service.create_purchase_invoice(name)


@frappe.whitelist(methods=["POST"])
def report_manual_purchase(purchase_invoice: str) -> dict:
	"""Report a submitted Purchase Invoice to ZRA as a manual purchase (POST /purchases)."""
	frappe.get_doc("Purchase Invoice", purchase_invoice).check_permission("submit")
	return purchase_service.report_manual_purchase(purchase_invoice)
