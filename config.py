"""Settings for generating a Black Lotus proxy order."""

# -----------------------------------------------------------------------------
# Your order
# -----------------------------------------------------------------------------

# Path or URL of a JSON card list. Each item needs a `name` and optional `quantity`.
WANTED_CARDS_SOURCE = "res/wanted_cards.json"

# Folder where order.csv and missing.txt are created.
OUTPUT_DIRECTORY = "output"

# True adds only the first match for each requested card. Set to False to include
# every match so you can select a specific version manually.
MATCH_FIRST_CANDIDATE = True

# Open a local browser picker when a requested card has multiple matching versions.
ENABLE_IMAGE_SELECTION = True

# Minimum similarity for a match when the normalized card names are not exact.
FUZZY_MATCH_THRESHOLD = 0.91


# -----------------------------------------------------------------------------
# Black Lotus vendor integration
# -----------------------------------------------------------------------------

VENDOR_NAME = "Black Lotus"

# Public workbook. The header and skip settings account for the sheet's title and
# link rows; change them only if the vendor restructures the workbook.
CATALOG_CACHE_PATH = "output/vendor_catalog.sqlite3"

# True refreshes the complete local catalog mirror from the vendor workbook before
# every run. Set to False to run entirely from the most recently synced database.
REFRESH_CATALOG_CACHE_ON_RUN = True

CATALOG_HEADER_ROW = 1
CATALOG_ROWS_TO_SKIP_AFTER_HEADER = 2
CATALOG_WORKBOOK_URL = (
    "https://docs.google.com/spreadsheets/d/"
    "1E2iSRhhNUjaZ3MblXx_rflpOIkMwK40OgX5X6yXE4zw/export?format=xlsx"
)
CATALOG_WORKBOOK_SHEET_NAMES = (
    "SINGLES",
    "Regular Sets",
    "Holo Sets",
    "Special Foil Sets",
    "Foil Sets",
)

# Public instructions export containing the live single-card quantity tiers.
PRICING_URL = (
    "https://docs.google.com/spreadsheets/d/"
    "1E2iSRhhNUjaZ3MblXx_rflpOIkMwK40OgX5X6yXE4zw/export?format=csv&gid=1033599937"
)


# -----------------------------------------------------------------------------
# Diagnostics
# -----------------------------------------------------------------------------

LOG_LEVEL = "INFO"
