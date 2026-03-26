const doctypeName = "Customer";

frappe.ui.form.on(doctypeName, {
	refresh: async function (frm) {
		if (frm.is_new()) return;

		// Fetch Smart compliance data for this customer
		const { message: data } = await frappe.call({
			method: "zambia_compliance_via_digitax.zambia_compliance_via_digitax.utils.smart_api_utils.get_smart_action_data",
			args: {
				doctype: frm.doctype,
				docname: frm.doc.name,
			},
		});

		const allSettings = data?.settings || [];
		const registered = data?.registered;

		if (!allSettings.length) return;

		// ---------------- UNREGISTERED ----------------
		if (!registered) {
			const unregisteredSettings = data?.unregistered_settings || [];
			if (unregisteredSettings.length > 0) {
				const settingsName = unregisteredSettings[0].name; // pick first
				frm.add_custom_button(
					__("Register Customer (SIS)"),
					function () {
						executeSmartCustomerAction(frm, "register_customer", settingsName);
					},
					__("SIS Actions")
				);
			}
		}

		// ---------------- REGISTERED ----------------
		if (registered) {
			const registeredSettings = data?.registered_mappings || [];
			if (registeredSettings.length > 0) {
				const settingsName = registeredSettings[0].smart_setup; // pick first
				frm.add_custom_button(
					__("Fetch Customer Details"),
					function () {
						executeSmartCustomerAction(frm, "fetch_customer_details", settingsName);
					},
					__("SIS Actions")
				);
			}
		}
	},
});

// ---------------- Execute Action ----------------
function executeSmartCustomerAction(frm, actionType, settingsName) {
	let method;

	switch (actionType) {
		case "register_customer":
			method = "zambia_compliance_via_digitax.zambia_compliance_via_digitax.apis.customer.perform_customer_registration";
			break;

		case "fetch_customer_details":
			method = "zambia_compliance_via_digitax.zambia_compliance_via_digitax.apis.customer.fetch_customer_details";
			break;

		default:
			frappe.msgprint(__("Unknown action type."));
			return;
	}

	frappe.call({
		method,
		args: {
			doc: frm.doc,
			settings_name: settingsName
		},
		callback: () => {
			const messages = {
				register_customer: "Customer registration queued successfully.",
				fetch_customer_details: "Customer details fetch queued."
			};

			frappe.msgprint(messages[actionType] || "SIS request queued.");
		},
		error: (err) => {
			frappe.msgprint(__("An error occurred during the SIS request."));
			console.error(err);
		},
	});
}