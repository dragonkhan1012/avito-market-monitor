"""Conservative product identities: unknown characteristics never become known ones."""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass


def clean(text: str) -> str:
    text = unicodedata.normalize('NFKC', text).casefold().replace('ё', 'е')
    for old, new in [('макбук', 'macbook'), ('мак бук', 'macbook'), ('mac book', 'macbook'),
                     ('айфон', 'iphone'), ('гб', 'gb'), ('тб', 'tb')]:
        text = text.replace(old, new)
    return re.sub(r'\s+', ' ', text).strip()


def one(pattern: str, text: str) -> str:
    values = {re.sub(r'\s+', ' ', m.group(1)).strip() for m in re.finditer(pattern, text)}
    return next(iter(values)) if len(values) == 1 else ''


def capacity(value: str, unit: str = 'gb') -> str:
    return str(round(float(value.replace(',', '.')) * (1024 if unit == 'tb' else 1)))


@dataclass(frozen=True)
class Identity:
    family: str
    model: str
    chip: str = ''
    year: str = ''
    screen: str = ''
    ram: str = ''
    storage: str = ''
    condition: str = 'б/у или не указано'
    complete: bool = False
    reason: str = ''

    @property
    def label(self) -> str:
        if self.family == 'macbook':
            return ' · '.join([self.model, self.year or 'год?', self.screen + '″' if self.screen else 'экран?',
                              self.chip.upper() or 'процессор?',
                              (self.ram or '?') + ' ГБ ОЗУ', (self.storage or '?') + ' ГБ SSD', self.condition])
        if self.family == 'iphone':
            return f'{self.model}, {self.storage or "?"} ГБ · {self.condition}'
        return f'{self.model} · {self.condition}'


