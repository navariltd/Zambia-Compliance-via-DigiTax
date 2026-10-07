// Wrapped in a function so names don't clash with other form scripts loaded on the same page.
(() => {
	const CUSTOMER_API = "zambia_compliance_via_digitax.zambia_compliance_via_digitax.apis.customer";

	frappe.ui.form.on("Customer", {
		onload(frm) {
			frappe.call({
				method: "zambia_compliance_via_digitax.zambia_compliance_via_digitax.utils.smart_api_utils.get_active_smart_settings",
				callback: (r) => {
					frm.zra_settings = r.message || [];
					frm.trigger("refresh_sis_actions");
				},
			});
		},

		refresh(frm) {
			frm.trigger("refresh_sis_actions");
		},

		refresh_sis_actions(frm) {
			if (frm.is_new() || !frm.zra_settings?.length) return;
			const group = __("ZRA SIS");

			if (!frm.doc.custom_sis_customer_id) {
				frm.add_custom_button(__("Register with ZRA"), () => registerCustomer(frm), group);
				frm.add_custom_button(__("Link Existing DigiTax Customer"), () => linkExistingCustomer(frm), group);
			} else {
				frm.add_custom_button(
					__("Check DigiTax Record"),
					() => runCustomerAction(frm, "refresh_customer", {}, __("Reading from DigiTax...")),
					group
				);
			}
		},
	});

	// Use the settings the customer is registered under, the only active one, or ask.
	function pickSettings(frm, callback) {
		const settings = frm.zra_settings || [];
		if (frm.doc.custom_sis_settings) return callback(frm.doc.custom_sis_settings);
		if (settings.length === 1) return callback(settings[0].name);

		frappe.prompt(
			{
				label: __("ZRA SIS Settings"),
				fieldname: "settings_name",
				fieldtype: "Select",
				reqd: 1,
				options: settings.map((s) => ({
					label: `${s.company || s.name} (${s.environment || __("unverified")})`,
					value: s.name,
				})),
				default: settings[0]?.name,
			},
			(values) => callback(values.settings_name),
			__("Choose DigiTax Business"),
			__("Continue")
		);
	}

	function registerCustomer(frm) {
		if (frm.is_dirty()) {
			frappe.msgprint(__("Save the customer before registering it."));
			return;
		}
		pickSettings(frm, (settings_name) =>
			runCustomerAction(frm, "register_customer", { settings_name }, __("Registering with DigiTax..."))
		);
	}

	function linkExistingCustomer(frm) {
		pickSettings(frm, (settings_name) => {
			frappe.prompt(
				{
					label: __("DigiTax Customer ID"),
					fieldname: "customer_id",
					fieldtype: "Data",
					reqd: 1,
					placeholder: "customer_01HC3HTDAXQ50E07CWR2A2CFBD",
					description: __(
						"The customer's ID on DigiTax, starting with customer_. Its TPIN must match this customer's Tax ID."
					),
				},
				(values) => {
					const customer_id = (values.customer_id || "").trim();
					if (!customer_id.startsWith("customer_")) {
						frappe.msgprint(__("Enter the DigiTax customer ID. It starts with customer_."));
						return;
					}
					runCustomerAction(
						frm,
						"link_existing_customer",
						{ settings_name, customer_id },
						__("Looking up the customer on DigiTax...")
					);
				},
				__("Link Existing DigiTax Customer"),
				__("Link")
			);
		});
	}

	function runCustomerAction(frm, action, args, freezeMessage) {
		frappe.call({
			method: `${CUSTOMER_API}.${action}`,
			args: { customer: frm.doc.name, ...args },
			freeze: true,
			freeze_message: freezeMessage,
			callback: (r) => {
				const result = r.message || {};
				if (result.ok === false) {
					frappe.msgprint({
						title: __("DigiTax Rejected the Request"),
						message: frappe.utils.escape_html(result.message || __("Unknown error")),
						indicator: "red",
					});
				} else if (result.differences?.length) {
					frappe.msgprint({
						title: __("ERPNext and DigiTax Differ"),
						message:
							__("DigiTax has no customer update endpoint.") +
							"<ul>" +
							result.differences.map((d) => `<li>${frappe.utils.escape_html(d)}</li>`).join("") +
							"</ul>",
						indicator: "orange",
					});
				} else {
					frappe.show_alert({ message: __("DigiTax customer ID: {0}", [result.id || "-"]), indicator: "green" }, 7);
				}
				frm.reload_doc();
			},
		});
	}
})();
