"""Purchases with DigiTax Zambia (ZRA Smart Invoice).

DigiTax endpoints used (see fixtures/zra_sis_routes.json):
- fetchPurchaseSales  GET /v1/purchases                         purchases ZRA received from suppliers (cursor pagination)
- fetchPurchase       GET /v1/purchases/{purchase_id}           one purchase
- linkPurchaseItems   PUT /v1/purchases/{purchase_id}/link-items map purchase lines to our DigiTax items
- reviewPurchase      PUT /v1/purchases/{purchase_id}/review     approve or reject
- savePurchase        POST /v1/purchases                        manual purchase (supplier not on Smart Invoice)
- fetchSupplier       GET /v1/suppliers/{supplier_id}           supplier name and TPIN for a purchase

Flow: pull -> map each line to an Item (auto where possible) -> Approve (link-items, then review) or Reject
-> Create Purchase Invoice (draft). Lines are linked with their quantity, so DigiTax adds the stock on
approval and the Purchase Invoice's stock is not reported again (services/stock.py).

With the settings' "Send Purchase Invoices to ZRA" on, every submitted Purchase Invoice that has registered
items and did not come from a ZRA purchase is sent as a manual purchase (POST /purchases), which also adds
the stock on DigiTax. A manual purchase cannot be cancelled on DigiTax, so its invoice cannot be cancelled.
"""

from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import cint, flt, now_datetime

from ..digitax.client import DigitaxClient, DigitaxError
from ..doctype.doctype_names_mapping import (
	PURCHASE_DOCTYPE_NAME,
	PURCHASE_ITEM_DOCTYPE_NAME,
	SETTINGS_DOCTYPE_NAME,
)
from . import suppliers as supplier_service
from .common import comparable, enqueue_job, get_active_settings, parse_compact_date
from .items import is_registered as is_registered_item

# Invoice Status Codes (docs: Sales: Data Attributes), used for purchase_status_code
WAIT_FOR_APPROVAL = "01"
APPROVED = "02"
CANCEL_REQUESTED = "03"
CANCELED = "04"
PURCHASE_STATUS_LABELS = {
	WAIT_FOR_APPROVAL: "Wait for Approval",
	APPROVED: "Approved",
	CANCEL_REQUESTED: "Cancel Requested",
	CANCELED: "Canceled",
	"05": "Credit Note Generated",
	"06": "Transferred",
}

# Payment Type Codes (docs: Sales: Data Attributes)
PAYMENT_CASH = "01"
PAYMENT_CREDIT = "02"

TAX_KEYS = ("vat", "ipl", "tl", "excise")
LINE_FIELDS = (
	"item_id",
	"item_code",
	"item_name",
	"item_class_code",
	"packing_unit_code",
	"quantity",
	"approved_quantity",
	"unit_price",
	"supply_price",
	"discount_rate",
	"discount_amount",
	"total_amount",
	*(f"{tax}_{kind}" for tax in TAX_KEYS for kind in ("taxable_amount", "tax_amount")),
)

FETCH_ALL_JOB = (
	"zambia_compliance_via_digitax.zambia_compliance_via_digitax.services.purchases.fetch_all_purchases"
)
SEND_JOB = "zambia_compliance_via_digitax.zambia_compliance_via_digitax.services.purchases.report_manual_purchase_job"


# --------------------------------------------------------------------------- #
# Pulling purchases
# --------------------------------------------------------------------------- #


def fetch_purchases(settings_name: str) -> dict:
	"""Pull every purchase and save new or changed ones. Returns {"ok": False, ...} when DigiTax rejects it."""
	client = DigitaxClient(settings_name)
	suppliers: dict = {}
	total = new = 0
	try:
		for data in client.iterate(
			"fetchPurchaseSales", reference_doctype=SETTINGS_DOCTYPE_NAME, reference_name=settings_name
		):
			total += 1
			new += save_purchase(data, settings_name, client, suppliers)
	except DigitaxError as e:
		return {"ok": False, "message": e.message, "total": total, "new": new}
	return {"ok": True, "total": total, "new": new}


