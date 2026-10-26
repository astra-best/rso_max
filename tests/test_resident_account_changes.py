"""Scenarios «Сменился лицевой счёт» and «Изменилась почта/пароль»."""
from __future__ import annotations

import json
import re
from unittest.mock import Mock, patch

import pytest

import bot
import database as db
import web
from rso_bot import operator_chat
from rso_bot.flows import accounts

_CSRF_RE = re.compile(r'name="csrf_token" value="([^"]+)"')


@pytest.fixture()
def residents_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "residents.sqlite"))
    monkeypatch.setattr(db, "BOOTSTRAP_ADMIN_PASSWORD", "")
    db.init_db()
    for number, fio, address in (
        ("100001", "Иванов И.И.", "ул. Ленина, 1"),
        ("200002", "Петров П.П.", "ул. Мира, 2"),
        ("300003", "Сидоров С.С.", "ул. Садовая, 3"),
    ):
        assert db.create_lschet(number, fio, address)
    db.upsert_bot_user(42, "100001", None, authorized_1c=True)
    db.bind_bot_user_account(42, "300003")
    db.upsert_bot_user(77, None, None)
    bot.user_states.clear()


def _bound(chat_id: int) -> list[str]:
    return [str(row["ls"]) for row in db.list_account_bindings(chat_id)]


def _outbox(chat_id: int) -> list[dict]:
    conn = db.get_conn()
    try:
        return [dict(row) for row in conn.execute(
            "SELECT * FROM operator_outbox WHERE chat_id=? ORDER BY id", (chat_id,)
        ).fetchall()]
    finally:
        conn.close()


def _client(role: str):
    ok, _ = db.create_user(f"{role}-res", "initial-password", role.title(), role)
    assert ok
    client = web.app.test_client()
    response = client.post(
        "/login", data={"username": f"{role}-res", "password": "initial-password"}
    )
    assert response.status_code == 302
    return client


def _csrf(client, chat_id: int = 42) -> str:
    response = client.get(f"/residents/{chat_id}")
    assert response.status_code == 200
    match = _CSRF_RE.search(response.text)
    assert match is not None
    return match.group(1)


# ── База данных ──────────────────────────────────────────────────────────────

def test_admin_replace_swaps_one_binding_logs_and_notifies(residents_db) -> None:
    result = db.admin_replace_bot_user_account(
        42, "100001", " 200002 ", actor_user_id=1, actor_name="Админ", comment="заявление",
    )

    assert result["new_ls"] == "200002"
    assert _bound(42) == ["200002", "300003"]
    user = db.get_bot_user(42)
    assert (user["ls"], user["authorized_1c"]) == ("200002", 1)
    event = db.list_bot_user_account_events(42)[0]
    assert (event["event_type"], event["old_ls"], event["new_ls"], event["comment"]) == (
        "ls_replaced", "100001", "200002", "заявление",
    )
    [notice] = _outbox(42)
    assert "100001" in notice["body"] and "200002" in notice["body"]
    assert json.loads(notice["buttons_json"])[0][0]["payload"] == "main_menu"


def test_replace_validation_leaves_bindings_untouched(residents_db) -> None:
    kwargs = {"actor_user_id": 1, "actor_name": "A"}
    with pytest.raises(ValueError, match="не найден в справочнике"):
        db.admin_replace_bot_user_account(42, "100001", "999999", **kwargs)
    with pytest.raises(ValueError, match="уже привязан"):
        db.admin_replace_bot_user_account(42, "100001", "300003", **kwargs)
    with pytest.raises(ValueError, match="совпадает"):
        db.admin_replace_bot_user_account(42, "100001", "100001", **kwargs)
    with pytest.raises(ValueError, match="не привязан"):
        db.admin_replace_bot_user_account(42, "200002", "100001", **kwargs)
    with pytest.raises(ValueError, match="Житель не найден"):
        db.admin_replace_bot_user_account(5, "100001", "200002", **kwargs)

    assert _bound(42) == ["100001", "300003"]
    assert db.list_bot_user_account_events(42) == []
    assert _outbox(42) == []


