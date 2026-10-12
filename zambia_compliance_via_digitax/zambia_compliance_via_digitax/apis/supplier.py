"""Whitelisted Supplier actions for the Supplier form. Logic lives in services/suppliers.py."""

import frappe

from ..services import suppliers as supplier_service


def _check(supplier: str, permission: str) -> None:
	frappe.get_doc("Supplier", supplier).check_permission(permission)


@frappe.whitelist(methods=["POST"])
def register_supplier(supplier: str, settings_name: str | None = None) -> dict:
	"""Register the supplier with DigiTax (POST /suppliers)."""
	_check(supplier, "write")
	return supplier_service.register_supplier(supplier, settings_name)


@frappe.whitelist(methods=["POST"])
def refresh_supplier(supplier: str) -> dict:
	"""Read the supplier from DigiTax (GET /suppliers/{id}) and report differences."""
	_check(supplier, "read")
	return supplier_service.refresh_supplier(supplier)


@frappe.whitelist(methods=["POST"])
def link_existing_supplier(supplier: str, supplier_id: str, settings_name: str | None = None) -> dict:
	"""Link the supplier to one that already exists on DigiTax, by its DigiTax supplier ID (supplier_...)."""
	_check(supplier, "write")
	return supplier_service.link_existing_supplier(supplier, supplier_id, settings_name)
