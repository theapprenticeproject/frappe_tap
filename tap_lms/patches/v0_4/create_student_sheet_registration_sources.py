import frappe


SOURCES = [
    ("English", "1bePUEi1KaXJfRB4O4KggkaIxDH9l7OpMZPBD9pBTgcg", 0),
    ("Hinglish", "1GY3-zhGzAFHbjHy65FyrLh1EyAdoNNifsJPB4RwE9YE", 0),
    ("Hindi", "1sJ-FsyNcI5yeyXeAVhFpNGJepNPWJ2gBTC6BYOkilr8", 0),
    ("Punjabi", "13bx8lXNT_0yi4cKbywOiVxiyUufoUujZmpbF4adSUCs", 0),
    ("Marathi", "1QH7Hi7tqe8c-ABAD5spIfA4dXJjxuOhzukua5nQP98c", 0),
    ("Telugu", "167P-9OXaayU1gaJtjS2IkNm6D3OIoN5LIQAm_CASG1Y", 0),
    ("Hindi", "1rtaPJ23KcKfSbt6VY_Lu0HwgAOWwnzOHv5mup7wE3TU", 1),
]


def execute():
    for language, spreadsheet_id, has_course_column in SOURCES:
        frappe.get_doc({
            "doctype": "Student Sheet Registration Source",
            "spreadsheet_url": f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}/edit?gid=0#gid=0",
            "language": language,
            "active": 1,
            "has_course_column": has_course_column,
        }).insert()