def fetch_all_purchases() -> None:
	"""Scheduled job."""
	for settings in get_active_settings():
		if not frappe.db.get_value(SETTINGS_DOCTYPE_NAME, settings.name, "pull_purchases_and_imports"):
			continue
		try:
			result = fetch_purchases(settings.name)
			frappe.db.commit()
			if not result.get("ok"):
				frappe.log_error(
					title=f"ZRA SIS: could not fetch purchases for {settings.name}",
					message=result.get("message"),
					reference_doctype=SETTINGS_DOCTYPE_NAME,
					reference_name=settings.name,
				)
		except Exception:
			frappe.db.rollback()
			frappe.log_error(
				title=f"ZRA SIS: could not fetch purchases for {settings.name}",
				reference_doctype=SETTINGS_DOCTYPE_NAME,
				reference_name=settings.name,
			)


def save_purchase(
	data: dict, settings_name: str, client: DigitaxClient | None = None, suppliers=None
) -> bool:
	"""Insert or update one purchase from DigiTax. Local fields (supplier, item mapping, review comment,
	Purchase Invoice) are kept. Returns True when it is new."""
	purchase_id = data.get("id")
	if not purchase_id:
		return False

	is_new = not frappe.db.exists(PURCHASE_DOCTYPE_NAME, purchase_id)
	doc = (
		frappe.new_doc(PURCHASE_DOCTYPE_NAME)
		if is_new
		else frappe.get_doc(PURCHASE_DOCTYPE_NAME, purchase_id)
	)
	before = signature(doc)

	if is_new:
		doc.purchase_id = purchase_id
		doc.sis_settings = settings_name

	status_code = (
		str(data.get("purchase_status_code") or "").zfill(2) if data.get("purchase_status_code") else ""
	)
	doc.update(
		{
			"invoice_number": data.get("invoice_number"),
			"purchase_number": data.get("purchase_number"),
			"receipt_type_code": data.get("receipt_type_code"),
			"payment_type_code": data.get("payment_type_code"),
			"purchase_date": parse_compact_date(data.get("purchase_date")),
			"supplier_invoice_number": data.get("supplier_invoice_number"),
			"supplier_id": data.get("supplier_id"),
			"status": (data.get("status") or "").upper(),
			"purchase_status_code": status_code,
			"purchase_status": PURCHASE_STATUS_LABELS.get(status_code, status_code),
		}
	)

	if doc.supplier_id and (not doc.supplier_tpin or not doc.supplier):
		remote = resolve_supplier(doc.supplier_id, client or DigitaxClient(settings_name), suppliers)
		doc.supplier_name = doc.supplier_name or remote.get("supplier_name")
		doc.supplier_tpin = doc.supplier_tpin or remote.get("supplier_tpin")
		doc.supplier = doc.supplier or remote.get("supplier")

	existing = {row.purchase_item_id: row for row in doc.get("items", [])}
	rows = []
	for line in data.get("item_list") or []:
		values = {field: line.get(field) for field in LINE_FIELDS}
		values["purchase_item_id"] = line.get("id")
		previous = existing.get(line.get("id"))
		values["item"] = (previous and previous.item) or match_item(values, doc.supplier_id, settings_name)
		rows.append(values)
	doc.set("items", rows)
	set_totals(doc)

	if not is_new and signature(doc) == before:
		return False

	doc.last_synced_on = now_datetime()
	doc.flags.ignore_permissions = True
	doc.save()
	return is_new


def signature(doc) -> str:
	"""What a sync can change, to skip saving unchanged purchases."""
	header = {
		f: comparable(doc.get(f)) for f in ("status", "purchase_status_code", "supplier", "supplier_tpin")
	}
	lines = [
		{f: comparable(row.get(f)) for f in (*LINE_FIELDS, "purchase_item_id", "item")}
		for row in doc.get("items", [])
	]
	return frappe.as_json([header, lines])


