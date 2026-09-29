from __future__ import annotations

import re
import sqlite3
from unittest.mock import Mock

import bot
import database as db
import web
from rso_bot.flows import accounts, auth
from rso_bot.states import S


def _init(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "multi.sqlite"))
    monkeypatch.setattr(db, "BOOTSTRAP_ADMIN_PASSWORD", "")
    db.init_db()


def test_legacy_binding_migration_is_idempotent(tmp_path, monkeypatch):
    path = tmp_path / "multi.sqlite"
    conn = sqlite3.connect(path)
    conn.executescript(
        "CREATE TABLE licschet(id INTEGER PRIMARY KEY,number TEXT UNIQUE NOT NULL,fio TEXT,address TEXT);"
        "CREATE TABLE bot_users(id INTEGER PRIMARY KEY,chat_id INTEGER UNIQUE NOT NULL,ls TEXT,fio TEXT,last_seen TEXT,authorized_1c INTEGER NOT NULL DEFAULT 0);"
        "INSERT INTO licschet(number) VALUES('LS-1');"
        "INSERT INTO bot_users(chat_id,ls,last_seen) VALUES(42,'LS-1','2026-01-01 00:00');"
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr(db, "DB_PATH", str(path))
    monkeypatch.setattr(db, "BOOTSTRAP_ADMIN_PASSWORD", "")

    db.init_db()
    db.init_db()

    assert [row["ls"] for row in db.list_account_bindings(42)] == ["LS-1"]


def test_many_to_many_bind_and_unlink_preserves_bot_user(tmp_path, monkeypatch):
    _init(tmp_path, monkeypatch)
    for number in ("LS-1", "LS-2"):
        assert db.create_lschet(number)
    db.upsert_bot_user(10, "LS-1", "User")
    db.upsert_bot_user(10, "LS-2", "User")
    db.upsert_bot_user(11, "LS-1", "Other")
    conn = db.get_conn()
    conn.execute(
        "INSERT INTO pokazaniya(chat_id,ls,created_at) VALUES(?,?,?)",
        (10, "LS-1", "2026-01-01 00:00"),
    )
    conn.commit()
    conn.close()

    assert [row["ls"] for row in db.list_account_bindings(10)] == ["LS-1", "LS-2"]
    assert db.unlink_bot_user_account(10, "LS-1") is True
    assert [row["ls"] for row in db.list_account_bindings(10)] == ["LS-2"]
    assert [row["ls"] for row in db.list_account_bindings(11)] == ["LS-1"]
    assert db.get_bot_user(10) is not None
    conn = db.get_conn()
    assert conn.execute("SELECT COUNT(*) FROM pokazaniya WHERE chat_id=10").fetchone()[0] == 1
    conn.close()


def test_customer_account_list_never_exposes_stored_fio():
    """The MAX-facing account screen may show an address, but never a name."""
    send_buttons = Mock()
    deps = accounts.AccountManagementDependencies(
        list_bindings=lambda _chat_id: [
            {
                "ls": "LS-1",
                "fio": "Секретное ФИО из базы",
                "address": "ул. Тестовая, 1",
            },
            {
                "ls": "LS-2",
                "fio": "ФИО из API 1С",
                "address": None,
            },
        ],
        make_callback=lambda text, payload: {"text": text, "payload": payload},
        send_buttons=send_buttons,
        start_auth=Mock(),
    )

    accounts.show(42, deps)

    message = send_buttons.call_args.args[1]
    assert message == (
        "👤 Мои лицевые счета:\n"
        "• LS-1 — ул. Тестовая, 1\n"
        "• LS-2"
    )
    assert "Секретное ФИО из базы" not in message
    assert "ФИО из API 1С" not in message


def test_durable_bindings_override_stale_session(tmp_path, monkeypatch):
    _init(tmp_path, monkeypatch)
    state = {"ls": "STALE", "authorized_1c": True}
    deps = auth.AccountDependencies(
        get_state=lambda _chat: state,
        get_bot_user=db.get_bot_user,
        upsert_bot_user=db.upsert_bot_user,
        get_ls=db.get_ls,
        integration_enabled=True,
        logger=Mock(),
        list_account_bindings=db.list_account_bindings,
    )
    assert auth.get_saved_ls(99, deps) is None
    assert "ls" not in state


def test_request_ls_prompts_for_each_bound_account(tmp_path, monkeypatch):
    _init(tmp_path, monkeypatch)
    for number in ("LS-1", "LS-2"):
        db.create_lschet(number)
        db.upsert_bot_user(42, number, "User")
    state = {}
    send_buttons = Mock()
    deps = auth.AuthFlowDependencies(
        get_state=lambda _chat: state, touch=lambda value: value,
        clear_flow=lambda value: value.clear(), send_message=Mock(),
        send_main_menu=Mock(), validate_ls=Mock(), check_ls_brute=Mock(),
        fail_ls=Mock(), reset_ls_brute=Mock(), save_ls=Mock(),
        start_1c_auth=Mock(), continuations={}, integration_enabled=True,
        logger=Mock(), list_account_bindings=db.list_account_bindings,
        send_buttons=send_buttons,
        make_callback=lambda text, payload: {"text": text, "payload": payload},
    )
    auth.request_ls(42, "appeal", deps)
    assert state == {"state": S.ACCOUNT_SELECT, "after_account_select": "appeal"}
    assert [row[0]["payload"] for row in send_buttons.call_args.args[2]] == [
        "account_select:LS-1", "account_select:LS-2",
    ]


def test_request_ls_auto_continues_for_single_account(tmp_path, monkeypatch):
    _init(tmp_path, monkeypatch)
    db.create_lschet("LS-1")
    db.upsert_bot_user(42, "LS-1", "User")
    state = {}
    continuation = Mock()
    deps = auth.AuthFlowDependencies(
        get_state=lambda _chat: state, touch=lambda value: value,
        clear_flow=lambda value: value.clear(), send_message=Mock(),
        send_main_menu=Mock(), validate_ls=Mock(), check_ls_brute=Mock(),
        fail_ls=Mock(), reset_ls_brute=Mock(), save_ls=Mock(),
        start_1c_auth=Mock(), continuations={"appeal": continuation},
        integration_enabled=True, logger=Mock(),
        list_account_bindings=db.list_account_bindings,
    )
    auth.request_ls(42, "appeal", deps)
    continuation.assert_called_once_with(42, "LS-1")
    assert state["flow_ls"] == "LS-1"


def test_module_access_defaults_and_admin_unlink(tmp_path, monkeypatch):
    _init(tmp_path, monkeypatch)
    settings = __import__("rso_bot.operator_chat", fromlist=["x"]).get_module_access_settings()
    assert settings["faq"]["allow_unauthenticated"] is True
    assert settings["ai"]["allow_unauthenticated"] is False
    assert settings["accounts"]["allow_unauthenticated"] is False

    db.create_lschet("LS-1")
    db.upsert_bot_user(42, "LS-1", "User")
    ok, _ = db.create_user("admin-multi", "password", "Admin", "admin")
    assert ok
    client = web.app.test_client()
    assert client.post(
        "/accounts/unlink", data={"chat_id": "42", "ls": "LS-1"}
    ).status_code == 302
    assert client.post("/login", data={"username": "admin-multi", "password": "password"}).status_code == 302
    assert client.post(
        "/accounts/unlink", data={"chat_id": "42", "ls": "LS-1"}
    ).status_code == 400
    page = client.get("/accounts")
    token = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)
    response = client.post(
        "/accounts/unlink",
        data={"csrf_token": token, "chat_id": "42", "ls": "LS-1"},
    )
    assert response.status_code == 302
    assert db.list_account_bindings(42) == []
    assert db.get_bot_user(42) is not None


