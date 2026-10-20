import csv
import logging
import re
import time
import unicodedata
from pathlib import Path

import jellyfish
import requests

from .cardmarket import CardmarketPriceProvider
from . import config
from .mtgstocks import MTGStocksPriceProvider
from .selector import CardSelector
from .scryfall import ScryfallPriceProvider
from .utils import (
    CardCandidate,
    CardOrder,
    load_vendor_catalog,
    load_vendor_catalog_cache,
    load_vendor_price_tiers,
    load_wanted_cards,
    save_wanted_cards,
    sync_vendor_catalog_cache,
)

logger = logging.getLogger(__name__)


def setup_logging():
    """Sets up the logging configuration based on the provided log level in the config.
    The log format includes the timestamp, log level, and message.
    """
    logging.basicConfig(
        level=logging.getLevelName(config.LOG_LEVEL),
        format="[%(asctime)s]::[%(levelname)s]::%(message)s",
    )


def normalize_card_name(card_name):
    normalized = unicodedata.normalize("NFKD", card_name)
    normalized = normalized.replace("Æ", "AE").replace("æ", "ae")
    normalized = normalized.encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", " ", normalized).strip()


def card_name_variants(card_name):
    variants = {normalize_card_name(card_name)}
    variants.update(normalize_card_name(value) for value in re.findall(r"\(([^)]+)\)", card_name))
    variants.update(
        normalize_card_name(value)
        for value in re.split(r"\s*//?\s*", card_name)
    )
    return {variant for variant in variants if variant}


def match_score(wanted_name, candidate_name):
    wanted = normalize_card_name(wanted_name)
    if not wanted:
        return None

    scores = []
    for variant in card_name_variants(candidate_name):
        if f" {wanted} " in f" {variant} ":
            scores.append(1.0 if variant == wanted else 0.99)
            continue

        similarity = jellyfish.jaro_winkler_similarity(wanted, variant)
        if similarity >= config.FUZZY_MATCH_THRESHOLD:
            scores.append(similarity)

    return max(scores, default=None)


def find_card_candidates(card_name, vendor_cards, image_links):
    candidates = []

    for card_set in vendor_cards.columns:
        card_set_candidates = []
        for candidate in vendor_cards[card_set]:
            if not isinstance(candidate, str):
                continue

            score = match_score(card_name, candidate)
            if score is None:
                continue
            image_url = image_links.get((card_set, candidate))
            if not image_url:
                original_set_name = re.sub(r"\.\d+$", "", card_set)
                image_url = image_links.get((original_set_name, candidate))
            card_set_candidates.append(
                CardCandidate(candidate, card_set, image_url, score)
            )

        candidates.extend(
            sorted(card_set_candidates, key=lambda candidate: candidate.score, reverse=True)
        )

    unique_candidates = []
    seen_names = set()
    for candidate in candidates:
        normalized_name = normalize_card_name(candidate.name)
        if normalized_name in seen_names:
            continue
        seen_names.add(normalized_name)
        unique_candidates.append(candidate)

    return unique_candidates


def add_to_order(candidate, quantity, set_counters, to_order):
    logger.debug("Selected %s from %s", candidate.name, candidate.card_set)
    to_order.append(CardOrder(candidate.name, candidate.card_set, quantity))
    set_counters[candidate.card_set] = (
        set_counters.get(candidate.card_set, 0) + int(quantity)
    )


