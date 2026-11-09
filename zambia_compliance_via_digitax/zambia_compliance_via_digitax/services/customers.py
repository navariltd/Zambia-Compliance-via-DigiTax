"""Customer registration with DigiTax Zambia (ZRA Smart Invoice).

DigiTax endpoints used (see fixtures/zra_sis_routes.json):
- saveCustomer   POST /v1/customers                register a customer (returns the customer at once, no PENDING step)
- fetchCustomer  GET  /v1/customers/{customer_id}  read one customer

DigiTax has no update or delete for customers. So the TPIN and LPO flag are locked after registration,
and registered customers cannot be deleted. Name, phone, email and address can still change in ERPNext:
sales send those values directly, so the DigiTax copy only matters for `customer_id` and LPO sales.
"""

from __future__ import annotations

import re

import frappe
from frappe import _
from frappe.contacts.doctype.address.address import get_default_address
from frappe.utils import now_datetime

from ..digitax.client import DigitaxClient, DigitaxError
from .common import enqueue_job, get_auto_register_settings, is_valid_tpin, normalize_phone, normalize_tpin, resolve_settings_name

REGISTER_JOB = "zambia_compliance_via_digitax.zambia_compliance_via_digitax.services.customers.register_customer_job"

LOCKED_AFTER_REGISTRATION = ["tax_id", "custom_is_lpo"]


# --------------------------------------------------------------------------- #
# State and field helpers
# --------------------------------------------------------------------------- #


def is_registered(customer) -> bool:
	return bool(customer.get("custom_sis_customer_id"))


def get_tpin(customer) -> str:
	return normalize_tpin(customer.get("tax_id"))


def get_phone(customer) -> str:
	"""Customer mobile, else the primary contact's mobile or phone, normalised."""
	phone = customer.get("mobile_no")
	if not phone and customer.get("customer_primary_contact"):
		phone = frappe.db.get_value("Contact", customer.customer_primary_contact, ["mobile_no", "phone"])
		phone = next((p for p in (phone or []) if p), None)
	return normalize_phone(phone)


def get_address(customer) -> str | None:
	address_name = customer.get("customer_primary_address") or get_default_address("Customer", customer.name)
	if not address_name:
		return None
	address = frappe.db.get_value("Address", address_name, ["address_line1", "address_line2", "city"], as_dict=True)
	if not address:
		return None
	return ", ".join(part.strip() for part in (address.address_line1, address.address_line2, address.city) if part)


# --------------------------------------------------------------------------- #
# Validation (Customer doc events)
# --------------------------------------------------------------------------- #


def validate_customer(customer) -> None:
	if customer.is_new() or not is_registered(customer):
		return

	changed = [customer.meta.get_label(f) for f in LOCKED_AFTER_REGISTRATION if customer.has_value_changed(f)]
	if changed:
		frappe.throw(
			_("{0} cannot change after the customer is registered with ZRA, because DigiTax has no customer update.").format(
				", ".join(changed)
			),
			title=_("Customer Registered with ZRA"),
		)


def prevent_deleting_registered_customer(customer) -> None:
	if is_registered(customer):
		frappe.throw(
			_(
				"Customer {0} is registered with ZRA (DigiTax ID {1}) and cannot be deleted, because DigiTax has no delete. Disable it instead."
			).format(customer.name, customer.custom_sis_customer_id),
			title=_("Customer Registered with ZRA"),
		)


def get_registration_blockers(customer) -> list[str]:
	"""Reasons the customer cannot be registered yet. Empty list = ready."""
	blockers = []
	if customer.disabled:
		blockers.append(_("The customer is disabled."))
	if not (customer.customer_name or "").strip():
		blockers.append(_("Customer Name is missing."))

	tpin = get_tpin(customer)
	if not tpin:
		blockers.append(_("Tax ID (TPIN) is missing."))
	elif not is_valid_tpin(tpin):
		blockers.append(_("Tax ID {0} is not a 10-digit ZRA TPIN.").format(tpin))

	if not get_phone(customer):
		blockers.append(_("A mobile or phone number is missing. Set it on the customer or its primary contact."))
	return blockers


