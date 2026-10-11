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
from urllib.parse import parse_qs, urljoin, urlparse, urlunparse

import requests

from utils import (
    load_vendor_catalog_card,
    load_vendor_catalog_image_urls,
    request_remote,
    search_vendor_catalog_cache,
    search_vendor_catalog_versions,
)

logger = logging.getLogger(__name__)


MAX_SELECTION_BODY_BYTES = 256 * 1024
MAX_CLIENT_LOG_BYTES = 4096
MAX_MARKET_BODY_BYTES = 64 * 1024
MAX_SEARCH_QUERY_LENGTH = 200


def google_full_image_url(image_url):
    parsed_url = urlparse(image_url)
    hostname = parsed_url.hostname or ""
    if hostname != "googleusercontent.com" and not hostname.endswith(".googleusercontent.com"):
        return image_url
    path_prefix, separator, size_parameters = parsed_url.path.rpartition("=")
    if not separator or not size_parameters.startswith(("w", "h", "s")):
        return image_url
    return urlunparse(parsed_url._replace(path=f"{path_prefix}=s0"))


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
            return (
                cached_image[0],
                cached_image[1],
                urljoin(source_url, record["resolved_url"]),
                True,
            )

        source_headers = self._conditional_headers(record, "source")
        source_response = request_remote(
            source_url,
            headers=source_headers,
            timeout=30,
        )
        if source_response.status_code == 304 and cached_image:
            source_response.close()
            self._touch(source_url)
            return (
                cached_image[0],
                cached_image[1],
                urljoin(source_url, record["resolved_url"]),
                True,
            )
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

        resolved_image_url = urljoin(source_url, parser.image_url)
        image_response = request_remote(
            resolved_image_url,
            headers=self._conditional_headers(record, "image"),
            stream=True,
            timeout=30,
        )
        if image_response.status_code == 304 and cached_image:
            image_response.close()
            self._touch(source_url)
            return cached_image[0], cached_image[1], resolved_image_url, True
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
            resolved_image_url,
            content_type,
            image_bytes,
            source_response_headers,
            image_headers,
        )
        return image_bytes, content_type, resolved_image_url, False

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


class OpportunisticThumbnailPrefetcher:
    def __init__(self, image_urls, loader, worker_count=2, idle_seconds=2.0):
        self._image_urls = list(image_urls)
        self._total_count = len(self._image_urls)
        self._loader = loader
        self._worker_count = max(1, int(worker_count))
        self._idle_seconds = max(0.1, float(idle_seconds))
        self._condition = threading.Condition()
        self._last_activity = time.monotonic()
        self._stop_requested = False
        self._workers = []
        self._completed_count = 0
        self._failure_count = 0

    def start(self):
        if not self._total_count:
            return

        logger.info(
            "Queued %d catalog thumbnails for idle preloading (%d workers).",
            self._total_count,
            self._worker_count,
        )
        for worker_index in range(min(self._worker_count, len(self._image_urls))):
            worker = threading.Thread(
                target=self._run,
                name=f"thumbnail-prefetch-{worker_index + 1}",
                daemon=True,
            )
            self._workers.append(worker)
            worker.start()

    def note_activity(self):
        with self._condition:
            self._last_activity = time.monotonic()
            self._condition.notify_all()

    def stop(self):
        with self._condition:
            self._stop_requested = True
            self._condition.notify_all()
        for worker in self._workers:
            worker.join(timeout=0.25)

        if self._total_count:
            logger.info(
                "Idle thumbnail preloading stopped: %d completed, %d failed, %d remaining.",
                self._completed_count,
                self._failure_count,
                max(0, self._total_count - self._completed_count - self._failure_count),
            )

    def _run(self):
        while True:
            image_page_url = self._next_url_when_idle()
            if image_page_url is None:
                return

            try:
                self._loader(image_page_url)
            except (OSError, requests.RequestException, ValueError, sqlite3.Error) as error:
                with self._condition:
                    self._failure_count += 1
                logger.debug(
                    "Idle thumbnail preload failed for %s: %s",
                    image_page_url,
                    error,
                )
            else:
                with self._condition:
                    self._completed_count += 1
                    completed_count = self._completed_count
                if completed_count == 1 or completed_count % 25 == 0:
                    logger.info(
                        "Idle thumbnail preloading: %d/%d complete.",
                        completed_count,
                        self._total_count,
                    )

    def _next_url_when_idle(self):
        with self._condition:
            while self._image_urls and not self._stop_requested:
                idle_for = time.monotonic() - self._last_activity
                if idle_for >= self._idle_seconds:
                    return self._image_urls.pop()
                self._condition.wait(self._idle_seconds - idle_for)
            return None


