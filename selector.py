from html import escape
from html.parser import HTMLParser
import logging
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse
import threading
import webbrowser

import requests


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
    def __init__(self, columns):
        self.columns = columns

    def select(self, wanted, candidates):
        image_urls = [self._get_image_url(candidate.image_url) for candidate in candidates]
        selection = {"index": None}
        completed = threading.Event()
        page = self._build_page(wanted, candidates, image_urls)
        handler = self._create_handler(page, selection, completed)
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

        if selection["index"] is None or selection["index"] >= len(candidates):
            return None
        return candidates[selection["index"]]

    def _get_image_url(self, image_page_url):
        if not image_page_url:
            return None

        try:
            response = requests.get(image_page_url, timeout=30)
            response.raise_for_status()
            parser = OpenGraphImageParser()
            parser.feed(response.text)
            if not parser.image_url:
                raise ValueError("The image page does not expose an Open Graph image")
            return parser.image_url
        except (requests.RequestException, ValueError) as error:
            logging.warning("Could not load card image from %s: %s", image_page_url, error)
            return None

    def _build_page(self, wanted, candidates, image_urls):
        cards = []
        for index, (candidate, image_url) in enumerate(zip(candidates, image_urls)):
            image = (
                f'<img src="{escape(image_url, quote=True)}" alt="{escape(candidate.name)}">'
                if image_url
                else '<div class="missing-image">Image unavailable</div>'
            )
            cards.append(
                f'<a class="card" href="/select?choice={index}">{image}'
                f'<strong>{escape(candidate.name)}</strong>'
                f'<span>{escape(candidate.card_set)}</span></a>'
            )

        return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Choose a proxy</title>
<style>
body {{ font-family: system-ui, sans-serif; margin: 0; background: #121212; color: #f7f7f7; }}
header {{ padding: 24px; text-align: center; }}
h1 {{ margin: 0; font-size: 1.5rem; }}
.grid {{ display: grid; grid-template-columns: repeat({self.columns}, minmax(200px, 1fr)); gap: 18px; padding: 24px; max-width: 1100px; margin: auto; }}
.card {{ background: #222; border-radius: 10px; color: inherit; overflow: hidden; text-align: center; text-decoration: none; padding-bottom: 12px; transition: transform .15s, background .15s; }}
.card:hover {{ background: #303030; transform: translateY(-3px); }}
.card img, .missing-image {{ display: block; width: 100%; height: 280px; object-fit: contain; background: #000; }}
.missing-image {{ align-content: center; color: #aaa; }}
.card strong, .card span {{ display: block; padding: 5px 10px 0; }}
.card span {{ color: #aaa; font-size: .85rem; }}
.skip {{ display: block; margin: 0 auto 30px; width: max-content; color: #ddd; }}
</style>
</head>
<body>
<header><h1>Choose a version of {escape(wanted)}</h1></header>
<main class="grid">{''.join(cards)}</main>
<a class="skip" href="/select?choice=skip">Skip this card</a>
</body>
</html>"""

    def _create_handler(self, page, selection, completed):
        class SelectionHandler(BaseHTTPRequestHandler):
            def do_GET(self):
                parsed_url = urlparse(self.path)
                if parsed_url.path == "/select":
                    choice = parse_qs(parsed_url.query).get("choice", ["skip"])[0]
                    if choice.isdigit():
                        selection["index"] = int(choice)
                    completed.set()
                    self._respond(
                        "<script>window.close()</script>"
                        "<p>Selection saved. You can close this tab if it remains open.</p>"
                    )
                    return

                self._respond(page)

            def _respond(self, body):
                encoded_body = body.encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(encoded_body)))
                self.end_headers()
                self.wfile.write(encoded_body)

            def log_message(self, format, *args):
                return

        return SelectionHandler