def test_account_selection_rechecks_target_module(monkeypatch):
    state = {
        "state": S.ACCOUNT_SELECT,
        "after_account_select": "appeal_start",
    }
    menu = Mock()
    monkeypatch.setattr(web.db, "list_account_bindings", Mock())
    monkeypatch.setattr(bot, "_get_state", lambda _chat_id: state)
    monkeypatch.setattr(bot, "send_main_menu", menu)
    monkeypatch.setattr(bot.operator_chat, "module_enabled", lambda key: key != "appeal")

    bot._cb_account_select(42, state, "LS-1")

    menu.assert_called_once_with(42, "Раздел временно недоступен.")
    assert "after_account_select" not in state
    web.db.list_account_bindings.assert_called_once_with(42)


def test_multiple_accounts_reading_selection_persists_selected_ls(
    tmp_path, monkeypatch
):
    _init(tmp_path, monkeypatch)
    bot.user_states.clear()
    for number in ("LS-1", "LS-2"):
        db.create_lschet(number)
        db.upsert_bot_user(42, number, "User", authorized_1c=True)
    db.upsert_1c_meters(
        "LS-2",
        [{
            "meter_number": "M-2",
            "resource_type": "Вода",
            "meter_type": "Однотарифный",
        }],
    )
    monkeypatch.setattr(bot, "ENABLE_1C_INTEGRATION", True)
    monkeypatch.setattr(bot, "send_buttons", Mock(return_value=True))
    monkeypatch.setattr(bot, "send_message", Mock(return_value=True))

    bot._start_pokazaniya(42)
    state = bot._get_state(42)
    assert state["state"] == S.ACCOUNT_SELECT
    bot._cb_account_select(42, state, "LS-2")
    assert state["ls"] == state["flow_ls"] == "LS-2"
    bot._cb_select_meter(42, state, "0")
    bot._on_value1(42, state, "12.5")
    bot._cb_meter_confirm(42, state)

    conn = db.get_conn()
    row = conn.execute(
        "SELECT ls,value1 FROM pokazaniya WHERE chat_id=? ORDER BY id DESC LIMIT 1",
        (42,),
    ).fetchone()
    conn.close()
    assert dict(row) == {"ls": "LS-2", "value1": "12.5"}


