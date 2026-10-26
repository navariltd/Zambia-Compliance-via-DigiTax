"""ZRA notices for the business (GET /notices).

DigiTax returns the full list in one response (no pagination). Notices are stored in "ZRA SIS Notice",
named by ZRA's notice number, and refreshed daily and from the ZRA SIS Settings form.
"""

from __future__ import annotations

import frappe
from frappe import _

from ..digitax.client import DigitaxClient, DigitaxError
from ..doctype.doctype_names_mapping import NOTICE_DOCTYPE_NAME, SETTINGS_DOCTYPE_NAME
from .common import get_active_settings, parse_utc_datetime

NOTICE_FIELDS = ("title", "notice_date", "content", "issued_by", "detail_url")


def fetch_notices(settings_name: str) -> dict:
	"""Pull notices and save new or changed ones. Returns {"ok": False, ...} when DigiTax rejects it."""
	try:
		data = DigitaxClient(settings_name).get(
			"fetchNotices", reference_doctype=SETTINGS_DOCTYPE_NAME, reference_name=settings_name
		)
	except DigitaxError as e:
		return {"ok": False, "message": e.message}

	notices = data if isinstance(data, list) else []
	new = sum(save_notice(notice, settings_name) for notice in notices if isinstance(notice, dict))
	return {"ok": True, "total": len(notices), "new": new}


def save_notice(notice: dict, settings_name: str) -> bool:
	"""Insert or update one notice. Returns True when it is new."""
	notice_number = str(notice.get("notice_number") or "").strip()
	if not notice_number:
		return False

	values = {
		"title": (notice.get("title") or "").strip()[:140] or _("Notice {0}").format(notice_number),
		"notice_date": parse_utc_datetime(notice.get("notice_date")),
		"content": notice.get("content") or "",
		"issued_by": notice.get("by"),
		"detail_url": notice.get("detail_url"),
	}

	if frappe.db.exists(NOTICE_DOCTYPE_NAME, notice_number):
		doc = frappe.get_doc(NOTICE_DOCTYPE_NAME, notice_number)
		if any(str(doc.get(f) or "") != str(values[f] or "") for f in NOTICE_FIELDS):
			doc.update(values)
			doc.save(ignore_permissions=True)
		return False

	frappe.get_doc(
		{
			"doctype": NOTICE_DOCTYPE_NAME,
			"notice_number": notice_number,
			"sis_settings": settings_name,
			**values,
		}
	).insert(ignore_permissions=True)
	return True


def fetch_notices_for_all_settings() -> None:
	"""Daily job."""
	for settings in get_active_settings():
		try:
			result = fetch_notices(settings.name)
			if not result.get("ok"):
				frappe.log_error(
					title=f"ZRA SIS: could not fetch notices for {settings.name}",
					message=result.get("message"),
					reference_doctype=SETTINGS_DOCTYPE_NAME,
					reference_name=settings.name,
				)
			frappe.db.commit()
		except Exception:
			frappe.db.rollback()
			frappe.log_error(
				title=f"ZRA SIS: could not fetch notices for {settings.name}",
				reference_doctype=SETTINGS_DOCTYPE_NAME,
				reference_name=settings.name,
			)
