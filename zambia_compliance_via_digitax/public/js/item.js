const SIS_GROUP = __("ZRA SIS");

frappe.ui.form.on("Item", {
	setup(frm) {
		frm.set_query("custom_smart_item_classification_code", () => ({
			filters: getClassificationFilters(frm),
		}));
	},

	onload(frm) {
		frappe.call({
			method: "zambia_compliance_via_digitax.zambia_compliance_via_digitax.utils.smart_api_utils.get_active_smart_settings",
			callback: (r) => {
				frm.zra_settings = r.message || [];
				frm.zra_environments = frm.zra_settings.map((s) => s.environment).filter(Boolean);
				frm.trigger("refresh_sis_actions");
			},
		});
	},

	refresh(frm) {
		frm.trigger("refresh_sis_actions");
	},

	refresh_sis_actions(frm) {
		if (frm.is_new() || !frm.zra_settings?.length) return;

		const remoteId = frm.doc.custom_smart_remote_id;
		const registered = remoteId && frm.doc.custom_smart_status !== "FAILED";

		if (!registered) {
			frm.add_custom_button(__("Register with ZRA"), () => registerItem(frm), SIS_GROUP);
			frm.add_custom_button(__("Link Existing DigiTax Item"), () => linkExistingItem(frm), SIS_GROUP);
		}

		if (remoteId) {
			frm.add_custom_button(
				__("Refresh Status"),
				() => runItemAction(frm, "refresh_item", {}, __("Refreshing from DigiTax...")),
				SIS_GROUP
			);
		}

		if (registered) {
			frm.add_custom_button(
				__("Update Name and Price"),
				() => runItemAction(frm, "update_item", {}, __("Updating in DigiTax...")),
				SIS_GROUP
			);
		}
	},
});

// Only offer classification codes DigiTax accepts in the active environment(s).
// Sandbox codes are a subset of production codes, so production wins when both are active.
function getClassificationFilters(frm) {
	const environments = frm.zra_environments || [];
	if (environments.includes("Production") || !environments.length) {
		return { available_in_production: 1 };
	}
	return { available_in_sandbox: 1 };
}

// Use the settings the item is registered under, the only active one, or ask. (No Support for multiple active settings yet.)
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

function registerItem(frm) {
	if (frm.is_dirty()) {
		frappe.msgprint(__("Save the item before registering it."));
		return;
	}
	pickSettings(frm, (settings_name) =>
		runItemAction(frm, "register_item", { settings_name }, __("Registering with DigiTax..."))
	);
}

function linkExistingItem(frm) {
	pickSettings(frm, (settings_name) => {
		frappe.prompt(
			{
				label: __("DigiTax Item ID"),
				fieldname: "item_id",
				fieldtype: "Data",
				reqd: 1,
				placeholder: "item_01M3V6XP4QDQ351BFVHTMMYRD7",
				description: __(
					"The item's ID on DigiTax, starting with item_. The item's SIS codes will be replaced with DigiTax's."
				),
			},
			(values) => {
				const item_id = (values.item_id || "").trim();
				if (!item_id.startsWith("item_")) {
					frappe.msgprint(__("Enter the DigiTax item ID. It starts with item_."));
					return;
				}
				runItemAction(
					frm,
					"link_existing_item",
					{ settings_name, item_id },
					__("Looking up the item on DigiTax...")
				);
			},
			__("Link Existing DigiTax Item"),
			__("Link")
		);
	});
}

function runItemAction(frm, action, args, freezeMessage) {
	frappe.call({
		method: `zambia_compliance_via_digitax.zambia_compliance_via_digitax.apis.item.${action}`,
		args: { item: frm.doc.name, ...args },
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
			} else {
				let message = __("ZRA status: {0}", [result.status || "-"]);
				if (result.item_code) message += ` · ${__("ZRA item code")}: ${result.item_code}`;
				if (result.stock_quantity !== undefined && result.stock_quantity !== null) {
					message += ` · ${__("DigiTax stock")}: ${result.stock_quantity}`;
				}
				frappe.show_alert({ message, indicator: "green" }, 7);
			}
			frm.reload_doc();
		},
	});
}
