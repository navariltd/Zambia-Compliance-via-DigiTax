"""Customer doc events. Thin wrappers around services/customers.py."""

from ..services import customers as customer_service


def validate(doc, method=None):
	customer_service.validate_customer(doc)


def on_update(doc, method=None):
	customer_service.on_customer_update(doc)


def on_trash(doc, method=None):
	customer_service.prevent_deleting_registered_customer(doc)
