# Ticket Service (модуль управления заявками)

Микросервис управления заявками (tasks/tickets) в рамках CRM-подобной архитектуры.

## Возможности

* Создание заявки из предупреждения, пришедшего **с объекта** (`object_id` — обязательная привязка).
* Жизненный цикл заявки по стадиям:
  `НЕ ОБРАБОТАНА -> ОЖИДАЕТ ТО -> ДИАГНОСТИКА -> В РАБОТЕ -> КОНТРОЛЬ -> ОБРАБОТАНА`
* Ролевая модель доступа (**проверку прав выполняет внешний модуль прав доступа**, сервис только обращается к нему):
  * `ADMIN` - видит все заявки;
  * `MANAGER` - ставит и редактирует заявки, управляет стадией, выступает постановщиком или наблюдателем;
  * `ENGINEER` - исполнитель, управляет стадией своих заявок.
* История изменений (аудит) каждой заявки.
* REST API (FastAPI), хранилище - PostgreSQL (SQLAlchemy Core, без ORM).

## Структура

```
ticket_service/
├── run.py                  # точка входа (uvicorn)
├── requirements.txt
├── .env.example
├── db/schema.sql           # DDL (таблицы tickets / ticket_users / ticket_history)
└── app/
    ├── main.py             # FastAPI + регистрация роутеров
    ├── config.py           # настройки из ENV
    ├── database.py         # пул соединений с PostgreSQL
    ├── models.py           # Enum-стадии, Pydantic-схемы запросов/ответов
    ├── exceptions.py       # доменные исключения -> HTTP
    ├── auth/
    │   ├── access_control.py   # клиент модуля прав доступа (HTTP / in-memory fallback)
    │   └── dependencies.py     # FastAPI-зависимости (X-API-Key, X-User-Id)
    ├── repositories/
    │   └── ticket_repository.py  # слой работы с БД (CRUD, только SQL)
    ├── services/
    │   └── ticket_service.py     # бизнес-логика: одна операция = один метод
    └── api/routes/
        ├── tickets.py        # эндпоинты заявок
        ├── objects.py        # сводка по объектам
        └── users.py          # справочник пользователей (для UI)
```

## Запуск

### Вариант 1 — Docker (рекомендуется, поднимает и PostgreSQL)

```bash
docker compose up -d --build     # БД + сервис в одном стенде
curl http://localhost:8081/health
# Swagger UI: http://localhost:8081/docs
docker compose down -v           # остановить и удалить том с данными
```

Только образ сервиса без compose:

```bash
docker build -t ticket-service:1.0.0 .
docker run -d --name ticket-service -p 8081:8081 \
  -e TICKETS_DATABASE_URL="postgresql+psycopg2://tickets_app:tickets_pass@host.docker.internal:5433/tickets_db" \
  ticket-service:1.0.0
```

Распределённый кэш на Redis (опционально): `docker compose --profile cache up -d`
и `REDIS_URL=redis://redis:6379/0` в окружении сервиса.

### Вариант 2 — локально без Docker

```bash
pip install -r requirements.txt

# подготовить БД (PostgreSQL должен быть запущен)
psql "$TICKETS_DATABASE_URL" -f db/schema.sql

python run.py            # http://127.0.0.1:8081
# интерактивная документация: http://127.0.0.1:8081/docs
```

Быстрый старт без PostgreSQL (in-memory хранилище, только для разработки):

```bash
TICKETS_DATABASE_URL=memory python run.py
```

Переменные окружения (см. `.env.example`):

| Переменная | Описание | По умолчанию |
|---|---|---|
| `TICKETS_DATABASE_URL` | строка подключения к PostgreSQL (порт БД — нестандартный **5433**) | `postgresql://tickets_app:tickets_pass@127.0.0.1:5433/tickets_db` |
| `ACCESS_CONTROL_URL` | адрес модуля прав доступа (если пуст - встроенный stub) | пусто |
| `ACCESS_CONTROL_API_KEY` | ключ сервиса для обращений к модулю прав | `service-secret-key` |
| `TICKETS_API_KEY` | ключ для обращений клиентов к этому API | `gateway-secret-key` |
| `SEED_DEMO_DATA` | `1` - загрузить демо-пользователей | `1` |

