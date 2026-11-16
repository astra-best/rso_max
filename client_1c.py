"""Синхронный HTTPS-клиент FastAPI → 1С с локальным mock-режимом."""
from __future__ import annotations

import logging
import json
import threading
import uuid
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import httpx

from config import (
    INTEGRATION_1C_AUTH_TIMEOUT_SECONDS,
    INTEGRATION_1C_AUTH_TOKEN,
    INTEGRATION_1C_BASE_URL,
    INTEGRATION_1C_CODE_MAX_ATTEMPTS,
    INTEGRATION_1C_CODE_TTL_MINUTES,
    INTEGRATION_1C_MOCK,
    INTEGRATION_1C_MOCK_CODE,
    INTEGRATION_1C_SYNC_TIMEOUT_SECONDS,
)

log = logging.getLogger("rso.client_1c")


def _join_url(base_url: str, path: str) -> str:
    return f"{base_url.rstrip('/')}/{path.lstrip('/')}"


class Mock1CBackend:
    """Детерминированная имитация 1С для разработки без опубликованных методов."""

    def __init__(
        self,
        *,
        now: Callable[[], datetime] | None = None,
        code: str = "000000",
        ttl_minutes: int = 10,
        max_attempts: int = 5,
    ) -> None:
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._code = code
        self._ttl = timedelta(minutes=ttl_minutes)
        self._max_attempts = max_attempts
        self._pending: dict[tuple[str, int], dict] = {}
        self._task_statuses: dict[str, dict] = {}
        self._lock = threading.Lock()

    def request_auth_code(self, ls: str, chat_id: int) -> dict:
        with self._lock:
            self._pending[(ls, chat_id)] = {
                "created_at": self._now(),
                "attempts": 0,
                "blocked": False,
            }
        return {"status": "ok", "message": "Код отправлен на электронную почту"}

    def verify_auth_code(self, ls: str, chat_id: int, code: str) -> dict:
        key = (ls, chat_id)
        with self._lock:
            pending = self._pending.get(key)
            if pending is None or self._now() - pending["created_at"] > self._ttl:
                return {"status": "expired_code", "message": "Срок действия кода истёк"}
            if pending["blocked"]:
                return {"status": "attempts_exceeded", "message": "Число попыток исчерпано"}
            if code != self._code:
                pending["attempts"] += 1
                if pending["attempts"] >= self._max_attempts:
                    pending["blocked"] = True
                    return {"status": "attempts_exceeded", "message": "Число попыток исчерпано"}
                return {"status": "wrong_code", "message": "Код неверный"}

            return {
                "status": "ok",
                "message": "Авторизация выполнена",
                "meters": [
                    {
                        "meter_number": "MOCK-001",
                        "resource_type": "Электроэнергия",
                        "meter_type": "Однотарифный",
                    },
                    {
                        "meter_number": "MOCK-002",
                        "resource_type": "Электроэнергия",
                        "meter_type": "Двухтарифный",
                    },
                ],
            }

    def send_tasks(self, tasks: list[dict]) -> dict:
        with self._lock:
            for task in tasks:
                self._task_statuses.setdefault(
                    task["TaskID"], {"TaskID": task["TaskID"], "Status": "PENDING", "message": ""}
                )
        return {"status": "ok", "message": ""}

    def sync_task_statuses(self, task_ids: list[str]) -> list[dict]:
        with self._lock:
            return [
                self._task_statuses.get(
                    task_id, {"TaskID": task_id, "Status": "ERROR", "message": "Task not found"}
                )
                for task_id in task_ids
            ]


_mock_backend = Mock1CBackend(
    code=INTEGRATION_1C_MOCK_CODE,
    ttl_minutes=INTEGRATION_1C_CODE_TTL_MINUTES,
    max_attempts=INTEGRATION_1C_CODE_MAX_ATTEMPTS,
)


