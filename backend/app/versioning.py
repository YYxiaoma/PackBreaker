import tomllib
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path


def _source_tree_version() -> str | None:
    pyproject = Path(__file__).resolve().parents[2] / "pyproject.toml"
    if not pyproject.is_file():
        return None
    data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    project = data.get("project")
    if not isinstance(project, dict):
        return None
    candidate = project.get("version")
    return candidate if isinstance(candidate, str) and candidate else None


def app_version() -> str:
    """返回当前 PackBreaker 包版本；源码工作区优先使用当前项目版本。"""

    source_version = _source_tree_version()
    if source_version is not None:
        return source_version
    try:
        return version("packbreaker")
    except PackageNotFoundError:
        return "1.1.0"
