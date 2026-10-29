"""Общие правила значений показаний: ввод в боте и поле Reading выгрузки 1С.

Модуль без зависимостей от бота и БД, чтобы одни и те же ограничения
применялись и к введённому пользователем тексту, и к числу из JSON 1С.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation

READING_MAX_INTEGER_DIGITS = 15
READING_MAX_FRACTION_DIGITS = 6

# Результаты сохранения показаний (database.save_meter_reading).
SAVE_OK = "saved"
SAVE_METER_CHANGED = "meter_changed"
SAVE_TOO_LOW = "too_low"


def reading_error(value: Decimal) -> str | None:
    """Причина отказа для значения показания или None, если оно допустимо.

    Цифры считаются по записи значения: ``1.5000000`` содержит 7 знаков после
    запятой и отклоняется так же, как такой же текст в боте.
    """
    if not value.is_finite():
        return "invalid"
    sign, digits, exponent = value.as_tuple()
    if sign:
        return "negative"
    if exponent < 0 and -exponent > READING_MAX_FRACTION_DIGITS:
        return "fraction"
    if len(digits) + exponent > READING_MAX_INTEGER_DIGITS:
        return "integer"
    return None


def format_reading(value: Decimal) -> str:
    """Нормализованная запись: без лишних нулей и экспоненты (``123.50`` → ``123.5``)."""
    return format(value.normalize(), "f")


def json_reading(raw) -> str | None:
    """Нормализовать число из JSON 1С; None для любого недопустимого значения.

    Принимаются только числа JSON в том виде, как их разбирает импорт
    (``json.loads(..., parse_float=Decimal, parse_constant=Decimal)``):
    int или Decimal. bool, строки, float и прочие типы отклоняются — float
    уже потерял точность длинного числа, поэтому не угадывается.
    Незначащие нули в записи 1С не считаются: ``50.0000000`` — это ``50``.
    """
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        value = Decimal(raw)
    elif isinstance(raw, Decimal):
        value = raw.normalize() if raw.is_finite() else raw
    else:
        return None
    if reading_error(value) is not None:
        return None
    return format_reading(value)


def reading_decimal(value: str | None) -> Decimal | None:
    """Decimal из сохранённого нормализованного значения; None, если его нет."""
    if value is None or value == "":
        return None
    try:
        result = Decimal(value)
    except InvalidOperation:
        return None
    return result if result.is_finite() else None


def max_reading(*values: str | None) -> str | None:
    """Наибольшее из сохранённых значений (сравнение как Decimal) или None."""
    decimals = [d for d in (reading_decimal(v) for v in values) if d is not None]
    if not decimals:
        return None
    return format_reading(max(decimals))
