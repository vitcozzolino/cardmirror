<img src="img/mtg_autoproxy.jpg" alt="Generated with https://openart.ai/" width="120" height="120">

# MTG Autoproxy

MTG Autoproxy is a Python program to automatically generate a vendor-specific list of MTG proxies. Currently, the only supported vendor is Blacklotus [1](https://www.aliexpress.com/item/1005001356681692.html?spm=a2g0o.store_home.productList_1386204970.subject_0), [2](https://docs.google.com/spreadsheets/d/1E2iSRhhNUjaZ3MblXx_rflpOIkMwK40OgX5X6yXE4zw/edit#gid=1033599937).

For example, if you have `Ancient Tomb` in the list of wanted cards, the program will select the matching proxy card for it (in this case, `Ancient Tomb English`) based on [Jaro–Winkler distance](http://en.wikipedia.org/wiki/Jaro%E2%80%93Winkler_distance). In the case of blacklotus, it also prioritises regular cards (non foil/holo).

MTG Autoproxy downloads Black Lotus's current catalog automatically, then matches it
against a local wanted-card JSON file. Each entry needs a `name` and optional
`quantity`; Google Sheet CSV exports remain optional.

The estimated total uses the vendor's live single-card quantity tiers and excludes
delivery costs.

The picker shows the same live estimate in the top-right of the order table. It
recalculates from active row quantities as cards are confirmed, edited, or cancelled.

Card matching searches the vendor's singles, regular, holo, and foil tabs. Names
are normalized before comparison, so punctuation and alternate names in parentheses
do not hide available versions.

The vendor workbook is mirrored into `output/vendor_catalog.sqlite3` on each
successful refresh. This speeds up interactive search and lets matching continue
from the most recent cache when the vendor workbook is unavailable.

Picker thumbnails are stored in `output/thumbnail_cache` using a content-addressed
blob store and SQLite metadata. Cached images are reused immediately; older entries
are revalidated with HTTP cache validators and the cache is bounded to avoid growing
without limit.

Set `REFRESH_CATALOG_CACHE_ON_RUN` to `False` in `config.py` to run solely from
the complete local catalog mirror. Leave it as `True` to refresh the mirror before
each run, then load the refreshed database for matching and interactive search.

When cards have multiple available versions, the app preloads their linked images
in parallel, then opens one local browser page. Each requested card is a row of
thumbnails; choose one option in every row and save the selections to write them
to `output/order.csv`. Set `ENABLE_IMAGE_SELECTION` to `False` in `config.py` to
use automatic matching without the picker.

The picker also has a top-level catalog search. Search results identify a card, and
`Choose version` opens every matching set/version without preselecting anything.
Choose a favourite explicitly before confirming it. Added rows can be cancelled
before saving.

Saving opens an in-browser recap and keeps the local session backend running. Use
`Continue editing` to go back, `Finish order` to finalize the session, or `Cancel
session` to discard it. Session state is persisted in
`output/selection_sessions.sqlite3` while the picker is open.

As `output`, it generates a `csv` with the list of [card name, set, quantity] matching with the cards offered by the specific vendor. Additionally, it will generate a `missing.txt` file with the cards that were not found.

Finally, MTG Autoproxy is not fail proof! Always double check the output before ordering your proxies :)

# Prerequisites

- Python 3.10 or higher
- [uv](https://docs.astral.sh/uv/getting-started/installation/)
- A list of wanted cards formatted as `res/wanted_cards.json`
  
# Running MTG Autoproxy

1. Create the virtual environment and install locked dependencies with `uv sync`.
2. Create a wanted-card JSON list:

   ```json
   [
     {"name": "Ancient Tomb", "quantity": 1},
     {"name": "Atraxa, Grand Unifier", "quantity": 2}
   ]
   ```

   A starter file is available at `res/wanted_cards.json`.
3. Open `config.py` to adjust the settings you need:
   1. `WANTED_CARDS_SOURCE`: local path of your wanted-card list (defaults to `res/wanted_cards.json`).
   2. `OUTPUT_DIRECTORY`: folder for CSV and Markdown order files (defaults to `output`).
   3. `MATCH_FIRST_CANDIDATE`: set to `False` to include every matching version for manual selection.
   4. `ENABLE_IMAGE_SELECTION`: set to `False` to skip the visual card picker.
   5. `FUZZY_MATCH_THRESHOLD`: increase this value (maximum `1.0`) to make fuzzy matching more strict.
   6. `THUMBNAIL_CACHE_MAX_AGE_DAYS` and `THUMBNAIL_CACHE_MAX_SIZE_MB`: control
      thumbnail revalidation and the local cache size.

   The Black Lotus catalog and pricing URLs are kept in a separate section. Leave
   them unchanged unless the vendor restructures its public spreadsheet.
4. Run `uv run main.py`.
5. Check `output/order.md` for the Markdown order table. `output/order.csv` remains available for vendor ordering.

# Planned Improvements

- Add more customization options (e.g., preferred card art, preferred card language, etc.).
- Improve search loop.
- Support multiple vendors.
