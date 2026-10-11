import base64
import hashlib
import json
import logging
import os
import sqlite3
import threading
import time
import uuid
import webbrowser
from concurrent.futures import ThreadPoolExecutor, as_completed
from html import escape
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urljoin, urlparse

import requests

from utils import (
    load_vendor_catalog_card,
    request_remote,
    search_vendor_catalog_cache,
    search_vendor_catalog_versions,
)

logger = logging.getLogger(__name__)


MAX_SELECTION_BODY_BYTES = 256 * 1024
MAX_SEARCH_QUERY_LENGTH = 200


class OpenGraphImageParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.image_url = None

    def handle_starttag(self, tag, attrs):
        if tag != "meta" or self.image_url:
            return

        attributes = dict(attrs)
        if attributes.get("property") == "og:image":
            self.image_url = attributes.get("content")


class ThumbnailCache:
    def __init__(self, cache_directory, max_age_days=30, max_size_mb=512):
        self.cache_directory = Path(cache_directory)
        self.blob_directory = self.cache_directory / "blobs"
        self.database_path = self.cache_directory / "index.sqlite3"
        self.max_age_seconds = max_age_days * 24 * 60 * 60
        self.max_size_bytes = max_size_mb * 1024 * 1024
        self.blob_directory.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS thumbnails (
                    source_url TEXT PRIMARY KEY,
                    resolved_url TEXT NOT NULL,
                    blob_name TEXT NOT NULL,
                    content_type TEXT NOT NULL,
                    byte_size INTEGER NOT NULL,
                    source_etag TEXT,
                    source_last_modified TEXT,
                    image_etag TEXT,
                    image_last_modified TEXT,
                    fetched_at REAL NOT NULL,
                    last_used_at REAL NOT NULL
                )
                """
            )

    def get_or_fetch(self, source_url):
        record = self._get_record(source_url)
        cached_image = self._read_cached_image(record)
        if cached_image and time.time() - record["fetched_at"] <= self.max_age_seconds:
            self._touch(source_url)
            return cached_image[0], cached_image[1], True

        source_headers = self._conditional_headers(record, "source")
        source_response = request_remote(
            source_url,
            headers=source_headers,
            timeout=30,
        )
        if source_response.status_code == 304 and cached_image:
            source_response.close()
            self._touch(source_url)
            return cached_image[0], cached_image[1], True
        if source_response.status_code == 304:
            source_response.close()
            source_response = request_remote(source_url, timeout=30)
        try:
            source_response.raise_for_status()
            parser = OpenGraphImageParser()
            parser.feed(source_response.text)
            source_response_headers = dict(source_response.headers)
        finally:
            source_response.close()
        if not parser.image_url:
            raise ValueError("The image page does not expose an Open Graph image")

        image_response = request_remote(
            urljoin(source_url, parser.image_url),
            headers=self._conditional_headers(record, "image"),
            stream=True,
            timeout=30,
        )
        if image_response.status_code == 304 and cached_image:
            image_response.close()
            self._touch(source_url)
            return cached_image[0], cached_image[1], True
        if image_response.status_code == 304:
            image_response.close()
            image_response = request_remote(
                urljoin(source_url, parser.image_url),
                stream=True,
                timeout=30,
            )
        try:
            image_response.raise_for_status()
            content_type = image_response.headers.get("Content-Type", "image/jpeg")
            content_type = content_type.split(";", 1)[0].strip().lower()
            if content_type not in {"image/gif", "image/jpeg", "image/png", "image/webp"}:
                raise ValueError("The remote response is not a supported image")
            image_headers = dict(image_response.headers)
            image_bytes = self._read_limited_image(image_response)
        finally:
            image_response.close()
        self._store(
            source_url,
            parser.image_url,
            content_type,
            image_bytes,
            source_response_headers,
            image_headers,
        )
        return image_bytes, content_type, False

    def _connect(self):
        connection = sqlite3.connect(self.database_path, timeout=30)
        connection.execute("PRAGMA busy_timeout = 30000")
        connection.execute("PRAGMA journal_mode = WAL")
        return connection

    def _get_record(self, source_url):
        with self._connect() as connection:
            connection.row_factory = sqlite3.Row
            return connection.execute(
                "SELECT * FROM thumbnails WHERE source_url = ?",
                (source_url,),
            ).fetchone()

    def _read_cached_image(self, record):
        if not record:
            return None
        blob_path = self.blob_directory / record["blob_name"]
        if not blob_path.is_file():
            return None
        return blob_path.read_bytes(), record["content_type"]

    @staticmethod
    def _conditional_headers(record, prefix):
        if not record:
            return {}
        headers = {}
        if record[f"{prefix}_etag"]:
            headers["If-None-Match"] = record[f"{prefix}_etag"]
        if record[f"{prefix}_last_modified"]:
            headers["If-Modified-Since"] = record[f"{prefix}_last_modified"]
        return headers

    def _store(
        self,
        source_url,
        resolved_url,
        content_type,
        image_bytes,
        source_headers,
        image_headers,
    ):
        blob_name = hashlib.sha256(image_bytes).hexdigest()
        blob_path = self.blob_directory / blob_name
        if not blob_path.exists():
            temporary_path = self.blob_directory / f".{blob_name}.tmp"
            temporary_path.write_bytes(image_bytes)
            os.replace(temporary_path, blob_path)

        now = time.time()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO thumbnails (
                    source_url, resolved_url, blob_name, content_type, byte_size,
                    source_etag, source_last_modified, image_etag,
                    image_last_modified, fetched_at, last_used_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(source_url) DO UPDATE SET
                    resolved_url = excluded.resolved_url,
                    blob_name = excluded.blob_name,
                    content_type = excluded.content_type,
                    byte_size = excluded.byte_size,
                    source_etag = excluded.source_etag,
                    source_last_modified = excluded.source_last_modified,
                    image_etag = excluded.image_etag,
                    image_last_modified = excluded.image_last_modified,
                    fetched_at = excluded.fetched_at,
                    last_used_at = excluded.last_used_at
                """,
                (
                    source_url,
                    resolved_url,
                    blob_name,
                    content_type,
                    len(image_bytes),
                    source_headers.get("ETag"),
                    source_headers.get("Last-Modified"),
                    image_headers.get("ETag"),
                    image_headers.get("Last-Modified"),
                    now,
                    now,
                ),
            )
        self._prune()

    def _touch(self, source_url):
        with self._connect() as connection:
            connection.execute(
                "UPDATE thumbnails SET last_used_at = ? WHERE source_url = ?",
                (time.time(), source_url),
            )

    def _prune(self):
        with self._connect() as connection:
            total_size = connection.execute(
                "SELECT COALESCE(SUM(byte_size), 0) FROM thumbnails"
            ).fetchone()[0]
            if total_size <= self.max_size_bytes:
                return
            records = connection.execute(
                "SELECT source_url, blob_name, byte_size FROM thumbnails ORDER BY last_used_at"
            ).fetchall()
            for source_url, blob_name, byte_size in records:
                if total_size <= self.max_size_bytes:
                    break
                (self.blob_directory / blob_name).unlink(missing_ok=True)
                connection.execute(
                    "DELETE FROM thumbnails WHERE source_url = ?",
                    (source_url,),
                )
                total_size -= byte_size

    @staticmethod
    def _read_limited_image(response, max_bytes=12 * 1024 * 1024):
        content_length = response.headers.get("Content-Length")
        if content_length and int(content_length) > max_bytes:
            raise ValueError("The thumbnail exceeds the configured download limit")
        chunks = []
        total_bytes = 0
        for chunk in response.iter_content(chunk_size=64 * 1024):
            total_bytes += len(chunk)
            if total_bytes > max_bytes:
                raise ValueError("The thumbnail exceeds the configured download limit")
            chunks.append(chunk)
        return b"".join(chunks)


