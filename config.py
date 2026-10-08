"""Settings for generating a Black Lotus proxy order."""

# -----------------------------------------------------------------------------
# Your order
# -----------------------------------------------------------------------------

# Path or URL of a CSV with a required `Wanted` column and optional `QTY` column.
WANTED_CARDS_SOURCE = "res/wanted_cards.csv"

# Folder where order.csv and missing.txt are created.
OUTPUT_DIRECTORY = "output"

# True adds only the first match for each requested card. Set to False to include
# every match so you can select a specific version manually.
MATCH_FIRST_CANDIDATE = True

# Open a local browser picker when a requested card has multiple matching versions.
ENABLE_IMAGE_SELECTION = True

IMAGE_SELECTION_COLUMNS = 3


# -----------------------------------------------------------------------------
# Black Lotus vendor integration
# -----------------------------------------------------------------------------

VENDOR_NAME = "Black Lotus"

# Public catalog export. The header and skip settings account for the sheet's
# title and link rows; change them only if the vendor restructures the sheet.
CATALOG_URL = (
    "https://docs.google.com/spreadsheets/d/"
    "1E2iSRhhNUjaZ3MblXx_rflpOIkMwK40OgX5X6yXE4zw/export?format=csv&gid=251059144"
)
CATALOG_HEADER_ROW = 1
CATALOG_ROWS_TO_SKIP_AFTER_HEADER = 2
CATALOG_WORKBOOK_URL = (
    "https://docs.google.com/spreadsheets/d/"
    "1E2iSRhhNUjaZ3MblXx_rflpOIkMwK40OgX5X6yXE4zw/export?format=xlsx"
)
CATALOG_WORKBOOK_SHEET_NAME = "Holo Sets"

# Public instructions export containing the live single-card quantity tiers.
PRICING_URL = (
    "https://docs.google.com/spreadsheets/d/"
    "1E2iSRhhNUjaZ3MblXx_rflpOIkMwK40OgX5X6yXE4zw/export?format=csv&gid=1033599937"
)


# -----------------------------------------------------------------------------
# Diagnostics
# -----------------------------------------------------------------------------

LOG_LEVEL = "INFO"
