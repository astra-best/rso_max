# API интеграции с 1С

Документ описывает текущий контракт обмена между MAX-ботом, внутренним FastAPI и опубликованным HTTP-сервисом 1С.

## 1. Общая схема

```text
MAX-бот -> внутренний FastAPI -> HTTPS-сервис 1С
```

Бот не обращается к 1С напрямую:

1. Бот вызывает внутренние методы FastAPI для авторизации пользователя.
2. FastAPI вызывает опубликованные методы 1С.
3. Фоновая задача FastAPI передаёт в 1С накопленные показания.
4. FastAPI persists task statuses in SQLite; the portal displays the current processing status.

Внутреннее и внешнее API используют разные Bearer-токены:

- `INTERNAL_API_TOKEN` защищает соединение MAX-бота с FastAPI;
- `INTEGRATION_1C_AUTH_TOKEN` защищает соединение FastAPI с 1С.

Реальные токены нельзя записывать в документацию, журналы и `.env.example`.

## 2. Общие требования к HTTP-сервису 1С

- Протокол: HTTPS.
- Формат запросов и ответов: JSON в UTF-8.
- Заголовок запроса: `Content-Type: application/json`.
- Авторизация: `Authorization: Bearer <INTEGRATION_1C_AUTH_TOKEN>`.
- Бизнес-результат возвращается с HTTP `200` и полем `status` в JSON.
- При неверном Bearer-токене 1С должна вернуть HTTP `401` или `403`.
- Ответ авторизационных методов должен быть получен не позднее чем через 5 секунд.
- Допустимое время синхронизации показаний на стороне Python: 60 секунд.

Базовый адрес сервиса хранится в `INTEGRATION_1C_BASE_URL`. Python добавляет к нему один из путей:

```text
/auth/request-code
/auth/verify-code
/sent
/sync
```

Например, если базовый адрес заканчивается на `/hs/integration`, итоговый адрес первого метода будет заканчиваться на `/hs/integration/auth/request-code`.

## 3. Внешнее API: FastAPI вызывает 1С

Эти три метода должны быть реализованы в опубликованном HTTP-сервисе 1С.

### 3.1 Запрос кода авторизации

```text
POST /auth/request-code
```

Назначение: проверить ЛС, сгенерировать одноразовый код и отправить его на электронную почту клиента.

Запрос:

```json
{
  "ls": "100001",
  "chat_id": 123456789
}
```

Поля:

- `ls` — номер лицевого счёта, строка длиной от 1 до 64 символов;
- `chat_id` — идентификатор пользователя MAX (`MaxUserID`), целое число.

Успешный ответ:

```json
{
  "status": "sent",
  "message": "Код отправлен на электронную почту"
}
```

ЛС не найден:

```json
{
  "status": "ls_not_found",
  "message": "Лицевой счёт не найден"
}
```

У ЛС нет электронного адреса:

```json
{
  "status": "no_contact",
  "message": "Для лицевого счёта не указан e-mail"
}
```

Допустимые значения `status`:

- `sent`;
- `ls_not_found`;
- `no_contact`.

1С не должна возвращать электронный адрес клиента или сам код.

### 3.2 Проверка кода авторизации

```text
POST /auth/verify-code
```

Назначение: проверить одноразовый код, сохранить привязку пользователя MAX к ЛС и вернуть активные счётчики.

Запрос:

```json
{
  "ls": "100001",
  "chat_id": 123456789,
  "code": "482910"
}
```

Поля:

- `ls` — тот же ЛС, для которого был запрошен код;
- `chat_id` — тот же идентификатор пользователя MAX;
- `code` — введённый пользователем код, строка длиной от 1 до 32 символов.

Правила кода на стороне 1С:

- срок действия — 10 минут;
- максимум 5 неверных попыток;
- код нельзя возвращать в ответе или писать в журнал;
- при успешной проверке 1С сохраняет связку `chat_id` и `ls`.

Успешный ответ:

```json
{
  "status": "ok",
  "message": "Авторизация выполнена",
  "meters": [
    {
      "meter_number": "METER-001",
      "resource_type": "Электроэнергия",
      "meter_type": "Однотарифный"
    },
    {
      "meter_number": "METER-002",
      "resource_type": "Электроэнергия",
      "meter_type": "Двухтарифный"
    }
  ]
}
```

Ошибочные бизнес-результаты:

```json
{
  "status": "wrong_code",
  "message": "Код неверный"
}
```

