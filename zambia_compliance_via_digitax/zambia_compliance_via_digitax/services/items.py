"""Item registration and sync with DigiTax Zambia (ZRA Smart Invoice).

DigiTax endpoints used (see fixtures/zra_sis_routes.json):
- saveItem      POST /v1/items            register an item
- selectItem    GET  /v1/items/{item_id}  read one item (status, ZRA item code)
- UpdateItem    PUT  /v1/items/{item_id}  update item_name / default_unit_price

DigiTax has no delete endpoint, so registered items cannot be deleted in ERPNext.
Only item_name and prices can change after registration; every other SIS field is locked.
"""

from __future__ import annotations

import time

import frappe
from frappe import _
from frappe.utils import add_to_date, flt, get_datetime, now_datetime

from ..digitax.client import DigitaxClient, DigitaxError
from ..doctype.doctype_names_mapping import SETTINGS_DOCTYPE_NAME
from ..utils.payload_utils import build_callback_url

STATUS_PENDING = "PENDING"
STATUS_FAILED = "FAILED"
STATUS_COMPLETED = "COMPLETED"

CALLBACK_METHOD = "zambia_compliance_via_digitax.zambia_compliance_via_digitax.apis.item.item_registration_callback"
REGISTER_JOB = "zambia_compliance_via_digitax.zambia_compliance_via_digitax.services.items.register_item_job"
UPDATE_JOB = "zambia_compliance_via_digitax.zambia_compliance_via_digitax.services.items.update_item_job"
REFRESH_JOB = "zambia_compliance_via_digitax.zambia_compliance_via_digitax.services.items.refresh_item_job"
STATUS_CHECK_JOB = "zambia_compliance_via_digitax.zambia_compliance_via_digitax.services.items.status_check_job"

# One follow-up status check this long after registration, unless DigiTax's callback settled it first.
STATUS_CHECK_DELAY_SECONDS = 180

# Item field -> DigiTax payload key. All values are code-list document names, which equal the codes.
REQUIRED_CODE_FIELDS = {
	"custom_smart_item_classification_code": "item_class_code",
	"custom_smart_item_type_code": "item_type_code",
	"custom_smart_origin_country_code": "origin_nation_code",
	"custom_smart_packaging_unit_code": "package_unit_code",
	"custom_smart_quantity_unit_code": "quantity_unit_code",
	"custom_smart_tax_type_code": "vat_category_code",
}
LEVY_CODE_FIELDS = {
	"custom_smart_insurance_premium_levy": "ipl_category_code",
	"custom_smart_tourism_levy": "tl_category_code",
	"custom_smart_excise_duty_category_code": "excise_category_code",
	"custom_smart_turn_over_tax_category_code": "tot_category_code",
}
# Fields DigiTax cannot update after registration (PUT only accepts item_name and prices).
LOCKED_AFTER_REGISTRATION = [*REQUIRED_CODE_FIELDS, *LEVY_CODE_FIELDS, "custom_smart_insurance_applicable"]

SERVICE_ITEM_TYPE = "3"


# --------------------------------------------------------------------------- #
# State helpers
# --------------------------------------------------------------------------- #


def is_registered(item) -> bool:
	"""Registered = DigiTax holds a live record for this item (pending or completed)."""
	return bool(item.get("custom_smart_remote_id")) and item.get("custom_smart_status") != STATUS_FAILED


def get_active_settings() -> list[dict]:
	return frappe.get_all(
		SETTINGS_DOCTYPE_NAME,
		filters={"is_active": 1},
		fields=["name", "company_name", "environment", "auto_register_items"],
		order_by="creation asc",
	)


def resolve_settings_name(item, settings_name: str | None = None) -> str:
	"""Pick the settings for an item: explicit choice, then the one it is registered under,
	then the only active settings. Throws when the choice is ambiguous."""
	if settings_name:
		return settings_name
	if item.get("custom_sis_settings"):
		return item.custom_sis_settings

	active = get_active_settings()
	if len(active) == 1:
		return active[0].name
	if not active:
		frappe.throw(_("No active ZRA SIS Settings found."))
	frappe.throw(_("Several ZRA SIS Settings are active. Choose which one to use."))


