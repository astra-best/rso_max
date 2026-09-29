from __future__ import annotations

from unittest.mock import Mock

import pytest

import bot


def _callback(payload: str) -> dict:
    return {
        "message": {"recipient": {"chat_id": 77}},
        "callback": {"callback_id": "cb", "payload": payload},
    }


def test_every_registered_callback_is_owned_or_explicitly_exempt():
    assert set(bot._CALLBACK_STATIC) == set(bot._CALLBACK_STATIC_MODULES) | bot._CALLBACK_STATIC_EXEMPT
    assert set(bot._CALLBACK_PREFIXES) == set(bot._CALLBACK_PREFIX_MODULES) | bot._CALLBACK_PREFIX_EXEMPT


@pytest.mark.parametrize("payload,module", sorted(bot._CALLBACK_STATIC_MODULES.items()))
def test_disabled_static_callback_fails_closed(monkeypatch, payload, module):
    if module == "auth":
        pytest.skip("authorization remains available as the private-module recovery path")
    state = {"state": "anything", "private": "value"}
    menu = Mock()
    monkeypatch.setattr(bot, "_ack_callback", Mock())
    monkeypatch.setattr(bot, "_get_state", lambda _chat_id: state)
    monkeypatch.setattr(bot, "send_main_menu", menu)
    monkeypatch.setattr(bot.operator_chat, "module_enabled", lambda key: key != module)
    bot.handle_callback(_callback(payload))
    menu.assert_called_once_with(77, "Раздел временно недоступен.")
    assert state.get("state") == bot.S.MENU


def test_disabled_auth_setting_cannot_strand_unauthenticated_user(monkeypatch):
    state = {"state": bot.S.MENU}
    starter = Mock()
    monkeypatch.setattr(bot, "_ack_callback", Mock())
    monkeypatch.setattr(bot, "_get_state", lambda _chat_id: state)
    monkeypatch.setattr(bot, "_is_authenticated", lambda _chat_id: False)
    monkeypatch.setattr(bot, "_start_1c_auth", starter)
    monkeypatch.setattr(bot.operator_chat, "module_enabled", lambda _key: False)

    bot.handle_callback(_callback("auth_1c"))

    starter.assert_called_once_with(77, None)


def test_module_disabled_during_deferred_auth_cancels_before_validation(monkeypatch):
    state = {
        "state": bot.S.AWAIT_LS_1C,
        "after_1c_auth": "appeal_start",
    }
    menu = Mock()
    validator = Mock()
    monkeypatch.setattr(bot, "_get_state", lambda _chat_id: state)
    monkeypatch.setattr(bot, "send_main_menu", menu)
    monkeypatch.setattr(bot, "_validate_ls", validator)
    monkeypatch.setattr(bot.operator_chat, "get_open_dialog_for_chat", lambda _chat_id: None)
    monkeypatch.setattr(bot.operator_chat, "module_enabled", lambda key: key != "appeal")

    bot.handle_message({"recipient": {"chat_id": 77}, "body": {"text": "LS-1"}})

    validator.assert_not_called()
    menu.assert_called_once_with(77, "Раздел временно недоступен.")


@pytest.mark.parametrize("prefix,module", sorted(bot._CALLBACK_PREFIX_MODULES.items()))
def test_disabled_prefix_callback_fails_closed(monkeypatch, prefix, module):
    state = {"state": "anything", "private": "value"}
    menu = Mock()
    monkeypatch.setattr(bot, "_ack_callback", Mock())
    monkeypatch.setattr(bot, "_get_state", lambda _chat_id: state)
    monkeypatch.setattr(bot, "send_main_menu", menu)
    monkeypatch.setattr(bot.operator_chat, "module_enabled", lambda key: key != module)
    bot.handle_callback(_callback(f"{prefix}:1"))
    menu.assert_called_once_with(77, "Раздел временно недоступен.")
    assert state.get("state") == bot.S.MENU


def test_every_text_state_is_owned_or_explicitly_exempt():
    assert set(bot._MESSAGE_HANDLERS) <= set(bot._STATE_MODULES) | bot._STATE_EXEMPT


@pytest.mark.parametrize(
    "state_name,module",
    sorted((state, bot._STATE_MODULES[state]) for state in bot._MESSAGE_HANDLERS if state in bot._STATE_MODULES),
)
def test_disabled_text_flow_fails_closed(monkeypatch, state_name, module):
    if module == "auth":
        pytest.skip("an in-progress authorization must remain usable")
    state = {"state": state_name}
    menu = Mock()
    monkeypatch.setattr(bot, "_get_state", lambda _chat_id: state)
    monkeypatch.setattr(bot, "send_main_menu", menu)
    monkeypatch.setattr(bot.operator_chat, "get_open_dialog_for_chat", lambda _chat_id: None)
    monkeypatch.setattr(bot.operator_chat, "module_enabled", lambda key: key != module)
    bot.handle_message({"recipient": {"chat_id": 77}, "body": {"text": "payload"}})
    menu.assert_called_once_with(77, "Раздел временно недоступен.")
    assert state.get("state") == bot.S.MENU


def test_rating_resets_operator_flow_before_menu(monkeypatch):
    state = {"state": bot.S.OPERATOR_CHAT, "private": "value"}
    menu = Mock()
    monkeypatch.setattr(bot.operator_chat, "rate_dialog", lambda *_args: True)
    monkeypatch.setattr(bot, "_get_state", lambda _chat_id: state)
    monkeypatch.setattr(bot, "send_main_menu", menu)
    bot._rate_operator(77, 3, 5)
    assert state.get("state") == bot.S.MENU
    menu.assert_called_once_with(77, "Спасибо за оценку!")
