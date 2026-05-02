"""Testes offline para Scripts/payload_validators.py."""
import pytest
import payload_validators as pv


# ══════════════════════════════════════════════════════
# validate_login_request
# ══════════════════════════════════════════════════════
class TestValidateLoginRequest:
    def test_valid_credentials(self):
        ok, errs = pv.validate_login_request({"username": "admin", "password": "pass"})
        assert ok and errs == []

    def test_missing_username(self):
        ok, errs = pv.validate_login_request({"password": "x"})
        assert not ok
        assert any("username" in e for e in errs)

    def test_empty_username(self):
        ok, errs = pv.validate_login_request({"username": "   ", "password": "x"})
        assert not ok

    def test_missing_password(self):
        ok, errs = pv.validate_login_request({"username": "u"})
        assert not ok
        assert any("password" in e for e in errs)

    def test_empty_password(self):
        ok, errs = pv.validate_login_request({"username": "u", "password": ""})
        assert not ok

    def test_username_too_long(self):
        ok, errs = pv.validate_login_request({
            "username": "a" * (pv.MAX_USERNAME_LEN + 1),
            "password": "p",
        })
        assert not ok
        assert any("username" in e for e in errs)

    def test_username_exactly_at_limit(self):
        ok, errs = pv.validate_login_request({
            "username": "a" * pv.MAX_USERNAME_LEN,
            "password": "p",
        })
        assert ok

    def test_password_too_long(self):
        ok, errs = pv.validate_login_request({
            "username": "u",
            "password": "x" * (pv.MAX_PASSWORD_LEN + 1),
        })
        assert not ok
        assert any("password" in e for e in errs)

    def test_password_exactly_at_limit(self):
        ok, errs = pv.validate_login_request({
            "username": "u",
            "password": "x" * pv.MAX_PASSWORD_LEN,
        })
        assert ok

    def test_non_string_username(self):
        ok, errs = pv.validate_login_request({"username": 42, "password": "p"})
        assert not ok

    def test_non_string_password(self):
        ok, errs = pv.validate_login_request({"username": "u", "password": ["list"]})
        assert not ok

    def test_empty_dict_returns_both_errors(self):
        ok, errs = pv.validate_login_request({})
        assert not ok
        assert len(errs) == 2


