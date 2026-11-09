"""Sales Invoice doc events. Logic lives in services/sales.py."""

from ..services import sales as sales_service


def on_submit(doc, method=None):
	sales_service.on_submit_sales_invoice(doc)


def before_cancel(doc, method=None):
	sales_service.before_cancel_sales_invoice(doc)
