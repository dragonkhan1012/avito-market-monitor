#!/bin/sh
cd "$(dirname "$0")" || exit 1
if [ -x .venv/bin/python ]; then
  .venv/bin/python avito.py
elif command -v uv >/dev/null 2>&1; then
  uv run python avito.py
else
  echo "Python-окружение не найдено. Инструкция по установке: README.md"
  read -r _
  exit 1
fi
status=$?
if [ "$status" -ne 0 ]; then
  echo "Парсер завершился с ошибкой $status. Нажмите Enter."
  read -r _
fi
exit "$status"
