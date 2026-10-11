import time
import logging
from pathlib import Path
import re
import unicodedata
import config
import jellyfish
import requests

from selector import CardSelector
from utils import (
    CardCandidate,
    CardOrder,
    load_vendor_catalog,
    load_vendor_catalog_cache,
    load_vendor_price_tiers,
    load_wanted_cards,
    sync_vendor_catalog_cache,
)


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
    logging.debug("Selected %s from %s", candidate.name, candidate.card_set)
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
        quantity = row.get("QTY", 1) or 1
        if wanted:
            candidates = find_card_candidates(wanted, vendor_cards, image_links)
            if not candidates:
                missing.append(wanted)
                continue

            if card_selector and len(candidates) > 1:
                selection_requests.append((wanted, quantity, candidates))
            elif config.MATCH_FIRST_CANDIDATE:
                add_to_order(candidates[0], quantity, set_counters, to_order)
            else:
                for candidate in candidates:
                    add_to_order(candidate, quantity, set_counters, to_order)

    if not selection_requests:
        return

    try:
        selections, additional_selections = card_selector.select_all(selection_requests)
    except RuntimeError as error:
        logging.warning("Image selector unavailable: %s", error)
        selections = [candidates[0] for _, _, candidates in selection_requests]
        additional_selections = []

    for (wanted, quantity, _), selected in zip(selection_requests, selections):
        if not selected:
            missing.append(wanted)
            continue
        add_to_order(selected, quantity, set_counters, to_order)

    for selected, quantity in additional_selections:
        add_to_order(selected, quantity, set_counters, to_order)


def write_output(to_order, missing):
    output_directory = Path(config.OUTPUT_DIRECTORY)
    output_directory.mkdir(parents=True, exist_ok=True)

    with open(output_directory / "order.csv", "w", newline="") as f:
        f.write("Card Name|Set|Quantity\n")
        for item in to_order:
            f.write(f"{item}\n")

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

    with open(output_directory / "missing.md", "w", encoding="utf-8") as f:
        f.write("# Missing or Skipped Cards\n\n")
        f.write("| Card Name |\n")
        f.write("| --- |\n")
        for item in missing:
            f.write(f"| {markdown_cell(item)} |\n")


def markdown_cell(value):
    return str(value).replace("\\", "\\\\").replace("|", "\\|").replace("\n", " ")


def estimate_cost(to_order):
    total_cards = sum(int(item.quantity) for item in to_order)
    if not total_cards:
        logging.info("No cards found, so no price estimate was calculated.")
        return

    try:
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
        logging.warning("Could not calculate the vendor price: %s", error)
        return

    cost = total_cards * price_per_card
    logging.info(
        "Estimated single-card cost for %d cards at $%.2f/card, excluding delivery: $%.2f",
        total_cards,
        price_per_card,
        cost,
    )


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
            logging.info("Refreshed %d vendor card entries in the local cache.", cached_cards)
        except (OSError, requests.RequestException, ValueError) as error:
            logging.warning("Could not refresh vendor catalog: %s", error)
            logging.info("Using the local vendor catalog cache.")
    else:
        logging.info("Using the local vendor catalog cache without refreshing it.")

    vendor_cards, image_links = load_vendor_catalog_cache(config.CATALOG_CACHE_PATH)
    to_order = []
    missing = []
    set_counters = {}
    card_selector = None

    if config.ENABLE_IMAGE_SELECTION:
        card_selector = CardSelector(config.CATALOG_CACHE_PATH)

    process_cards(
        wanted_cards,
        vendor_cards,
        set_counters,
        to_order,
        missing,
        image_links,
        card_selector,
    )

    logging.info(f"Total Missing Cards: {len(missing)}")
    logging.info(set_counters)

    write_output(to_order, missing)
    estimate_cost(to_order)

    et = time.process_time()
    logging.debug(f"CPU Execution time: {et - st} seconds")
    logging.info("Done!")


if __name__ == "__main__":
    main()
