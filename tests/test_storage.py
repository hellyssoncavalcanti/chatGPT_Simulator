import db
import storage


def _use_temp_db(tmp_path, monkeypatch):
    db_file = tmp_path / "app.db"
    monkeypatch.setattr(storage.config, "APP_DB_FILE", str(db_file), raising=False)
    monkeypatch.setattr(db.config, "APP_DB_FILE", str(db_file), raising=False)
    db._INITIALIZED = False


def test_append_message_deduplicates_consecutive(tmp_path, monkeypatch):
    _use_temp_db(tmp_path, monkeypatch)
    storage.append_message("chat1", "user", "oi")
    storage.append_message("chat1", "user", "oi")
    data = storage.load_chats()
    assert len(data["chat1"]["messages"]) == 1


def test_find_chat_by_origin_prefers_latest_by_context(tmp_path, monkeypatch):
    _use_temp_db(tmp_path, monkeypatch)
    storage.save_chat(
        "a",
        "Chat A",
        "https://chatgpt.com/c/a",
        [{"role": "user", "content": "1"}],
        origin_url="https://site/app?id_paciente=10&id_atendimento=20",
    )
    storage.save_chat(
        "b",
        "Chat B",
        "https://chatgpt.com/c/b",
        [{"role": "user", "content": "2"}],
        origin_url="https://site/app?id_paciente=10&id_atendimento=20",
    )
    found = storage.find_chat_by_origin("https://site/app?id_paciente=10&id_atendimento=20")
    assert found is not None
    assert found["chat_id"] == "b"
