"""Optional MTGStocks price lookup with a local cache.

The provider supports the old public HTML search flow and a compatible JSON
endpoint for deployments that expose one. It deliberately does not attempt to
bypass bot checks, CAPTCHAs, or other access controls.
"""

import html
import json
import logging
import re
import sqlite3
import time
from difflib import SequenceMatcher
from pathlib import Path
from urllib.parse import urlencode, urljoin

from .cardmarket import MarketEstimate
from .utils import normalize_catalog_name, request_remote

logger = logging.getLogger(__name__)


class MTGStocksError(RuntimeError):
    """Raised when MTGStocks cannot return a usable price."""


class MTGStocksCache:
    def __init__(self, database_path):
        self.database_path = Path(database_path)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS mtgstocks_cache (
                    cache_key TEXT PRIMARY KEY,
                    payload TEXT NOT NULL,
                    fetched_at REAL NOT NULL
                )
                """
            )

    def get(self, cache_key, max_age_seconds):
        with self._connect() as connection:
            row = connection.execute(
                "SELECT payload, fetched_at FROM mtgstocks_cache WHERE cache_key = ?",
                (cache_key,),
            ).fetchone()
        if not row or time.time() - row[1] > max_age_seconds:
            return None
        try:
            return json.loads(row[0])
        except json.JSONDecodeError:
            logger.warning("Ignoring invalid MTGStocks cache entry %s", cache_key)
            return None

    def put(self, cache_key, payload):
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO mtgstocks_cache (cache_key, payload, fetched_at)
                VALUES (?, ?, ?)
                ON CONFLICT(cache_key) DO UPDATE SET
                    payload = excluded.payload,
                    fetched_at = excluded.fetched_at
                """,
                (cache_key, json.dumps(payload), time.time()),
            )

    def _connect(self):
        connection = sqlite3.connect(self.database_path, timeout=30)
        connection.execute("PRAGMA busy_timeout = 30000")
        connection.execute("PRAGMA journal_mode = WAL")
        return connection


class MTGStocksClient:
    def __init__(self, base_url, api_url=None, request_delay_seconds=1.0):
        self.base_url = base_url.rstrip("/")
        self.api_url = api_url.rstrip("/") if api_url else ""
        self.request_delay_seconds = max(0.0, float(request_delay_seconds))
        self._last_request_at = 0.0

    def get_price(self, name, card_set):
        self._wait_for_rate_limit()
        if self.api_url:
            return self._get_api_price(name, card_set)
        return self._get_html_price(name, card_set)

    def _get_api_price(self, name, card_set):
        query = {"name": name}
        if card_set:
            query["set"] = card_set
        response = request_remote(
            f"{self.api_url}?{urlencode(query)}",
            headers={
                "Accept": "application/json",
                "User-Agent": "cardmirror/0.1 (+local-card-order-tool)",
            },
            timeout=30,
        )
        try:
            _raise_for_access_error(response, "MTGStocks-compatible API")
            response.raise_for_status()
            try:
                payload = response.json()
            except ValueError as error:
                raise MTGStocksError("MTGStocks-compatible API returned invalid JSON") from error
        except MTGStocksError:
            raise
        except Exception as error:
            raise MTGStocksError(f"MTGStocks API request failed: {error}") from error
        finally:
            response.close()

        result = _select_result(_normalise_api_payload(payload), name, card_set)
        if not result:
            raise MTGStocksError(f"no matching printing found for {name} ({card_set})")
        return result

    def _get_html_price(self, name, card_set):
        query = urlencode({"utf8": "✓", "print[card]": name, "button": ""})
        search_url = f"{self.base_url}/cards/search?{query}"
        response = request_remote(
            search_url,
            headers={
                "Accept": "text/html,application/xhtml+xml",
                "User-Agent": "cardmirror/0.1 (+local-card-order-tool)",
            },
            timeout=30,
        )
        try:
            _raise_for_access_error(response, "MTGStocks")
            response.raise_for_status()
            page_url = response.url
            page_html = response.text
        except MTGStocksError:
            raise
        except Exception as error:
            raise MTGStocksError(f"MTGStocks request failed: {error}") from error
        finally:
            response.close()

        print_urls = _print_links(page_html, self.base_url)
        if not print_urls and "/prints/" in page_url:
            print_urls = [page_url]
        if not print_urls:
            raise MTGStocksError("no printable result links were returned")

        selected_url = _select_print_url(print_urls, name, card_set)
        if not selected_url:
            raise MTGStocksError(f"no matching printing found for {name} ({card_set})")
        return self._get_printing(selected_url, name, card_set)

    def _get_printing(self, print_url, name, card_set):
        self._wait_for_rate_limit()
        response = request_remote(
            print_url,
            headers={
                "Accept": "text/html,application/xhtml+xml",
                "User-Agent": "cardmirror/0.1 (+local-card-order-tool)",
            },
            timeout=30,
        )
        try:
            _raise_for_access_error(response, "MTGStocks printing page")
            response.raise_for_status()
            result = _parse_printing_page(response.text, print_url)
        except MTGStocksError:
            raise
        except Exception as error:
            raise MTGStocksError(f"MTGStocks printing request failed: {error}") from error
        finally:
            response.close()

        if not result.get("prices"):
            raise MTGStocksError(f"no usable prices found for {name} ({card_set})")
        return result

    def _wait_for_rate_limit(self):
        elapsed = time.monotonic() - self._last_request_at
        if elapsed < self.request_delay_seconds:
            time.sleep(self.request_delay_seconds - elapsed)
        self._last_request_at = time.monotonic()


