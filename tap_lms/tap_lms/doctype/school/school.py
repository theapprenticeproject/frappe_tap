import frappe
from frappe.model.document import Document
from tap_lms.utils.school_utils import generate_unique_keyword

class School(Document):
    pass

def before_save(doc, method):
    # Generate a unique keyword only for new documents
    if doc.is_new():
        if not doc.keyword:
            unique_keyword = generate_unique_keyword(doc.name1)
            while frappe.db.exists("School", {"keyword": unique_keyword}):
                unique_keyword = generate_unique_keyword(doc.name1)
            doc.keyword = unique_keyword
