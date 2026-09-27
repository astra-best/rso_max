from __future__ import annotations

from unittest.mock import Mock

import bot
import pytest
from rso_bot import max_transport
from rso_bot.flows import faq


def _dependencies(
    *,
    scripts: tuple[list[dict], object | None] = ([], None),
    tree: tuple[dict | None, object | None] = (None, None),
) -> tuple[faq.FaqDependencies, dict]:
    state: dict = {"state": "menu"}
    deps = faq.FaqDependencies(
        list_scripts=Mock(return_value=scripts),
        get_script_tree=Mock(return_value=tree),
        get_state=Mock(return_value=state),
        touch=Mock(side_effect=lambda value: value),
        make_callback=lambda label, payload: {
            "type": "callback",
            "text": label,
            "payload": payload,
        },
        make_link=lambda label, url: {"type": "link", "text": label, "url": url},
        send_message=Mock(),
        send_buttons=Mock(),
        send_main_menu=Mock(),
        logger=Mock(),
        script_list_state="script_list",
        script_node_state="script_node",
        menu_state="menu",
    )
    return deps, state


def test_show_scripts_list_renders_topics_and_main_menu():
    deps, state = _dependencies(
        scripts=([{"id": 8, "title": "Оплата"}, {"id": 3, "title": "Льготы"}], None),
    )

    faq.show_scripts_list(42, deps)

    assert state["state"] == "script_list"
    deps.touch.assert_called_once_with(state)
    deps.send_buttons.assert_called_once_with(
        42,
        "📚 Выберите тему:",
        [
            [{"type": "callback", "text": "Оплата", "payload": "script:8"}],
            [{"type": "callback", "text": "Льготы", "payload": "script:3"}],
            [{"type": "callback", "text": "🏠 Главное меню", "payload": "main_menu"}],
        ],
    )


def test_show_scripts_list_preserves_empty_and_error_responses():
    empty, _ = _dependencies()
    faq.show_scripts_list(42, empty)
    empty.send_message.assert_called_once_with(42, "📚 Раздел FAQ пуст.")
    empty.send_main_menu.assert_called_once_with(42)

    failed, _ = _dependencies(scripts=([], "unavailable"))
    faq.show_scripts_list(43, failed)
    failed.send_message.assert_called_once_with(43, "⚠️ Сервис временно недоступен.")
    failed.send_main_menu.assert_called_once_with(43)


def test_script_list_paginates_without_dropping_actions():
    scripts = [{"id": index, "title": f"Тема {index}"} for index in range(35)]
    deps, _ = _dependencies(scripts=(scripts, None))
    faq.show_scripts_list(42, deps)
    assert deps.send_buttons.call_count == 1
    first_page = deps.send_buttons.call_args.args[2]
    assert len(first_page) == 30
    token = deps.get_state.return_value["faq_scripts_token"]
    assert first_page[-1][-1]["payload"] == f"faq_scripts_page:{token}:1"
    faq.show_scripts_page(42, token, 1, deps)
    second_page = deps.send_buttons.call_args.args[2]
    payloads = [
        button["payload"]
        for rows in (first_page[:-1], second_page[:-1])
        for row in rows for button in row
    ]
    assert payloads == [f"script:{index}" for index in range(35)] + ["main_menu"]


def test_open_script_selects_root_and_renders_its_children():
    tree = {
        "nodes": [
            {"id": 20, "title": "Ответ", "is_terminal": True},
            {"id": 10, "title": "Вопрос", "is_terminal": False},
        ],
        "edges": [{"from_node_id": 10, "label": "Продолжить", "to_node_id": 20}],
    }
    deps, state = _dependencies(tree=(tree, None))

    faq.open_script(42, 7, deps)

    assert state["state"] == "script_node"
    assert state["script"]["current"] == 10
    rows = deps.send_buttons.call_args.args[2]
    assert deps.send_buttons.call_args.args[:2] == (42, "📌 Вопрос")
    assert rows[0][0]["text"] == "Продолжить"
    assert rows[0][0]["payload"].startswith("faq_go:7:10:20:")
    assert rows[1] == [{
        "type": "callback", "text": "🏠 Главное меню", "payload": "main_menu",
    }]


