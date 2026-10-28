import pytest

from notes.store import LimitError, Store


@pytest.fixture
def store():
    return Store(":memory:")


def test_create_and_get(store):
    note = store.create("Shopping", "milk, bread", ["Home", "home"])
    assert store.get(note.id).title == "Shopping"
    assert note.tags == ["home"]                     # normalized and deduplicated


def test_limits(store):
    with pytest.raises(LimitError) as exc:
        store.create("x" * 121, "")
    assert exc.value.code == "title_too_long"
    with pytest.raises(LimitError):
        store.create("ok", "", ["bad tag"])


def test_soft_delete_and_restore(store):
    note = store.create("a", "b")
    assert store.delete(note.id) is True
    assert store.get(note.id) is None and store.list_active() == []
    assert store.restore(note.id).id == note.id


def test_update_without_change_keeps_updated_at(store):
    note = store.create("a", "b")
    assert store.update(note.id, title="a").updated_at == note.updated_at


def test_ids_are_not_reused(store):
    first = store.create("a", "b")
    store.delete(first.id)
    assert store.create("c", "d").id > first.id
