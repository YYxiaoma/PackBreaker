"""Allow the explicit content-verification retry state for unpack items."""

from collections.abc import Sequence

from alembic import op

revision: str = "0052_unpack_content_verify_error"
down_revision: str | None = "0051_unpack_template_match_acceptance"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_STATUS_CHECK = (
    "status IN ('DISCOVERED', 'MATCH_PENDING', 'MATCHING', 'MATCHED_AUTO', "
    "'REVIEW_REQUIRED', 'MATCHED_MANUAL', 'NO_MATCH', 'MATCH_TIMEOUT', "
    "'MATCH_ERROR', 'TORRENT_FETCHING', 'AUXILIARY_FETCHING', "
    "'CONTENT_VERIFYING', 'CONTENT_VERIFIED', 'CONTENT_MISMATCH', "
    "'CONTENT_VERIFY_ERROR', 'PLAN_PENDING', 'EXECUTING', 'CLIENT_VERIFYING', "
    "'COMPLETED', 'EXECUTION_ERROR', 'CANCELLED')"
)


def _set_sqlite_foreign_keys(enabled: bool) -> None:
    """Keep item child rows intact while SQLite rebuilds the referenced table."""

    connection = op.get_bind()
    if connection.dialect.name != "sqlite":
        return
    expected = 1 if enabled else 0
    pragma = "ON" if enabled else "OFF"
    # SQLite cannot toggle foreign_keys inside a transaction. Alembic's
    # autocommit block brackets this connection-level setting and commits each side.
    with op.get_context().autocommit_block():
        connection.exec_driver_sql(f"PRAGMA foreign_keys={pragma}")
        actual = int(connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one())
        if actual != expected:
            raise RuntimeError(f"Unable to set SQLite foreign_keys={pragma}")


def upgrade() -> None:
    connection = op.get_bind()
    constraint_name = op.f("ck_unpack_execution_item_status")
    if connection.dialect.name == "sqlite":
        # unpack_match_candidate, unpack_review_decision and
        # unpack_external_operation_journal all reference unpack_execution_item
        # with ON DELETE CASCADE. Recreating the parent with FK checks enabled
        # silently destroys those historical child rows.
        _set_sqlite_foreign_keys(False)
        try:
            with op.batch_alter_table("unpack_execution_item", recreate="always") as batch:
                batch.drop_constraint(constraint_name, type_="check")
                batch.create_check_constraint(constraint_name, _STATUS_CHECK)
            violations = connection.exec_driver_sql("PRAGMA foreign_key_check").fetchall()
            if violations:
                raise RuntimeError("SQLite foreign-key check failed after unpack item migration")
        finally:
            _set_sqlite_foreign_keys(True)
    else:
        op.drop_constraint(
            constraint_name,
            "unpack_execution_item",
            type_="check",
        )
        op.create_check_constraint(
            constraint_name,
            "unpack_execution_item",
            _STATUS_CHECK,
        )

    # Earlier builds misclassified a torrent-fetch timeout as a matching
    # failure. These rows already have a selected candidate and are safe to
    # re-verify without discarding their match decision.
    op.execute(
        "UPDATE unpack_execution_item "
        "SET status = 'CONTENT_VERIFY_ERROR' "
        "WHERE status = 'MATCH_ERROR' "
        "AND last_error_code = 'UNPACK_VERIFY_TORRENT_FETCH_TIMEOUT' "
        "AND selected_candidate_id IS NOT NULL "
        "AND match_origin IN ('AUTO', 'MANUAL')"
    )


def downgrade() -> None:
    # The previous schema cannot represent this state. Preserve retryability by
    # mapping it to the legacy generic error state before narrowing the CHECK.
    op.execute(
        "UPDATE unpack_execution_item "
        "SET status = 'MATCH_ERROR' "
        "WHERE status = 'CONTENT_VERIFY_ERROR'"
    )
    legacy_status_check = (
        "status IN ('DISCOVERED', 'MATCH_PENDING', 'MATCHING', 'MATCHED_AUTO', "
        "'REVIEW_REQUIRED', 'MATCHED_MANUAL', 'NO_MATCH', 'MATCH_TIMEOUT', "
        "'MATCH_ERROR', 'TORRENT_FETCHING', 'AUXILIARY_FETCHING', "
        "'CONTENT_VERIFYING', 'CONTENT_VERIFIED', 'CONTENT_MISMATCH', "
        "'PLAN_PENDING', 'EXECUTING', 'CLIENT_VERIFYING', 'COMPLETED', "
        "'EXECUTION_ERROR', 'CANCELLED')"
    )
    constraint_name = op.f("ck_unpack_execution_item_status")
    connection = op.get_bind()
    if connection.dialect.name == "sqlite":
        _set_sqlite_foreign_keys(False)
        try:
            with op.batch_alter_table("unpack_execution_item", recreate="always") as batch:
                batch.drop_constraint(constraint_name, type_="check")
                batch.create_check_constraint(constraint_name, legacy_status_check)
            violations = connection.exec_driver_sql("PRAGMA foreign_key_check").fetchall()
            if violations:
                raise RuntimeError("SQLite foreign-key check failed after unpack item downgrade")
        finally:
            _set_sqlite_foreign_keys(True)
    else:
        op.drop_constraint(
            constraint_name,
            "unpack_execution_item",
            type_="check",
        )
        op.create_check_constraint(
            constraint_name,
            "unpack_execution_item",
            legacy_status_check,
        )