def test_open_script_uses_smallest_root_and_logs_ambiguous_tree():
    tree = {
        "nodes": [
            {"id": 8, "title": "Восьмой", "is_terminal": True},
            {"id": 2, "title": "Второй", "is_terminal": True},
        ],
        "edges": [],
    }
    deps, state = _dependencies(tree=(tree, None))

    faq.open_script(42, 99, deps)

    deps.logger.warning.assert_called_once_with(
        "Скрипт id=%s: найдено %d корневых узлов, берём минимальный",
        99,
        2,
    )
    assert deps.send_buttons.call_args.args[1] == (
        "📌 Второй\n\n"
        "Если вы не получили ответ на ваш вопрос, "
        "вы можете обратиться к ИИ-помощнику."
    )
    assert deps.send_buttons.call_args.args[2][0][0]["payload"].startswith("faq_terminal_ai:")
    assert state["state"] == "menu"
    assert "script" not in state


def test_terminal_node_returns_to_main_menu_and_clears_script():
    deps, state = _dependencies()
    state.update(
        {
            "state": "script_node",
            "script": {
                "nodes": {5: {"id": 5, "title": "Готовый ответ", "is_terminal": True}},
                "edges_by_from": {},
                "current": 5,
            },
        }
    )

    faq.show_script_node(42, deps)

    assert deps.send_buttons.call_args.args[1] == (
        "📌 Готовый ответ\n\n"
        "Если вы не получили ответ на ваш вопрос, "
        "вы можете обратиться к ИИ-помощнику."
    )
    assert deps.send_buttons.call_args.args[2][0][0]["payload"].startswith("faq_terminal_ai:")
    assert state["state"] == "menu"
    assert "script" not in state


def test_terminal_node_does_not_invite_disabled_ai():
    deps, state = _dependencies()
    object.__setattr__(deps, "ai_available", lambda: False)
    state.update({
        "state": "script_node",
        "script": {
            "nodes": {5: {"id": 5, "title": "Готовый ответ", "is_terminal": True}},
            "edges_by_from": {}, "current": 5,
        },
    })
    faq.show_script_node(42, deps)
    text, rows = deps.send_buttons.call_args.args[1:]
    assert "ИИ-помощнику" not in text
    assert all(not button["payload"].startswith("faq_terminal_ai:") for row in rows for button in row)


def test_terminal_node_hides_disabled_appeal_transition():
    deps, state = _dependencies()
    object.__setattr__(deps, "appeal_available", lambda: False)
    state.update({
        "state": "script_node",
        "script": {
            "nodes": {5: {"id": 5, "title": "Готовый ответ", "is_terminal": True}},
            "edges_by_from": {}, "current": 5,
        },
    })
    faq.show_script_node(42, deps)
    rows = deps.send_buttons.call_args.args[2]
    assert all(button["payload"] != "appeal_start" for row in rows for button in row)


def test_faq_node_renders_structured_site_link_without_markdown():
    deps, state = _dependencies()
    state["script"] = {
        "nodes": {5: {
            "id": 5, "title": "Подробнее [здесь](не-разметка)",
            "is_terminal": True, "link_url": "https://example.test/help",
            "link_text": "Открыть инструкцию",
        }},
        "edges_by_from": {}, "current": 5,
    }
    faq.show_script_node(42, deps)
    text, rows = deps.send_buttons.call_args.args[1:]
    assert "[здесь](не-разметка)" in text
    assert rows[0] == [{
        "type": "link", "text": "Открыть инструкцию",
        "url": "https://example.test/help",
    }]


