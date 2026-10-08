<img src="img/mtg_autoproxy.jpg" alt="Generated with https://openart.ai/" width="120" height="120">

# MTG Autoproxy

MTG Autoproxy is a Python program to automatically generate a vendor-specific list of MTG proxies. Currently, the only supported vendor is Blacklotus [1](https://www.aliexpress.com/item/1005001356681692.html?spm=a2g0o.store_home.productList_1386204970.subject_0), [2](https://docs.google.com/spreadsheets/d/1E2iSRhhNUjaZ3MblXx_rflpOIkMwK40OgX5X6yXE4zw/edit#gid=1033599937).

For example, if you have `Ancient Tomb` in the list of wanted cards, the program will select the matching proxy card for it (in this case, `Ancient Tomb English`) based on [Jaro–Winkler distance](http://en.wikipedia.org/wiki/Jaro%E2%80%93Winkler_distance). In the case of blacklotus, it also prioritises regular cards (non foil/holo).

MTG Autoproxy downloads Black Lotus's current catalog automatically, then matches it
against a local wanted-card CSV file. The wanted list only needs `Wanted` and `QTY`
columns; Google Sheet exports remain optional.

The estimated total uses the vendor's live single-card quantity tiers and excludes
delivery costs.

As `output`, it generates a `csv` with the list of [card name, set, quantity] matching with the cards offered by the specific vendor. Additionally, it will generate a `missing.txt` file with the cards that were not found.

Finally, MTG Autoproxy is not fail proof! Always double check the output before ordering your proxies :)

# Prerequisites

- Python 3.10 or higher
- [uv](https://docs.astral.sh/uv/getting-started/installation/)
- A list of wanted cards formatted as `res/wanted_cards.csv`
  
# Running MTG Autoproxy

1. Create the virtual environment and install locked dependencies with `uv sync`.
2. Create a wanted-card list with these columns:

   ```csv
   Wanted,QTY
   Ancient Tomb,1
   "Atraxa, Grand Unifier",2
   ```

   A starter file is available at `res/wanted_cards.csv`.
3. Open `config.py` to adjust the settings you need:
   1. `WANTED_CARDS_SOURCE`: local path of your wanted-card list (defaults to `res/wanted_cards.csv`).
   2. `OUTPUT_DIRECTORY`: folder for `order.csv` and `missing.txt` (defaults to `output`).
   3. `MATCH_FIRST_CANDIDATE`: set to `False` to include every matching version for manual selection.

   The Black Lotus catalog and pricing URLs are kept in a separate section. Leave
   them unchanged unless the vendor restructures its public spreadsheet.
4. Run `uv run main.py`.
5. Check the output in `output/order.csv` and `output/missing.txt`.

# Planned Improvements

- Add more customization options (e.g., preferred card art, preferred card language, etc.).
- Improve search loop.
- Support multiple vendors.
