<img src="assets/cardmirror_icon.png" alt="CardMirror icon" width="120" height="120">

# CardMirror

CardMirror helps you find available MTG proxy versions, choose card art, and
generate an order for the Black Lotus vendor catalog.

## Quick Start

### 1. Install `uv`

On macOS with Homebrew:

```bash
brew install uv
```

Or use the official installer:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

### 2. Install dependencies

From the project directory:

```bash
uv sync
```

### 3. Add wanted cards

Edit `data/wanted_cards.json`:

```json
[
  {"name": "Ancient Tomb", "quantity": 1},
  {"name": "Atraxa, Grand Unifier", "quantity": 2}
]
```

### 4. Run CardMirror

```bash
uv run main.py
```

The app opens a local browser page where you can search cards, browse all
versions, choose thumbnails, set quantities, and confirm your selections.

## Output

After finishing, check:

- `output/order.md` — readable Markdown order table.
- `output/order.csv` — vendor-ready order data.
- `output/missing.txt` — cards that could not be matched.

## Configuration

Common settings are in `src/cardmirror/config.py`:

- `WANTED_CARDS_SOURCE` — input JSON path.
- `ENABLE_IMAGE_SELECTION` — enable or disable the browser picker.
- `MATCH_FIRST_CANDIDATE` — choose only the first match or review versions.
- `REFRESH_CATALOG_CACHE_ON_RUN` — refresh or use the local catalog mirror.
- `MARKET_PRICE_PROVIDER` — use `scryfall`, `cardmarket`, or `mtgstocks`.
- `MTGSTOCKS_ENABLED` — opt into the best-effort MTGStocks provider. It is
  disabled by default because MTGStocks may challenge automated requests.
- `MTGSTOCKS_API_URL` — optional compatible JSON endpoint; otherwise CardMirror
  uses MTGStocks' public HTML search flow with caching and a one-second delay.

To use MTGStocks instead of Scryfall, change these settings in
`src/cardmirror/config.py`:

```python
MARKET_PRICE_PROVIDER = "mtgstocks"
MTGSTOCKS_ENABLED = True
```

CardMirror keeps local caches for the vendor catalog, thumbnails, sessions, and
market prices. These are stored under `output/` and can be deleted when a full
refresh is needed.

## Development

Run the auto-reloading development launcher with:

```bash
uv run dev.py
```

Press `Ctrl+C` to stop it.
