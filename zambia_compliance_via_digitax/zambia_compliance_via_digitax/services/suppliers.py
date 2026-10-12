"""Supplier registration with DigiTax Zambia (ZRA Smart Invoice).

DigiTax endpoints used (see fixtures/zra_sis_routes.json):
- saveSupplier   POST /v1/suppliers                register a supplier (returns the supplier at once, no PENDING step)
- fetchSupplier  GET  /v1/suppliers/{supplier_id}  read one supplier

DigiTax has no update or delete for suppliers. So the TPIN is locked after registration and registered
suppliers cannot be deleted. Suppliers are needed for manual purchases and to match purchases ZRA sends us.
"""

from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import now_datetime

from ..digitax.client import DigitaxClient, DigitaxError
from .common import (
	enqueue_job,
	get_auto_register_settings,
	is_valid_tpin,
	normalize_phone,
	normalize_tpin,
	resolve_settings_name,
)

REGISTER_JOB = "zambia_compliance_via_digitax.zambia_compliance_via_digitax.services.suppliers.register_supplier_job"

LOCKED_AFTER_REGISTRATION = ["tax_id"]


# --------------------------------------------------------------------------- #
# State and field helpers
# --------------------------------------------------------------------------- #


def is_registered(supplier) -> bool:
	return bool(supplier.get("custom_sis_supplier_id"))


def get_tpin(supplier) -> str:
	return normalize_tpin(supplier.get("tax_id"))


def get_phone(supplier) -> str:
	"""Supplier mobile (fetched from the primary contact), else the contact's phone, normalised."""
	phone = supplier.get("mobile_no")
	if not phone and supplier.get("supplier_primary_contact"):
		phone = frappe.db.get_value("Contact", supplier.supplier_primary_contact, ["mobile_no", "phone"])
		phone = next((p for p in (phone or []) if p), None)
	return normalize_phone(phone)


def get_email(supplier) -> str:
	email = supplier.get("email_id")
	if not email and supplier.get("supplier_primary_contact"):
		email = frappe.db.get_value("Contact", supplier.supplier_primary_contact, "email_id")
	return (email or "").strip()


# --------------------------------------------------------------------------- #
# Validation (Supplier doc events)
# --------------------------------------------------------------------------- #


def validate_supplier(supplier) -> None:
	if supplier.is_new() or not is_registered(supplier):
		return

	changed = [supplier.meta.get_label(f) for f in LOCKED_AFTER_REGISTRATION if supplier.has_value_changed(f)]
	if changed:
		frappe.throw(
			_("{0} cannot change after the supplier is registered with ZRA, because DigiTax has no supplier update.").format(
				", ".join(changed)
			),
			title=_("Supplier Registered with ZRA"),
		)


def prevent_deleting_registered_supplier(supplier) -> None:
	if is_registered(supplier):
		frappe.throw(
			_(
				"Supplier {0} is registered with ZRA (DigiTax ID {1}) and cannot be deleted, because DigiTax has no delete. Disable it instead."
			).format(supplier.name, supplier.custom_sis_supplier_id),
			title=_("Supplier Registered with ZRA"),
		)


def get_registration_blockers(supplier) -> list[str]:
	"""Reasons the supplier cannot be registered yet. Empty list = ready."""
	blockers = []
	if supplier.disabled:
		blockers.append(_("The supplier is disabled."))
	if not (supplier.supplier_name or "").strip():
		blockers.append(_("Supplier Name is missing."))

	tpin = get_tpin(supplier)
	if not tpin:
		blockers.append(_("Tax ID (TPIN) is missing."))
	elif not is_valid_tpin(tpin):
		blockers.append(_("Tax ID {0} is not a 10-digit ZRA TPIN.").format(tpin))
	return blockers


# --------------------------------------------------------------------------- #
# Payload and results
# --------------------------------------------------------------------------- #


def build_registration_payload(supplier) -> dict:
	payload = {"supplier_name": supplier.supplier_name.strip(), "supplier_tin": get_tpin(supplier)}
	phone, email = get_phone(supplier), get_email(supplier)
	if phone:
		payload["phone"] = phone
	if email:
		payload["email"] = email
	return payload


def apply_remote_supplier(supplier_name: str, data: dict, settings_name: str) -> None:
	frappe.db.set_value(
		"Supplier",
		supplier_name,
		{
			"custom_sis_supplier_id": data.get("id"),
			"custom_sis_registered": 1,
			"custom_sis_settings": settings_name,
			"custom_sis_last_synced_on": now_datetime(),
			"custom_sis_last_error": None,
		},
	)


def record_failure(supplier_name: str, message: str) -> None:
	frappe.db.set_value(
		"Supplier",
		supplier_name,
		{"custom_sis_last_error": message, "custom_sis_last_synced_on": now_datetime()},
	)


def compare_with_remote(supplier, data: dict) -> list[str]:
	"""Differences between ERPNext and DigiTax's copy, as readable lines."""
	local = {
		_("Name"): (supplier.supplier_name or "").strip(),
		_("TPIN"): get_tpin(supplier),
		_("Phone"): get_phone(supplier),
		_("Email"): get_email(supplier),
	}
	remote = {
		_("Name"): (data.get("supplier_name") or "").strip(),
		_("TPIN"): normalize_tpin(data.get("supplier_tpin")),
		_("Phone"): normalize_phone(data.get("phone")),
		_("Email"): (data.get("email") or "").strip(),
	}
	return [
		_("{0}: ERPNext has {1}, DigiTax has {2}").format(label, local[label] or "-", remote[label] or "-")
		for label in local
		if str(local[label]).lower() != str(remote[label]).lower()
	]


