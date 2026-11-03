frappe.listview_settings["ZRA SIS Purchases"] = {
	add_fields: ["purchase_status_code", "purchase_invoice"],

	get_indicator(doc) {
		const status = doc.purchase_status || __("Unknown");
		if (doc.purchase_status_code === "01") return [status, "orange", "purchase_status_code,=,01"];
		if (doc.purchase_status_code === "02") {
			return doc.purchase_invoice ? [__("Invoiced"), "green"] : [status, "blue", "purchase_status_code,=,02"];
		}
		return [status, "gray", `purchase_status_code,=,${doc.purchase_status_code}`];
	},

	onload(listview) {
		listview.page.add_inner_button(__("Fetch from ZRA"), () =>
			frappe.require("/assets/zambia_compliance_via_digitax/js/zra_sis_fetch.js", () =>
				zra_sis_fetch(
					listview,
					"zambia_compliance_via_digitax.zambia_compliance_via_digitax.apis.purchase.fetch_purchases",
					__("purchases")
				)
			)
		);
	},
};
