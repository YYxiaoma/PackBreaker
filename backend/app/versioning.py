from importlib.metadata import PackageNotFoundError, version


def app_version() -> str:
    """返回当前 PackBreaker 包版本；源码工作区未安装元数据时使用项目初始版本。"""

    try:
        return version("packbreaker")
    except PackageNotFoundError:
        return "0.1.0"
