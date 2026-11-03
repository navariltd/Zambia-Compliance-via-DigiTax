(() => {
	frappe.ui.form.on("Purchase Invoice", {
		refresh(frm) {
			if (frm.doc.custom_sis_last_error && !frm.doc.custom_sis_purchase) {
				frm.dashboard.set_headline_alert(
					`${__("Not sent to ZRA")}: ${frappe.utils.escape_html(frm.doc.custom_sis_last_error)}`,
					"red"
				);
			}
			if (
				frm.doc.docstatus !== 1 ||
				frm.doc.is_return ||
				frm.doc.custom_sis_purchase ||
				frm.doc.custom_sis_is_import
			)
				return;

			frappe.db
				.get_value(
					"ZRA SIS Settings",
					{ company_name: frm.doc.company, is_active: 1, send_purchases_to_zra: 1 },
					"name"
				)
				.then((r) => {
					if (!r.message?.name) return;
					frm.add_custom_button(__("Send Purchase to ZRA"), () => reportManualPurchase(frm), __("ZRA SIS"));
				});
		},
	});

	// Sends the invoice as a purchase (POST /purchases), which adds its stock on DigiTax. With the settings'
	// "Send Purchase Invoices to ZRA" on this happens on submit; the button is for retries and for sending
	// one invoice by hand. A ZRA purchase with the same supplier invoice number is linked instead.
	function reportManualPurchase(frm) {
		frappe.confirm(
			__(
				"Send this invoice to ZRA as a purchase? DigiTax adds its stock, and the purchase cannot be cancelled afterwards. If the supplier issues ZRA Smart Invoices, approve their purchase under ZRA SIS Purchases instead."
			),
			() =>
				frappe.call({
					method: "zambia_compliance_via_digitax.zambia_compliance_via_digitax.apis.purchase.report_manual_purchase",
					args: { purchase_invoice: frm.doc.name },
					freeze: true,
					freeze_message: __("Sending the purchase to DigiTax..."),
					callback: (r) => {
						const result = r.message || {};
						if (result.ok === false) {
							frappe.msgprint({
								title: __("DigiTax Rejected the Request"),
								message: frappe.utils.escape_html(result.message || __("Unknown error")),
								indicator: "red",
							});
						} else {
							frappe.show_alert({
								message: result.linked
									? __("Linked to ZRA purchase {0}", [result.purchase])
									: __("Sent to ZRA as {0}", [result.purchase]),
								indicator: "green",
							});
						}
						frm.reload_doc();
					},
				})
		);
	}
})();
