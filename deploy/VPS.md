# Деплой Ticket Service на VPS
#
# Требования: Docker + Docker Compose v2, открытые порты (8080 или 80/443).
#
# ============================================================================
# Вариант A — быстро по IP (HTTP :8080)
# ============================================================================
#
# 1. Скопируйте проект на сервер:
#      scp -r . user@YOUR_VPS_IP:~/ticket-service
#      # или: git clone <repo> && cd ticket-service
#
# 2. Создайте .env:
#      cp .env.example .env
#      nano .env
#    Обязательно задайте:
#      POSTGRES_PASSWORD=...
#      TICKETS_API_KEY=...
#      CORS_ORIGINS=https://your-frontend.example.com
#      PUBLIC_BASE_URL=http://YOUR_VPS_IP:8080
#
# 3. Firewall (пример ufw):
#      sudo ufw allow 22/tcp
#      sudo ufw allow 8080/tcp
#      sudo ufw enable
#
# 4. Запуск:
#      docker compose up -d --build
#
# 5. Проверка:
#      curl http://YOUR_VPS_IP:8080/health
#      # Swagger: http://YOUR_VPS_IP:8080/docs
#
# ============================================================================
# Вариант B — домен + HTTPS (Caddy / Let's Encrypt)
# ============================================================================
#
# 1. DNS: A-запись api.example.com → IP VPS
#
# 2. В .env:
#      DOMAIN=api.example.com
#      PUBLIC_BASE_URL=https://api.example.com
#      CORS_ORIGINS=https://app.example.com
#
# 3. Firewall:
#      sudo ufw allow 22/tcp
#      sudo ufw allow 80/tcp
#      sudo ufw allow 443/tcp
#      # порт 8080 можно не открывать наружу
#
# 4. (Рекомендуется) не публиковать app наружу — в docker-compose.yml
#    закомментируйте секцию ports у сервиса app, затем:
#      docker compose --profile https up -d --build
#
# 5. Проверка:
#      curl https://api.example.com/health
#
# ============================================================================
# Запросы с фронтенда
# ============================================================================
#
# Базовый URL API: PUBLIC_BASE_URL из .env
#
# Обязательные заголовки на каждый запрос:
#   X-API-Key:  <значение TICKETS_API_KEY из .env>
#   X-User-Id:  <id пользователя, напр. manager_ivanov>
#   X-Role:     MANAGER | ENGINEER | ADMIN | OBSERVER
#
# Пример (fetch):
#
#   const API = "https://api.example.com";
#   const res = await fetch(`${API}/api/v1/tickets`, {
#     headers: {
#       "X-API-Key": "…",
#       "X-User-Id": "manager_ivanov",
#       "X-Role": "MANAGER",
#       "Content-Type": "application/json",
#     },
#   });
#
# CORS_ORIGINS должен содержать точный origin фронта (схема+хост+порт),
# например https://app.example.com — не путь.
#
# ============================================================================
# Полезные команды
# ============================================================================
#
#   docker compose logs -f app
#   docker compose ps
#   docker compose down          # остановить
#   docker compose down -v       # + удалить том БД
#