def test_unlink_after_reading_selection_blocks_stale_confirm(tmp_path, monkeypatch):
    _init(tmp_path, monkeypatch)
    bot.user_states.clear()
    for number in ("LS-1", "LS-2"):
        db.create_lschet(number)
        db.upsert_bot_user(42, number, "User", authorized_1c=True)
    state = bot._get_state(42)
    state.update({
        "state": S.CONFIRM_POKAZANIYA,
        "flow_ls": "LS-2",
        "ls": "LS-2",
        "meters": [{
            "resource_type": "Вода", "meter_number": "M-2",
            "meter_type": "Однотарифный",
        }],
        "meter_idx": 0,
        "new_value1": "10",
        "new_value2": None,
    })
    db.unlink_bot_user_account(42, "LS-2")
    add_reading = Mock()
    menu = Mock()
    monkeypatch.setattr(bot.db, "add_pokazaniya", add_reading)
    monkeypatch.setattr(bot, "send_main_menu", menu)

    bot._cb_meter_confirm(42, state)

    add_reading.assert_not_called()
    menu.assert_called_once_with(
        42, "Выбранный лицевой счёт больше не привязан. Начните действие заново."
    )
    assert "flow_ls" not in state and "ls" not in state


def test_unlink_after_appointment_selection_blocks_stale_finalize(
    tmp_path, monkeypatch
):
    _init(tmp_path, monkeypatch)
    bot.user_states.clear()
    for number in ("LS-1", "LS-2"):
        db.create_lschet(number)
        db.upsert_bot_user(42, number, "User", authorized_1c=True)
    state = bot._get_state(42)
    state.update({
        "state": S.APPOINTMENT_CONFIRM,
        "flow_ls": "LS-2",
        "ls": "LS-2",
        "appt_branch_id": 1,
        "appt_date": "2026-10-01",
        "appt_time": "10:00",
    })
    db.unlink_bot_user_account(42, "LS-2")
    finalize = Mock()
    menu = Mock()
    monkeypatch.setattr(bot.appointments, "finalize_appointment", finalize)
    monkeypatch.setattr(bot, "send_main_menu", menu)

    bot._finalize_appointment(42)

    finalize.assert_not_called()
    menu.assert_called_once_with(
        42, "Выбранный лицевой счёт больше не привязан. Начните действие заново."
    )


def test_revoked_account_blocks_representative_receipt_side_effect(
    tmp_path, monkeypatch
):
    _init(tmp_path, monkeypatch)
    bot.user_states.clear()
    for number in ("LS-1", "LS-2"):
        db.create_lschet(number)
        db.upsert_bot_user(42, number, "User", authorized_1c=True)
    state = bot._get_state(42)
    state.update({"flow_ls": "LS-2", "ls": "LS-2"})
    db.unlink_bot_user_account(42, "LS-2")
    deliver = Mock()
    menu = Mock()
    monkeypatch.setattr(bot.receipts, "deliver", deliver)
    monkeypatch.setattr(bot, "send_main_menu", menu)

    bot._deliver_kvitanciya(42, "LS-2")

    deliver.assert_not_called()
    menu.assert_called_once_with(
        42, "Выбранный лицевой счёт больше не привязан. Начните действие заново."
    )