# --------------------------------------------------------------------------- #
# Validation (Item doc events)
# --------------------------------------------------------------------------- #


def validate_item(item) -> None:
	validate_levy_combinations(item)
	validate_service_item(item)
	validate_locked_fields(item)
	sync_item_tax_templates(item)


def validate_levy_combinations(item) -> None:
	"""Allowed combinations per the DigiTax docs (Items: Tax Category Combinations)."""
	vat = item.get("custom_smart_tax_type_code")
	if item.get("custom_smart_insurance_premium_levy") and vat not in ("D", "C3"):
		frappe.throw(_("An Insurance Premium Levy code can only be combined with VAT category D or C3."))
	if item.get("custom_smart_turn_over_tax_category_code") and vat != "D":
		frappe.throw(_("A Turnover Tax code can only be combined with VAT category D."))


def validate_service_item(item) -> None:
	if item.get("custom_smart_item_type_code") == SERVICE_ITEM_TYPE and item.is_stock_item:
		frappe.throw(
			_("SIS item type 3 (Service) is for items without stock. Untick Maintain Stock or pick another item type.")
		)


def validate_locked_fields(item) -> None:
	if item.is_new() or not is_registered(item):
		return

	changed = [item.meta.get_label(f) for f in LOCKED_AFTER_REGISTRATION if item.has_value_changed(f)]
	if changed:
		frappe.throw(
			_(
				"{0} cannot change after the item is registered with ZRA. DigiTax only allows updating the item name and price."
			).format(", ".join(changed)),
			title=_("Item Registered with ZRA"),
		)


def sync_item_tax_templates(item) -> None:
	"""Keep the Item Tax rows that are tagged with an SIS taxation type in line with the item's VAT category.
	Rows using untagged templates are left alone."""
	if not item.is_new() and not item.has_value_changed("custom_smart_tax_type_code"):
		return

	tagged = set(
		frappe.get_all(
			"Item Tax Template", filters={"custom_taxation_type": ["is", "set"], "disabled": 0}, pluck="name"
		)
	)
	item.set("taxes", [row for row in item.get("taxes", []) if row.item_tax_template not in tagged])

	vat = item.get("custom_smart_tax_type_code")
	if not vat:
		return
	for template in frappe.get_all(
		"Item Tax Template", filters={"custom_taxation_type": vat, "disabled": 0}, pluck="name"
	):
		item.append("taxes", {"item_tax_template": template})


def prevent_deleting_registered_item(item) -> None:
	if item.get("custom_smart_remote_id"):
		frappe.throw(
			_(
				"Item {0} is registered with ZRA (DigiTax ID {1}) and cannot be deleted, because DigiTax has no delete. Disable it instead."
			).format(item.name, item.custom_smart_remote_id),
			title=_("Item Registered with ZRA"),
		)


# --------------------------------------------------------------------------- #
# Readiness
# --------------------------------------------------------------------------- #


def get_registration_blockers(item, settings_name: str) -> list[str]:
	"""Reasons the item cannot be registered yet. Empty list = ready."""
	blockers = []
	if item.disabled:
		blockers.append(_("The item is disabled."))
	if item.get("custom_prevent_smart_registration"):
		blockers.append(_("Prevent Smart Registration is ticked."))

	missing = [item.meta.get_label(f) for f in REQUIRED_CODE_FIELDS if not item.get(f)]
	if missing:
		blockers.append(_("Missing: {0}.").format(", ".join(missing)))

	code = item.get("custom_smart_item_classification_code")
	environment = frappe.db.get_value(SETTINGS_DOCTYPE_NAME, settings_name, "environment")
	if code and environment:
		field = "available_in_sandbox" if environment == "Sandbox" else "available_in_production"
		if not frappe.db.get_value("ZRA SIS Item Classification Codes", code, field):
			blockers.append(
				_("Item Classification Code {0} is not available in the DigiTax {1} environment.").format(
					code, environment
				)
			)
	return blockers


