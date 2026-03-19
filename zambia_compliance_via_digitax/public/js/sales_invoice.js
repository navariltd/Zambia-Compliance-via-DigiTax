// === Doctype definitions ===
const parentDoctype = "Sales Invoice";
const childDoctype = `${parentDoctype} Item`;


const settingsDoctypeName = " ZRA SIS Settings";

// === Real-time form refresh handler ===
frappe.realtime.on("refresh_form", function (name) {
	const currentForm = cur_frm;
	if (currentForm && currentForm.doc.name === name) {
		currentForm.reload_doc();
	}
});

// === Parent Doctype: Sales Invoice ===
frappe.ui.form.on(parentDoctype, {
	refresh: async function (frm) {
		

		if (frm.is_new()) return;

		// Fetch active VSDC settings for current company
		const { message: activeSetting } = await frappe.call({
			method: "zambia_compliance_via_digitax.zambia_compliance_via_digitax.utils.smart_api_utils.get_active_smart_settings",
			args: { doctype: settingsDoctypeName, company: frm.doc.company },
		});

		if (!activeSetting?.length || frm.doc.docstatus === 0 || frm.doc.prevent_vsdc_submission)
			return;

		// --- Send Invoice Button ---
		if (!frm.doc.custom_successfully_submitted) {
			frm.add_custom_button(
				__("Send Invoice"),
				function () {
					executeVSDCAction("Send Invoice", activeSetting, (settings_name) => ({
						method: "zambia_compliance_via_digitax.zambia_compliance_via_digitax.apis.sales_invoice.send_invoice_details",
						args: { name: frm.doc.name, settings_name: settings_name },
						success_msg: "Invoice submission queued",
					}));
				},
				__("SIS Actions")
			);
		}

		// --- Sync Invoice Button ---
		if (frm.doc.custom_successfully_submitted || frm.doc.custom_vsdc_id) {
			frm.add_custom_button(
				__("Sync Invoice Details"),
				function () {
					executeVSDCAction("Sync Invoice", activeSetting, (settings_name) => ({
						method: "zambia_compliance_via_digitax.zambia_compliance_via_digitax.apis.sales_invoice.get_invoice_details",
						args: {
							document_name: frm.doc.name,
							invoice_type: "Sales Invoice",
							settings_name: settings_name,
							company: frm.doc.company,
						},
						success_msg: "Invoice sync queued",
					}));
				},
				__("SIS Actions")
			);
		}

		// --- Verify and Fix Button ---
		// frm.add_custom_button(
		// 	__("Verify Submission and Fix if Incorrect"),
		// 	function () {
		// 		executeVSDCAction("Verify Submission", activeSetting, (settings_name) => ({
		// 			method: "ca_erpnext_zra.ca_erpnext_zra.apis.invoice_processor.verify_vsdc_invoice",
		// 			args: {
		// 				document_name: frm.doc.name,
		// 				invoice_type: "Sales Invoice",
		// 				settings_name: settings_name,
		// 				company: frm.doc.company,
		// 			},
		// 			success_msg: "Verification and correction queued",
		// 		}));
		// 	},
		// 	__("Smart Actions")
		// );
	},
});

// === Helper function: show settings modal if multiple, else call directly ===
function executeVSDCAction(title, settings, getCallArgs) {
	if (settings.length === 1) {
		const { method, args, success_msg } = getCallArgs(settings[0].name);
		frappe.call({
			method: method,
			args: args,
			callback: () => frappe.msgprint(__(success_msg)),
			error: (err) => {
				console.error(err);
				frappe.msgprint(__("An error occurred during the request."));
			},
		});
		return;
	}

	// If multiple settings exist, show selection dialog
	const dialog = new frappe.ui.Dialog({
		title: __(title),
		fields: [
			{
				label: __("Select VSDC Settings"),
				fieldname: "settings_name",
				fieldtype: "Select",
				options: settings.map((s) => ({
					label: `${s.company} (${s.name})`,
					value: s.name,
				})),
				reqd: 1,
				default: settings[0]?.name,
			},
		],
		primary_action_label: __("Proceed"),
		primary_action: ({ settings_name }) => {
			dialog.hide();
			const { method, args, success_msg } = getCallArgs(settings_name);
			frappe.call({
				method: method,
				args: args,
				callback: () => frappe.msgprint(__(success_msg)),
				error: (err) => {
					console.error(err);
					frappe.msgprint(__("An error occurred during the request."));
				},
			});
		},
	});
	dialog.show();
}



