import json

import storage


def test_append_message_deduplicates_consecutive(tmp_path, monkeypatch):
    chats_file = tmp_path / "history.json"
    monkeypatch.setattr(storage.config, "CHATS_FILE", str(chats_file), raising=True)

    storage.append_message("chat1", "user", "oi")
    storage.append_message("chat1", "user", "oi")
    data = storage.load_chats()

    assert len(data["chat1"]["messages"]) == 1


def test_find_chat_by_origin_prefers_latest_by_context(tmp_path, monkeypatch):
    chats_file = tmp_path / "history.json"
    monkeypatch.setattr(storage.config, "CHATS_FILE", str(chats_file), raising=True)

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
