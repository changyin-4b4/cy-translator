from pathlib import Path


def load_prompt(file_path: str) -> str:
    """Read system prompt from a txt file. Raises on missing/empty."""
    path = Path(file_path)
    if not path.exists():
        raise FileNotFoundError(f"Prompt 文件不存在: {file_path}")
    if not path.is_file():
        raise ValueError(f"路径不是文件: {file_path}")
    content = path.read_text(encoding="utf-8").strip()
    if not content:
        raise ValueError(f"Prompt 文件内容为空: {file_path}")
    return content