```json
{
  "status": "expired_code",
  "message": "Срок действия кода истёк"
}
```

```json
{
  "status": "attempts_exceeded",
  "message": "Число попыток исчерпано"
}
```

Допустимые значения `status`:

- `ok`;
- `wrong_code`;
- `expired_code`;
- `attempts_exceeded`.

Каждый объект `meters` содержит:

- `meter_number` — непустой уникальный номер счётчика в пределах ЛС;
- `resource_type` — вид ресурса;
- `meter_type` — строго `Однотарифный` или `Двухтарифный`.

Начальные показания в этом ответе не передаются. Python создаёт новые счётчики с техническими значениями `initial1=0` и `initial2=0`; при включённой интеграции они не используются для локальной проверки показаний.

### 3.3 Sending readings

```text
POST /sent
```

Request contains up to 100 tasks in `Tasks`. One task is one meter reading for one personal account. Bot creates `TaskID` when saving the reading and reuses it on network retry. Nesting: `MeteringPoints` -> `Meters` -> `Scales` -> `TariffZones`. `Value` is a JSON number with up to 15 integer digits and 6 fractional digits. `Date` format is `DD.MM.YYYY HH:MM:SS`, without timezone.

```json
{
  "Tasks": [{
    "TaskID": "e8bb6d5e-2022-45e3-ae57-055dc46df68b",
    "PersonalAccountID": "20c484a6-6045-11ec-83a6-d41b81e4026e",
    "RoomKeyID": "00000000-0000-0000-0000-000000000000",
    "Date": "30.10.2026 20:07:07",
    "MeteringPoints": [{
      "MeteringPointID": "20c484a7-6045-11ec-83a6-d41b81e4026e",
      "Meters": [{
        "MeteringDeviceID": "20c484a8-6045-11ec-83a6-d41b81e4026e",
        "Scales": [{
          "ScaleID": "ff0a78b9-9ae3-11ea-9612-00155d016301",
          "TariffZones": [
            {"TariffZoneID": "ab89b37f-94e4-11ea-960f-00155d016301", "Value": 21852},
            {"TariffZoneID": "ab89b380-94e4-11ea-960f-00155d016301", "Value": 8492}
          ]
        }]
      }]
    }]
  }]
}
```

Whole-batch acceptance: `{"status":"ok","message":""}`. Whole-batch rejection: `{"status":"error","message":"error text"}`. On `ok`, each task is recorded as `PENDING`. Resending the same `TaskID` must be idempotent so a retry after timeout cannot create a duplicate.

### 3.4 Polling task statuses

```text
POST /sync
```

Request contains only IDs of locally `PENDING` tasks:

```json
{"Tasks":[{"TaskID":"e8bb6d5e-2022-45e3-ae57-055dc46df68b"}]}
```

1C returns a status for every requested task. An unknown `TaskID` is returned as `ERROR`. Allowed statuses: `PENDING`, `SUCCESS`, `ERROR`; `message` contains error details for `ERROR` and is empty otherwise. Polling interval defaults to one hour and is configurable through `INTEGRATION_1C_SYNC_INTERVAL_MINUTES`. `SUCCESS` and `ERROR` are terminal.

```json
{"Tasks":[{"TaskID":"e8bb6d5e-2022-45e3-ae57-055dc46df68b","Status":"PENDING","message":""}]}
```

Both sending and polling are limited by `INTEGRATION_1C_BATCH_SIZE` (default 100 tasks).

### 3.5 Предыдущие показания в выгрузке лицевых счетов

В JSON-выгрузке лицевых счетов каждая тарифная зона может содержать поле `Reading` — последнее показание по зоне, известное 1С:

```json
{
  "TariffZoneID": "fb6e02a6-4442-11eb-86b5-7c2a31835b20",
  "TariffZone": "День",
  "Reading": 50
}
```

- `Reading` — JSON-число (не строка и не `true`/`false`), неотрицательное, не более 15 цифр до запятой и 6 после. Число сохраняется точно, без округления через число с плавающей точкой.
- Отсутствующее поле или `null` означает, что показания нет.
- Некорректное значение не записывается; зона, прибор и лицевой счёт при этом импортируются, в журнал пишется предупреждение без самого значения.
- При повторной выгрузке значение обновляется.
- GUID в выгрузке сохраняются в нижнем регистре: один GUID в разном регистре считается одним объектом.