def test_public_operator_allows_unbound_profile(monkeypatch):
    state = {}
    request_dialog = Mock(return_value={"id": 7, "status": "active"})
    monkeypatch.setattr(bot, "_get_state", lambda _chat_id: state)
    monkeypatch.setattr(bot, "_ack_callback", Mock())
    monkeypatch.setattr(bot, "_is_authenticated", lambda _chat_id: False)
    monkeypatch.setattr(bot, "_bound_account_numbers", lambda _chat_id: set())
    monkeypatch.setattr(bot.operator_chat, "module_enabled", lambda _key: True)
    monkeypatch.setattr(bot.operator_chat, "module_accessible", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(bot.operator_chat, "is_client_blocked", lambda _chat_id: False)
    monkeypatch.setattr(bot.operator_chat, "get_settings", lambda: {"enabled": True})
    monkeypatch.setattr(bot.operator_chat, "has_active_operators", lambda: True)
    monkeypatch.setattr(bot.operator_chat, "request_dialog", request_dialog)
    monkeypatch.setattr(bot.db, "get_ai_session", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(bot, "_flush_operator_outbox", Mock())

    bot.handle_callback({
        "message": {"recipient": {"chat_id": 42}},
        "callback": {"callback_id": "cb", "payload": "operator_start"},
    })

    assert state["state"] == S.OPERATOR_CHAT
    assert request_dialog.call_args.kwargs["profile"] is None


def test_non_public_operator_prompts_unbound_user_for_auth(monkeypatch):
    state = {"state": S.MENU}
    sender = Mock()
    starter = Mock()
    monkeypatch.setattr(bot, "_ack_callback", Mock())
    monkeypatch.setattr(bot, "_get_state", lambda _chat_id: state)
    monkeypatch.setattr(bot, "_is_authenticated", lambda _chat_id: False)
    monkeypatch.setattr(bot.operator_chat, "module_enabled", lambda _key: True)
    monkeypatch.setattr(
        bot.operator_chat, "module_accessible",
        lambda key, authenticated: key != "operator",
    )
    monkeypatch.setattr(bot, "send_buttons", sender)
    monkeypatch.setattr(bot, "_start_operator_chat", starter)

    bot.handle_callback({
        "message": {"recipient": {"chat_id": 42}},
        "callback": {"callback_id": "cb", "payload": "operator_start"},
    })

    starter.assert_not_called()
    assert state["after_auth_callback"] == "operator_start"
    assert sender.call_args.args[1].startswith("Для использования")


def test_bound_legacy_auth_callback_respects_disabled_accounts(monkeypatch):
    state = {"state": S.MENU}
    menu = Mock()
    starter = Mock()
    monkeypatch.setattr(bot, "_ack_callback", Mock())
    monkeypatch.setattr(bot, "_get_state", lambda _chat_id: state)
    monkeypatch.setattr(bot, "_is_authenticated", lambda _chat_id: True)
    monkeypatch.setattr(
        bot.operator_chat, "module_enabled", lambda key: key != "accounts"
    )
    monkeypatch.setattr(bot, "send_main_menu", menu)
    monkeypatch.setattr(bot, "_start_1c_auth", starter)

    bot.handle_callback({
        "message": {"recipient": {"chat_id": 42}},
        "callback": {"callback_id": "cb", "payload": "auth_1c"},
    })

    starter.assert_not_called()
    menu.assert_called_once_with(42, "Раздел временно недоступен.")


def test_revoked_reading_callback_never_falls_back_to_remaining_account(
    tmp_path, monkeypatch
):
    _init(tmp_path, monkeypatch)
    bot.user_states.clear()
    for number in ("LS-1", "LS-2"):
        db.create_lschet(number)
        db.upsert_bot_user(42, number, "User", authorized_1c=True)
    state = bot._get_state(42)
    state.update({
        "state": S.CONFIRM_POKAZANIYA,
        "flow_ls": "LS-2",
        "ls": "LS-2",
        "meters": [{
            "resource_type": "Вода", "meter_number": "M-2",
            "meter_type": "Однотарифный",
        }],
        "meter_idx": 0,
        "new_value1": "15",
        "new_value2": None,
    })
    db.unlink_bot_user_account(42, "LS-2")
    monkeypatch.setattr(bot, "_ack_callback", Mock())
    monkeypatch.setattr(bot, "send_main_menu", Mock(return_value=True))

    bot.handle_callback({
        "message": {"recipient": {"chat_id": 42}},
        "callback": {"callback_id": "cb", "payload": "meter_confirm"},
    })

    conn = db.get_conn()
    assert conn.execute(
        "SELECT COUNT(*) FROM pokazaniya WHERE chat_id=?", (42,)
    ).fetchone()[0] == 0
    conn.close()
    assert state.get("state") == S.MENU
    assert "flow_ls" not in state and "ls" not in state


def test_revoked_appointment_text_never_continues_on_remaining_account(
    tmp_path, monkeypatch
):
    _init(tmp_path, monkeypatch)
    bot.user_states.clear()
    for number in ("LS-1", "LS-2"):
        db.create_lschet(number)
        db.upsert_bot_user(42, number, "User", authorized_1c=True)
    state = bot._get_state(42)
    state.update({
        "state": S.APPOINTMENT_THEME,
        "flow_ls": "LS-2",
        "ls": "LS-2",
        "appt_branch_id": 1,
        "appt_date": "2026-10-01",
        "appt_time": "10:00",
    })
    db.unlink_bot_user_account(42, "LS-2")
    theme_handler = Mock()
    monkeypatch.setattr(bot.operator_chat, "get_open_dialog_for_chat", lambda _chat: None)
    monkeypatch.setattr(bot, "_on_appointment_theme", theme_handler)
    monkeypatch.setattr(bot, "send_main_menu", Mock(return_value=True))

    bot.handle_message({
        "recipient": {"chat_id": 42},
        "body": {"text": "Тема визита"},
    })

    theme_handler.assert_not_called()
    assert state.get("state") == S.MENU
    assert "appt_branch_id" not in state


def test_revoked_operator_selection_never_opens_dialog_for_remaining_account(
    tmp_path, monkeypatch
):
    _init(tmp_path, monkeypatch)
    bot.user_states.clear()
    for number in ("LS-1", "LS-2"):
        db.create_lschet(number)
        db.upsert_bot_user(42, number, "User", authorized_1c=True)
    state = bot._get_state(42)
    state.update({"flow_ls": "LS-2", "ls": "LS-2"})
    db.unlink_bot_user_account(42, "LS-2")
    request_dialog = Mock()
    monkeypatch.setattr(bot.operator_chat, "is_client_blocked", lambda _chat: False)
    monkeypatch.setattr(bot.operator_chat, "get_settings", lambda: {"enabled": True})
    monkeypatch.setattr(bot.operator_chat, "has_active_operators", lambda: True)
    monkeypatch.setattr(bot.operator_chat, "request_dialog", request_dialog)
    monkeypatch.setattr(bot, "send_main_menu", Mock(return_value=True))

    bot._start_operator_chat(42)

    request_dialog.assert_not_called()
    assert state.get("state") == S.MENU


def test_revoked_appeal_selection_never_creates_for_remaining_account(
    tmp_path, monkeypatch
):
    _init(tmp_path, monkeypatch)
    bot.user_states.clear()
    for number in ("LS-1", "LS-2"):
        db.create_lschet(number)
        db.upsert_bot_user(42, number, "User", authorized_1c=True)
    state = bot._get_state(42)
    state.update({
        "flow_ls": "LS-2",
        "ls": "LS-2",
        "appeal": {"category": "other", "body": "Question"},
    })
    db.unlink_bot_user_account(42, "LS-2")
    create = Mock()
    monkeypatch.setattr(bot.client_api, "create_appeal", create)
    monkeypatch.setattr(bot, "send_main_menu", Mock(return_value=True))

    bot._submit_appeal(42, "LS-2")

    create.assert_not_called()
    assert state.get("state") == S.MENU
    assert "appeal" not in state


def test_real_request_ls_consumes_revoke_marker_without_auto_continuation(
    tmp_path, monkeypatch
):
    _init(tmp_path, monkeypatch)
    bot.user_states.clear()
    db.create_lschet("LS-1")
    db.upsert_bot_user(42, "LS-1", "User", authorized_1c=True)
    state = bot._get_state(42)
    state.update({
        "revoked_selected_ls": True,
        "appeal": {"category": "other", "body": "Question"},
    })
    submit = Mock()
    menu = Mock(return_value=True)
    monkeypatch.setitem(bot._RAW_AFTER_LS_ACTIONS, "appeal", submit)
    monkeypatch.setattr(bot, "send_main_menu", menu)

    bot._request_ls(42, "appeal")

    submit.assert_not_called()
    menu.assert_called_once_with(
        42, "Выбранный лицевой счёт больше не привязан. Начните действие заново."
    )
    assert state.get("state") == S.MENU