# --------------------------------------------------------------------------- #
# Payload and results
# --------------------------------------------------------------------------- #


def build_registration_payload(customer) -> dict:
	payload = {
		"customer_name": customer.customer_name.strip(),
		"customer_tin": get_tpin(customer),
		"phone": get_phone(customer),
		"is_lpo": bool(customer.get("custom_is_lpo")),
	}
	if customer.get("email_id"):
		payload["email"] = customer.email_id.strip()
	address = get_address(customer)
	if address:
		payload["address"] = address
	return payload


def apply_remote_customer(customer_name: str, data: dict, settings_name: str) -> None:
	frappe.db.set_value(
		"Customer",
		customer_name,
		{
			"custom_sis_customer_id": data.get("id"),
			"custom_registered_successfuly": 1,
			"custom_sis_settings": settings_name,
			"custom_sis_last_synced_on": now_datetime(),
			"custom_sis_last_error": None,
		},
	)

	from .sales import enqueue_recheck

	enqueue_recheck()  # sales waiting for this customer (e.g. LPO)


def record_failure(customer_name: str, message: str) -> None:
	frappe.db.set_value(
		"Customer",
		customer_name,
		{"custom_sis_last_error": message, "custom_sis_last_synced_on": now_datetime()},
	)


def compare_with_remote(customer, data: dict) -> list[str]:
	"""Differences between ERPNext and DigiTax's copy, as readable lines."""
	local = {
		_("Name"): (customer.customer_name or "").strip(),
		_("TPIN"): get_tpin(customer),
		_("Phone"): get_phone(customer),
		_("Email"): (customer.get("email_id") or "").strip(),
		_("LPO"): bool(customer.get("custom_is_lpo")),
	}
	remote = {
		_("Name"): (data.get("customer_name") or "").strip(),
		_("TPIN"): data.get("customer_tpin") or "",
		_("Phone"): normalize_phone(data.get("phone")),
		_("Email"): (data.get("email") or "").strip(),
		_("LPO"): bool(data.get("is_lpo")),
	}
	return [
		_("{0}: ERPNext has {1}, DigiTax has {2}").format(label, local[label] or "-", remote[label] or "-")
		for label in local
		if str(local[label]).lower() != str(remote[label]).lower()
	]


# --------------------------------------------------------------------------- #
# Actions
# --------------------------------------------------------------------------- #


def register_customer(customer_name: str, settings_name: str | None = None) -> dict:
	"""POST /customers. Throws for local problems; returns {"ok": False, ...} when DigiTax rejects it."""
	customer = frappe.get_doc("Customer", customer_name)
	if is_registered(customer):
		frappe.throw(
			_("Customer {0} is already registered (DigiTax ID {1}).").format(customer.name, customer.custom_sis_customer_id)
		)

	blockers = get_registration_blockers(customer)
	if blockers:
		frappe.throw("<br>".join(blockers), title=_("Customer Not Ready for ZRA"))

	settings_name = resolve_settings_name(customer, settings_name)
	try:
		data = DigitaxClient(settings_name).post(
			"saveCustomer", build_registration_payload(customer), reference_doctype="Customer", reference_name=customer.name
		)
	except DigitaxError as e:
		record_failure(customer.name, e.message)
		return {"ok": False, "message": e.message}

	apply_remote_customer(customer.name, data, settings_name)
	return {"ok": True, "id": data.get("id")}


