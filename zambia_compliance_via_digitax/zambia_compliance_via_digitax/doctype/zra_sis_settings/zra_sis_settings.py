# Copyright (c) 2026, Navari Ltd and contributors
# For license information, please see license.txt

import frappe
import requests
from frappe import _
from frappe.model.document import Document
from frappe.utils import now_datetime

DEFAULT_SERVER_URL = "https://api.digitax.tech/zm"
REQUEST_TIMEOUT = (5, 30)


class ZRASISSettings(Document):
	def validate(self):
		self.api_server_url = normalize_server_url(self.api_server_url)
		self.api_key = (self.api_key or "").strip()
		self.callback_base_url = (self.callback_base_url or "").strip().rstrip("/")

		if self.api_key and self._connection_details_changed():
			self.update_business_info(raise_on_error=bool(self.is_active))

	def _connection_details_changed(self) -> bool:
		if self.is_new() or not self.tpin:
			return True
		return self.has_value_changed("api_key") or self.has_value_changed("api_server_url")

	def update_business_info(self, raise_on_error: bool = True) -> None:
		"""Fill TPIN, environment and business details from DigiTax GET /info."""
		try:
			info = fetch_business_info(self.api_server_url, self.api_key)
		except Exception as e:
			message = _("Could not verify the API key with DigiTax: {0}").format(str(e))
			if raise_on_error:
				frappe.throw(message, title=_("DigiTax Verification Failed"))
			frappe.msgprint(message, indicator="orange", title=_("DigiTax Verification Failed"))
			return

		self.tpin = info.get("tax_pin")
		self.business_id = info.get("id")
		self.environment = "Sandbox" if info.get("is_test") else "Production"
		self.is_live = 1 if info.get("is_live") else 0
		self.last_verified_on = now_datetime()

		self._warn_if_tpin_differs_from_company()

	def _warn_if_tpin_differs_from_company(self) -> None:
		if not self.company_name or not self.tpin:
			return

		company_tax_id = frappe.db.get_value("Company", self.company_name, "tax_id")
		if company_tax_id and company_tax_id.strip() != self.tpin:
			frappe.msgprint(
				_("The TPIN from DigiTax ({0}) does not match the Tax ID on Company {1} ({2}).").format(
					self.tpin, self.company_name, company_tax_id
				),
				indicator="orange",
				title=_("TPIN Mismatch"),
			)


@frappe.whitelist()
def refresh_business_info(name: str) -> None:
	"""Re-fetch business details from DigiTax and save them on the settings."""
	doc = frappe.get_doc("ZRA SIS Settings", name)
	doc.check_permission("write")

	if not doc.api_key:
		frappe.throw(_("Set the API Key first."))

	doc.update_business_info(raise_on_error=True)
	doc.save()


def normalize_server_url(url: str | None) -> str:
	"""Strip trailing slashes and a trailing /v1, since route paths already start with /v1."""
	url = (url or "").strip().rstrip("/") or DEFAULT_SERVER_URL
	if url.endswith("/v1"):
		url = url[: -len("/v1")]
	return url


def fetch_business_info(server_url: str, api_key: str) -> dict:
    response = requests.get(
        f"{normalize_server_url(server_url)}/v1/info",
        headers={"X-API-Key": api_key.strip(), "Accept": "application/json"},
        timeout=REQUEST_TIMEOUT,
    )

    try:
        data = response.json()
    except ValueError:
        data = {}

    if response.status_code != 200:
        message = data.get("message") if isinstance(data, dict) else None
        raise Exception(f"HTTP {response.status_code}: {message or response.text[:200]}")

    if not isinstance(data, dict) or not data.get("tax_pin"):
        raise Exception(f"Unexpected response from DigiTax: {response.text[:200]}")

    return data