# --------------------------------------------------------------------------- #
# Payloads
# --------------------------------------------------------------------------- #


def get_default_unit_price(item) -> float:
	"""Selling price from the default selling price list, then Standard Selling Rate, then valuation rate."""
	price_list = frappe.db.get_single_value("Selling Settings", "selling_price_list")
	rate = 0
	if price_list:
		rate = frappe.db.get_value(
			"Item Price",
			{"item_code": item.name, "price_list": price_list, "customer": ["is", "not set"]},
			"price_list_rate",
			order_by="valid_from desc",
		)
	return round(flt(rate or item.get("standard_rate") or item.get("valuation_rate")), 4)


def build_registration_payload(item, settings_name: str) -> dict:
	payload = {key: item.get(field) for field, key in REQUIRED_CODE_FIELDS.items()}
	payload["item_name"] = item.item_name
	payload["default_unit_price"] = get_default_unit_price(item)

	for field, key in LEVY_CODE_FIELDS.items():
		if item.get(field):
			payload[key] = item.get(field)

	barcode = next((row.barcode for row in item.get("barcodes", []) if row.barcode), None)
	if barcode:
		payload["bar_code"] = barcode

	if item.get("custom_smart_insurance_applicable"):
		payload["insurable"] = True

	# Opening stock is reported through stock adjustments, not here, to avoid counting it twice.
	payload["callback_url"] = build_callback_url(CALLBACK_METHOD, settings_name)
	return payload


def build_update_payload(item) -> dict:
	return {"item_name": item.item_name, "default_unit_price": get_default_unit_price(item)}


# --------------------------------------------------------------------------- #
# Writing DigiTax results back to the Item
# --------------------------------------------------------------------------- #


def apply_remote_item(item_name: str, data: dict, settings_name: str) -> None:
	status = (data.get("status") or "").upper()
	values = {
		"custom_smart_remote_id": data.get("id"),
		"custom_smart_item_code": data.get("item_code"),
		"custom_smart_status": status,
		"custom_active": 1 if data.get("active") else 0,
		"custom_item_registered": 1 if status == STATUS_COMPLETED else 0,
		"custom__sent_to_digitax": 1,
		"custom_sis_settings": settings_name,
		"custom_sis_last_synced_on": now_datetime(),
		"custom_sis_last_error": None,
	}
	if status == STATUS_FAILED:
		values["custom_sis_last_error"] = _(
			"ZRA marked this item as FAILED. DigiTax does not give a reason. Check the SIS codes and register the item again."
		)
	else:
		values["custom_submission_tries"] = 0

	frappe.db.set_value("Item", item_name, values)


def record_failure(item_name: str, message: str) -> None:
	tries = frappe.db.get_value("Item", item_name, "custom_submission_tries") or 0
	frappe.db.set_value(
		"Item",
		item_name,
		{
			"custom_sis_last_error": message,
			"custom_submission_tries": tries + 1,
			"custom__sent_to_digitax": 1,
			"custom_sis_last_synced_on": now_datetime(),
		},
	)


# --------------------------------------------------------------------------- #
# CRUD
# --------------------------------------------------------------------------- #


def register_item(item_name: str, settings_name: str | None = None) -> dict:
	"""POST /items. Throws for local problems; returns {"ok": False, ...} when DigiTax rejects it."""
	item = frappe.get_doc("Item", item_name)
	if is_registered(item):
		frappe.throw(_("Item {0} is already registered (DigiTax ID {1}).").format(item.name, item.custom_smart_remote_id))

	settings_name = resolve_settings_name(item, settings_name)
	blockers = get_registration_blockers(item, settings_name)
	if blockers:
		frappe.throw("<br>".join(blockers), title=_("Item Not Ready for ZRA"))

	client = DigitaxClient(settings_name)
	try:
		data = client.post(
			"saveItem", build_registration_payload(item, settings_name), reference_doctype="Item", reference_name=item.name
		)
	except DigitaxError as e:
		record_failure(item.name, e.message)
		return {"ok": False, "message": e.message}

	apply_remote_item(item.name, data, settings_name)
	schedule_status_check(item.name, data)
	return {"ok": True, "status": data.get("status"), "item_code": data.get("item_code"), "id": data.get("id")}