def identify(title: str, description: str = '') -> Identity:
    t, d = clean(title), clean(description)
    condition_title = re.sub(r'как\s+нов(?:ый|ая|ое)', '', t)
    condition = 'новое' if re.search(r'\b(новый|новая|новое|запечатан\w*|не распакован\w*)\b', condition_title) else 'б/у или не указано'
    # Parse configuration only from title or explicit labels; do not infer from the search query.
    if 'macbook' in t:
        # A description's opening device specification can fill gaps in its title.
        # Later marketing text and lists of devices accepted for trade-in cannot.
        opening = re.split(r'[!;\n]', description, maxsplit=1)[0][:350]
        opening = clean(opening)
        specs = t
        title_models = set(re.findall(r'\bmacbook\s+(pro|air)\b', t))
        description_models = set(re.findall(r'\bmacbook\s+(pro|air)\b', opening))
        if description_models and description_models == title_models:
            specs += ' ' + opening
        model = one(r'\bmacbook\s+(pro|air)\b', specs)
        chip_pattern = r'\b(m[1-9](?:\s+(?:pro|max|ultra))?|i[3579](?:-\d{4,5}[a-z0-9]*)?)\b'
        chip = one(chip_pattern, specs)
        if not chip:
            chip = one(r'(?:процессор[а-я]*|processor|cpu)\s*[:—-]?\s*(?:intel\s+core\s+|apple\s+)?(m[1-9](?:\s+(?:pro|max|ultra))?|i[3579](?:-\d{4,5}[a-z0-9]*)?)\b', d)
        year = one(r'\b(20(?:0[6-9]|[12]\d|3\d))\b', specs)
        if not year:
            year = one(r'(?:год(?:\s+выпуска|\s+модели)?|модельный\s+год)\s*[:—-]?\s*(20\d{2})\b', d)
        screen = one(r'\bmacbook\s+(?:pro|air)\s+(1[23456](?:[.,]\d)?)\b', specs)
        if not screen:
            screen = one(r'\b(1[23456](?:[.,]\d)?)\s*(?:дюйм|inch|["″])', specs)
        if not screen:
            screen = one(r'(?:диагональ(?:\s+экрана)?|экран)\s*[:—-]?\s*(1[23456](?:[.,]\d)?)\b', d)
        screen = screen.replace(',', '.')
        ram_patterns = [r'(?:ram|озу|оперативн\w*(?:\s+память)?)(?:\s*\((?:ram|озу)\))?\s*[:—-]?\s*(\d+)\s*(?:gb)?\b',
                        r'\b(\d+)\s*gb\s*(?:ram|озу|оперативн)']
        ram_values = {m[1] for pattern in ram_patterns for m in re.finditer(pattern, t + ' ' + d)}
        capacities = {capacity(m[1], m[2]) for m in re.finditer(r'\b(\d+(?:[.,]\d+)?)\s*(gb|tb)\b', specs)}
        small = {v for v in capacities if 4 <= int(v) <= 128}
        if not ram_values:
            ram_values = small
        storage_values = {v for v in capacities if int(v) >= 128 and v not in ram_values}
        pair_pattern = r'\b(4|8|16|18|24|32|36|48|64|96|128)\s*(?:[/\\]|gb\s*[/, -]?)\s*(128|256|512|1024|2048|1\s*tb|2\s*tb)\b'
        pairs = {(m[1], capacity(re.sub(r'\s*tb', '', m[2]), 'tb' if 'tb' in m[2] else 'gb')) for m in re.finditer(pair_pattern, specs)}
        for pair_ram, pair_ssd in pairs:
            ram_values.add(pair_ram)
            storage_values.add(pair_ssd)
        storage_patterns = [r'(?:ssd|накопитель|storage)(?:\s*\((?:storage|ssd)\))?\s*[:—-]?\s*(\d+(?:[.,]\d+)?)\s*(gb|tb)',
                            r'\b(\d+(?:[.,]\d+)?)\s*(gb|tb)\s*ssd']
        for pattern in storage_patterns:
            storage_values.update(capacity(m[1], m[2]) for m in re.finditer(pattern, t + ' ' + d))
        ram = next(iter(ram_values)) if len(ram_values) == 1 else ''
        storage = next(iter(storage_values)) if len(storage_values) == 1 else ''
        conflicts = []
        if len(ram_values) > 1 or len(storage_values) > 1:
            conflicts.append('противоречивые сведения о памяти')
        if len({m[1] for m in re.finditer(chip_pattern, specs)}) > 1:
            conflicts.append('несколько процессоров')
        missing = [label for label, val in [('серия', model), ('год', year), ('экран', screen), ('процессор', chip), ('ОЗУ', ram), ('SSD', storage)] if not val]
        missing.extend(conflicts)
        # A bare Intel i5/i7 is not a precise processor configuration.
        if chip in ('i3', 'i5', 'i7', 'i9'):
            missing.append('точная модель Intel')
        return Identity('macbook', 'MacBook ' + (model.title() or '?'), chip, year, screen, ram, storage,
                        condition, not missing, 'Не уточнены: ' + ', '.join(missing) if missing else '')
    phone = re.search(r'\biphone\s*(se(?:\s*20\d{2})?|\d{1,2}(?:\s*(?:pro\s*max|pro|plus|mini))?)\b', t)
    if phone:
        memory = {capacity(m[1], m[2]) for m in re.finditer(r'\b(\d+(?:[.,]\d+)?)\s*(gb|tb)\b', t)}
        storage = next(iter(memory)) if len(memory) == 1 else ''
        model = 'iPhone ' + re.sub(r'\s+', ' ', phone[1]).title()
        # SE without generation is ambiguous.
        complete = bool(storage) and phone[1] != 'se'
        return Identity('iphone', model, storage=storage, condition=condition, complete=complete,
                        reason='' if complete else 'Не уточнены память или поколение')
    model = re.sub(r'[^\w\s.+/-]', ' ', t.split(',')[0])
    model = re.sub(r'\s+', ' ', model).strip()
    return Identity('generic', model or 'Неизвестный товар', condition=condition,
                    reason='Для этой категории нужна проверка характеристик; автоматическая оценка отключена')


def matches_query(identity: Identity, query: str) -> bool:
    q = identify(query)
    if q.family == 'generic':
        return True  # the source search remains responsible for generic categories
    if q.family != identity.family or q.model != identity.model:
        return False
    return all(not getattr(q, k) or getattr(q, k) == getattr(identity, k)
               for k in ('year', 'screen', 'chip', 'ram', 'storage'))


def risk_reason(title: str, description: str) -> str:
    text = clean(title + ' ' + description)
    text = re.sub(r'(?:без\s+неисправност\w*|неисправност\w*\s+нет|не\s+(?:залит|утоплен|разбит|треснут)\w*)', '', text)
    text = re.sub(r'(?:без|нет)\s+(?:каких-либо\s+)?блокиров\w*|не\s+заблокирован\w*|не\s+бит(?:ый|ая|ое)|icloud\s*(?:чист\w*|отвязан\w*)|(?:вышел|выйду)\s+из\s+icloud', '', text)
    if re.search(r'на\s+запчаст|на\s+разбор|под\s+восстановл|не\s+включ|не\s+работа|неисправ|нерабоч|разбит|треснут|утоплен|залит|заблок|блокировк|донор|icloud\s*(?:lock|блок)|(?<!не )бит(?:ый|ая|ое)\b', text):
        return 'Неисправность или блокировка по описанию'
    if re.search(r'\b(?:чехол|корпус|запчасти|матрица|зарядка|клавиатура|плата)\s+(?:для|на)\b|\b(?:куплю|скупка|ремонт)\b', clean(title)):
        return 'Аксессуар, услуга или покупка, а не продажа устройства'
    return ''
