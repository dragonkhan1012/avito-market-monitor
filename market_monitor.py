"""Resumable, full-catalogue goods monitoring with durable analysis and exports."""
from __future__ import annotations

import time
from contextlib import ExitStack
from datetime import datetime

import avito_client
import goods
import product_identity
import telegram_notify
from market_store import MarketStore
from process_lock import exclusive
from telegram_browser import DealBrowser


class Monitor:
    def __init__(self, search, client, log, output_dir, *, enqueue=False):
        self.search, self.client, self.log, self.output_dir = search, client, log, output_dir
        self.enqueue = enqueue
        self.store = MarketStore(search.path('market.sqlite'))
        self.store.import_legacy(search.load('listings.json'), search.load('seen.json'))
        self.store.set('expected_total', search.expected_total)
        self.store.set('max_age_days', search.market_max_age_days)
        self.last_export = 0
        self.last_exported_deals = None

    def close(self):
        self.store.close()

    def analyze(self):
        offers = self.store.analyze(self.search, enqueue=self.enqueue)
        for fields in self.store.get('new_deals', []):
            self.log('ALERT', search=self.search.name, **fields)
        return offers

    def sync_legacy(self):
        self.search.save('listings.json', self.store.raw())
        self.search.save('seen.json', self.store.seen())
        self.search.save('state.json', self.store.state())

    def enrich(self, limit=None):
        import listing_details
        if not callable(getattr(self.client, 'get_document', None)):
            return
        for key, raw in self.store.pending_details()[:limit or self.search.details_per_cycle]:
            try:
                listing = goods.offer_from_item(raw).listing
                document = self.client.get_document(listing.url, catalog_url=self.search.catalog_url)
                if not document:
                    raise ValueError('Страница объявления пока недоступна')
                description = listing_details.parse(document)
            except (RuntimeError, ValueError, TypeError, KeyError, AttributeError, *avito_client.TRANSPORT_ERRORS) as exc:
                self.store.save_details(key, raw, error=exc)
                self.log('ERROR', search=self.search.name, error=f'Описание {key}: {type(exc).__name__}; повторим позже')
            else:
                self.store.save_details(key, raw, description=description)

    def step(self, *, head=True):
        state = self.store.state()
        if state.get('retry_at', 0) > time.time():
            return False
        state = self.store.start_pass()
        number = state['next_page']
        try:
            # The first page is refreshed between deeper pages, so a long sweep still notices arrivals.
            if head and number > 1:
                page = self.client.page(catalog_url=self.search.catalog_url, params=self.search.request_params, number=1)
                self.store.ingest(page, advance=False)
            page = self.client.page(catalog_url=self.search.catalog_url, params=self.search.request_params, number=number)
            if page.number != number:
                raise ValueError('Ответ относится к другой странице; прогресс не изменён')
            self.store.ingest(page)
        except (RuntimeError, ValueError, TypeError, KeyError, *avito_client.TRANSPORT_ERRORS) as exc:
            self.store.failed(f'{type(exc).__name__}: {str(exc)[:200]}')
            self.log('ERROR', search=self.search.name, error=self.store.state()['error'])
            # Anything saved by the head refresh still receives analysis.
            self.analyze()
            self.sync_legacy()
            return False
        self.enrich()
        self.analyze()
        self.sync_legacy()
        state = self.store.state()
        current_deals = tuple(
            (deal['id'], deal['price_value'], deal['market_value'])
            for deal in self.store.deals()
        )
        deals_changed = current_deals != self.last_exported_deals
        self.log('PAGE', search=self.search.name, page=number, items=len(page.items), new=0, total=page.total_count)
        if state.get('error'):
            self.log('ERROR', search=self.search.name, error=state['error'])
        if self.store.get('analysis_errors'):
            self.log('ERROR', search=self.search.name, error=f"Не обработано объявлений: {len(self.store.get('analysis_errors'))}. Они сохранены для повторного анализа.")
        if deals_changed or state.get('complete') or time.time() - self.last_export >= self.search.export_interval:
            try:
                self.export()
            except (OSError, ValueError) as exc:
                self.log('ERROR', search=self.search.name, error=f'Excel не обновлён: {exc}. Данные сохранены в базе.')
        return True

    def export(self):
        from openpyxl import load_workbook
        from openpyxl.styles import Alignment, Font, PatternFill
        offers = self.store.analyze(self.search, enqueue=False)
        # Summary reflects only fresh, comparable local offers matching the chosen product.
        cutoff = time.time() - self.search.market_max_age_days * 86400
        live = {r[0] for r in self.store.db.execute('SELECT id FROM items WHERE last_seen>=? AND missed<2', (cutoff,))}
        market = [o for o in offers if str(o.listing.id) in live and o.city == self.search.home_city
                  and not o.delivery_only
                  and (self.search.alert_sellers != 'private' or (o.seller == 'частное лицо' and (o.listing.closed_listings or 0) < goods.SHOP_CLOSED_LISTINGS))
                  and o.identity and product_identity.matches_query(o.identity, self.search.params.get('q', ''))]
        path = self.output_dir / f'{self.search.name}-monitor.xlsx'
        temporary = path.with_name(path.stem + '.building.xlsx')
        goods.write_workbook(temporary, offers, goods.market_stats(market))
        workbook = load_workbook(temporary)
        if not any(o.eligible for o in offers):
            best = workbook["Выгодные"]
            best.merge_cells("A3:H3")
            best["A3"] = "Подтверждённых ценой выгодных предложений пока нет. Причины по каждому товару — в листе «Объявления». Неполный сбор или недостаток характеристик виден в листе «Состояние сбора»."
            best["A3"].alignment = Alignment(wrap_text=True, vertical='center')
            best.row_dimensions[3].height = 52
        history = workbook.create_sheet('История цен')
        history.append(['Объявление', 'Когда замечено', 'Цена, ₽', 'Ссылка на Авито'])
        mapping = {str(o.listing.id): o for o in offers}
        for row in self.store.db.execute('SELECT * FROM prices ORDER BY observed DESC'):
            offer = mapping.get(row['item_id'])
            history.append([offer.listing.title if offer else row['item_id'], datetime.fromtimestamp(row['observed']), row['price'], offer.listing.url if offer else ''])
            history.cell(history.max_row, 2).number_format = 'dd.mm.yyyy hh:mm'
            if offer:
                cell = history.cell(history.max_row, 4)
                cell.hyperlink, cell.value, cell.style = offer.listing.url, 'Открыть на Авито', 'Hyperlink'
        status = workbook.create_sheet('Состояние сбора')
        status.append(['Показатель', 'Значение'])
        state = self.store.state()
        for row in [('Обновлено', datetime.now().strftime('%d.%m.%Y %H:%M')),
                    ('Обход доступной выдачи', 'Завершён' if state.get('complete') else 'Не завершён'),
                    ('Следующая страница', state['next_page']), ('Собрано за этот проход', state.get('collected', 0)),
                    ('Авито сообщил объявлений', state.get('expected', 0)),
                    ('Описаний в очереди сейчас', len(self.store.pending_details())),
                    ('Не удалось прочитать описание', self.store.db.execute('SELECT COUNT(*) FROM details WHERE error IS NOT NULL').fetchone()[0]),
                    ('Нужно повторить анализ', len(self.store.get('analysis_errors', {}))),
                    ('Проблема', state.get('error', '')),
                    ('Минимум похожих объявлений', self.search.min_analogs),
                    ('Минимальная скидка', f'{self.search.deal_discount:.0%}'),
                    ('Минимальная разница в рублях', self.search.deal_min_saving),
                    ('Давность наблюдения, дней', self.search.market_max_age_days),
                    ('Правило сравнения', 'Точная конфигурация и город. Само объявление исключено из расчёта.'),
                    ('Ограничение', 'Проверяется доступная выдача. Объявления могут исчезнуть или измениться между запросами.')]:
            status.append(row)
        for key, error in self.store.get('analysis_errors', {}).items():
            status.append([f'Ошибка объявления {key}', error])
        for tab in (history, status):
            tab.freeze_panes = 'A2'
            tab.auto_filter.ref = tab.dimensions
            tab.sheet_view.showGridLines = False
            for col, width in [('A', 55), ('B', 60 if tab == status else 24), ('C', 18), ('D', 28)]:
                tab.column_dimensions[col].width = width
            for cell in tab[1]:
                cell.font = Font(color='FFFFFF', bold=True)
                cell.fill = PatternFill('solid', fgColor='17365D')
            for row in tab:
                for cell in row:
                    cell.alignment = Alignment(wrap_text=True, vertical='top')
                    if cell.data_type == 'f':
                        cell.data_type = 's'
                tab.row_dimensions[row[0].row].height = 34 if tab == status else 42
            tab.row_dimensions[1].height = 28
        workbook.save(temporary)
        workbook.close()
        temporary.replace(path)
        self.last_export = time.time()
        self.last_exported_deals = tuple(
            (deal['id'], deal['price_value'], deal['market_value'])
            for deal in self.store.deals()
        )
        self.log('TABLE', search=self.search.name, path=str(path), **{'объявлений': len(offers), 'выгодных': sum(o.eligible for o in offers)})
        return path


