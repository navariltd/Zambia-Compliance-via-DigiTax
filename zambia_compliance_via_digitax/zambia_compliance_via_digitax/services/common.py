"""Helpers shared by the item, customer and supplier services."""

from __future__ import annotations

import frappe
from frappe import _

from ..doctype.doctype_names_mapping import SETTINGS_DOCTYPE_NAME


def get_active_settings() -> list[dict]:
	return frappe.get_all(
		SETTINGS_DOCTYPE_NAME,
		filters={"is_active": 1},
		fields=["name", "company_name", "environment", "auto_register_items", "auto_register_customers"],
		order_by="creation asc",
	)


def resolve_settings_name(doc, settings_name: str | None = None) -> str:
	"""Pick the settings for a document: explicit choice, then the one it is registered under
	(`custom_sis_settings`), then the only active settings. Throws when the choice is ambiguous."""
	if settings_name:
		return settings_name
	if doc.get("custom_sis_settings"):
		return doc.custom_sis_settings

	active = get_active_settings()
	if len(active) == 1:
		return active[0].name
	if not active:
		frappe.throw(_("No active ZRA SIS Settings found."))
	frappe.throw(_("Several ZRA SIS Settings are active. Choose which one to use."))


def get_auto_register_settings(doc, flag_field: str) -> str | None:
	"""Settings to auto-register under, or None when the settings' `flag_field` is off or the choice is ambiguous."""
	active = get_active_settings()
	if doc.get("custom_sis_settings"):
		match = next((s for s in active if s.name == doc.custom_sis_settings), None)
		return match.name if match and match.get(flag_field) else None
	if len(active) == 1 and active[0].get(flag_field):
		return active[0].name
	return None


def enqueue_job(method: str, key: str, queue: str = "short", timeout: int | None = None, **kwargs) -> None:
	"""Enqueue after commit, at most one queued job per (method, key)."""
	frappe.enqueue(
		method,
		queue=queue,
		timeout=timeout,
		job_id=f"zra_sis::{method.rsplit('.', 1)[-1]}::{key}",
		deduplicate=True,
		enqueue_after_commit=True,
		**kwargs,
	)