## Аутентификация и авторизация

Клиент (API-шлюз / фронтенд) передаёт заголовки:

```
X-API-Key: gateway-secret-key     # ключ вызывающей стороны
X-User-Id: manager_ivanov          # субъект от имени которого выполняется действие
X-Role: MANAGER                    # роль, выданная модулем аутентификации (не доверяем - сверяем)
```

Сервис **не хранит пароли и не решает самостоятельно, можно ли пользователю что-то делать**:
он запрашивает разрешение у модуля прав доступа (`POST {ACCESS_CONTROL_URL}/check`)
и получает список доступных объектов (`GET {ACCESS_CONTROL_URL}/objects/{user_id}`).
Если модуль недоступен, используется встроенный `InMemoryAccessControlClient` (только для разработки).

## Основные эндпоинты

| Метод | Путь | Назначение |
|---|---|---|
| GET | `/health` | состояние сервиса и БД |
| POST | `/api/v1/tickets` | создать заявку (из предупреждения объекта) |
| GET | `/api/v1/tickets` | список заявок с фильтрами/пагинацией |
| GET | `/api/v1/tickets/{id}` | карточка заявки |
| PATCH | `/api/v1/tickets/{id}` | редактировать заявку |
| DELETE | `/api/v1/tickets/{id}` | удалить заявку (admin) |
| PUT | `/api/v1/tickets/{id}/stage` | перевести на другую стадию |
| POST | `/api/v1/tickets/{id}/comments` | комментарий (пишет историю) |
| GET | `/api/v1/tickets/{id}/history` | история изменений |
| GET | `/api/v1/tickets/stats/stages` | счётчики по стадиям |
| GET | `/api/v1/objects` | объекты пользователя со сводкой заявок |
| GET | `/api/v1/objects/{object_id}/tickets` | заявки конкретного объекта |
| GET | `/api/v1/users/me` | текущий субъект и его права |
| GET | `/api/v1/users` | справочник пользователей (для выбора исполнителя) |

## Тесты

```bash
pytest -q            # unit-тесты (in-memory репозиторий + mock модуля прав)
```

## Swagger / OpenAPI — как посмотреть пошагово

### Шаг 1. Запустить сервис
```bash
# без PostgreSQL (in-memory, для разработки/демо):
TICKETS_DATABASE_URL=memory python run.py

# или через Docker:
docker compose up -d --build
```

### Шаг 2. Открыть интерактивную документацию в браузере
| Адрес | Что там |
|---|---|
| `http://localhost:8081/docs` | **Swagger UI** — можно просматривать эндпоинты и дёргать их кнопкой «Try it out» |
| `http://localhost:8081/redoc` | **ReDoc** — читаемое справочное описание (схемы, поля, примеры) |
| `http://localhost:8081/openapi.json` | сырая OpenAPI 3.1 спецификация (для Postman/Insomnia/editor.swagger.io) |

### Шаг 3. Авторизоваться в Swagger UI
Нажмите кнопку **Authorize** (вверху справа) и заполните:
- `ApiKeyAuth` (X-API-Key): `gateway-secret-key` (значение `TICKETS_API_KEY`);
- `UserHeaderAuth` (X-User-Id): ID пользователя, например `admin-001`, `manager-001`, `engineer-001` (роль определяется модулем прав доступа).

После этого все запросы из UI будут отправляться с нужными заголовками.

### Шаг 4. Посмотреть офлайн (без запущенного сервиса)
В корне проекта лежит актуальный дамп спецификации — **`openapi.json`** (19 эндпоинтов).
Варианты просмотра:
- открыть на https://editor.swagger.io (File → Import);
- импортировать в Postman/Insomnia;
- локально: `npx @redocly/cli preview-docs openapi.json`.
