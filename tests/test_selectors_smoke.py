import app_selectors


def test_critical_selector_groups_non_empty():
    required = ["chat_input", "send_button", "menu_button", "sync_anchor", "download_link"]
    for key in required:
        vals = app_selectors.selector_group(key)
        assert vals, f"selector group vazio: {key}"


def test_selector_metadata_present():
    assert app_selectors.LAST_VALIDATED_DATE
    assert app_selectors.LAST_VALIDATED_COMMIT
