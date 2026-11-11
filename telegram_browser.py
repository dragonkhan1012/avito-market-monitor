"""Authenticated Telegram browsing while watch is running, independent of Avito requests."""
from __future__ import annotations

import hashlib
import threading
import sqlite3
from datetime import datetime

import apartments
import telegram_notify as tg
from market_store import MarketStore

PAGE_SIZE = 5


class DealBrowser:
    def __init__(self, notifier, searches, log):
        self.notifier, self.searches, self.log = notifier, searches, log
        self.by_key = {hashlib.sha256(s.name.encode()).hexdigest()[:12]: s for s in searches}
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self.run, name='telegram-market', daemon=True)
        self.offset_path = tg.SETTINGS_PATH.with_name('telegram-offset-' + notifier.token.split(':')[0] + '.json')

    def start(self):
        self.thread.start()

    def close(self):
        self.stop.set()
        self.thread.join(timeout=25)

    def api(self, method, payload):
        if self.stop.is_set():
            raise tg.TelegramNetworkError("Мониторинг остановлен")
        # One bounded request: the worker's interruptible loop handles retry/backoff.
        return tg._call_once(self.notifier.token, method, payload)

    def send(self, text, keyboard=None):
        payload = {'chat_id': self.notifier.chat_id, 'text': text, 'parse_mode': 'HTML', 'disable_web_page_preview': True}
        if keyboard:
            payload['reply_markup'] = {'inline_keyboard': keyboard}
        return self.api('sendMessage', payload)

    def menu(self, page=0):
        entries = list(self.by_key.items())
        page = max(0, min(page, max(0, (len(entries) - 1) // 10)))
        buttons = [[{'text': s.title[:60], 'callback_data': f'd:{key}:0'}] for key, s in entries[page*10:(page+1)*10]]
        nav = []
        if page:
            nav.append({'text': '← Поиски', 'callback_data': f's:{page-1}'})
        if (page+1)*10 < len(entries):
            nav.append({'text': 'Ещё поиски →', 'callback_data': f's:{page+1}'})
        if nav:
            buttons.append(nav)
        buttons.append([{'text': 'Состояние мониторинга', 'callback_data': 'status'}])
        self.send('🔎 <b>Выгодные предложения</b>\nВыберите активный поиск.\n/deals — предложения · /status — состояние', buttons)

    def show_deals(self, key, page):
        search = self.by_key.get(key)
        if not search:
            self.menu()
            return
        store = MarketStore(search.path('market.sqlite'))
        try:
            rows, state = store.deals(), store.state()
            analysis = store.get('analyzed_at', 0)
        finally:
            store.close()
        page = max(0, min(page, max(0, (len(rows) - 1) // PAGE_SIZE)))
        lines = [f'<b>{tg.esc(search.title[:120])}</b>', f'Подходящих предложений: {len(rows)}.']
        if not state.get('complete'):
            lines.append('Обход ещё не завершён; список пополняется.')
        if analysis:
            lines.append('Анализ: ' + datetime.fromtimestamp(analysis).strftime('%d.%m %H:%M'))
        keyboard = []
        for index, fields in enumerate(rows[page*PAGE_SIZE:(page+1)*PAGE_SIZE], page*PAGE_SIZE+1):
            seen = datetime.fromtimestamp(fields['last_seen']).strftime('%d.%m %H:%M')
            lines.append(f"\n<b>{index}. {tg.esc(fields['title'][:100])}</b>\n{tg.esc(fields['price'])} · похожие {tg.esc(fields['market'])}\nДешевле на {fields['discount']:.0%} ({fields['saving']:,} ₽); аналогов: {fields['analogs']}\nПоследний раз в выдаче: {seen}")
            keyboard.append([{'text': f'{index}. Открыть на Авито', 'url': fields['url']}])
        if not rows:
            lines.append('Пока нет предложений с достаточным числом аналогов и нужной скидкой. Неуточнённые товары сохранены в Excel.')
        nav = []
        if page:
            nav.append({'text': '← Назад', 'callback_data': f'd:{key}:{page-1}'})
        if (page+1)*PAGE_SIZE < len(rows):
            nav.append({'text': 'Далее →', 'callback_data': f'd:{key}:{page+1}'})
        if nav:
            keyboard.append(nav)
        keyboard.append([{'text': 'Обновить', 'callback_data': f'd:{key}:{page}'}, {'text': 'Все поиски', 'callback_data': 's:0'}])
        self.send('\n'.join(lines), keyboard)

    def status(self):
        lines = ['<b>Активный мониторинг</b>']
        for search in self.searches:
            store = MarketStore(search.path('market.sqlite'))
            try:
                state = store.state()
                errors = len(store.get('analysis_errors', {}))
                count = store.db.execute('SELECT COUNT(*) FROM items').fetchone()[0]
                details = len(store.pending_details())
                pending = store.db.execute('SELECT COUNT(*) FROM outbox WHERE sent IS NULL').fetchone()[0]
                lines.append(f"\n{tg.esc(search.name)}: в базе {count}; выгодных {len(store.deals())}.\nСтраница {state['next_page']}, обход: {'завершён' if state.get('complete') else 'идёт'}. Описаний в очереди сейчас: {details}; ошибок анализа: {errors}; уведомлений в очереди: {pending}.")
                if state.get('error'):
                    lines.append(tg.esc(state['error']))
            finally:
                store.close()
            if sum(map(len, lines)) > 3000:
                self.send('\n'.join(lines))
                lines = []
        if lines:
            self.send('\n'.join(lines), [[{'text': 'Выгодные объявления', 'callback_data': 's:0'}]])

    def handle(self, update):
        callback = update.get('callback_query') or {}
        message = callback.get('message') or update.get('message') or {}
        # Only the configured chat can read the stored searches.
        if str((message.get('chat') or {}).get('id')) != str(self.notifier.chat_id):
            return
        if callback:
            self.api('answerCallbackQuery', {'callback_query_id': callback['id']})
            data = callback.get('data', '')
            parts = data.split(':')
            if len(parts) == 3 and parts[0] == 'd' and parts[2].isdigit():
                self.show_deals(parts[1], min(int(parts[2][:8]), 100000))
            elif len(parts) == 2 and parts[0] == 's' and parts[1].isdigit():
                self.menu(min(int(parts[1][:8]), 100000))
            elif data == 'status':
                self.status()
        else:
            command = str(message.get('text', '')).split(' ', 1)[0].split('@', 1)[0]
            if command == '/status':
                self.status()
            elif command in ('/start', '/help', '/deals', '/searches'):
                self.menu()

    def run(self):
        try:
            offset = max(0, int(apartments.load_seen(self.offset_path).get('offset', 0)))
        except (ValueError, TypeError):
            offset = 0
        try:
            self.api('setMyCommands', {'commands': [
                {'command': 'deals', 'description': 'Выгодные объявления по активным поискам'},
                {'command': 'searches', 'description': 'Активные поиски'},
                {'command': 'status', 'description': 'Состояние сбора и анализа'},
            ], 'scope': {'type': 'chat', 'chat_id': self.notifier.chat_id}})
        except tg.TelegramError as exc:
            self.log('TELEGRAM-ERROR', error=str(exc))
        while not self.stop.is_set():
            try:
                updates = self.api('getUpdates', {'offset': offset, 'timeout': 5, 'allowed_updates': ['message', 'callback_query']})
                if not isinstance(updates, list):
                    raise tg.TelegramError('Telegram вернул некорректный список обновлений')
                for update in updates:
                    if self.stop.is_set():
                        return
                    self.handle(update)
                    offset = update['update_id'] + 1
                    apartments.save_json(self.offset_path, {'offset': offset})
                for search in self.searches:
                    if self.stop.is_set():
                        return
                    store = MarketStore(search.path('market.sqlite'))
                    try:
                        # Outbox sending uses single requests too, so stopping remains bounded.
                        store.deliver(self, search.name, self.log)
                    finally:
                        store.close()
            except (tg.TelegramError, OSError, ValueError, sqlite3.Error) as exc:
                self.log('TELEGRAM-ERROR', error=f'{type(exc).__name__}: {str(exc)[:220]}')
                self.stop.wait(max(15, getattr(exc, 'retry_after', 0)))
