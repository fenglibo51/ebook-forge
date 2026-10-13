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
    "cbz": "CBZ (漫画 · zip)",
    "cbr": "CBR (漫画 · RAR)",
    "cb7": "CB7 (漫画 · 7z)",
    "cbc": "CBC (漫画集合)",
}

# Calibre can convert FROM -> TO
# 注意：cbz 作为目标对 epub 和各类漫画包（ZIP/CBZ/CBR/CB7/CBC）开放 —— 小说类格式不涉及 CBZ
CONVERT_MATRIX = {
    "epub": ["mobi", "azw3", "pdf", "txt", "docx", "fb2", "cbz"],
    "mobi": ["epub", "azw3", "pdf", "txt", "docx", "fb2"],
    "azw3": ["epub", "mobi", "pdf", "txt", "docx", "fb2"],
    "pdf":  ["epub", "mobi", "azw3", "txt", "docx", "fb2", "cbz"],
    "txt":  ["epub", "mobi", "azw3", "pdf", "docx", "fb2"],
    "docx": ["epub", "mobi", "azw3", "pdf", "txt", "fb2"],
    "fb2":  ["epub", "mobi", "azw3", "pdf", "txt", "docx"],
    "zip":  ["epub", "pdf", "cbz"],
    "cbz":  ["epub", "pdf"],
    "cbr":  ["epub", "pdf", "cbz"],
    "cb7":  ["epub", "pdf", "cbz"],
    "cbc":  ["epub", "pdf", "cbz"],
}

DEFAULT_NAMING_RULE = "{title}.{author}.{ext}"