def test_navigate_can_move_to_child_and_back_to_parent():
    deps, state = _dependencies()
    state["script"] = {
        "nodes": {
            1: {"id": 1, "title": "Родитель", "is_terminal": False},
            2: {"id": 2, "title": "Ребёнок", "is_terminal": False},
        },
        "edges_by_from": {
            1: [{"label": "Вперёд", "to_node_id": 2}],
            2: [{"label": "Назад", "to_node_id": 1}],
        },
        "current": 1,
    }

    faq.navigate_script_node(42, 2, deps)
    assert state["script"]["current"] == 2
    assert deps.send_buttons.call_args.args[1] == "📌 Ребёнок"

    faq.navigate_script_node(42, 1, deps)
    assert state["script"]["current"] == 1
    assert deps.send_buttons.call_args.args[1] == "📌 Родитель"


def test_faq_telemetry_tracks_start_views_and_final_without_content():
    tree = {
        "title": "Секретный текст",
        "nodes": [
            {"id": 1, "title": "Начало", "is_terminal": False},
            {"id": 2, "title": "Ответ с ПДн", "is_terminal": True},
        ],
        "edges": [{"from_node_id": 1, "to_node_id": 2, "label": "Далее"}],
    }
    deps, state = _dependencies(tree=(tree, None))
    recorder = Mock()
    object.__setattr__(deps, "record_event", recorder)
    faq.open_script(42, 9, deps)
    faq.navigate_script_node(42, 2, deps)
    event_names = [call.args[0] for call in recorder.call_args_list]
    assert event_names == ["faq_script_start", "faq_node_view", "faq_node_view", "faq_final"]
    assert all("Секрет" not in repr(call) and "ПДн" not in repr(call) for call in recorder.call_args_list)
    assert state["faq_terminal"]["script_id"] == 9


def test_node_edges_paginate_without_invalid_max_keyboard():
    deps, state = _dependencies()
    edges = [{"label": f"Вариант {index}", "to_node_id": index + 2} for index in range(35)]
    state["script"] = {
        "id": 7, "nodes": {1: {"id": 1, "title": "Выбор", "is_terminal": False}},
        "edges_by_from": {1: edges}, "current": 1, "render_token": "deadbeef",
    }
    state["state"] = "script_node"
    faq.show_script_node(42, deps)
    assert deps.send_buttons.call_count == 1
    assert len(deps.send_buttons.call_args.args[2]) == 30
    faq.show_node_page(42, 7, 1, "deadbeef", 1, deps)
    assert deps.send_buttons.call_count == 2
    assert len(deps.send_buttons.call_args.args[2]) == 8


def test_bound_faq_action_rejects_stale_and_non_edge_targets():
    deps, state = _dependencies()
    state.update({
        "state": "script_node",
        "script": {
            "id": 7,
            "render_token": "deadbeef",
            "nodes": {
                1: {"id": 1, "title": "Root", "is_terminal": False},
                2: {"id": 2, "title": "Child", "is_terminal": False},
                3: {"id": 3, "title": "Other", "is_terminal": False},
            },
            "edges_by_from": {
                1: [{"label": "Next", "to_node_id": 2}],
                2: [{"label": "Next again", "to_node_id": 3}],
            },
            "current": 1,
            "path": ["FAQ", "Root"],
        },
    })
    faq.navigate_bound_action(42, 7, 1, 3, "deadbeef", deps)
    assert state["script"]["current"] == 1
    faq.navigate_bound_action(42, 7, 1, 2, "cafebabe", deps)
    assert state["script"]["current"] == 1
    faq.navigate_bound_action(42, 7, 1, 2, "deadbeef", deps)
    assert state["script"]["current"] == 2
    new_token = state["script"]["render_token"]
    assert new_token != "deadbeef"
    faq.navigate_bound_action(42, 7, 1, 2, "deadbeef", deps)
    assert state["script"]["current"] == 2
    assert state["script"]["render_token"] == new_token


