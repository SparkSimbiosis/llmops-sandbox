import pytest

from notes.search import search
from notes.store import Store


@pytest.fixture
def store():
    s = Store(":memory:")
    s.create("Trip plan", "tickets and hotel")
    s.create("Groceries", "plan the menu for the trip")
    s.create("Old trip", "deleted one")
    s.delete(3)
    return s


def test_title_match_ranks_higher(store):
    assert [n.title for n in search(store, "trip plan")] == ["Trip plan", "Groceries"]


def test_deleted_notes_are_never_returned(store):
    assert all(n.id != 3 for n in search(store, "trip"))


def test_empty_query_is_an_error(store):
    with pytest.raises(ValueError):
        search(store, " ")
