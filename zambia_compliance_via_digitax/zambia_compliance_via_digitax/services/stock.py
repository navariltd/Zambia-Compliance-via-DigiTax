"""Stock movements to DigiTax Zambia (ZRA Smart Invoice).

DigiTax endpoints used (see fixtures/zra_sis_routes.json):
- saveStockItems  PUT /v1/stock/adjust        item_id, quantity, action ADD|DEDUCT, movement_type
- selectItem      GET /v1/items/{item_id}     stock_quantity = DigiTax's balance

How it works:
- Every Stock Ledger Entry of a registered stock item is marked Queued with its movement code on submit
  (MOVEMENT_RULES decides the code; vouchers that report nothing are left blank).
- One background worker sends the queue oldest first, so a deduction never reaches DigiTax before the
  receipt it depends on. All stock calls for a site run under one file lock.
- PUT /stock/adjust is not idempotent. Each entry is committed as Sending before the call and as Sent or
  Failed after it. An entry is never sent twice: a Failed or interrupted one is fixed by syncing the item's
  balance (sync_item_stock), which sends ERPNext's balance minus DigiTax's and marks the item's Queued,
  Failed and interrupted entries Reconciled, since ERPNext's balance already includes them.
- Stock Reconciliation entries carry no usable quantity (actual_qty is 0), so they trigger a balance sync.
- Opening stock: an item's balance is synced once when it becomes COMPLETED on DigiTax, or for all items
  from ZRA SIS Settings. Entries dated before the settings' Go-Live Date are not reported.

Stock DigiTax moves by itself is never reported here:
- Sales: DigiTax deducts stock when a sale is posted (and adds it back on a credit note), so Sales
  Invoices and Delivery Notes report nothing.
- Purchases: DigiTax adds stock when a purchase is recorded: a ZRA purchase when we approve it (lines
  linked with their quantity), an import when we approve it, and a manual purchase when it is sent. So
  incoming stock of a Purchase Invoice created from a ZRA purchase, of a voucher ticked "Is Import", and,
  with the settings' "Send Purchase Invoices to ZRA" on, of every purchase, is not reported. Without that
  setting, other purchases report movement 02. Purchase returns always report 12 (there is no purchase
  return endpoint).
"""

from __future__ import annotations

import frappe
from frappe import _
from frappe.query_builder.functions import Sum
from frappe.utils import add_to_date, cint, flt, getdate, now_datetime
from frappe.utils.synchronization import filelock

from ..digitax.client import DigitaxClient, DigitaxError
from ..doctype.doctype_names_mapping import SETTINGS_DOCTYPE_NAME
from .common import enqueue_job
from .items import is_registered

SLE = "Stock Ledger Entry"

# Stock movement codes (docs: Stock: Data Attributes)
IN_IMPORT = "01"
IN_PURCHASE = "02"
IN_RETURN = "03"
IN_MOVEMENT = "04"
IN_PROCESSING = "05"
IN_ADJUSTMENT = "06"
OUT_SALE = "11"
OUT_RETURN = "12"
OUT_MOVEMENT = "13"
OUT_PROCESSING = "14"
OUT_DISCARDING = "15"
OUT_ADJUSTMENT = "16"

# (code when qty > 0, code when qty < 0). None = not reported.
PURCHASE = (IN_PURCHASE, OUT_RETURN)
PROCESSING = (IN_PROCESSING, OUT_PROCESSING)
ADJUSTMENT = (IN_ADJUSTMENT, OUT_ADJUSTMENT)
NOT_REPORTED = (None, None)

# By voucher type. Voucher types not listed here report as adjustments.
MOVEMENT_RULES = {
	"Purchase Receipt": PURCHASE,
	"Purchase Invoice": PURCHASE,
	"Sales Invoice": NOT_REPORTED,
	"Delivery Note": NOT_REPORTED,
	"POS Invoice": NOT_REPORTED,
	"Subcontracting Receipt": PROCESSING,
}
# Stock Entry by purpose. Transfers stay inside the business, so they net to zero and are not reported.
STOCK_ENTRY_RULES = {
	"Material Receipt": ADJUSTMENT,
	"Material Issue": ADJUSTMENT,
	"Manufacture": PROCESSING,
	"Repack": PROCESSING,
	"Disassemble": PROCESSING,
	"Material Consumption for Manufacture": PROCESSING,
	"Material Transfer": NOT_REPORTED,
	"Material Transfer for Manufacture": NOT_REPORTED,
	"Send to Subcontractor": NOT_REPORTED,
}
PURCHASE_VOUCHERS = ("Purchase Receipt", "Purchase Invoice")

