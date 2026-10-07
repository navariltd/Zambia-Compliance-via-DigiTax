"""Whitelisted Customer actions for the Customer form. Logic lives in services/customers.py."""

import frappe

from ..services import customers as customer_service


def _check(customer: str, permission: str) -> None:
	frappe.get_doc("Customer", customer).check_permission(permission)


@frappe.whitelist(methods=["POST"])
def register_customer(customer: str, settings_name: str | None = None) -> dict:
	"""Register the customer with DigiTax (POST /customers)."""
	_check(customer, "write")
	return customer_service.register_customer(customer, settings_name)


@frappe.whitelist(methods=["POST"])
def refresh_customer(customer: str) -> dict:
	"""Read the customer from DigiTax (GET /customers/{id}) and report differences."""
	_check(customer, "read")
	return customer_service.refresh_customer(customer)


@frappe.whitelist(methods=["POST"])
def link_existing_customer(customer: str, customer_id: str, settings_name: str | None = None) -> dict:
	"""Link the customer to one that already exists on DigiTax, by its DigiTax customer ID (customer_...)."""
	_check(customer, "write")
	return customer_service.link_existing_customer(customer, customer_id, settings_name)
