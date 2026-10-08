import csv
from io import BytesIO, StringIO
import re
import pandas as pd
import requests
import logging
from openpyxl import load_workbook


class CardOrder:
    def __init__(self, name, set, quantity=1):
        self.name = name
        self.set = set
        self.quantity = quantity

    def __str__(self):
        return f"{self.name}|{self.set}|{self.quantity}"


class CardCandidate:
    def __init__(self, name, card_set, image_url, score):
        self.name = name
        self.card_set = card_set
        self.image_url = image_url
        self.score = score


def load_card_list(uri, header_row=0, rows_to_skip_after_header=0):
    if uri.startswith(("https:", "http:")):
        df = get_csv_from_gcloud(uri, header_row)
    else:
        df = pd.read_csv(uri, header=header_row)

    if rows_to_skip_after_header:
        df = df.iloc[rows_to_skip_after_header:].reset_index(drop=True)

    df.fillna("", inplace=True)
    return df


def load_vendor_price_tiers(uri):
    price_tiers = []
    for row in csv.reader(StringIO(download_csv(uri))):
        if len(row) < 4:
            continue

        quantity_range = row[0].strip()
        price_text = row[3].strip()
        price_match = re.fullmatch(r"\$(\d+(?:\.\d+)?)\s*/\s*card", price_text)
        quantities = re.findall(r"\d+", quantity_range)
        if price_match and quantities:
            price_tiers.append((int(quantities[0]), float(price_match.group(1))))

    if not price_tiers:
        raise ValueError("No single-card price tiers were found in the vendor sheet")

    return sorted(price_tiers)


def load_vendor_image_links(
    uri, sheet_name, header_row, rows_to_skip_after_header
):
    workbook = load_workbook(BytesIO(download_file(uri)), data_only=True)
    if sheet_name not in workbook.sheetnames:
        raise ValueError(f"Vendor workbook does not contain the '{sheet_name}' sheet")

    worksheet = workbook[sheet_name]
    header_excel_row = header_row + 1
    first_card_row = header_excel_row + rows_to_skip_after_header + 1
    image_links = {}

    for column_index in range(1, worksheet.max_column + 1):
        card_set = worksheet.cell(header_excel_row, column_index).value
        if not card_set:
            continue

        for row_index in range(first_card_row, worksheet.max_row + 1):
            cell = worksheet.cell(row_index, column_index)
            if cell.value and cell.hyperlink and cell.hyperlink.target:
                image_links[(str(card_set), str(cell.value))] = cell.hyperlink.target

    return image_links


def get_csv_from_gcloud(uri, header_row=0):
    return pd.read_csv(StringIO(download_csv(uri)), header=header_row)


def download_csv(uri):
    logging.info("Downloading CSV list from %s...", uri)
    response = requests.get(uri, timeout=30)
    response.raise_for_status()
    return response.text


def download_file(uri):
    logging.info("Downloading file from %s...", uri)
    response = requests.get(uri, timeout=30)
    response.raise_for_status()
    return response.content