def process_cards(
    wanted_cards,
    vendor_cards,
    set_counters,
    to_order,
    missing,
    image_links=None,
    card_selector=None,
):
    required_columns = {"Wanted"}
    missing_columns = required_columns - set(wanted_cards.columns)
    if missing_columns:
        columns = ", ".join(sorted(missing_columns))
        raise ValueError(f"Wanted-card list is missing required columns: {columns}")

    image_links = image_links or {}
    selection_requests = []
    for _, row in wanted_cards.iterrows():
        wanted = row.get("Wanted", "")
        requested_set = str(row.get("Set", "") or "").strip()
        quantity = row.get("QTY", 1) or 1
        if wanted:
            candidates = find_card_candidates(wanted, vendor_cards, image_links)
            if requested_set:
                requested_set_name = normalize_card_name(requested_set)
                set_candidates = [
                    candidate
                    for candidate in candidates
                    if normalize_card_name(candidate.card_set) == requested_set_name
                ]
                if set_candidates:
                    candidates = set_candidates
                else:
                    logger.warning(
                        "Saved card version '%s' was not found for '%s'; reviewing all matches.",
                        requested_set,
                        wanted,
                    )
            if not candidates:
                missing.append(wanted)
                continue

            if card_selector:
                selection_requests.append((wanted, quantity, candidates))
            elif config.MATCH_FIRST_CANDIDATE:
                add_to_order(candidates[0], quantity, set_counters, to_order)
            else:
                for candidate in candidates:
                    add_to_order(candidate, quantity, set_counters, to_order)

    if not selection_requests:
        return False

    base_order = list(to_order)
    original_orders = []
    for _, row in wanted_cards.iterrows():
        wanted = row.get("Wanted", "")
        if not wanted:
            continue
        original_orders.append(
            CardOrder(
                str(wanted),
                str(row.get("Set", "") or ""),
                int(row.get("QTY", 1) or 1),
            )
        )
    draft_state = {"saved": False}

    def persist_draft(selected_rows, list_cleared):
        draft_orders = [] if list_cleared else list(base_order)
        draft_orders.extend(
            CardOrder(candidate.name, candidate.card_set, quantity)
            for _, candidate, quantity in selected_rows
        )
        save_wanted_cards(config.WANTED_CARDS_SOURCE, draft_orders)
        draft_state["saved"] = True
        logger.info(
            "Persisted draft selection of %d card line(s) to %s.",
            len(draft_orders),
            config.WANTED_CARDS_SOURCE,
        )

    def restore_original_list():
        if not draft_state["saved"]:
            return
        save_wanted_cards(config.WANTED_CARDS_SOURCE, original_orders)
        logger.info("Restored the wanted-card list after session cancellation.")

    try:
        (
            selections,
            selection_quantities,
            additional_selections,
            selection_finished,
            list_cleared,
            removed_rows,
        ) = card_selector.select_all(
            selection_requests,
            on_save=persist_draft,
            on_cancel=restore_original_list,
        )
    except RuntimeError as error:
        logger.warning("Image selector unavailable: %s", error)
        selections = [candidates[0] for _, _, candidates in selection_requests]
        selection_quantities = [quantity for _, quantity, _ in selection_requests]
        additional_selections = []
        selection_finished = False
        list_cleared = False
        removed_rows = set()

    if list_cleared:
        to_order.clear()
        set_counters.clear()
        missing.clear()

    for request_index, ((wanted, _, _), selected, quantity) in enumerate(zip(
        selection_requests,
        selections,
        selection_quantities,
    )):
        if not selected:
            if not list_cleared and request_index not in removed_rows:
                missing.append(wanted)
            continue
        add_to_order(selected, quantity, set_counters, to_order)

    for selected, quantity in additional_selections:
        add_to_order(selected, quantity, set_counters, to_order)

    return selection_finished


