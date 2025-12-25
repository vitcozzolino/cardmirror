import time
import logging
import config
import jellyfish

from utils import CardOrder, load_card_list

logging.basicConfig(
    level=logging.getLevelName(config.LOG_LEVEL),
    format="[%(asctime)s]::[%(levelname)s]::%(message)s",
)


# Vars
to_order = []
missing = []
set_counters = {}


def card_lookup(card_name: str, df_col, set, quantity=1):
    found = False
    similarity_score = 0

    for elem in df_col:
        skip = False
        if card_name.lower() in elem.lower() and elem.lower().startswith(
            card_name.lower()
        ):
            tokens_a = card_name.lower().split(" ")
            tokens_b = elem.lower().split(" ")

            # Priority token matching based on wanted card name
            # to avoid the "Damn" -> "Damnation" 'cases'.
            for idx, word_r in enumerate(tokens_a):
                if word_r != tokens_b[idx]:
                    skip = True

            if not skip and (
                jellyfish.jaro_winkler_similarity(card_name.lower(), elem.lower())
                > similarity_score
            ):
                temp_set = set
                temp_name = elem
                found = True
                similarity_score = jellyfish.jaro_winkler_similarity(
                    card_name.lower(), elem.lower()
                )
                logging.debug(f"{card_name} {elem} {similarity_score}")

    if found:
        to_order.append(CardOrder(temp_name, temp_set, quantity)) # type: ignore
        if set in set_counters:
            set_counters[set] += int(quantity)
        else:
            set_counters[set] = 1

    return found


if __name__ == "__main__":
    st = time.process_time()

    # Loads card list from disk or remote link
    df = load_card_list(config.URI)

    # Main search loop. Inefficient, but it works
    for index, row in df.iterrows():
        found = False
        if row["Wanted"] != "":
            for col in df.columns:
                if col not in ["Wanted", "QTY"] and card_lookup(
                    row["Wanted"], df[col], col, quantity=row["QTY"]
                ):
                    found = True
                    if config.EXIT_AT_FIRST_MATCH:
                        break

            if not found:
                missing.append(row["Wanted"])

    logging.info("Total Missing Cards: %d" % len(missing))
    logging.info(set_counters)

    logging.info(
        "Total estimated cost (no discount, no delivery): %.2f $"
        % (
            set_counters["REGULAR SINGLE CARDS"] * config.cost_per_card["normal"]
            + (
                (set_counters["HOLO SINGLE CARDS"] + set_counters["FOIL SINGLE CARDS"])
                * config.cost_per_card["holo"]
            )
        )
    )

    # Write on disk
    with open("output/order.csv", "w", newline="") as f:
        f.write(f"Card Name|Set|Quantity\n")
        for item in to_order:
            f.write("%s\n" % item)

    with open("output/missing.txt", "w", newline="") as f:
        f.write("--- MISSING CARDS ---\n")
        for item in missing:
            f.write("%s\n" % item)

    # get the end time
    et = time.process_time()

    # get execution time
    res = et - st
    logging.debug(f"CPU Execution time: {res} seconds")
    logging.info("Done!")
