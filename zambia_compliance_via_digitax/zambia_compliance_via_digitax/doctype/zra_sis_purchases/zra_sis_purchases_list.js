frappe.listview_settings["ZRA SIS Purchases"] = {
	onload(listview) {
		listview.page.add_inner_button(__("Get Raised Purchases"), function () {

			// Fetch companies
			frappe.call({
				method: "frappe.client.get_list",
				args: {
					doctype: "Company",
					fields: ["name"],
					limit_page_length: 999,
				},
				callback(companyRes) {
					let companies = companyRes.message || [];

					let dialog = new frappe.ui.Dialog({
						title: "Fetch Purchases from ZRA",
						fields: [
							{
								fieldname: "company",
								label: "Company",
								fieldtype: "Select",
								reqd: 1,
								options: companies.map(c => c.name).join("\n"),
							},
							{
								fieldname: "before",
								label: "Before",
								fieldtype: "Data",
								description: "Pointer to an ID before which to fetch results (optional)",
								default: "" // <-- send empty string by default
							},
							{
								fieldname: "after",
								label: "After",
								fieldtype: "Data",
								description: "Pointer to an ID after which to fetch results (optional)",
								default: "" // <-- send empty string by default
							},
							{
								fieldname: "page_size",
								label: "Page Size",
								fieldtype: "Int",
								default: 20,
								description: "Number of items per page (1-20, default 20)",
							},
						],
						primary_action_label: "Fetch Purchases",
						primary_action(values) {
							dialog.hide();

							frappe.call({
								method: "zambia_compliance_via_digitax.zambia_compliance_via_digitax.apis.purchase_invoice.perform_purchases_search",
								args: {
									company: values.company,
									before: values.before || "", // <-- always send a string
									after: values.after || "",   // <-- always send a string
									page_size: values.page_size || 20
								},
								freeze: true,
								freeze_message: __("Fetching Purchases..."),
								callback() {
									frappe.show_alert({
										message: __("Purchases Fetch Initiated"),
										indicator: "green",
									});
								}
							});
						},
					});

					dialog.show();
				}
			});
		});
	},
};