# --------------------------------------------------------------------------- #
# Actions
# --------------------------------------------------------------------------- #


def register_supplier(supplier_name: str, settings_name: str | None = None) -> dict:
	"""POST /suppliers. Throws for local problems; returns {"ok": False, ...} when DigiTax rejects it."""
	supplier = frappe.get_doc("Supplier", supplier_name)
	if is_registered(supplier):
		frappe.throw(
			_("Supplier {0} is already registered (DigiTax ID {1}).").format(supplier.name, supplier.custom_sis_supplier_id)
		)

	blockers = get_registration_blockers(supplier)
	if blockers:
		frappe.throw("<br>".join(blockers), title=_("Supplier Not Ready for ZRA"))

	settings_name = resolve_settings_name(supplier, settings_name)
	try:
		data = DigitaxClient(settings_name).post(
			"saveSupplier", build_registration_payload(supplier), reference_doctype="Supplier", reference_name=supplier.name
		)
	except DigitaxError as e:
		record_failure(supplier.name, e.message)
		return {"ok": False, "message": e.message}

	apply_remote_supplier(supplier.name, data, settings_name)
	return {"ok": True, "id": data.get("id")}


def refresh_supplier(supplier_name: str) -> dict:
	"""GET /suppliers/{id}. Confirms the record and reports differences. ERPNext data is never overwritten."""
	supplier = frappe.get_doc("Supplier", supplier_name)
	if not is_registered(supplier):
		frappe.throw(_("Supplier {0} is not registered with ZRA yet.").format(supplier.name))

	settings_name = resolve_settings_name(supplier)
	try:
		data = DigitaxClient(settings_name).get(
			"fetchSupplier",
			path_params={"supplier_id": supplier.custom_sis_supplier_id},
			reference_doctype="Supplier",
			reference_name=supplier.name,
		)
	except DigitaxError as e:
		record_failure(supplier.name, e.message)
		return {"ok": False, "message": e.message}

	frappe.db.set_value(
		"Supplier", supplier.name, {"custom_sis_last_synced_on": now_datetime(), "custom_sis_last_error": None}
	)
	return {"ok": True, "id": data.get("id"), "differences": compare_with_remote(supplier, data)}


def link_existing_supplier(supplier_name: str, supplier_id: str, settings_name: str | None = None) -> dict:
	"""Attach a supplier that already exists on DigiTax, by its DigiTax supplier ID (supplier_...).
	The TPIN must match when ERPNext has one; an empty ERPNext Tax ID is filled from DigiTax."""
	supplier = frappe.get_doc("Supplier", supplier_name)
	if is_registered(supplier):
		frappe.throw(
			_("Supplier {0} is already registered (DigiTax ID {1}).").format(supplier.name, supplier.custom_sis_supplier_id)
		)

	supplier_id = (supplier_id or "").strip()
	if not supplier_id.startswith("supplier_"):
		frappe.throw(
			_("Enter the DigiTax supplier ID. It starts with supplier_, for example supplier_01J56FWS9KV3KAS0RHD4QJ1QCT.")
		)

	settings_name = resolve_settings_name(supplier, settings_name)
	try:
		data = DigitaxClient(settings_name).get(
			"fetchSupplier", path_params={"supplier_id": supplier_id}, reference_doctype="Supplier", reference_name=supplier.name
		)
	except DigitaxError as e:
		frappe.throw(e.message, title=_("DigiTax Error"))

	if not data or not data.get("id"):
		frappe.throw(_("No DigiTax supplier found for {0}.").format(supplier_id))

	local_tpin, remote_tpin = get_tpin(supplier), normalize_tpin(data.get("supplier_tpin"))
	if local_tpin and remote_tpin and local_tpin != remote_tpin:
		frappe.throw(
			_("DigiTax supplier {0} has TPIN {1}, but this supplier has {2}.").format(supplier_id, remote_tpin, local_tpin)
		)

	other = frappe.db.get_value("Supplier", {"custom_sis_supplier_id": data["id"], "name": ["!=", supplier.name]}, "name")
	if other:
		frappe.throw(_("DigiTax supplier {0} is already linked to Supplier {1}.").format(data["id"], other))

	if not local_tpin and remote_tpin:
		frappe.db.set_value("Supplier", supplier.name, "tax_id", remote_tpin)

	apply_remote_supplier(supplier.name, data, settings_name)
	return {"ok": True, "id": data.get("id"), "differences": compare_with_remote(frappe.get_doc("Supplier", supplier.name), data)}


# --------------------------------------------------------------------------- #
# Automatic registration
# --------------------------------------------------------------------------- #


def on_supplier_update(supplier) -> None:
	if is_registered(supplier):
		return
	settings_name = get_auto_register_settings(supplier, "auto_register_suppliers")
	if settings_name and not get_registration_blockers(supplier):
		enqueue_job(REGISTER_JOB, f"Supplier::{supplier.name}", supplier_name=supplier.name, settings_name=settings_name)


def register_supplier_job(supplier_name: str, settings_name: str | None = None) -> None:
	if is_registered(frappe.get_doc("Supplier", supplier_name)):
		return  # a manual click got there first
	try:
		register_supplier(supplier_name, settings_name)
	except Exception:
		frappe.log_error(
			title=f"ZRA SIS: auto-registration failed for supplier {supplier_name}",
			reference_doctype="Supplier",
			reference_name=supplier_name,
		)
