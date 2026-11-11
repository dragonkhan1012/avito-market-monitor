"""Background free-proxy scraper, validator, and rotator for curl_cffi."""

from __future__ import annotations

import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Callable

from curl_cffi import requests

CUSTOM_PROXIES_FILE = Path(__file__).resolve().parent / "proxies.txt"

# Расширенные автообновляемые источники (с приоритетом RU и быстрых пулов)
FREE_PROXY_SOURCES = (
    # Приоритетные RU прокси
    ("http", "https://api.proxyscrape.com/v2/?request=displayproxies&protocol=http&timeout=4000&country=RU&ssl=all&anonymity=all"),
    ("socks5", "https://api.proxyscrape.com/v2/?request=displayproxies&protocol=socks5&timeout=4000&country=RU"),
    ("http", "https://www.proxy-list.download/api/v1/get?type=http&country=RU"),
    ("socks5", "https://www.proxy-list.download/api/v1/get?type=socks5&country=RU"),
    # Глобальные быстрые списки SOCKS5 и HTTP
    ("socks5", "https://api.proxyscrape.com/v2/?request=displayproxies&protocol=socks5&timeout=4000&country=all"),
    ("http", "https://api.proxyscrape.com/v2/?request=displayproxies&protocol=http&timeout=4000&country=all&ssl=yes&anonymity=elite"),
    ("http", "https://raw.githubusercontent.com/TheSpeedX/SOCKS-List/master/http.txt"),
    ("socks5", "https://raw.githubusercontent.com/TheSpeedX/SOCKS-List/master/socks5.txt"),
    ("http", "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/http.txt"),
    ("socks5", "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/socks5.txt"),
    ("socks5", "https://raw.githubusercontent.com/hookzof/socks5_list/master/proxy.txt"),
    ("http", "https://raw.githubusercontent.com/clarketm/proxy-list/master/proxy-list-raw.txt"),
    ("http", "https://raw.githubusercontent.com/ShiftyTR/Proxy-List/master/https.txt"),
)

TEST_URL = "https://www.avito.ru/favicon.ico"


