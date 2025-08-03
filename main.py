import time
import logging
import config
import jellyfish

from utils import CardOrder, load_card_list


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


def process_cards(df, set_counters, to_order, missing):
    for _, row in df.iterrows():
        wanted = row.get("Wanted", "")
        quantity = row.get("QTY", 1)
        if wanted:
            found = False
            for col in df.columns:
                if col not in ["Wanted", "QTY"]:
                    if card_lookup(
                        wanted, df[col], col, quantity, set_counters, to_order
                    ):
                        found = True
                        if config.EXIT_AT_FIRST_MATCH:
                            break
            if not found:
                missing.append(wanted)


def write_output(to_order, missing):
    with open("output/order.csv", "w", newline="") as f:
        f.write("Card Name|Set|Quantity\n")
        for item in to_order:
            f.write(f"{item}\n")

    with open("output/missing.txt", "w", newline="") as f:
        f.write("--- MISSING CARDS ---\n")
        for item in missing:
            f.write(f"{item}\n")


def estimate_cost(set_counters):
    regular = set_counters.get("REGULAR SINGLE CARDS", 0)
    holo = set_counters.get("HOLO SINGLE CARDS", 0)
    foil = set_counters.get("FOIL SINGLE CARDS", 0)
    cost = (
        regular * config.cost_per_card["normal"]
        + (holo + foil) * config.cost_per_card["holo"]
    )
    logging.info(f"Total estimated cost (no discount, no delivery): {cost:.2f} $")


def main():
    setup_logging()
    st = time.process_time()

    df = load_card_list(config.URI)
    to_order = []
    missing = []
    set_counters = {}

    process_cards(df, set_counters, to_order, missing)

    logging.info(f"Total Missing Cards: {len(missing)}")
    logging.info(set_counters)

    write_output(to_order, missing)
    estimate_cost(set_counters)

    et = time.process_time()
    logging.debug(f"CPU Execution time: {et - st} seconds")
    logging.info("Done!")


if __name__ == "__main__":
    main()
