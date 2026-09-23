from importlib.metadata import PackageNotFoundError, version


def app_version() -> str:
    """返回当前 PackBreaker 包版本；源码工作区未安装元数据时使用当前项目版本。"""

    try:
        return version("packbreaker")
    except PackageNotFoundError:
        return "1.0.2"