def test_interactive_pagination_over_sixty_rows_has_no_loss():
    scripts = [{"id": index, "title": f"Тема {index}"} for index in range(65)]
    deps, _ = _dependencies(scripts=(scripts, None))
    faq.show_scripts_list(42, deps)
    token = deps.get_state.return_value["faq_scripts_token"]
    faq.show_scripts_page(42, token, 1, deps)
    faq.show_scripts_page(42, token, 2, deps)
    assert deps.send_buttons.call_count == 3
    action_payloads = []
    for call in deps.send_buttons.call_args_list:
        keyboard = call.args[2]
        max_transport.validate_inline_keyboard(keyboard)
        action_payloads.extend(
            button["payload"] for row in keyboard for button in row
            if not button["payload"].startswith("faq_scripts_page:")
        )
    assert action_payloads == [f"script:{index}" for index in range(65)] + ["main_menu"]


def test_stale_or_invalid_faq_page_fails_safe():
    deps, state = _dependencies(scripts=([{"id": 1, "title": "A"}] * 61, None))
    faq.show_scripts_page(42, "stale", 1, deps)
    deps.send_main_menu.assert_called_once_with(42)
    state["state"] = "script_list"
    state["faq_scripts"] = [{"id": index, "title": f"A {index}"} for index in range(61)]
    state["faq_scripts_token"] = "deadbeef"
    faq.show_scripts_page(42, "deadbeef", 99, deps)
    assert deps.send_message.call_args.args == (42, "Неверная страница FAQ.")


@pytest.mark.parametrize("bad_token", ["юникод00", "a" * 4096, "ABCDEF12", "abc"])
def test_all_faq_callback_tokens_fail_closed_without_type_error(bad_token):
    deps, state = _dependencies(scripts=([{"id": 1, "title": "A"}], None))
    state.update({
        "state": "script_list", "faq_scripts": [{"id": 1, "title": "A"}],
        "faq_scripts_token": "deadbeef",
    })
    faq.show_scripts_page(42, bad_token, 0, deps)
    assert state["state"] == "menu"

    for action in ("page", "go"):
        state.update({
            "state": "script_node",
            "script": {
                "id": 7, "render_token": "deadbeef", "current": 1,
                "nodes": {
                    1: {"id": 1, "title": "Root", "is_terminal": False},
                    2: {"id": 2, "title": "Child", "is_terminal": True},
                },
                "edges_by_from": {1: [{"label": "Next", "to_node_id": 2}]},
            },
        })
        if action == "page":
            faq.show_node_page(42, 7, 1, bad_token, 0, deps)
        else:
            faq.navigate_bound_action(42, 7, 1, 2, bad_token, deps)
        assert state.get("script", {}).get("current") != 2


def test_invalid_legacy_faq_button_is_skipped_without_crash():
    deps, _ = _dependencies(
        scripts=([{"id": 1, "title": "X\n"}, {"id": 2, "title": "Valid"}], None),
    )
    deps = faq.FaqDependencies(
        **{
            **deps.__dict__,
            "make_callback": max_transport.make_callback_button,
        },
    )
    faq.show_scripts_list(42, deps)
    keyboard = deps.send_buttons.call_args.args[2]
    assert [row[0]["payload"] for row in keyboard] == ["script:2", "main_menu"]
    deps.logger.warning.assert_called_once()


def test_terminal_faq_preserves_traversed_path_for_ai():
    tree = {
        "title": "Оплата",
        "nodes": [
            {"id": 1, "title": "Выберите тему", "is_terminal": False},
            {"id": 2, "title": "Проверьте квитанцию", "is_terminal": True},
        ],
        "edges": [{"from_node_id": 1, "label": "Неверная сумма", "to_node_id": 2}],
    }
    deps, state = _dependencies(tree=(tree, None))
    faq.open_script(42, 7, deps)
    faq.navigate_script_node(42, 2, deps)
    assert state["ai_faq_context"] == (
        "Оплата → Выберите тему → Неверная сумма → Проверьте квитанцию"
    )
    assert deps.send_buttons.call_args.args[2][0][0]["payload"].startswith("faq_terminal_ai:")


