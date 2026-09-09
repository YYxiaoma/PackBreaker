class ApplicationError(RuntimeError):
    """可安全映射为 problem+json 的应用层错误。"""

    def __init__(
        self,
        *,
        code: str,
        status: int,
        title: str,
        detail: str,
        retry_after: int | None = None,
    ) -> None:
        self.code = code
        self.status = status
        self.title = title
        self.detail = detail
        self.retry_after = retry_after
        super().__init__(detail)
