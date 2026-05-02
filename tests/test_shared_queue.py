from shared import BrowserTaskQueue


def test_remote_chat_prioritized_over_python_chat():
    q = BrowserTaskQueue()
    q.put({"action": "CHAT", "chat_id": "py", "request_source": "analisador_prontuarios.py"})
    q.put({"action": "CHAT", "chat_id": "web", "request_source": "frontend"})

    first = q.get()
    second = q.get()

    assert first["chat_id"] == "web"
    assert second["chat_id"] == "py"


def test_round_robin_between_tenants_in_same_priority_lane():
    q = BrowserTaskQueue()
    for _ in range(2):
        q.put({"action": "CHAT", "chat_id": "A", "request_source": "frontend"})
        q.put({"action": "CHAT", "chat_id": "B", "request_source": "frontend"})

    order = [q.get()["chat_id"] for _ in range(4)]
    assert order == ["A", "B", "A", "B"]


def test_dlq_register_and_retry():
    q = BrowserTaskQueue()
    task = {"action": "CHAT", "chat_id": "x", "request_source": "frontend"}
    q.mark_failed(task, "boom")
    failed = q.list_failed(limit=10)
    assert len(failed) == 1
    assert failed[0]["task"]["chat_id"] == "x"

    retried = q.retry_failed(0)
    assert retried is not None
    assert retried["retry_count"] == 1
    got = q.get()
    assert got["chat_id"] == "x"