def test_admin_unlink_keeps_other_accounts_and_last_used_value_consistent(residents_db) -> None:
    db.admin_unlink_bot_user_account(42, "100001", actor_user_id=1, actor_name="A")

    assert _bound(42) == ["300003"]
    assert db.get_bot_user(42)["ls"] == "300003"
    assert db.list_bot_user_account_events(42)[0]["event_type"] == "ls_unlinked"
    assert "Мои лицевые счета" in _outbox(42)[0]["body"]


@pytest.mark.parametrize("reason", ["ls_changed", "email_changed", "password_changed"])
def test_reset_revokes_every_binding_and_keeps_history(residents_db, reason) -> None:
    db.admin_reset_bot_user_auth(42, reason, actor_user_id=1, actor_name="Админ")

    assert _bound(42) == []
    user = db.get_bot_user(42)
    assert (user["ls"], user["authorized_1c"]) == (None, 0)
    event = db.list_bot_user_account_events(42)[0]
    assert (event["event_type"], event["reason"], event["old_ls"]) == (
        "auth_reset", reason, "100001, 300003",
    )
    assert "авторизуйтесь заново" in _outbox(42)[0]["body"]


def test_reset_validation_and_silent_mode(residents_db) -> None:
    kwargs = {"actor_user_id": 1, "actor_name": "A"}
    with pytest.raises(ValueError):
        db.admin_reset_bot_user_auth(42, "other", **kwargs)
    with pytest.raises(ValueError, match="Комментарий"):
        db.admin_reset_bot_user_auth(42, "email_changed", comment="x" * 501, **kwargs)
    with pytest.raises(ValueError, match="нет привязанных"):
        db.admin_reset_bot_user_auth(77, "email_changed", **kwargs)

    db.admin_reset_bot_user_auth(42, "email_changed", notify=False, **kwargs)
    assert _outbox(42) == []
    assert db.list_bot_user_account_events(42)[0]["notified"] == 0


def test_list_residents_shows_all_bindings_and_searches_directory(residents_db) -> None:
    assert db.count_residents() == 2
    rows = {row["chat_id"]: row for row in db.list_residents()}
    assert [a["ls"] for a in rows[42]["accounts"]] == ["100001", "300003"]
    assert rows[77]["accounts"] == []
    assert [r["chat_id"] for r in db.list_residents("Садовая")] == [42]
    assert [r["chat_id"] for r in db.list_residents("77")] == [77]
    assert db.count_residents("100%") == 0


def test_account_notice_is_delivered_through_operator_outbox(residents_db) -> None:
    db.admin_reset_bot_user_auth(42, "ls_changed", actor_user_id=1, actor_name="A")
    sent: list[tuple[int, dict]] = []

    def fake_payload(chat_id, body):
        sent.append((chat_id, body))
        return True, None

    with patch.object(bot, "_send_operator_payload", side_effect=fake_payload):
        results = operator_chat.deliver_outbox(bot._deliver_operator_outbox)

    assert [r["status"] for r in results] == ["delivered"]
    [(chat_id, body)] = sent
    assert chat_id == 42 and "авторизуйтесь заново" in body["text"]
    assert body["attachments"][0]["payload"]["buttons"][0][0]["payload"] == "main_menu"


# ── Портал ───────────────────────────────────────────────────────────────────

def test_operator_cannot_open_residents(residents_db) -> None:
    client = _client("operator")
    assert client.get("/residents").status_code == 302
    assert client.get("/residents/42").status_code == 302


def test_admin_sees_resident_list_and_card(residents_db) -> None:
    client = _client("admin")
    listing = client.get("/residents?q=Иванов")
    assert listing.status_code == 200 and "100001" in listing.text
    card = client.get("/residents/42")
    assert "Сменился лицевой счёт" in card.text
    assert "Изменилась почта/пароль" in card.text
    assert "300003" in card.text
    assert client.get("/residents/999", follow_redirects=True).status_code == 200


def test_admin_replaces_account_through_portal(residents_db) -> None:
    client = _client("admin")
    response = client.post(
        "/residents/42/change-ls",
        data={"csrf_token": _csrf(client), "mode": "replace", "old_ls": "100001",
              "new_ls": "200002", "comment": "<b>заявление</b>", "notify": "on"},
        follow_redirects=True,
    )
    assert "Лицевой счёт жителя заменён" in response.text
    assert "&lt;b&gt;заявление&lt;/b&gt;" in response.text
    assert _bound(42) == ["200002", "300003"]
    assert len(_outbox(42)) == 1