STATUS_QUEUED = "Queued"
STATUS_SENDING = "Sending"
STATUS_SENT = "Sent"
STATUS_FAILED = "Failed"
STATUS_RECONCILED = "Reconciled"

ADD = "ADD"
DEDUCT = "DEDUCT"

PROCESS_QUEUE_JOB = (
	"zambia_compliance_via_digitax.zambia_compliance_via_digitax.services.stock.process_stock_queue"
)
SYNC_ITEM_JOB = (
	"zambia_compliance_via_digitax.zambia_compliance_via_digitax.services.stock.sync_item_stock_job"
)
SYNC_ALL_JOB = "zambia_compliance_via_digitax.zambia_compliance_via_digitax.services.stock.sync_all_stock_job"

LOCK_NAME = "zra_sis_stock"
LOCK_TIMEOUT = 120
MAX_PER_RUN = 500
# A Sending entry older than this was interrupted mid-call; whether DigiTax applied it is unknown.
INTERRUPTED_AFTER_MINUTES = 10


def interrupted_message() -> str:
	return _(
		"DigiTax may or may not have applied this movement, so it will not be sent again. Use Sync Stock with ZRA on the item to fix the balance."
	)


# --------------------------------------------------------------------------- #
# Settings
# --------------------------------------------------------------------------- #


def get_stock_settings(company: str) -> dict | None:
	"""Active settings for the company with stock reporting on, or None."""
	if not company:
		return None
	settings = frappe.get_all(
		SETTINGS_DOCTYPE_NAME,
		filters={"company_name": company, "is_active": 1, "stock_auto_submission_enabled": 1},
		fields=["name", "go_live_date", "send_purchases_to_zra"],
		limit=1,
	)
	return settings[0] if settings else None


# --------------------------------------------------------------------------- #
# Movement codes
# --------------------------------------------------------------------------- #


def get_movement_type(sle, settings: dict) -> str | None:
	"""Movement code for a Stock Ledger Entry, or None when it is not reported."""
	qty = flt(sle.actual_qty)
	if not qty:
		return None

	if sle.is_cancelled:
		return get_reversal_movement_type(sle, qty)

	if sle.voucher_type in PURCHASE_VOUCHERS and qty > 0 and is_added_by_digitax_purchase(sle, settings):
		return None

	if sle.voucher_type == "Stock Entry":
		purpose = frappe.db.get_value("Stock Entry", sle.voucher_no, "purpose")
		rule = STOCK_ENTRY_RULES.get(purpose, ADJUSTMENT)
	else:
		rule = MOVEMENT_RULES.get(sle.voucher_type, ADJUSTMENT)

	return rule[0] if qty > 0 else rule[1]


def is_added_by_digitax_purchase(sle, settings: dict) -> bool:
	"""Whether DigiTax adds this incoming purchase stock itself, through a purchase or import record."""
	if settings.get("send_purchases_to_zra"):
		return True  # the Purchase Invoice is sent as a purchase (services/purchases.py)
	if frappe.db.get_value(sle.voucher_type, sle.voucher_no, "custom_sis_is_import"):
		return True  # added when the import was approved
	if sle.voucher_type == "Purchase Invoice":
		return bool(frappe.db.get_value("Purchase Invoice", sle.voucher_no, "custom_sis_purchase"))
	# A Purchase Receipt made from a Purchase Invoice that came from a ZRA purchase
	invoices = frappe.get_all(
		"Purchase Receipt Item",
		filters={"parent": sle.voucher_no, "purchase_invoice": ["is", "set"]},
		pluck="purchase_invoice",
		distinct=True,
	)
	return bool(
		invoices
		and frappe.db.exists(
			"Purchase Invoice", {"name": ["in", invoices], "custom_sis_purchase": ["is", "set"]}
		)
	)


def get_reversal_movement_type(sle, qty: float) -> str | None:
	"""Cancelling a voucher adds entries with the opposite quantity. Report one only if the entry it
	reverses reached (or is on its way to) DigiTax, as an adjustment."""
	reversed_entry_sent = frappe.db.exists(
		SLE,
		{
			"name": ["!=", sle.name],
			"voucher_type": sle.voucher_type,
			"voucher_no": sle.voucher_no,
			"voucher_detail_no": sle.voucher_detail_no,
			"item_code": sle.item_code,
			"warehouse": sle.warehouse,
			"actual_qty": -qty,
			"custom_sis_stock_status": ["in", [STATUS_QUEUED, STATUS_SENDING, STATUS_SENT]],
		},
	)
	if not reversed_entry_sent:
		return None
	return IN_ADJUSTMENT if qty > 0 else OUT_ADJUSTMENT


