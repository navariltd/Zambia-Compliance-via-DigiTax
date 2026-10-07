"""Whitelisted Item actions for the Item form. Logic lives in services/items.py."""

import frappe

from ..services import items as item_service


def _get_item(item: str, permission: str):
	doc = frappe.get_doc("Item", item)
	doc.check_permission(permission)
	return doc


@frappe.whitelist(methods=["POST"])
def register_item(item: str, settings_name: str | None = None) -> dict:
	"""Register the item with DigiTax (POST /items)."""
	_get_item(item, "write")
	return item_service.register_item(item, settings_name)


@frappe.whitelist(methods=["POST"])
def refresh_item(item: str) -> dict:
	"""Pull the item's status and ZRA item code from DigiTax (GET /items/{id})."""
	_get_item(item, "read")
	return item_service.refresh_item(item)


@frappe.whitelist(methods=["POST"])
def update_item(item: str) -> dict:
	"""Push the item name and default price to DigiTax (PUT /items/{id})."""
	_get_item(item, "write")
	return item_service.update_item(item)


@frappe.whitelist(methods=["POST"])
def link_existing_item(item: str, item_id: str, settings_name: str | None = None) -> dict:
	"""Link the item to one that already exists on DigiTax, by its DigiTax item ID (item_...)."""
	_get_item(item, "write")
	return item_service.link_existing_item(item, item_id, settings_name)


@frappe.whitelist(allow_guest=True, methods=["POST"])
def item_registration_callback(**kwargs) -> None:
	"""DigiTax calls this when an item's status changes.

	The body is not trusted: it is only used to find the item, which is then re-read from DigiTax.
	"""
	data = kwargs.get("data") if isinstance(kwargs.get("data"), dict) else kwargs
	remote_id = data.get("id")
	if not remote_id or not isinstance(remote_id, str):
		return

	item_name = frappe.db.get_value("Item", {"custom_smart_remote_id": remote_id}, "name")
	if item_name:
		item_service.enqueue_item_job(item_service.REFRESH_JOB, item_name)