def write_output(to_order, missing, market_estimate=None):
    output_directory = Path(config.OUTPUT_DIRECTORY)
    output_directory.mkdir(parents=True, exist_ok=True)

    with open(output_directory / "order.csv", "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f, delimiter="|", lineterminator="\n")
        writer.writerow(["Card Name", "Set", "Quantity"])
        for item in to_order:
            writer.writerow(
                [csv_cell(item.name), csv_cell(item.set), item.quantity]
            )

    with open(output_directory / "missing.txt", "w", newline="") as f:
        f.write("--- MISSING CARDS ---\n")
        for item in missing:
            f.write(f"{item}\n")

    with open(output_directory / "order.md", "w", encoding="utf-8") as f:
        f.write("# Proxy Order\n\n")
        f.write("| Card Name | Set | Quantity |\n")
        f.write("| --- | --- | ---: |\n")
        for item in to_order:
            f.write(
                f"| {markdown_cell(item.name)} | {markdown_cell(item.set)} | "
                f"{item.quantity} |\n"
            )
        if market_estimate and market_estimate.available:
            f.write("\n## Market comparison\n\n")
            f.write(
                f"- Market benchmark: {market_estimate.currency} "
                f"{market_estimate.market_total:.2f}\n"
            )
            if market_estimate.vendor_total is not None and market_estimate.saving is not None:
                f.write(
                    f"- Estimated difference: {market_estimate.currency} "
                    f"{market_estimate.saving:.2f}\n"
                )
            elif market_estimate.vendor_total is not None:
                f.write(
                    f"- Vendor total: {market_estimate.vendor_currency} "
                    f"{market_estimate.vendor_total:.2f}\n"
                )
                f.write("- Estimated difference: configure a currency conversion rate\n")
            f.write(f"- Source: {markdown_cell(market_estimate.source)}\n")
            if market_estimate.unavailable:
                f.write(
                    f"- Unpriced cards: {len(market_estimate.unavailable)}\n"
                )

    with open(output_directory / "missing.md", "w", encoding="utf-8") as f:
        f.write("# Missing or Skipped Cards\n\n")
        f.write("| Card Name |\n")
        f.write("| --- |\n")
        for item in missing:
            f.write(f"| {markdown_cell(item)} |\n")


def markdown_cell(value):
    return str(value).replace("\\", "\\\\").replace("|", "\\|").replace("\n", " ")


def csv_cell(value):
    text = str(value)
    if text.startswith(("=", "+", "-", "@")):
        return "'" + text
    return text


def vendor_cost(to_order, price_tiers=None):
    total_cards = sum(int(item.quantity) for item in to_order)
    if not total_cards:
        return None

    try:
        if price_tiers is None:
            price_tiers = load_vendor_price_tiers(config.PRICING_URL)
        applicable_tiers = [
            price
            for minimum_quantity, price in price_tiers
            if total_cards >= minimum_quantity
        ]
        if not applicable_tiers:
            raise ValueError(f"No price tier covers an order of {total_cards} cards")
        price_per_card = applicable_tiers[-1]
    except (requests.RequestException, ValueError) as error:
        logger.warning("Could not calculate the vendor price: %s", error)
        return None

    return {
        "total_cards": total_cards,
        "price_per_card": price_per_card,
        "total": total_cards * price_per_card,
    }


def estimate_cost(to_order, price_tiers=None):
    estimate = vendor_cost(to_order, price_tiers)
    if not estimate:
        logger.info("No cards found, so no price estimate was calculated.")
        return None

    logger.info(
        "Estimated single-card cost for %d cards at $%.2f/card, excluding delivery: $%.2f",
        estimate["total_cards"],
        estimate["price_per_card"],
        estimate["total"],
    )
    return estimate


def main():
    setup_logging()
    st = time.process_time()

    wanted_cards = load_wanted_cards(config.WANTED_CARDS_SOURCE)
    if config.REFRESH_CATALOG_CACHE_ON_RUN:
        try:
            fresh_vendor_cards, fresh_image_links = load_vendor_catalog(
                config.CATALOG_WORKBOOK_URL,
                config.CATALOG_WORKBOOK_SHEET_NAMES,
                header_row=config.CATALOG_HEADER_ROW,
                rows_to_skip_after_header=config.CATALOG_ROWS_TO_SKIP_AFTER_HEADER,
            )
            cached_cards = sync_vendor_catalog_cache(
                config.CATALOG_CACHE_PATH,
                fresh_vendor_cards,
                fresh_image_links,
            )
            logger.info("Refreshed %d vendor card entries in the local cache.", cached_cards)
        except (OSError, requests.RequestException, ValueError) as error:
            logger.warning("Could not refresh vendor catalog: %s", error)
            logger.info("Using the local vendor catalog cache.")
    else:
        logger.info("Using the local vendor catalog cache without refreshing it.")

    vendor_cards, image_links = load_vendor_catalog_cache(config.CATALOG_CACHE_PATH)
    to_order = []
    missing = []
    set_counters = {}
    card_selector = None
    price_tiers = None
    market_provider = None
    if config.MARKET_PRICE_PROVIDER == "scryfall" and config.SCRYFALL_ENABLED:
        market_provider = ScryfallPriceProvider(
            cache_path=config.SCRYFALL_CACHE_PATH,
            cache_hours=config.SCRYFALL_CACHE_HOURS,
            vendor_currency=config.MARKET_VENDOR_CURRENCY,
            vendor_to_market_rate=config.MARKET_VENDOR_TO_EUR_RATE,
            foil_mode=config.SCRYFALL_FOIL_MODE,
        )
    elif config.MARKET_PRICE_PROVIDER == "cardmarket" and config.CARDMARKET_ENABLED:
        market_provider = CardmarketPriceProvider(
            cache_path=config.CARDMARKET_CACHE_PATH,
            api_base_url=config.CARDMARKET_API_BASE_URL,
            price_guide_base_url=config.CARDMARKET_PRICE_GUIDE_BASE_URL,
            price_source=config.CARDMARKET_PRICE_SOURCE,
            price_guide_fallback=config.CARDMARKET_PRICE_GUIDE_FALLBACK,
            article_cache_minutes=config.CARDMARKET_ARTICLE_CACHE_MINUTES,
            price_guide_cache_hours=config.CARDMARKET_PRICE_GUIDE_CACHE_HOURS,
            product_cache_days=config.CARDMARKET_PRODUCT_CACHE_DAYS,
            language_id=config.CARDMARKET_LANGUAGE_ID,
            min_condition=config.CARDMARKET_MIN_CONDITION,
            foil=config.CARDMARKET_FOIL,
            price_guide_field=config.CARDMARKET_PRICE_GUIDE_FIELD,
            currency=config.CARDMARKET_CURRENCY,
            vendor_currency=config.CARDMARKET_VENDOR_CURRENCY,
            vendor_to_market_rate=config.CARDMARKET_VENDOR_TO_MARKET_RATE,
        )
    elif config.MARKET_PRICE_PROVIDER == "mtgstocks" and config.MTGSTOCKS_ENABLED:
        market_provider = MTGStocksPriceProvider(
            cache_path=config.MTGSTOCKS_CACHE_PATH,
            base_url=config.MTGSTOCKS_BASE_URL,
            api_url=config.MTGSTOCKS_API_URL,
            cache_hours=config.MTGSTOCKS_CACHE_HOURS,
            request_delay_seconds=config.MTGSTOCKS_REQUEST_DELAY_SECONDS,
            currency=config.MTGSTOCKS_CURRENCY,
            vendor_currency=config.MTGSTOCKS_VENDOR_CURRENCY,
            vendor_to_market_rate=config.MTGSTOCKS_VENDOR_TO_MARKET_RATE,
        )
    if market_provider and not market_provider.configured:
        logger.info("Market comparison provider is not configured.")

    if config.ENABLE_IMAGE_SELECTION:
        try:
            price_tiers = load_vendor_price_tiers(config.PRICING_URL)
        except (requests.RequestException, ValueError) as error:
            logger.warning("Live price estimate unavailable: %s", error)
        card_selector = CardSelector(
            catalog_cache_path=config.CATALOG_CACHE_PATH,
            thumbnail_cache_directory=config.THUMBNAIL_CACHE_DIRECTORY,
            thumbnail_cache_max_age_days=config.THUMBNAIL_CACHE_MAX_AGE_DAYS,
            thumbnail_cache_max_size_mb=config.THUMBNAIL_CACHE_MAX_SIZE_MB,
            session_database_path=config.SELECTION_SESSION_DATABASE_PATH,
            price_tiers=price_tiers,
            market_provider=market_provider,
            thumbnail_prefetch_enabled=config.THUMBNAIL_PREFETCH_ENABLED,
            thumbnail_prefetch_workers=config.THUMBNAIL_PREFETCH_WORKERS,
            thumbnail_prefetch_idle_seconds=config.THUMBNAIL_PREFETCH_IDLE_SECONDS,
        )

    selection_finished = process_cards(
        wanted_cards,
        vendor_cards,
        set_counters,
        to_order,
        missing,
        image_links,
        card_selector,
    )

    if selection_finished:
        try:
            save_wanted_cards(config.WANTED_CARDS_SOURCE, to_order)
            logger.info(
                "Persisted %d selected card line(s) to %s.",
                len(to_order),
                config.WANTED_CARDS_SOURCE,
            )
        except (OSError, ValueError) as error:
            logger.warning("Could not persist selected cards to disk: %s", error)

    logger.info("Total Missing Cards: %d", len(missing))
    logger.info(set_counters)

    vendor_estimate = estimate_cost(to_order, price_tiers)
    market_estimate = None
    if market_provider:
        vendor_total = vendor_estimate["total"] if vendor_estimate else None
        market_estimate = market_provider.estimate_orders(to_order, vendor_total)
        if market_estimate.available:
            logger.info(
                "Market benchmark: %s %.2f%s",
                market_estimate.currency,
                market_estimate.market_total,
                (
                    f"; estimated difference: {market_estimate.currency} "
                    f"{market_estimate.saving:.2f}"
                    f" ({market_estimate.saving_percent:.1f}%)"
                    if market_estimate.saving is not None
                    else "; saving comparison needs a configured currency conversion rate"
                ),
            )
            if market_estimate.unavailable:
                logger.warning(
                "Market provider could not price %d card line(s).",
                    len(market_estimate.unavailable),
                )
        elif market_estimate.unavailable and market_provider.configured:
            logger.warning(
                "Market comparison unavailable: %s",
                "; ".join(market_estimate.unavailable[:3]),
            )

    write_output(to_order, missing, market_estimate)

    et = time.process_time()
    logger.debug("CPU Execution time: %s seconds", et - st)
    logger.info("Done!")


if __name__ == "__main__":
    main()