def refresh_item(item_name: str) -> dict:
	"""GET /items/{id}. Pulls status, ZRA item code and active flag."""
	item = frappe.get_doc("Item", item_name)
	if not item.get("custom_smart_remote_id"):
		frappe.throw(_("Item {0} has no DigiTax ID yet.").format(item.name))

	settings_name = resolve_settings_name(item)
	try:
		data = DigitaxClient(settings_name).get(
			"selectItem", path_params={"item_id": item.custom_smart_remote_id}, reference_doctype="Item", reference_name=item.name
		)
	except DigitaxError as e:
		record_failure(item.name, e.message)
		return {"ok": False, "message": e.message}

	apply_remote_item(item.name, data, settings_name)
	return {"ok": True, "status": data.get("status"), "item_code": data.get("item_code"), "stock_quantity": data.get("stock_quantity")}


def update_item(item_name: str) -> dict:
	"""PUT /items/{id} with item_name and default_unit_price, the only fields DigiTax lets us change."""
	item = frappe.get_doc("Item", item_name)
	if not is_registered(item):
		frappe.throw(_("Item {0} is not registered with ZRA yet.").format(item.name))

	settings_name = resolve_settings_name(item)
	try:
		data = DigitaxClient(settings_name).put(
			"UpdateItem",
			build_update_payload(item),
			path_params={"item_id": item.custom_smart_remote_id},
			reference_doctype="Item",
			reference_name=item.name,
		)
	except DigitaxError as e:
		record_failure(item.name, e.message)
		return {"ok": False, "message": e.message}

	apply_remote_item(item.name, data, settings_name)
	return {"ok": True, "status": data.get("status"), "item_code": data.get("item_code")}


def link_existing_item(item_name: str, item_id: str, settings_name: str | None = None) -> dict:
	"""Attach an item that already exists on DigiTax (e.g. created on the dashboard).

	Only the DigiTax item ID (item_...) is accepted: it is one GET /items/{item_id} call. Searching by
	ZRA item code would mean paging through every item on DigiTax, which is too many requests.
	The item's SIS codes are overwritten with DigiTax's, since they cannot differ after registration.
	"""
	item = frappe.get_doc("Item", item_name)
	if is_registered(item):
		frappe.throw(_("Item {0} is already registered (DigiTax ID {1}).").format(item.name, item.custom_smart_remote_id))

	item_id = (item_id or "").strip()
	if not item_id.startswith("item_"):
		frappe.throw(_("Enter the DigiTax item ID. It starts with item_, for example item_01M3V6XP4QDQ351BFVHTMMYRD7."))

	settings_name = resolve_settings_name(item, settings_name)
	try:
		data = DigitaxClient(settings_name).get(
			"selectItem", path_params={"item_id": item_id}, reference_doctype="Item", reference_name=item.name
		)
	except DigitaxError as e:
		frappe.throw(e.message, title=_("DigiTax Error"))

	if not data or not data.get("id"):
		frappe.throw(_("No DigiTax item found for {0}.").format(item_id))

	other = frappe.db.get_value("Item", {"custom_smart_remote_id": data["id"], "name": ["!=", item.name]}, "name")
	if other:
		frappe.throw(_("DigiTax item {0} is already linked to Item {1}.").format(data["id"], other))

	copy_remote_codes(item, data)
	apply_remote_item(item.name, data, settings_name)
	schedule_status_check(item.name, data)
	return {"ok": True, "status": data.get("status"), "item_code": data.get("item_code"), "id": data.get("id")}


