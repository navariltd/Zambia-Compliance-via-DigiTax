// Copyright (c) 2026, Navari Ltd and contributors
// For license information, please see license.txt

frappe.query_reports["ZRA SIS Sales Reconciliation"] = {
	filters: [
		{
			fieldname: "company",
			label: __("Company"),
			fieldtype: "Link",
			options: "Company",
			default: frappe.defaults.get_user_default("Company"),
			reqd: 1,
		},
		{
			fieldname: "from_date",
			label: __("From Date"),
			fieldtype: "Date",
			default: frappe.datetime.add_months(frappe.datetime.get_today(), -1),
			reqd: 1,
		},
		{
			fieldname: "to_date",
			label: __("To Date"),
			fieldtype: "Date",
			default: frappe.datetime.get_today(),
			reqd: 1,
		},
		{
			fieldname: "only_issues",
			label: __("Only Issues"),
			fieldtype: "Check",
			default: 1,
		},
	],
	formatter(value, row, column, data, default_formatter) {
		value = default_formatter(value, row, column, data);
		if (column.fieldname === "issue" && data?.issue) {
			const color = data.issue === __("OK") ? "green" : data.is_open ? "orange" : "red";
			value = `<span style="color: var(--${color}-600)">${value}</span>`;
		}
		return value;
	},
};
