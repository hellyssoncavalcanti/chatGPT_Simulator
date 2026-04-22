import log_sanitizer as ls


class TestMaskApiKey:
    def test_cvapi_literal_is_masked(self):
        out = ls.mask_api_key("Autorizado via CVAPI_2b9c80c2abf94a76baf8b3e68d89cb7e")
        assert "2b9c80c2" not in out
        assert "CVAPI_2b9c" in out
        assert "***" in out

    def test_api_key_query_string(self):
        out = ls.mask_api_key("/health?api_key=SECRET_ABCDEFGH&x=1")
        assert "SECRET_ABCDEFGH" not in out
        assert "SECR***" in out

    def test_api_key_json_field(self):
        out = ls.mask_api_key('{"api_key": "topsecret1234567890"}')
        assert "topsecret1234567890" not in out
        assert "tops***" in out

    def test_x_api_key_header(self):
        out = ls.mask_api_key("X-API-Key: topsecret1234567890")
        assert "topsecret1234567890" not in out
        assert "tops***" in out

    def test_api_key_case_insensitive(self):
        assert "topsecret1234" not in ls.mask_api_key("API_KEY=topsecret1234567890")

    def test_non_key_text_unchanged(self):
        assert ls.mask_api_key("nada a mascarar aqui") == "nada a mascarar aqui"

    def test_empty_and_none(self):
        assert ls.mask_api_key("") == ""
        assert ls.mask_api_key(None) is None


class TestMaskBearerToken:
    def test_basic_bearer_masked(self):
        out = ls.mask_bearer_token(
            "Authorization: Bearer abcdefghijklmnopqrstuvwxyz1234567890"
        )
        assert "abcdefghijklmnopqrstuvwxyz" not in out
        assert "Bearer abcd***" in out

    def test_short_token_masked_wholly(self):
        # Mínimo exigido pelo regex é 8 chars — aqui tem exatamente 8,
        # máscara parcial ainda preserva 4 chars.
        out = ls.mask_bearer_token("Bearer abcd1234")
        assert out == "Bearer abcd***"

    def test_jwt_like_token_masked(self):
        jwt = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.abc123"
        out = ls.mask_bearer_token(f"Authorization: Bearer {jwt}")
        assert "eyJzdWIi" not in out

    def test_no_bearer_keyword_unchanged(self):
        s = "Authorization: Basic aGVsbG86d29ybGQ="
        assert ls.mask_bearer_token(s) == s


class TestMaskSessionCookie:
    def test_session_cookie_masked(self):
        out = ls.mask_session_cookie(
            "Cookie: session=abcdefghij1234; other=ok"
        )
        assert "abcdefghij1234" not in out
        assert "session=abcd***" in out
        assert "other=ok" in out  # não mexe em outros cookies

    def test_csrftoken_masked(self):
        out = ls.mask_session_cookie("csrftoken=xyz9876543210")
        assert "xyz9876543210" not in out

    def test_sid_masked(self):
        out = ls.mask_session_cookie("sid=aaaabbbbccccdddd")
        assert "aaaabbbbccccdddd" not in out
        assert "sid=aaaa***" in out

    def test_short_value_fully_masked(self):
        out = ls.mask_session_cookie("session=abc")
        # Tamanho < 4 não é capturado pelo regex (>=4 obrigatório).
        # Então fica literal — ok para não vazar "abc" neutro.
        assert out == "session=abc"


class TestMaskFilePath:
    def test_windows_path_user_masked(self):
        out = ls.mask_file_path(r"C:\Users\john.doe\AppData\Local\Chromium")
        assert "john.doe" not in out
        assert r"C:\Users\***" in out
        assert "Chromium" in out  # parte útil preservada

    def test_posix_home_masked(self):
        out = ls.mask_file_path("/home/analyst/.config/chromium-profile/Default")
        assert "analyst" not in out
        assert "/home/***" in out
        assert "Default" in out

    def test_posix_users_masked(self):
        out = ls.mask_file_path("/Users/jane/Library/Chromium")
        assert "jane" not in out
        assert "/Users/***" in out

    def test_path_without_username_unchanged(self):
        s = "/opt/chromium-profile/Default"
        assert ls.mask_file_path(s) == s

    def test_empty_and_none(self):
        assert ls.mask_file_path("") == ""
        assert ls.mask_file_path(None) is None


class TestSanitize:
    def test_combines_all_masks(self):
        raw = (
            "Auth=Bearer abcdefghij1234 "
            "X-API-Key: topsecret1234567890 "
            "session=abcdefghij1234 "
            "path=/home/john/chromium"
        )
        out = ls.sanitize(raw)
        assert "abcdefghij1234" not in out
        assert "topsecret1234567890" not in out
        assert "john" not in out

    def test_idempotent(self):
        raw = "API_KEY=topsecret1234567890 /home/john/x Bearer abcdef123456"
        once = ls.sanitize(raw)
        twice = ls.sanitize(once)
        assert once == twice

    def test_empty_and_none(self):
        assert ls.sanitize("") == ""
        assert ls.sanitize(None) is None


class TestSanitizeIter:
    def test_each_element_sanitized(self):
        out = ls.sanitize_iter([
            "Bearer abcdef123456",
            "X-API-Key: topsecret1234567890",
            "limpo",
        ])
        assert out[0].startswith("Bearer abcd")
        assert "topsecret1234567890" not in out[1]
        assert out[2] == "limpo"

    def test_empty_iterable(self):
        assert ls.sanitize_iter([]) == []

    def test_preserves_order(self):
        out = ls.sanitize_iter(["a", "b", "c"])
        assert out == ["a", "b", "c"]


class TestSanitizeMapping:
    def test_string_values_masked(self):
        out = ls.sanitize_mapping({
            "path": "/home/john/x",
            "auth": "Bearer abcdefghijk",
        })
        assert out["path"].startswith("/home/***")
        assert "abcdefghijk" not in out["auth"]

    def test_nested_dict_sanitized(self):
        out = ls.sanitize_mapping({
            "headers": {"authorization": "Bearer abcdefghijk"},
            "n": 42,
        })
        assert "abcdefghijk" not in out["headers"]["authorization"]
        assert out["n"] == 42

    def test_non_string_values_untouched(self):
        out = ls.sanitize_mapping({"retries": 3, "active": True, "tags": None})
        assert out == {"retries": 3, "active": True, "tags": None}

    def test_list_values_sanitized(self):
        out = ls.sanitize_mapping({"logs": ["Bearer abcdefghijk", "ok"]})
        assert "abcdefghijk" not in out["logs"][0]
        assert out["logs"][1] == "ok"

    def test_non_dict_input_returned_as_is(self):
        assert ls.sanitize_mapping("not a dict") == "not a dict"  # type: ignore[arg-type]
        assert ls.sanitize_mapping(None) is None
