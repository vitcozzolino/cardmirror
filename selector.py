from concurrent.futures import ThreadPoolExecutor, as_completed
from html import escape
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
import threading
from urllib.parse import parse_qs, urlparse
import webbrowser

import requests

from utils import load_vendor_catalog_card, search_vendor_catalog_cache


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


class CardSelector:
    def __init__(self, catalog_cache_path):
        self.catalog_cache_path = catalog_cache_path

    def select_all(self, selection_requests):
        image_data = self._preload_images(selection_requests)
        selections = [None] * len(selection_requests)
        additional_selections = []
        completed = threading.Event()
        page, image_ids = self._build_page(selection_requests, image_data)
        handler = self._create_handler(
            page,
            image_data,
            image_ids,
            selection_requests,
            selections,
            additional_selections,
            completed,
        )
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        url = f"http://127.0.0.1:{server.server_port}"

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
        return selected_candidates, additional_selections

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

        logging.info("Preloading %d card images for interactive selection...", len(image_page_urls))
        image_data = {}
        with ThreadPoolExecutor(max_workers=min(12, len(image_page_urls))) as executor:
            futures = {
                executor.submit(self._download_image, image_page_url): image_page_url
                for image_page_url in image_page_urls
            }
            for completed_count, future in enumerate(as_completed(futures), start=1):
                image_page_url = futures[future]
                try:
                    image_data[image_page_url] = future.result()
                except (requests.RequestException, ValueError) as error:
                    logging.warning(
                        "Could not preload card image from %s: %s",
                        image_page_url,
                        error,
                    )
                logging.info(
                    "Preloaded card images: %d/%d",
                    completed_count,
                    len(image_page_urls),
                )

        return image_data

    def _download_image(self, image_page_url):
        response = requests.get(image_page_url, timeout=30)
        response.raise_for_status()
        parser = OpenGraphImageParser()
        parser.feed(response.text)
        if not parser.image_url:
            raise ValueError("The image page does not expose an Open Graph image")

        image_response = requests.get(parser.image_url, timeout=30)
        image_response.raise_for_status()
        content_type = image_response.headers.get("Content-Type", "image/jpeg")
        if not content_type.startswith("image/"):
            raise ValueError("The image page did not return image data")
        return image_response.content, content_type

    def _build_page(self, selection_requests, image_data):
        image_ids = {image_page_url: index for index, image_page_url in enumerate(image_data)}
        rows = []
        for request_index, (wanted, _, candidates) in enumerate(selection_requests):
            options = []
            for candidate_index, candidate in enumerate(candidates):
                image_id = image_ids.get(candidate.image_url)
                image = (
                    f'<img src="/images/{image_id}" alt="{escape(candidate.name)}">'
                    if image_id is not None
                    else '<div class="missing-image">Image unavailable</div>'
                )
                checked = " checked" if candidate_index == 0 else ""
                options.append(
                    f'<label class="option"><input type="radio" '
                    f'name="selection-{request_index}" value="{candidate_index}"{checked}>'
                    f"{image}<strong>{escape(candidate.name)}</strong>"
                    f"<span>{escape(candidate.card_set)}</span></label>"
                )
            options.append(
                f'<label class="skip"><input type="radio" '
                f'name="selection-{request_index}" value="skip">Skip this card</label>'
            )
            rows.append(
                f'<section class="row"><h2>{escape(wanted)}</h2>'
                f'<div class="options">{"".join(options)}</div></section>'
            )

        return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Choose proxy versions</title>
