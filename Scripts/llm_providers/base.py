from abc import ABC, abstractmethod


class LLMProvider(ABC):
    """Contract that every LLM back-end must satisfy.

    A provider receives a *task dict* (the same dict that previously went
    straight into browser_queue) and is solely responsible for placing SSE
    events onto task['stream_queue'] and sending the ``None`` sentinel when
    done.  server.py never learns which provider is active.
    """

    @abstractmethod
    def dispatch_task(self, task: dict) -> None:
        """Enqueue or execute *task*.  Results arrive via task['stream_queue']."""

    def provider_name(self) -> str:
        return self.__class__.__name__
