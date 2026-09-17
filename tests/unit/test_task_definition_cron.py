from datetime import UTC, datetime

from backend.app.domain.task_definition import cron_matches, next_cron_run


def test_cron_matches_five_field_expression_in_configured_timezone() -> None:
    instant = datetime(2026, 9, 17, 2, 0, tzinfo=UTC)

    assert cron_matches("0 */2 * * *", instant, timezone="Asia/Shanghai") is True
    assert cron_matches("30 */2 * * *", instant, timezone="Asia/Shanghai") is False


def test_next_cron_run_is_strictly_after_base_and_returned_in_utc() -> None:
    after = datetime(2026, 9, 17, 0, 30, tzinfo=UTC)

    result = next_cron_run("0 */2 * * *", after, timezone="Asia/Shanghai")

    assert result == datetime(2026, 9, 17, 2, 0, tzinfo=UTC)


def test_cron_sunday_accepts_zero_and_seven_aliases() -> None:
    sunday = datetime(2026, 9, 20, 0, 0, tzinfo=UTC)

    assert cron_matches("0 8 * * 0", sunday, timezone="Asia/Shanghai") is True
    assert cron_matches("0 8 * * 7", sunday, timezone="Asia/Shanghai") is True
