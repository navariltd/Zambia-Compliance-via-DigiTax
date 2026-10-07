from typing import Any
from .settings_utils import get_settings
import frappe


@frappe.whitelist()
def get_active_smart_settings() -> list[dict]:
	"""
	Return all active Smart Zambia settings records (multi-company).
	Each record represents a company setup.
	"""
	settings = frappe.get_all(
		"ZRA SIS Settings",
		filters={"is_active": 1},
		fields=["name", "company_name", "tpin", "api_server_url", "environment"],
	)

	if not settings:
		return []

	active_settings = []
	for s in settings:
		active_settings.append(
			{
				"name": s["name"],
				"company": s.get("company_name") or frappe.defaults.get_global_default("company"),
				"tpin": s.get("tpin"),
				"bhfId": "000",  # Default branch ID; can be replaced by real value
				"api_url": s.get("api_server_url"),
				"environment": s.get("environment"),
				"is_valid": True,
			}
		)

	return active_settings