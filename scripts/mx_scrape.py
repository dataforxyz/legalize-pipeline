"""Slow downloader for Mexican federal legislation from Cámara de Diputados.

Phase 1: build inventory from index.htm + regla.htm
Phase 2: download .doc for each entry (skip existing), 5s gap
Phase 3: for entries with bad/missing .doc, download .pdf as fallback
Phase 4: download .pdf for ALL entries (skip existing)

Run:  python scripts/mx_scrape.py
Resume-safe: re-running picks up where it left off.
"""
from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path
from urllib.parse import urljoin

import requests

BASE = "https://www.diputados.gob.mx/LeyesBiblio/"
INDEX_PAGES = ["index.htm", "regla.htm"]
UA = "legalize-bot/1.0 (+https://github.com/legalize-dev/legalize-pipeline)"
SLEEP_SECONDS = 5.0
REQUEST_TIMEOUT = 60
MIN_VALID_BYTES = 8 * 1024  # below this, treat as broken/error page

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data-mx"
DOC_DIR = DATA / "doc"
PDF_DIR = DATA / "pdf"
INVENTORY = DATA / "inventory.json"
LOG = DATA / "log.txt"


def log(msg: str) -> None:
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line, flush=True)
    with LOG.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def fetch(url: str) -> bytes:
    r = requests.get(url, headers={"User-Agent": UA}, timeout=REQUEST_TIMEOUT)
    r.raise_for_status()
    return r.content


def build_inventory() -> dict:
    """Parse index pages once and return {slug: {title, doc_url, pdf_url, source_page}}."""
    if INVENTORY.exists():
        log(f"using cached inventory at {INVENTORY}")
        return json.loads(INVENTORY.read_text(encoding="utf-8"))

    inventory: dict[str, dict] = {}
    href_re = re.compile(r'href="(doc|pdf)/([A-Za-z0-9_-]+)\.(doc|pdf)"', re.IGNORECASE)

    for page in INDEX_PAGES:
        url = urljoin(BASE, page)
        log(f"fetching index page {url}")
        raw = fetch(url)
        try:
            html = raw.decode("windows-1252")
        except UnicodeDecodeError:
            html = raw.decode("latin-1", errors="replace")
        time.sleep(SLEEP_SECONDS)

        for kind, slug, ext in href_re.findall(html):
            entry = inventory.setdefault(
                slug,
                {"slug": slug, "title": None, "doc_url": None, "pdf_url": None, "source_pages": []},
            )
            if page not in entry["source_pages"]:
                entry["source_pages"].append(page)
            if ext.lower() == "doc":
                entry["doc_url"] = urljoin(BASE, f"doc/{slug}.{ext}")
            else:
                entry["pdf_url"] = urljoin(BASE, f"pdf/{slug}.{ext}")

    DATA.mkdir(parents=True, exist_ok=True)
    INVENTORY.write_text(json.dumps(inventory, indent=2, ensure_ascii=False), encoding="utf-8")
    log(f"wrote inventory: {len(inventory)} entries -> {INVENTORY}")
    return inventory


def download_one(url: str, dest: Path) -> tuple[bool, int, str]:
    """Download url -> dest. Returns (ok, size, note)."""
    if dest.exists() and dest.stat().st_size > 0:
        return True, dest.stat().st_size, "skip-exists"
    try:
        data = fetch(url)
    except Exception as e:
        return False, 0, f"error: {e}"
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    tmp.write_bytes(data)
    tmp.rename(dest)
    return True, len(data), "downloaded"


def is_bad(path: Path) -> bool:
    if not path.exists():
        return True
    return path.stat().st_size < MIN_VALID_BYTES


def phase_doc(inventory: dict) -> list[str]:
    """Download .doc for every entry. Returns list of slugs whose .doc looks bad."""
    log(f"=== PHASE 2: downloading .doc files ({len(inventory)} entries) ===")
    bad: list[str] = []
    for i, (slug, entry) in enumerate(sorted(inventory.items()), 1):
        if not entry.get("doc_url"):
            log(f"[{i}/{len(inventory)}] {slug}: no doc url, skipping")
            continue
        dest = DOC_DIR / f"{slug}.doc"
        existed = dest.exists()
        ok, size, note = download_one(entry["doc_url"], dest)
        log(f"[{i}/{len(inventory)}] doc/{slug}.doc {note} ({size} bytes)")
        if not ok or is_bad(dest):
            bad.append(slug)
        if not existed and note != "skip-exists":
            time.sleep(SLEEP_SECONDS)
    log(f"phase 2 done. bad/missing doc count: {len(bad)}")
    return bad


def phase_pdf_fallback(inventory: dict, bad_slugs: list[str]) -> None:
    log(f"=== PHASE 3: pdf fallback for {len(bad_slugs)} bad .doc entries ===")
    for i, slug in enumerate(bad_slugs, 1):
        entry = inventory.get(slug, {})
        if not entry.get("pdf_url"):
            log(f"[{i}/{len(bad_slugs)}] {slug}: no pdf url either, giving up")
            continue
        dest = PDF_DIR / f"{slug}.pdf"
        existed = dest.exists()
        ok, size, note = download_one(entry["pdf_url"], dest)
        log(f"[{i}/{len(bad_slugs)}] pdf/{slug}.pdf {note} ({size} bytes) [fallback]")
        if not existed and note != "skip-exists":
            time.sleep(SLEEP_SECONDS)


def phase_pdf_all(inventory: dict) -> None:
    log(f"=== PHASE 4: downloading .pdf for all entries ({len(inventory)}) ===")
    for i, (slug, entry) in enumerate(sorted(inventory.items()), 1):
        if not entry.get("pdf_url"):
            log(f"[{i}/{len(inventory)}] {slug}: no pdf url, skipping")
            continue
        dest = PDF_DIR / f"{slug}.pdf"
        existed = dest.exists()
        ok, size, note = download_one(entry["pdf_url"], dest)
        log(f"[{i}/{len(inventory)}] pdf/{slug}.pdf {note} ({size} bytes)")
        if not existed and note != "skip-exists":
            time.sleep(SLEEP_SECONDS)


def main() -> int:
    DATA.mkdir(parents=True, exist_ok=True)
    DOC_DIR.mkdir(parents=True, exist_ok=True)
    PDF_DIR.mkdir(parents=True, exist_ok=True)
    log("starting mx_scrape")
    inv = build_inventory()
    bad = phase_doc(inv)
    phase_pdf_fallback(inv, bad)
    phase_pdf_all(inv)
    log("done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