def scan(search, args, log, output_dir):
    with exclusive(search.path('monitor.lock')):
        monitor = Monitor(search, avito_client.CalmClient(delay=args.delay, log=log), log, output_dir)
        try:
            if args.full:
                monitor.store.start_pass(force=True)
            log('SCAN', search=search.name, start_page=monitor.store.start_pass()['next_page'])
            for _ in range(max(1, args.max_pages)):
                succeeded = False
                for attempt in range(3):
                    wait = monitor.store.state().get('retry_at', 0) - time.time()
                    if wait > 0:
                        time.sleep(wait)
                    if monitor.step(head=False):
                        succeeded = True
                        break
                if not succeeded or monitor.store.state().get('complete') or monitor.store.state().get('restart'):
                    break
            # Finish detail reads for the collected catalogue, including offers from earlier scans.
            while monitor.store.pending_details():
                pending_before = len(monitor.store.pending_details())
                monitor.enrich()
                monitor.store.analyze(search, enqueue=False)
                if len(monitor.store.pending_details()) >= pending_before:
                    break
        except KeyboardInterrupt:
            log('ERROR', search=search.name, error='Остановлено. Страница и объявления сохранены; следующий запуск продолжит обход.')
        finally:
            try:
                if monitor.store.raw():
                    monitor.export()
            finally:
                monitor.close()


