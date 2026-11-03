// Pull records from DigiTax for the list views of ZRA SIS Purchases and ZRA SIS Import.
window.zra_sis_fetch = function (listview, method, noun) {
	frappe.call({
		method: "zambia_compliance_via_digitax.zambia_compliance_via_digitax.utils.smart_api_utils.get_active_smart_settings",
		callback: (r) => {
			const settings = r.message || [];
			if (!settings.length) return frappe.msgprint(__("No active ZRA SIS Settings."));

			const run = (settings_name) =>
				frappe.call({
					method,
					args: { settings_name },
					freeze: true,
					freeze_message: __("Fetching {0} from DigiTax...", [noun]),
					callback: (res) => {
						const result = res.message || {};
						if (result.ok === false) {
							frappe.msgprint({
								title: __("DigiTax Rejected the Request"),
								message: frappe.utils.escape_html(result.message || __("Unknown error")),
								indicator: "red",
							});
						} else {
							frappe.show_alert({
								message: __("{0} {1}, {2} new", [result.total, noun, result.new]),
								indicator: "green",
							});
						}
						listview.refresh();
					},
				});

			if (settings.length === 1) return run(settings[0].name);
			frappe.prompt(
				{
					label: __("ZRA SIS Settings"),
					fieldname: "settings_name",
					fieldtype: "Select",
					reqd: 1,
					options: settings.map((s) => ({ label: s.company_name || s.name, value: s.name })),
				},
				(values) => run(values.settings_name),
				__("Choose DigiTax Business")
			);
		},
	});
};