class MTGStocksPriceProvider:
    def __init__(
        self,
        cache_path,
        base_url="https://www.mtgstocks.com",
        api_url=None,
        cache_hours=24,
        request_delay_seconds=1.0,
        currency="USD",
        vendor_currency="USD",
        vendor_to_market_rate=None,
    ):
        self.cache = MTGStocksCache(cache_path)
        self.client = MTGStocksClient(base_url, api_url, request_delay_seconds)
        self.cache_seconds = cache_hours * 60 * 60
        self.currency = currency
        self.vendor_currency = vendor_currency
        self.vendor_to_market_rate = vendor_to_market_rate

    @property
    def configured(self):
        return bool(self.client.base_url)

    def estimate_orders(self, orders, vendor_total=None):
        rows = [(order.name, order.set, int(order.quantity)) for order in orders]
        return self.estimate_rows(rows, vendor_total=vendor_total)

    def estimate_rows(self, rows, vendor_total=None):
        total_quantity = sum(max(0, quantity) for _, _, quantity in rows)
        if not total_quantity:
            return MarketEstimate(
                False,
                currency=self.currency,
                vendor_currency=self.vendor_currency,
                vendor_total=vendor_total,
            )

        market_total = 0.0
        matched_quantity = 0
        unavailable = []
        retrieved_at = time.time()
        for name, card_set, quantity in rows:
            if quantity <= 0:
                continue
            try:
                result = self._find_price(name, card_set)
                price = _preferred_price(result.get("prices", {}))
                if price is None:
                    unavailable.append(f"{name} ({card_set}): no average, low, or high price")
                    continue
                market_total += price * quantity
                matched_quantity += quantity
            except (MTGStocksError, ValueError) as error:
                logger.warning("MTGStocks price unavailable for %s (%s): %s", name, card_set, error)
                unavailable.append(f"{name} ({card_set}): {error}")

        if matched_quantity == 0:
            return MarketEstimate(
                False,
                currency=self.currency,
                vendor_currency=self.vendor_currency,
                vendor_total=vendor_total,
                total_quantity=total_quantity,
                unavailable=unavailable,
                retrieved_at=retrieved_at,
            )

        converted_vendor_total = _converted_vendor_total(
            vendor_total,
            self.vendor_currency,
            self.currency,
            self.vendor_to_market_rate,
        )
        saving = (
            converted_vendor_total - market_total
            if converted_vendor_total is not None
            else None
        )
        saving_percent = (
            saving / converted_vendor_total * 100
            if saving is not None and converted_vendor_total
            else None
        )
        return MarketEstimate(
            True,
            currency=self.currency,
            vendor_currency=self.vendor_currency,
            market_total=market_total,
            vendor_total=vendor_total,
            vendor_total_market_currency=converted_vendor_total,
            saving=saving,
            saving_percent=saving_percent,
            matched_quantity=matched_quantity,
            total_quantity=total_quantity,
            source="MTGStocks average/low/high price",
            retrieved_at=retrieved_at,
            unavailable=unavailable,
        )

    def _find_price(self, name, card_set):
        cache_key = f"price:{normalize_catalog_name(name)}:{normalize_catalog_name(card_set)}"
        cached = self.cache.get(cache_key, self.cache_seconds)
        if cached is not None:
            return cached
        result = self.client.get_price(name, card_set)
        self.cache.put(cache_key, result)
        return result


