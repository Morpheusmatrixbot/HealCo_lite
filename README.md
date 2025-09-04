# HealCo Lite

## Описание
HealCo Lite — упрощённая версия телеграм‑бота HealCo для подсчёта КБЖУ, подбора меню, тренировок и мотивации.

## Установка зависимостей
```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Переменные окружения
Перед запуском задайте переменные окружения:

- `OPENAI_API_KEY` — ключ OpenAI.
- `TELEGRAM_BOT_TOKEN` — токен Telegram‑бота.
- `TELEGRAM_PAYMENT_PROVIDER_TOKEN` — токен платёжного провайдера Telegram.
- `DEVELOPER_USER_ID` — Telegram ID разработчика или администратора.
- `GOOGLE_CSE_KEY` — API‑ключ Google Custom Search.
- `GOOGLE_CSE_CX` — идентификатор поисковой системы Google CSE.
- `VISION_KEY` — ключ API для распознавания изображений (опционально).
- `USDA_FDC_API_KEY` — ключ USDA FoodData Central.
- `EXTERNAL_JSONL_URL` — URL внешнего JSONL файла с данными (опционально).
- `GDRIVE_ID` — идентификатор файла в Google Drive (опционально).
- `FATSECRET_KEY` — ключ API FatSecret.
- `FATSECRET_SECRET` — секрет API FatSecret.
- `HLITE_DB_PATH` — путь к файлу локальной БД (по умолчанию `db.json`).

## Примеры запуска

### Локально
```bash
export OPENAI_API_KEY=<ваш ключ>
export TELEGRAM_BOT_TOKEN=<ваш токен>
# другие переменные по необходимости
python main.py
```

### Cloud Run
```bash
gcloud builds submit --tag gcr.io/<PROJECT_ID>/healco-lite
gcloud run deploy healco-lite \
  --image gcr.io/<PROJECT_ID>/healco-lite \
  --region <REGION> \
  --port 8080 \
  --set-env-vars PORT=8080,OPENAI_API_KEY=...,TELEGRAM_BOT_TOKEN=...,TELEGRAM_PAYMENT_PROVIDER_TOKEN=... \
  --health-check-http-path /healthz \
  --allow-unauthenticated
```

При долгой инициализации можно увеличить таймауты проверок здоровья, например:

```bash
gcloud run services update healco-lite \
  --health-check-initial-delay 180 \
  --health-check-timeout 60
```

## Лицензия
Проект распространяется по лицензии MIT. См. файл [LICENSE](LICENSE) для подробностей.
