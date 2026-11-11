# Установка и первый запуск

## 1. Подготовьте окружение

Установите [uv по официальной инструкции](https://docs.astral.sh/uv/getting-started/installation/), затем скачайте архив репозитория или клонируйте его:

```bash
git clone https://github.com/dragonkhan1012/avito-market-monitor.git
cd avito-market-monitor
uv sync --locked
```

Проект требует Python 3.11 или новее. Зависимости перечислены в `pyproject.toml`, зафиксированные версии — в `uv.lock`. Обычная установка не устанавливает дополнительные библиотеки `captcha`.

## 2. Создайте поиск

1. На Avito выберите город, категорию и необходимые фильтры.
2. Скопируйте ссылку страницы выдачи.
3. Выполните `uv run python avito.py` и выберите добавление поиска.

То же действие через команду:

```bash
uv run python avito.py add "ССЫЛКА_НА_ПОИСК_AVITO" --name my-search
uv run python avito.py list
```

`my-search` — пример имени: используйте латиницу, цифры и дефисы. Команда добавления проверяет ссылку и может обращаться к источнику. Личные настройки создаются в `searches/`; в публичном репозитории чужих поисков нет.

## 3. Соберите данные

```bash
# Конечный обход с сохранением прогресса
uv run python avito.py scan my-search

# Непрерывный мониторинг без отправки в Telegram
uv run python avito.py watch my-search --no-telegram
```

Остановить мониторинг можно через Ctrl+C. Для товаров данные сохраняются в `data/searches/my-search/market.sqlite`, Excel — в `output/my-search-monitor.xlsx`. Команда `table my-search` пересобирает отчёт из локальной базы.

На macOS после установки окружения доступны `Avito Parser.command` и `Мониторинг рынка.command`. Второй файл запускает сохранённые поиски с профилем `goods`; если их ещё нет, открывается меню.

## 4. Подключите Telegram при необходимости

Создайте собственного бота через BotFather. Подключите его через меню или:

```bash
uv run python avito.py telegram setup
```

Следуйте подсказкам программы, затем запустите мониторинг. В привязанном чате доступны `/deals`, `/searches`, `/status`. Команда `telegram test` отправляет настоящее тестовое сообщение — используйте её только после подключения своего чата.

Токен хранится в `data/telegram.json`; альтернативный источник токена — переменная окружения `AVITO_TELEGRAM_TOKEN`. Файлы `.env` приложение автоматически не загружает.

## Установка без uv

При установленном Python 3.11+ создайте окружение и поставьте основные зависимости:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install "curl-cffi>=0.15.0" "openpyxl>=3.1.5" "questionary>=2.1.1"
.venv/bin/python avito.py
```

На Windows команды имеют вид:

```powershell
py -3.11 -m venv .venv
.venv\Scripts\python.exe -m pip install "curl-cffi>=0.15.0" "openpyxl>=3.1.5" "questionary>=2.1.1"
.venv\Scripts\python.exe avito.py
```

Для воспроизводимого набора версий предпочтителен вариант `uv sync --locked`.

## Настройки оценки

Откройте созданный `searches/my-search.json`. Сохранив обязательные поля поиска, добавьте параметры из [примера](../examples/monitor-settings.json). Это фрагмент настроек, а не самостоятельный готовый поиск. Подробное описание — в [MONITORING.md](../MONITORING.md).
