from datetime import datetime

import frappe
from frappe import _

from .settings_utils import get_settings




def build_headers(settings_name: str | None = None) -> dict[str, str] | None:

	settings = get_settings(settings_name)

	if not settings:
		return None

	api_key = settings.get("api_key")




	# Build base headers
	headers = {
		"X-API-Key": api_key,
		"Content-Type": "application/json",
		"Accept": "application/json",
	}

	

	return headers
