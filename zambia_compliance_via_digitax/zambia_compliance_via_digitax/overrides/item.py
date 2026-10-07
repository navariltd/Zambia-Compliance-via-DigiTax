"""Item doc events. Thin wrappers around services/items.py."""

from ..services import items as item_service


def validate(doc, method=None):
	item_service.validate_item(doc)


def on_update(doc, method=None):
	item_service.on_item_update(doc)


def on_trash(doc, method=None):
	item_service.prevent_deleting_registered_item(doc)