Бот показывает пользователю предыдущее показание и не принимает значение меньше него. Предыдущим считается наибольшее из двух значений: `Reading` из выгрузки и последнее показание, введённое через бот (кроме отклонённых 1С).

## 4. Внутреннее API: MAX-бот вызывает FastAPI

Базовый адрес по умолчанию:

```text
http://127.0.0.1:8000
```

Оба метода защищены заголовком:

```text
Authorization: Bearer <INTERNAL_API_TOKEN>
```

### 4.1 Запрос кода

```text
POST /api/v1/integrations/1c/auth/request-code
```

Тело и бизнес-ответы совпадают с внешним методом `/auth/request-code`.

Пример PowerShell с шаблонным токеном:

```powershell
$headers = @{ Authorization = "Bearer <INTERNAL_API_TOKEN>" }
$body = @{
    ls = "100001"
    chat_id = 123456789
} | ConvertTo-Json

Invoke-RestMethod `
    -Method Post `
    -Uri "http://127.0.0.1:8000/api/v1/integrations/1c/auth/request-code" `
    -Headers $headers `
    -ContentType "application/json" `
    -Body $body
```

### 4.2 Проверка кода

```text
POST /api/v1/integrations/1c/auth/verify-code
```

Тело и бизнес-ответы совпадают с внешним методом `/auth/verify-code`.

При `status=ok` FastAPI:

1. Сохраняет привязку `chat_id` к `ls`.
2. Устанавливает `authorized_1c=1`.
3. Добавляет или обновляет счётчики по паре `ls` и `meter_number`.
4. Возвращает ответ боту.

### 4.3 HTTP-ошибки внутреннего API

| HTTP | Значение |
|---:|---|
| `200` | Запрос обработан; бизнес-результат находится в поле `status` |
| `401` | Неверный или отсутствующий `INTERNAL_API_TOKEN` |
| `422` | Некорректное тело запроса |
| `502` | Ошибка соединения, неверный HTTP-ответ, неизвестный статус или невалидный JSON от 1С |
| `503` | Не заданы обязательные настройки реального подключения к 1С |
| `504` | Тайм-аут запроса к 1С |

There is no internal HTTP route for 1C exchange. The FastAPI background job calls `client_1c.send_tasks()` and `client_1c.sync_task_statuses()` directly.

Интерактивная документация внутреннего API доступна после запуска FastAPI:

```text
http://127.0.0.1:8000/api/docs
```

## 5. Mock-режим

Mock включается настройками:

```env
ENABLE_1C_INTEGRATION=true
INTEGRATION_1C_MOCK=true
INTEGRATION_1C_MOCK_CODE=000000
```

Поведение mock:

- принимает любой непустой ЛС;
- не отправляет реальное письмо;
- использует код `000000`, если значение не переопределено;
- код действует 10 минут;
- после 5 неверных попыток возвращает `attempts_exceeded`;
- возвращает счётчики `MOCK-001` и `MOCK-002`;
- принимает все показания;
- хранит коды и результаты пакетов в памяти FastAPI;
- после перезапуска FastAPI временное состояние mock теряется.

## 6. Переход на реальную 1С

После реализации трёх методов в 1С:

1. Записать базовый HTTPS-адрес публикации в `INTEGRATION_1C_BASE_URL`.
2. Записать один и тот же случайный секрет в константу 1С и `INTEGRATION_1C_AUTH_TOKEN`.
3. Установить `INTEGRATION_1C_MOCK=false`.
4. Перезапустить FastAPI и MAX-бот.
5. Проверить запрос кода, проверку кода и получение счётчиков.
/sync`.
7. Убедиться, что коды, токены, e-mail и полные тела запросов не попадают в журналы.

Если локальные запросы бота к FastAPI перехватывает VPN или системный прокси, добавить в `.env`:

```env
NO_PROXY=127.0.0.1,localhost
```

## 7. Связанные файлы

- `client_1c.py` — клиент внешнего API 1С и mock-реализация;
- `api/routers/integration_1c.py` — внутреннее API авторизации;
- `api/schemas.py` — проверка входных данных;
- `client_api.py` — вызовы FastAPI из MAX-бота;
- `sync_1c.py` — пакетная синхронизация;
- `database.py` — хранение привязок, очереди, статусов и счётчиков;
- `TOR_1C_INTEGRATION_PART1_AUTH (2).md` — требования к авторизации;
- `TOR_1C_INTEGRATION_PART2_READINGS.md` — требования к показаниям.
