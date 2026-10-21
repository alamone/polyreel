import pytest

from polyreel import layout


@pytest.fixture(autouse=True)
def deterministic_search(monkeypatch):
    """Search a fixed amount instead of for a fixed time, so results don't
    depend on how fast the machine running the tests is."""
    monkeypatch.setattr(layout, "SEARCH_BUDGET", None)
    monkeypatch.setattr(layout, "QUICK_BUDGET", None)
