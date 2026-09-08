from unittest.mock import Mock

from server.connection_relay import FileOffsetStore, poll_once, wire_telegram_test_client


def test_offsets_survive_restart(tmp_path):
    path = tmp_path / "offset.json"
    store = FileOffsetStore(path)
    assert store.load("tg:12") is None
    store.save("tg:12", 43)
    assert FileOffsetStore(path).load("tg:12") == 43
    assert path.stat().st_mode & 0o777 == 0o600


def test_poll_persists_only_after_success(tmp_path):
    client = Mock()
    client.get_updates.return_value = [{"update_id": 42}]
    handler = Mock()
    store = FileOffsetStore(tmp_path / "offset.json")
    poll_once(client, "tg:12", handler, store)
    assert store.load("tg:12") == 43
    client.get_updates.assert_called_once_with(None, 25)


def test_test_client_wired(monkeypatch):
    client = Mock()
    monkeypatch.setattr("server.connection_relay.client_from_env", lambda: client)
    app = Mock()
    wire_telegram_test_client(app)
    assert app.state.telegram_client is client
