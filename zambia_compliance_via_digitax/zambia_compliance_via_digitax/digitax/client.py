"""Small DigiTax Zambia HTTP client.

One instance per ZRA SIS Settings document. Every call:
- resolves its path from the "ZRA SIS Routes" table by route key,
- logs an Integration Request with the API key redacted,
- raises DigitaxError with a readable message on any failure.
"""

from __future__ import annotations

import json
import re
from typing import Any

import frappe
import requests
from frappe import _
from frappe.integrations.utils import create_request_log

from ..apis.api_builder import redact_headers
from ..doctype.doctype_names_mapping import SETTINGS_DOCTYPE_NAME
from ..utils.routes_utils import get_route_path

REQUEST_TIMEOUT = (5, 30)  # connect, read (seconds)


class DigitaxError(Exception):
	"""A DigiTax request failed. `message` is safe to show to users."""

	def __init__(self, message: str, status_code: int | None = None, response: Any = None):
		super().__init__(message)
		self.message = message
		self.status_code = status_code
		self.response = response


class DigitaxClient:
	def __init__(self, settings_name: str):
		if not settings_name or not frappe.db.exists(SETTINGS_DOCTYPE_NAME, settings_name):
			frappe.throw(_("ZRA SIS Settings {0} not found.").format(settings_name or ""))

		self.settings = frappe.get_cached_doc(SETTINGS_DOCTYPE_NAME, settings_name)
		if not self.settings.is_active:
			frappe.throw(_("ZRA SIS Settings {0} is not active.").format(settings_name))
		if not self.settings.api_key:
			frappe.throw(_("Set the API Key on ZRA SIS Settings {0}.").format(settings_name))

		self.base_url = (self.settings.api_server_url or "").rstrip("/")
		self.headers = {
			"X-API-Key": self.settings.api_key.strip(),
			"Content-Type": "application/json",
			"Accept": "application/json",
		}

	def get(self, route_key: str, path_params: dict | None = None, params: dict | None = None, **ref):
		return self.request("GET", route_key, path_params=path_params, params=params, **ref)

	def post(self, route_key: str, body: dict, path_params: dict | None = None, **ref):
		return self.request("POST", route_key, path_params=path_params, body=body, **ref)

	def put(self, route_key: str, body: dict, path_params: dict | None = None, **ref):
		return self.request("PUT", route_key, path_params=path_params, body=body, **ref)

	def iterate(self, route_key: str, page_size: int = 20, max_pages: int = 100, **ref):
		"""Yield every record from a cursor-paginated list endpoint."""
		after = None
		for _page in range(max_pages):
			params = {"page_size": page_size}
			if after:
				params["after"] = after
			response = self.get(route_key, params=params, **ref) or {}
			yield from response.get("data") or []

			after = (response.get("pagination") or {}).get("next")
			if not after:
				return


	def request(
		self,
		method: str,
		route_key: str,
		path_params: dict | None = None,
		params: dict | None = None,
		body: dict | None = None,
		reference_doctype: str | None = None,
		reference_name: str | None = None,
	) -> Any:
		url = self._build_url(route_key, path_params)
		log = self._create_log(method, route_key, url, params or body, reference_doctype, reference_name)

		try:
			response = requests.request(
				method,
				url,
				params=params,
				json=body if method != "GET" else None,
				headers=self.headers,
				timeout=REQUEST_TIMEOUT,
			)
		except requests.exceptions.Timeout:
			self._finish_log(log, "Failed", error="Request timed out")
			raise DigitaxError(_("DigiTax did not respond in time. Try again later."))
		except requests.exceptions.RequestException as e:
			self._finish_log(log, "Failed", error=str(e))
			raise DigitaxError(_("Could not reach DigiTax: {0}").format(str(e)))

		data = _parse_body(response)

		if response.status_code not in (200, 201):
			message = extract_error_message(data, response)
			self._finish_log(log, "Failed", output=data, error=f"HTTP {response.status_code}: {message}")
			raise DigitaxError(message, status_code=response.status_code, response=data)

		self._finish_log(log, "Completed", output=data)
		return data

	def _build_url(self, route_key: str, path_params: dict | None) -> str:
		path, _last_request = get_route_path(route_key)
		if not path:
			frappe.throw(_("No DigiTax route configured for {0} in ZRA SIS Routes.").format(route_key))

		def substitute(match: re.Match) -> str:
			key = match.group(1)
			value = (path_params or {}).get(key)
			if value in (None, ""):
				frappe.throw(_("Missing value for {0} in DigiTax route {1}.").format(key, route_key))
			return requests.utils.quote(str(value), safe="")

		# Query strings in route templates (e.g. "?after={after}") are dropped; pass params instead.
		path = path.split("?", 1)[0]
		return self.base_url + re.sub(r"\{(\w+)\}", substitute, path)

	def _create_log(self, method, route_key, url, data, reference_doctype, reference_name):
		try:
			return create_request_log(
				data=data or {},
				service_name="DigiTax Zambia",
				request_headers=redact_headers(self.headers),
				is_remote_request=1,
				request_description=f"{method} {route_key}",
				url=url,
				reference_doctype=reference_doctype,
				reference_docname=reference_name,
				status="Queued",
			)
		except Exception:
			frappe.log_error(title="DigiTax: failed to create Integration Request")
			return None

	def _finish_log(self, log, status: str, output: Any = None, error: str | None = None) -> None:
		if not log:
			return
		values = {"status": status}
		if output is not None:
			values["output"] = frappe.as_json(output) if not isinstance(output, str) else output
		if error:
			values["error"] = error
		frappe.db.set_value("Integration Request", log.name, values, update_modified=False)

		# The log row itself is committed by create_request_log, but this update rides on the caller's
		# transaction. If the caller later throws (e.g. "No DigiTax item found"), the rollback would undo
		# it and leave the log stuck on "Queued". Re-apply it after any rollback and commit just that.
		def persist_after_rollback():
			frappe.db.set_value("Integration Request", log.name, values, update_modified=False)
			frappe.db.commit()

		frappe.db.after_rollback.add(persist_after_rollback)


def _parse_body(response: requests.Response) -> Any:
	try:
		return response.json()
	except ValueError:
		return response.text or None


def extract_error_message(data: Any, response: requests.Response | None = None) -> str:
	"""Turn DigiTax's error shapes into one readable line.

	Shapes per the docs:
	- {"message": "...", "code": "...", "metadata": {...}}
	- [{"type": "Body", "errors": {"issues": [{"path": [...], "message": "..."}], "name": "ZodError"}}]
	"""
	if isinstance(data, dict):
		message = data.get("message") or json.dumps(data)
		metadata = data.get("metadata")
		if isinstance(metadata, dict) and metadata:
			details = ", ".join(f"{k}: {v}" for k, v in metadata.items())
			message = f"{message} ({details})"
		return message

	if isinstance(data, list):
		issues = []
		for entry in data:
			for issue in ((entry or {}).get("errors") or {}).get("issues") or []:
				path = ".".join(str(p) for p in issue.get("path") or []) or "body"
				issues.append(f"{path}: {issue.get('message')}")
		if issues:
			return "; ".join(issues)

	if isinstance(data, str) and data.strip():
		return data.strip()[:300]

	status = response.status_code if response is not None else "?"
	return _("DigiTax returned HTTP {0}").format(status)
