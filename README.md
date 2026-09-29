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
├── .env.example            # шаблон ENV (CORS, ключи, домен)
├── docker-compose.yml      # БД + API (+ Caddy HTTPS, profile https)
├── Dockerfile              # порт 8080 внутри контейнера
├── deploy/
│   ├── Caddyfile           # reverse-proxy + Let's Encrypt
│   └── VPS.md              # пошаговый деплой на VPS
├── db/schema.sql           # DDL (tickets / ticket_users / ticket_history / notifications)
└── app/
    ├── main.py             # FastAPI + CORS + роутеры
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
        ├── users.py          # справочник пользователей (для UI)
        ├── notifications.py  # inbox уведомлений
        └── cache.py          # метрики/сброс кэша
```

## Запуск

### Вариант 1 — Docker (рекомендуется, поднимает и PostgreSQL)

```bash
cp .env.example .env         # задайте TICKETS_API_KEY и CORS_ORIGINS
docker compose up -d --build
curl http://localhost:8080/health
# Swagger UI: http://localhost:8080/docs
docker compose down -v       # остановить и удалить том с данными
```

Порты (везде **8080** для HTTP API):

| Где | Порт |
|---|---|
| Uvicorn внутри контейнера | `8080` |
| Проброс на хост | `${TICKETS_PUBLISH_PORT:-8080}:8080` |
| PostgreSQL | только внутри docker-сети (`db:5432`), наружу не публикуется |
| HTTPS (Caddy, profile `https`) | `80` / `443` → `app:8080` |

Только образ сервиса без compose:

```bash
docker build -t ticket-service:1.0.0 .
docker run -d --name ticket-service -p 8080:8080 \
  -e TICKETS_DATABASE_URL="postgresql+psycopg2://tickets_app:tickets_pass@host.docker.internal:5432/tickets_db" \
  -e CORS_ORIGINS="http://localhost:5173" \
  ticket-service:1.0.0
```

Распределённый кэш на Redis (опционально): `docker compose --profile cache up -d`
и `REDIS_URL=redis://redis:6379/0` в `.env`.

### Деплой на VPS (доступ из интернета + фронт)

Краткая схема — подробности в [`deploy/VPS.md`](deploy/VPS.md):

```bash
cp .env.example .env
# POSTGRES_PASSWORD, TICKETS_API_KEY, CORS_ORIGINS, PUBLIC_BASE_URL
docker compose up -d --build
# API: http://YOUR_VPS_IP:8080  |  docs: http://YOUR_VPS_IP:8080/docs
```

С доменом и HTTPS:

```bash
# в .env: DOMAIN=api.example.com  PUBLIC_BASE_URL=https://api.example.com
#         CORS_ORIGINS=https://your-frontend.example.com
docker compose --profile https up -d --build
```

С фронта на каждый запрос передавайте заголовки `X-API-Key`, `X-User-Id`, `X-Role`.
`CORS_ORIGINS` должен совпадать с origin фронта (схема + хост + порт).

### Вариант 2 — локально без Docker

```bash
pip install -r requirements.txt

# подготовить БД (PostgreSQL должен быть запущен)
psql "$TICKETS_DATABASE_URL" -f db/schema.sql

python run.py            # http://127.0.0.1:8080
# интерактивная документация: http://127.0.0.1:8080/docs
```

Быстрый старт без PostgreSQL (in-memory хранилище, только для разработки):

```bash
TICKETS_DATABASE_URL=memory python run.py
```

Переменные окружения (см. `.env.example`):

| Переменная | Описание | По умолчанию |
|---|---|---|
| `TICKETS_DATABASE_URL` | строка подключения к PostgreSQL | `postgresql+psycopg2://…@127.0.0.1:5432/tickets_db` |
| `TICKETS_PORT` | порт HTTP API | `8080` |
| `CORS_ORIGINS` | origin фронта через запятую или `*` | `*` |
| `PUBLIC_BASE_URL` | публичный URL (для Swagger) | пусто / localhost |
| `ACCESS_CONTROL_URL` | адрес модуля прав доступа (если пуст - встроенный stub) | пусто |
| `ACCESS_CONTROL_API_KEY` | ключ сервиса для обращений к модулю прав | `service-secret-key` |
| `TICKETS_API_KEY` | ключ для обращений клиентов к этому API | `gateway-secret-key` |
| `SEED_DEMO_DATA` | `1` - загрузить демо-пользователей | `1` |
| `DOMAIN` | домен для Caddy HTTPS | — |
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
| GET | `/api/v1/health` | то же (алиас) |
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