def set_totals(doc) -> None:
	total = sum(flt(row.total_amount) for row in doc.items)
	tax = sum(flt(row.get(f"{key}_tax_amount")) for row in doc.items for key in TAX_KEYS)
	doc.total_amount = total
	doc.total_tax_amount = tax
	doc.total_taxable_amount = total - tax


def resolve_supplier(supplier_id: str, client: DigitaxClient, cache: dict | None = None) -> dict:
	"""Name, TPIN and ERPNext Supplier for a DigiTax supplier ID. ERPNext suppliers match by DigiTax ID,
	then by TPIN when exactly one supplier has it."""
	cache = cache if cache is not None else {}
	if supplier_id in cache:
		return cache[supplier_id]

	result = {}
	local = frappe.db.get_value(
		"Supplier", {"custom_sis_supplier_id": supplier_id}, ["name", "supplier_name", "tax_id"], as_dict=True
	)
	if local:
		result = {"supplier": local.name, "supplier_name": local.supplier_name, "supplier_tpin": local.tax_id}
	else:
		try:
			remote = client.get("fetchSupplier", path_params={"supplier_id": supplier_id}) or {}
		except DigitaxError:
			remote = {}
		tpin = supplier_service.normalize_tpin(remote.get("supplier_tpin"))
		matches = frappe.get_all("Supplier", filters={"tax_id": tpin}, pluck="name", limit=2) if tpin else []
		result = {
			"supplier": matches[0] if len(matches) == 1 else None,
			"supplier_name": remote.get("supplier_name"),
			"supplier_tpin": tpin,
		}

	cache[supplier_id] = result
	return result


def match_item(line: dict, supplier_id: str | None, settings_name: str) -> str | None:
	"""Our Item for a purchase line: the DigiTax item it is linked to, else the item used for the same
	supplier item code on an earlier purchase."""
	if line.get("item_id"):
		item = frappe.db.get_value(
			"Item", {"custom_smart_remote_id": line["item_id"], "custom_sis_settings": settings_name}, "name"
		)
		if item:
			return item

	if not line.get("item_code") or not supplier_id:
		return None
	previous = frappe.db.sql(
		f"""select row.item from `tab{PURCHASE_ITEM_DOCTYPE_NAME}` row
		join `tab{PURCHASE_DOCTYPE_NAME}` purchase on purchase.name = row.parent
		where row.item_code = %s and ifnull(row.item, '') != '' and purchase.supplier_id = %s
		order by purchase.modified desc limit 1""",
		(line["item_code"], supplier_id),
	)
	return previous[0][0] if previous else None


# --------------------------------------------------------------------------- #
# Review
# --------------------------------------------------------------------------- #


def refresh_purchase(name: str) -> dict:
	doc = frappe.get_doc(PURCHASE_DOCTYPE_NAME, name)
	try:
		data = DigitaxClient(doc.sis_settings).get(
			"fetchPurchase",
			path_params={"purchase_id": doc.purchase_id},
			reference_doctype=PURCHASE_DOCTYPE_NAME,
			reference_name=name,
		)
	except DigitaxError as e:
		return record_failure(name, e.message)
	save_purchase(data, doc.sis_settings)
	return {"ok": True, "purchase_status": PURCHASE_STATUS_LABELS.get(doc.purchase_status_code)}


def ensure_reviewable(doc) -> None:
	if doc.purchase_status_code != WAIT_FOR_APPROVAL:
		frappe.throw(
			_("Only purchases waiting for approval can be reviewed. This one is {0}.").format(
				doc.purchase_status or doc.purchase_status_code or _("unknown")
			)
		)


