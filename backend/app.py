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


def collect_source_urls(source: Source) -> list[str]:
    urls: list[str] = []
    for page in range(source.start_page, source.start_page + source.max_pages):
        try:
            response = requests.get(source_page_url(source, page), timeout=30, allow_redirects=True, headers={"User-Agent": "Mozilla/5.0"})
            response.raise_for_status()
            if source.kind == "json":
                urls.extend(extract_urls_from_json(source, response.json()))
            else:
                html = response.content.decode(source.encoding, errors="ignore")
                urls.extend(extract_urls_from_html(source, html))
        except Exception:
            continue
    return list(dict.fromkeys(urls))


@app.get("/health")
def health() -> dict[str, Any]:
    return {"ok": True, "sources": len(SOURCES)}


@app.post("/sync")
def sync() -> dict[str, Any]:
    discovered: list[str] = []
    for source in SOURCES:
        discovered.extend(collect_source_urls(source))
    discovered = list(dict.fromkeys(discovered))

    with connect() as db:
        existing = {row[0] for row in db.execute("SELECT image_url FROM products").fetchall()}

    inserted = 0
    for image_url in discovered:
        if image_url in existing:
            continue
        try:
            phash = make_phash(request_bytes(image_url))
        except Exception:
            continue
        with connect() as db:
            db.execute("INSERT OR IGNORE INTO products (image_url, phash) VALUES (?, ?)", (image_url, phash))
            if db.total_changes:
                inserted += 1
    return {"updated": True, "discovered": len(discovered), "inserted": inserted}


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
