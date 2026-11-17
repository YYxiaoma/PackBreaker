"""Allow the sampled content-verification level for unpack items and candidates."""

from collections.abc import Sequence

from alembic import op

revision: str = "0053_unpack_sampled_verification"
down_revision: str | None = "0052_unpack_content_verify_error"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_LEVEL_CHECK = (
    "{column} IS NULL OR {column} IN "
    "('FULL_VERIFIED', 'SAMPLED_VERIFIED', 'CLIENT_CHECK_REQUIRED', 'BLOCKED')"
)
_LEGACY_LEVEL_CHECK = (
    "{column} IS NULL OR {column} IN ('FULL_VERIFIED', 'CLIENT_CHECK_REQUIRED', 'BLOCKED')"
)


def _set_sqlite_foreign_keys(enabled: bool) -> None:
    """Keep referencing rows intact while SQLite rebuilds the referenced table."""

    connection = op.get_bind()
    if connection.dialect.name != "sqlite":
        return
    expected = 1 if enabled else 0
    pragma = "ON" if enabled else "OFF"
    with op.get_context().autocommit_block():
        connection.exec_driver_sql(f"PRAGMA foreign_keys={pragma}")
        actual = int(connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one())
        if actual != expected:
            raise RuntimeError(f"Unable to set SQLite foreign_keys={pragma}")


def _rewrite_check(
    *,
    table: str,
    column: str,
    constraint: str,
    check_template: str,
) -> None:
    connection = op.get_bind()
    constraint_name = op.f(constraint)
    if connection.dialect.name == "sqlite":
        # unpack_execution_item 被 unpack_match_candidate 引用，unpack_match_candidate
        # 又被 unpack_execution_item.selected_candidate_id 引用；重建任一表前必须
        # 关闭外键，否则 SQLite 会静默清空引用列。
        _set_sqlite_foreign_keys(False)
        try:
            with op.batch_alter_table(table, recreate="always") as batch:
                batch.drop_constraint(constraint_name, type_="check")
                batch.create_check_constraint(constraint_name, check_template.format(column=column))
            violations = connection.exec_driver_sql("PRAGMA foreign_key_check").fetchall()
            if violations:
                raise RuntimeError(f"SQLite foreign-key check failed after {table} migration")
        finally:
            _set_sqlite_foreign_keys(True)
    else:
        op.drop_constraint(constraint_name, table, type_="check")
        op.create_check_constraint(constraint_name, table, check_template.format(column=column))


def upgrade() -> None:
    _rewrite_check(
        table="unpack_execution_item",
        column="content_verification_level",
        constraint="ck_unpack_execution_item_content_verification_level",
        check_template=_LEVEL_CHECK,
    )
    _rewrite_check(
        table="unpack_match_candidate",
        column="verification_level",
        constraint="ck_unpack_match_candidate_verification_level",
        check_template=_LEVEL_CHECK,
    )


def downgrade() -> None:
    # 旧 schema 无法表达抽样证据：降级前先映射到“需要下载器校验”，
    # 让已有条目仍可被人工处理，而不是静默丢弃验证记录。
    op.execute(
        "UPDATE unpack_execution_item SET content_verification_level = 'CLIENT_CHECK_REQUIRED' "
        "WHERE content_verification_level = 'SAMPLED_VERIFIED'"
    )
    op.execute(
        "UPDATE unpack_match_candidate SET verification_level = 'CLIENT_CHECK_REQUIRED' "
        "WHERE verification_level = 'SAMPLED_VERIFIED'"
    )
    _rewrite_check(
        table="unpack_execution_item",
        column="content_verification_level",
        constraint="ck_unpack_execution_item_content_verification_level",
        check_template=_LEGACY_LEVEL_CHECK,
    )
    _rewrite_check(
        table="unpack_match_candidate",
        column="verification_level",
        constraint="ck_unpack_match_candidate_verification_level",
        check_template=_LEGACY_LEVEL_CHECK,
    )