def test_navigate_without_active_script_returns_to_menu():
    deps, _ = _dependencies()

    faq.navigate_script_node(42, 7, deps)

    deps.send_main_menu.assert_called_once_with(42)
    deps.touch.assert_not_called()


def test_missing_node_preserves_completion_response():
    deps, state = _dependencies()
    state["script"] = {"nodes": {}, "edges_by_from": {}, "current": 404}

    faq.show_script_node(42, deps)

    deps.send_message.assert_called_once_with(42, "Скрипт завершён.")
    deps.send_main_menu.assert_called_once_with(42)


def test_open_script_handles_load_failure_and_empty_tree():
    failed, _ = _dependencies(tree=(None, "timeout"))
    faq.open_script(42, 7, failed)
    failed.send_message.assert_called_once_with(42, "⚠️ Не удалось загрузить скрипт.")
    failed.send_main_menu.assert_called_once_with(42)

    empty, _ = _dependencies(tree=({"nodes": [], "edges": []}, None))
    faq.open_script(43, 8, empty)
    empty.send_message.assert_called_once_with(43, "Скрипт пуст.")
    empty.send_main_menu.assert_called_once_with(43)


def test_bot_wrappers_delegate_with_runtime_patch_points(monkeypatch):
    delegated = Mock()
    monkeypatch.setattr(bot.faq, "show_scripts_list", delegated)
    patched_sender = Mock()
    monkeypatch.setattr(bot, "send_message", patched_sender)

    bot._show_scripts_list(42)

    delegated.assert_called_once()
    chat_id, deps = delegated.call_args.args
    assert chat_id == 42
    assert deps.send_message is patched_sender
    assert deps.list_scripts is bot.client_api.list_scripts
    assert deps.get_state is bot._get_state


def test_all_bot_faq_wrappers_delegate(monkeypatch):
    open_script = Mock()
    show_node = Mock()
    navigate = Mock()
    monkeypatch.setattr(bot.faq, "open_script", open_script)
    monkeypatch.setattr(bot.faq, "show_script_node", show_node)
    monkeypatch.setattr(bot.faq, "navigate_script_node", navigate)

    bot._open_script(42, 7)
    bot._show_script_node(42)
    bot._navigate_script_node(42, 8)

    assert open_script.call_args.args[:2] == (42, 7)
    assert show_node.call_args.args[:1] == (42,)
    assert navigate.call_args.args[:2] == (42, 8)


def test_disabled_faq_rejects_pagination_callback(monkeypatch):
    monkeypatch.setattr(bot, "_ack_callback", Mock())
    monkeypatch.setattr(bot.operator_chat, "module_enabled", lambda _key: False)
    menu = Mock()
    monkeypatch.setattr(bot, "send_main_menu", menu)
    bot.handle_callback({
        "message": {"recipient": {"chat_id": 987654}},
        "callback": {"callback_id": "cb", "payload": "faq_scripts_page:token:1"},
    })
    menu.assert_called_once_with(987654, "Раздел временно недоступен.")


def test_main_menu_with_all_modules_disabled_is_plain_text(monkeypatch):
    monkeypatch.setattr(bot.operator_chat, "get_module_settings", lambda: {})
    monkeypatch.setattr(bot, "_get_saved_ls", lambda _chat_id: None)
    text_sender = Mock(return_value=True)
    buttons_sender = Mock()
    monkeypatch.setattr(bot, "send_message", text_sender)
    monkeypatch.setattr(bot, "send_buttons", buttons_sender)
    assert bot.send_main_menu(12, "Нет доступных разделов") is True
    text_sender.assert_called_once_with(12, "Нет доступных разделов")
    buttons_sender.assert_not_called()


def _bot_callback(payload: str) -> dict:
    return {
        "message": {"recipient": {"chat_id": 42}},
        "callback": {"callback_id": "cb", "payload": payload},
    }


