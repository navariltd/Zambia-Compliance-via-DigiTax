"""Whitelisted stock actions for the Item and ZRA SIS Settings forms. Logic lives in services/stock.py."""

import frappe

from ..doctype.doctype_names_mapping import SETTINGS_DOCTYPE_NAME
from ..services import stock as stock_service


@frappe.whitelist(methods=["POST"])
def compare_item_stock(item: str) -> dict:
	"""ERPNext vs DigiTax stock balance for the item (GET /items/{id})."""
	frappe.get_doc("Item", item).check_permission("read")
	return stock_service.compare_item_stock(item)


@frappe.whitelist(methods=["POST"])
def sync_item_stock(item: str) -> dict:
	"""Send the difference between ERPNext's and DigiTax's balance (PUT /stock/adjust)."""
	frappe.get_doc("Item", item).check_permission("write")
	return stock_service.sync_item_stock(item)


@frappe.whitelist(methods=["POST"])
def sync_all_stock(settings_name: str) -> None:
	"""Queue a balance sync for every registered stock item under the settings."""
	frappe.get_doc(SETTINGS_DOCTYPE_NAME, settings_name).check_permission("write")
	stock_service.enqueue_sync_all_stock(settings_name)