class CardSelector:
    def __init__(
        self,
        catalog_cache_path,
        thumbnail_cache_directory="output/thumbnail_cache",
        thumbnail_cache_max_age_days=30,
        thumbnail_cache_max_size_mb=512,
        session_database_path="output/selection_sessions.sqlite3",
        price_tiers=None,
        market_provider=None,
        thumbnail_prefetch_enabled=True,
        thumbnail_prefetch_workers=2,
        thumbnail_prefetch_idle_seconds=2.0,
    ):
        self.catalog_cache_path = catalog_cache_path
        self.price_tiers = list(price_tiers or [])
        self.market_provider = market_provider
        self.thumbnail_cache = ThumbnailCache(
            thumbnail_cache_directory,
            max_age_days=thumbnail_cache_max_age_days,
            max_size_mb=thumbnail_cache_max_size_mb,
        )
        self.session_store = SelectionSessionStore(session_database_path)
        self.thumbnail_prefetch_enabled = thumbnail_prefetch_enabled
        self.thumbnail_prefetch_workers = thumbnail_prefetch_workers
        self.thumbnail_prefetch_idle_seconds = thumbnail_prefetch_idle_seconds

    def _vendor_total_for_rows(self, selected_rows):
        total_cards = sum(int(quantity) for _, _, quantity in selected_rows)
        if not total_cards or not self.price_tiers:
            return None
        applicable_tiers = [
            price
            for minimum_quantity, price in self.price_tiers
            if total_cards >= minimum_quantity
        ]
        return total_cards * applicable_tiers[-1] if applicable_tiers else None

    def select_all(self, selection_requests):
        image_data = self._preload_images(selection_requests)
        selections = [None] * len(selection_requests)
        selection_quantities = [quantity for _, quantity, _ in selection_requests]
        additional_selections = []
        completed = threading.Event()
        session_id = self.session_store.create()
        page, image_ids = self._build_page(selection_requests, image_data)
        prefetcher = self._create_thumbnail_prefetcher(image_data)
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
            prefetcher,
        )
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        prefetcher.start()
        url = f"http://127.0.0.1:{server.server_port}/?session={session_id}"

        try:
            if not webbrowser.open(url):
                raise RuntimeError(f"Could not open the card picker at {url}")
            completed.wait()
        finally:
            prefetcher.stop()
            server.shutdown()
            server.server_close()
            server_thread.join()

        selected_candidates = [
            candidates[index] if index is not None else None
            for (_, _, candidates), index in zip(selection_requests, selections)
        ]
        return selected_candidates, selection_quantities, additional_selections

    def _create_thumbnail_prefetcher(self, image_data):
        if not self.thumbnail_prefetch_enabled:
            return OpportunisticThumbnailPrefetcher([], self._load_image)

        image_urls = set(load_vendor_catalog_image_urls(self.catalog_cache_path))
        image_urls.difference_update(image_data)
        return OpportunisticThumbnailPrefetcher(
            image_urls,
            self._load_image,
            worker_count=self.thumbnail_prefetch_workers,
            idle_seconds=self.thumbnail_prefetch_idle_seconds,
        )

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
                    image, content_type, resolved_image_url, cache_hit = future.result()
                    image_data[image_page_url] = image, content_type, resolved_image_url
                    cache_hits += int(cache_hit)
                except (OSError, requests.RequestException, ValueError, sqlite3.Error) as error:
                    logger.warning(
                        "Could not load full embedded image from %s; the thumbnail will be unavailable: %s",
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
                image_payload = image_data.get(candidate.image_url)
                if candidate.image_url and image_payload is None:
                    logger.warning(
                        "Full image unavailable for card '%s' from %s; showing a placeholder.",
                        candidate.name,
                        candidate.image_url,
                    )
                expanded_image_url = (
                    google_full_image_url(image_payload[2])
                    if image_payload and len(image_payload) > 2
                    else f"/images/{image_id}"
                )
                image = (
                    f'<img class="option-thumbnail" src="/images/{image_id}" alt="{escape(candidate.name)}" data-full-image="{escape(expanded_image_url)}" data-local-image="/images/{image_id}" tabindex="0" role="button" aria-label="Expand {escape(candidate.name)}" loading="eager" decoding="async">'
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
.search-label {{ color: var(--muted); display: block; font-size: .75rem; font-weight: 750; letter-spacing: .08em; margin: 0 0 7px 24px; text-transform: uppercase; }}
.search {{ margin-left: 24px; max-width: 760px; position: relative; }}
.search input {{ background: rgba(32, 39, 34, .9); border: 1px solid var(--border-strong); border-radius: 10px; box-shadow: 0 8px 26px rgba(0, 0, 0, .18); color: var(--text); font-size: 1rem; outline: none; padding: 13px 15px; transition: border-color .16s, box-shadow .16s; width: 100%; }}
.search input:focus {{ border-color: var(--accent); box-shadow: 0 0 0 3px rgba(184, 243, 107, .15); }}
.suggestions {{ background: #1e251f; border: 1px solid var(--border-strong); border-radius: 10px; box-shadow: 0 18px 34px rgba(0, 0, 0, .35); display: none; left: 0; max-height: 330px; overflow-y: auto; position: absolute; right: 0; top: 49px; z-index: 3; }}
.suggestion {{ align-items: center; background: transparent; border: 0; border-bottom: 1px solid var(--border); color: var(--text); cursor: pointer; display: flex; gap: 12px; padding: 9px; text-align: left; transition: background .12s; width: 100%; }}
.suggestion.is-loading {{ cursor: wait; opacity: .6; }}
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
.live-totals {{ display: flex; flex-wrap: wrap; gap: 10px; justify-content: flex-end; }}
.live-total {{ background: var(--surface-raised); border: 1px solid var(--border-strong); border-radius: 10px; display: grid; gap: 2px; min-width: 180px; padding: 10px 13px; text-align: right; }}
.live-total span {{ color: var(--muted); font-size: .68rem; font-weight: 800; letter-spacing: .08em; text-transform: uppercase; }}
.live-total strong {{ color: var(--accent); font-size: 1.25rem; line-height: 1.1; }}
.live-total small {{ color: var(--muted); font-size: .72rem; }}
.thumbnail-loader {{ align-items: center; background: rgba(32, 39, 34, .96); border: 1px solid var(--border-strong); border-radius: 10px; box-shadow: 0 10px 28px rgba(0, 0, 0, .24); display: flex; gap: 11px; margin: 10px 24px 0; padding: 10px 13px; }}
.thumbnail-loader[hidden] {{ display: none; }}
.thumbnail-loader strong, .thumbnail-loader span {{ display: block; }}
.thumbnail-loader strong {{ font-size: .82rem; }}
.thumbnail-loader span {{ color: var(--muted); font-size: .74rem; margin-top: 2px; }}
.loader-spinner {{ animation: spin .8s linear infinite; border: 2px solid var(--border-strong); border-radius: 50%; border-top-color: var(--accent); flex: 0 0 16px; height: 16px; width: 16px; }}
@keyframes spin {{ to {{ transform: rotate(360deg); }} }}
.option-thumbnail {{ cursor: zoom-in; }}
.image-modal[hidden] {{ display: none; }}
.image-modal {{ background: rgba(4, 7, 5, .88); display: grid; inset: 0; padding: 24px; place-items: center; position: fixed; z-index: 10; }}
.image-modal-dialog {{ margin: auto; max-height: calc(100vh - 48px); position: relative; width: min(620px, 92vw); }}
.image-modal-dialog img {{ border: 1px solid var(--border-strong); border-radius: 12px; box-shadow: 0 20px 60px rgba(0, 0, 0, .55); display: block; height: auto; margin: 0 auto; max-height: calc(100vh - 100px); max-width: 100%; object-fit: contain; }}
.image-modal-close {{ background: var(--surface-raised); border: 1px solid var(--border-strong); color: var(--text); position: absolute; right: 10px; top: 10px; z-index: 1; }}
.image-modal-caption {{ background: rgba(16, 19, 17, .86); border-radius: 7px; bottom: 10px; color: var(--text); font-size: .8rem; left: 10px; margin: 0; max-width: calc(100% - 20px); overflow: hidden; padding: 5px 8px; position: absolute; text-overflow: ellipsis; white-space: nowrap; }}
body.modal-open {{ overflow: hidden; }}
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
.options {{ display: grid; gap: 13px; grid-template-columns: repeat(4, minmax(0, 1fr)); }}
.option {{ background: var(--surface-raised); border: 2px solid transparent; border-radius: 10px; cursor: pointer; min-width: 0; overflow: hidden; padding-bottom: 9px; text-align: center; transition: border-color .14s, background .14s, transform .14s; }}
.option:hover {{ background: var(--surface-hover); transform: translateY(-2px); }}
.option:has(input:checked) {{ border-color: var(--accent); box-shadow: 0 0 0 1px rgba(184, 243, 107, .15); }}
.option input {{ position: absolute; opacity: 0; }}
.option img, .missing-image {{ background: #080b09; display: block; height: 218px; object-fit: contain; width: 100%; }}
.missing-image {{ align-content: center; color: var(--muted); font-size: .8rem; }}
.option strong, .option span {{ display: block; overflow: hidden; padding: 6px 8px 0; text-overflow: ellipsis; white-space: nowrap; }}
.option span {{ color: var(--muted); font-size: .76rem; }}
.draft-area:not(.has-drafts) {{ display: none; }}
.draft-area {{ margin: 0 auto 4px; max-width: 1100px; }}
.draft-heading {{ color: var(--warning); font-size: .76rem; font-weight: 800; letter-spacing: .08em; margin: 20px 0 6px; text-transform: uppercase; }}
.additional-input {{ position: absolute; opacity: 0; }}
.submit {{ align-items: center; background: rgba(23, 28, 25, .92); border: 1px solid var(--border); border-radius: 12px; box-shadow: 0 12px 35px rgba(0, 0, 0, .35); display: flex; gap: 16px; justify-content: flex-end; margin: 30px auto 0; max-width: 1100px; padding: 8px 24px; }}
.save-note {{ color: var(--muted); font-size: .78rem; padding-left: 8px; }}
button {{ background: var(--accent); border: 0; border-radius: 8px; color: var(--accent-dark); cursor: pointer; font-size: .9rem; font-weight: 800; padding: 10px 14px; transition: filter .14s, transform .14s; }}
button:hover {{ filter: brightness(1.08); transform: translateY(-1px); }}
.confirm-row {{ background: var(--border-strong); color: var(--text); }}
.cancel-row {{ background: transparent; color: var(--muted); }}
.card-row[data-cancelled="true"] {{ border-color: var(--border); opacity: .68; }}
@media (max-width: 640px) {{
  body {{ padding-bottom: 96px; }}
  header {{ padding: 20px 16px; }}
  .search-label, .search {{ margin-left: 0; }}
  .table-heading {{ align-items: stretch; flex-direction: column; gap: 12px; padding: 22px 16px 7px; }}
  .live-totals {{ flex-direction: column; }}
  .live-total {{ text-align: left; }}
  .thumbnail-loader {{ margin-left: 16px; margin-right: 16px; }}
  .card-table, .draft-area {{ padding: 0 16px; }}
  .card-panel summary {{ gap: 8px; padding: 15px; }}
  .row-summary-meta {{ align-items: end; flex-direction: column; gap: 5px; }}
  .row-name::before {{ margin-right: 7px; }}
  .row-content {{ padding: 15px; }}
  .options {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }}
  .option img, .missing-image {{ height: 180px; }}
  .submit {{ margin: 24px 16px 0; padding: 8px; }}
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
<div id="thumbnail-loader" class="thumbnail-loader" role="status" aria-live="polite"><span class="loader-spinner" aria-hidden="true"></span><div><strong>Loading thumbnails</strong><span id="thumbnail-progress">0 of 0 ready</span></div></div>
<div id="draft-area" class="draft-area"><p class="draft-heading">Cards waiting for confirmation</p></div>
<div class="workspace">
<div class="table-heading"><div><p class="eyebrow">Order review</p><h2>Card versions</h2></div><div class="live-totals"><div class="live-total" aria-live="polite"><span>Estimated order</span><strong id="estimated-cost">—</strong><small id="estimated-rate">Pricing unavailable</small><small id="card-total">0 cards</small></div><div class="live-total" aria-live="polite"><span>Estimated market cost</span><strong id="market-cost">—</strong><small id="market-rate">Confirm cards to estimate</small></div></div></div>
<main id="card-table" class="card-table">{"".join(rows)}</main>
</div>
<div class="submit"><span id="confirmed-count" class="save-note">0 cards confirmed</span><button type="submit">Save selections <span aria-hidden="true">→</span></button></div>
</form>
<div id="image-modal" class="image-modal" hidden role="dialog" aria-modal="true" aria-labelledby="image-modal-caption"><div class="image-modal-dialog"><button id="image-modal-close" class="image-modal-close" type="button" aria-label="Close image preview">Close</button><img id="expanded-image" alt=""><p id="image-modal-caption" class="image-modal-caption"></p></div></div>
<script>
const searchInput = document.getElementById('proxy-search');
const suggestions = document.getElementById('suggestions');
const draftArea = document.getElementById('draft-area');
const cardTable = document.getElementById('card-table');
const confirmedCount = document.getElementById('confirmed-count');
const estimatedCost = document.getElementById('estimated-cost');
const estimatedRate = document.getElementById('estimated-rate');
const cardTotal = document.getElementById('card-total');
const marketCost = document.getElementById('market-cost');
const marketRate = document.getElementById('market-rate');
const thumbnailLoader = document.getElementById('thumbnail-loader');
const thumbnailProgress = document.getElementById('thumbnail-progress');
const imageModal = document.getElementById('image-modal');
const imageModalClose = document.getElementById('image-modal-close');
const expandedImage = document.getElementById('expanded-image');
const imageModalCaption = document.getElementById('image-modal-caption');
const priceTiers = {price_tiers_json};
const currencyFormatter = new Intl.NumberFormat('en-US', {{ style: 'currency', currency: 'USD' }});
let searchTimer;
let readyThumbnailCount = 0;
let searchRequestId = 0;
let imageModalReturnFocus = null;
let imageModalRequestId = 0;
let imageModalLoadTimer = null;
let marketRefreshTimer;
let marketRequestId = 0;

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
    scheduleMarketTotal();
    return;
  }}

  let pricePerCard = priceTiers[0][1];
  for (const [minimumQuantity, tierPrice] of priceTiers) {{
    if (totalCards >= minimumQuantity) pricePerCard = tierPrice;
  }}
  estimatedCost.textContent = currencyFormatter.format(totalCards * pricePerCard);
  estimatedRate.textContent = currencyFormatter.format(pricePerCard) + ' / card';
  scheduleMarketTotal();
}}

function selectedMarketRows() {{
  return Array.from(cardTable.querySelectorAll('.card-row[data-confirmed="true"]'))
    .filter((row) => row.dataset.cancelled !== 'true')
    .map((row) => {{
      const selected = row.querySelector('input[type="radio"]:checked');
      const option = selected ? selected.closest('.option') : null;
      const quantityInput = row.querySelector('.quantity-input');
      const quantity = Number.parseInt(quantityInput ? quantityInput.value : '0', 10);
      if (!option || !Number.isFinite(quantity) || quantity < 1) return null;
      const name = option.querySelector('strong');
      const cardSet = option.querySelector('span');
      return {{ name: name ? name.textContent : '', set: cardSet ? cardSet.textContent : '', quantity }};
    }})
    .filter((row) => row && row.name && row.set);
}}

function scheduleMarketTotal() {{
  clearTimeout(marketRefreshTimer);
  marketRefreshTimer = setTimeout(refreshMarketTotal, 300);
}}

async function refreshMarketTotal() {{
  const rows = selectedMarketRows();
  if (!rows.length) {{
    marketCost.textContent = '—';
    marketRate.textContent = 'Confirm cards to estimate';
    return;
  }}
  const requestId = ++marketRequestId;
  const session = new URLSearchParams(window.location.search).get('session');
  const marketUrl = session ? '/market?session=' + encodeURIComponent(session) : '/market';
  marketCost.textContent = 'Loading…';
  marketRate.textContent = 'Fetching cached market prices';
  try {{
    const response = await fetch(marketUrl, {{
      method: 'POST',
      headers: {{ 'Content-Type': 'application/json' }},
      body: JSON.stringify({{ rows }}),
    }});
    if (!response.ok) throw new Error('Market lookup failed with HTTP ' + response.status);
    const result = await response.json();
    if (requestId !== marketRequestId) return;
    if (!result.available) {{
      marketCost.textContent = 'Unavailable';
      marketRate.textContent = result.message || 'No market price found';
      return;
    }}
    marketCost.textContent = new Intl.NumberFormat('en-US', {{ style: 'currency', currency: result.currency }}).format(result.market_total);
    marketRate.textContent = result.unavailable_count
      ? result.matched_quantity + '/' + result.total_quantity + ' cards priced'
      : result.source;
  }} catch (error) {{
    if (requestId !== marketRequestId) return;
    marketCost.textContent = 'Unavailable';
    marketRate.textContent = 'Market lookup failed';
    console.warn('[mtg-autoproxy] Market price lookup failed.', error);
  }}
}}

function updateThumbnailProgress() {{
  const thumbnails = document.querySelectorAll('.option-thumbnail');
  thumbnailProgress.textContent = readyThumbnailCount + ' of ' + thumbnails.length + ' ready';
  if (readyThumbnailCount >= thumbnails.length) thumbnailLoader.hidden = true;
}}

function markThumbnailReady(image) {{
  if (image.dataset.ready === 'true') return;
  image.dataset.ready = 'true';
  readyThumbnailCount += 1;
  updateThumbnailProgress();
}}

function watchThumbnails() {{
  const thumbnails = document.querySelectorAll('.option-thumbnail');
  if (!thumbnails.length) {{ thumbnailLoader.hidden = true; return; }}
  thumbnails.forEach((image) => {{
    image.addEventListener('load', () => markThumbnailReady(image), {{ once: true }});
    image.addEventListener('error', () => markThumbnailReady(image), {{ once: true }});
    if (image.complete) markThumbnailReady(image);
  }});
  updateThumbnailProgress();
}}

function closeImageModal() {{
  imageModalRequestId += 1;
  clearTimeout(imageModalLoadTimer);
  imageModalLoadTimer = null;
  imageModal.hidden = true;
  document.body.classList.remove('modal-open');
  expandedImage.onerror = null;
  expandedImage.removeAttribute('src');
  expandedImage.removeAttribute('data-fallback');
  if (imageModalReturnFocus) imageModalReturnFocus.focus();
}}

function reportClientWarning(message, source) {{
  const session = new URLSearchParams(window.location.search).get('session');
  if (!session) return;
  fetch('/client-log?session=' + encodeURIComponent(session), {{
    method: 'POST',
    headers: {{ 'Content-Type': 'application/json' }},
    body: JSON.stringify({{ event: 'full-image-load-failed', message, source }}),
    keepalive: true,
  }}).catch(() => {{}});
}}

function preloadFullImages(root) {{
  root.querySelectorAll('.option-thumbnail').forEach((thumbnail) => {{
    const source = thumbnail.dataset.fullImage;
    if (!source || thumbnail.dataset.fullPreloadStarted === 'true') return;
    thumbnail.dataset.fullPreloadStarted = 'true';
    const fullImage = new Image();
    fullImage.onload = () => {{ thumbnail.dataset.fullPreloadState = 'ready'; }};
    fullImage.onerror = () => {{
      thumbnail.dataset.fullPreloadState = 'failed';
      const warning = 'Could not preload full embedded image; the cached thumbnail will be used.';
      console.warn('[mtg-autoproxy] ' + warning, source);
      reportClientWarning(warning, source);
    }};
    fullImage.src = source;
  }});
}}

function openImageModal(image) {{
  const source = image.dataset.fullImage || image.currentSrc || image.src;
  if (!source) return;
  const requestId = ++imageModalRequestId;
  imageModalReturnFocus = image;
  const fallback = image.dataset.localImage || image.currentSrc || image.src;
  expandedImage.dataset.fallback = fallback;
  expandedImage.onerror = () => {{
    if (expandedImage.dataset.fallback && expandedImage.src !== expandedImage.dataset.fallback) {{
      expandedImage.onerror = null;
      expandedImage.src = expandedImage.dataset.fallback;
    }}
  }};
  expandedImage.src = fallback;
  expandedImage.alt = image.alt;
  imageModalCaption.textContent = image.alt;
  imageModal.hidden = false;
  document.body.classList.add('modal-open');
  imageModalClose.focus();
  if (source !== fallback) {{
    const remoteImage = new Image();
    remoteImage.onload = () => {{
      clearTimeout(imageModalLoadTimer);
      imageModalLoadTimer = null;
      if (requestId === imageModalRequestId && !imageModal.hidden) expandedImage.src = source;
    }};
    remoteImage.onerror = () => {{
      clearTimeout(imageModalLoadTimer);
      imageModalLoadTimer = null;
      const warning = 'Could not load full embedded image; using the cached image instead.';
      console.warn('[mtg-autoproxy] ' + warning, source);
      reportClientWarning(warning, source);
    }};
    imageModalLoadTimer = setTimeout(() => {{
      if (requestId !== imageModalRequestId || imageModal.hidden) return;
      const warning = 'Timed out loading full embedded image; using the cached image instead.';
      console.warn('[mtg-autoproxy] ' + warning, source);
      reportClientWarning(warning, source);
    }}, 10000);
    remoteImage.src = source;
  }}
}}

function bindThumbnailPreview(root) {{
  root.querySelectorAll('.option-thumbnail').forEach((image) => {{
    if (image.dataset.previewBound === 'true') return;
    image.dataset.previewBound = 'true';
    image.addEventListener('click', (event) => {{
      event.preventDefault();
      event.stopPropagation();
      openImageModal(image);
    }});
    image.addEventListener('keydown', (event) => {{
      if (event.key !== 'Enter' && event.key !== ' ') return;
      event.preventDefault();
      event.stopPropagation();
      openImageModal(image);
    }});
  }});
}}

imageModalClose.addEventListener('click', closeImageModal);
imageModal.addEventListener('click', (event) => {{
  if (event.target === imageModal) closeImageModal();
}});
document.addEventListener('keydown', (event) => {{
  if (event.key === 'Escape' && !imageModal.hidden) closeImageModal();
}});

searchInput.addEventListener('input', () => {{
  clearTimeout(searchTimer);
  const query = searchInput.value.trim();
  if (query.length < 2) {{ suggestions.replaceChildren(); suggestions.dataset.query = ''; suggestions.style.display = 'none'; return; }}
  searchTimer = setTimeout(() => searchCatalog(query), 250);
}});

searchInput.addEventListener('keydown', (event) => {{
  if (event.key !== 'Enter') return;
  event.preventDefault();
  clearTimeout(searchTimer);
  const query = searchInput.value.trim();
  if (query.length < 2) return;
  const allVersionsButton = suggestions.dataset.query === query
    ? suggestions.querySelector('.suggestion:first-child .suggestion-action.primary')
    : null;
  if (allVersionsButton) {{ allVersionsButton.click(); return; }}
  searchCatalog(query).then(() => {{
    const refreshedButton = suggestions.dataset.query === query
      ? suggestions.querySelector('.suggestion:first-child .suggestion-action.primary')
      : null;
    if (refreshedButton) refreshedButton.click();
  }});
}});

async function searchCatalog(query) {{
  const requestId = ++searchRequestId;
  const response = await fetch('/search?q=' + encodeURIComponent(query));
  const results = await response.json();
  if (requestId !== searchRequestId || searchInput.value.trim() !== query) return;
  suggestions.dataset.query = query;
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
    suggestion.tabIndex = 0;
    suggestion.setAttribute('role', 'button');
    suggestion.setAttribute('aria-label', 'Choose versions for ' + result.name);
    suggestion.addEventListener('click', () => addAllVersions(result, suggestion, query));
    suggestion.addEventListener('keydown', (event) => {{
      if (event.key !== 'Enter' && event.key !== ' ') return;
      event.preventDefault();
      addAllVersions(result, suggestion, query);
    }});
    suggestions.append(suggestion);
  }}
  suggestions.style.display = results.length ? 'block' : 'none';
}}

async function addAllVersions(result, trigger, query) {{
  if (trigger.getAttribute('aria-busy') === 'true') return;
  trigger.setAttribute('aria-busy', 'true');
  if (trigger.tagName === 'BUTTON') {{
    trigger.disabled = true;
    trigger.textContent = 'Loading...';
  }} else {{
    trigger.classList.add('is-loading');
  }}
  try {{
    const response = await fetch('/versions?q=' + encodeURIComponent(query));
    if (!response.ok) throw new Error('Version lookup failed with HTTP ' + response.status);
    const versions = await response.json();
    addSearchCard(result, versions);
  }} catch (error) {{
    console.error('[mtg-autoproxy] Could not load card versions.', error);
    trigger.removeAttribute('aria-busy');
    trigger.classList.remove('is-loading');
    if (trigger.tagName === 'BUTTON') trigger.disabled = false;
  }}
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
    image.className = version.image_path ? 'option-thumbnail' : 'missing-image';
    if (version.image_path) {{
      image.src = version.image_path;
      image.alt = version.name;
      image.dataset.fullImage = version.image_full_url || version.image_path;
      image.dataset.localImage = version.image_path;
      image.tabIndex = 0;
      image.setAttribute('role', 'button');
      image.setAttribute('aria-label', 'Expand ' + version.name);
      image.loading = 'eager';
      image.decoding = 'async';
    }} else {{ image.textContent = 'Image unavailable'; }}
    const name = document.createElement('strong'); name.textContent = version.name;
    const set = document.createElement('span'); set.textContent = version.card_set;
    option.append(radio, image, name, set); options.append(option);
  }});
  draftArea.append(row); suggestions.replaceChildren(); suggestions.style.display = 'none'; searchInput.value = '';
  bindRow(row);
  bindThumbnailPreview(row);
  preloadFullImages(row);
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
watchThumbnails();
bindThumbnailPreview(document);
preloadFullImages(document);
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
        prefetcher=None,
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
                if prefetcher:
                    prefetcher.note_activity()
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
                if prefetcher:
                    prefetcher.note_activity()
                parsed_url = urlparse(self.path)
                if parsed_url.path not in {"/select", "/client-log", "/market"}:
                    self.send_error(405, "Method not allowed")
                    return

                try:
                    content_length = int(self.headers.get("Content-Length", "0"))
                except ValueError:
                    self.send_error(400, "Invalid request body")
                    return
                body_limit = (
                    MAX_CLIENT_LOG_BYTES
                    if parsed_url.path == "/client-log"
                    else MAX_MARKET_BODY_BYTES
                    if parsed_url.path == "/market"
                    else MAX_SELECTION_BODY_BYTES
                )
                if content_length > body_limit:
                    self.send_error(413, "Selection request is too large")
                    return

                body = self.rfile.read(content_length)
                query = parse_qs(parsed_url.query)
                if parsed_url.path == "/market":
                    if not self._session_matches(query):
                        self.send_error(403, "Invalid selection session")
                        return
                    self._market_estimate(body)
                    return
                if parsed_url.path == "/client-log":
                    if not self._session_matches(query):
                        self.send_error(403, "Invalid selection session")
                        return
                    self._log_client_warning(body)
                    return
                for key, values in parse_qs(
                    body.decode("utf-8", errors="replace")
                ).items():
                    query.setdefault(key, []).extend(values)
                if not self._session_matches(query):
                    self.send_error(403, "Invalid selection session")
                    return
                self._save_selections(query)

            def _market_estimate(self, body):
                if not selector.market_provider:
                    self._respond_json({"available": False, "message": "Market pricing is disabled"})
                    return
                try:
                    payload = json.loads(body.decode("utf-8"))
                    raw_rows = payload.get("rows") if isinstance(payload, dict) else None
                    if not isinstance(raw_rows, list) or len(raw_rows) > 100:
                        raise ValueError("Invalid market rows")
                    rows = []
                    for raw_row in raw_rows:
                        if not isinstance(raw_row, dict):
                            raise ValueError("Invalid market row")
                        name = str(raw_row.get("name", ""))[:200].strip()
                        card_set = str(raw_row.get("set", ""))[:200].strip()
                        quantity = int(raw_row.get("quantity", 0))
                        if not name or not card_set or quantity < 1 or quantity > 100000:
                            raise ValueError("Invalid market row values")
                        rows.append((name, card_set, quantity))
                    estimate = selector.market_provider.estimate_rows(rows)
                except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as error:
                    self.send_error(400, str(error))
                    return
                self._respond_json(
                    {
                        "available": estimate.available,
                        "currency": estimate.currency,
                        "market_total": estimate.market_total,
                        "matched_quantity": estimate.matched_quantity,
                        "total_quantity": estimate.total_quantity,
                        "unavailable_count": len(estimate.unavailable),
                        "source": estimate.source,
                        "message": estimate.unavailable[0] if estimate.unavailable else "",
                    }
                )

            @staticmethod
            def _session_matches(query):
                return not session_id or query.get("session", [""])[0] == session_id

            def _log_client_warning(self, body):
                try:
                    payload = json.loads(body.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    self.send_error(400, "Invalid client log")
                    return
                if not isinstance(payload, dict) or payload.get("event") != "full-image-load-failed":
                    self.send_error(400, "Invalid client log event")
                    return
                message = str(payload.get("message", "client image warning"))[:500]
                source = str(payload.get("source", "unknown source"))[:500]
                logger.warning("Browser picker warning: %s Source: %s", message, source)
                self.send_response(204)
                self.send_header("Cache-Control", "no-store")
                self.end_headers()

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
                    image = image_data_by_id.get(image_id)
                    result["image_full_url"] = (
                        google_full_image_url(image[2])
                        if image and len(image) > 2
                        else result["image_path"]
                    )
                    if result["image_url"] and result["image_path"] is None:
                        logger.warning(
                            "Full image unavailable for search result '%s' from %s.",
                            result["name"],
                            result["image_url"],
                        )
                return results

            def _serve_image(self, path):
                image_id = path.removeprefix("/images/")
                if not image_id.isdigit() or int(image_id) not in image_data_by_id:
                    self.send_error(404)
                    return
                image, content_type, _ = image_data_by_id[int(image_id)]
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
                    logger.info(
                        "Selection recap is ready; keeping session %s alive for editing or finalization.",
                        session_id,
                    )

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
                market_estimate = None
                if selector.market_provider:
                    market_estimate = selector.market_provider.estimate_rows(
                        [
                            (candidate.name, candidate.card_set, quantity)
                            for _, candidate, quantity in selected_rows
                        ],
                        vendor_total=selector._vendor_total_for_rows(selected_rows),
                    )
                table_rows = []
                for requested_name, candidate, quantity in selected_rows:
                    image_id = image_ids.get(candidate.image_url)
                    image = image_data_by_id.get(image_id)
                    if image:
                        image_bytes, content_type, _ = image
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
                market_markup = ""
                if market_estimate:
                    if market_estimate.available:
                        saving_markup = ""
                        if market_estimate.saving is not None:
                            saving_markup = (
                                f'<span>Estimated difference: {escape(market_estimate.currency)} '
                                f'{market_estimate.saving:.2f}'
                                + (
                                    f' ({market_estimate.saving_percent:.1f}%)'
                                    if market_estimate.saving_percent is not None
                                    else ""
                                )
                                + "</span>"
                            )
                        elif market_estimate.vendor_total is not None:
                            saving_markup = (
                                f'<span>Vendor total: {escape(market_estimate.vendor_currency)} '
                                f'{market_estimate.vendor_total:.2f}; configure currency conversion '
                                "to estimate saving</span>"
                            )
                        partial_markup = ""
                        if market_estimate.unavailable:
                            partial_markup = (
                                f'<small>{len(market_estimate.unavailable)} line(s) could not be priced</small>'
                            )
                        market_markup = (
                            '<div class="market-comparison">'
                            f'<strong>Market benchmark: {escape(market_estimate.currency)} '
                            f'{market_estimate.market_total:.2f}</strong>'
                            f'<span>Source: {escape(market_estimate.source)}</span>'
                            f'{saving_markup}{partial_markup}</div>'
                        )
                    else:
                        reason = market_estimate.unavailable[0] if market_estimate.unavailable else "No data"
                        market_markup = (
                            '<div class="market-comparison unavailable">'
                            f'<strong>Market comparison unavailable</strong>'
                            f'<span>{escape(reason)}</span></div>'
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
.market-comparison {{ background: var(--surface); border-bottom: 1px solid var(--border); display: flex; flex-wrap: wrap; gap: 8px 18px; padding: 13px 20px; }}
.market-comparison strong {{ color: var(--success); font-size: .92rem; }}
.market-comparison span, .market-comparison small {{ color: var(--muted); font-size: .78rem; }}
.market-comparison.unavailable strong {{ color: #f0c66b; }}
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
{market_markup}
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