def copy_remote_codes(item, data: dict) -> None:
	"""Adopt DigiTax's SIS codes on the local item, skipping codes missing from local code lists."""
	link_doctypes = {f: item.meta.get_field(f).options for f in [*REQUIRED_CODE_FIELDS, *LEVY_CODE_FIELDS]}
	values, skipped = {}, []
	for field, key in [*REQUIRED_CODE_FIELDS.items(), *LEVY_CODE_FIELDS.items()]:
		code = data.get(key)
		if not code:
			continue
		if frappe.db.exists(link_doctypes[field], code):
			values[field] = code
		else:
			skipped.append(f"{key}={code}")
	if values:
		frappe.db.set_value("Item", item.name, values)
	if skipped:
		frappe.msgprint(
			_("These DigiTax codes are not in the local code lists and were not copied: {0}").format(", ".join(skipped)),
			indicator="orange",
		)


# --------------------------------------------------------------------------- #
# Automatic sync
# --------------------------------------------------------------------------- #


def on_item_update(item) -> None:
	"""Auto-register new items and push name changes for registered ones."""
	if is_registered(item):
		if item.has_value_changed("item_name"):
			enqueue_item_job(UPDATE_JOB, item.name)
		return

	settings_name = get_auto_register_settings(item)
	if settings_name and not get_registration_blockers(item, settings_name):
		enqueue_item_job(REGISTER_JOB, item.name, settings_name=settings_name)


def get_auto_register_settings(item) -> str | None:
	"""Settings to auto-register under, or None when auto-registration is off or ambiguous."""
	active = get_active_settings()
	if item.get("custom_sis_settings"):
		match = next((s for s in active if s.name == item.custom_sis_settings), None)
		return match.name if match and match.auto_register_items else None
	if len(active) == 1 and active[0].auto_register_items:
		return active[0].name
	return None


def enqueue_item_job(method: str, item_name: str, queue: str = "short", timeout: int | None = None, **kwargs) -> None:
	frappe.enqueue(
		method,
		queue=queue,
		timeout=timeout,
		job_id=f"zra_sis::{method.rsplit('.', 1)[-1]}::{item_name}",
		deduplicate=True,
		enqueue_after_commit=True,
		item_name=item_name,
		**kwargs,
	)


def register_item_job(item_name: str, settings_name: str | None = None) -> None:
	item = frappe.get_doc("Item", item_name)
	if is_registered(item):
		return  # another job or a manual click got there first
	try:
		register_item(item_name, settings_name)
	except Exception:
		frappe.log_error(title=f"ZRA SIS: auto-registration failed for {item_name}", reference_doctype="Item", reference_name=item_name)


def update_item_job(item_name: str) -> None:
	try:
		update_item(item_name)
	except Exception:
		frappe.log_error(title=f"ZRA SIS: item update failed for {item_name}", reference_doctype="Item", reference_name=item_name)


def refresh_item_job(item_name: str) -> None:
	try:
		refresh_item(item_name)
	except Exception:
		frappe.log_error(title=f"ZRA SIS: item refresh failed for {item_name}", reference_doctype="Item", reference_name=item_name)


def schedule_status_check(item_name: str, data: dict) -> None:
	"""After DigiTax accepts an item it syncs with ZRA in the background. Queue one check for
	STATUS_CHECK_DELAY_SECONDS later, in case the callback never reaches us (e.g. a localhost site)."""
	if (data.get("status") or "").upper() != STATUS_PENDING:
		return
	enqueue_item_job(
		STATUS_CHECK_JOB,
		item_name,
		queue="long",
		timeout=STATUS_CHECK_DELAY_SECONDS + 300,
		check_after=str(add_to_date(now_datetime(), seconds=STATUS_CHECK_DELAY_SECONDS)),
	)


def status_check_job(item_name: str, check_after: str | None = None) -> None:
	"""Wait until `check_after`, then fetch the status only if the callback hasn't settled it."""
	if check_after:
		wait = (get_datetime(check_after) - now_datetime()).total_seconds()
		if wait > 0:
			time.sleep(wait)
		frappe.db.rollback()  # start a fresh read so we see what the callback wrote meanwhile

	status, remote_id = frappe.db.get_value("Item", item_name, ["custom_smart_status", "custom_smart_remote_id"]) or (None, None)
	if status != STATUS_PENDING or not remote_id:
		return

	refresh_item_job(item_name)
