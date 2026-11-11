"""Read the listing description and specification blocks without executing page scripts."""
import json
from html.parser import HTMLParser


class DetailsParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.depth = 0
        self.targets = []
        self.parts = []
        self.json_depth = None
        self.json_parts = []
        self.structured = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag not in ('br', 'hr', 'img', 'meta', 'input', 'link', 'source', 'wbr'):
            self.depth += 1
        marker = attrs.get('data-marker', '')
        if marker in ('item-view/item-description', 'item-view/item-params', 'item-view/params'):
            self.targets.append(self.depth)
        if self.targets and tag in ('br', 'li', 'p'):
            self.parts.append('\n')
        if tag == 'script' and attrs.get('type') == 'application/ld+json':
            self.json_depth = self.depth
            self.json_parts = []

    def handle_endtag(self, tag):
        if self.targets and tag in ('p', 'li', 'div'):
            self.parts.append('\n')
        if self.json_depth == self.depth and tag == 'script':
            try:
                self.structured.append(json.loads(''.join(self.json_parts)))
            except ValueError:
                pass
            self.json_depth = None
        if self.targets and self.targets[-1] == self.depth:
            self.targets.pop()
        if tag not in ('br', 'hr', 'img', 'meta', 'input', 'link', 'source', 'wbr'):
            self.depth = max(0, self.depth-1)

    def handle_data(self, data):
        if self.json_depth is not None:
            self.json_parts.append(data)
        elif self.targets:
            self.parts.append(data.strip() + ' ' if data.strip() else data)


def parse(document):
    parser = DetailsParser()
    parser.feed(document)
    text = ''.join(parser.parts).strip()
    if not text:
        pending = list(parser.structured)
        while pending:
            value = pending.pop()
            if isinstance(value, list):
                pending.extend(value)
            elif isinstance(value, dict):
                if value.get('@type') in ('Product', 'IndividualProduct') and isinstance(value.get('description'), str):
                    text = value['description']
                    break
                pending.extend(v for v in value.values() if isinstance(v, (dict, list)))
    if not text:
        raise ValueError('Не удалось прочитать полное описание. Объявление осталось в очереди повторов.')
    return text[:100000]
