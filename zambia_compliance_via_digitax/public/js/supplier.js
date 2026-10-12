// Wrapped in a function so names don't clash with other form scripts loaded on the same page.
(() => {
	const SUPPLIER_API = "zambia_compliance_via_digitax.zambia_compliance_via_digitax.apis.supplier";

	frappe.ui.form.on("Supplier", {
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

			if (!frm.doc.custom_sis_supplier_id) {
				frm.add_custom_button(__("Register with ZRA"), () => registerSupplier(frm), group);
				frm.add_custom_button(__("Link Existing DigiTax Supplier"), () => linkExistingSupplier(frm), group);
			} else {
				frm.add_custom_button(
					__("Check DigiTax Record"),
					() => runSupplierAction(frm, "refresh_supplier", {}, __("Reading from DigiTax...")),
					group
				);
			}
		},
	});

	// Use the settings the supplier is registered under, the only active one, or ask.
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

	function registerSupplier(frm) {
		if (frm.is_dirty()) {
			frappe.msgprint(__("Save the supplier before registering it."));
			return;
		}
		pickSettings(frm, (settings_name) =>
			runSupplierAction(frm, "register_supplier", { settings_name }, __("Registering with DigiTax..."))
		);
	}

	function linkExistingSupplier(frm) {
		pickSettings(frm, (settings_name) => {
			frappe.prompt(
				{
					label: __("DigiTax Supplier ID"),
					fieldname: "supplier_id",
					fieldtype: "Data",
					reqd: 1,
					placeholder: "supplier_01J56FWS9KV3KAS0RHD4QJ1QCT",
					description: __(
						"The supplier's ID on DigiTax, starting with supplier_. Its TPIN must match this supplier's Tax ID."
					),
				},
				(values) => {
					const supplier_id = (values.supplier_id || "").trim();
					if (!supplier_id.startsWith("supplier_")) {
						frappe.msgprint(__("Enter the DigiTax supplier ID. It starts with supplier_."));
						return;
					}
					runSupplierAction(
						frm,
						"link_existing_supplier",
						{ settings_name, supplier_id },
						__("Looking up the supplier on DigiTax...")
					);
				},
				__("Link Existing DigiTax Supplier"),
				__("Link")
			);
		});
	}

	function runSupplierAction(frm, action, args, freezeMessage) {
		frappe.call({
			method: `${SUPPLIER_API}.${action}`,
			args: { supplier: frm.doc.name, ...args },
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
							__("DigiTax has no supplier update endpoint.") +
							"<ul>" +
							result.differences.map((d) => `<li>${frappe.utils.escape_html(d)}</li>`).join("") +
							"</ul>",
						indicator: "orange",
					});
				} else {
					frappe.show_alert({ message: __("DigiTax supplier ID: {0}", [result.id || "-"]), indicator: "green" }, 7);
				}
				frm.reload_doc();
			},
		});
	}
})();
