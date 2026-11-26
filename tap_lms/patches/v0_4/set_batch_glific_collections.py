import frappe


MAIN_COLLECTION_BY_BATCH_ID = {
    "BT00000027": "TLM26_AllStudents_Batch MCD",
    "BT00000026": "TLM26_AllStudents_Batch Moga_P",
    "BT00000025": "TLM26_AllStudents_Batch2",
    "BT00000024": "TLM26_AllStudents_Batch1",
    "BT00000029": "TLM26_AllStudents_Batch UP",
    "BT00000030": "TLM26_AllStudents_Batch3",
    "BT00000031": "TLM26_AllStudents_BatchTelangana",
}
COLLECTION_WITHOUT_COURSE_BY_BATCH_ID = {
    "BT00000029": "CourseSelection_Batch_UP",
    "BT00000030": "TLM26_AllStudents_Batch3_CourseSelection",
}


def execute():
    for batch_id, main_collection in MAIN_COLLECTION_BY_BATCH_ID.items():
        frappe.db.set_value("Batch", {"batch_id": batch_id}, {
            "main_collection": main_collection,
            "collection_without_course": COLLECTION_WITHOUT_COURSE_BY_BATCH_ID.get(
                batch_id, "TLM26_AllStudents_APS"
            ),
        })
