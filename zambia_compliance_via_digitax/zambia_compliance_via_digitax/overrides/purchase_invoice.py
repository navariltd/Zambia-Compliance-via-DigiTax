"""Purchase Invoice doc events. Logic lives in services/purchases.py."""

from ..services import purchases as purchase_service


def validate(doc, method=None):
	purchase_service.validate_purchase_invoice(doc)


def before_submit(doc, method=None):
	purchase_service.before_submit_purchase_invoice(doc)


def on_submit(doc, method=None):
	purchase_service.on_submit_purchase_invoice(doc)


def before_cancel(doc, method=None):
	purchase_service.before_cancel_purchase_invoice(doc)


def on_cancel(doc, method=None):
	purchase_service.unlink_purchase_invoice(doc)


def on_trash(doc, method=None):
	purchase_service.unlink_purchase_invoice(doc)
