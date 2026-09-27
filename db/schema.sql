-- ============================================================================
-- Схема микросервиса управления заявками (Ticket Service)
-- СУБД: PostgreSQL 13+
-- Идемпотентно: можно выполнять повторно.
-- ============================================================================

-- Стадии заявки (жизненный цикл)
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'ticket_stage') THEN
        CREATE TYPE ticket_stage AS ENUM (
            'UNPROCESSED',   -- Не обработана
            'PENDING_MAINT', -- Ожидает ТО
            'DIAGNOSTICS',   -- Диагностика
            'IN_PROGRESS',   -- В работе
            'CONTROL',       -- Контроль
            'PROCESSED'      -- Обработана
        );
    END IF;
END$$;

-- Роли пользователей (полный набор ролей системы; права выдаёт модуль прав доступа)
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'user_role') THEN
        CREATE TYPE user_role AS ENUM ('ADMIN', 'MANAGER', 'ENGINEER', 'OBSERVER');
    END IF;
END$$;

-- ---------------------------------------------------------------------------
-- Заявка (классическая задача CRM)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS tickets (
    id             SERIAL PRIMARY KEY,
    title          VARCHAR(255)  NOT NULL,
    description    TEXT          NOT NULL DEFAULT '',
    -- привязка к объекту, с которого пришло предупреждение (обязательна)
    object_id      VARCHAR(64)   NOT NULL,
    author_id      VARCHAR(64)   NOT NULL,          -- постановщик
    assignee_ids   TEXT          NOT NULL DEFAULT '[]',  -- исполнители (JSON-массив)
    watcher_ids    TEXT          NOT NULL DEFAULT '[]',  -- наблюдатели (JSON-массив)
    stage          ticket_stage  NOT NULL DEFAULT 'UNPROCESSED',
    due_date       DATE,                             -- дата исполнения
    created_at     TIMESTAMPTZ   NOT NULL DEFAULT now(),  -- дата постановки
    updated_at     TIMESTAMPTZ   NOT NULL DEFAULT now(),
    closed_at      TIMESTAMPTZ,                      -- перевод в ОБРАБОТАНА
    warning_source VARCHAR(128),                     -- источник предупреждения
    created_by     VARCHAR(64)   NOT NULL,           -- кто нажал "создать заявку"
    is_deleted     SMALLINT      NOT NULL DEFAULT 0,

    -- Бизнес-инварианты на уровне хранилища: даже если валидация сервиса
    -- будет обойдена (ручная правка, другой микросервис), данные не "сломаются".
    CONSTRAINT ck_tickets_title               CHECK (length(btrim(title)) >= 3),
    CONSTRAINT ck_tickets_object_id           CHECK (length(btrim(object_id)) >= 1),
    CONSTRAINT ck_tickets_due_not_before_created CHECK (due_date IS NULL OR due_date >= created_at::date)
);

COMMENT ON TABLE  tickets              IS 'Заявки (задачи), созданные по предупреждениям с объектов';
COMMENT ON COLUMN tickets.object_id    IS 'ID объекта, с которого поступило предупреждение';
COMMENT ON COLUMN tickets.author_id    IS 'Постановщик заявки';
COMMENT ON COLUMN tickets.assignee_ids IS 'Исполнители (может быть несколько), JSON-массив user_id';
COMMENT ON COLUMN tickets.watcher_ids  IS 'Наблюдатели, JSON-массив user_id';
COMMENT ON COLUMN tickets.due_date     IS 'Дата исполнения (план)';
COMMENT ON COLUMN tickets.created_at   IS 'Дата постановки заявки';

CREATE INDEX IF NOT EXISTS idx_tickets_object_id ON tickets (object_id);
CREATE INDEX IF NOT EXISTS idx_tickets_stage     ON tickets (stage);
CREATE INDEX IF NOT EXISTS idx_tickets_author    ON tickets (author_id);
CREATE INDEX IF NOT EXISTS idx_tickets_created   ON tickets (created_at DESC);
-- выборка "мои заявки как исполнитель": containment-опрос по JSON-массиву
CREATE INDEX IF NOT EXISTS idx_tickets_assignees ON tickets USING GIN ((assignee_ids::jsonb) jsonb_path_ops);
CREATE INDEX IF NOT EXISTS idx_tickets_watchers  ON tickets USING GIN ((watcher_ids::jsonb)  jsonb_path_ops);

-- ---------------------------------------------------------------------------
-- Справочник пользователей (локальный кэш данных модуля прав доступа)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS ticket_users (
    user_id   VARCHAR(64) PRIMARY KEY,
    full_name VARCHAR(255) NOT NULL,
    role      user_role    NOT NULL,
    position  VARCHAR(128),
    email     VARCHAR(255),
    is_active BOOLEAN      NOT NULL DEFAULT TRUE
);

COMMENT ON TABLE ticket_users IS 'Справочник сотрудников (ФИО/роль) для карточек заявок и выбора исполнителя';

-- ---------------------------------------------------------------------------
-- История изменений заявки (аудит)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS ticket_history (
    id         SERIAL PRIMARY KEY,
    ticket_id  INTEGER     NOT NULL REFERENCES tickets (id) ON DELETE CASCADE,
    changed_by VARCHAR(64) NOT NULL,
    action     VARCHAR(32) NOT NULL,   -- CREATED / UPDATED / STAGE_CHANGED / DELETED / COMMENTED
    field      VARCHAR(64),
    old_value  TEXT,
    new_value  TEXT,
    comment    TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_ticket_history_ticket ON ticket_history (ticket_id, created_at);

-- ---------------------------------------------------------------------------
-- Журнал уведомлений исполнителям/наблюдателям (email/user_id подтягиваются
-- из справочника ticket_users, который является локальным кэшем модуля прав)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS ticket_notifications (
    id          SERIAL PRIMARY KEY,
    ticket_id   INTEGER     NOT NULL REFERENCES tickets (id) ON DELETE CASCADE,
    recipient   VARCHAR(64) NOT NULL,               -- user_id получателя
    event_type  VARCHAR(32) NOT NULL,               -- CREATED / STAGE_CHANGED / ASSIGNEE_ADDED / ...
    subject     VARCHAR(255) NOT NULL,
    body        TEXT        NOT NULL DEFAULT '',
    channel     VARCHAR(32) NOT NULL DEFAULT 'inbox',
    status      VARCHAR(16) NOT NULL DEFAULT 'sent', -- sent / failed
    error       TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMENT ON TABLE ticket_notifications IS 'Журнал отправленных уведомлений по заявкам';

CREATE INDEX IF NOT EXISTS idx_notifications_recipient ON ticket_notifications (recipient, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_notifications_ticket    ON ticket_notifications (ticket_id, created_at DESC);
