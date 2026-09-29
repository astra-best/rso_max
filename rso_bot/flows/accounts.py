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


def show(chat_id: int, deps: AccountManagementDependencies) -> None:
    bindings = deps.list_bindings(chat_id)
    if not bindings:
        deps.start_auth(chat_id, "accounts")
        return
    lines = ["👤 Мои лицевые счета:"]
    for row in bindings:
        ls = str(row["ls"])
        details = " — ".join(value for value in (row["fio"], row["address"]) if value)
        lines.append(f"• {ls}" + (f" — {details}" if details else ""))
    deps.send_buttons(
        chat_id,
        "\n".join(lines),
        [
            [deps.make_callback("➕ Добавить лицевой счёт", "account_add")],
            [deps.make_callback("🏠 Главное меню", "main_menu")],
        ],
    )


def add(chat_id: int, deps: AccountManagementDependencies) -> None:
    deps.start_auth(chat_id, "accounts")