class SelectionSessionStore:
    def __init__(self, database_path):
        self.database_path = Path(database_path)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS selection_sessions (
                    session_id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                )
                """
            )

    def create(self):
        session_id = uuid.uuid4().hex
        now = time.time()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO selection_sessions (
                    session_id, status, payload, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (session_id, "editing", "{}", now, now),
            )
        return session_id

    def save(self, session_id, selected_rows, skipped_rows):
        payload = {
            "selected": [
                {
                    "requested": requested,
                    "name": candidate.name,
                    "set": candidate.card_set,
                    "image_url": candidate.image_url,
                    "quantity": quantity,
                }
                for requested, candidate, quantity in selected_rows
            ],
            "skipped": skipped_rows,
        }
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE selection_sessions
                SET status = ?, payload = ?, updated_at = ?
                WHERE session_id = ?
                """,
                ("reviewing", json.dumps(payload), time.time(), session_id),
            )

    def finish(self, session_id, status="finished"):
        with self._connect() as connection:
            connection.execute(
                "UPDATE selection_sessions SET status = ?, updated_at = ? WHERE session_id = ?",
                (status, time.time(), session_id),
            )

    def _connect(self):
        connection = sqlite3.connect(self.database_path, timeout=30)
        connection.execute("PRAGMA busy_timeout = 30000")
        connection.execute("PRAGMA journal_mode = WAL")
        return connection


class CardSelector:
    def __init__(
        self,
        catalog_cache_path,
        thumbnail_cache_directory="output/thumbnail_cache",
        thumbnail_cache_max_age_days=30,
        thumbnail_cache_max_size_mb=512,
        session_database_path="output/selection_sessions.sqlite3",
        price_tiers=None,
    ):
        self.catalog_cache_path = catalog_cache_path
        self.price_tiers = list(price_tiers or [])
        self.thumbnail_cache = ThumbnailCache(
            thumbnail_cache_directory,
            max_age_days=thumbnail_cache_max_age_days,
            max_size_mb=thumbnail_cache_max_size_mb,
        )
        self.session_store = SelectionSessionStore(session_database_path)

    def select_all(self, selection_requests):
        image_data = self._preload_images(selection_requests)
        selections = [None] * len(selection_requests)
        selection_quantities = [quantity for _, quantity, _ in selection_requests]
        additional_selections = []
        completed = threading.Event()
        session_id = self.session_store.create()
        page, image_ids = self._build_page(selection_requests, image_data)
        handler = self._create_handler(
            page,
            image_data,
            image_ids,
            selection_requests,
            selections,
            selection_quantities,
            additional_selections,
            completed,
            self.session_store,
            session_id,
        )
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        url = f"http://127.0.0.1:{server.server_port}/?session={session_id}"

        try:
            if not webbrowser.open(url):
                raise RuntimeError(f"Could not open the card picker at {url}")
            completed.wait()
        finally:
            server.shutdown()
            server.server_close()
            server_thread.join()

        selected_candidates = [
            candidates[index] if index is not None else None
            for (_, _, candidates), index in zip(selection_requests, selections)
        ]
        return selected_candidates, selection_quantities, additional_selections

    def _preload_images(self, selection_requests):
        image_page_urls = {
            candidate.image_url
            for _, _, candidates in selection_requests
            for candidate in candidates
            if candidate.image_url
        }
        return self._preload_image_urls(image_page_urls)

    def _preload_image_urls(self, image_page_urls):
        if not image_page_urls:
            return {}

        logger.info("Preloading %d card images for interactive selection...", len(image_page_urls))
        image_data = {}
        with ThreadPoolExecutor(max_workers=min(12, len(image_page_urls))) as executor:
            futures = {
                executor.submit(self._load_image, image_page_url): image_page_url
                for image_page_url in image_page_urls
            }
            cache_hits = 0
            for completed_count, future in enumerate(as_completed(futures), start=1):
                image_page_url = futures[future]
                try:
                    image, content_type, cache_hit = future.result()
                    image_data[image_page_url] = image, content_type
                    cache_hits += int(cache_hit)
                except (OSError, requests.RequestException, ValueError, sqlite3.Error) as error:
                    logger.warning(
                        "Could not preload card image from %s: %s",
                        image_page_url,
                        error,
                    )
                logger.info(
                    "Preloaded card images: %d/%d",
                    completed_count,
                    len(image_page_urls),
                )

        logger.info(
            "Thumbnail cache: %d hit(s), %d fetched.",
            cache_hits,
            len(image_data) - cache_hits,
        )
        return image_data

    def _load_image(self, image_page_url):
        return self.thumbnail_cache.get_or_fetch(image_page_url)

    def _download_image(self, image_page_url):
        response = request_remote(image_page_url, timeout=30)
        try:
            response.raise_for_status()
            parser = OpenGraphImageParser()
            parser.feed(response.text)
        finally:
            response.close()
        if not parser.image_url:
            raise ValueError("The image page does not expose an Open Graph image")

        image_response = request_remote(
            urljoin(image_page_url, parser.image_url),
            timeout=30,
        )
        try:
            image_response.raise_for_status()
            content_type = image_response.headers.get("Content-Type", "image/jpeg")
            content_type = content_type.split(";", 1)[0].strip().lower()
            if content_type not in {"image/gif", "image/jpeg", "image/png", "image/webp"}:
                raise ValueError("The remote response is not a supported image")
            return image_response.content, content_type
        finally:
            image_response.close()

    def _build_page(self, selection_requests, image_data):
        image_ids = {image_page_url: index for index, image_page_url in enumerate(image_data)}
        price_tiers_json = json.dumps(self.price_tiers)
        rows = []
        for request_index, (wanted, quantity, candidates) in enumerate(selection_requests):
            options = []
            for candidate_index, candidate in enumerate(candidates):
                image_id = image_ids.get(candidate.image_url)
                image = (
                    f'<img src="/images/{image_id}" alt="{escape(candidate.name)}">'
                    if image_id is not None
                    else '<div class="missing-image">Image unavailable</div>'
                )
                options.append(
                    f'<label class="option"><input type="radio" '
                    f'name="selection-{request_index}" value="{candidate_index}">'
                    f"{image}<strong>{escape(candidate.name)}</strong>"
                    f"<span>{escape(candidate.card_set)}</span></label>"
                )
            rows.append(
                f'<section class="card-row" data-row-key="initial-{request_index}">'
                f'<details class="card-panel"{(" open" if request_index == 0 else "")}>'
                f'<summary><span class="row-name">{escape(wanted)}</span>'
                f'<span class="row-summary-meta"><span class="status">Needs confirmation</span>'
                f'<span class="summary-quantity">{quantity} item(s)</span></span></summary>'
                f'<div class="row-content"><div class="row-controls">'
                f'<label>Quantity <input class="quantity-input" type="number" '
                f'name="quantity-{request_index}" min="1" value="{quantity}"></label>'
                f'<input type="hidden" name="confirmed-{request_index}" value="0">'
                f'<div class="row-actions"><button type="button" class="cancel-row">Cancel</button>'
                f'<button type="button" class="confirm-row">Confirm card</button></div></div>'
                f'<div class="options">{"".join(options)}</div></div></details></section>'
            )

        return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Choose proxy versions</title>