def get_link_payload(doc) -> list[dict]:
	"""link-items rows. Every line must be mapped to an Item registered under the same settings."""
	problems, links = [], []
	for row in doc.items:
		if not row.item:
			problems.append(_("Row {0} ({1}): choose the Item it is.").format(row.idx, row.item_name))
			continue
		item = frappe.db.get_value(
			"Item",
			row.item,
			["custom_smart_remote_id", "custom_smart_status", "custom_sis_settings"],
			as_dict=True,
		)
		if not is_registered_item(item) or item.custom_sis_settings != doc.sis_settings:
			problems.append(_("Row {0}: Item {1} is not registered with ZRA.").format(row.idx, row.item))
			continue
		# The quantity makes DigiTax add the stock; the Purchase Invoice's stock is then not reported.
		links.append(
			{
				"purchase_item_id": row.purchase_item_id,
				"item_id": item.custom_smart_remote_id,
				"quantity": flt(row.approved_quantity) or flt(row.quantity),
			}
		)
	if problems:
		frappe.throw("<br>".join(problems), title=_("Map Every Line to a Registered Item"))
	return links


def approve_purchase(name: str) -> dict:
	"""Link the lines to our items, then approve. Returns {"ok": False, ...} when DigiTax rejects it."""
	doc = frappe.get_doc(PURCHASE_DOCTYPE_NAME, name)
	ensure_reviewable(doc)
	links = get_link_payload(doc)

	client = DigitaxClient(doc.sis_settings)
	ref = {"reference_doctype": PURCHASE_DOCTYPE_NAME, "reference_name": name}
	try:
		client.put("linkPurchaseItems", {"items": links}, path_params={"purchase_id": doc.purchase_id}, **ref)
		data = client.put(
			"reviewPurchase", {"reject": False}, path_params={"purchase_id": doc.purchase_id}, **ref
		)
	except DigitaxError as e:
		return record_failure(name, e.message)

	frappe.db.set_value(PURCHASE_DOCTYPE_NAME, name, {"review_status": "Approved", "last_error": None})
	save_purchase(data, doc.sis_settings, client)
	return {"ok": True}


def reject_purchase(name: str, comment: str) -> dict:
	doc = frappe.get_doc(PURCHASE_DOCTYPE_NAME, name)
	ensure_reviewable(doc)
	comment = (comment or "").strip()
	if not comment:
		frappe.throw(_("Give a reason for rejecting the purchase."))

	try:
		data = DigitaxClient(doc.sis_settings).put(
			"reviewPurchase",
			{"reject": True, "comment": comment},
			path_params={"purchase_id": doc.purchase_id},
			reference_doctype=PURCHASE_DOCTYPE_NAME,
			reference_name=name,
		)
	except DigitaxError as e:
		return record_failure(name, e.message)

	frappe.db.set_value(
		PURCHASE_DOCTYPE_NAME,
		name,
		{"review_status": "Rejected", "review_comment": comment, "last_error": None},
	)
	save_purchase(data, doc.sis_settings)
	return {"ok": True}


def record_failure(name: str, message: str) -> dict:
	frappe.db.set_value(
		PURCHASE_DOCTYPE_NAME, name, {"last_error": message, "last_synced_on": now_datetime()}
	)
	return {"ok": False, "message": message}


# --------------------------------------------------------------------------- #
# ERPNext documents
# --------------------------------------------------------------------------- #


def create_supplier(name: str) -> str:
	"""Create the ERPNext Supplier for a purchase from the DigiTax supplier record (already on DigiTax)."""
	doc = frappe.get_doc(PURCHASE_DOCTYPE_NAME, name)
	if doc.supplier:
		frappe.throw(_("Supplier {0} is already set.").format(doc.supplier))
	if not doc.supplier_name or not doc.supplier_id:
		frappe.throw(_("DigiTax did not return the supplier's name. Refresh the purchase and try again."))

	supplier = frappe.get_doc(
		{
			"doctype": "Supplier",
			"supplier_name": doc.supplier_name,
			"tax_id": doc.supplier_tpin,
			"supplier_group": frappe.db.get_single_value("Buying Settings", "supplier_group")
			or frappe.db.get_value("Supplier Group", {"is_group": 0}),
			"custom_sis_supplier_id": doc.supplier_id,
			"custom_sis_registered": 1,
			"custom_sis_settings": doc.sis_settings,
		}
	).insert()

	frappe.db.set_value(PURCHASE_DOCTYPE_NAME, name, "supplier", supplier.name)
	frappe.db.sql(
		f"update `tab{PURCHASE_DOCTYPE_NAME}` set supplier = %s where supplier_id = %s and ifnull(supplier, '') = ''",
		(supplier.name, doc.supplier_id),
	)
	return supplier.name


