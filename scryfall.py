"""Scryfall-backed MTG printing and EUR price lookup."""

import json
import logging
import sqlite3
import time
from pathlib import Path
from urllib.parse import urlencode

from cardmarket import MarketEstimate
from utils import normalize_catalog_name, request_remote

logger = logging.getLogger(__name__)


class ScryfallError(RuntimeError):
    """Raised when Scryfall cannot return usable card data."""


class ScryfallCache:
    def __init__(self, database_path):
        self.database_path = Path(database_path)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS scryfall_cache (
                    cache_key TEXT PRIMARY KEY,
                    payload TEXT NOT NULL,
                    fetched_at REAL NOT NULL
                )
                """
            )

    def get(self, cache_key, max_age_seconds):
        with self._connect() as connection:
            row = connection.execute(
                "SELECT payload, fetched_at FROM scryfall_cache WHERE cache_key = ?",
                (cache_key,),
            ).fetchone()
        if not row or time.time() - row[1] > max_age_seconds:
            return None
        try:
            return json.loads(row[0])
        except json.JSONDecodeError:
            logger.warning("Ignoring invalid Scryfall cache entry %s", cache_key)
            return None

    def put(self, cache_key, payload):
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO scryfall_cache (cache_key, payload, fetched_at)
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


class ScryfallClient:
    def __init__(self, base_url="https://api.scryfall.com"):
        self.base_url = base_url.rstrip("/")

    def get_json(self, path, params=None):
        query = urlencode({key: str(value) for key, value in (params or {}).items()})
        url = path if str(path).startswith("https://") else f"{self.base_url}/{path.lstrip('/')}"
        if query:
            url += f"?{query}"
        response = request_remote(
            url,
            headers={
                "Accept": "application/json",
                "User-Agent": "cardmirror/0.1 (+local-card-order-tool)",
            },
            timeout=30,
        )
        try:
            if response.status_code == 429:
                raise ScryfallError("Scryfall rate limit reached (HTTP 429).")
            response.raise_for_status()
            try:
                return response.json()
            except ValueError as error:
                raise ScryfallError("Scryfall returned invalid JSON.") from error
        except ScryfallError:
            raise
        except Exception as error:
            raise ScryfallError(f"Scryfall request failed: {error}") from error
        finally:
            response.close()


class ScryfallPriceProvider:
    def __init__(
        self,
        cache_path,
        cache_hours=24,
        vendor_currency="USD",
        currency="EUR",
        vendor_to_market_rate=None,
        foil_mode="auto",
    ):
        self.cache = ScryfallCache(cache_path)
        self.client = ScryfallClient()
        self.cache_seconds = cache_hours * 60 * 60
        self.vendor_currency = vendor_currency
        self.currency = currency
        self.vendor_to_market_rate = vendor_to_market_rate
        self.foil_mode = foil_mode

    @property
    def configured(self):
        return True

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
        fallback_count = 0
        retrieved_at = time.time()
        for name, card_set, quantity in rows:
            if quantity <= 0:
                continue
            try:
                card = self._find_printing(name, card_set)
                if not card:
                    unavailable.append(f"{name} ({card_set}): ambiguous Scryfall printing")
                    continue
                fallback_count += int(card.get("_mtg_autoproxy_fallback", False))
                price_key = self._price_key(name, card)
                price = _card_price(card, price_key)
                if price is None:
                    unavailable.append(f"{name} ({card_set}): no EUR price")
                    continue
                market_total += price * quantity
                matched_quantity += quantity
            except ScryfallError as error:
                logger.warning("Scryfall price unavailable for %s: %s", name, error)
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
            source=(
                "Scryfall EUR lowest matching printing"
                if fallback_count
                else "Scryfall EUR printing price"
            ),
            retrieved_at=retrieved_at,
            unavailable=unavailable,
        )

    def _find_printing(self, candidate_name, candidate_set):
        resolved_name = self._resolve_card_name(candidate_name)
        cache_key = f"printings:{normalize_catalog_name(resolved_name)}"
        cards = self.cache.get(cache_key, self.cache_seconds)
        if cards is None:
            cards = self._load_all_printings(resolved_name)
            self.cache.put(cache_key, cards)
        selected = _select_printing(cards, candidate_name, candidate_set, resolved_name)
        if selected:
            return selected

        priced_cards = [
            card for card in cards
            if _card_price(card, self._price_key(candidate_name, card)) is not None
        ]
        if not priced_cards:
            return None
        fallback = min(
            priced_cards,
            key=lambda card: _card_price(card, self._price_key(candidate_name, card)),
        )
        fallback = dict(fallback)
        fallback["_mtg_autoproxy_fallback"] = True
        logger.warning(
            "Could not identify the exact Scryfall printing for '%s' (%s); using the lowest priced matching printing.",
            candidate_name,
            candidate_set,
        )
        return fallback

    def _resolve_card_name(self, candidate_name):
        cache_key = f"name:{normalize_catalog_name(candidate_name)}"
        cached = self.cache.get(cache_key, self.cache_seconds)
        if cached:
            return cached
        try:
            payload = self.client.get_json("/cards/named", {"fuzzy": candidate_name})
        except ScryfallError:
            for search_term in _fallback_search_terms(candidate_name):
                try:
                    payload = self.client.get_json("/cards/named", {"fuzzy": search_term})
                    break
                except ScryfallError:
                    continue
            else:
                raise
        resolved_name = payload.get("name") if isinstance(payload, dict) else None
        if not resolved_name:
            raise ScryfallError(f"Could not resolve card name '{candidate_name}'")
        self.cache.put(cache_key, resolved_name)
        return resolved_name

    def _load_all_printings(self, resolved_name):
        cards = []
        payload = self.client.get_json(
            "/cards/search",
            {"q": f'!"{resolved_name}"', "unique": "prints", "order": "released"},
        )
        while True:
            cards.extend(payload.get("data", []))
            if not payload.get("has_more") or not payload.get("next_page"):
                return cards
            payload = self.client.get_json(payload["next_page"])

    def _price_key(self, candidate_name, card):
        if self.foil_mode == "foil":
            return "eur_foil"
        if self.foil_mode == "auto" and _looks_foil(candidate_name):
            return "eur_foil"
        return "eur"


def _number(value):
    if value is None or value == "":
        return None
    try:
        return float(str(value).replace(",", ".").strip())
    except ValueError:
        return None


def _card_price(card, price_key):
    return _number((card.get("prices") or {}).get(price_key))


def _fallback_search_terms(candidate_name):
    words = str(candidate_name).replace("#", " #").split()
    terms = []
    for start in range(len(words)):
        for end in range(len(words), start + 1, -1):
            term = " ".join(words[start:end]).replace(" #", "#")
            if len(term.split()) >= 2:
                terms.append(term)
    seen = set()
    for term in terms:
        if term not in seen:
            seen.add(term)
            yield term


def _select_printing(cards, candidate_name, candidate_set, resolved_name):
    if not cards:
        return None
    if len(cards) == 1:
        return cards[0]

    base_tokens = set(normalize_catalog_name(resolved_name).split())
    variant_tokens = set(normalize_catalog_name(candidate_name).split()) - base_tokens
    ignored_tokens = {
        "regular", "sets", "foil", "special", "holo", "singles", "cards", "mixed",
        "bl", "new", "side", "double", "galaxy",
    }
    variant_tokens -= ignored_tokens
    set_tokens = {
        token for token in normalize_catalog_name(candidate_set).split()
        if len(token) > 1 and token not in ignored_tokens
    }
    language_aliases = {
        "english": "en", "german": "de", "french": "fr", "italian": "it",
        "spanish": "es", "russian": "ru", "japanese": "ja", "jp": "ja",
        "korean": "ko", "portuguese": "pt", "chinese": "zhs",
    }
    language_requested = any(token in language_aliases for token in variant_tokens)
    variant_tokens.update(
        language_aliases[token]
        for token in tuple(variant_tokens)
        if token in language_aliases
    )
    scored = []
    for card in cards:
        searchable = set()
        for key in ("set_name", "set", "collector_number", "lang"):
            searchable.update(normalize_catalog_name(str(card.get(key, ""))).split())
        score = 0
        score += 8 * len(variant_tokens & searchable)
        score += 4 * len(set_tokens & searchable)
        if not language_requested and card.get("lang") == "en":
            score += 1
        collector_number = str(card.get("collector_number", "")).lower()
        for token in variant_tokens:
            if token.lstrip("0") and token.lstrip("0") == collector_number.lstrip("0"):
                score += 10
        scored.append((score, card))

    scored.sort(key=lambda item: item[0], reverse=True)
    if scored[0][0] == 0 or (len(scored) > 1 and scored[0][0] == scored[1][0]):
        return None
    return scored[0][1]


def _looks_foil(candidate_name):
    return "foil" in normalize_catalog_name(candidate_name).split()