def _raise_for_access_error(response, source):
    if response.status_code in {401, 403, 429, 202}:
        raise MTGStocksError(
            f"{source} denied the request (HTTP {response.status_code}); "
            "the provider was not bypassed"
        )


def _normalise_api_payload(payload):
    if isinstance(payload, dict):
        for key in ("data", "results", "cards", "prints"):
            if isinstance(payload.get(key), list):
                return [item for item in payload[key] if isinstance(item, dict)]
        return [payload] if isinstance(payload.get("prices"), dict) else []
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    return []


def _select_result(results, name, card_set):
    if not results:
        return None
    normalized_name = normalize_catalog_name(name)
    normalized_set = normalize_catalog_name(card_set)
    ranked = []
    for result in results:
        result_name = normalize_catalog_name(str(result.get("name", "")))
        result_set = normalize_catalog_name(str(result.get("set", result.get("set_name", ""))))
        name_score = SequenceMatcher(None, normalized_name, result_name).ratio()
        set_score = SequenceMatcher(None, normalized_set, result_set).ratio() if normalized_set else 0
        score = name_score * 10 + set_score
        if result_name == normalized_name:
            score += 10
        if normalized_set and result_set == normalized_set:
            score += 10
        ranked.append((score, result))
    ranked.sort(key=lambda item: item[0], reverse=True)
    if ranked[0][0] < 8:
        return None
    return ranked[0][1]


def _print_links(page_html, base_url):
    links = []
    for match in re.finditer(
        r"<a\b[^>]+href=[\"']([^\"']*/prints/[^\"']*)[\"'][^>]*>(.*?)</a>",
        page_html,
        flags=re.IGNORECASE | re.DOTALL,
    ):
        href = html.unescape(match.group(1))
        label = _clean_html(match.group(2))
        links.append({"url": urljoin(f"{base_url}/", href), "label": label})
    return links


def _select_print_url(print_links, name, card_set):
    normalized_name = normalize_catalog_name(name)
    normalized_set = normalize_catalog_name(card_set)
    ranked = []
    for item in print_links:
        searchable = normalize_catalog_name(item["label"] + " " + item["url"])
        score = SequenceMatcher(None, normalized_name, searchable).ratio()
        if normalized_name and normalized_name in searchable:
            score += 1
        if normalized_set and normalized_set in searchable:
            score += 1
        ranked.append((score, item["url"]))
    ranked.sort(key=lambda item: item[0], reverse=True)
    return ranked[0][1] if ranked and ranked[0][0] >= 0.7 else None


def _parse_printing_page(page_html, print_url):
    name = _first_tag_text(page_html, "h2")
    card_set = _first_tag_text(page_html, "h5")
    prices = {}
    price_values = re.findall(
        r"<[^>]*class=[\"'][^\"']*priceheader[^\"']*[\"'][^>]*>(.*?)</[^>]+>",
        page_html,
        flags=re.IGNORECASE | re.DOTALL,
    )
    price_keys = ("avg",) if len(price_values) == 1 else ("low", "avg", "high")
    for key, value in zip(price_keys, price_values):
        parsed = _parse_price(_clean_html(value))
        if parsed is not None:
            prices[key] = parsed
    return {"name": name, "set": card_set, "link": print_url, "prices": prices}


def _first_tag_text(page_html, tag_name):
    match = re.search(
        rf"<{tag_name}\b[^>]*>(.*?)</{tag_name}>",
        page_html,
        flags=re.IGNORECASE | re.DOTALL,
    )
    return _clean_html(match.group(1)) if match else ""


def _clean_html(value):
    value = re.sub(r"<[^>]+>", " ", html.unescape(value))
    return re.sub(r"\s+", " ", value).strip()


def _parse_price(value):
    match = re.search(r"(?:\$|USD\s*)?([0-9]+(?:[.,][0-9]+)?)", str(value))
    if not match:
        return None
    try:
        return float(match.group(1).replace(",", "."))
    except ValueError:
        return None


def _preferred_price(prices):
    for key in ("avg", "average", "low", "high"):
        value = _parse_price(prices.get(key)) if isinstance(prices, dict) else None
        if value is not None:
            return value
    return None


def _converted_vendor_total(vendor_total, vendor_currency, market_currency, rate):
    if vendor_total is None:
        return None
    if vendor_currency == market_currency:
        return vendor_total
    if rate is not None:
        return vendor_total * rate
    return None