# --------------------------------------------------------------------------- #
# Queueing (Stock Ledger Entry on_submit)
# --------------------------------------------------------------------------- #


def queue_stock_ledger_entry(sle) -> None:
	if sle.get("via_landed_cost_voucher"):
		return  # landed costs re-post the same quantities; nothing moved
	if not flt(sle.actual_qty) and sle.voucher_type != "Stock Reconciliation":
		return

	settings = get_stock_settings(sle.company)
	if not settings:
		return
	if settings.go_live_date and getdate(sle.posting_date) < getdate(settings.go_live_date):
		return

	item = frappe.db.get_value(
		"Item",
		sle.item_code,
		["is_stock_item", "custom_smart_remote_id", "custom_smart_status", "custom_sis_settings"],
		as_dict=True,
	)
	if (
		not item
		or not item.is_stock_item
		or not is_registered(item)
		or item.custom_sis_settings != settings.name
	):
		return

	if sle.voucher_type == "Stock Reconciliation":
		movement_type = None  # the worker syncs the balance instead
	else:
		movement_type = get_movement_type(sle, settings)
		if not movement_type:
			return

	sle.db_set(
		{"custom_sis_stock_status": STATUS_QUEUED, "custom_sis_movement_type": movement_type},
		update_modified=False,
	)
	enqueue_job(PROCESS_QUEUE_JOB, "queue", queue="long")


# --------------------------------------------------------------------------- #
# Worker
# --------------------------------------------------------------------------- #


def process_stock_queue() -> None:
	"""Send Queued entries one at a time, oldest first."""
	fail_interrupted_movements()

	for _i in range(MAX_PER_RUN):
		name = frappe.db.get_value(
			SLE, {"custom_sis_stock_status": STATUS_QUEUED}, "name", order_by="creation asc, name asc"
		)
		if not name:
			return
		process_movement(name)

	enqueue_job(PROCESS_QUEUE_JOB, "queue", queue="long")  # more left; continue in a fresh job


def requeue_stuck_movements() -> None:
	"""Hourly safety net: an entry queued while the worker was finishing can miss its run."""
	if frappe.db.exists(SLE, {"custom_sis_stock_status": STATUS_QUEUED}):
		enqueue_job(PROCESS_QUEUE_JOB, "queue", queue="long")


def fail_interrupted_movements() -> None:
	cutoff = add_to_date(now_datetime(), minutes=-INTERRUPTED_AFTER_MINUTES)
	for name in frappe.get_all(
		SLE,
		filters={"custom_sis_stock_status": STATUS_SENDING, "custom_sis_synced_on": ["<", cutoff]},
		pluck="name",
	):
		set_entry_status(
			name, STATUS_FAILED, error=_("Sending was interrupted.") + " " + interrupted_message()
		)
	frappe.db.commit()


def process_movement(name: str) -> None:
	with filelock(LOCK_NAME, timeout=LOCK_TIMEOUT):
		sle = frappe.db.get_value(
			SLE,
			name,
			[
				"name",
				"item_code",
				"actual_qty",
				"voucher_type",
				"custom_sis_stock_status",
				"custom_sis_movement_type",
			],
			as_dict=True,
			for_update=True,
		)
		if not sle or sle.custom_sis_stock_status != STATUS_QUEUED:
			frappe.db.commit()
			return

		# Committed before the call: if this process dies mid-request, the entry is not sent again.
		set_entry_status(name, STATUS_SENDING)
		frappe.db.commit()

		try:
			if sle.voucher_type == "Stock Reconciliation":
				result = sync_balance(sle.item_code, reference_doctype=SLE, reference_name=name)
				values = {
					"custom_sis_movement_type": result.get("movement_type"),
					"custom_sis_stock_balance": result.get("digitax_qty"),
				}
			else:
				data = send_movement(
					sle.item_code,
					flt(sle.actual_qty),
					sle.custom_sis_movement_type,
					reference_doctype=SLE,
					reference_name=name,
				)
				values = {"custom_sis_stock_balance": data.get("stock_quantity")}
		except DigitaxError as e:
			message = e.message if e.status_code else f"{e.message} {interrupted_message()}"
			set_entry_status(name, STATUS_FAILED, error=message)
			frappe.db.commit()
			return
		except Exception as e:
			frappe.db.rollback()
			set_entry_status(name, STATUS_FAILED, error=str(e) or _("Unexpected error"))
			frappe.log_error(
				title=f"ZRA SIS: stock movement {name} failed", reference_doctype=SLE, reference_name=name
			)
			frappe.db.commit()
			return

		set_entry_status(name, STATUS_SENT, **values)
		frappe.db.commit()


