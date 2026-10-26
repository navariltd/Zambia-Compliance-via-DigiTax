"""Stock Ledger Entry doc events. Logic lives in services/stock.py."""

from ..services import stock as stock_service


def on_submit(doc, method=None):
	stock_service.queue_stock_ledger_entry(doc)
