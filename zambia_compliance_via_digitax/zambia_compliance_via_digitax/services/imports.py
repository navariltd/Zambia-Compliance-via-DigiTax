"""Imports with DigiTax Zambia (ZRA Smart Invoice).

DigiTax endpoints used (see fixtures/zra_sis_routes.json):
- fetchImports  GET /v1/imports                    import items from customs declarations (cursor pagination)
- linkImport    PUT /v1/imports/{import_id}/link   link an import item to our DigiTax item
- reviewImport  PUT /v1/imports/{import_id}/review approve or reject

Each record is one item of a customs declaration. DigiTax has no endpoint for a single import, so the
list is the only way to refresh. Approving links the import item with its quantity, so DigiTax adds the
stock; the ERPNext receipt ticked "Is Import" then reports no stock (services/stock.py).
"""

from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import flt, now_datetime

from ..digitax.client import DigitaxClient, DigitaxError
from ..doctype.doctype_names_mapping import IMPORT_DOCTYPE_NAME, SETTINGS_DOCTYPE_NAME
from .common import comparable, get_active_settings, parse_utc_datetime
from .items import is_registered as is_registered_item

# Import Item Status (docs: Import: Data Attributes)
IMPORT_STATUS_LABELS = {"1": "Unsent", "2": "Waiting", "3": "Approved", "4": "Cancelled"}
REVIEWABLE = ("1", "2")

COPIED_FIELDS = (
	"task_code",
	"item_sequence",
	"declaration_number",
	"hs_code",
	"item_name",
	"origin_nation_code",
	"export_nation_code",
	"package",
	"package_unit_code",
	"quantity",
	"quantity_unit_code",
	"gross_weight",
	"net_weight",
	"supplier_name",
	"agent_name",
	"invoice_foreign_currency_amount",
	"invoice_foreign_currency_code",
	"invoice_foreign_currency_rate",
	"item_id",
	"item_class_code",
	"item_code",
	"remarks",
	"approved_quantity",
)


def fetch_imports(settings_name: str) -> dict:
	"""Pull every import item and save new or changed ones. Returns {"ok": False, ...} when DigiTax rejects it."""
	total = new = 0
	try:
		for data in DigitaxClient(settings_name).iterate(
			"fetchImports", reference_doctype=SETTINGS_DOCTYPE_NAME, reference_name=settings_name
		):
			total += 1
			new += save_import(data, settings_name)
	except DigitaxError as e:
		return {"ok": False, "message": e.message, "total": total, "new": new}
	return {"ok": True, "total": total, "new": new}


def fetch_all_imports() -> None:
	"""Scheduled job."""
	for settings in get_active_settings():
		if not frappe.db.get_value(SETTINGS_DOCTYPE_NAME, settings.name, "pull_purchases_and_imports"):
			continue
		try:
			result = fetch_imports(settings.name)
			frappe.db.commit()
			if not result.get("ok"):
				frappe.log_error(
					title=f"ZRA SIS: could not fetch imports for {settings.name}",
					message=result.get("message"),
					reference_doctype=SETTINGS_DOCTYPE_NAME,
					reference_name=settings.name,
				)
		except Exception:
			frappe.db.rollback()
			frappe.log_error(
				title=f"ZRA SIS: could not fetch imports for {settings.name}",
				reference_doctype=SETTINGS_DOCTYPE_NAME,
				reference_name=settings.name,
			)


def save_import(data: dict, settings_name: str) -> bool:
	"""Insert or update one import item. The local Item mapping is kept. Returns True when it is new."""
	import_id = data.get("id")
	if not import_id:
		return False

	is_new = not frappe.db.exists(IMPORT_DOCTYPE_NAME, import_id)
	doc = frappe.new_doc(IMPORT_DOCTYPE_NAME) if is_new else frappe.get_doc(IMPORT_DOCTYPE_NAME, import_id)
	before = signature(doc)

	if is_new:
		doc.import_id = import_id
		doc.sis_settings = settings_name

	status_code = str(data.get("import_item_status_code") or "")
	doc.update({field: data.get(field) for field in COPIED_FIELDS})
	doc.update(
		{
			"declaration_date": parse_utc_datetime(data.get("declaration_date")),
			"status": (data.get("status") or "").upper(),
			"import_item_status_code": status_code,
			"import_item_status": IMPORT_STATUS_LABELS.get(status_code, status_code),
		}
	)
	if not doc.item:
		doc.item = match_item(doc, settings_name)

	if not is_new and signature(doc) == before:
		return False

	doc.last_synced_on = now_datetime()
	doc.flags.ignore_permissions = True
	doc.save()
	return is_new


