// Copyright (c) 2026, Navari Ltd and contributors
// For license information, please see license.txt

frappe.ui.form.on("ZRA SIS Settings", {
	onload_post_render(frm) {
		mask_api_key(frm);
	},

	refresh(frm) {
		mask_api_key(frm);

		if (frm.is_new() || !frm.doc.api_key) return;

		frm.add_custom_button(__("Refresh from DigiTax"), () => {
			frappe.call({
				method: "zambia_compliance_via_digitax.zambia_compliance_via_digitax.doctype.zra_sis_settings.zra_sis_settings.refresh_business_info",
				args: { name: frm.doc.name },
				freeze: true,
				freeze_message: __("Contacting DigiTax..."),
				callback: () => {
					frm.reload_doc();
					frappe.show_alert({ message: __("Business details updated"), indicator: "green" });
				},
			});
		});

		if (frm.doc.is_active) {
			frm.add_custom_button(
				__("Fetch ZRA Notices"),
				() => {
					frappe.call({
						method: "zambia_compliance_via_digitax.zambia_compliance_via_digitax.apis.notice.fetch_notices",
						args: { settings_name: frm.doc.name },
						freeze: true,
						freeze_message: __("Fetching notices from DigiTax..."),
						callback: (r) => {
							const result = r.message || {};
							if (result.ok === false) {
								frappe.msgprint({
									title: __("DigiTax Rejected the Request"),
									message: frappe.utils.escape_html(result.message || __("Unknown error")),
									indicator: "red",
								});
								return;
							}
							frappe.show_alert({
								message: __("{0} notices, {1} new", [result.total, result.new]),
								indicator: "green",
							});
							frappe.set_route("List", "ZRA SIS Notice");
						},
					});
				},
				__("ZRA SIS")
			);
		}

		if (frm.doc.is_active && frm.doc.stock_auto_submission_enabled) {
			frm.add_custom_button(
				__("Sync Stock Balances"),
				() => {
					frappe.confirm(
						__(
							"For every registered stock item, DigiTax's balance will be compared with ERPNext's and the difference sent as a stock adjustment. Use this once at go-live, or to fix failed movements. Continue?"
						),
						() =>
							frappe.call({
								method: "zambia_compliance_via_digitax.zambia_compliance_via_digitax.apis.stock.sync_all_stock",
								args: { settings_name: frm.doc.name },
								callback: () =>
									frappe.show_alert({
										message: __("Stock sync queued. Failures appear in the Error Log and on each item."),
										indicator: "blue",
									}),
							})
					);
				},
				__("ZRA SIS")
			);
		}

		if (frm.doc.environment) {
			frm.dashboard.set_headline_alert(
				frm.doc.environment === "Production"
					? __("This API key sends invoices to <b>live ZRA</b>.")
					: __("This API key is for a <b>test business</b>. Data goes to the ZRA sandbox."),
				frm.doc.environment === "Production" ? "red" : "blue"
			);
		}
	},
});

// Show the API key as dots, with a toggle to reveal it. The value is still stored as plain text.
function mask_api_key(frm) {
	const field = frm.fields_dict.api_key;
	if (!field || !field.$input || field.$input.data("masked")) return;

	field.$input.attr({ type: "password", autocomplete: "new-password" }).data("masked", true);

	const $toggle = $(
		`<button type="button" class="btn btn-xs btn-default" style="margin-top: 6px;">${__("Show")}</button>`
	);
	$toggle.on("click", () => {
		const hidden = field.$input.attr("type") === "password";
		field.$input.attr("type", hidden ? "text" : "password");
		$toggle.text(hidden ? __("Hide") : __("Show"));
	});
	field.$input.after($toggle);
}