def test_portal_unlink_mode_without_notification(residents_db) -> None:
    client = _client("admin")
    client.post(
        "/residents/42/change-ls",
        data={"csrf_token": _csrf(client), "mode": "unlink", "old_ls": "300003"},
    )
    assert _bound(42) == ["100001"]
    assert _outbox(42) == []


def test_portal_reset_auth_for_password_change(residents_db) -> None:
    client = _client("admin")
    response = client.post(
        "/residents/42/reset-auth",
        data={"csrf_token": _csrf(client), "reason": "password_changed", "notify": "on"},
        follow_redirects=True,
    )
    assert "Авторизация жителя сброшена" in response.text
    assert _bound(42) == []


def test_portal_validation_errors_and_csrf(residents_db) -> None:
    client = _client("admin")
    assert client.post(
        "/residents/42/reset-auth", data={"reason": "email_changed"}
    ).status_code == 400
    response = client.post(
        "/residents/42/change-ls",
        data={"csrf_token": _csrf(client), "mode": "replace", "old_ls": "100001",
              "new_ls": "999999"},
        follow_redirects=True,
    )
    assert "не найден в справочнике" in response.text
    bad_reason = client.post(
        "/residents/42/reset-auth",
        data={"csrf_token": _csrf(client), "reason": "ls_changed"},
        follow_redirects=True,
    )
    assert "почта или пароль" in bad_reason.text
    assert _bound(42) == ["100001", "300003"]


def test_accounts_page_unlink_is_recorded_in_history(residents_db) -> None:
    client = _client("admin")
    token = _csrf(client)
    response = client.post(
        "/accounts/unlink",
        data={"csrf_token": token, "chat_id": "42", "ls": "300003"},
        follow_redirects=True,
    )
    assert "Привязка удалена" in response.text
    again = client.post(
        "/accounts/unlink",
        data={"csrf_token": token, "chat_id": "42", "ls": "300003"},
        follow_redirects=True,
    )
    assert "Привязка уже отсутствует" in again.text
    [event] = db.list_bot_user_account_events(42)
    assert (event["event_type"], event["old_ls"], event["notified"]) == ("ls_unlinked", "300003", 0)


# ── Бот ──────────────────────────────────────────────────────────────────────

def _deps(**overrides) -> accounts.AccountManagementDependencies:
    values = {
        "list_bindings": Mock(return_value=[{"ls": "LS-1", "address": None}]),
        "make_callback": lambda text, payload: {"text": text, "payload": payload},
        "send_buttons": Mock(),
        "start_auth": Mock(),
        "unlink_binding": Mock(return_value=True),
        "send_main_menu": Mock(),
    }
    values.update(overrides)
    return accounts.AccountManagementDependencies(**values)


def test_accounts_list_offers_unlink_per_account() -> None:
    deps = _deps()
    accounts.show(42, deps)
    payloads = [row[0]["payload"] for row in deps.send_buttons.call_args.args[2]]
    assert payloads == ["account_add", "account_unlink:LS-1", "main_menu"]


def test_accounts_list_without_unlink_dependency_stays_read_only() -> None:
    deps = _deps(unlink_binding=None)
    accounts.show(42, deps)
    payloads = [row[0]["payload"] for row in deps.send_buttons.call_args.args[2]]
    assert payloads == ["account_add", "main_menu"]


def test_confirm_unlink_ignores_foreign_account() -> None:
    deps = _deps()
    accounts.confirm_unlink(42, "LS-OTHER", deps)
    # Falls back to the list instead of offering to unlink a foreign account.
    assert "Мои лицевые счета" in deps.send_buttons.call_args.args[1]


def test_resident_unlinks_account_in_bot_and_it_is_logged(residents_db) -> None:
    update = lambda payload: {  # noqa: E731
        "callback": {"callback_id": "cb", "payload": payload},
        "message": {"recipient": {"chat_id": 42}},
    }
    with (
        patch.object(bot, "_ack_callback"),
        patch.object(bot, "send_buttons") as send_buttons,
        patch.object(bot, "send_main_menu"),
    ):
        bot.handle_callback(update("account_unlink:300003"))
        assert "Отвязать лицевой счёт 300003" in send_buttons.call_args.args[1]
        bot.handle_callback(update("account_unlink_yes:300003"))

    assert _bound(42) == ["100001"]
    event = db.list_bot_user_account_events(42)[0]
    assert (event["event_type"], event["old_ls"], event["actor_name"]) == (
        "self_unlink", "300003", None,
    )


