"""Durable catalogue, price history, crawl checkpoints and Telegram outbox."""
from __future__ import annotations

import json
import hashlib
import sqlite3
import time
from pathlib import Path
from typing import Any

import goods


def dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


class MarketStore:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=5)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS items (
                id TEXT PRIMARY KEY, raw TEXT NOT NULL, first_seen REAL NOT NULL,
                last_seen REAL NOT NULL, epoch INTEGER NOT NULL DEFAULT 0,
                missed INTEGER NOT NULL DEFAULT 0, result TEXT, error TEXT);
            CREATE TABLE IF NOT EXISTS prices (
                seq INTEGER PRIMARY KEY, item_id TEXT NOT NULL, observed REAL NOT NULL, price INTEGER);
            CREATE INDEX IF NOT EXISTS price_item ON prices(item_id, seq);
            CREATE TABLE IF NOT EXISTS details (
                item_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, description TEXT,
                checked REAL NOT NULL DEFAULT 0, attempts INTEGER NOT NULL DEFAULT 0,
                next_try REAL NOT NULL DEFAULT 0, error TEXT);
            CREATE TABLE IF NOT EXISTS outbox (
                event TEXT PRIMARY KEY, item_id TEXT NOT NULL, price INTEGER NOT NULL,
                payload TEXT NOT NULL, created REAL NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
                next_try REAL NOT NULL DEFAULT 0, sent REAL, error TEXT);
        ''')

    def close(self):
        self.db.close()

    def get(self, key: str, default=None):
        row = self.db.execute('SELECT value FROM meta WHERE key=?', (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def put(self, key: str, value):
        self.db.execute('INSERT INTO meta VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value', (key, dump(value)))

    def set(self, key: str, value):
        with self.db:
            self.put(key, value)

    def state(self):
        return self.get('crawl', {'epoch': 1, 'next_page': 1, 'complete': False, 'expected': 0, 'failures': 0})

    def start_pass(self, force=False):
        state = self.state()
        # Earlier releases incorrectly marked a first empty response as a
        # complete pass. Re-open that checkpoint when it contradicts the
        # search total saved during link validation.
        if (state.get('complete') and state.get('expected', 0) == 0
                and self.get('expected_total', 0) > 0 and not state.get('collected', 0)):
            state['complete'] = False
            state['next_page'] = 1
            state['error'] = 'Предыдущий пустой ответ противоречит числу объявлений поиска; повторяем сбор.'
            self.set('crawl', state)
        if force or state.get('complete') or state.get('restart'):
            state = {'epoch': state['epoch'] + 1, 'next_page': 1, 'complete': False,
                     'expected': 0, 'failures': 0, 'previous_issue': state.get('error', '')}
            self.set('crawl', state)
        return state

    def import_legacy(self, base: dict, seen: dict):
        if self.get('legacy_imported'):
            return
        # Old JSON has no reliable last-observed timestamp. Imported offers require a fresh observation.
        with self.db:
            for key, raw in base.items():
                if not isinstance(raw, dict) or not raw.get('id'):
                    continue
                from datetime import datetime
                try:
                    first_seen = datetime.fromisoformat(seen.get(str(raw['id']), '')).timestamp()
                except (ValueError, TypeError):
                    first_seen = 0
                self.db.execute('INSERT OR IGNORE INTO items(id,raw,first_seen,last_seen) VALUES (?,?,?,?)',
                                (str(raw['id']), dump(raw), first_seen, 0))
            self.put('legacy_imported', True)

    @staticmethod
    def fingerprint(raw):
        return hashlib.sha256(dump([raw.get('title'), raw.get('description'), raw.get('priceDetailed')]).encode()).hexdigest()

    def pending_details(self):
        now = time.time()
        rows = self.db.execute("""SELECT i.id,i.raw,d.fingerprint,d.checked,d.next_try,d.error FROM items i
            LEFT JOIN details d ON d.item_id=i.id WHERE i.missed<2 AND i.last_seen>=?
            ORDER BY COALESCE(d.checked,0), i.first_seen, i.id""", (now-self.get('max_age_days',7)*86400,)).fetchall()
        return [(row['id'], json.loads(row['raw'])) for row in rows
                if row['fingerprint'] != self.fingerprint(json.loads(row['raw'])) or
                ((row['next_try'] or 0) <= now and (row['error'] or (row['checked'] or 0) < now-86400))]

    def save_details(self, key, raw, description=None, error=None):
        old = self.db.execute('SELECT attempts FROM details WHERE item_id=?', (key,)).fetchone()
        attempts = (old[0] if old else 0) + 1 if error else 0
        now = time.time()
        retry = now + min(3600, 30 * 2**min(attempts,7)) if error else now+86400
        with self.db:
            self.db.execute("""INSERT INTO details(item_id,fingerprint,description,checked,attempts,next_try,error)
                VALUES (?,?,?,?,?,?,?) ON CONFLICT(item_id) DO UPDATE SET fingerprint=excluded.fingerprint,
                description=excluded.description,checked=excluded.checked,attempts=excluded.attempts,
                next_try=excluded.next_try,error=excluded.error""",
                (key, self.fingerprint(raw), description, now, attempts, retry, str(error)[:250] if error else None))

    def ingest(self, page, *, advance=True, cap=100):
        state = self.state()
        now = time.time()
        with self.db:
            for item in page.items:
                if not isinstance(item, dict) or not isinstance(item.get('id'), (str, int)) or not str(item['id']).isdigit():
                    raise ValueError('Страница содержит объявление без корректного ID; повторим её целиком')
                key = str(item['id'])
                old = self.db.execute('SELECT raw FROM items WHERE id=?', (key,)).fetchone()
                detail = item.get('priceDetailed')
                price = detail.get('value') if isinstance(detail, dict) else None
                if type(price) not in (int, float):
                    price = None
                old_detail = json.loads(old[0]).get('priceDetailed') if old else None
                previous = old_detail.get('value') if isinstance(old_detail, dict) else None
                has_history = self.db.execute('SELECT 1 FROM prices WHERE item_id=? LIMIT 1', (key,)).fetchone()
                if old is None or price != previous or not has_history:
                    self.db.execute('INSERT INTO prices(item_id,observed,price) VALUES (?,?,?)', (key, now, price))
                self.db.execute('''INSERT INTO items(id,raw,first_seen,last_seen,epoch) VALUES (?,?,?,?,?)
                    ON CONFLICT(id) DO UPDATE SET raw=excluded.raw,last_seen=excluded.last_seen,
                    epoch=excluded.epoch,missed=0,result=NULL,error=NULL''', (key, dump(item), now, now, state['epoch']))
            self.put('last_observed', now)
            if not advance:
                return
            signature = ','.join(sorted(str(i['id']) for i in page.items))
            signatures = state.get('signatures', {})
            if signature and signature in signatures.values() and signatures.get(str(page.number)) != signature:
                raise ValueError('Повтор одной и той же страницы выдачи; требуется новый обход')
            signatures[str(page.number)] = signature
            state['signatures'] = signatures
            count = self.db.execute('SELECT COUNT(*) FROM items WHERE epoch=?', (state['epoch'],)).fetchone()[0]
            expected_total = self.get('expected_total', 0)
            state['expected'] = max(state.get('expected', 0), page.total_count or 0, expected_total)
            if expected_total and not page.items:
                state.update(complete=False, restart=True, next_page=1,
                             error=f'Авито вернуло пустую выдачу, хотя при проверке ссылки было {expected_total} объявлений. Повторим сбор.')
                self.put('crawl', state)
                return
            state.update(next_page=page.number + 1, failures=0, retry_at=0, error='', collected=count)
            # The items API's totalCount can be page-limited (for example 50)
            # while the browser catalog shows a much larger result count.
            reached_known_end = bool(expected_total and page.number * 50 >= expected_total)
            if page.is_last and (not expected_total or reached_known_end):
                if not state['expected'] or count == state['expected']:
                    state.update(complete=True, next_page=1, completed_at=now)
                    self.db.execute('UPDATE items SET missed=missed+1 WHERE epoch != ?', (state['epoch'],))
                else:
                    state.update(complete=False, restart=True, next_page=1,
                                 error=f"Выдача изменилась: собрано {count} из заявленных {state['expected']}. Повторный обход.")
            elif page.number >= cap:
                state.update(complete=False, restart=True, next_page=1,
                             error=f'Достигнут предел {cap} страниц. Сузьте поиск фильтрами. Обход будет повторён.')
            self.put('crawl', state)

    def failed(self, error: str):
        state = self.state()
        failures = state.get('failures', 0) + 1
        if 'Повтор одной и той же' in error and failures >= 3:
            state['restart'] = True
        state.update(complete=False, failures=failures, error=error[:300], retry_at=time.time() + min(900, 30 * 2 ** min(failures, 5)))
        self.set('crawl', state)

    def raw(self):
        return {r['id']: json.loads(r['raw']) for r in self.db.execute('SELECT id,raw FROM items')}

    def seen(self):
        from datetime import datetime
        return {r['id']: datetime.fromtimestamp(r['first_seen']).isoformat(timespec='seconds')
                for r in self.db.execute('SELECT id,first_seen FROM items')}

    def analyze(self, search, *, enqueue=False):
        offers, failures = [], {}
        cutoff = time.time() - search.market_max_age_days * 86400
        rows = list(self.db.execute('SELECT * FROM items ORDER BY id'))
        for row in rows:
            try:
                raw = json.loads(row['raw'])
                detail = self.db.execute('SELECT * FROM details WHERE item_id=?', (row['id'],)).fetchone()
                if detail and detail['description'] and detail['fingerprint'] == self.fingerprint(raw):
                    raw['description'] = detail['description']
                offers.append(goods.offer_from_item(raw))
            except (ValueError, TypeError, KeyError, AttributeError, OverflowError) as exc:
                failures[row['id']] = f'{type(exc).__name__}: {str(exc)[:160]}'
        rows_by_id = {r['id']: r for r in rows}
        active = [o for o in offers if rows_by_id[str(o.listing.id)]['last_seen'] >= cutoff and rows_by_id[str(o.listing.id)]['missed'] < 2]
        goods.evaluate_offers(active, home_city=search.home_city, query=search.params.get('q', ''),
                              min_analogs=search.min_analogs, discount=search.deal_discount,
                              min_saving=search.deal_min_saving, alert_sellers=search.alert_sellers)
        live_ids = {str(o.listing.id) for o in active}
        discoveries = []
        with self.db:
            for key, error in failures.items():
                self.db.execute('UPDATE items SET result=NULL,error=? WHERE id=?', (error, key))
            for offer in offers:
                key = str(offer.listing.id)
                row = rows_by_id[key]
                if key not in live_ids:
                    offer.eligible = False
                    offer.decision = 'Давно не встречалось в выдаче — требуется обновление'
                    offer.notes.append(offer.decision)
                detail = self.db.execute('SELECT description,error,fingerprint FROM details WHERE item_id=?', (key,)).fetchone()
                if not detail or not detail['description'] or detail['fingerprint'] != self.fingerprint(json.loads(row['raw'])):
                    offer.notes.append('Полное описание ещё не прочитано; состояние требует проверки')
                if offer.decision not in offer.notes:
                    offer.notes.append(offer.decision)
                fields = goods.alert_fields(offer)
                fields.update(last_seen=row['last_seen'], first_seen=row['first_seen'])
                self.db.execute('UPDATE items SET result=?,error=NULL WHERE id=?', (dump(fields), key))
                if enqueue and fields['eligible']:
                    event = f"{key}:{offer.listing.price}"
                    inserted = self.db.execute('''INSERT OR IGNORE INTO outbox(event,item_id,price,payload,created)
                        VALUES (?,?,?,?,?)''', (event, key, offer.listing.price, dump(fields), time.time()))
                    if inserted.rowcount:
                        discoveries.append(fields)
            self.put('new_deals', discoveries)
            self.put('analysis_errors', failures)
            self.put('analyzed_at', time.time())
        return offers

    def deals(self):
        cutoff = time.time() - self.get('max_age_days', 7) * 86400
        results = [json.loads(r[0]) for r in self.db.execute('SELECT result FROM items WHERE result IS NOT NULL AND last_seen>=? AND missed<2', (cutoff,))]
        return sorted((r for r in results if r['eligible']), key=lambda r: (-r['discount'], -r['saving'], r['id']))

    def deliver(self, notifier, search_name, log):
        from telegram_notify import TelegramError, message_for
        for row in self.db.execute('SELECT * FROM outbox WHERE sent IS NULL AND next_try<=? ORDER BY created LIMIT 5', (time.time(),)).fetchall():
            if getattr(notifier, 'stop', None) is not None and notifier.stop.is_set():
                return
            current = self.db.execute('SELECT result,last_seen,missed FROM items WHERE id=?', (row['item_id'],)).fetchone()
            fields = json.loads(current['result']) if current and current['result'] else None
            if not fields or not fields['eligible'] or fields['price_value'] != row['price'] or current['missed'] >= 2 or current['last_seen'] < time.time() - self.get('max_age_days', 7) * 86400:
                # Pause obsolete events; if this exact price becomes attractive again it may be delivered.
                with self.db:
                    self.db.execute('UPDATE outbox SET next_try=? WHERE event=?', (time.time() + 300, row['event']))
                continue
            try:
                notifier.send(message_for('goods', search_name, fields))
            except TelegramError as exc:
                delay = max(getattr(exc, 'retry_after', 0), min(3600, 30 * 2 ** min(row['attempts'], 7)))
                with self.db:
                    self.db.execute('UPDATE outbox SET attempts=attempts+1,next_try=?,error=? WHERE event=?', (time.time() + delay, type(exc).__name__, row['event']))
                log('TELEGRAM-ERROR', error=str(exc))
                break
            else:
                with self.db:
                    self.db.execute('UPDATE outbox SET sent=?,error=NULL WHERE event=?', (time.time(), row['event']))
                log('TELEGRAM-SENT', search=search_name, **fields)
