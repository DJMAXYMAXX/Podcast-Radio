#!/usr/bin/env python3
"""
Podcast Radio server.

Builds a big random pool of podcast episodes from every corner of the
podcast world, then runs N "channels" on a shared clock. Every channel is
always "on air": tune in and you land wherever that channel currently is,
mid-episode included.

Standard library only. Run:  python3 server.py   then open http://localhost:8088
"""

import json
import os
import random
import sys
import threading
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "config.json")
LIBRARY_PATH = os.path.join(HERE, "library.json")
DURATIONS_PATH = os.path.join(HERE, "durations.json")
STATIC_DIR = os.path.join(HERE, "static")

ITUNES_NS = "{http://www.itunes.com/dtds/podcast-1.0.dtd}"
UA = "PodcastRadio/0.1 (+personal project)"


def log(*a):
    print(time.strftime("[%H:%M:%S]"), *a, flush=True)


def load_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default


def save_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f)
    os.replace(tmp, path)


CONFIG = load_json(CONFIG_PATH, {})


# ---------------------------------------------------------------- fetching

def http_get(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def discover_feeds(terms, per_term):
    """Ask Apple's public podcast directory for shows matching each term."""
    feeds = {}

    def one(term):
        q = urllib.parse.urlencode(
            {"media": "podcast", "entity": "podcast", "term": term, "limit": per_term}
        )
        try:
            data = json.loads(http_get("https://itunes.apple.com/search?" + q))
            return [
                (r["feedUrl"], r.get("collectionName", ""))
                for r in data.get("results", [])
                if r.get("feedUrl")
            ]
        except Exception as e:
            log("discover failed:", term, e)
            return []

    with ThreadPoolExecutor(8) as pool:
        for found in pool.map(one, terms):
            for url, name in found:
                feeds[url] = name
    return feeds


def parse_duration(text):
    if not text:
        return None
    text = text.strip()
    try:
        if ":" in text:
            secs = 0
            for part in text.split(":"):
                secs = secs * 60 + float(part)
            return int(secs)
        return int(float(text))
    except ValueError:
        return None


def parse_feed(feed_url, max_eps, max_scan=60, max_bytes=6_000_000):
    """Stream a feed and stop early, so huge feeds never sit in memory whole."""
    req = urllib.request.Request(feed_url, headers={"User-Agent": UA})
    parser = ET.XMLPullParser(events=("start", "end"))
    show, show_img, eps, depth_tags = None, None, [], []
    read = 0
    with urllib.request.urlopen(req, timeout=20) as r:
        while len(eps) < max_scan and read < max_bytes:
            chunk = r.read(65536)
            if not chunk:
                break
            read += len(chunk)
            parser.feed(chunk)
            for ev, el in parser.read_events():
                if ev == "start":
                    depth_tags.append(el.tag)
                    continue
                depth_tags.pop()
                parent = depth_tags[-1] if depth_tags else None
                if parent == "channel":
                    if el.tag == "title" and show is None:
                        show = (el.text or "").strip() or "Unknown show"
                    elif el.tag == ITUNES_NS + "image" and not show_img:
                        show_img = el.get("href")
                    elif el.tag == "image" and not show_img:
                        show_img = el.findtext("url")
                    if el.tag == "item":
                        ep = item_to_episode(el, show or "Unknown show", show_img, feed_url)
                        if ep:
                            eps.append(ep)
                    el.clear()
    if len(eps) > max_eps:
        recent = eps[: max_eps // 2]
        older = random.Random(feed_url).sample(eps[max_eps // 2 :], max_eps - len(recent))
        eps = recent + older
    return eps


def item_to_episode(item, show, show_img, feed_url):
    enc = item.find("enclosure")
    if enc is None or not enc.get("url"):
        return None
    if "audio" not in (enc.get("type") or "audio"):
        return None
    dur = parse_duration(item.findtext(ITUNES_NS + "duration"))
    if not dur:
        try:
            dur = int(int(enc.get("length") or 0) / 16000) or None
        except ValueError:
            dur = None
    dur = dur or 2700
    if dur < 180 or dur > 4 * 3600:
        return None
    ei = item.find(ITUNES_NS + "image")
    return {
        "show": show,
        "title": (item.findtext("title") or "Untitled").strip(),
        "audio": enc.get("url"),
        "duration": dur,
        "image": (ei.get("href") if ei is not None else None) or show_img,
        "feed": feed_url,
    }


def build_library():
    cfg = CONFIG
    feeds = {u: "" for u in cfg.get("curated_feeds", [])}
    if cfg.get("discover", True):
        terms = cfg.get("discovery_terms", [])
        log(f"Discovering shows for {len(terms)} topics...")
        found = discover_feeds(terms, cfg.get("shows_per_term", 4))
        feeds.update(found)
    log(f"Fetching {len(feeds)} feeds...")

    def one(url):
        try:
            return parse_feed(url, cfg.get("episodes_per_show", 12))
        except Exception as e:
            log("feed failed:", url[:80], type(e).__name__)
            return []

    episodes = []
    done = 0
    with ThreadPoolExecutor(4) as pool:
        for eps in pool.map(one, feeds):
            episodes.extend(eps)
            done += 1
            if done == 40 and not STATION.library and episodes:
                # get on air early instead of waiting for every feed
                log(f"On air early with {len(episodes)} episodes, still loading...")
                STATION.set_library(sorted(episodes, key=lambda e: e["audio"]))
    # De-duplicate by audio url, keep a stable order so schedules are repeatable.
    seen, unique = set(), []
    for e in episodes:
        if e["audio"] not in seen:
            seen.add(e["audio"])
            unique.append(e)
    unique.sort(key=lambda e: e["audio"])
    shows = len({e["show"] for e in unique})
    log(f"Library ready: {len(unique)} episodes from {shows} shows.")
    return unique


# ---------------------------------------------------------------- broadcast clock

class Channel:
    """One always-on station. Walks a seeded random playlist forward in time."""

    def __init__(self, number, freq, seed):
        self.number = number
        self.freq = freq
        self.seed = seed
        self.reset()

    def reset(self):
        self.rng = random.Random(self.seed)
        self.start = EPOCH
        self.current = None
        self.prev_show = None

    def _pick(self, library):
        for _ in range(8):
            ep = self.rng.choice(library)
            if ep["show"] != self.prev_show:
                break
        self.prev_show = ep["show"]
        return ep

    def now(self, library, durations, t):
        if self.current is None:
            self.current = self._pick(library)
        while True:
            dur = durations.get(self.current["audio"], self.current["duration"])
            if self.start + dur > t:
                return self.current, t - self.start, dur
            self.start += dur
            self.current = self._pick(library)


EPOCH = 1767225600  # 2026-01-01 00:00 UTC, the day the station "went on air"


class Station:
    def __init__(self):
        self.lock = threading.Lock()
        self.library = []
        self.durations = load_json(DURATIONS_PATH, {})
        n = CONFIG.get("channels", 10)
        lo, hi = 88.1, 107.9
        step = (hi - lo) / max(n - 1, 1)
        self.channels = [
            Channel(i, round(lo + i * step, 1), f"podcast-radio-ch{i}") for i in range(n)
        ]
        self.status = "starting"

    def set_library(self, lib):
        with self.lock:
            self.library = lib
            for c in self.channels:
                c.reset()
            self.status = "ready" if lib else "empty"

    def now(self, ch):
        with self.lock:
            if not self.library:
                return None
            c = self.channels[ch]
            ep, offset, dur = c.now(self.library, self.durations, time.time())
            return {
                "channel": ch,
                "freq": c.freq,
                "episode": {**ep, "duration": dur},
                "offset": offset,
                "server_time": time.time(),
            }

    def correct_duration(self, audio, actual):
        with self.lock:
            if actual == 0 or 60 < actual < 5 * 3600:
                # 0 marks an episode that won't play; the schedule skips it
                self.durations[audio] = int(actual)
                # skips stay in memory only, so a bad night of wifi isn't permanent
                save_json(DURATIONS_PATH, {k: v for k, v in self.durations.items() if v})


STATION = Station()


def library_loader():
    cached = load_json(LIBRARY_PATH, None)
    max_age = CONFIG.get("refresh_hours", 24) * 3600
    if cached and time.time() - cached.get("built", 0) < max_age and cached.get("episodes"):
        log(f"Using cached library ({len(cached['episodes'])} episodes).")
        STATION.set_library(cached["episodes"])
    while True:
        cached = load_json(LIBRARY_PATH, None)
        if not cached or time.time() - cached.get("built", 0) >= max_age:
            STATION.status = "building library" if not STATION.library else STATION.status
            lib = build_library()
            if lib:
                save_json(LIBRARY_PATH, {"built": time.time(), "episodes": lib})
                STATION.set_library(lib)
            elif not STATION.library:
                STATION.status = "empty"
        time.sleep(600)


# ---------------------------------------------------------------- web

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send_json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(u.query)
        if u.path == "/api/channels":
            return self.send_json(
                {
                    "status": STATION.status,
                    "episodes": len(STATION.library),
                    "shows": len({e["show"] for e in STATION.library}),
                    "channels": [{"channel": c.number, "freq": c.freq} for c in STATION.channels],
                }
            )
        if u.path == "/api/now":
            try:
                ch = int(qs.get("ch", ["0"])[0])
                assert 0 <= ch < len(STATION.channels)
            except Exception:
                return self.send_json({"error": "bad channel"}, 400)
            data = STATION.now(ch)
            return self.send_json(data or {"error": STATION.status}, 200 if data else 503)
        if u.path in ("/", "/index.html"):
            path = os.path.join(STATIC_DIR, "index.html")
            with open(path, "rb") as f:
                body = f.read()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            return self.wfile.write(body)
        self.send_json({"error": "not found"}, 404)

    def do_POST(self):
        if self.path == "/api/duration":
            n = int(self.headers.get("Content-Length", 0))
            try:
                d = json.loads(self.rfile.read(n))
                STATION.correct_duration(d["audio"], float(d["duration"]))
            except Exception:
                return self.send_json({"ok": False}, 400)
            return self.send_json({"ok": True})
        self.send_json({"error": "not found"}, 404)


def main():
    port = int(os.environ.get("PORT", CONFIG.get("port", 8088)))
    threading.Thread(target=library_loader, daemon=True).start()
    srv = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    log(f"Podcast Radio on http://localhost:{port}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        sys.exit(0)


if __name__ == "__main__":
    main()
