(() => {
	const API = "zambia_compliance_via_digitax.zambia_compliance_via_digitax.apis.sales";
	const RETRYABLE = ["Not Ready", "Rejected", "Unknown", "Failed", "Queued"];
	const COLORS = {
		"Not Ready": "orange",
		Queued: "blue",
		Sending: "blue",
		Pending: "yellow",
		Completed: "green",
		Rejected: "red",
		Unknown: "orange",
		Failed: "red",
		Cancelled: "gray",
	};

	frappe.ui.form.on("Sales Invoice", {
		setup(frm) {
			frappe.realtime.on("zra_sis_sales_status", (data) => {
				if (cur_frm === frm && data?.name === frm.doc.name && !frm.is_dirty())
					frm.reload_doc();
			});
		},

		refresh(frm) {
			if (frm.doc.docstatus !== 1) return;

			frappe.db
				.get_value(
					"ZRA SIS Settings",
					{
						company_name: frm.doc.company,
						is_active: 1,
						sales_auto_submission_enabled: 1,
					},
					"name"
				)
				.then((r) => {
					if (!r.message?.name) return;
					if (frm.doc.custom_sis_ledger_entry) {
						showLedgerStatus(frm);
					} else if (
						!frm.doc.custom_prevent_sis_submission &&
						frm.doc.is_opening !== "Yes"
					) {
						frm.add_custom_button(
							__("Send to ZRA"),
							() => sendOrRetry(frm),
							__("ZRA SIS")
						);
					}
				});
		},

		customer(frm) {
			if (!frm.doc.customer) return;
			frappe.db.get_value("Customer", frm.doc.customer, "custom_is_lpo").then((r) => {
				const isLpo = r.message?.custom_is_lpo;
				if (isLpo && frm.doc.custom_kind_of_sale === "NORMAL")
					frm.set_value("custom_kind_of_sale", "LPO");
				if (!isLpo && frm.doc.custom_kind_of_sale === "LPO")
					frm.set_value("custom_kind_of_sale", "NORMAL");
			});
		},
	});

	function showLedgerStatus(frm) {
		const status = frm.doc.custom_sis_status;
		frappe.db
			.get_value("ZRA SIS Sales Ledger", frm.doc.custom_sis_ledger_entry, [
				"problems",
				"last_error",
				"sale_id",
			])
			.then((r) => {
				const entry = r.message || {};
				const detail = status === "Not Ready" ? entry.problems : entry.last_error;
				let message = `${__("ZRA")}: ${__(status)}`;
				if (detail)
					message += `<br>${frappe.utils.escape_html(detail).replace(/\n/g, "<br>")}`;
				frm.dashboard.set_headline_alert(message, COLORS[status] || "gray");

				if (RETRYABLE.includes(status)) {
					frm.add_custom_button(__("Retry"), () => sendOrRetry(frm), __("ZRA SIS"));
				}
				if (entry.sale_id && status !== "Completed") {
					frm.add_custom_button(
						__("Check Status"),
						() => checkStatus(frm),
						__("ZRA SIS")
					);
				}
				if (status === "Unknown") {
					frm.add_custom_button(
						__("Link Sale ID"),
						() => linkSaleId(frm),
						__("ZRA SIS")
					);
				}
				frm.add_custom_button(
					__("Ledger Entry"),
					() =>
						frappe.set_route(
							"Form",
							"ZRA SIS Sales Ledger",
							frm.doc.custom_sis_ledger_entry
						),
					__("ZRA SIS")
				);
			});
	}

	function sendOrRetry(frm) {
		frappe.call({
			method: `${API}.send_or_retry`,
			args: { invoice: frm.doc.name },
			freeze: true,
			callback: (r) => {
				const result = r.message || {};
				if (result.ok) {
					frappe.show_alert({
						message: __("ZRA status: {0}", [__(result.status)]),
						indicator: "green",
					});
				} else {
					frappe.msgprint({
						title: __("Not Sent to ZRA"),
						message: frappe.utils
							.escape_html(
								result.message || __("ZRA status: {0}", [__(result.status)])
							)
							.replace(/\n/g, "<br>"),
						indicator: "orange",
					});
				}
				frm.reload_doc();
			},
		});
	}

	function checkStatus(frm) {
		frappe.call({
			method: `${API}.check_status`,
			args: { invoice: frm.doc.name },
			freeze: true,
			freeze_message: __("Checking the sale on DigiTax..."),
			callback: (r) => {
				const result = r.message || {};
				if (result.ok === false) {
					frappe.msgprint({
						title: __("DigiTax Error"),
						message: frappe.utils.escape_html(result.message || __("Unknown error")),
						indicator: "red",
					});
				}
				frm.reload_doc();
			},
		});
	}

	// For an Unknown send whose sale exists on DigiTax but was not found automatically.
	function linkSaleId(frm) {
		frappe.prompt(
			{
				fieldname: "sale_id",
				fieldtype: "Data",
				label: __("DigiTax Sale ID"),
				reqd: 1,
				description: __(
					"From the DigiTax dashboard, starting with sale_. Its trader invoice number must be this invoice's."
				),
			},
			(values) =>
				frappe.call({
					method: `${API}.link_sale_id`,
					args: { invoice: frm.doc.name, sale_id: values.sale_id },
					freeze: true,
					callback: () => frm.reload_doc(),
				}),
			__("Link DigiTax Sale")
		);
	}
})();
