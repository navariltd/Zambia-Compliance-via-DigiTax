# Copyright (c) 2026, Navari Ltd and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document


class ZRASISSalesLedger(Document):
	"""A fiscal record. Only services/sales.py writes it (with flags.from_sales_service, or db_set),
	and it is never deleted."""

	def validate(self):
		if not self.flags.from_sales_service:
			frappe.throw(_("ZRA SIS Sales Ledger entries are kept by the system and cannot be edited."))

	def on_trash(self):
		frappe.throw(_("ZRA SIS Sales Ledger entries are fiscal records and cannot be deleted."))
