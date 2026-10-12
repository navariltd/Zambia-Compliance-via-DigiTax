"""Supplier doc events. Thin wrappers around services/suppliers.py."""

from ..services import suppliers as supplier_service


def validate(doc, method=None):
	supplier_service.validate_supplier(doc)


def on_update(doc, method=None):
	supplier_service.on_supplier_update(doc)


def on_trash(doc, method=None):
	supplier_service.prevent_deleting_registered_supplier(doc)