def _terminal_state() -> tuple[dict, str]:
    run_id = "a" * 24
    token = "b" * 8
    state = {
        "state": bot.S.MENU,
        "ai_faq_context": "Сценарий",
        "faq_terminal": {
            "script_id": 7, "node_id": 9, "run_id": run_id, "token": token,
        },
    }
    return state, f"7:9:{run_id}:{token}"


def test_terminal_ai_requires_current_bound_context_and_deduplicates(monkeypatch):
    state, argument = _terminal_state()
    events = Mock()
    starter = Mock(side_effect=lambda _chat, _context: state.update(state=bot.S.AI_QUESTION))
    monkeypatch.setattr(bot, "_ack_callback", Mock())
    monkeypatch.setattr(bot, "_reconcile_operator_terminal", lambda _chat: False)
    monkeypatch.setattr(bot, "_get_state", lambda _chat: state)
    monkeypatch.setattr(bot, "_start_ai", starter)
    monkeypatch.setattr(bot.analytics, "record_event", events)
    monkeypatch.setattr(bot.operator_chat, "module_enabled", lambda _key: True)
    monkeypatch.setattr(bot, "send_message", Mock())

    bot.handle_callback(_bot_callback(f"faq_terminal_ai:{argument}"))
    bot.handle_callback(_bot_callback(f"faq_terminal_ai:{argument}"))

    starter.assert_called_once_with(42, "Сценарий")
    events.assert_called_once_with(
        "faq_transition_ai", script_id=7, node_id=9,
        dedupe_key=f"faq:{'a' * 24}:transition:ai",
    )


def test_stale_and_crafted_terminal_ai_never_start_or_log(monkeypatch):
    state, argument = _terminal_state()
    state["faq_terminal"]["token"] = "c" * 8
    starter = Mock()
    appeal_starter = Mock()
    operator_starter = Mock()
    events = Mock()
    monkeypatch.setattr(bot, "_ack_callback", Mock())
    monkeypatch.setattr(bot, "_reconcile_operator_terminal", lambda _chat: False)
    monkeypatch.setattr(bot, "_get_state", lambda _chat: state)
    monkeypatch.setattr(bot, "_start_ai", starter)
    monkeypatch.setattr(bot, "_start_appeal", appeal_starter)
    monkeypatch.setattr(bot, "_start_operator_chat", operator_starter)
    monkeypatch.setattr(bot.analytics, "record_event", events)
    monkeypatch.setattr(bot.operator_chat, "module_enabled", lambda _key: True)
    monkeypatch.setattr(bot, "send_message", Mock())

    bot.handle_callback(_bot_callback(f"faq_terminal_ai:{argument}"))
    bot.handle_callback(_bot_callback("ai_from_faq"))
    bot.handle_callback(_bot_callback("appeal_start"))
    bot.handle_callback(_bot_callback("operator_start"))

    starter.assert_not_called()
    appeal_starter.assert_not_called()
    operator_starter.assert_not_called()
    events.assert_not_called()


def test_terminal_ai_failure_keeps_context_and_does_not_log(monkeypatch):
    state, argument = _terminal_state()
    events = Mock()
    monkeypatch.setattr(bot, "_ack_callback", Mock())
    monkeypatch.setattr(bot, "_reconcile_operator_terminal", lambda _chat: False)
    monkeypatch.setattr(bot, "_get_state", lambda _chat: state)
    monkeypatch.setattr(bot, "_start_ai", Mock())
    monkeypatch.setattr(bot.analytics, "record_event", events)
    monkeypatch.setattr(bot.operator_chat, "module_enabled", lambda _key: True)

    bot.handle_callback(_bot_callback(f"faq_terminal_ai:{argument}"))

    assert state["faq_terminal"]["script_id"] == 7
    events.assert_not_called()