def create_purchase_invoice(name: str) -> str:
	"""Draft Purchase Invoice for an approved purchase. Line rates exclude tax; ERPNext's purchase tax
	template adds the tax, so check the total against ZRA's before submitting."""
	doc = frappe.get_doc(PURCHASE_DOCTYPE_NAME, name)
	if doc.purchase_status_code != APPROVED:
		frappe.throw(_("Approve the purchase before creating a Purchase Invoice."))
	if (
		doc.purchase_invoice
		and frappe.db.get_value("Purchase Invoice", doc.purchase_invoice, "docstatus") != 2
	):
		frappe.throw(_("Purchase Invoice {0} already exists for this purchase.").format(doc.purchase_invoice))
	if not doc.supplier:
		frappe.throw(_("Set the Supplier first, or use Create Supplier."))
	unmapped = [str(row.idx) for row in doc.items if not row.item]
	if unmapped:
		frappe.throw(_("Choose the Item for rows {0}.").format(", ".join(unmapped)))

	company = frappe.db.get_value(SETTINGS_DOCTYPE_NAME, doc.sis_settings, "company_name")
	warehouse = frappe.db.get_single_value("Stock Settings", "default_warehouse")
	if warehouse and frappe.db.get_value("Warehouse", warehouse, "company") != company:
		warehouse = None
	has_stock = any(frappe.db.get_value("Item", row.item, "is_stock_item") for row in doc.items)

	pi = frappe.new_doc("Purchase Invoice")
	pi.update(
		{
			"supplier": doc.supplier,
			"company": company,
			"bill_no": str(doc.supplier_invoice_number or doc.purchase_number or ""),
			"bill_date": doc.purchase_date,
			"update_stock": 1 if has_stock and warehouse else 0,
			"set_warehouse": warehouse if has_stock else None,
			"custom_sis_purchase": doc.name,
		}
	)
	for row in doc.items:
		qty = flt(row.approved_quantity) or flt(row.quantity)
		net_amount = flt(row.total_amount) - sum(flt(row.get(f"{key}_tax_amount")) for key in TAX_KEYS)
		pi.append(
			"items",
			{
				"item_code": row.item,
				"qty": qty,
				"rate": flt(net_amount / qty, 4) if qty else 0,
				"warehouse": warehouse if has_stock else None,
			},
		)
	pi.set_missing_values()
	pi.insert()
	pi.add_comment(
		"Info",
		_("Created from ZRA purchase {0}. ZRA total {1}, of which tax {2}.").format(
			doc.name,
			frappe.format(doc.total_amount, "Currency"),
			frappe.format(doc.total_tax_amount, "Currency"),
		),
	)

	frappe.db.set_value(PURCHASE_DOCTYPE_NAME, name, "purchase_invoice", pi.name)
	return pi.name


# --------------------------------------------------------------------------- #
# Purchase Invoice doc events
# --------------------------------------------------------------------------- #


def get_send_settings(pi) -> str | None:
	"""Settings to send this invoice under as a manual purchase, or None when it is not sent."""
	if pi.is_return or pi.get("custom_sis_purchase") or pi.get("custom_sis_is_import"):
		return None
	return frappe.db.get_value(
		SETTINGS_DOCTYPE_NAME, {"company_name": pi.company, "is_active": 1, "send_purchases_to_zra": 1}
	)


def validate_purchase_invoice(pi) -> None:
	# The field is no_copy, so duplicates and returns start without it. An amendment replaces the
	# cancelled invoice of the same ZRA purchase, so it keeps it.
	if pi.amended_from and not pi.get("custom_sis_purchase") and not pi.is_return:
		pi.custom_sis_purchase = frappe.db.get_value(
			"Purchase Invoice", pi.amended_from, "custom_sis_purchase"
		)


