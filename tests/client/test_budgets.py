import pytest

from loom.client.budgets import parse_token_budget


@pytest.mark.parametrize("value,expected", [("10M", 10000000), ("2.5m", 2500000), ("500K", 500000), ("1_000_000", 1000000), ("0.000001M", 1)])
def test_parse_budget_count_and_units(value, expected):
    assert parse_token_budget(value) == expected


@pytest.mark.parametrize("value", ["0", "-1", "1.5", "10MB", "unlimited", "nan", ""])
def test_parse_budget_rejects_nonpositive_fractional_or_unknown_units(value):
    with pytest.raises(ValueError):
        parse_token_budget(value)
