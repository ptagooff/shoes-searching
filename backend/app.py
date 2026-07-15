from __future__ import annotations

import io
import json
import os
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urljoin

import imagehash
import requests
from fastapi import FastAPI, File, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from PIL import Image, UnidentifiedImageError

DB_PATH = Path(os.getenv("DB_PATH", "./data/sole_search.db"))
ALLOWED_ORIGINS = [x.strip() for x in os.getenv("ALLOWED_ORIGINS", "*").split(",") if x.strip()]
DATA_SOURCES_JSON = os.getenv("DATA_SOURCES_JSON", "[]")
MAX_IMAGE_BYTES = int(os.getenv("MAX_IMAGE_BYTES", str(12 * 1024 * 1024)))
SYNC_BATCH_SIZE = int(os.getenv("SYNC_BATCH_SIZE", "40"))
MAX_SYNC_ATTEMPTS = int(os.getenv("MAX_SYNC_ATTEMPTS", "2"))


@dataclass(frozen=True)
class Source:
    list_url: str
    name: str = ""
    kind: str = "html"
    url_template: str = ""
    start_page: int = 1
    max_pages: int = 1
    page_param: str = "page"
    encoding: str = "utf-8"
    items_field: str = "data.list"
    image_field: str = "image_url"
    image_url_regex: str = r"""<a[^>]+href=['"]?ProductShow\.asp\?ID=\d+[^>]*>\s*<img[^>]+src=['"]?([^'"\s>]+)"""


def load_sources() -> list[Source]:
    rows = json.loads(DATA_SOURCES_JSON)
    return [Source(**row) for row in rows]


SOURCES = load_sources()
last_sync: dict[str, Any] = {}
app = FastAPI(title="鞋底识图 API")
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


def connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    return db