def set_entry_status(name: str, status: str, error: str | None = None, **values) -> None:
	frappe.db.set_value(
		SLE,
		name,
		{
			"custom_sis_stock_status": status,
			"custom_sis_last_error": error,
			"custom_sis_synced_on": now_datetime(),
			**values,
		},
		update_modified=False,
	)


# --------------------------------------------------------------------------- #
# DigiTax calls
# --------------------------------------------------------------------------- #


def get_registered_item(item_code: str) -> dict:
	item = frappe.db.get_value(
		"Item",
		item_code,
		["name", "is_stock_item", "custom_smart_remote_id", "custom_smart_status", "custom_sis_settings"],
		as_dict=True,
	)
	if not item or not is_registered(item):
		frappe.throw(_("Item {0} is not registered with ZRA.").format(item_code))
	if not item.is_stock_item:
		frappe.throw(_("Item {0} does not maintain stock.").format(item_code))
	return item


def send_movement(
	item_code: str,
	qty: float,
	movement_type: str,
	reference_doctype: str | None = None,
	reference_name: str | None = None,
) -> dict:
	"""PUT /stock/adjust for one movement. `qty` is signed: positive adds, negative deducts.
	Raises DigitaxError. Callers must make sure the same movement is never sent twice."""
	item = get_registered_item(item_code)
	data = (
		DigitaxClient(item.custom_sis_settings).put(
			"saveStockItems",
			{
				"item_id": item.custom_smart_remote_id,
				"quantity": round(abs(flt(qty)), 4),
				"action": ADD if qty > 0 else DEDUCT,
				"movement_type": movement_type,
			},
			reference_doctype=reference_doctype or "Item",
			reference_name=reference_name or item_code,
		)
		or {}
	)
	save_item_balance(item_code, data.get("stock_quantity"))
	return data


def get_digitax_balance(item) -> float:
	data = (
		DigitaxClient(item.custom_sis_settings).get(
			"selectItem",
			path_params={"item_id": item.custom_smart_remote_id},
			reference_doctype="Item",
			reference_name=item.name,
		)
		or {}
	)
	return flt(data.get("stock_quantity"))


def save_item_balance(item_code: str, digitax_qty) -> None:
	if digitax_qty is None:
		return
	frappe.db.set_value(
		"Item",
		item_code,
		{"custom_sis_stock_quantity": flt(digitax_qty), "custom_sis_stock_synced_on": now_datetime()},
		update_modified=False,
	)


# --------------------------------------------------------------------------- #
# Balance sync (opening stock, Stock Reconciliation, fixing failed movements)
# --------------------------------------------------------------------------- #


def get_erpnext_balance(item_code: str, company: str) -> float:
	bin_ = frappe.qb.DocType("Bin")
	warehouse = frappe.qb.DocType("Warehouse")
	result = (
		frappe.qb.from_(bin_)
		.join(warehouse)
		.on(bin_.warehouse == warehouse.name)
		.select(Sum(bin_.actual_qty))
		.where((bin_.item_code == item_code) & (warehouse.company == company))
	).run()
	return flt(result[0][0]) if result else 0


def get_stock_comparison(item_code: str) -> dict:
	"""ERPNext vs DigiTax balance for an item. Raises DigitaxError."""
	item = get_registered_item(item_code)
	company = frappe.db.get_value(SETTINGS_DOCTYPE_NAME, item.custom_sis_settings, "company_name")
	if not company:
		frappe.throw(_("Set the Company on ZRA SIS Settings {0}.").format(item.custom_sis_settings))

	erpnext_qty = get_erpnext_balance(item.name, company)
	digitax_qty = get_digitax_balance(item)
	return {
		"item": item,
		"company": company,
		"erpnext_qty": erpnext_qty,
		"digitax_qty": digitax_qty,
		"difference": round(erpnext_qty - digitax_qty, 4),
	}