def refresh_customer(customer_name: str) -> dict:
	"""GET /customers/{id}. Confirms the record and reports differences. ERPNext data is never overwritten."""
	customer = frappe.get_doc("Customer", customer_name)
	if not is_registered(customer):
		frappe.throw(_("Customer {0} is not registered with ZRA yet.").format(customer.name))

	settings_name = resolve_settings_name(customer)
	try:
		data = DigitaxClient(settings_name).get(
			"fetchCustomer",
			path_params={"customer_id": customer.custom_sis_customer_id},
			reference_doctype="Customer",
			reference_name=customer.name,
		)
	except DigitaxError as e:
		record_failure(customer.name, e.message)
		return {"ok": False, "message": e.message}

	differences = compare_with_remote(customer, data)
	frappe.db.set_value(
		"Customer",
		customer.name,
		{"custom_sis_last_synced_on": now_datetime(), "custom_sis_last_error": None},
	)
	return {"ok": True, "id": data.get("id"), "differences": differences}


def link_existing_customer(customer_name: str, customer_id: str, settings_name: str | None = None) -> dict:
	"""Attach a customer that already exists on DigiTax, by its DigiTax customer ID (customer_...).

	The TPIN must match when ERPNext has one. The LPO flag is taken from DigiTax, since it cannot change later.
	"""
	customer = frappe.get_doc("Customer", customer_name)
	if is_registered(customer):
		frappe.throw(
			_("Customer {0} is already registered (DigiTax ID {1}).").format(customer.name, customer.custom_sis_customer_id)
		)

	customer_id = (customer_id or "").strip()
	if not customer_id.startswith("customer_"):
		frappe.throw(
			_("Enter the DigiTax customer ID. It starts with customer_, for example customer_01HC3HTDAXQ50E07CWR2A2CFBD.")
		)

	settings_name = resolve_settings_name(customer, settings_name)
	try:
		data = DigitaxClient(settings_name).get(
			"fetchCustomer", path_params={"customer_id": customer_id}, reference_doctype="Customer", reference_name=customer.name
		)
	except DigitaxError as e:
		frappe.throw(e.message, title=_("DigiTax Error"))

	if not data or not data.get("id"):
		frappe.throw(_("No DigiTax customer found for {0}.").format(customer_id))

	local_tpin, remote_tpin = get_tpin(customer), (data.get("customer_tpin") or "").strip()
	if local_tpin and remote_tpin and local_tpin != remote_tpin:
		frappe.throw(
			_("DigiTax customer {0} has TPIN {1}, but this customer has {2}.").format(customer_id, remote_tpin, local_tpin)
		)

	other = frappe.db.get_value("Customer", {"custom_sis_customer_id": data["id"], "name": ["!=", customer.name]}, "name")
	if other:
		frappe.throw(_("DigiTax customer {0} is already linked to Customer {1}.").format(data["id"], other))

	values = {"custom_is_lpo": 1 if data.get("is_lpo") else 0}
	if not local_tpin and remote_tpin:
		values["tax_id"] = remote_tpin
	frappe.db.set_value("Customer", customer.name, values)

	apply_remote_customer(customer.name, data, settings_name)
	return {"ok": True, "id": data.get("id"), "differences": compare_with_remote(frappe.get_doc("Customer", customer.name), data)}


# --------------------------------------------------------------------------- #
# Automatic registration
# --------------------------------------------------------------------------- #


def on_customer_update(customer) -> None:
	if is_registered(customer):
		return
	settings_name = get_auto_register_settings(customer, "auto_register_customers")
	if settings_name and not get_registration_blockers(customer):
		enqueue_job(
			REGISTER_JOB, f"Customer::{customer.name}", customer_name=customer.name, settings_name=settings_name
		)


def register_customer_job(customer_name: str, settings_name: str | None = None) -> None:
	if is_registered(frappe.get_doc("Customer", customer_name)):
		return  # a manual click got there first
	try:
		register_customer(customer_name, settings_name)
	except Exception:
		frappe.log_error(
			title=f"ZRA SIS: auto-registration failed for customer {customer_name}",
			reference_doctype="Customer",
			reference_name=customer_name,
		)
