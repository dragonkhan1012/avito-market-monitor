"""Offline regressions for the goods monitoring pipeline (stdlib unittest)."""
import argparse
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import avito
import avito_client
import goods
import product_identity
import telegram_notify
from market_monitor import Monitor
from market_store import MarketStore
from process_lock import exclusive
from telegram_browser import DealBrowser


def item(key, price=50000, title='MacBook Pro 13 2020 M1 8/256', **extra):
    return {'id': key, 'type': 'item', 'title': title, 'urlPath': f'/moskva/noutbuki/mac_{key}',
            'description': '', 'priceDetailed': {'value': price}, **extra}


def search(directory):
    s = avito.Search('test', 'MacBook', 'goods', 'https://www.avito.ru/moskva/noutbuki',
                     {'q': 'Macbook pro 13 2020'}, min_analogs=5)
    return s


class IdentityTests(unittest.TestCase):
    def test_normalization(self):
        a = product_identity.identify('Apple MACBOOK Pro 13 2020 M1 8GB 256GB')
        b = product_identity.identify('Макбук pro 13 2020 m1 8/256')
        self.assertEqual(a.label, b.label)
        self.assertTrue(a.complete)

    def test_configurations_do_not_mix(self):
        labels = {product_identity.identify(t).label for t in [
            'Macbook Pro 13 2020 M1 8/256', 'Macbook Pro 13 2020 M1 16/256',
            'Macbook Pro 13 2020 M1 8/512', 'Macbook Air 13 2020 M1 8/256',
            'Macbook Pro 13 2020 i5-1038ng7 8/256']}
        self.assertEqual(len(labels), 5)

    def test_unknowns_stay_unknown(self):
        identity = product_identity.identify('MacBook Pro 13 2020')
        self.assertFalse(identity.complete)
        self.assertIn('процессор', identity.reason)
        self.assertFalse(product_identity.identify('Macbook Pro 13 2020 i5 8/256').complete)

    def test_selected_product(self):
        self.assertFalse(product_identity.matches_query(product_identity.identify('Macbook Air 13 2020 M1 8/256'), 'macbook pro 13 2020'))
        self.assertTrue(product_identity.matches_query(product_identity.identify('Macbook Pro 13 2020 M1 8/256'), 'macbook pro 13 2020'))

    def test_defect_negation(self):
        self.assertFalse(product_identity.risk_reason('', 'Не битый, без блокировок, iCloud отвязан'))
        self.assertTrue(product_identity.risk_reason('', 'Не включается, на запчасти'))

    def test_conservative_generic(self):
        self.assertFalse(product_identity.identify('Хороший ноутбук').complete)


