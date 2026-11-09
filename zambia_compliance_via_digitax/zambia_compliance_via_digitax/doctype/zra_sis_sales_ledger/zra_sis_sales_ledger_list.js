frappe.listview_settings["ZRA SIS Sales Ledger"] = {
	add_fields: ["status"],
	get_indicator(doc) {
		const colors = {
			"Not Ready": "orange",
			Queued: "blue",
			Sending: "blue",
			Pending: "yellow",
			Completed: "green",
			Rejected: "red",
			Unknown: "orange",
			Failed: "red",
			Superseded: "gray",
			Cancelled: "gray",
		};
		return [__(doc.status), colors[doc.status] || "gray", `status,=,${doc.status}`];
	},
};