<style>
:root {{
  --background: #101311;
  --surface: #171c19;
  --surface-raised: #202722;
  --surface-hover: #29332c;
  --border: #344039;
  --border-strong: #4f6254;
  --text: #f4f7f3;
  --muted: #9ca99f;
  --accent: #b8f36b;
  --accent-dark: #243619;
  --success: #79d98b;
  --warning: #f0c66b;
}}
* {{ box-sizing: border-box; }}
body {{ background: radial-gradient(circle at 50% -20%, #26352a 0, var(--background) 43rem); color: var(--text); font-family: Inter, ui-sans-serif, system-ui, -apple-system, sans-serif; line-height: 1.4; margin: 0; min-width: 320px; padding-bottom: 84px; }}
header {{ backdrop-filter: blur(18px); background: rgba(16, 19, 17, .86); border-bottom: 1px solid rgba(79, 98, 84, .55); padding: 24px max(24px, calc((100vw - 1100px) / 2)); position: sticky; top: 0; z-index: 2; }}
.header-inner {{ margin: 0 auto; max-width: 1100px; }}
.eyebrow {{ color: var(--accent); font-size: .7rem; font-weight: 800; letter-spacing: .14em; margin: 0; text-transform: uppercase; }}
h1 {{ font-size: clamp(1.65rem, 3vw, 2.35rem); letter-spacing: -.04em; line-height: 1.05; margin: 8px 0; }}
.hint {{ color: var(--muted); margin: 0 0 20px; }}
.search-label {{ color: var(--muted); display: block; font-size: .75rem; font-weight: 750; letter-spacing: .08em; margin: 0 0 7px; text-transform: uppercase; }}
.search {{ max-width: 760px; position: relative; }}
.search input {{ background: rgba(32, 39, 34, .9); border: 1px solid var(--border-strong); border-radius: 10px; box-shadow: 0 8px 26px rgba(0, 0, 0, .18); color: var(--text); font-size: 1rem; outline: none; padding: 13px 15px; transition: border-color .16s, box-shadow .16s; width: 100%; }}
.search input:focus {{ border-color: var(--accent); box-shadow: 0 0 0 3px rgba(184, 243, 107, .15); }}
.suggestions {{ background: #1e251f; border: 1px solid var(--border-strong); border-radius: 10px; box-shadow: 0 18px 34px rgba(0, 0, 0, .35); display: none; left: 0; max-height: 330px; overflow-y: auto; position: absolute; right: 0; top: 49px; z-index: 3; }}
.suggestion {{ align-items: center; background: transparent; border: 0; border-bottom: 1px solid var(--border); color: var(--text); cursor: pointer; display: flex; gap: 12px; padding: 9px; text-align: left; transition: background .12s; width: 100%; }}
.suggestion:last-child {{ border-bottom: 0; }}
.suggestion:hover, .suggestion:focus {{ background: var(--surface-hover); outline: none; }}
.suggestion img, .suggestion .missing-image {{ background: #080b09; border-radius: 6px; flex: 0 0 42px; height: 58px; object-fit: contain; width: 42px; }}
.suggestion strong, .suggestion span {{ display: block; }}
.suggestion span {{ color: var(--muted); font-size: .8rem; margin-top: 3px; }}
.suggestion-actions {{ display: flex; gap: 6px; margin-left: auto; }}
.suggestion-action {{ background: var(--surface-hover); color: var(--text); font-size: .72rem; padding: 7px 9px; white-space: nowrap; }}
.suggestion-action.primary {{ background: var(--accent); color: var(--accent-dark); }}
.workspace {{ margin: 0 auto; max-width: 1100px; }}
.table-heading {{ align-items: end; display: flex; justify-content: space-between; padding: 28px 24px 8px; }}
.table-heading h2 {{ font-size: 1.2rem; letter-spacing: -.02em; margin: 4px 0 0; }}
.table-hint {{ color: var(--muted); font-size: .82rem; }}
.live-total {{ background: var(--surface-raised); border: 1px solid var(--border-strong); border-radius: 10px; display: grid; gap: 2px; min-width: 180px; padding: 10px 13px; text-align: right; }}
.live-total span {{ color: var(--muted); font-size: .68rem; font-weight: 800; letter-spacing: .08em; text-transform: uppercase; }}
.live-total strong {{ color: var(--accent); font-size: 1.25rem; line-height: 1.1; }}
.live-total small {{ color: var(--muted); font-size: .72rem; }}
.card-table, .draft-area {{ counter-reset: card-row; padding: 0 24px; }}
.card-table {{ margin-bottom: 90px; }}
.card-row {{ border: 1px solid var(--border); border-radius: 12px; counter-increment: card-row; margin: 12px 0; overflow: hidden; transition: border-color .16s, transform .16s; }}
.card-row:hover {{ border-color: var(--border-strong); }}
.card-row[data-confirmed="true"] {{ border-color: rgba(121, 217, 139, .55); }}
.card-panel {{ background: var(--surface); }}
.card-panel summary {{ align-items: center; cursor: pointer; display: flex; gap: 15px; justify-content: space-between; list-style: none; padding: 18px 20px; }}
.card-panel summary::-webkit-details-marker {{ display: none; }}
.card-panel summary::before {{ color: var(--muted); content: "＋"; font-size: 1.1rem; width: 18px; }}
.card-panel[open] summary::before {{ content: "−"; }}
.row-name {{ flex: 1; font-size: 1.03rem; font-weight: 750; }}
.row-name::before {{ color: var(--muted); content: counter(card-row, decimal-leading-zero); font-size: .7rem; font-variant-numeric: tabular-nums; margin-right: 12px; vertical-align: 2px; }}
.row-summary-meta {{ align-items: center; display: flex; gap: 14px; }}
.status {{ align-items: center; color: var(--warning); display: inline-flex; font-size: .76rem; font-weight: 700; gap: 6px; }}
.status::before {{ background: currentColor; border-radius: 50%; content: ""; height: 6px; width: 6px; }}
.card-row[data-confirmed="true"] .status {{ color: var(--success); }}
.summary-quantity {{ background: var(--surface-raised); border: 1px solid var(--border); border-radius: 999px; color: var(--muted); font-size: .76rem; padding: 4px 9px; }}
.row-content {{ background: #151a17; border-top: 1px solid var(--border); padding: 18px 20px 20px; }}
.row-controls {{ align-items: center; display: flex; gap: 16px; justify-content: space-between; margin-bottom: 16px; }}
.row-actions {{ align-items: center; display: flex; gap: 8px; }}
.row-controls label {{ color: var(--muted); font-size: .82rem; font-weight: 700; }}
.quantity-input {{ background: var(--surface-raised); border: 1px solid var(--border-strong); border-radius: 7px; color: var(--text); font-size: .95rem; margin-left: 6px; padding: 8px; width: 78px; }}
.quantity-input:focus {{ border-color: var(--accent); outline: none; }}
.options {{ display: grid; gap: 13px; grid-template-columns: repeat(auto-fill, minmax(155px, 1fr)); }}
.option {{ background: var(--surface-raised); border: 2px solid transparent; border-radius: 10px; cursor: pointer; min-width: 0; overflow: hidden; padding-bottom: 9px; text-align: center; transition: border-color .14s, background .14s, transform .14s; }}
.option:hover {{ background: var(--surface-hover); transform: translateY(-2px); }}
.option:has(input:checked) {{ border-color: var(--accent); box-shadow: 0 0 0 1px rgba(184, 243, 107, .15); }}
.option input {{ position: absolute; opacity: 0; }}
.option img, .missing-image {{ background: #080b09; display: block; height: 218px; object-fit: contain; width: 100%; }}
.missing-image {{ align-content: center; color: var(--muted); font-size: .8rem; }}
.option strong, .option span {{ display: block; overflow: hidden; padding: 6px 8px 0; text-overflow: ellipsis; white-space: nowrap; }}
.option span {{ color: var(--muted); font-size: .76rem; }}
.draft-area:not(.has-drafts) {{ display: none; }}
.draft-area {{ margin-bottom: 4px; }}
.draft-heading {{ color: var(--warning); font-size: .76rem; font-weight: 800; letter-spacing: .08em; margin: 20px 0 6px; text-transform: uppercase; }}
.additional-input {{ position: absolute; opacity: 0; }}
.submit {{ align-items: center; background: rgba(23, 28, 25, .92); border: 1px solid var(--border); border-radius: 12px; bottom: 18px; box-shadow: 0 12px 35px rgba(0, 0, 0, .35); display: flex; gap: 16px; padding: 8px; position: fixed; right: max(24px, calc((100vw - 1100px) / 2)); z-index: 4; }}
.save-note {{ color: var(--muted); font-size: .78rem; padding-left: 8px; }}
button {{ background: var(--accent); border: 0; border-radius: 8px; color: var(--accent-dark); cursor: pointer; font-size: .9rem; font-weight: 800; padding: 10px 14px; transition: filter .14s, transform .14s; }}
button:hover {{ filter: brightness(1.08); transform: translateY(-1px); }}
.confirm-row {{ background: var(--border-strong); color: var(--text); }}
.cancel-row {{ background: transparent; color: var(--muted); }}
.card-row[data-cancelled="true"] {{ border-color: var(--border); opacity: .68; }}
@media (max-width: 640px) {{
  body {{ padding-bottom: 96px; }}
  header {{ padding: 20px 16px; }}
  .table-heading {{ align-items: stretch; flex-direction: column; gap: 12px; padding: 22px 16px 7px; }}
  .live-total {{ text-align: left; }}
  .card-table, .draft-area {{ padding: 0 16px; }}
  .card-panel summary {{ gap: 8px; padding: 15px; }}
  .row-summary-meta {{ align-items: end; flex-direction: column; gap: 5px; }}
  .row-name::before {{ margin-right: 7px; }}
  .row-content {{ padding: 15px; }}
  .options {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }}
  .option img, .missing-image {{ height: 180px; }}
  .submit {{ bottom: 12px; left: 16px; right: 16px; }}
  .submit button {{ flex: 1; }}
  .save-note {{ display: none; }}
}}
</style>
</head>
<body>
<form action="/select" method="post">
<header>
<div class="header-inner">
<p class="eyebrow">MTG Autoproxy / Card review</p>
<h1>Build your proxy order</h1>
<p class="hint">Open a row, choose a version, set the quantity, and confirm it.</p>
<label class="search-label" for="proxy-search">Add a card</label>
<div class="search"><input id="proxy-search" type="search" autocomplete="off" placeholder="Search the vendor catalog to add a proxy">
<div id="suggestions" class="suggestions"></div></div>
</div>
</header>
<div id="draft-area" class="draft-area"><p class="draft-heading">Cards waiting for confirmation</p></div>
<div class="workspace">
<div class="table-heading"><div><p class="eyebrow">Order review</p><h2>Card versions</h2></div><div class="live-total" aria-live="polite"><span>Estimated order</span><strong id="estimated-cost">—</strong><small id="estimated-rate">Pricing unavailable</small><small id="card-total">0 cards</small></div></div>
<main id="card-table" class="card-table">{"".join(rows)}</main>
</div>
<div class="submit"><span id="confirmed-count" class="save-note">0 cards confirmed</span><button type="submit">Save selections <span aria-hidden="true">→</span></button></div>
</form>
<script>
const searchInput = document.getElementById('proxy-search');
const suggestions = document.getElementById('suggestions');
const draftArea = document.getElementById('draft-area');
const cardTable = document.getElementById('card-table');
const confirmedCount = document.getElementById('confirmed-count');
const estimatedCost = document.getElementById('estimated-cost');
const estimatedRate = document.getElementById('estimated-rate');
const cardTotal = document.getElementById('card-total');
const priceTiers = {price_tiers_json};
const currencyFormatter = new Intl.NumberFormat('en-US', {{ style: 'currency', currency: 'USD' }});
let searchTimer;

function updateDraftArea() {{
  draftArea.classList.toggle('has-drafts', Boolean(draftArea.querySelector('.draft-row')));
}}

function updateConfirmedCount() {{
  const count = document.querySelectorAll('.card-row[data-confirmed="true"]').length;
  confirmedCount.textContent = count + (count === 1 ? ' card confirmed' : ' cards confirmed');
}}

function updateLiveTotal() {{
  const rows = cardTable.querySelectorAll('.card-row:not([data-cancelled="true"])');
  const totalCards = Array.from(rows).reduce((total, row) => {{
    const input = row.querySelector('.quantity-input');
    const quantity = Number.parseInt(input ? input.value : '0', 10);
    return total + (Number.isFinite(quantity) ? Math.max(0, quantity) : 0);
  }}, 0);
  cardTotal.textContent = totalCards + (totalCards === 1 ? ' card' : ' cards');
  if (!priceTiers.length || !totalCards) {{
    estimatedCost.textContent = '—';
    estimatedRate.textContent = priceTiers.length ? 'Add cards to estimate' : 'Pricing unavailable';
    return;
  }}

  let pricePerCard = priceTiers[0][1];
  for (const [minimumQuantity, tierPrice] of priceTiers) {{
    if (totalCards >= minimumQuantity) pricePerCard = tierPrice;
  }}
  estimatedCost.textContent = currencyFormatter.format(totalCards * pricePerCard);
  estimatedRate.textContent = currencyFormatter.format(pricePerCard) + ' / card';
}}

searchInput.addEventListener('input', () => {{
  clearTimeout(searchTimer);
  const query = searchInput.value.trim();
  if (query.length < 2) {{ suggestions.replaceChildren(); suggestions.style.display = 'none'; return; }}
  searchTimer = setTimeout(() => searchCatalog(query), 250);
}});

async function searchCatalog(query) {{
  const response = await fetch('/search?q=' + encodeURIComponent(query));
  const results = await response.json();
  suggestions.replaceChildren();
  const allSuggestion = document.createElement('div');
  allSuggestion.className = 'suggestion';
  const allText = document.createElement('span');
  const allName = document.createElement('strong');
  allName.textContent = 'All versions for "' + query + '"';
  allText.append(allName);
  const allHint = document.createElement('span');
  allHint.textContent = 'Browse every matching version without a preselection';
  allText.append(allHint);
  allSuggestion.append(allText);
  const allActions = document.createElement('div'); allActions.className = 'suggestion-actions';
  const allVersions = document.createElement('button');
  allVersions.type = 'button'; allVersions.className = 'suggestion-action primary'; allVersions.textContent = 'Browse all versions';
  allVersions.addEventListener('click', () => addAllVersions({{name: query}}, allVersions, query));
  allActions.append(allVersions); allSuggestion.append(allActions); suggestions.append(allSuggestion);
  for (const result of results) {{
    const suggestion = document.createElement('div');
    suggestion.className = 'suggestion';
    if (result.image_path) {{
      const image = document.createElement('img'); image.src = result.image_path; image.alt = result.name; suggestion.append(image);
    }}
    const text = document.createElement('span');
    const name = document.createElement('strong'); name.textContent = result.name; text.append(name);
    const set = document.createElement('span'); set.textContent = result.card_set; text.append(set);
    suggestion.append(text);
    const actions = document.createElement('div'); actions.className = 'suggestion-actions';
    const chooseVersion = document.createElement('button');
    chooseVersion.type = 'button'; chooseVersion.className = 'suggestion-action primary'; chooseVersion.textContent = 'Browse versions';
    chooseVersion.addEventListener('click', () => addAllVersions(result, chooseVersion, query));
    actions.append(chooseVersion); suggestion.append(actions);
    suggestions.append(suggestion);
  }}
  suggestions.style.display = results.length ? 'block' : 'none';
}}

async function addAllVersions(result, button, query) {{
  button.disabled = true; button.textContent = 'Loading...';
  const response = await fetch('/versions?q=' + encodeURIComponent(query));
  const versions = await response.json();
  addSearchCard(result, versions);
}}

function addSearchCard(result, versions) {{
  const draftId = (result.id || 'query') + '-' + Date.now();
  const choices = versions.length ? versions : [result];
  const row = document.createElement('section');
  row.className = 'card-row draft-row'; row.dataset.cardId = '';
  row.innerHTML = `<details class="card-panel" open>
    <summary><span class="row-name"></span><span class="row-summary-meta"><span class="status">Needs confirmation</span><span class="summary-quantity">1 item(s)</span></span></summary>
    <div class="row-content"><div class="row-controls"><label>Quantity <input class="quantity-input" type="number" name="quantity-${{draftId}}" min="1" value="1"></label>
      <input class="additional-input" type="checkbox" name="additional" value="">
      <div class="row-actions"><button type="button" class="cancel-row">Cancel</button><button type="button" class="confirm-row">Confirm card</button></div></div><div class="options"></div></div></details>`;
  row.querySelector('.row-name').textContent = result.name;
  const options = row.querySelector('.options');
  choices.forEach((version) => {{
    const option = document.createElement('label'); option.className = 'option';
    const radio = document.createElement('input'); radio.type = 'radio'; radio.name = 'additional-version-' + draftId;
    radio.value = version.id; radio.dataset.cardId = version.id;
    const image = version.image_path ? document.createElement('img') : document.createElement('div');
    image.className = version.image_path ? '' : 'missing-image';
    if (version.image_path) {{ image.src = version.image_path; image.alt = version.name; }} else {{ image.textContent = 'Image unavailable'; }}
    const name = document.createElement('strong'); name.textContent = version.name;
    const set = document.createElement('span'); set.textContent = version.card_set;
    option.append(radio, image, name, set); options.append(option);
  }});
  draftArea.append(row); suggestions.replaceChildren(); suggestions.style.display = 'none'; searchInput.value = '';
  bindRow(row);
  updateDraftArea();
}}

function bindRow(row) {{
  const details = row.querySelector('details');
  const quantity = row.querySelector('.quantity-input');
  const summaryQuantity = row.querySelector('.summary-quantity');
  const confirmed = row.querySelector('input[name^="confirmed-"]');
  const additional = row.querySelector('.additional-input');
  const cancelButton = row.querySelector('.cancel-row');
  const confirmButton = row.querySelector('.confirm-row');
  const setAdditionalVersion = (input) => {{
    if (!additional || !input) return;
    additional.value = input.dataset.cardId;
    quantity.name = 'quantity-' + input.dataset.cardId;
    row.dataset.cardId = input.dataset.cardId;
  }};
  const markChanged = () => {{
    row.dataset.confirmed = 'false';
    row.querySelector('.status').textContent = 'Needs confirmation';
    if (confirmed) confirmed.value = '0';
    if (additional) additional.checked = false;
    updateConfirmedCount();
  }};
  quantity.addEventListener('input', () => {{
    const value = Math.max(1, parseInt(quantity.value || '1', 10));
    summaryQuantity.textContent = value + ' item(s)';
    if (row.dataset.confirmed === 'true') markChanged();
    updateLiveTotal();
  }});
  row.querySelectorAll('input[type="radio"]').forEach(input => input.addEventListener('change', () => {{
    markChanged(); setAdditionalVersion(input);
  }}));
  cancelButton.addEventListener('click', () => {{
    if (row.classList.contains('draft-row')) {{
      row.remove(); updateDraftArea(); updateLiveTotal(); return;
    }}
    markChanged();
    row.dataset.cancelled = 'true';
    row.querySelector('.status').textContent = 'Cancelled';
    details.open = false;
    updateLiveTotal();
  }});
  confirmButton.addEventListener('click', () => {{
    if (!quantity.reportValidity()) return;
    const selectedVersion = row.querySelector('input[type="radio"]:checked');
    if (!selectedVersion) {{
      row.querySelector('.status').textContent = 'Choose a version first';
      details.open = true;
      return;
    }}
    setAdditionalVersion(selectedVersion);
    row.dataset.confirmed = 'true';
    row.dataset.cancelled = 'false';
    row.querySelector('.status').textContent = 'Confirmed';
    if (confirmed) confirmed.value = '1';
    if (additional) additional.checked = true;
    details.open = false;
    if (row.classList.contains('draft-row')) {{
      row.classList.remove('draft-row'); cardTable.append(row);
      updateDraftArea();
    }}
    updateConfirmedCount();
    updateLiveTotal();
  }});
}}

document.querySelectorAll('.card-row').forEach(bindRow);
updateDraftArea();
updateConfirmedCount();
updateLiveTotal();
</script>
</body>
</html>""", image_ids

    def _create_handler(
        self,
        page,
        image_data,
        image_ids,
        selection_requests,
        selections,
        selection_quantities,
        additional_selections,
        completed,
        session_store=None,
        session_id=None,
    ):
        if session_id:
            page = page.replace(
                'action="/select"',
                f'action="/select?session={session_id}"',
            )
            page = page.replace(
                'href="/finish"',
                f'href="/finish?session={session_id}"',
            )
            page = page.replace(
                'href="/cancel"',
                f'href="/cancel?session={session_id}"',
            )
        image_data_by_id = {
            image_id: image_data[image_page_url]
            for image_page_url, image_id in image_ids.items()
        }
        selector = self
        last_recap = {"selected_rows": [], "skipped_rows": []}

        class SelectionHandler(BaseHTTPRequestHandler):
            def do_GET(self):
                parsed_url = urlparse(self.path)
                query = parse_qs(parsed_url.query)
                if parsed_url.path in {"/select", "/finish", "/cancel"} and not self._session_matches(query):
                    self.send_error(403, "Invalid selection session")
                    return
                if parsed_url.path == "/search":
                    self._search_catalog(query.get("q", [""])[0])
                    return
                if parsed_url.path == "/versions":
                    self._all_versions(
                        query.get("q", query.get("name", [""]))[0]
                    )
                    return
                if parsed_url.path.startswith("/images/"):
                    self._serve_image(parsed_url.path)
                    return
                if parsed_url.path == "/select":
                    self._save_selections(query)
                    return
                if parsed_url.path == "/finish":
                    self._finish_session()
                    return
                if parsed_url.path == "/cancel":
                    self._cancel_session()
                    return
                self._respond_html(page)

            def do_POST(self):
                parsed_url = urlparse(self.path)
                if parsed_url.path != "/select":
                    self.send_error(405, "Method not allowed")
                    return

                try:
                    content_length = int(self.headers.get("Content-Length", "0"))
                except ValueError:
                    self.send_error(400, "Invalid request body")
                    return
                if content_length > MAX_SELECTION_BODY_BYTES:
                    self.send_error(413, "Selection request is too large")
                    return

                body = self.rfile.read(content_length)
                query = parse_qs(parsed_url.query)
                for key, values in parse_qs(
                    body.decode("utf-8", errors="replace")
                ).items():
                    query.setdefault(key, []).extend(values)
                if not self._session_matches(query):
                    self.send_error(403, "Invalid selection session")
                    return
                self._save_selections(query)

            @staticmethod
            def _session_matches(query):
                return not session_id or query.get("session", [""])[0] == session_id

            def _search_catalog(self, query):
                if len(query) > MAX_SEARCH_QUERY_LENGTH:
                    self.send_error(414, "Search query is too long")
                    return
                results = self._prepare_results(
                    search_vendor_catalog_cache(selector.catalog_cache_path, query)
                )
                self._respond_json(results)

            def _all_versions(self, query):
                if len(query) > MAX_SEARCH_QUERY_LENGTH:
                    self.send_error(414, "Search query is too long")
                    return
                results = self._prepare_results(
                    search_vendor_catalog_versions(selector.catalog_cache_path, query)
                )
                self._respond_json(results)

            def _prepare_results(self, results):
                requested_urls = {
                    result["image_url"]
                    for result in results
                    if result["image_url"] and result["image_url"] not in image_ids
                }
                for image_page_url, image in selector._preload_image_urls(requested_urls).items():
                    image_id = len(image_ids)
                    image_ids[image_page_url] = image_id
                    image_data_by_id[image_id] = image

                for result in results:
                    image_id = image_ids.get(result["image_url"])
                    result["image_path"] = (
                        f"/images/{image_id}" if image_id is not None else None
                    )
                return results

            def _serve_image(self, path):
                image_id = path.removeprefix("/images/")
                if not image_id.isdigit() or int(image_id) not in image_data_by_id:
                    self.send_error(404)
                    return
                image, content_type = image_data_by_id[int(image_id)]
                self.send_response(200)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(image)))
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Cache-Control", "private, max-age=3600")
                self.end_headers()
                self.wfile.write(image)

            def _save_selections(self, query):
                selected_rows = []
                skipped_rows = []
                for request_index, (_, original_quantity, _) in enumerate(selection_requests):
                    selections[request_index] = None
                    selection_quantities[request_index] = original_quantity
                additional_selections.clear()
                for request_index, (_, original_quantity, candidates) in enumerate(selection_requests):
                    wanted = selection_requests[request_index][0]
                    confirmed = query.get(f"confirmed-{request_index}", ["0"])[0] == "1"
                    choice = query.get(f"selection-{request_index}", [""])[0]
                    if not confirmed or not choice.isdigit() or int(choice) >= len(candidates):
                        skipped_rows.append(wanted)
                        continue
                    selections[request_index] = int(choice)
                    quantity = self._quantity(
                        query.get(f"quantity-{request_index}", [original_quantity])[0],
                        original_quantity,
                    )
                    selection_quantities[request_index] = quantity
                    selected_rows.append((wanted, candidates[int(choice)], quantity))

                for card_id in query.get("additional", []):
                    if not card_id.isdigit():
                        continue
                    candidate = load_vendor_catalog_card(
                        selector.catalog_cache_path,
                        int(card_id),
                    )
                    if not candidate:
                        continue
                    quantity = self._quantity(query.get(f"quantity-{card_id}", ["1"])[0], 1)
                    additional_selections.append((candidate, quantity))
                    selected_rows.append((candidate.name, candidate, quantity))

                last_recap["selected_rows"] = selected_rows
                last_recap["skipped_rows"] = skipped_rows
                if session_store and session_id:
                    session_store.save(session_id, selected_rows, skipped_rows)
                try:
                    self._respond_html(
                        self._recap_html(selected_rows, skipped_rows)
                    )
                finally:
                    completed.set()

            def _finish_session(self):
                if session_store and session_id:
                    session_store.finish(session_id)
                try:
                    self._respond_html(
                        self._recap_html(
                            last_recap["selected_rows"],
                            last_recap["skipped_rows"],
                            finished=True,
                        )
                    )
                finally:
                    completed.set()

            def _cancel_session(self):
                for request_index in range(len(selections)):
                    selections[request_index] = None
                additional_selections.clear()
                if session_store and session_id:
                    session_store.finish(session_id, status="cancelled")
                try:
                    self._respond_html(
                        """<!doctype html><html lang="en"><head><meta charset="utf-8">
<title>Proxy session cancelled</title><style>
body { background: #101311; color: #f4f7f3; font-family: system-ui, sans-serif; margin: 0; padding: 12vh 24px; text-align: center; }
main { margin: auto; max-width: 620px; } p { color: #9ca99f; }
</style></head><body><main><h1>Proxy session cancelled</h1>
<p>No selections were added to the order.</p></main></body></html>"""
                    )
                finally:
                    completed.set()

            def _recap_html(self, selected_rows, skipped_rows, finished=False):
                total_quantity = sum(quantity for _, _, quantity in selected_rows)
                table_rows = []
                for requested_name, candidate, quantity in selected_rows:
                    image_id = image_ids.get(candidate.image_url)
                    image = image_data_by_id.get(image_id)
                    if image:
                        image_bytes, content_type = image
                        image_markup = (
                            f'<img class="recap-image" src="data:{content_type};base64,'
                            f'{base64.b64encode(image_bytes).decode("ascii")}" '
                            f'alt="{escape(candidate.name)}">'
                        )
                    else:
                        image_markup = '<div class="recap-image missing-image">No image</div>'
                    table_rows.append(
                        f'<tr><td>{image_markup}</td>'
                        f'<td><strong>{escape(requested_name)}</strong></td>'
                        f'<td>{escape(candidate.name)}</td>'
                        f'<td>{escape(candidate.card_set)}</td>'
                        f'<td class="quantity">{quantity}</td></tr>'
                    )

                skipped_markup = ""
                if skipped_rows:
                    skipped_markup = (
                        '<aside class="skipped"><strong>Not included:</strong> '
                        f'{escape(", ".join(skipped_rows))}</aside>'
                    )
                if finished:
                    action_markup = (
                        '<span>This session is finalized and the order generator will now finish.</span>'
                    )
                else:
                    action_markup = (
                        '<button type="button" onclick="history.back()">Continue editing</button>'
                        '<a class="finish" href="/finish">Finish order</a>'
                        '<a class="cancel" href="/cancel">Cancel session</a>'
                    )
                return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Proxy order recap</title>
<style>
:root {{ --background: #101311; --surface: #171c19; --surface-raised: #202722; --border: #344039; --text: #f4f7f3; --muted: #9ca99f; --accent: #b8f36b; --success: #79d98b; }}
* {{ box-sizing: border-box; }}
body {{ background: radial-gradient(circle at 50% -20%, #26352a 0, var(--background) 43rem); color: var(--text); font-family: Inter, ui-sans-serif, system-ui, sans-serif; margin: 0; min-width: 320px; padding: 48px 24px; }}
.recap {{ margin: 0 auto; max-width: 1100px; }}
.eyebrow {{ color: var(--accent); font-size: .7rem; font-weight: 800; letter-spacing: .14em; text-transform: uppercase; }}
h1 {{ font-size: clamp(1.8rem, 4vw, 2.8rem); letter-spacing: -.045em; line-height: 1.05; margin: 10px 0; }}
.lead {{ color: var(--muted); margin: 0 0 26px; }}
.summary-card {{ background: rgba(23, 28, 25, .92); border: 1px solid var(--border); border-radius: 14px; overflow: hidden; }}
.summary-bar {{ align-items: center; background: var(--surface-raised); border-bottom: 1px solid var(--border); display: flex; gap: 12px; justify-content: space-between; padding: 18px 20px; }}
.summary-bar strong {{ color: var(--success); font-size: 1.2rem; }}
.summary-bar span {{ color: var(--muted); font-size: .82rem; }}
.table-wrap {{ overflow-x: auto; }}
table {{ border-collapse: collapse; min-width: 760px; width: 100%; }}
th {{ color: var(--muted); font-size: .7rem; letter-spacing: .08em; text-align: left; text-transform: uppercase; }}
th, td {{ border-bottom: 1px solid var(--border); padding: 12px 16px; vertical-align: middle; }}
tbody tr:last-child td {{ border-bottom: 0; }}
td {{ color: var(--muted); font-size: .86rem; }}
td strong {{ color: var(--text); display: block; margin-bottom: 3px; }}
.recap-image {{ background: #080b09; border-radius: 6px; display: block; height: 70px; object-fit: contain; width: 50px; }}
.missing-image {{ align-content: center; font-size: .65rem; text-align: center; }}
.quantity {{ color: var(--text); font-size: 1rem; font-weight: 800; text-align: right; }}
.skipped {{ background: #2d2412; border-top: 1px solid #70552a; color: #f0c66b; font-size: .82rem; padding: 13px 16px; }}
.actions {{ align-items: center; display: flex; gap: 10px; justify-content: flex-end; padding: 18px 20px; }}
.actions span {{ color: var(--muted); font-size: .82rem; }}
button {{ background: var(--accent); border: 0; border-radius: 8px; color: #243619; cursor: pointer; font-size: .9rem; font-weight: 800; padding: 10px 14px; }}
.finish {{ background: var(--accent); border-radius: 8px; color: #243619; font-size: .9rem; font-weight: 800; padding: 10px 14px; text-decoration: none; }}
.cancel {{ color: var(--muted); font-size: .82rem; padding: 10px 8px; text-decoration: none; }}
@media print {{ body {{ background: #fff; color: #111; padding: 0; }} .summary-card {{ border-color: #ccc; }} .actions {{ display: none; }} }}
@media (max-width: 640px) {{ body {{ padding: 28px 14px; }} .summary-bar, .actions {{ align-items: stretch; flex-direction: column; }} .cancel {{ text-align: center; }} }}
</style>
</head>
<body>
<main class="recap">
<p class="eyebrow">MTG Autoproxy / Selection complete</p>
<h1>Proxy order recap</h1>
<p class="lead">Review the versions and quantities selected for this order.</p>
<section class="summary-card">
<div class="summary-bar"><span>{len(selected_rows)} selected line(s)</span><strong>{total_quantity} total card(s)</strong></div>
<div class="table-wrap"><table><thead><tr><th>Preview</th><th>Requested</th><th>Selected version</th><th>Set</th><th>Qty</th></tr></thead><tbody>{"".join(table_rows)}</tbody></table></div>
{skipped_markup}
<div class="actions">{action_markup}<button type="button" onclick="window.print()">Print recap</button></div>
</section>
</main>
</body>
</html>"""

            @staticmethod
            def _quantity(value, fallback):
                try:
                    return max(1, int(value))
                except (TypeError, ValueError):
                    return fallback

            def _respond_html(self, body):
                encoded_body = body.encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(encoded_body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Referrer-Policy", "no-referrer")
                self.end_headers()
                self.wfile.write(encoded_body)

            def _respond_json(self, body):
                encoded_body = json.dumps(body).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(encoded_body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Referrer-Policy", "no-referrer")
                self.end_headers()
                self.wfile.write(encoded_body)

            def log_message(self, format, *args):
                return

        return SelectionHandler
