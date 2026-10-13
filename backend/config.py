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
    "zip": "ZIP (漫画压缩包)",
    "cbz": "CBZ (漫画)",
}

# Calibre can convert FROM -> TO
CONVERT_MATRIX = {
    "epub": ["mobi", "azw3", "pdf", "txt", "docx", "fb2", "cbz"],
    "mobi": ["epub", "azw3", "pdf", "txt", "docx", "fb2", "cbz"],
    "azw3": ["epub", "mobi", "pdf", "txt", "docx", "fb2", "cbz"],
    "pdf":  ["epub", "mobi", "azw3", "txt", "docx", "fb2", "cbz"],
    "txt":  ["epub", "mobi", "azw3", "pdf", "docx", "fb2", "cbz"],
    "docx": ["epub", "mobi", "azw3", "pdf", "txt", "fb2", "cbz"],
    "fb2":  ["epub", "mobi", "azw3", "pdf", "txt", "docx", "cbz"],
    "zip":  ["epub", "pdf", "cbz"],
    "cbz":  ["epub", "pdf"],
}

# 电子书 → CBZ 光栅化默认 DPI（可被请求参数覆盖）
DEFAULT_CBZ_DPI = 150

DEFAULT_NAMING_RULE = "{title}.{author}.{ext}"