# ══════════════════════════════════════════════════════
# validate_chat_request
# ══════════════════════════════════════════════════════
class TestValidateChatRequest:
    def _valid(self, **extra):
        """Payload mínimo válido com overrides."""
        base = {"message": "oi", "stream": False}
        base.update(extra)
        return pv.validate_chat_request(base)

    # -- message --
    def test_message_valid(self):
        ok, errs = self._valid(message="olá mundo")
        assert ok and errs == []

    def test_message_absent_ok(self):
        ok, _ = pv.validate_chat_request({})
        assert ok

    def test_message_too_long(self):
        ok, errs = pv.validate_chat_request({"message": "x" * (pv.MAX_MESSAGE_CHARS + 1)})
        assert not ok
        assert any("message" in e for e in errs)

    def test_message_at_limit_ok(self):
        ok, _ = pv.validate_chat_request({"message": "x" * pv.MAX_MESSAGE_CHARS})
        assert ok

    def test_message_non_string(self):
        ok, errs = pv.validate_chat_request({"message": 123})
        assert not ok

    # -- chat_id --
    def test_chat_id_valid_uuid(self):
        ok, _ = self._valid(chat_id="550e8400-e29b-41d4-a716-446655440000")
        assert ok

    def test_chat_id_absent_ok(self):
        ok, _ = pv.validate_chat_request({})
        assert ok

    def test_chat_id_too_long(self):
        ok, errs = self._valid(chat_id="x" * (pv.MAX_CHAT_ID_LEN + 1))
        assert not ok

    def test_chat_id_non_string(self):
        ok, errs = self._valid(chat_id={"nested": "dict"})
        assert not ok

    # -- url --
    def test_url_valid_https(self):
        ok, _ = self._valid(url="https://chatgpt.com/c/abc")
        assert ok

    def test_url_valid_http(self):
        ok, _ = self._valid(url="http://localhost:3000")
        assert ok

    def test_url_absent_ok(self):
        ok, _ = pv.validate_chat_request({})
        assert ok

    def test_url_none_string_ok(self):
        ok, _ = self._valid(url="None")
        assert ok

    def test_url_empty_string_ok(self):
        ok, _ = self._valid(url="")
        assert ok

    def test_url_no_protocol_rejected(self):
        ok, errs = self._valid(url="chatgpt.com/c/abc")
        assert not ok
        assert any("http" in e for e in errs)

    def test_url_too_long(self):
        ok, errs = self._valid(url="https://" + "x" * pv.MAX_URL_LEN)
        assert not ok

    # -- browser_profile --
    def test_browser_profile_valid(self):
        ok, _ = self._valid(browser_profile="default")
        assert ok

    def test_browser_profile_with_dash(self):
        ok, _ = self._valid(browser_profile="segunda-chance")
        assert ok

    def test_browser_profile_absent_ok(self):
        ok, _ = pv.validate_chat_request({})
        assert ok

    def test_browser_profile_empty_ok(self):
        ok, _ = self._valid(browser_profile="")
        assert ok

    def test_browser_profile_special_chars_rejected(self):
        ok, errs = self._valid(browser_profile="profile; DROP TABLE")
        assert not ok

    def test_browser_profile_too_long(self):
        ok, errs = self._valid(browser_profile="a" * (pv.MAX_BROWSER_PROFILE_LEN + 1))
        assert not ok

    # -- attachments --
    def test_attachments_valid_list(self):
        ok, _ = self._valid(attachments=[{"name": "a.txt", "content": "aGVsbG8="}])
        assert ok

    def test_attachments_absent_ok(self):
        ok, _ = pv.validate_chat_request({})
        assert ok

    def test_attachments_not_list(self):
        ok, errs = self._valid(attachments={"name": "x"})
        assert not ok

    def test_attachments_too_many(self):
        ok, errs = self._valid(
            attachments=[{"name": f"f{i}.txt", "content": ""} for i in range(pv.MAX_ATTACHMENT_COUNT + 1)]
        )
        assert not ok
        assert any("attachments" in e for e in errs)

    def test_attachment_item_not_dict(self):
        ok, errs = self._valid(attachments=["not-a-dict"])
        assert not ok

    # -- stream --
    def test_stream_bool_true(self):
        ok, _ = self._valid(stream=True)
        assert ok

    def test_stream_non_bool_rejected(self):
        ok, errs = self._valid(stream="true")
        assert not ok

    # -- messages array --
    def test_messages_valid_list(self):
        ok, _ = self._valid(messages=[{"role": "user", "content": "oi"}])
        assert ok

    def test_messages_too_many(self):
        ok, errs = self._valid(messages=[{}] * (pv.MAX_MESSAGES_COUNT + 1))
        assert not ok

    def test_messages_not_list(self):
        ok, errs = self._valid(messages="texto")
        assert not ok

    # -- source_hint --
    def test_source_hint_valid(self):
        ok, _ = self._valid(source_hint="analisador.py")
        assert ok

    def test_source_hint_too_long(self):
        ok, errs = self._valid(source_hint="x" * (pv.MAX_SOURCE_HINT_LEN + 1))
        assert not ok


# ══════════════════════════════════════════════════════
# validate_sync_request
# ══════════════════════════════════════════════════════
class TestValidateSyncRequest:
    def test_valid_with_url(self):
        ok, errs = pv.validate_sync_request({"url": "https://chatgpt.com/c/abc"})
        assert ok and errs == []

    def test_valid_with_chat_id(self):
        ok, errs = pv.validate_sync_request({"chat_id": "abc-123"})
        assert ok and errs == []

    def test_both_absent_rejected(self):
        ok, errs = pv.validate_sync_request({})
        assert not ok
        assert any("url ou chat_id" in e for e in errs)

    def test_url_no_protocol_rejected(self):
        ok, errs = pv.validate_sync_request({"url": "chatgpt.com/c/abc"})
        assert not ok

    def test_url_too_long(self):
        ok, errs = pv.validate_sync_request({"url": "https://" + "x" * pv.MAX_URL_LEN})
        assert not ok

    def test_chat_id_too_long(self):
        ok, errs = pv.validate_sync_request({"chat_id": "x" * (pv.MAX_CHAT_ID_LEN + 1)})
        assert not ok

    def test_browser_profile_valid(self):
        ok, _ = pv.validate_sync_request({"url": "https://a.com", "browser_profile": "default"})
        assert ok

    def test_browser_profile_invalid_chars(self):
        ok, errs = pv.validate_sync_request({
            "url": "https://a.com",
            "browser_profile": "../../etc/passwd",
        })
        assert not ok

    def test_sync_browser_profile_also_validated(self):
        ok, errs = pv.validate_sync_request({
            "chat_id": "abc",
            "sync_browser_profile": "profile/../evil",
        })
        assert not ok

    def test_none_string_url_with_chat_id_ok(self):
        ok, _ = pv.validate_sync_request({"url": "None", "chat_id": "abc"})
        assert ok
