import time
import logging
from pathlib import Path
import config
import jellyfish
import requests

from utils import CardOrder, load_card_list, load_vendor_price_tiers


def setup_logging():
    """Sets up the logging configuration based on the provided log level in the config.
    The log format includes the timestamp, log level, and message.
    """
    logging.basicConfig(
        level=logging.getLevelName(config.LOG_LEVEL),
        format="[%(asctime)s]::[%(levelname)s]::%(message)s",
    )


def card_lookup(
    card_name: str, candidates, card_set, quantity=1, set_counters=None, to_order=None
):
    """
    Attempts to find the best matching card name from a list of candidates and adds it to the order list.
    Args:
        card_name (str): The name of the card to look up.
        candidates (list): A list of candidate card names to search through.
        card_set: The set to which the card belongs.
        quantity (int, optional): The number of cards to order. Defaults to 1.
        set_counters (dict, optional): A dictionary tracking the count of cards per set.
        to_order (list, optional): A list to append the CardOrder object to.
    Returns:
        bool: True if a matching card was found and added to the order, False otherwise.
    """
    best_match = None
    best_score = 0

    for candidate in candidates:
        candidate_lower = candidate.lower()
        card_name_lower = card_name.lower()
        if card_name_lower in candidate_lower and candidate_lower.startswith(
            card_name_lower
        ):
            tokens_a = card_name_lower.split()
            tokens_b = candidate_lower.split()
            if all(
                tokens_a[i] == tokens_b[i]
                for i in range(min(len(tokens_a), len(tokens_b)))
            ):
                score = jellyfish.jaro_winkler_similarity(
                    card_name_lower, candidate_lower
                )
                if score > best_score:
                    best_score = score
                    best_match = candidate

    if best_match:
        logging.debug(f"{card_name} {best_match} {best_score}")
        to_order.append(CardOrder(best_match, card_set, quantity))
        set_counters[card_set] = set_counters.get(card_set, 0) + int(quantity)
        return True
    return False


def process_cards(wanted_cards, vendor_cards, set_counters, to_order, missing):
    required_columns = {"Wanted"}
    missing_columns = required_columns - set(wanted_cards.columns)
    if missing_columns:
        columns = ", ".join(sorted(missing_columns))
        raise ValueError(f"Wanted-card list is missing required columns: {columns}")

    for _, row in wanted_cards.iterrows():
        wanted = row.get("Wanted", "")
        quantity = row.get("QTY", 1) or 1
        if wanted:
            found = False
            for card_set in vendor_cards.columns:
                if card_lookup(
                    wanted,
                    vendor_cards[card_set],
                    card_set,
                    quantity,
                    set_counters,
                    to_order,
                ):
                    found = True
                    if config.MATCH_FIRST_CANDIDATE:
                        break
            if not found:
                missing.append(wanted)


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

    process_cards(wanted_cards, vendor_cards, set_counters, to_order, missing)

    logging.info(f"Total Missing Cards: {len(missing)}")
    logging.info(set_counters)

    write_output(to_order, missing)
    estimate_cost(to_order)

    et = time.process_time()
    logging.debug(f"CPU Execution time: {et - st} seconds")
    logging.info("Done!")


if __name__ == "__main__":
    main()
