import os

BOOK_PATH = os.environ.get("BOOK_PATH", "/Book")
OUTPUT_PATH = os.environ.get("OUTPUT_PATH", BOOK_PATH)
CLEANUP_HOURS = int(os.environ.get("CLEANUP_HOURS", "24"))
TEMP_DIR = os.environ.get("TEMP_DIR", "/temp")
HISTORY_DB = "/data/convert_history.json"

SUPPORTED_FORMATS = {
    "epub": "EPUB",
    "mobi": "MOBI (Kindle)",
    "azw3": "AZW3/KF8 (Kindle)",
    "pdf": "PDF",
    "txt": "纯文本",
    "docx": "DOCX (Word)",
    "fb2": "FictionBook",
}

# Calibre can convert FROM -> TO
CONVERT_MATRIX = {
    "epub": ["mobi", "azw3", "pdf", "txt", "docx", "fb2"],
    "mobi": ["epub", "azw3", "pdf", "txt", "docx", "fb2"],
    "azw3": ["epub", "mobi", "pdf", "txt", "docx", "fb2"],
    "pdf":  ["epub", "mobi", "azw3", "txt", "docx", "fb2"],
    "txt":  ["epub", "mobi", "azw3", "pdf", "docx", "fb2"],
    "docx": ["epub", "mobi", "azw3", "pdf", "txt", "fb2"],
    "fb2":  ["epub", "mobi", "azw3", "pdf", "txt", "docx"],
}

DEFAULT_NAMING_RULE = "{title}.{author}.{ext}"