class PriceTests(unittest.TestCase):
    def offers(self, cheap=35000):
        return [goods.offer_from_item(item(i, p)) for i, p in enumerate([50000, 50000, 50000, 50000, 50000, cheap], 1)]

    def test_excludes_candidate_and_requires_enough_analogs(self):
        offers = self.offers()
        goods.evaluate_offers(offers, home_city='moskva')
        self.assertEqual(offers[-1].market, 50000)
        self.assertEqual(offers[-1].analogs, 5)
        self.assertTrue(offers[-1].eligible)
        goods.evaluate_offers(offers[:4] + offers[-1:], home_city='moskva')
        self.assertFalse(offers[-1].eligible)
        self.assertIsNone(offers[-1].market)

    def test_extreme_and_expensive_are_not_deals(self):
        for price in (10000, 80000):
            offers = self.offers(price)
            goods.evaluate_offers(offers, home_city='moskva')
            self.assertFalse(offers[-1].eligible)

    def test_defects_and_wrong_city_excluded(self):
        offers = self.offers()
        offers[-1] = goods.offer_from_item(item(6, 35000, description='На запчасти'))
        goods.evaluate_offers(offers, home_city='moskva')
        self.assertFalse(offers[-1].eligible)
        self.assertTrue(offers[-1].notes)
        offers = self.offers()
        offers[-1].city = 'kazan'
        goods.evaluate_offers(offers, home_city='moskva')
        self.assertFalse(offers[-1].eligible)


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)
        self.patch = patch.object(avito, 'DATA_DIR', self.directory)
        self.patch.start()
        self.search = search(self.directory)
        self.store = MarketStore(self.search.path('market.sqlite'))

    def tearDown(self):
        self.store.close()
        self.patch.stop()
        self.temp.cleanup()

    def page(self, items, n=1, total=None):
        return avito_client.Page(n, tuple(items), len(items) if total is None else total, len(items))

    def seed(self):
        self.store.ingest(self.page([item(i) for i in range(1, 7)]))
        self.store.analyze(self.search, enqueue=True)

    def test_price_change_history_and_outbox_dedup(self):
        self.seed()
        self.store.start_pass()
        self.store.ingest(self.page([item(i, 35000 if i == 6 else 50000) for i in range(1, 7)]))
        self.store.analyze(self.search, enqueue=True)
        self.store.analyze(self.search, enqueue=True)
        self.assertEqual(self.store.deals()[0]['id'], 6)
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM outbox').fetchone()[0], 1)
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM prices WHERE item_id="6"').fetchone()[0], 2)

    def test_failed_notification_survives_restart(self):
        self.store.ingest(self.page([item(i, 35000 if i == 6 else 50000) for i in range(1, 7)]))
        self.store.analyze(self.search, enqueue=True)
        class Broken:
            def send(self, text):
                raise telegram_notify.TelegramNetworkError('offline')
        self.store.deliver(Broken(), 'test', lambda *a, **kw: None)
        self.assertEqual(self.store.db.execute('SELECT attempts FROM outbox').fetchone()[0], 1)
        self.store.close()
        self.store = MarketStore(self.search.path('market.sqlite'))
        with self.store.db:
            self.store.db.execute('UPDATE outbox SET next_try=0')
        sent = []
        class Good:
            def send(self, text):
                sent.append(text)
        self.store.deliver(Good(), 'test', lambda *a, **kw: None)
        self.store.deliver(Good(), 'test', lambda *a, **kw: None)
        self.assertEqual(len(sent), 1)

    def test_resume_and_partial_are_not_complete(self):
        self.store.ingest(self.page([item(1)], total=100))
        self.assertEqual(self.store.state()['next_page'], 2)
        self.store.failed('offline')
        self.store.close()
        self.store = MarketStore(self.search.path('market.sqlite'))
        self.assertEqual(self.store.state()['next_page'], 2)
        self.store.ingest(self.page([], n=2, total=100))
        self.assertFalse(self.store.state()['complete'])
        self.assertTrue(self.store.state()['restart'])
        self.assertEqual(len(self.store.raw()), 1)

    def test_duplicate_page_does_not_advance(self):
        self.store.ingest(self.page([item(1)], total=100))
        with self.assertRaises(ValueError):
            self.store.ingest(self.page([item(1)], n=2, total=100))
        self.assertEqual(self.store.state()['next_page'], 2)

    def test_page_cap_is_partial(self):
        self.store.ingest(self.page([item(1)], total=100), cap=1)
        self.assertFalse(self.store.state()['complete'])
        self.assertIn('предел', self.store.state()['error'])

    def test_malformed_item_keeps_raw_for_retry(self):
        self.store.ingest(self.page([item(1, priceDetailed='broken'), item(2)]))
        self.store.analyze(self.search)
        self.assertEqual(len(self.store.raw()), 2)
        self.assertIn('1', self.store.get('analysis_errors'))

    def test_imported_data_requires_refresh(self):
        self.store.import_legacy({str(i): item(i, 35000 if i == 6 else 50000) for i in range(1, 7)}, {})
        self.store.analyze(self.search, enqueue=True)
        self.assertEqual(self.store.deals(), [])

    def test_two_missing_passes_remove_from_current_deals(self):
        self.store.ingest(self.page([item(i, 35000 if i == 6 else 50000) for i in range(1, 7)]))
        self.store.analyze(self.search)
        self.assertTrue(self.store.deals())
        for _ in range(2):
            self.store.start_pass()
            self.store.ingest(self.page([item(i) for i in range(1, 6)]))
        self.store.analyze(self.search)
        self.assertFalse(self.store.deals())
        self.assertEqual(len(self.store.raw()), 6)

    def test_excel_contains_links_history_status(self):
        from openpyxl import load_workbook
        self.store.ingest(self.page([item(i, 35000 if i == 6 else 50000) for i in range(1, 7)]))
        m = Monitor(self.search, None, lambda *a, **kw: None, self.directory)
        try:
            path = m.export()
        finally:
            m.close()
        wb = load_workbook(path)
        self.assertIn('История цен', wb.sheetnames)
        self.assertIn('Состояние сбора', wb.sheetnames)
        self.assertTrue(wb['Выгодные']['H2'].hyperlink.target.endswith('mac_6'))
        self.assertEqual(wb['Выгодные']['D2'].value, 15000)
        wb.close()


class BoundaryTests(unittest.TestCase):
    def test_missing_catalog_is_error(self):
        with self.assertRaises(ValueError):
            avito_client.page_from_payload({'error': 'temporary'}, 1)

    def test_missing_counters_do_not_end_nonempty_page(self):
        # Incomplete responses are rejected instead of marking a pass complete.
        with self.assertRaises(ValueError):
            avito_client.page_from_payload({'catalog': {'items': [item(1)]}}, 1)
        self.assertFalse(avito_client.Page(1, (item(1),), 1000, 2).is_last)

    def test_invalid_token_safe_error(self):
        with self.assertRaises(telegram_notify.TelegramError) as e:
            telegram_notify._call_once('curl -Ls secret', 'getMe', {})
        self.assertNotIn('secret', str(e.exception))

    def test_bot_rejects_other_chat_and_accepts_deals(self):
        browser = DealBrowser(telegram_notify.Notifier('123:' + 'a'*25, 42), [], lambda *a, **kw: None)
        with patch.object(browser, 'menu') as menu:
            browser.handle({'message': {'chat': {'id': 99}, 'text': '/deals'}})
            menu.assert_not_called()
            browser.handle({'message': {'chat': {'id': 42}, 'text': '/deals'}})
            menu.assert_called_once()

    def test_process_lock(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d)/'lock'
            with exclusive(path):
                with self.assertRaises(RuntimeError):
                    with exclusive(path):
                        pass
            with exclusive(path):
                pass