def before_submit_purchase_invoice(pi) -> None:
	"""Check an invoice that will be sent as a purchase can be, so problems show before it is submitted."""
	settings_name = get_send_settings(pi)
	if settings_name and get_registered_lines(pi, settings_name):
		build_manual_purchase_payload(pi, settings_name)


def on_submit_purchase_invoice(pi) -> None:
	if pi.get("custom_sis_purchase") and not pi.is_return:
		frappe.db.set_value(PURCHASE_DOCTYPE_NAME, pi.custom_sis_purchase, "purchase_invoice", pi.name)
		return

	settings_name = get_send_settings(pi)
	if settings_name and get_registered_lines(pi, settings_name):
		enqueue_job(SEND_JOB, f"Purchase Invoice::{pi.name}", purchase_invoice=pi.name)


def before_cancel_purchase_invoice(pi) -> None:
	if pi.get("custom_sis_purchase") and frappe.db.get_value(
		PURCHASE_DOCTYPE_NAME, pi.custom_sis_purchase, "is_manual"
	):
		frappe.throw(
			_(
				"This invoice was sent to ZRA as purchase {0}, and DigiTax cannot cancel purchases. Make a return (debit note) instead."
			).format(pi.custom_sis_purchase),
			title=_("Sent to ZRA"),
		)


def unlink_purchase_invoice(pi) -> None:
	"""Purchase Invoice on_cancel / on_trash: free the ZRA purchase for a new invoice."""
	if pi.get("custom_sis_purchase"):
		frappe.db.set_value(
			PURCHASE_DOCTYPE_NAME,
			{"name": pi.custom_sis_purchase, "purchase_invoice": pi.name},
			"purchase_invoice",
			None,
		)


# --------------------------------------------------------------------------- #
# Manual purchases (supplier not on Smart Invoice)
# --------------------------------------------------------------------------- #


def get_settings_for_company(company: str) -> str:
	settings = frappe.db.get_value(
		SETTINGS_DOCTYPE_NAME,
		{"company_name": company, "is_active": 1},
		["name", "send_purchases_to_zra"],
		as_dict=True,
	)
	if not settings:
		frappe.throw(_("No active ZRA SIS Settings for company {0}.").format(company))
	if not settings.send_purchases_to_zra:
		# Without it, purchase stock is reported as stock movements; sending the purchase too would add it twice.
		frappe.throw(
			_("Turn on Send Purchase Invoices to ZRA in ZRA SIS Settings {0} first.").format(settings.name)
		)
	return settings.name


def get_registered_lines(pi, settings_name: str) -> list[tuple]:
	"""(row, DigiTax item ID) for lines whose item is registered under the settings. Other lines (expenses,
	services not on ZRA) are not part of the purchase sent to ZRA."""
	lines = []
	for row in pi.items:
		item = frappe.db.get_value(
			"Item",
			row.item_code,
			["custom_smart_remote_id", "custom_smart_status", "custom_sis_settings"],
			as_dict=True,
		)
		if item and is_registered_item(item) and item.custom_sis_settings == settings_name:
			lines.append((row, item.custom_smart_remote_id))
	return lines