def init_db() -> None:
    with connect() as db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS products (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                image_url TEXT NOT NULL UNIQUE,
                phash TEXT NOT NULL
            )
        """)
        columns = [row[1] for row in db.execute("PRAGMA table_info(products)").fetchall()]
        if columns != ["id", "image_url", "phash"]:
            db.execute("""
                CREATE TABLE products_new (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    image_url TEXT NOT NULL UNIQUE,
                    phash TEXT NOT NULL
                )
            """)
            if "image_url" in columns and "phash" in columns:
                db.execute("INSERT OR IGNORE INTO products_new (image_url, phash) SELECT image_url, phash FROM products")
            db.execute("DROP TABLE products")
            db.execute("ALTER TABLE products_new RENAME TO products")
        db.execute("""
            CREATE TABLE IF NOT EXISTS sync_queue (
                image_url TEXT PRIMARY KEY,
                status TEXT NOT NULL DEFAULT 'pending',
                attempts INTEGER NOT NULL DEFAULT 0,
                last_error TEXT NOT NULL DEFAULT ''
            )
        """)


@app.on_event("startup")
def startup() -> None:
    init_db()


def nested(value: Any, path: str) -> Any:
    for key in path.split("."):
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value


def source_page_url(source: Source, page: int) -> str:
    if source.url_template:
        return source.url_template.format(page=page)
    sep = "&" if "?" in source.list_url else "?"
    return f"{source.list_url}{sep}{source.page_param}={page}"


def request_bytes(url: str) -> bytes:
    response = requests.get(url, timeout=30, allow_redirects=True, stream=True, headers={"User-Agent": "Mozilla/5.0"})
    response.raise_for_status()
    chunks: list[bytes] = []
    size = 0
    for chunk in response.iter_content(64 * 1024):
        if not chunk:
            continue
        size += len(chunk)
        if size > MAX_IMAGE_BYTES:
            raise ValueError("图片过大")
        chunks.append(chunk)
    return b"".join(chunks)


def product_count() -> int:
    with connect() as db:
        return int(db.execute("SELECT COUNT(*) FROM products").fetchone()[0])


def queue_counts() -> dict[str, int]:
    with connect() as db:
        rows = db.execute("SELECT status, COUNT(*) AS count FROM sync_queue GROUP BY status").fetchall()
    counts = {"pending": 0, "done": 0, "failed": 0}
    for row in rows:
        counts[str(row["status"])] = int(row["count"])
    return counts


def enqueue_urls(urls: list[str]) -> int:
    added = 0
    with connect() as db:
        existing_products = {row[0] for row in db.execute("SELECT image_url FROM products").fetchall()}
        for url in urls:
            if url in existing_products:
                db.execute("""
                    INSERT OR IGNORE INTO sync_queue (image_url, status, attempts, last_error)
                    VALUES (?, 'done', 0, '')
                """, (url,))
                continue
            before = db.total_changes
            db.execute("""
                INSERT OR IGNORE INTO sync_queue (image_url, status, attempts, last_error)
                VALUES (?, 'pending', 0, '')
            """, (url,))
            if db.total_changes > before:
                added += 1
    return added


def pending_urls(limit: int) -> list[str]:
    with connect() as db:
        rows = db.execute("""
            SELECT image_url
            FROM sync_queue
            WHERE status = 'pending' AND attempts < ?
            ORDER BY attempts ASC, image_url ASC
            LIMIT ?
        """, (MAX_SYNC_ATTEMPTS, limit)).fetchall()
    return [str(row["image_url"]) for row in rows]


def mark_done(image_url: str) -> None:
    with connect() as db:
        db.execute("UPDATE sync_queue SET status = 'done', last_error = '' WHERE image_url = ?", (image_url,))


def mark_failed(image_url: str, error: str) -> None:
    with connect() as db:
        row = db.execute("SELECT attempts FROM sync_queue WHERE image_url = ?", (image_url,)).fetchone()
        attempts = int(row["attempts"]) + 1 if row else 1
        status = "failed" if attempts >= MAX_SYNC_ATTEMPTS else "pending"
        db.execute("""
            UPDATE sync_queue
            SET status = ?, attempts = ?, last_error = ?
            WHERE image_url = ?
        """, (status, attempts, error[:220], image_url))


def make_phash(data: bytes) -> str:
    try:
        with Image.open(io.BytesIO(data)) as image:
            return str(imagehash.phash(image.convert("RGB")))
    except (UnidentifiedImageError, OSError) as exc:
        raise ValueError("无法读取图片") from exc


def extract_urls_from_html(source: Source, html: str) -> list[str]:
    urls: list[str] = []
    for match in re.finditer(source.image_url_regex, html, re.IGNORECASE | re.DOTALL):
        urls.append(urljoin(source.list_url, match.group(1).strip().strip("'\"")))
    return list(dict.fromkeys(urls))


def extract_urls_from_json(source: Source, payload: Any) -> list[str]:
    items = nested(payload, source.items_field)
    if not isinstance(items, list):
        return []
    urls = [nested(item, source.image_field) for item in items]
    return [url for url in urls if isinstance(url, str) and url.startswith(("http://", "https://"))]


def collect_source_urls(source: Source) -> dict[str, Any]:
    urls: list[str] = []
    page_errors: list[dict[str, Any]] = []
    for page in range(source.start_page, source.start_page + source.max_pages):
        try:
            response = requests.get(source_page_url(source, page), timeout=30, allow_redirects=True, headers={"User-Agent": "Mozilla/5.0"})
            response.raise_for_status()
            if source.kind == "json":
                urls.extend(extract_urls_from_json(source, response.json()))
            else:
                html = response.content.decode(source.encoding, errors="ignore")
                urls.extend(extract_urls_from_html(source, html))
        except Exception as exc:
            if len(page_errors) < 10:
                page_errors.append({"page": page, "error": str(exc)[:180]})
            continue
    return {
        "source": source.name or source.list_url,
        "start_page": source.start_page,
        "max_pages": source.max_pages,
        "urls": list(dict.fromkeys(urls)),
        "page_errors": page_errors,
    }


@app.get("/health")
def health() -> dict[str, Any]:
    return {"ok": True, "sources": len(SOURCES)}


@app.get("/stats")
def stats() -> dict[str, Any]:
    counts = queue_counts()
    return {
        "ok": True,
        "sources": len(SOURCES),
        "products": product_count(),
        "queue": counts,
        "pending": counts["pending"],
        "failed_total": counts["failed"],
        "last_sync": last_sync,
    }


@app.post("/sync")
def sync(reset_failed: bool = Query(False)) -> dict[str, Any]:
    global last_sync
    if reset_failed:
        with connect() as db:
            db.execute("UPDATE sync_queue SET status = 'pending', attempts = 0, last_error = '' WHERE status = 'failed'")

    discovered: list[str] = []
    source_reports: list[dict[str, Any]] = []
    counts_before = queue_counts()
    if counts_before["pending"] == 0:
        for source in SOURCES:
            report = collect_source_urls(source)
            urls = report.pop("urls")
            report["discovered"] = len(urls)
            source_reports.append(report)
            discovered.extend(urls)
    discovered = list(dict.fromkeys(discovered))
    queued_new = enqueue_urls(discovered) if discovered else 0

    with connect() as db:
        existing = {row[0] for row in db.execute("SELECT image_url FROM products").fetchall()}

    inserted = 0
    skipped_existing = 0
    failed = 0
    failure_samples: list[dict[str, str]] = []
    batch = pending_urls(SYNC_BATCH_SIZE)
    for image_url in batch:
        if image_url in existing:
            skipped_existing += 1
            mark_done(image_url)
            continue
        try:
            phash = make_phash(request_bytes(image_url))
        except Exception as exc:
            failed += 1
            mark_failed(image_url, str(exc))
            if len(failure_samples) < 10:
                failure_samples.append({"url": image_url, "error": str(exc)[:220]})
            continue
        with connect() as db:
            db.execute("INSERT OR IGNORE INTO products (image_url, phash) VALUES (?, ?)", (image_url, phash))
            if db.total_changes:
                inserted += 1
        mark_done(image_url)
    counts_after = queue_counts()
    last_sync = {
        "updated": True,
        "discovered": len(discovered),
        "queued_new": queued_new,
        "processed": len(batch),
        "inserted": inserted,
        "skipped_existing": skipped_existing,
        "failed": failed,
        "pending": counts_after["pending"],
        "failed_total": counts_after["failed"],
        "complete": counts_after["pending"] == 0,
        "products": product_count(),
        "sources": source_reports,
        "failure_samples": failure_samples,
    }
    return last_sync


@app.post("/search")
async def search(image: UploadFile = File(...), limit: int = Query(30, ge=1, le=100)) -> dict[str, Any]:
    data = await image.read(MAX_IMAGE_BYTES + 1)
    if len(data) > MAX_IMAGE_BYTES:
        raise HTTPException(413, "图片过大")
    try:
        query_hash = imagehash.hex_to_hash(make_phash(data))
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc

    with connect() as db:
        rows = db.execute("SELECT id, image_url, phash FROM products").fetchall()

    results = []
    for row in rows:
        distance = query_hash - imagehash.hex_to_hash(row["phash"])
        results.append({
            "id": row["id"],
            "similarity": round(max(0, 1 - distance / 64) * 100, 1),
            "proxy_url": "/proxy?" + urlencode({"url": row["image_url"]}),
        })
    results.sort(key=lambda item: item["similarity"], reverse=True)
    return {"count": min(len(results), limit), "results": results[:limit]}


@app.get("/proxy")
def proxy(url: str) -> StreamingResponse:
    response = requests.get(url, timeout=30, allow_redirects=True, stream=True, headers={"User-Agent": "Mozilla/5.0"})
    response.raise_for_status()
    return StreamingResponse(
        response.iter_content(64 * 1024),
        media_type=response.headers.get("content-type", "image/jpeg"),
    )
