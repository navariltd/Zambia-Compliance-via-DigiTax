const itemDoctypeName = "Item";

frappe.ui.form.on(itemDoctypeName, {
	refresh: async function (frm) {
		if (frm.is_new()) return;

		//  Fetch Smart compliance data for this item
		const { message: data } = await frappe.call({
			method: "zambia_compliance_via_digitax.zambia_compliance_via_digitax.utils.smart_api_utils.get_smart_action_data",
			args: {
				doctype: frm.doctype,
				docname: frm.doc.name,
			},
		});

		const allSettings = data?.settings || [];
		const registeredMappings = data?.registered_mappings || [];
		const unregisteredSettings = data?.unregistered_settings || [];

		if (!allSettings.length) return;

		const registered = data?.registered;

		// 🔹 For registered and unregistered items, show multi-company aware actions
		if (!frm.is_new()) {
			if (!registered) {
				// Show "Register Item" for unregistered companies
				const canRegister = unregisteredSettings.length > 0;
				if (canRegister) {
					frm.add_custom_button(
						__("Register Item (SIS)"),
						function () {
							showCompanySelectionModal(frm, "register_item", unregisteredSettings);
						},
						__("SIS Actions")
					);
				}
			} else {
				// Registered mappings found → enable fetch, update, inventory
				const mappedCompanies = registeredMappings.map((r) => ({
					name: r.smart_setup,
					company: getCompanyName(allSettings, r.smart_setup),
				}));

		
				frm.add_custom_button(
					__("Fetch Item Details"),
					function () {
						showCompanySelectionModal(frm, "fetch_item_details", mappedCompanies);
					},
					__("SIS Actions")
				);
			

			}
		}
	},

	// Optional: sync product type with stock flag
	custom_product_type_name: function (frm) {
		frm.set_value("is_stock_item", frm.doc.custom_product_type_name !== "Service" ? 1 : 0);
	},
});

// 🔹 Helper: Get company name from settings
function getCompanyName(allSettings, settingName) {
    const match = allSettings.find((s) =>
        s.name === settingName || 
        s.settings_name === settingName ||
        s.setup === settingName
    );
    return match ? match.company : "Unknown";
}


// 🔹 Company selection modal
async function showCompanySelectionModal(frm, actionType, availableSettings) {
	if (!availableSettings.length) {
		frappe.msgprint(
			__("No available SIS settings for this action. Please check configuration.")
		);
		return;
	}

	// If only one company available → skip dialog
	// if (availableSettings.length === 1) {
	// 	executeSmartItemAction(frm, actionType, availableSettings[0].name);
	// 	return;
	// }

	const options = availableSettings.map((setting) => ({
		label: `${setting.company} (${setting.name})`,
		value: setting.name,
	}));

	const dialog = new frappe.ui.Dialog({
		title: __("Select Company Setup"),
		fields: [
			{
				label: __("Select Smart Setup"),
				fieldname: "selected_settings_name",
				fieldtype: "Select",
				options: options.map(o => `${o.label}`),
				reqd: 1,
				default: options[0]?.value || null,
			},
			// 			{
			// 	label: __("Branch"),
			// 	fieldname: "selected_branch",
			// 	fieldtype: "Link",
			// 	options: "Branch",
			// 	reqd: 1,
			// }
		],
		primary_action_label: __("Proceed"),
		primary_action: (data) => {
			const selectedSettingName = data.selected_settings_name;
			// const selectedBranch = data.selected_branch || null;
    		// const selectedBranch = dialog.get_value("selected_branch") || null;

			dialog.hide();
			executeSmartItemAction(frm, actionType, selectedSettingName);
		},
	});

	dialog.show();
}

// 🔹 Execute Smart API call per selected company
function executeSmartItemAction(frm, actionType, settingsName) {
	let method;

	switch (actionType) {
		case "register_item":
			method = "zambia_compliance_via_digitax.zambia_compliance_via_digitax.apis.item.perform_item_registration";
			break;
		case "fetch_item_details":
			method = "zambia_compliance_via_digitax.zambia_compliance_via_digitax.apis.item.fetch_item_details";
			break;

	

		default:
			frappe.msgprint(__("Unknown action type."));
			return;
	}

	frappe.call({
		method,
		args: {
			doc: frm.doc,
			item_name: frm.doc.item_name,
			item_code: frm.doc.item_code,
			item_id: frm.doc.custom_smart_remote_id,
			settings_name: settingsName,
		
		},

		callback: () => {
			const messages = {
				register_item: "Smart Item Registration Queued. Please check later.",
		
			};
			frappe.msgprint(messages[actionType] || "SIS Request queued.");
		},
		error: (error) => {
			frappe.msgprint(__("An error occurred during the SIS request."));
			console.error(error);
		},
	});
}
