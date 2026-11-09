// Copyright (c) 2026, Navari Ltd and contributors
// For license information, please see license.txt

frappe.ui.form.on("ZRA SIS Sales Ledger", {
	refresh(frm) {
		frm.disable_save();
		const detail = frm.doc.status === "Not Ready" ? frm.doc.problems : frm.doc.last_error;
		if (detail) {
			frm.dashboard.set_headline_alert(
				frappe.utils.escape_html(detail).replace(/\n/g, "<br>"),
				["Rejected", "Failed"].includes(frm.doc.status) ? "red" : "orange"
			);
		}
		if (!frm.doc.document_key || frm.doc.reference_doctype !== "Sales Invoice") return;

		const API = "zambia_compliance_via_digitax.zambia_compliance_via_digitax.apis.sales";
		if (["Not Ready", "Rejected", "Unknown", "Failed", "Queued"].includes(frm.doc.status)) {
			frm.add_custom_button(__("Retry"), () =>
				frappe.call({
					method: `${API}.send_or_retry`,
					args: { invoice: frm.doc.reference_name },
					freeze: true,
					callback: () => frm.reload_doc(),
				})
			);
		}
		if (frm.doc.sale_id && frm.doc.status !== "Completed") {
			frm.add_custom_button(__("Check Status"), () =>
				frappe.call({
					method: `${API}.check_status`,
					args: { invoice: frm.doc.reference_name },
					freeze: true,
					callback: () => frm.reload_doc(),
				})
			);
		}
	},
});
