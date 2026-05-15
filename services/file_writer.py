from pathlib import Path


def normalize_filename(name: str) -> str:
    """Strip .md suffix if present, then append .md."""
    name = name.strip()
    if name.lower().endswith(".md"):
        name = name[:-3]
    return f"{name}.md"


def write_new_file(directory: str, filename: str, content: str) -> Path:
    """Write content to a new .md file. Returns the file path."""
    dir_path = Path(directory)
    if not dir_path.exists():
        raise FileNotFoundError(f"输出目录不存在: {directory}")
    if not dir_path.is_dir():
        raise NotADirectoryError(f"路径不是目录: {directory}")

    safe_name = normalize_filename(filename)
    if not safe_name or safe_name == ".md":
        raise ValueError("文件名不能为空")

    out_path = dir_path / safe_name
    out_path.write_text(content, encoding="utf-8")
    return out_path


def append_to_file(file_path: str, content: str) -> Path:
    """Append content to an existing .md file. Returns the file path."""
    path = Path(file_path)
    if not path.exists():
        raise FileNotFoundError(f"目标文件不存在: {file_path}")
    if path.suffix.lower() != ".md":
        raise ValueError(f"目标文件不是 .md 文件: {file_path}")

    with open(path, "a", encoding="utf-8") as f:
        f.write("\n\n")
        f.write(content)
    return path
