"""Goods profile: group listings by model and memory, compute market prices.

Avito titles in structured categories look like "iPhone 15, 128 ГБ, SIM +
eSIM": the first part is the model, one part is the storage. A group is
"model + storage"; its market price is the median after dropping extreme
prices, which are usually broken devices, parts, scams or typos.
"""

from __future__ import annotations

import re
import statistics
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import apartments
import product_identity

MEMORY_PATTERN = re.compile(r"^\d+\s*(?:ГБ|ТБ|GB|TB)$", re.IGNORECASE)
# Prices outside [LOW, HIGH] x raw median are left out of the market price.
OUTLIER_LOW = 0.5
OUTLIER_HIGH = 1.8
CHEAP_SHARE = 0.85
MIN_GROUP_SIZE = 3
# A "private" seller with this many closed listings is a shop or a reseller.
SHOP_CLOSED_LISTINGS = 100

# Locked or broken devices are sold far below market; they are marked and
# left out of the market price.
DEFECT_PATTERN = re.compile(
    r"(?<!не\s)бит(?:ый|ая|ое|ые)\b|заблок|блокировк|icloud|айклауд|аклауд|на\s+запчаст|на\s+разбор|под\s+восстановл|"
    r"не\s+включ|не\s+работает|разбит|треснут|утоплен|после\s+воды|залит|"
    r"не\s+ловит|плата\s+мертв|донор",
    re.IGNORECASE,
)


@dataclass
class Offer:
    listing: apartments.Listing
    model: str
    memory: str
    seller: str
    delivery_only: bool
    avito_price_badge: str
    city: str = ""
    defect: bool = False
    market: int | None = None
    vs_market: float | None = None
    notes: list[str] = field(default_factory=list)
    identity: product_identity.Identity | None = None
    eligible: bool = False
    analogs: int = 0
    saving: int = 0
    decision: str = "Не оценено"

    @property
    def group(self) -> str:
        return self.identity.label if self.identity else (f"{self.model}, {self.memory}" if self.memory else self.model)


def offer_from_item(item: dict[str, Any]) -> Offer:
    listing = apartments.listing_from_item(item)
    parts = [part.strip() for part in listing.title.split(",") if part.strip()]
    model = parts[0] if parts else listing.title
    memory = next((part for part in parts[1:] if MEMORY_PATTERN.match(part)), "")
    identity = product_identity.identify(listing.title, listing.description)
    badges = listing.avito_badges
    price_badge = next(
        (badge for badge in badges if "рыночн" in badge.lower()),
        "",
    )
    return Offer(
        listing=listing,
        model=model,
        memory=memory,
        seller=listing.seller_label or "частное лицо",
        delivery_only="Только доставка" in badges,
        avito_price_badge=price_badge,
        city=str(item.get("urlPath") or "").strip("/").split("/")[0],
        identity=identity,
        defect=bool(product_identity.risk_reason(listing.title, listing.description)),
    )


@dataclass(frozen=True)
class GroupStats:
    group: str
    count: int
    used: int
    low: int
    q1: int
    median: int
    q3: int
    high: int


def market_stats(offers: list[Offer]) -> dict[str, GroupStats]:
    groups: dict[str, list[int]] = {}
    for offer in offers:
        if offer.listing.price and offer.listing.price > 0 and not offer.defect and (not offer.identity or offer.identity.complete):
            groups.setdefault(offer.group, []).append(offer.listing.price)
    result = {}
    for group, prices in groups.items():
        if len(prices) < MIN_GROUP_SIZE:
            continue
        raw_median = statistics.median(prices)
        kept = sorted(
            price for price in prices
            if OUTLIER_LOW * raw_median <= price <= OUTLIER_HIGH * raw_median
        )
        if len(kept) < MIN_GROUP_SIZE:
            continue
        q1, median, q3 = statistics.quantiles(kept, n=4)
        result[group] = GroupStats(
            group=group, count=len(prices), used=len(kept),
            low=kept[0], q1=round(q1), median=round(median), q3=round(q3), high=kept[-1],
        )
    return result


