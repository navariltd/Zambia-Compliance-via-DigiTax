// Copyright (c) 2026, Navari Ltd and contributors
// For license information, please see license.txt

(() => {
	const API = "zambia_compliance_via_digitax.zambia_compliance_via_digitax.apis.imports";
	const REVIEWABLE = ["1", "2"];

	frappe.ui.form.on("ZRA SIS Import", {
		setup(frm) {
			// Only items registered with the same DigiTax business can be linked.
			frm.set_query("item", () => ({
				filters: { custom_sis_settings: frm.doc.sis_settings, custom_smart_remote_id: ["is", "set"] },
			}));
		},

		refresh(frm) {
			if (frm.is_new()) return;

			if (frm.doc.last_error) {
				frm.dashboard.set_headline_alert(frappe.utils.escape_html(frm.doc.last_error), "red");
			}

			if (REVIEWABLE.includes(frm.doc.import_item_status_code)) {
				frm.add_custom_button(__("Approve"), () => review(frm, "approve_import")).addClass("btn-primary");
				frm.add_custom_button(__("Reject"), () => review(frm, "reject_import"));
			}
		},
	});

	function review(frm, action) {
		if (frm.is_dirty()) return frappe.msgprint(__("Save the import first."));
		const approving = action === "approve_import";
		frappe.prompt(
			{
				label: approving ? __("Remarks (optional)") : __("Reason"),
				fieldname: "remarks",
				fieldtype: "Small Text",
				reqd: approving ? 0 : 1,
				description: approving
					? __("The import is linked to Item {0} on DigiTax, then approved. This cannot be undone.", [
							frm.doc.item || "-",
					  ])
					: "",
			},
			(values) =>
				frappe.call({
					method: `${API}.${action}`,
					args: { name: frm.doc.name, remarks: values.remarks },
					freeze: true,
					freeze_message: __("Sending the review to DigiTax..."),
					callback: (r) => {
						const result = r.message || {};
						if (result.ok === false) {
							frappe.msgprint({
								title: __("DigiTax Rejected the Request"),
								message: frappe.utils.escape_html(result.message || __("Unknown error")),
								indicator: "red",
							});
						}
						frm.reload_doc();
					},
				}),
			approving ? __("Approve Import") : __("Reject Import"),
			approving ? __("Approve") : __("Reject")
		);
	}
})();