def signature(doc) -> str:
	fields = (*COPIED_FIELDS, "status", "import_item_status_code", "item")
	return frappe.as_json({f: comparable(doc.get(f)) for f in fields})


def match_item(doc, settings_name: str) -> str | None:
	"""Our Item for an import: the DigiTax item it is linked to, else the item used for an earlier import
	with the same HS code and description."""
	if doc.item_id:
		item = frappe.db.get_value(
			"Item", {"custom_smart_remote_id": doc.item_id, "custom_sis_settings": settings_name}, "name"
		)
		if item:
			return item
	if not doc.hs_code or not doc.item_name:
		return None
	return frappe.db.get_value(
		IMPORT_DOCTYPE_NAME,
		{
			"hs_code": doc.hs_code,
			"item_name": doc.item_name,
			"item": ["is", "set"],
			"name": ["!=", doc.name or ""],
		},
		"item",
		order_by="modified desc",
	)


def ensure_reviewable(doc) -> None:
	if doc.import_item_status_code not in REVIEWABLE:
		frappe.throw(
			_("Only imports waiting for approval can be reviewed. This one is {0}.").format(
				doc.import_item_status or _("unknown")
			)
		)


def approve_import(name: str, remarks: str | None = None) -> dict:
	"""Link the import to its Item, then approve. Returns {"ok": False, ...} when DigiTax rejects it."""
	doc = frappe.get_doc(IMPORT_DOCTYPE_NAME, name)
	ensure_reviewable(doc)
	if not doc.item:
		frappe.throw(_("Choose the Item this import is."))
	item = frappe.db.get_value(
		"Item",
		doc.item,
		["custom_smart_remote_id", "custom_smart_status", "custom_sis_settings"],
		as_dict=True,
	)
	if not is_registered_item(item) or item.custom_sis_settings != doc.sis_settings:
		frappe.throw(_("Item {0} is not registered with ZRA.").format(doc.item))

	client = DigitaxClient(doc.sis_settings)
	ref = {"reference_doctype": IMPORT_DOCTYPE_NAME, "reference_name": name}
	body = {"reject": False}
	if (remarks or "").strip():
		body["remarks"] = remarks.strip()
	try:
		# The quantity makes DigiTax add the stock; the receipt ticked "Is Import" then reports none.
		client.put(
			"linkImport",
			{
				"item_id": item.custom_smart_remote_id,
				"quantity": flt(doc.approved_quantity) or flt(doc.quantity),
			},
			path_params={"import_id": doc.import_id},
			**ref,
		)
		data = client.put("reviewImport", body, path_params={"import_id": doc.import_id}, **ref)
	except DigitaxError as e:
		return record_failure(name, e.message)

	frappe.db.set_value(IMPORT_DOCTYPE_NAME, name, {"review_status": "Approved", "last_error": None})
	save_import(data, doc.sis_settings)
	return {"ok": True}


def reject_import(name: str, remarks: str) -> dict:
	doc = frappe.get_doc(IMPORT_DOCTYPE_NAME, name)
	ensure_reviewable(doc)
	remarks = (remarks or "").strip()
	if not remarks:
		frappe.throw(_("Give a reason for rejecting the import."))

	try:
		data = DigitaxClient(doc.sis_settings).put(
			"reviewImport",
			{"reject": True, "remarks": remarks},
			path_params={"import_id": doc.import_id},
			reference_doctype=IMPORT_DOCTYPE_NAME,
			reference_name=name,
		)
	except DigitaxError as e:
		return record_failure(name, e.message)

	frappe.db.set_value(IMPORT_DOCTYPE_NAME, name, {"review_status": "Rejected", "last_error": None})
	save_import(data, doc.sis_settings)
	return {"ok": True}


def record_failure(name: str, message: str) -> dict:
	frappe.db.set_value(IMPORT_DOCTYPE_NAME, name, {"last_error": message, "last_synced_on": now_datetime()})
	return {"ok": False, "message": message}
