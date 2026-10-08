import time
import logging
from pathlib import Path
import re
import config
import jellyfish
import requests

from selector import CardSelector
from utils import (
    CardCandidate,
    CardOrder,
    load_card_list,
    load_vendor_image_links,
    load_vendor_price_tiers,
)


def setup_logging():
    """Sets up the logging configuration based on the provided log level in the config.
    The log format includes the timestamp, log level, and message.
    """
    logging.basicConfig(
        level=logging.getLevelName(config.LOG_LEVEL),
        format="[%(asctime)s]::[%(levelname)s]::%(message)s",
    )


def find_card_candidates(card_name, vendor_cards, image_links):
    card_name_lower = card_name.lower()
    candidates = []

    for card_set in vendor_cards.columns:
        card_set_candidates = []
        for candidate in vendor_cards[card_set]:
            if not isinstance(candidate, str):
                continue

            candidate_lower = candidate.lower()
            if not candidate_lower.startswith(card_name_lower):
                continue

            wanted_tokens = card_name_lower.split()
            candidate_tokens = candidate_lower.split()
            if not all(
                wanted_tokens[index] == candidate_tokens[index]
                for index in range(min(len(wanted_tokens), len(candidate_tokens)))
            ):
                continue

            score = jellyfish.jaro_winkler_similarity(card_name_lower, candidate_lower)
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

    return candidates


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
    for _, row in wanted_cards.iterrows():
        wanted = row.get("Wanted", "")
        quantity = row.get("QTY", 1) or 1
        if wanted:
            candidates = find_card_candidates(wanted, vendor_cards, image_links)
            if not candidates:
                missing.append(wanted)
                continue

            if card_selector and len(candidates) > 1:
                try:
                    selected = card_selector.select(wanted, candidates)
                except RuntimeError as error:
                    logging.warning("Image selector unavailable: %s", error)
                    card_selector = None
                    selected = candidates[0]
                if not selected:
                    missing.append(wanted)
                    continue
                add_to_order(selected, quantity, set_counters, to_order)
            elif config.MATCH_FIRST_CANDIDATE:
                add_to_order(candidates[0], quantity, set_counters, to_order)
            else:
                for candidate in candidates:
                    add_to_order(candidate, quantity, set_counters, to_order)


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

    wanted_cards = load_card_list(config.WANTED_CARDS_SOURCE)
    vendor_cards = load_card_list(
        config.CATALOG_URL,
        header_row=config.CATALOG_HEADER_ROW,
        rows_to_skip_after_header=config.CATALOG_ROWS_TO_SKIP_AFTER_HEADER,
    )
    to_order = []
    missing = []
    set_counters = {}
    image_links = {}
    card_selector = None

    if config.ENABLE_IMAGE_SELECTION:
        try:
            image_links = load_vendor_image_links(
                config.CATALOG_WORKBOOK_URL,
                config.CATALOG_WORKBOOK_SHEET_NAME,
                config.CATALOG_HEADER_ROW,
                config.CATALOG_ROWS_TO_SKIP_AFTER_HEADER,
            )
            card_selector = CardSelector(
                config.IMAGE_SELECTION_COLUMNS,
            )
        except (OSError, requests.RequestException, ValueError) as error:
            logging.warning("Could not load vendor image links: %s", error)

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