def annotate(
    offers: list[Offer], stats: dict[str, GroupStats], home_city: str = ""
) -> None:
    for offer in offers:
        group = stats.get(offer.group)
        offer.notes = []
        offer.market = offer.vs_market = None
        if offer.defect:
            offer.notes.append("заблокирован/неисправен (по описанию)")
        if group and offer.listing.price:
            offer.market = group.median
            offer.vs_market = offer.listing.price / group.median - 1
            if not offer.defect and offer.listing.price < OUTLIER_LOW * group.median:
                offer.notes.append("подозрительно дёшево: битый, запчасти или обман?")
            elif not offer.defect and offer.listing.price <= CHEAP_SHARE * group.median:
                offer.notes.append("дешевле рынка")
        if offer.seller != "частное лицо":
            offer.notes.append(f"продавец: {offer.seller}")
        elif (offer.listing.closed_listings or 0) >= SHOP_CLOSED_LISTINGS:
            offer.notes.append(
                f"похоже на магазин/перекупа: {offer.listing.closed_listings} завершённых"
            )
        if offer.delivery_only:
            offer.notes.append("только доставка")
        if home_city and offer.city and offer.city != home_city:
            offer.notes.append(f"другой город: {offer.city}")


def evaluate_offers(offers: list[Offer], *, home_city: str = "", query: str = "",
                    min_analogs: int = 5, discount: float = 0.15, min_saving: int = 3000,
                    alert_sellers: str = "private") -> None:
    """Leave-one-out prices of matching, non-defective offers in the same city.

    These are asking prices, not confirmed sales or guaranteed profit.
    """
    grouped: dict[tuple[str, str], list[Offer]] = {}
    for offer in offers:
        if (offer.identity and offer.identity.complete and not offer.defect and not offer.delivery_only
                and (offer.listing.price or 0) > 0
                and (alert_sellers != "private" or (offer.seller == "частное лицо" and (offer.listing.closed_listings or 0) < SHOP_CLOSED_LISTINGS))):
            grouped.setdefault((offer.city, offer.group), []).append(offer)
    for offer in offers:
        offer.notes = []
        offer.market = offer.vs_market = None
        offer.eligible, offer.analogs, offer.saving = False, 0, 0
        offer.decision = "Недостаточно похожих предложений"
        risk = product_identity.risk_reason(offer.listing.title, offer.listing.description)
        if risk:
            offer.notes.append(risk)
        if offer.identity and not offer.identity.complete:
            offer.notes.append(offer.identity.reason)
        if not offer.identity or not offer.identity.complete:
            offer.decision = "Нужно уточнить характеристики"
            continue
        if len(offer.listing.url.split("/", 3)[-1]) < 4:
            offer.decision = "Нет ссылки на объявление"
            continue
        if query and not product_identity.matches_query(offer.identity, query):
            offer.decision = "Не соответствует выбранному товару"
            continue
        if risk:
            offer.decision = "Нужна проверка состояния"
            continue
        if not offer.listing.price or offer.listing.price <= 0:
            offer.decision = "Нет корректной цены"
            continue
        # ID exclusion also protects callers that append an existing offer to the pool.
        prices = [o.listing.price for o in grouped.get((offer.city, offer.group), [])
                  if o.listing.id != offer.listing.id]
        if not prices:
            continue
        center = statistics.median(prices)
        prices = sorted(p for p in prices if OUTLIER_LOW * center <= p <= OUTLIER_HIGH * center)
        offer.analogs = len(prices)
        if len(prices) < min_analogs:
            continue
        q1, _, q3 = statistics.quantiles(prices, n=4, method="inclusive")
        median = round(statistics.median(prices))
        if (q3 - q1) / median > 0.5:
            offer.decision = "Слишком большой разброс цен — нужна проверка группы"
            continue
        offer.market = median
        offer.vs_market = offer.listing.price / median - 1
        offer.saving = median - offer.listing.price
        if offer.listing.price < OUTLIER_LOW * median:
            offer.decision = "Подозрительно низкая цена"
        elif home_city and offer.city != home_city:
            offer.decision = "Другой город"
        elif alert_sellers == "private" and (offer.seller != "частное лицо" or (offer.listing.closed_listings or 0) >= SHOP_CLOSED_LISTINGS):
            offer.decision = "Продавец не подходит под настройки"
        elif offer.delivery_only:
            offer.decision = "Только доставка — проверьте условия"
        elif offer.vs_market <= -discount and offer.saving >= min_saving:
            offer.eligible = True
            offer.decision = "Выгодное предложение по цене"
        else:
            offer.decision = "Обычная или высокая цена"
        offer.notes.append(offer.decision)