def build_manual_purchase_payload(pi, settings_name: str) -> dict:
	problems = []
	bill_no = (pi.bill_no or "").strip()
	if not bill_no.isdigit():
		problems.append(_("Supplier Invoice No must be the number on the supplier's invoice (digits only)."))

	supplier = frappe.get_doc("Supplier", pi.supplier)
	payload = {
		"purchase_date": str(pi.bill_date or pi.posting_date),
		"supplier_invoice_number": cint(bill_no),
		"payment_type_code": PAYMENT_CASH if pi.is_paid else PAYMENT_CREDIT,
		"items": [],
	}
	if supplier_service.is_registered(supplier):
		payload["supplier_id"] = supplier.custom_sis_supplier_id
	else:
		problems += supplier_service.get_registration_blockers(supplier)
		# Same fields as POST /suppliers; phone and email are named supplier_phone / supplier_email here.
		payload.update(supplier_service.build_registration_payload(supplier))
		for key in ("phone", "email"):
			if key in payload:
				payload[f"supplier_{key}"] = payload.pop(key)

	line_tax = {}
	tax_rows = {tax.name: tax for tax in pi.get("taxes", [])}
	for detail in pi.get("item_wise_tax_details", []):
		tax = tax_rows.get(detail.tax_row)
		if tax and tax.category != "Valuation":
			sign = -1 if tax.add_deduct_tax == "Deduct" else 1
			line_tax[detail.item_row] = line_tax.get(detail.item_row, 0) + sign * flt(detail.amount)

	lines = get_registered_lines(pi, settings_name)
	if not lines:
		problems.append(_("None of the items is registered with ZRA."))
	for row, item_id in lines:
		qty = flt(row.stock_qty) or flt(row.qty)
		total = flt(row.base_net_amount) + line_tax.get(row.name, 0)
		payload["items"].append(
			{
				"item_id": item_id,
				"quantity": qty,
				"unit_price": round(total / qty, 4) if qty else 0,
				"total_amount": round(total, 4),
				"discount_rate": 0,
				"discount_amount": 0,
			}
		)

	if problems:
		frappe.throw("<br>".join(problems), title=_("Purchase Not Ready for ZRA"))
	return payload


def report_manual_purchase(purchase_invoice: str) -> dict:
	"""POST /purchases for a submitted Purchase Invoice. When a ZRA purchase with the same supplier and
	supplier invoice number has already arrived, the invoice is linked to it instead of sending it again.
	DigiTax rejects a repeated supplier_invoice_number, which also guards against sending twice."""
	pi = frappe.get_doc("Purchase Invoice", purchase_invoice)
	if pi.docstatus != 1:
		frappe.throw(_("Submit the Purchase Invoice first."))
	if pi.is_return:
		frappe.throw(_("Returns cannot be reported as manual purchases."))
	if pi.get("custom_sis_purchase"):
		frappe.throw(_("This invoice is already linked to ZRA purchase {0}.").format(pi.custom_sis_purchase))

	received = frappe.db.get_value(
		PURCHASE_DOCTYPE_NAME,
		{
			"supplier": pi.supplier,
			"supplier_invoice_number": (pi.bill_no or "").strip(),
			"is_manual": 0,
			"purchase_invoice": ["is", "not set"],
		},
		"name",
	)
	if received:
		pi.db_set({"custom_sis_purchase": received, "custom_sis_last_error": None})
		frappe.db.set_value(PURCHASE_DOCTYPE_NAME, received, "purchase_invoice", pi.name)
		return {"ok": True, "purchase": received, "linked": True}

	settings_name = get_settings_for_company(pi.company)
	payload = build_manual_purchase_payload(pi, settings_name)
	try:
		data = DigitaxClient(settings_name).post(
			"savePurchase", payload, reference_doctype="Purchase Invoice", reference_name=pi.name
		)
	except DigitaxError as e:
		pi.db_set("custom_sis_last_error", e.message)
		return {"ok": False, "message": e.message}

	pi.db_set({"custom_sis_purchase": data.get("id"), "custom_sis_last_error": None})
	save_purchase(data, settings_name)
	frappe.db.set_value(PURCHASE_DOCTYPE_NAME, data.get("id"), {"purchase_invoice": pi.name, "is_manual": 1})
	return {"ok": True, "purchase": data.get("id")}


def report_manual_purchase_job(purchase_invoice: str) -> None:
	if frappe.db.get_value("Purchase Invoice", purchase_invoice, "custom_sis_purchase"):
		return  # a manual click got there first
	try:
		report_manual_purchase(purchase_invoice)
	except Exception as e:
		frappe.db.rollback()
		frappe.db.set_value("Purchase Invoice", purchase_invoice, "custom_sis_last_error", str(e))
		frappe.log_error(
			title=f"ZRA SIS: sending purchase {purchase_invoice} failed",
			reference_doctype="Purchase Invoice",
			reference_name=purchase_invoice,
		)
