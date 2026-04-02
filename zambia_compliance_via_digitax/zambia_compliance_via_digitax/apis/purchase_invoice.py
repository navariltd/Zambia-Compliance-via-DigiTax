import frappe
from ..apis.api_processor import process_request
from frappe.utils import flt
REGISTERED_PURCHASES_DOCTYPE_NAME = "ZRA SIS Purchases"



@frappe.whitelist()
def perform_purchases_search(company: str, before: str = None, after: str = None, page_size: int = 20) -> None:
    """
    Fetch purchases from ZRA Smart Invoice System for a given company using pagination query params.
    Automatically fetches all pages.
    """
   
    request_data = {
        
        
        "after": after or "",
        "pagesize": min(max(page_size, 1), 20)
    }

    try:
        # Initial request
        process_request(
            request_data=request_data,
            route_key="fetchPurchaseSales",
            handler_function=purchase_search_on_success,
            request_method="GET",
            doctype=REGISTERED_PURCHASES_DOCTYPE_NAME,
            
        )
        frappe.msgprint("Smart purchase fetch request sent successfully.")
    except Exception as e:
        frappe.log_error(frappe.get_traceback(), "Smart Purchase Fetch Failed")
        frappe.throw(f"Error fetching purchases from ZRA: {e}")


def purchase_search_on_success(response: dict, branch=None, **kwargs) -> None:
    """
    Handles a single page response from ZRA.
    Saves purchases and schedules next page if pagination.next exists.
    """
    purchases = response.get("data", [])
    pagination = response.get("pagination", {})
    meta = kwargs.get("meta", {})

    if not purchases:
        frappe.log_error(message=f"No purchases found in response: {response}",
                         title="Smart Purchase Fetch Empty")
        return

    successful_saves = 0
    failed_saves = 0

    for i, purchase in enumerate(purchases):
        try:
            purchase_doc = create_purchase_from_response(purchase, branch, save_and_submit=False)
            if purchase_doc is None:
                continue

            for item in purchase.get("item_list", []):
                create_and_link_purchase_item_from_response(item, purchase_doc)

            save_and_submit_purchase(purchase_doc)
            successful_saves += 1
        except Exception as e:
            frappe.log_error(message=f"Failed to process purchase {i+1}: {str(e)}\nPurchase data: {purchase}",
                             title=f"Smart Purchase Processing Error - {i+1}")
            failed_saves += 1

    frappe.msgprint(f"Page processed: {successful_saves} saved, {failed_saves} failed")

    # ---------------- Handle Pagination ----------------
    next_id = pagination.get("next")
    if next_id:
        company = meta.get("company")
        page_size = meta.get("page_size", 20)
        next_request_data = {
            "Company": company,
            "After": next_id,
            "PageSize": page_size
        }
        # Enqueue next page fetch
        frappe.enqueue(
            request_data=next_request_data,
            route_key="fetchPurchaseSales",
            handler_function=purchase_search_on_success,
            request_method="GET",
            doctype=REGISTERED_PURCHASES_DOCTYPE_NAME,
           
        )
def create_purchase_from_response(purchase: dict, branch=None, save_and_submit=True):
    """
    Create or update Registered Purchase from new API response.
    """

    purchase_id = purchase.get("id")

    if not purchase_id:
        return None

    # جلوگیری duplicates
    existing = frappe.db.exists("Registered Purchases", {"purchase_id": purchase_id})
    if existing:
        return None

    doc = frappe.new_doc("Registered Purchases")

    # Safe flags
    doc.flags.ignore_permissions = True
    doc.flags.ignore_validate = True
    doc.flags.ignore_mandatory = True

    # Core fields
    doc.purchase_id = purchase_id
    doc.invoice_number = purchase.get("invoice_number")
    doc.receipt_type_code = purchase.get("receipt_type_code")
    doc.payment_type_code = purchase.get("payment_type_code")
    doc.purchase_date = purchase.get("purchase_date")
    doc.purchase_number = purchase.get("purchase_number")
    doc.supplier_id = purchase.get("supplier_id")
    doc.supplier_invoice_number = purchase.get("supplier_invoice_number")
    doc.status = purchase.get("status")
    doc.purchase_status_code = purchase.get("purchase_status_code")
    doc.branch = branch

    doc.items = []

    if save_and_submit:
        doc.save(ignore_permissions=True)
        return doc.name

    return doc



def create_and_link_purchase_item_from_response(item: dict, parent_document):
    """
    Append item row using new API structure.
    """

    if isinstance(parent_document, str):
        parent = frappe.get_doc("Registered Purchases", parent_document)
    else:
        parent = parent_document

    parent.flags.ignore_permissions = True
    parent.flags.ignore_validate = True

    parent.append("items", {
        "item_id": item.get("item_id"),
        "item_name": item.get("item_name"),
        "item_code": item.get("item_code"),
        "item_class_code": item.get("item_class_code"),
        "packing_unit_code": item.get("packing_unit_code"),

        "quantity": flt(item.get("quantity")),
        "approved_quantity": flt(item.get("approved_quantity")),

        "unit_price": flt(item.get("unit_price")),
        "supply_price": flt(item.get("supply_price")),

        "total_amount": flt(item.get("total_amount")),

        "vat_tax_amount": flt(item.get("vat_tax_amount")),
        "vat_taxable_amount": flt(item.get("vat_taxable_amount")),

        "ipl_tax_amount": flt(item.get("ipl_tax_amount")),
        "ipl_taxable_amount": flt(item.get("ipl_taxable_amount")),

        "tl_tax_amount": flt(item.get("tl_tax_amount")),
        "tl_taxable_amount": flt(item.get("tl_taxable_amount")),

        "excise_tax_amount": flt(item.get("excise_tax_amount")),
        "excise_taxable_amount": flt(item.get("excise_taxable_amount")),

        "discount_rate": flt(item.get("discount_rate")),
        "discount_amount": flt(item.get("discount_amount")),
    })