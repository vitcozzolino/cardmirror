"""Optional Cardmarket market-price integration with a local SQLite cache."""

import base64
import csv
import gzip
import hashlib
import hmac
import json
import logging
import os
import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from io import StringIO
from urllib.parse import quote, urlencode, urlparse, urlunparse

from utils import normalize_catalog_name, request_remote

logger = logging.getLogger(__name__)


class CardmarketError(RuntimeError):
    """Raised when Cardmarket cannot return a usable response."""


@dataclass
class MarketEstimate:
    available: bool
    currency: str = "EUR"
    vendor_currency: str = "USD"
    market_total: float | None = None
    vendor_total: float | None = None
    vendor_total_market_currency: float | None = None
    saving: float | None = None
    saving_percent: float | None = None
    matched_quantity: int = 0
    total_quantity: int = 0
    source: str = ""
    retrieved_at: float | None = None
    unavailable: list[str] = field(default_factory=list)

    @property
    def matched(self):
        return not self.unavailable and self.matched_quantity == self.total_quantity


class CardmarketCache:
    def __init__(self, database_path):
        self.database_path = database_path
        os.makedirs(os.path.dirname(database_path) or ".", exist_ok=True)
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS cardmarket_cache (
                    cache_key TEXT PRIMARY KEY,
                    payload TEXT NOT NULL,
                    fetched_at REAL NOT NULL
                )
                """
            )

    def get(self, cache_key, max_age_seconds):
        with self._connect() as connection:
            row = connection.execute(
                "SELECT payload, fetched_at FROM cardmarket_cache WHERE cache_key = ?",
                (cache_key,),
            ).fetchone()
        if not row or time.time() - row[1] > max_age_seconds:
            return None
        try:
            return json.loads(row[0])
        except json.JSONDecodeError:
            logger.warning("Ignoring invalid Cardmarket cache entry %s", cache_key)
            return None

    def put(self, cache_key, payload):
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO cardmarket_cache (cache_key, payload, fetched_at)
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


class CardmarketClient:
    def __init__(self, base_url, consumer_key=None, consumer_secret=None,
                 access_token=None, access_token_secret=None):
        self.base_url = base_url.rstrip("/")
        self.consumer_key = consumer_key or os.getenv("CARDMARKET_CONSUMER_KEY", "")
        self.consumer_secret = consumer_secret or os.getenv("CARDMARKET_CONSUMER_SECRET", "")
        self.access_token = access_token or os.getenv("CARDMARKET_ACCESS_TOKEN", "")
        self.access_token_secret = access_token_secret or os.getenv("CARDMARKET_ACCESS_TOKEN_SECRET", "")

    @property
    def configured(self):
        return bool(self.consumer_key and self.consumer_secret)

    def get_json(self, path, params=None):
        if not self.configured:
            raise CardmarketError(
                "Cardmarket credentials are not configured; set CARDMARKET_CONSUMER_KEY "
                "and CARDMARKET_CONSUMER_SECRET."
            )

        params = {str(key): str(value) for key, value in (params or {}).items()}
        endpoint = f"{self.base_url}/{path.lstrip('/')}"
        oauth = {
            "oauth_consumer_key": self.consumer_key,
            "oauth_nonce": uuid.uuid4().hex,
            "oauth_signature_method": "HMAC-SHA1",
            "oauth_timestamp": str(int(time.time())),
            "oauth_version": "1.0",
        }
        if self.access_token:
            oauth["oauth_token"] = self.access_token

        signature_params = list(params.items()) + list(oauth.items())
        normalized_params = "&".join(
            f"{quote(key, safe='~')}={quote(value, safe='~')}"
            for key, value in sorted(signature_params)
        )
        parsed = urlparse(endpoint)
        base_endpoint = urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", "", ""))
        base_string = "&".join(
            [
                "GET",
                quote(base_endpoint, safe="~"),
                quote(normalized_params, safe="~"),
            ]
        )
        signing_key = f"{quote(self.consumer_secret, safe='~')}&{quote(self.access_token_secret, safe='~')}"
        oauth["oauth_signature"] = base64.b64encode(
            hmac.new(signing_key.encode(), base_string.encode(), hashlib.sha1).digest()
        ).decode()
        authorization = "OAuth " + ", ".join(
            f'{quote(key, safe="~")}="{quote(value, safe="~")}"'
            for key, value in sorted(oauth.items())
        )
        query = urlencode(params)
        request_url = f"{endpoint}?{query}" if query else endpoint
        response = request_remote(
            request_url,
            headers={"Accept": "application/json", "Authorization": authorization},
            timeout=30,
        )
        try:
            if response.status_code == 429:
                raise CardmarketError("Cardmarket rate limit reached (HTTP 429).")
            response.raise_for_status()
            try:
                return response.json()
            except ValueError as error:
                raise CardmarketError("Cardmarket returned invalid JSON.") from error
        except CardmarketError:
            raise
        except Exception as error:
            raise CardmarketError(f"Cardmarket request failed: {error}") from error
        finally:
            response.close()


class CardmarketPriceProvider:
    def __init__(
        self,
        cache_path,
        api_base_url,
        price_guide_base_url=None,
        price_source="articles",
        price_guide_fallback=True,
        article_cache_minutes=15,
        price_guide_cache_hours=24,
        product_cache_days=30,
        language_id=1,
        min_condition="NM",
        foil=False,
        price_guide_field="Trend Price",
        currency="EUR",
        vendor_currency="USD",
        vendor_to_market_rate=None,
    ):
        self.cache = CardmarketCache(cache_path)
        self.client = CardmarketClient(api_base_url)
        self.price_guide_client = (
            self.client
            if not price_guide_base_url or price_guide_base_url.rstrip("/") == api_base_url.rstrip("/")
            else CardmarketClient(price_guide_base_url)
        )
        if self.price_guide_client is not self.client:
            self.price_guide_client.consumer_key = self.client.consumer_key
            self.price_guide_client.consumer_secret = self.client.consumer_secret
            self.price_guide_client.access_token = self.client.access_token
            self.price_guide_client.access_token_secret = self.client.access_token_secret
        self.price_source = price_source
        self.price_guide_fallback = price_guide_fallback
        self.article_cache_seconds = article_cache_minutes * 60
        self.price_guide_cache_seconds = price_guide_cache_hours * 60 * 60
        self.product_cache_seconds = product_cache_days * 24 * 60 * 60
        self.language_id = language_id
        self.min_condition = min_condition
        self.foil = foil
        self.price_guide_field = price_guide_field
        self.currency = currency
        self.vendor_currency = vendor_currency
        self.vendor_to_market_rate = vendor_to_market_rate

    @property
    def configured(self):
        return self.client.configured

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
        if not self.configured:
            return MarketEstimate(
                False,
                currency=self.currency,
                vendor_currency=self.vendor_currency,
                vendor_total=vendor_total,
                total_quantity=total_quantity,
                unavailable=["Cardmarket credentials are not configured"],
            )

        market_total = 0.0
        matched_quantity = 0
        unavailable = []
        source_used = []
        retrieved_at = time.time()
        for name, card_set, quantity in rows:
            if quantity <= 0:
                continue
            try:
                product = self._find_product(name, card_set)
                if not product:
                    unavailable.append(f"{name} ({card_set}): ambiguous Cardmarket product")
                    continue
                quote, source = self._quote_product(product["idProduct"], quantity)
                if quote is None:
                    unavailable.append(f"{name} ({card_set}): no matching market listings")
                    continue
                market_total += quote
                matched_quantity += quantity
                source_used.append(source)
            except CardmarketError as error:
                logger.warning("Cardmarket price unavailable for %s: %s", name, error)
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
        converted_vendor_total = None
        if vendor_total is not None:
            if self.vendor_currency == self.currency:
                converted_vendor_total = vendor_total
            elif self.vendor_to_market_rate is not None:
                converted_vendor_total = vendor_total * self.vendor_to_market_rate
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
            source=" + ".join(sorted(set(source_used))),
            retrieved_at=retrieved_at,
            unavailable=unavailable,
        )

    def _find_product(self, card_name, card_set):
        cache_key = f"product:{normalize_catalog_name(card_name)}:{normalize_catalog_name(card_set)}"
        cached = self.cache.get(cache_key, self.product_cache_seconds)
        if cached is not None:
            return cached or None

        products = []
        for search_term, exact in _candidate_search_terms(card_name):
            payload = self.client.get_json(
                "/products/find",
                {
                    "search": search_term,
                    "exact": str(exact).lower(),
                    "idGame": "1",
                    "idLanguage": str(self.language_id),
                },
            )
            products = _product_list(payload)
            if products:
                break
        if not products:
            self.cache.put(cache_key, {})
            return None
        if len(products) > 1:
            products = self._add_product_details(products[:30])
        selected = _select_product(products, card_name, card_set)
        self.cache.put(cache_key, selected or {})
        return selected

    def _add_product_details(self, products):
        detailed = []
        for product in products:
            product_id = _value(product, "idProduct", "id_product")
            if product_id is None or _has_set_metadata(product):
                detailed.append(product)
                continue
            try:
                detail = self.client.get_json(f"/products/{int(product_id)}")
            except CardmarketError as error:
                logger.debug("Could not load Cardmarket product %s details: %s", product_id, error)
                detailed.append(product)
                continue
            if isinstance(detail, dict):
                merged = dict(product)
                merged.update(detail.get("product", detail))
                detailed.append(merged)
            else:
                detailed.append(product)
        return detailed

    def _quote_product(self, product_id, quantity):
        if self.price_source == "price_guide":
            return self._quote_from_price_guide(product_id, quantity)
        try:
            quote = self._quote_from_articles(product_id, quantity)
            if quote is not None:
                return quote, "live articles"
        except CardmarketError:
            if not self.price_guide_fallback:
                raise
            logger.info("Falling back to the Cardmarket price guide for product %s", product_id)
        if self.price_guide_fallback:
            return self._quote_from_price_guide(product_id, quantity)
        return None, ""

    def _quote_from_articles(self, product_id, quantity):
        cache_key = f"articles:{product_id}:{self.language_id}:{self.min_condition}:{int(self.foil)}"
        listings = self.cache.get(cache_key, self.article_cache_seconds)
        if listings is None:
            payload = self.client.get_json(
                f"/articles/{int(product_id)}",
                {
                    "idLanguage": str(self.language_id),
                    "minCondition": self.min_condition,
                    "isFoil": str(self.foil).lower(),
                    "start": "0",
                    "maxResults": "100",
                },
            )
            listings = _article_list(payload)
            self.cache.put(cache_key, listings)

        normalized = []
        for listing in listings:
            price = _number(_value(listing, "price"))
            count = int(_number(_value(listing, "count", "amount", "available")) or 0)
            if price is not None and count > 0:
                normalized.append((price, count))
        normalized.sort()
        remaining = quantity
        total = 0.0
        for price, count in normalized:
            taken = min(remaining, count)
            total += taken * price
            remaining -= taken
            if remaining == 0:
                return total
        return None

    def _quote_from_price_guide(self, product_id, quantity):
        price_guide = self.cache.get("price-guide:1", self.price_guide_cache_seconds)
        if price_guide is None:
            payload = self.price_guide_client.get_json("/priceguide", {"idGame": "1"})
            price_guide = _price_guide_rows(payload)
            self.cache.put("price-guide:1", price_guide)
        row = price_guide.get(str(product_id))
        if not row:
            return None, "price guide"
        price = _number(row.get(self.price_guide_field))
        if price is None:
            return None, "price guide"
        return price * quantity, "daily price guide"


def _value(mapping, *names):
    if not isinstance(mapping, dict):
        return None
    normalized = {str(key).lower().replace("_", ""): value for key, value in mapping.items()}
    for name in names:
        value = normalized.get(name.lower().replace("_", ""))
        if value is not None:
            return value
    return None


def _number(value):
    if value is None or value == "":
        return None
    try:
        return float(str(value).replace("€", "").replace(",", ".").strip())
    except ValueError:
        return None


def _product_list(payload):
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("product", "products", "Product"):
            value = payload.get(key)
            if isinstance(value, list):
                return value
            if isinstance(value, dict):
                return [value]
    return []


def _article_list(payload):
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("article", "articles", "Article"):
            value = payload.get(key)
            if isinstance(value, list):
                return value
            if isinstance(value, dict):
                return [value]
    return []


def _select_product(products, card_name, card_set):
    if len(products) == 1:
        product_id = _value(products[0], "idProduct", "id_product")
        return {"idProduct": int(product_id)} if product_id is not None else None

    ignored_set_tokens = {"regular", "sets", "foil", "special", "holo", "singles", "cards"}
    set_tokens = set(normalize_catalog_name(card_set).split()) - ignored_set_tokens
    name_tokens = set(normalize_catalog_name(card_name).split())
    scored = []
    for product in products:
        product_id = _value(product, "idProduct", "id_product")
        if product_id is None:
            continue
        searchable = " ".join(
            str(_value(product, key) or "")
            for key in ("expansion", "expansionName", "set", "edition", "name", "enName")
        )
        searchable_tokens = set(normalize_catalog_name(searchable).split())
        overlap = len((set_tokens | name_tokens) & searchable_tokens)
        scored.append((overlap, {"idProduct": int(product_id)}))
    if not scored:
        return None
    scored.sort(key=lambda item: item[0], reverse=True)
    if scored[0][0] == 0 or (len(scored) > 1 and scored[0][0] == scored[1][0]):
        return None
    return scored[0][1]


def _candidate_search_terms(card_name):
    """Try exact vendor names before progressively simpler card names."""
    normalized_name = " ".join(str(card_name).split()).strip()
    terms = [(normalized_name, True), (normalized_name, False)]
    without_parentheses = " ".join(
        part for part in normalized_name.replace("#", " #").split()
        if not (part.startswith("(") and part.endswith(")"))
    ).replace(" #", "#")
    if without_parentheses != normalized_name:
        terms.extend([(without_parentheses, True), (without_parentheses, False)])
    words = without_parentheses.split()
    for size in range(len(words) - 1, 1, -1):
        terms.append((" ".join(words[:size]), True))
    seen = set()
    for term, exact in terms:
        key = (term.lower(), exact)
        if term and key not in seen:
            seen.add(key)
            yield term, exact


def _has_set_metadata(product):
    return any(
        _value(product, key)
        for key in ("expansion", "expansionName", "set", "edition", "idExpansion")
    )


def _price_guide_rows(payload):
    encoded = payload.get("priceguidefile") if isinstance(payload, dict) else None
    if not encoded:
        raise CardmarketError("Cardmarket price-guide response did not contain a file")
    try:
        csv_bytes = gzip.decompress(base64.b64decode(encoded))
        rows = csv.DictReader(StringIO(csv_bytes.decode("utf-8-sig")))
        return {
            str(row.get("idProduct")): dict(row)
            for row in rows
            if row.get("idProduct")
        }
    except (ValueError, OSError, UnicodeDecodeError) as error:
        raise CardmarketError("Cardmarket price guide could not be decoded") from error
