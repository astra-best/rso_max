"""Customer-facing management of durable MAX/account bindings."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class AccountManagementDependencies:
    list_bindings: Callable[[int], list[Any]]
    make_callback: Callable[[str, str], dict[str, Any]]
    send_buttons: Callable[[int, str, list[list[dict[str, Any]]]], Any]
    start_auth: Callable[[int, str | None], None]
    # Self-service unlink (scenario «Сменился лицевой счёт»). Optional so that
    # older embedders without it keep the read-only list.
    unlink_binding: Callable[[int, str], bool] | None = None
    send_main_menu: Callable[..., Any] | None = None


def show(chat_id: int, deps: AccountManagementDependencies) -> None:
    bindings = deps.list_bindings(chat_id)
    if not bindings:
        deps.start_auth(chat_id, "accounts")
        return
    lines = ["👤 Мои лицевые счета:"]
    for row in bindings:
        ls = str(row["ls"])
        # ФИО хранится для внутренних административных и операторских
        # сценариев, но не должно возвращаться клиенту в MAX.  Даже если
        # list_bindings() по-прежнему отдаёт поле fio, пользовательское
        # представление намеренно использует только ЛС и адрес.
        address = row["address"]
        lines.append(f"• {ls}" + (f" — {address}" if address else ""))
    rows = [[deps.make_callback("➕ Добавить лицевой счёт", "account_add")]]
    if deps.unlink_binding is not None:
        rows += [
            [deps.make_callback(f"➖ Отвязать {row['ls']}", f"account_unlink:{row['ls']}")]
            for row in bindings
        ]
    rows.append([deps.make_callback("🏠 Главное меню", "main_menu")])
    deps.send_buttons(chat_id, "\n".join(lines), rows)


def confirm_unlink(chat_id: int, ls: str, deps: AccountManagementDependencies) -> None:
    """Ask before removing a binding: the resident may lose access to the account."""
    bound = {str(row["ls"]) for row in deps.list_bindings(chat_id)}
    if ls not in bound:
        show(chat_id, deps)
        return
    deps.send_buttons(
        chat_id,
        f"Отвязать лицевой счёт {ls} от этого чата?\n"
        "Если лицевой счёт сменился, сначала добавьте новый, затем отвяжите старый.",
        [
            [deps.make_callback("✅ Да, отвязать", f"account_unlink_yes:{ls}")],
            [deps.make_callback("❌ Отмена", "accounts_list")],
        ],
    )


def unlink(chat_id: int, ls: str, deps: AccountManagementDependencies) -> None:
    if deps.unlink_binding is None:
        show(chat_id, deps)
        return
    removed = deps.unlink_binding(chat_id, ls)
    remaining = deps.list_bindings(chat_id)
    if remaining:
        show(chat_id, deps)
        return
    text = (
        f"Лицевой счёт {ls} отвязан. Привязанных лицевых счетов больше нет — "
        "чтобы продолжить, авторизуйтесь заново."
        if removed else "Привязанных лицевых счетов нет."
    )
    if deps.send_main_menu is not None:
        deps.send_main_menu(chat_id, text)


def add(chat_id: int, deps: AccountManagementDependencies) -> None:
    deps.start_auth(chat_id, "accounts")