class DetailTests(unittest.TestCase):
    def test_description_fills_title_gaps(self):
        identity = product_identity.identify('MacBook Pro 13 m1', 'В продаже MacBook Pro 13 M1 (2020), 16/1 TB. Оригинал.')
        self.assertTrue(identity.complete)
        self.assertEqual(identity.storage, '1024')
        self.assertEqual(identity.ram, '16')

    def test_conflicting_memory_not_valued(self):
        identity = product_identity.identify('Macbook Pro 13 2020 M1 8/256', 'Macbook Pro 13 2020 M1 16/512')
        self.assertFalse(identity.complete)
        self.assertIn('противоречивые', identity.reason)

    def test_html_description_and_parameters(self):
        import listing_details
        result = listing_details.parse('<div><div data-marker="item-view/item-description"><p>MacBook &amp; Apple</p><p>16 ГБ</p></div><ul data-marker="item-view/item-params"><li>SSD: 512 ГБ</li></ul></div><div>Похожие объявления 8/256</div>')
        self.assertIn('MacBook & Apple', result)
        self.assertIn('512', result)
        self.assertNotIn('8/256', result)
        self.assertIn('16 ГБ', result)

    def test_empty_html_is_not_success(self):
        import listing_details
        with self.assertRaises(ValueError):
            listing_details.parse('<html>Недоступно</html>')
        self.assertEqual(listing_details.parse('<script type="application/ld+json">{"@type":"Product","description":"Оригинал"}</script>'), 'Оригинал')

    def test_queue_retries_and_reenqueues_changed_items(self):
        with tempfile.TemporaryDirectory() as d:
            store = MarketStore(Path(d)/'market.sqlite')
            try:
                raw = item(1)
                store.ingest(avito_client.Page(1, (raw,), 1, 1))
                self.assertEqual(len(store.pending_details()), 1)
                store.save_details('1', raw, error='offline')
                self.assertEqual(store.pending_details(), [])
                with store.db:
                    store.db.execute('UPDATE details SET next_try=0')
                self.assertEqual(len(store.pending_details()), 1)
                store.save_details('1', raw, description='Полное описание')
                self.assertEqual(store.pending_details(), [])
                store.start_pass()
                store.ingest(avito_client.Page(1, (item(1, 30000),), 1, 1))
                # A changed listing must not wait a day for the old description to expire.
                self.assertEqual(len(store.pending_details()), 1)
            finally:
                store.close()

    def test_bot_buttons_have_avito_links_and_pagination(self):
        with tempfile.TemporaryDirectory() as d, patch.object(avito, 'DATA_DIR', Path(d)):
            s = search(Path(d))
            store = MarketStore(s.path('market.sqlite'))
            try:
                rows = [item(i, 40000 if i < 7 else 50000) for i in range(1, 20)]
                store.ingest(avito_client.Page(1, tuple(rows), 19, 19))
                store.analyze(s)
            finally:
                store.close()
            browser = DealBrowser(telegram_notify.Notifier('123:'+'a'*25, 42), [s], lambda *a, **kw: None)
            key = next(iter(browser.by_key))
            with patch.object(browser, 'send') as send:
                browser.show_deals(key, 0)
                text, keyboard = send.call_args.args
                self.assertLess(len(text), 4096)
                urls = [b['url'] for row in keyboard for b in row if 'url' in b]
                self.assertEqual(len(urls), 5)
                self.assertTrue(all(url.startswith('https://www.avito.ru/') for url in urls))
                self.assertTrue(any(b.get('callback_data') == f'd:{key}:1' for row in keyboard for b in row))


    def test_structured_labels_fill_configuration(self):
        identity = product_identity.identify('MacBook Pro 13 M1', 'Год выпуска: 2020. Оперативная память (ОЗУ): 8 ГБ. Накопитель (SSD): 256 ГБ.')
        self.assertTrue(identity.complete)
        self.assertFalse(product_identity.risk_reason('', 'Без неисправностей. Не залит, не разбит.'))

    def test_full_description_can_remove_deal(self):
        with tempfile.TemporaryDirectory() as d, patch.object(avito, 'DATA_DIR', Path(d)):
            s = search(Path(d))
            store = MarketStore(s.path('market.sqlite'))
            try:
                rows = [item(i, 35000 if i == 6 else 50000) for i in range(1, 7)]
                store.ingest(avito_client.Page(1, tuple(rows), 6, 6))
                store.analyze(s, enqueue=True)
                self.assertEqual(len(store.deals()), 1)
                store.save_details('6', rows[-1], description='Не работает, на запчасти')
                store.analyze(s, enqueue=True)
                self.assertEqual(store.deals(), [])
                with patch.object(telegram_notify.Notifier, 'send') as send:
                    store.deliver(telegram_notify.Notifier('123:'+'a'*25, 42), 'test', lambda *a,**kw:None)
                    send.assert_not_called()
            finally:
                store.close()


if __name__ == '__main__':
    unittest.main()