def watch(searches, args, log, output_dir):
    notifier = None if args.no_telegram else telegram_notify.Notifier.from_settings()
    with ExitStack() as stack:
        if notifier:
            stack.enter_context(exclusive(telegram_notify.SETTINGS_PATH.with_name('telegram-poll.lock')))
        monitors = []
        for search in searches:
            stack.enter_context(exclusive(search.path('monitor.lock')))
            monitor = Monitor(search, avito_client.CalmClient(delay=args.delay, log=log), log, output_dir, enqueue=True)
            stack.callback(monitor.close)
            monitor.analyze()
            monitors.append(monitor)
        # One paced network client shared across searches prevents multiplying the request rate.
        for monitor in monitors[1:]:
            monitor.client = monitors[0].client
        browser = DealBrowser(notifier, searches, log) if notifier else None
        if browser:
            browser.start()
            stack.callback(browser.close)
        log('WATCH', searches=[s.name for s in searches], interval_s=args.interval, telegram=bool(notifier))
        try:
            while True:
                for monitor in monitors:
                    for index in range(max(1, monitor.search.watch_pages)):
                        monitor.step(head=index == 0)
                        if monitor.store.state().get('complete') or monitor.store.state().get('retry_at', 0) > time.time():
                            break
                time.sleep(max(1, args.interval))
        except KeyboardInterrupt:
            log('ERROR', search='', error='Мониторинг остановлен; прогресс, ссылки и очередь уведомлений сохранены.')
        finally:
            for monitor in monitors:
                try:
                    monitor.export()
                except (OSError, ValueError) as exc:
                    log('ERROR', search=monitor.search.name, error=f'Excel: {exc}')