class FreeProxyRotator:
    """Continuously collects proxies and rotates them every N requests without waiting."""

    def __init__(
        self,
        *,
        rotate_every: int = 3,
        refresh_interval: int = 180,
        max_workers: int = 60,
        target_pool_size: int = 30,
        check_timeout: float = 4.5,
        log: Callable[..., None] | None = None,
    ) -> None:
        self.rotate_every = rotate_every
        self.refresh_interval = refresh_interval
        self.max_workers = max_workers
        self.target_pool_size = target_pool_size
        self.check_timeout = check_timeout
        self.log = log or (lambda kind, **fields: None)

        self._lock = threading.Lock()
        self._refresh_lock = threading.Lock()
        self._working: list[str] = []
        self._bad: set[str] = set()
        self.current_proxy: str | None = None
        self.requests_on_current: int = 0
        self._stop_event = threading.Event()

        # Первичное наполнение пула
        self.refresh_pool(initial=True)

        # Постоянная фоновая докачка
        self._thread = threading.Thread(target=self._background_loop, daemon=True)
        self._thread.start()

    def _load_custom_proxies(self) -> list[str]:
        """Подгружает свои прокси из файла proxies.txt, если он создан."""
        if not CUSTOM_PROXIES_FILE.exists():
            return []
        custom: list[str] = []
        try:
            for line in CUSTOM_PROXIES_FILE.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or ":" not in line:
                    continue
                if "://" not in line:
                    line = f"http://{line}"
                if line not in self._bad:
                    custom.append(line)
        except OSError:
            pass
        return custom

    def _fetch_candidates(self) -> list[str]:
        candidates: list[str] = self._load_custom_proxies()
        seen: set[str] = set(candidates)
        scraped: list[str] = []

        for scheme, url in FREE_PROXY_SOURCES:
            try:
                resp = requests.get(url, timeout=6, trust_env=False)
                if resp.status_code == 200:
                    for line in resp.text.splitlines():
                        line = line.strip()
                        if not line or ":" not in line or " " in line:
                            continue
                        if "://" not in line:
                            line = f"{scheme}://{line}"
                        if line not in self._bad and line not in seen:
                            seen.add(line)
                            scraped.append(line)
            except Exception:
                continue

        random.shuffle(scraped)
        return candidates + scraped

    def _check_single(self, proxy: str) -> str | None:
        proxies = {"http": proxy, "https": proxy}
        try:
            resp = requests.get(
                TEST_URL,
                proxies=proxies,
                timeout=self.check_timeout,
                impersonate="firefox147",
                allow_redirects=False,
                trust_env=False,
            )
            # Не берем 403 и 429 (уже в жестком бане Qrator/Avito).
            # Берем только чистые ответы (200) или стандартные челленджи (302/439), которые парсер умеет проходить.
            if resp.status_code in (200, 301, 302, 439):
                return proxy
        except Exception:
            pass
        return None

    def refresh_pool(self, *, initial: bool = False) -> None:
        if not self._refresh_lock.acquire(blocking=False):
            return
        try:
            candidates = self._fetch_candidates()
            if not candidates:
                return

            batch_limit = 400 if initial else 800
            sample = candidates[:batch_limit]
            found: list[str] = []
            needed = 12 if initial else self.target_pool_size

            with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
                futures = {executor.submit(self._check_single, p): p for p in sample}
                for future in as_completed(futures):
                    result = future.result()
                    if result:
                        found.append(result)
                        if len(found) >= needed:
                            for f in futures:
                                f.cancel()
                            break

            with self._lock:
                existing_alive = [p for p in self._working if p not in self._bad]
                merged = list(dict.fromkeys(found + existing_alive))
                self._working = merged[:120]
                if self.current_proxy is None and self._working:
                    self.current_proxy = self._working[0]
                    self.requests_on_current = 0

            self.log("PROXY-POOL", alive=len(self._working), current=self.current_proxy or "direct")
        finally:
            self._refresh_lock.release()

    def _background_loop(self) -> None:
        while not self._stop_event.is_set():
            time.sleep(self.refresh_interval)
            with self._lock:
                if len(self._bad) > 3000:
                    self._bad.clear()
            self.refresh_pool(initial=False)

    def ensure_proxy_available(self) -> None:
        """Если пул прокси пуст или почти пуст, немедленно докачивает новые."""
        with self._lock:
            low_pool = len(self._working) < 3
            empty = self.current_proxy is None

        if empty:
            self.refresh_pool(initial=False)
            with self._lock:
                if not self._working:
                    # Если все из черного списка кончились, сбрасываем бан-лист и ищем заново
                    self._bad.clear()
            if not self.current_proxy:
                self.refresh_pool(initial=True)
        elif low_pool:
            threading.Thread(target=self.refresh_pool, kwargs={"initial": False}, daemon=True).start()

    def register_request_and_check_rotation(self) -> bool:
        """Увеличивает счетчик запросов. Возвращает True каждые N запросов."""
        with self._lock:
            self.requests_on_current += 1
            if self.requests_on_current >= self.rotate_every:
                self._rotate_unlocked(reason=f"{self.rotate_every} запросов")
                return True
            return False

    def mark_dead_and_rotate(self, reason: str = "error") -> str | None:
        """Удаляет проблемный прокси и сразу переключает на следующий без пауз."""
        with self._lock:
            if self.current_proxy:
                self._bad.add(self.current_proxy)
                if self.current_proxy in self._working:
                    self._working.remove(self.current_proxy)
            nxt = self._rotate_unlocked(reason=reason)
            need_refill = len(self._working) < 4

        if need_refill:
            threading.Thread(target=self.refresh_pool, kwargs={"initial": False}, daemon=True).start()
        return nxt

    def _rotate_unlocked(self, reason: str = "rotate") -> str | None:
        self.requests_on_current = 0
        if not self._working:
            self.current_proxy = None
            self.log("PROXY-ROTATE", proxy="direct (обновляю пул прокси...)", reason=reason)
            return None

        if self.current_proxy in self._working and len(self._working) > 1:
            idx = (self.working_index(self.current_proxy) + 1) % len(self._working)
            self.current_proxy = self._working[idx]
        else:
            self.current_proxy = self._working[0]

        self.log("PROXY-ROTATE", proxy=self.current_proxy, reason=reason, pool=len(self._working))
        return self.current_proxy

    def working_index(self, proxy: str) -> int:
        try:
            return self._working.index(proxy)
        except ValueError:
            return 0

    def get_proxies_dict(self) -> dict[str, str] | None:
        with self._lock:
            if not self.current_proxy:
                return None
            return {"http": self.current_proxy, "https": self.current_proxy}