def test_faq_exit_is_recorded_only_after_successful_real_transition(monkeypatch):
    state = {
        "state": bot.S.SCRIPT_NODE,
        "script": {"id": 3, "current": 4, "run_id": "d" * 24},
    }
    events = Mock()
    monkeypatch.setattr(bot, "_ack_callback", Mock())
    monkeypatch.setattr(bot, "_reconcile_operator_terminal", lambda _chat: False)
    monkeypatch.setattr(bot, "_get_state", lambda _chat: state)
    monkeypatch.setattr(bot.analytics, "record_event", events)
    monkeypatch.setattr(bot.operator_chat, "module_enabled", lambda _key: True)
    monkeypatch.setattr(bot, "send_main_menu", Mock())
    monkeypatch.setattr(bot, "_rate_operator", Mock())

    bot.handle_callback(_bot_callback("unknown:crafted"))
    bot.handle_callback(_bot_callback("operator_rate:1-5"))
    bot.handle_callback(_bot_callback("script:not-an-int"))
    events.assert_not_called()

    bot.handle_callback(_bot_callback("main_menu"))
    events.assert_called_once_with(
        "faq_exit", script_id=3, node_id=4, metadata={"reason": "main_menu"},
        dedupe_key=f"faq:{'d' * 24}:exit:main_menu",
    )


def test_opening_new_faq_run_exits_previous_run_only_after_success(monkeypatch):
    old_run = "e" * 24
    state = {
        "state": bot.S.SCRIPT_NODE,
        "script": {"id": 1, "current": 11, "run_id": old_run},
    }
    events = Mock()
    tree = {
        "title": "Новый FAQ",
        "nodes": [
            {"id": 20, "title": "Начало", "is_terminal": False},
            {"id": 21, "title": "Ответ", "is_terminal": True},
        ],
        "edges": [{"from_node_id": 20, "to_node_id": 21, "label": "Далее"}],
    }
    monkeypatch.setattr(bot, "_ack_callback", Mock())
    monkeypatch.setattr(bot, "_reconcile_operator_terminal", lambda _chat: False)
    monkeypatch.setattr(bot, "_get_state", lambda _chat: state)
    monkeypatch.setattr(bot.client_api, "get_script_tree", lambda _script_id: (tree, None))
    monkeypatch.setattr(bot.analytics, "record_event", events)
    monkeypatch.setattr(bot.operator_chat, "module_enabled", lambda _key: True)
    monkeypatch.setattr(bot, "send_buttons", Mock())

    bot.handle_callback(_bot_callback("script:2"))

    assert state["script"]["id"] == 2
    assert state["script"]["run_id"] != old_run
    assert [call.args[0] for call in events.call_args_list] == [
        "faq_script_start", "faq_node_view", "faq_exit",
    ]
    assert events.call_args_list[-1].kwargs == {
        "script_id": 1, "node_id": 11, "metadata": {"reason": "new_flow"},
        "dedupe_key": f"faq:{old_run}:exit:new_flow",
    }


def test_failed_new_faq_run_does_not_exit_current_run(monkeypatch):
    state = {
        "state": bot.S.SCRIPT_NODE,
        "script": {"id": 1, "current": 11, "run_id": "f" * 24},
    }
    events = Mock()
    monkeypatch.setattr(bot, "_ack_callback", Mock())
    monkeypatch.setattr(bot, "_reconcile_operator_terminal", lambda _chat: False)
    monkeypatch.setattr(bot, "_get_state", lambda _chat: state)
    monkeypatch.setattr(
        bot.client_api, "get_script_tree", lambda _script_id: (None, "unavailable"),
    )
    monkeypatch.setattr(bot.analytics, "record_event", events)
    monkeypatch.setattr(bot.operator_chat, "module_enabled", lambda _key: True)
    monkeypatch.setattr(bot, "send_message", Mock())
    monkeypatch.setattr(bot, "send_main_menu", Mock())

    bot.handle_callback(_bot_callback("script:2"))

    assert state["script"]["run_id"] == "f" * 24
    events.assert_not_called()