def _encode_json(body) -> bytes:
    """Encode Decimal values as exact unquoted JSON numbers."""
    decimals: dict[str, str] = {}
    nonce = uuid.uuid4().hex

    def replace(value):
        if isinstance(value, Decimal):
            marker = f"__JSON_DECIMAL_{nonce}_{len(decimals)}__"
            decimals[marker] = format(value, "f")
            return marker
        if isinstance(value, dict):
            return {key: replace(item) for key, item in value.items()}
        if isinstance(value, list):
            return [replace(item) for item in value]
        return value

    encoded = json.dumps(replace(body), ensure_ascii=False)
    for marker, number in decimals.items():
        encoded = encoded.replace(json.dumps(marker), number)
    return encoded.encode("utf-8")


def _post_json(path: str, body, timeout: int) -> tuple[dict | list | None, str | None]:
    if not INTEGRATION_1C_BASE_URL or not INTEGRATION_1C_AUTH_TOKEN:
        log.error("1С-запрос %s не выполнен: отсутствует обязательная конфигурация", path)
        return None, "configuration_error"

    try:
        headers = {"Authorization": f"Bearer {INTEGRATION_1C_AUTH_TOKEN}"}
        if _contains_decimal(body):
            headers["Content-Type"] = "application/json"
            response = httpx.post(
                _join_url(INTEGRATION_1C_BASE_URL, path), headers=headers,
                content=_encode_json(body), timeout=timeout,
            )
        else:
            response = httpx.post(
                _join_url(INTEGRATION_1C_BASE_URL, path), headers=headers,
                json=body, timeout=timeout,
            )
    except httpx.TimeoutException:
        log.warning("1С-запрос %s: timeout", path)
        return None, "timeout"
    except Exception as exc:
        # Текст исключения может содержать URL или секреты из стороннего кода.
        log.error("1С-запрос %s: ошибка соединения (%s)", path, type(exc).__name__)
        return None, "connection_error"

    if response.status_code == 401:
        log.error("1С-запрос %s: авторизация отклонена", path)
        return None, "unauthorized"
    if response.status_code in (408, 504):
        log.warning("1С-запрос %s: timeout HTTP %s", path, response.status_code)
        return None, "timeout"
    if response.status_code != 200:
        log.error("1С-запрос %s: HTTP %s", path, response.status_code)
        return None, "http_error"

    try:
        data = response.json()
    except ValueError:
        log.error("1С-запрос %s: ответ не является JSON", path)
        return None, "invalid_response"
    if not isinstance(data, (dict, list)):
        log.error("1С-запрос %s: JSON верхнего уровня не объект", path)
        return None, "invalid_response"
    return data, None


def _contains_decimal(value) -> bool:
    if isinstance(value, Decimal):
        return True
    if isinstance(value, dict):
        return any(_contains_decimal(item) for item in value.values())
    if isinstance(value, list):
        return any(_contains_decimal(item) for item in value)
    return False


def request_auth_code(ls: str, chat_id: int) -> tuple[dict | None, str | None]:
    if INTEGRATION_1C_MOCK:
        return _mock_backend.request_auth_code(ls, chat_id), None
    return _post_json(
        "/auth/request-code",
        {"ls": ls, "chat_id": chat_id},
        INTEGRATION_1C_AUTH_TIMEOUT_SECONDS,
    )


def verify_auth_code(ls: str, chat_id: int, code: str) -> tuple[dict | None, str | None]:
    if INTEGRATION_1C_MOCK:
        return _mock_backend.verify_auth_code(ls, chat_id, code), None
    return _post_json(
        "/auth/verify-code",
        {"ls": ls, "chat_id": chat_id, "code": code},
        INTEGRATION_1C_AUTH_TIMEOUT_SECONDS,
    )


def send_tasks(tasks: list[dict]) -> tuple[dict | None, str | None]:
    if INTEGRATION_1C_MOCK:
        return _mock_backend.send_tasks(tasks), None
    return _post_json("/RegistrationMetersValue", tasks, INTEGRATION_1C_SYNC_TIMEOUT_SECONDS)


def sync_task_statuses(task_ids: list[str]) -> tuple[list | None, str | None]:
    if INTEGRATION_1C_MOCK:
        return _mock_backend.sync_task_statuses(task_ids), None
    return _post_json(
        "/MetersValueStatus", [{"TaskID": task_id} for task_id in task_ids],
        INTEGRATION_1C_SYNC_TIMEOUT_SECONDS,
    )
