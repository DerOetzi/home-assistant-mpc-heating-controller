from datetime import UTC, datetime

from heating_controller.control.write_budget import WriteBudget, WritePriority

DAY1 = datetime(2026, 11, 5, 12, 0, tzinfo=UTC).timestamp()
# Onecta resets at midnight UTC: 00:30 UTC is already the next day.
DAY2 = datetime(2026, 11, 6, 0, 30, tzinfo=UTC).timestamp()


def test_normal_write_needs_calls_above_reserve() -> None:
    budget = WriteBudget(reserve=20, max_per_day=40)

    assert budget.request(WritePriority.NORMAL, 21, DAY1)
    assert not budget.request(WritePriority.NORMAL, 20, DAY1)
    assert budget.counts(DAY1) == (1, 1)


def test_required_write_ignores_reserve_and_maximum() -> None:
    budget = WriteBudget(reserve=20, max_per_day=1)
    budget.request(WritePriority.NORMAL, None, DAY1)

    assert not budget.request(WritePriority.NORMAL, None, DAY1)
    assert budget.request(WritePriority.REQUIRED, 0, DAY1)
    assert budget.counts(DAY1) == (2, 1)


def test_without_entity_only_own_maximum_counts() -> None:
    budget = WriteBudget(reserve=20, max_per_day=2)

    assert budget.request(WritePriority.NORMAL, None, DAY1)
    assert budget.request(WritePriority.NORMAL, None, DAY1)
    assert not budget.request(WritePriority.NORMAL, None, DAY1)


def test_counts_start_over_at_midnight_utc() -> None:
    budget = WriteBudget(reserve=20, max_per_day=1)
    budget.request(WritePriority.NORMAL, None, DAY1)
    budget.request(WritePriority.NORMAL, None, DAY1)

    assert budget.counts(DAY2) == (0, 0)
    assert budget.request(WritePriority.NORMAL, None, DAY2)


def test_restores_counts_of_the_same_day() -> None:
    budget = WriteBudget(reserve=20, max_per_day=40)
    budget.request(WritePriority.NORMAL, None, DAY1)

    restored = WriteBudget(reserve=20, max_per_day=40)
    restored.restore(budget.export())

    assert restored.counts(DAY1) == (1, 0)
    assert restored.counts(DAY2) == (0, 0)
