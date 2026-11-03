// Copyright (c) 2026, Navari Ltd and contributors
// For license information, please see license.txt

(() => {
	const API = "zambia_compliance_via_digitax.zambia_compliance_via_digitax.apis.purchase";
	const WAIT_FOR_APPROVAL = "01";
	const APPROVED = "02";

	frappe.ui.form.on("ZRA SIS Purchases", {
		setup(frm) {
			// Only items registered with the same DigiTax business can be linked.
			frm.set_query("item", "items", () => ({
				filters: { custom_sis_settings: frm.doc.sis_settings, custom_smart_remote_id: ["is", "set"] },
			}));
		},

		refresh(frm) {
			if (frm.is_new()) return;

			if (frm.doc.last_error) {
				frm.dashboard.set_headline_alert(frappe.utils.escape_html(frm.doc.last_error), "red");
			}

			if (frm.doc.purchase_status_code === WAIT_FOR_APPROVAL) {
				frm.add_custom_button(__("Approve"), () => approve(frm)).addClass("btn-primary");
				frm.add_custom_button(__("Reject"), () => reject(frm));
			}

			if (!frm.doc.supplier && frm.doc.supplier_id) {
				frm.add_custom_button(__("Create Supplier"), () =>
					call(frm, "create_supplier", __("Creating supplier..."), (name) =>
						frappe.show_alert({ message: __("Supplier {0} created", [name]), indicator: "green" })
					)
				);
			}

			if (frm.doc.purchase_status_code === APPROVED && !frm.doc.purchase_invoice) {
				frm.add_custom_button(__("Create Purchase Invoice"), () => {
					if (frm.is_dirty()) return frappe.msgprint(__("Save the purchase first."));
					call(frm, "create_purchase_invoice", __("Creating Purchase Invoice..."), (name) =>
						frappe.set_route("Form", "Purchase Invoice", name)
					);
				});
			}

			frm.add_custom_button(
				__("Refresh from DigiTax"),
				() => call(frm, "refresh_purchase", __("Reading the purchase from DigiTax...")),
				__("ZRA SIS")
			);
		},
	});

	function approve(frm) {
		if (frm.is_dirty()) return frappe.msgprint(__("Save the item mapping before approving."));
		frappe.confirm(
			__("Link every line to its Item and approve this purchase with ZRA? This cannot be undone."),
			() => call(frm, "approve_purchase", __("Approving with DigiTax..."))
		);
	}

	function reject(frm) {
		frappe.prompt(
			{ label: __("Reason"), fieldname: "comment", fieldtype: "Small Text", reqd: 1 },
			(values) => call(frm, "reject_purchase", __("Rejecting with DigiTax..."), null, { comment: values.comment }),
			__("Reject Purchase"),
			__("Reject")
		);
	}

	function call(frm, action, freezeMessage, onSuccess, args = {}) {
		frappe.call({
			method: `${API}.${action}`,
			args: { name: frm.doc.name, ...args },
			freeze: true,
			freeze_message: freezeMessage,
			callback: (r) => {
				const result = r.message;
				if (result && result.ok === false) {
					frappe.msgprint({
						title: __("DigiTax Rejected the Request"),
						message: frappe.utils.escape_html(result.message || __("Unknown error")),
						indicator: "red",
					});
				} else if (onSuccess) {
					onSuccess(result);
				}
				frm.reload_doc();
			},
		});
	}
})();
