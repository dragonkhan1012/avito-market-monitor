"""Goods watch: full sweeps, existing-item changes and persistent decisions."""
import argparse
import json
import time
from unittest.mock import patch

import avito
import avito_client
from market_monitor import Monitor
from market_store import MarketStore
from test_market_monitor import item


def configured(tmp_path, monkeypatch):
    monkeypatch.setattr(avito, 'SEARCHES_DIR', tmp_path/'searches')
    monkeypatch.setattr(avito, 'DATA_DIR', tmp_path/'data')
    monkeypatch.setattr(avito, 'OUTPUT_DIR', tmp_path/'output')
    avito.SEARCHES_DIR.mkdir()
    (avito.SEARCHES_DIR/'mac.json').write_text(json.dumps({
        'title': 'Macbook', 'profile': 'goods',
        'catalog_url': 'https://www.avito.ru/moskva/noutbuki',
        'params': {'q': 'macbook pro 13 2020'},
    }))
    return avito.load_search('mac')


def test_watch_updates_existing_item_and_preserves_event(tmp_path, monkeypatch):
    search = configured(tmp_path, monkeypatch)
    cycles = [tuple(item(i) for i in range(1, 7)),
              tuple(item(i, 35000 if i == 6 else 50000) for i in range(1, 7))]
    calls = []
    class Client:
        def __init__(self, **kw):
            pass
        def page(self, *, number, **kw):
            calls.append(number)
            return avito_client.Page(number, cycles[len(calls)-1], 6, 6)
    sleeps = []
    def sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) >= 2:
            raise KeyboardInterrupt
    monkeypatch.setattr(avito_client, 'CalmClient', Client)
    monkeypatch.setattr(time, 'sleep', sleep)
    avito.cmd_watch(argparse.Namespace(names=['mac'], delay=30, interval=60, no_telegram=True))
    store = MarketStore(search.path('market.sqlite'))
    try:
        assert store.deals()[0]['price_value'] == 35000
        assert store.db.execute('SELECT COUNT(*) FROM outbox').fetchone()[0] == 1
        assert store.db.execute('SELECT COUNT(*) FROM prices WHERE item_id="6"').fetchone()[0] == 2
        assert json.loads(search.path('listings.json').read_text())['6']['priceDetailed']['value'] == 35000
    finally:
        store.close()
    assert (avito.OUTPUT_DIR/'mac-monitor.xlsx').exists()


def test_deep_sweep_checks_head_between_pages_and_resumes(tmp_path, monkeypatch):
    search = configured(tmp_path, monkeypatch)
    calls = []
    class Client:
        def page(self, *, number, **kw):
            calls.append(number)
            ids = range((number-1)*50+1, number*50+1)
            return avito_client.Page(number, tuple(item(i) for i in ids), 150, 50)
    monitor = Monitor(search, Client(), lambda *a, **kw: None, avito.OUTPUT_DIR)
    try:
        monitor.step()
        assert monitor.store.state()['next_page'] == 2
    finally:
        monitor.close()
    monitor = Monitor(search, Client(), lambda *a, **kw: None, avito.OUTPUT_DIR)
    try:
        monitor.step()
        monitor.step()
        assert monitor.store.state()['complete']
        assert len(monitor.store.raw()) == 150
    finally:
        monitor.close()
    assert calls == [1, 1, 2, 1, 3]


def test_failure_keeps_cursor_and_later_retry_succeeds(tmp_path, monkeypatch):
    search = configured(tmp_path, monkeypatch)
    class Client:
        broken = True
        def page(self, *, number, **kw):
            if self.broken:
                raise RuntimeError('offline')
            return avito_client.Page(number, (item(1),), 1, 1)
    client = Client()
    monitor = Monitor(search, client, lambda *a, **kw: None, avito.OUTPUT_DIR)
    try:
        assert not monitor.step()
        assert monitor.store.state()['next_page'] == 1
        assert not monitor.store.state()['complete']
        client.broken = False
        state = monitor.store.state()
        state['retry_at'] = 0
        monitor.store.set('crawl', state)
        assert monitor.step()
        assert monitor.store.state()['complete']
        assert len(monitor.store.raw()) == 1
    finally:
        monitor.close()


def test_existing_deal_on_restart_is_not_silently_suppressed(tmp_path, monkeypatch):
    search = configured(tmp_path, monkeypatch)
    store = MarketStore(search.path('market.sqlite'))
    store.ingest(avito_client.Page(1, tuple(item(i, 35000 if i == 6 else 50000) for i in range(1, 7)), 6, 6))
    store.close()
    monitor = Monitor(search, None, lambda *a, **kw: None, avito.OUTPUT_DIR, enqueue=True)
    try:
        monitor.store.analyze(search, enqueue=True)
        monitor.store.analyze(search, enqueue=True)
        assert monitor.store.db.execute('SELECT COUNT(*) FROM outbox').fetchone()[0] == 1
    finally:
        monitor.close()


def test_excel_refreshes_when_more_deals_are_found(tmp_path, monkeypatch):
    search = configured(tmp_path, monkeypatch)
    first_page = tuple(item(i, 35_000 if i == 50 else 50_000) for i in range(1, 51))
    second_page = tuple(item(i, 34_000 if i == 100 else 50_000) for i in range(51, 101))

    class Client:
        def page(self, *, number, **kwargs):
            rows = first_page if number == 1 else second_page
            return avito_client.Page(number, rows, 150, len(rows))

    monitor = Monitor(search, Client(), lambda *a, **kw: None, avito.OUTPUT_DIR)
    try:
        monitor.step()
        from openpyxl import load_workbook
        path = avito.OUTPUT_DIR / 'mac-monitor.xlsx'
        workbook = load_workbook(path, read_only=True)
        assert workbook['Выгодные'].max_row == 2
        workbook.close()

        monitor.step()
        workbook = load_workbook(path)
        links = [row[7].hyperlink.target for row in workbook['Выгодные'].iter_rows(min_row=2)]
        workbook.close()
        assert len(links) == 2
        assert not monitor.store.state()['complete']
        assert len(set(links)) == 2
        assert all(link.startswith('https://www.avito.ru/') for link in links)
    finally:
        monitor.close()
