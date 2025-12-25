import csv
import pandas as pd
import requests
import logging


class CardOrder:
    def __init__(self, name, set, quantity=1):
        self.name = name
        self.set = set
        self.quantity = quantity

    def __str__(self):
        return f"{self.name}|{self.set}|{self.quantity}"


def load_card_list(URI):
    if URI.startswith("https:") or URI.startswith("http:"):
        df = get_csv_from_gcloud(URI)
    else:
        df = pd.read_csv(URI, header=0)

    df.fillna("", inplace=True)
    return df

def get_csv_from_gcloud(URI):
    logging.info("Downloading csv list from Google Cloud...")
    with requests.get(URI, stream=True) as r:
        lines = (line.decode("utf-8") for line in r.iter_lines())
        csv_to_list = list(csv.reader(lines))
        return pd.DataFrame(csv_to_list[1:], columns=csv_to_list[0])