<style>
body {{ font-family: system-ui, sans-serif; margin: 0; background: #121212; color: #f7f7f7; }}
header {{ padding: 24px; position: sticky; top: 0; background: #121212; border-bottom: 1px solid #333; z-index: 1; }}
h1 {{ margin: 0 0 12px; font-size: 1.5rem; }}
.search {{ display: flex; gap: 8px; max-width: 680px; }}
.search input {{ border: 1px solid #555; border-radius: 6px; flex: 1; font-size: 1rem; padding: 9px; }}
.search-results {{ display: flex; gap: 12px; max-width: 100%; overflow-x: auto; padding-top: 14px; }}
.row {{ border-bottom: 1px solid #333; padding: 20px 24px; }}
h2 {{ margin: 0 0 12px; font-size: 1.2rem; }}
.options {{ display: flex; gap: 12px; overflow-x: auto; padding-bottom: 8px; }}
.option, .search-card {{ background: #222; border-radius: 10px; cursor: pointer; flex: 0 0 190px; overflow: hidden; padding-bottom: 10px; text-align: center; }}
.option:has(input:checked), .search-card:has(input:checked) {{ box-shadow: 0 0 0 3px #66bb6a; }}
.option input, .search-card input[type=checkbox] {{ position: absolute; opacity: 0; }}
.option img, .search-card img, .missing-image {{ background: #000; display: block; height: 250px; object-fit: contain; width: 100%; }}
.missing-image {{ align-content: center; color: #aaa; }}
.option strong, .option span, .search-card strong, .search-card span {{ display: block; padding: 5px 10px 0; }}
.option span, .search-card span {{ color: #aaa; font-size: .8rem; }}
.search-card input[type=number] {{ margin-top: 8px; width: 55px; }}
.skip {{ align-self: center; color: #bbb; flex: 0 0 auto; }}
.submit {{ bottom: 18px; position: fixed; right: 24px; }}
button {{ background: #66bb6a; border: 0; border-radius: 8px; color: #102010; cursor: pointer; font-size: 1rem; font-weight: 700; padding: 10px 14px; }}
</style>
</head>
<body>
<form action="/select" method="get">
<header>
<h1>Choose proxy versions</h1>
<div class="search"><input id="proxy-search" type="search" placeholder="Search the vendor catalog"><button type="button" onclick="searchCatalog()">Add proxy</button></div>
<div id="search-results" class="search-results"></div>
</header>
<main>{"".join(rows)}</main>
<div class="submit"><button type="submit">Save selections</button></div>
</form>
<script>
async function searchCatalog() {{
  const query = document.getElementById('proxy-search').value;
  const response = await fetch('/search?q=' + encodeURIComponent(query));
  const results = await response.json();
  const container = document.getElementById('search-results');
  container.replaceChildren();
  for (const result of results) {{
    const card = document.createElement('label');
    card.className = 'search-card';
    const selected = document.createElement('input');
    selected.type = 'checkbox'; selected.name = 'additional'; selected.value = result.id;
    card.append(selected);
    if (result.image_path) {{
      const image = document.createElement('img'); image.src = result.image_path; image.alt = result.name; card.append(image);
    }}
    const name = document.createElement('strong'); name.textContent = result.name; card.append(name);
    const set = document.createElement('span'); set.textContent = result.card_set; card.append(set);
    const quantity = document.createElement('input');
    quantity.type = 'number'; quantity.name = 'quantity-' + result.id; quantity.min = '1'; quantity.value = '1';
    card.append(quantity); container.append(card);
  }}
}}
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
        additional_selections,
        completed,
    ):
        image_data_by_id = {
            image_id: image_data[image_page_url]
            for image_page_url, image_id in image_ids.items()
        }
        selector = self

        class SelectionHandler(BaseHTTPRequestHandler):
            def do_GET(self):
                parsed_url = urlparse(self.path)
                if parsed_url.path == "/search":
                    self._search_catalog(parse_qs(parsed_url.query).get("q", [""])[0])
                    return
                if parsed_url.path.startswith("/images/"):
                    self._serve_image(parsed_url.path)
                    return
                if parsed_url.path == "/select":
                    self._save_selections(parse_qs(parsed_url.query))
                    return
                self._respond_html(page)

            def _search_catalog(self, query):
                results = search_vendor_catalog_cache(selector.catalog_cache_path, query)
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
                self._respond_json(results)

            def _serve_image(self, path):
                image_id = path.removeprefix("/images/")
                if not image_id.isdigit() or int(image_id) not in image_data_by_id:
                    self.send_error(404)
                    return
                image, content_type = image_data_by_id[int(image_id)]
                self.send_response(200)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(image)))
                self.end_headers()
                self.wfile.write(image)

            def _save_selections(self, query):
                for request_index, (_, _, candidates) in enumerate(selection_requests):
                    choice = query.get(f"selection-{request_index}", ["skip"])[0]
                    if choice.isdigit() and int(choice) < len(candidates):
                        selections[request_index] = int(choice)

                for card_id in query.get("additional", []):
                    if not card_id.isdigit():
                        continue
                    candidate = load_vendor_catalog_card(
                        selector.catalog_cache_path,
                        int(card_id),
                    )
                    if not candidate:
                        continue
                    quantity = query.get(f"quantity-{card_id}", ["1"])[0]
                    try:
                        quantity = max(1, int(quantity))
                    except ValueError:
                        quantity = 1
                    additional_selections.append((candidate, quantity))

                completed.set()
                self._respond_html(
                    "<script>window.close()</script>"
                    "<p>Selections saved. You can close this tab if it remains open.</p>"
                )

            def _respond_html(self, body):
                encoded_body = body.encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(encoded_body)))
                self.end_headers()
                self.wfile.write(encoded_body)

            def _respond_json(self, body):
                encoded_body = json.dumps(body).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(encoded_body)))
                self.end_headers()
                self.wfile.write(encoded_body)

            def log_message(self, format, *args):
                return

        return SelectionHandler
