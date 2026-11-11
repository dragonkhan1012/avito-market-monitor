#!/bin/sh
cd "$(dirname "$0")" || exit 1
if [ -x .venv/bin/python ]; then
  set -- .venv/bin/python
elif command -v uv >/dev/null 2>&1; then
  set -- uv run python
else
  echo "Python-окружение не найдено. Инструкция по установке: README.md"
  read -r _
  exit 1
fi
"$@" -c 'import avito; names = [p.stem for p in sorted(avito.SEARCHES_DIR.glob("*.json")) if avito.load_search(p.stem).profile == "goods"]; avito.main_cli(["watch", *names] if names else [])'
status=$?
echo "Мониторинг остановлен. Для закрытия окна нажмите Enter."
read -r _
exit "$status"