def test_resident_cannot_unlink_foreign_account_from_bot(residents_db) -> None:
    db.upsert_bot_user(55, "200002", None, authorized_1c=True)
    with (
        patch.object(bot, "_ack_callback"),
        patch.object(bot, "send_buttons"),
        patch.object(bot, "send_main_menu"),
    ):
        bot.handle_callback({
            "callback": {"callback_id": "cb", "payload": "account_unlink_yes:200002"},
            "message": {"recipient": {"chat_id": 42}},
        })
    assert _bound(55) == ["200002"]
    assert _bound(42) == ["100001", "300003"]


def test_unlinking_last_account_returns_to_unauthenticated_menu(residents_db) -> None:
    db.admin_unlink_bot_user_account(42, "300003", actor_user_id=1, actor_name="A", notify=False)
    with (
        patch.object(bot, "_ack_callback"),
        patch.object(bot, "send_buttons"),
        patch.object(bot, "send_main_menu") as menu,
    ):
        bot.handle_callback({
            "callback": {"callback_id": "cb", "payload": "account_unlink_yes:100001"},
            "message": {"recipient": {"chat_id": 42}},
        })
    assert _bound(42) == []
    assert "авторизуйтесь заново" in menu.call_args.args[1]
    assert bot._is_authenticated(42) is False


def test_portal_revocation_is_seen_by_bot_immediately(residents_db) -> None:
    assert bot._is_authenticated(42) is True
    db.admin_reset_bot_user_auth(42, "email_changed", actor_user_id=1, actor_name="A")
    assert bot._is_authenticated(42) is False


def _callback(chat_id: int, payload: str) -> dict:
    return {
        "callback": {"callback_id": "cb", "payload": payload},
        "message": {"recipient": {"chat_id": chat_id}},
    }


def test_unlink_callback_is_rejected_when_accounts_module_is_disabled(residents_db) -> None:
    settings = operator_chat.get_module_settings()
    settings["accounts"] = False
    operator_chat.update_module_settings(settings)
    with (
        patch.object(bot, "_ack_callback"),
        patch.object(bot, "send_buttons"),
        patch.object(bot, "send_main_menu"),
        patch.object(bot, "send_message"),
    ):
        bot.handle_callback(_callback(42, "account_unlink_yes:300003"))
    assert _bound(42) == ["100001", "300003"]
    assert db.list_bot_user_account_events(42) == []


def test_unlink_callback_for_unknown_chat_is_harmless(residents_db) -> None:
    assert db.self_unlink_bot_user_account(999, "100001") is False
    assert _bound(42) == ["100001", "300003"]


def test_self_unlink_clears_stale_session_account(residents_db) -> None:
    st = bot._get_state(42)
    st.update({"ls": "300003", "flow_ls": "300003", "authorized_1c": True})
    with (
        patch.object(bot, "_ack_callback"),
        patch.object(bot, "send_buttons"),
        patch.object(bot, "send_main_menu"),
    ):
        bot.handle_callback(_callback(42, "account_unlink_yes:300003"))
    assert "ls" not in st and "flow_ls" not in st
    assert db.list_bot_user_account_events(42)[0]["reason"] == "self_service"


def test_comment_with_emoji_is_accepted_but_control_chars_are_not(residents_db) -> None:
    db.admin_unlink_bot_user_account(
        42, "300003", actor_user_id=1, actor_name="A", comment="👨‍👩‍👧 переезд",
    )
    assert db.list_bot_user_account_events(42)[0]["comment"] == "👨‍👩‍👧 переезд"
    with pytest.raises(ValueError, match="недопустимые"):
        db.admin_unlink_bot_user_account(
            42, "100001", actor_user_id=1, actor_name="A", comment="a\x00b",
        )


def test_sidebar_opens_active_group_without_javascript(residents_db) -> None:
    client = _client("admin")
    html = client.get("/residents").text
    assert re.search(r'class="nav-group open has-active" data-group="data"', html)
    assert re.search(r'class="nav-group" data-group="clients"', html)