def sync_balance(
	item_code: str, reference_doctype: str | None = None, reference_name: str | None = None
) -> dict:
	"""Send the difference between ERPNext's and DigiTax's balance as one adjustment.
	Call under the stock lock and commit before releasing it. Raises DigitaxError."""
	comparison = get_stock_comparison(item_code)
	difference = comparison["difference"]
	result = {"movement_type": None, "digitax_qty": comparison["digitax_qty"], "difference": difference}

	if difference:
		movement_type = IN_ADJUSTMENT if difference > 0 else OUT_ADJUSTMENT
		data = send_movement(item_code, difference, movement_type, reference_doctype, reference_name)
		result.update(movement_type=movement_type, digitax_qty=flt(data.get("stock_quantity")))
	else:
		save_item_balance(item_code, comparison["digitax_qty"])

	# ERPNext's balance (for this company) already includes every entry still to be sent, Failed or
	# interrupted, so the sync covered them. Sending a Queued one afterwards would add it a second time.
	frappe.db.sql(
		"""update `tabStock Ledger Entry` set custom_sis_stock_status = %s
		where item_code = %s and company = %s and custom_sis_stock_status in (%s, %s, %s) and name != %s""",
		(
			STATUS_RECONCILED,
			item_code,
			comparison["company"],
			STATUS_QUEUED,
			STATUS_FAILED,
			STATUS_SENDING,
			reference_name or "",
		),
	)
	return result


def compare_item_stock(item_code: str) -> dict:
	"""For the Item form: show both balances before syncing."""
	try:
		comparison = get_stock_comparison(item_code)
	except DigitaxError as e:
		return {"ok": False, "message": e.message}
	comparison.pop("item")
	comparison.pop("company")
	return {"ok": True, **comparison}


def sync_item_stock(item_code: str) -> dict:
	"""Make DigiTax's balance match ERPNext's. Returns {"ok": False, ...} when DigiTax rejects it."""
	try:
		with filelock(LOCK_NAME, timeout=LOCK_TIMEOUT):
			result = sync_balance(item_code)
			frappe.db.commit()  # so the worker sees the Reconciled entries once it gets the lock
	except DigitaxError as e:
		frappe.db.set_value("Item", item_code, "custom_sis_last_error", e.message, update_modified=False)
		return {"ok": False, "message": e.message}
	return {"ok": True, **result}


def schedule_opening_stock(item_name: str) -> None:
	"""Called when an item becomes COMPLETED on DigiTax: send its current balance once."""
	item = frappe.db.get_value("Item", item_name, ["is_stock_item", "custom_sis_settings"], as_dict=True)
	if not item or not item.is_stock_item or not item.custom_sis_settings:
		return
	if not frappe.db.get_value(
		SETTINGS_DOCTYPE_NAME, item.custom_sis_settings, "stock_auto_submission_enabled"
	):
		return
	enqueue_job(SYNC_ITEM_JOB, f"Item::{item_name}", queue="long", item_name=item_name)


def sync_item_stock_job(item_name: str) -> None:
	try:
		sync_item_stock(item_name)
	except Exception:
		frappe.log_error(
			title=f"ZRA SIS: stock sync failed for {item_name}",
			reference_doctype="Item",
			reference_name=item_name,
		)


def enqueue_sync_all_stock(settings_name: str) -> None:
	enqueue_job(SYNC_ALL_JOB, settings_name, queue="long", timeout=3600, settings_name=settings_name)


def sync_all_stock_job(settings_name: str) -> None:
	"""Sync every registered stock item under the settings. Used once at go-live and after outages."""
	items = frappe.get_all(
		"Item",
		filters={
			"custom_sis_settings": settings_name,
			"custom_smart_remote_id": ["is", "set"],
			"custom_smart_status": ["!=", "FAILED"],
			"is_stock_item": 1,
			"disabled": 0,
		},
		pluck="name",
	)
	failed = []
	for item_name in items:
		try:
			result = sync_item_stock(item_name)
		except Exception:
			frappe.log_error(
				title=f"ZRA SIS: stock sync failed for {item_name}",
				reference_doctype="Item",
				reference_name=item_name,
			)
			result = {"ok": False}
		frappe.db.commit()
		if not result.get("ok"):
			failed.append(item_name)

	if failed:
		frappe.log_error(
			title=_("ZRA SIS: stock sync failed for {0} of {1} items").format(len(failed), len(items)),
			message=_("See Last SIS Error on these items:") + "\n" + "\n".join(failed),
			reference_doctype=SETTINGS_DOCTYPE_NAME,
			reference_name=settings_name,
		)
