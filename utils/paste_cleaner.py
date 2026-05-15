def clean_newlines(text: str) -> str:
    """Remove all newline characters from pasted text."""
    return text.replace("\r\n", "").replace("\n", "").replace("\r", "")
