frappe.listview_settings["ZRA SIS Import"] = {
	add_fields: ["import_item_status_code"],

	get_indicator(doc) {
		const status = doc.import_item_status || __("Unknown");
		const color = { 1: "gray", 2: "orange", 3: "green", 4: "red" }[doc.import_item_status_code] || "gray";
		return [status, color, `import_item_status_code,=,${doc.import_item_status_code}`];
	},

	onload(listview) {
		listview.page.add_inner_button(__("Fetch from ZRA"), () =>
			frappe.require("/assets/zambia_compliance_via_digitax/js/zra_sis_fetch.js", () =>
				zra_sis_fetch(
					listview,
					"zambia_compliance_via_digitax.zambia_compliance_via_digitax.apis.imports.fetch_imports",
					__("imports")
				)
			)
		);
	},
};