COLUMNS = (
    ("Новое", 8), ("Группа", 22), ("Цена, ₽", 11), ("Обычная цена похожих, ₽", 13),
    ("Разница с обычной ценой", 9), ("Оценка Авито", 18), ("Заметки", 40), ("Продавец", 14),
    ("Завершённых объявлений", 11), ("Опубликовано", 14), ("Адрес", 30),
    ("Заголовок", 34), ("Ссылка", 10), ("Описание", 70),
)


def write_workbook(path: Path, offers: list[Offer], stats: dict[str, GroupStats]) -> None:
    from openpyxl import Workbook
    from openpyxl.formatting.rule import FormulaRule
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.worksheet.table import Table, TableStyleInfo
    from openpyxl.utils import get_column_letter

    navy, pale_blue, pale_green, pale_yellow, white = "17365D", "D9EAF7", "E2F0D9", "FFF2CC", "FFFFFF"
    thin_gray = Side(style="thin", color="D9E1F2")
    workbook = Workbook()
    market = workbook.active
    market.title = "Рынок"
    market.sheet_view.showGridLines = False
    market.merge_cells("A1:H1")
    market["A1"] = "ОБЗОР РЫНКА И ОБЪЯВЛЕНИЙ"
    market["A1"].font = Font(name="Arial", size=16, bold=True, color=white)
    market["A1"].fill = PatternFill("solid", fgColor=navy)
    market["A1"].alignment = Alignment(vertical="center")
    market.row_dimensions[1].height = 30
    market.merge_cells("A2:H2")
    market["A2"] = f"Сформировано: {datetime.now():%d.%m.%Y %H:%M}  •  В выборке: {len(offers)} объявлений"
    market["A2"].font = Font(name="Arial", size=10, italic=True, color="5B6573")
    prices = [offer.listing.price for offer in offers if offer.listing.price]
    metrics = [
        ("Всего объявлений", len(offers)),
        ("Новые", sum(offer.listing.is_new for offer in offers)),
        ("Групп с оценкой рынка", len(stats)),
        ("Выгодных предложений", sum(o.eligible for o in offers)),
    ]
    for index, (label, value) in enumerate(metrics):
        col = index * 2 + 1
        market.merge_cells(start_row=4, start_column=col, end_row=4, end_column=col + 1)
        market.merge_cells(start_row=5, start_column=col, end_row=5, end_column=col + 1)
        title_cell, value_cell = market.cell(4, col, label.upper()), market.cell(5, col, value)
        title_cell.fill = PatternFill("solid", fgColor=pale_blue)
        title_cell.font = Font(name="Arial", size=9, bold=True, color=navy)
        value_cell.fill = PatternFill("solid", fgColor="F4F8FC")
        value_cell.font = Font(name="Arial", size=16, bold=True, color=navy)
        title_cell.alignment = value_cell.alignment = Alignment(horizontal="center", vertical="center")
        if label == "Цена посередине списка" and isinstance(value, int):
            value_cell.number_format = '#,##0 "₽"'
    market.row_dimensions[4].height, market.row_dimensions[5].height = 20, 32
    market.merge_cells("A7:H7")
    market["A7"] = "Ценовой диапазон по группам моделей"
    market["A7"].font = Font(name="Arial", size=12, bold=True, color=navy)
    market.append([])
    market.append(["Группа", "Объявлений", "В расчёте", "Мин. цена", "Четверть предложений дешевле", "Обычная цена (середина)", "Три четверти дешевле", "Макс. цена"])
    for row in sorted(stats.values(), key=lambda s: (-s.count, s.group)):
        market.append([row.group, row.count, row.used, row.low, row.q1, row.median, row.q3, row.high])
    for cell in market[9]:
        cell.fill = PatternFill("solid", fgColor=navy)
        cell.font = Font(name="Arial", size=10, bold=True, color=white)
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
    market.row_dimensions[9].height = 34
    for index, width in enumerate((28, 15, 14, 16, 19, 16, 19, 16), start=1):
        market.column_dimensions[get_column_letter(index)].width = width
    for row in market.iter_rows(min_row=10, min_col=4, max_col=8):
        for cell in row:
            cell.number_format = '#,##0 "₽"'
    if market.max_row >= 10:
        market_table = Table(displayName="MarketGroups", ref=f"A9:H{market.max_row}")
        market_table.tableStyleInfo = TableStyleInfo(name="TableStyleMedium2", showRowStripes=True, showColumnStripes=False)
        market.add_table(market_table)
    market.column_dimensions["A"].width = 68
    market.row_dimensions[9].height = 50
    for row in market.iter_rows(min_row=10):
        for cell in row:
            cell.alignment = Alignment(wrap_text=True, vertical="center")
        market.row_dimensions[row[0].row].height = 58
    market.freeze_panes = "B10"
    market.sheet_properties.pageSetUpPr.fitToPage = True
    market.page_setup.fitToWidth, market.page_setup.fitToHeight = 1, 0
    market.page_setup.orientation = "landscape"

    sheet = workbook.create_sheet("Объявления")
    sheet.sheet_view.showGridLines = False
    sheet.append([name for name, _ in COLUMNS])
    for cell in sheet[1]:
        cell.fill = PatternFill("solid", fgColor=navy)
        cell.font = Font(name="Arial", size=10, bold=True, color=white)
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        cell.border = Border(bottom=Side(style="medium", color="5B9BD5"))
    sheet.row_dimensions[1].height = 36
    for index, (_, width) in enumerate(COLUMNS, start=1):
        sheet.column_dimensions[get_column_letter(index)].width = width
    link_column = [name for name, _ in COLUMNS].index("Ссылка") + 1
    ordered = sorted(
        offers,
        key=lambda o: (0 if o.listing.is_new else 1, o.group, o.listing.price or 0),
    )
    for offer in ordered:
        listing = offer.listing
        sheet.append([
            "новое" if listing.is_new else "",
            offer.group,
            listing.price,
            offer.market,
            offer.vs_market,
            offer.avito_price_badge,
            "; ".join(offer.notes),
            offer.seller,
            listing.closed_listings,
            listing.published,
            apartments_short_address(listing),
            listing.title,
            listing.url,
            listing.description,
        ])
        link = sheet.cell(row=sheet.max_row, column=link_column)
        link.hyperlink = listing.url
        link.value = "открыть"
        link.font = Font(color="0563C1", underline="single")
        row = sheet.max_row
        sheet.cell(row, 3).number_format = '#,##0 "₽"'
        sheet.cell(row, 4).number_format = '#,##0 "₽"'
        sheet.cell(row, 5).number_format = '+0%;-0%;0%'
        for col in (7, 12, 14):
            sheet.cell(row, col).alignment = Alignment(wrap_text=True, vertical="top")
        for cell in sheet[row]:
            cell.border = Border(bottom=thin_gray)
    if sheet.max_row >= 2:
        offers_table = Table(displayName="Listings", ref=f"A1:N{sheet.max_row}")
        offers_table.tableStyleInfo = TableStyleInfo(name="TableStyleMedium2", showRowStripes=True, showColumnStripes=False)
        sheet.add_table(offers_table)
        sheet.conditional_formatting.add(f"A2:N{sheet.max_row}", FormulaRule(
            formula=['$A2="новое"'], fill=PatternFill("solid", fgColor=pale_green)))
        sheet.conditional_formatting.add(f"E2:E{sheet.max_row}", FormulaRule(
            formula=['AND(ISNUMBER($E2),$E2<=-15%)'], fill=PatternFill("solid", fgColor=pale_yellow)))
    sheet.column_dimensions["B"].width = 65
    for row in sheet.iter_rows(min_row=2):
        row[1].alignment = Alignment(wrap_text=True, vertical="top")
        sheet.row_dimensions[row[0].row].height = 72
    sheet.freeze_panes = "C2"
    sheet.sheet_properties.pageSetUpPr.fitToPage = True
    sheet.page_setup.fitToWidth, sheet.page_setup.fitToHeight = 1, 0
    sheet.page_setup.orientation = "landscape"
    best = workbook.create_sheet("Выгодные", 0)
    best.append(["Товар", "Цена, ₽", "Обычная цена похожих, ₽", "Дешевле на, ₽", "Дешевле на", "Похожих объявлений", "Почему выбрано", "Авито"])
    for offer in sorted((o for o in offers if o.eligible), key=lambda o: (o.vs_market or 0, -o.saving)):
        best.append([offer.listing.title, offer.listing.price, offer.market, offer.saving,
                     -(offer.vs_market or 0), offer.analogs, offer.decision, offer.listing.url])
        link = best.cell(best.max_row, 8)
        link.hyperlink, link.value, link.style = offer.listing.url, "Открыть на Авито", "Hyperlink"
        best.cell(best.max_row, 5).number_format = '0%'
        for col in (2, 3, 4):
            best.cell(best.max_row, col).number_format = '#,##0'
    explain = workbook.create_sheet("Пояснения")
    explain.append(["Показатель", "Что это означает"])
    for row in [
        ("Обычная цена", "Цена посередине списка похожих предложений. Дорогие и дешёвые крайности меньше влияют на неё, чем на среднее."),
        ("Четверть предложений дешевле", "25 из 100 похожих предложений стоят не дороже этой суммы. Это не скидка."),
        ("Три четверти дешевле", "75 из 100 похожих предложений стоят не дороже этой суммы. Это не скидка."),
        ("Выгодно", "Сравнение с другими объявлениями той же конфигурации и города; само объявление исключено из расчёта."),
        ("Цена", "Указанная продавцом цена, а не подтверждённая цена продажи. Прибыль не гарантируется."),
        ("Не хватает данных", "Объявления с неизвестными характеристиками сохранены, но не отмечаются как выгодные."),
        ("Актуальность", "Таблица из базы на момент выгрузки. Наличие и состояние товара нужно проверить у продавца."),
    ]:
        explain.append(row)
    for tab, widths in [(best, (42, 16, 20, 18, 18, 20, 40, 24)), (explain, (34, 110))]:
        tab.sheet_view.showGridLines = False
        tab.freeze_panes = "A2"
        tab.auto_filter.ref = tab.dimensions
        for i, width in enumerate(widths, 1):
            tab.column_dimensions[get_column_letter(i)].width = width
        for cell in tab[1]:
            cell.fill, cell.font = PatternFill("solid", fgColor=navy), Font(color=white, bold=True)
            cell.alignment = Alignment(wrap_text=True, vertical="center")
        tab.row_dimensions[1].height = 32
        for row in tab.iter_rows(min_row=2):
            for cell in row:
                cell.alignment = Alignment(wrap_text=True, vertical="top")
            tab.row_dimensions[row[0].row].height = 42
    # Seller text must remain literal text, never spreadsheet formulas.
    for tab in workbook:
        for row in tab:
            for cell in row:
                if cell.data_type == "f":
                    cell.data_type = "s"
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.stem + ".tmp.xlsx")
    workbook.save(temporary)
    temporary.replace(path)


def apartments_short_address(listing: apartments.Listing) -> str:
    return re.sub(r"^[^,]+,\s*", "", listing.address) if "," in listing.address else listing.address


def price_text(value: int | None) -> str:
    return f"{value:,} ₽".replace(",", " ") if value else "—"


def alert_fields(offer: Offer) -> dict[str, Any]:
    listing = offer.listing
    return {
        "id": listing.id,
        "group": offer.group,
        "price_value": listing.price,
        "market_value": offer.market,
        "saving": offer.saving,
        "discount": -(offer.vs_market or 0),
        "analogs": offer.analogs,
        "eligible": offer.eligible,
        "decision": offer.decision,
        "title": listing.title,
        "price": price_text(listing.price),
        "market": price_text(offer.market),
        "vs_market": f"{offer.vs_market:+.0%}" if offer.vs_market is not None else "",
        "avito_badge": offer.avito_price_badge,
        "seller": offer.seller,
        "closed_listings": listing.closed_listings,
        "notes": offer.notes,
        "published": listing.published,
        "address": apartments_short_address(listing),
        "url": listing.url,
    }
