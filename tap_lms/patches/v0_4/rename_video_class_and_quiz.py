import re

import frappe
from frappe.model.naming import getseries


def execute():
    """Rename VideoClass and Quiz records to '{name field}-{###}'."""
    for doctype, name_field in (("VideoClass", "video_name"), ("Quiz", "quiz_name")):
        for name, base in frappe.get_all(doctype, fields=["name", name_field], order_by="creation", as_list=True):
            if re.fullmatch(re.escape(f"{base}-") + r"\d{3}", name):
                continue
            prefix = f"{base}-"
            frappe.rename_doc(doctype, name, prefix + getseries(prefix, 3), force=True, rebuild_search=False)
