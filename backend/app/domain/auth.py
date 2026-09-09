from enum import StrEnum


class ApiScope(StrEnum):
    TASKS_READ = "tasks:read"
    TASKS_WRITE = "tasks:write"
    CONFIG_READ = "config:read"
    CONFIG_WRITE = "config:write"


ALL_API_SCOPES = frozenset(ApiScope)
