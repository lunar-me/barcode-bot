#!/usr/bin/env python3
"""
barcode-bot.py — Barcode Scanner Telegram bot

rebuilt around small classes with one job each:

    Settings        all configuration, read once, passed everywhere
    AccessControl   the file-backed allowlist of Telegram user IDs
    ProductRepository  every SQL Server query in one door
    KiwiSquareClient   the external price API
    BarcodeDecoder  barcode decoding with a preprocessing strategy pipeline
    FileDownloader  Telegram file downloads with retries
    ReplyBuilder    pure message formatting: data in, markdown out
    ScanService     one barcode in, a ScanResult out (plus DB housekeeping)
    BarcodeBot      the Telegram façade with thin handlers

Nothing happens at import time: no token is read, no allowlist is loaded, no
database is touched. main() is the composition root; it is the only place the
object graph gets built, which is what makes every class testable in isolation.

Dependencies: python-telegram-bot, pyzbar, Pillow, libzbar0, pymssql
"""

from __future__ import annotations

import asyncio
import httpx
import io
import json
import logging
import os
import re
import urllib.parse
import urllib.request
from abc import ABC, abstractmethod
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterator, Sequence

import pymssql
from PIL import Image
from pyzbar.pyzbar import decode as zbar_decode
from telegram import Update
from telegram.error import TimedOut, NetworkError
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
from telegram.request import HTTPXRequest

logger = logging.getLogger("barcode-bot")

USER_AGENT = "BarcodeBot/1.0"

# Secrets and instance values live in .env (or real environment variables);
# nothing sensitive is hardcoded any more. _load_dotenv() fills the gaps.
_DEFAULT_ADMIN_ID = 0
_DEFAULT_API_URL = "https://api.kiwisquare.co.nz/api/ProductElasticSearch/search-by-barcode-store"
_DEFAULT_STORE_IDS: tuple[str, ...] = ()


def _script_dir() -> str:
    return os.path.dirname(os.path.abspath(__file__))


def _load_dotenv(path: str | None = None) -> None:
    """Load KEY=VALUE pairs from a .env file into the environment.

    The file is looked up next to this script (default). It only fills
    gaps: any variable already present in the environment keeps its value,
    so run.sh exports, systemd units and shell exports always win. A
    missing file is not an error. Values are never logged. Inline comments
    are not supported -- quote a value if it contains '#'.
    """
    if path is None:
        path = os.path.join(_script_dir(), ".env")
    try:
        with open(path, encoding="utf-8-sig") as fh:
            lines = fh.read().splitlines()
    except FileNotFoundError:
        return
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):]
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key and value:
            os.environ.setdefault(key, value)


# ---------------------------------------------------------------------------
# Settings — one immutable object that knows the configuration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Settings:
    """All configuration in one place, read once at startup.

    In bot.py the configuration lived in module-level constants that were
    evaluated the moment you imported the file. Here it is a plain value:
    constructing it has no side effects beyond reading the environment, and
    every class that needs config receives the bits it cares about.
    """

    bot_token: str
    db_server: str
    db_user: str
    db_password: str
    db_name: str
    admin_user_id: int
    api_url: str
    store_ids: tuple[str, ...]

    @classmethod
    def load(cls) -> "Settings":
        """Build settings from the environment and the .env file beside the script."""
        _load_dotenv()

        token = os.environ.get("BARCODE_BOT_TOKEN", "")
        if not token:
            token_file = os.path.join(_script_dir(), "token.txt")
            if os.path.exists(token_file):
                with open(token_file) as f:
                    token = f.read().strip()
        if not token:
            raise RuntimeError("No bot token found. Set BARCODE_BOT_TOKEN (or put it in .env), or create token.txt")

        store_ids_env = os.environ.get("KIWISQUARE_STORE_IDS", "")
        store_ids = tuple(s.strip() for s in store_ids_env.split(",") if s.strip()) or _DEFAULT_STORE_IDS

        settings = cls(
            bot_token=token,
            db_server=os.environ.get("MSSQL_SERVER", ""),
            db_user=os.environ.get("MSSQL_USER", ""),
            db_password=os.environ.get("MSSQL_PASSWORD", ""),
            db_name=os.environ.get("MSSQL_DATABASE", "Barcodes"),
            admin_user_id=int(os.environ.get("BARCODE_BOT_ADMIN_ID", str(_DEFAULT_ADMIN_ID))),
            api_url=_DEFAULT_API_URL,
            store_ids=store_ids,
        )

        # Fail fast with a readable message instead of a login error mid-scan.
        missing = [
            name for name, value in (
                ("BARCODE_BOT_ADMIN_ID", settings.admin_user_id),
                ("KIWISQUARE_STORE_IDS", settings.store_ids),
                ("MSSQL_SERVER", settings.db_server),
                ("MSSQL_USER", settings.db_user),
                ("MSSQL_PASSWORD", settings.db_password),
            ) if not value
        ]
        if missing:
            raise RuntimeError(
                "Missing configuration: " + ", ".join(missing)
                + " -- put them in .env next to barcode-bot.py or export them in the environment"
            )
        return settings


# ---------------------------------------------------------------------------
# Data classes — the dicts from bot.py, but with names and types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Barcode:
    """One decoded barcode: its symbology and payload."""

    type: str
    data: str


@dataclass(frozen=True)
class Product:
    """A product stored in the local dbo.Item table."""

    item: str
    brand: str
    category: str
    item_image: bytes | None


@dataclass(frozen=True)
class StoreOffer:
    """One store's price for a product, as reported by the price API."""

    title: str
    brand: str
    store: str
    price: float | None
    unit_price: float | None
    unit_measure: str
    img_url: str
    url: str
    is_discounted: bool
    original_price: float | None
    is_active: bool
    category: str


@dataclass(frozen=True)
class PriceLookup:
    """A parsed API response: the offers plus the raw JSON for storage."""

    offers: tuple[StoreOffer, ...]
    raw_json: str | None

    def active_offer(self) -> StoreOffer | None:
        """The first offer the API flags as active, if any."""
        return next((offer for offer in self.offers if offer.is_active), None)


# Outcome actions, so ScanService can report what it did without doing I/O.
ACTION_NONE = "none"
ACTION_INSERTED = "inserted"
ACTION_INSERT_FAILED = "insert-failed"
ACTION_UPDATED = "updated"
ACTION_UPDATE_FAILED = "update-failed"


@dataclass
class ScanResult:
    """Everything the flow needs after looking up one barcode."""

    barcode: Barcode
    product: Product | None
    lookup: PriceLookup
    action: str = ACTION_NONE
    saved_offer: StoreOffer | None = None
    image_task: tuple[str, str] | None = None  # (barcode, img_url) to backfill

# ---------------------------------------------------------------------------
# AccessControl — the allowlist, with the globals replaced by state
# ---------------------------------------------------------------------------


class AccessControl:
    """The file-backed allowlist of Telegram user IDs.

    bot.py kept the allowlist in a module-level global set that three helpers
    mutated from anywhere. All of that state and every rule about it (the
    admin is always authorised, the admin is never written to the file) now
    lives behind six small methods, and the file format is an implementation
    detail nothing else needs to know about.
    """

    def __init__(self, admin_user_id: int, path: str) -> None:
        self._admin = admin_user_id
        self._path = path
        self._users: set[int] = set()
        self.load()

    def load(self) -> None:
        """Load authorised IDs from file; the admin is always included."""
        if os.path.exists(self._path):
            with open(self._path) as f:
                self._users = {int(line) for line in f if line.strip().isdigit()}
        self._users.add(self._admin)
        logger.info("Authorized users: %s", sorted(self._users))

    def is_authorized(self, user_id: int | None) -> bool:
        return user_id is not None and user_id in self._users

    def is_admin(self, user_id: int | None) -> bool:
        return user_id == self._admin

    def members(self) -> list[int]:
        """Sorted snapshot of every authorised ID, admin included."""
        return sorted(self._users)

    def add(self, user_id: int) -> None:
        self._users.add(user_id)
        self._save()

    def remove(self, user_id: int) -> None:
        if user_id != self._admin:
            self._users.discard(user_id)
            self._save()

    def _save(self) -> None:
        """Persist the allowlist (admin is implicit, never stored)."""
        with open(self._path, "w") as f:
            for uid in sorted(self._users):
                if uid != self._admin:
                    f.write(f"{uid}\n")

# ---------------------------------------------------------------------------
# ProductRepository — one door to SQL Server
# ---------------------------------------------------------------------------


class ProductRepository:
    """Every SQL Server query the bot performs, in one class.

    bot.py had four functions that each opened a connection, each wrapped the
    work in its own try/except-close dance, and each duplicated the config
    dict. Here the connection lifecycle lives in one context manager, the
    config is built once in the constructor, and the four operations are
    methods you can find in an instant.
    """

    _TDS_VERSION = "7.0"
    _QUERY_TIMEOUT = 10

    def __init__(self, settings: Settings) -> None:
        self._config = {
            "server": settings.db_server,
            "user": settings.db_user,
            "password": settings.db_password,
            "database": settings.db_name,
            "tds_version": self._TDS_VERSION,
            "timeout": self._QUERY_TIMEOUT,
        }

    @contextmanager
    def _connect(self) -> Iterator:
        """Open a connection; always close it, commit on the way out."""
        conn = pymssql.connect(**self._config)
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def find(self, barcode: str) -> Product | None:
        """Look up a barcode in dbo.Item; None when absent or on error."""
        try:
            with self._connect() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT Item, Brand, Category, ItemImage FROM dbo.Item WHERE EAN13 = %s",
                    (barcode,),
                )
                row = cursor.fetchone()
            if row is None:
                return None
            return Product(
                item=row[0] or "",
                brand=row[1] or "",
                category=row[2] or "",
                item_image=row[3],  # VARBINARY(MAX) or None
            )
        except Exception as e:
            logger.error("DB lookup error for barcode %s: %s", barcode, e)
            return None

    def insert(self, barcode: str, category: str, item: str, brand: str, raw_json: str) -> bool:
        """Insert a new product row; True on success."""
        try:
            with self._connect() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "INSERT INTO dbo.Item (EAN13, Category, Item, Brand, Src, RawJSON)"
                    " VALUES (%s, %s, %s, %s, %s, %s)",
                    (barcode, category, item, brand, "KiwiSquare", raw_json),
                )
            logger.info("Inserted new product: barcode=%s, item=%s", barcode, item)
            return True
        except Exception as e:
            logger.error("DB insert error for barcode %s: %s", barcode, e)
            return False

    def update_raw_json(self, barcode: str, raw_json: str) -> bool:
        """Refresh RawJSON and updatedOnTime for an existing row."""
        try:
            with self._connect() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "UPDATE dbo.Item SET RawJSON = %s, updatedOnTime = GETDATE() WHERE EAN13 = %s",
                    (raw_json, barcode),
                )
            logger.info("Updated product: barcode=%s", barcode)
            return True
        except Exception as e:
            logger.error("DB update error for barcode %s: %s", barcode, e)
            return False

    def store_image(self, barcode: str, image_bytes: bytes) -> bool:
        """Store image bytes into [ItemImage] for an existing product."""
        try:
            with self._connect() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "UPDATE dbo.Item SET ItemImage = %s, updatedOnTime = GETDATE() WHERE EAN13 = %s",
                    (image_bytes, barcode),
                )
            logger.info("Stored image for barcode %s (%d bytes)", barcode, len(image_bytes))
            return True
        except Exception as e:
            logger.error("DB image update error for barcode %s: %s", barcode, e)
            return False

# ---------------------------------------------------------------------------
# KiwiSquareClient — the external price API
# ---------------------------------------------------------------------------


class KiwiSquareClient:
    """The external price API: barcode in, per-store offers out.

    bot.py imported urllib, urllib.parse and json *inside* the lookup
    function and hardcoded the store IDs next to it. Here the URL, the store
    IDs and the timeout arrive through the constructor, and the parsing has
    its own method, so swapping or mocking the whole client is trivial.
    """

    def __init__(self, api_url: str, store_ids: tuple[str, ...], timeout: float = 15.0) -> None:
        self._api_url = api_url
        self._store_ids = store_ids
        self._timeout = timeout

    def lookup(self, barcode: str) -> PriceLookup:
        """Query the API for one barcode across all configured stores."""
        params = [("Barcode", barcode)]
        params += [("StoreIds", store_id) for store_id in self._store_ids]
        url = self._api_url + "?" + urllib.parse.urlencode(params)

        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                raw_text = resp.read().decode("utf-8")
                data = json.loads(raw_text)
        except Exception as e:
            logger.error("KiwiSquare API error for barcode %s: %s", barcode, e)
            return PriceLookup(offers=(), raw_json=None)

        offers = tuple(self._parse(item) for item in data.get("Items", []))
        return PriceLookup(offers=offers, raw_json=raw_text)

    @staticmethod
    def _parse(item: dict) -> StoreOffer:
        """Map one raw API item onto a StoreOffer."""
        categories = item.get("Categories") or []
        return StoreOffer(
            title=item.get("Title") or "",
            brand=item.get("Brand") or "",
            store=item.get("StoreName") or "",
            price=item.get("Price"),
            unit_price=item.get("UnitPriceValue"),
            unit_measure=item.get("UnitMeasure") or "",
            img_url=item.get("ImgUrl") or "",
            url=item.get("Url") or "",
            is_discounted=item.get("IsDiscounted", False),
            original_price=item.get("OriginalPrice"),
            is_active=item.get("IsActive", False),
            category=categories[0] if categories else "",
        )

# ---------------------------------------------------------------------------
# BarcodeDecoder — a pipeline of preprocessing strategies
# ---------------------------------------------------------------------------


class PreprocessStrategy(ABC):
    """One named way to transform an image before trying to decode it."""

    name: str

    @abstractmethod
    def apply(self, image: Image.Image) -> Image.Image:
        """Return the transformed image (the input is left untouched)."""


@dataclass(frozen=True)
class RawImage(PreprocessStrategy):
    name: str = "raw"

    def apply(self, image: Image.Image) -> Image.Image:
        return image


@dataclass(frozen=True)
class Grayscale(PreprocessStrategy):
    name: str = "grayscale"

    def apply(self, image: Image.Image) -> Image.Image:
        return image.convert("L")


@dataclass(frozen=True)
class Upscale(PreprocessStrategy):
    name: str = "upscale-x2"

    def apply(self, image: Image.Image) -> Image.Image:
        width, height = image.size
        return image.resize((width * 2, height * 2), Image.LANCZOS)


@dataclass(frozen=True)
class GrayscaleUpscale(PreprocessStrategy):
    name: str = "grayscale+upscale"

    def apply(self, image: Image.Image) -> Image.Image:
        width, height = image.size
        return image.convert("L").resize((width * 2, height * 2), Image.LANCZOS)


@dataclass(frozen=True)
class Threshold(PreprocessStrategy):
    name: str = "threshold-128"

    def apply(self, image: Image.Image) -> Image.Image:
        gray = image.convert("L")
        return gray.point(lambda x: 0 if x < 128 else 255, "L")


class BarcodeDecoder:
    """Decodes barcodes, trying increasingly aggressive preprocessing.

    bot.py spelled the five attempts out as one inline cascade. Here the
    attempts are first-class strategy objects held in a list: adding a sixth
    (say, adaptive thresholding) means appending one small class, and the log
    now tells you which strategy actually won.
    """

    def __init__(self, strategies: Sequence[PreprocessStrategy] | None = None) -> None:
        self._strategies = list(strategies) if strategies is not None else [
            RawImage(),
            Grayscale(),
            Upscale(),
            GrayscaleUpscale(),
            Threshold(),
        ]

    def decode(self, image: Image.Image) -> list[Barcode]:
        """Return every barcode found, or an empty list if none survive."""
        for strategy in self._strategies:
            prepared = strategy.apply(image)
            barcodes = self._zbar(prepared)
            if barcodes:
                logger.info("Decoded with strategy '%s'", strategy.name)
                return barcodes
        return []

    @staticmethod
    def _zbar(image: Image.Image) -> list[Barcode]:
        if image.mode not in ("L", "RGB"):
            image = image.convert("RGB")
        return [
            Barcode(type=r.type, data=r.data.decode("utf-8", errors="replace"))
            for r in zbar_decode(image)
        ]


# ---------------------------------------------------------------------------
# FileDownloader — Telegram file downloads with retries
# ---------------------------------------------------------------------------


class FileDownloader:
    """Downloads Telegram files with retries and generous timeouts.

    The home server can be slow reaching Telegram's file servers, so every
    download is retried on the transient network errors with a growing
    backoff, exactly as in bot.py (whose retry logic lived in a helper that
    imported httpx and asyncio mid-function).
    """

    def __init__(self, max_retries: int = 3) -> None:
        self._max_retries = max_retries

    async def download(self, bot, file_id: str) -> io.BytesIO:
        """Download a file into memory; retries on transient errors."""
        last_error: Exception | None = None
        for attempt in range(self._max_retries):
            try:
                file = await bot.get_file(file_id)
                buf = io.BytesIO()
                await file.download_to_memory(buf)
                buf.seek(0)
                return buf
            except (TimedOut, NetworkError, httpx.ReadTimeout, httpx.ConnectTimeout) as e:
                last_error = e
                logger.warning("Download attempt %d/%d failed: %s", attempt + 1, self._max_retries, e)
                if attempt < self._max_retries - 1:
                    await asyncio.sleep(2 * (attempt + 1))
        raise last_error  # type: ignore[misc]

# ---------------------------------------------------------------------------
# ReplyBuilder — pure formatting: data in, markdown text out
# ---------------------------------------------------------------------------


class ReplyBuilder:
    """Turns scan results and command state into Telegram markdown.

    None of these methods touch the network, the database or Telegram: they
    take data and return strings. That is what makes the reply format
    testable with plain asserts, no mocks required. The wording is kept
    byte-for-byte identical to bot.py, so users see no difference.
    """

    SEPARATOR = "━━━━━━━━━━━━━━"

    def scan_reply(self, results: list[ScanResult]) -> str:
        """The main reply for one scan: single-barcode or list layout."""
        if len(results) == 1:
            return self._single(results[0])
        return self._multi(results)

    def _single(self, r: ScanResult) -> str:
        barcode = r.barcode
        reply = f"✅ **Barcode found**\n\n📋 Type: `{barcode.type}`\n📝 Content: `{barcode.data}`"
        reply += "\n\n" + self.product_block(r.product)
        reply += "\n\n" + self.SEPARATOR
        reply += "\n\n" + self.pricing_block(r.lookup.offers)
        return reply

    def _multi(self, results: list[ScanResult]) -> str:
        lines = [f"✅ **{len(results)} barcodes found**\n"]
        for i, r in enumerate(results, 1):
            barcode = r.barcode
            lines.append(f"**{i}.** Type: `{barcode.type}`\n   Content: `{barcode.data}`")
            lines.append(self.product_block(r.product, indent="   "))
            lines.append(f"   {self.SEPARATOR}")
            lines.append(self.pricing_block(r.lookup.offers, indent="   "))
            lines.append("")
        return "\n".join(lines)

    @staticmethod
    def product_block(product: Product | None, indent: str = "") -> str:
        """Local DB product info, or the 'not found' line."""
        if product is None:
            return f"{indent}❓ Product not found in local database"
        lines = []
        if product.item:
            lines.append(f"{indent}🏷️ Product: {product.item}")
        if product.brand:
            lines.append(f"{indent}🏢 Brand: {product.brand}")
        if product.category:
            lines.append(f"{indent}📂 Category: {product.category}")
        return "\n".join(lines) if lines else ""

    @staticmethod
    def pricing_block(offers: tuple[StoreOffer, ...], indent: str = "") -> str:
        """Per-store prices, sorted by store name, store linked when possible."""
        if not offers:
            return f"{indent}Price not found."

        lines = []
        first = offers[0]
        if first.title:
            lines.append(f"{indent}{first.title}")

        for offer in sorted(offers, key=lambda o: o.store):
            if offer.url:
                store_label = f"[{offer.store}]({offer.url})"
            else:
                store_label = offer.store
            store_line = f"{indent}📍 {store_label}"
            if offer.price is not None:
                price_str = f"**${offer.price:.2f}**"
                if offer.is_discounted and offer.original_price and offer.original_price != offer.price:
                    price_str += f" (was ${offer.original_price:.2f})"
                store_line += f" — {price_str}"
                if offer.unit_price is not None and offer.unit_measure:
                    store_line += f"  [{offer.unit_price:.2f}/{offer.unit_measure}]"
            else:
                store_line += " — price not found"
            lines.append(store_line)

        return "\n".join(lines)

    # -- Database housekeeping messages -------------------------------------

    @staticmethod
    def saved_to_db(barcode: Barcode, offer: StoreOffer | None) -> str:
        text = f"📝 **Saved to local database**\n\n📋 Barcode: `{barcode.data}`"
        if offer:
            text += (
                f"\n🏷️ Product: {offer.title}"
                f"\n🏢 Brand: {offer.brand}"
                f"\n📂 Category: {offer.category}"
            )
        return text

    @staticmethod
    def save_failed(barcode: Barcode, offer: StoreOffer | None) -> str:
        title = offer.title if offer else "unknown"
        return (
            f"❌ **Failed to save to local database**\n\n"
            f"📋 Barcode: `{barcode.data}`\n"
            f"🏷️ Product: {title}\n"
            f"See bot logs for details."
        )

    @staticmethod
    def updated_db(barcode: Barcode) -> str:
        return (
            f"🔄 **Updated local database**\n\n"
            f"📋 Barcode: `{barcode.data}`\n"
            f"RawJSON and timestamp refreshed from KiwiSquare."
        )

    @staticmethod
    def update_failed(barcode: Barcode) -> str:
        return (
            f"❌ **Failed to update local database**\n\n"
            f"📋 Barcode: `{barcode.data}`\n"
            f"See bot logs for details."
        )

    @staticmethod
    def image_saved(barcode: str, size_bytes: int) -> str:
        return (
            f"🖼️ **Image saved to database**\n\n"
            f"📋 Barcode: `{barcode}`\n"
            f"📦 {size_bytes:,} bytes stored in [ItemImage]"
        )

    # -- Command messages ----------------------------------------------------

    @staticmethod
    def welcome() -> str:
        return (
            "👋 Welcome to the Barcode Scanner bot!\n\n"
            "📸 Send me a photo with a barcode to decode it.\n"
            "🔢 Or send me a 13-digit EAN-13 code as text.\n\n"
            "Commands:\n"
            "/debug on|off — toggle debug mode"
        )

    @staticmethod
    def lock_screen() -> str:
        return (
            "🔒 This bot is private. You are not authorized.\n\n"
            "Contact the bot admin to request access."
        )

    @staticmethod
    def text_hint() -> str:
        return (
            "📸 Send me a photo with a barcode and I'll decode it for you!\n"
            "Supports: QR codes, EAN-13, EAN-8, UPC-A, Code-128, and more."
        )

    @staticmethod
    def subs_list(user_ids: list[int], admin_user_id: int) -> str:
        lines = ["📋 **Authorized users**\n"]
        for uid in user_ids:
            tag = " (admin)" if uid == admin_user_id else ""
            lines.append(f"• `{uid}`{tag}")
        return "\n".join(lines)

    @staticmethod
    def debug_state(debug: bool) -> str:
        state = "Debug 🔍" if debug else "Normal ✅"
        detail = (
            "Verbose — sends message for every DB update." if debug
            else "Quiet — only sends message for new products and errors."
        )
        return f"Mode: {state}\n\n" + detail

    @staticmethod
    def debug_usage(current: bool) -> str:
        state = "Debug 🔍" if current else "Normal ✅"
        return (
            "Usage: `/debug on` or `/debug off`\n"
            f"Current mode: {state}"
        )

# ---------------------------------------------------------------------------
# ScanService — one barcode in, a ScanResult out
# ---------------------------------------------------------------------------


class ScanService:
    """Looks a barcode up locally and remotely, and keeps the local copy fresh.

    This is the decision logic that bot.py buried inside its 180-line
    _process_barcodes and then duplicated for the single- and multi-barcode
    paths, expressed once as data:

        not in DB + API hit   -> insert a new row (image backfill queued)
        in DB + API response  -> refresh RawJSON and the timestamp
        otherwise             -> nothing to store

    The service returns a ScanResult; the caller decides what to say and when.
    """

    def __init__(self, repository: ProductRepository, prices: KiwiSquareClient) -> None:
        self._repository = repository
        self._prices = prices

    def perform(self, barcode: Barcode) -> ScanResult:
        """Look one barcode up in the DB and the price API, and reconcile."""
        product = self._repository.find(barcode.data)
        lookup = self._prices.lookup(barcode.data)
        result = ScanResult(barcode=barcode, product=product, lookup=lookup)
        active = lookup.active_offer()

        if product is None and lookup.offers and lookup.raw_json and active:
            result.saved_offer = active
            if self._repository.insert(barcode.data, active.category, active.title, active.brand, lookup.raw_json):
                result.action = ACTION_INSERTED
                if active.img_url:
                    result.image_task = (barcode.data, active.img_url)
            else:
                result.action = ACTION_INSERT_FAILED
        elif product is not None and lookup.raw_json:
            if self._repository.update_raw_json(barcode.data, lookup.raw_json):
                result.action = ACTION_UPDATED
            else:
                result.action = ACTION_UPDATE_FAILED
            # Backfill the image only when the DB row has none yet.
            if active and active.img_url and product.item_image is None:
                result.image_task = (barcode.data, active.img_url)
        return result

    def backfill_image(self, barcode: str, img_url: str) -> int:
        """Download a product image and store it; returns stored size, 0 on failure."""
        data = self._fetch_image(img_url)
        if not data:
            return 0
        if self._repository.store_image(barcode, data):
            return len(data)
        return 0

    @staticmethod
    def _fetch_image(img_url: str) -> bytes | None:
        """Download an image from a URL; None on failure or empty URL."""
        if not img_url:
            return None
        try:
            req = urllib.request.Request(img_url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=15) as resp:
                return resp.read()
        except Exception as e:
            logger.error("Image download error from %s: %s", img_url, e)
            return None

# ---------------------------------------------------------------------------
# BarcodeBot — the Telegram façade
# ---------------------------------------------------------------------------


class BarcodeBot:
    """Thin Telegram handlers that delegate to the services.

    Every handler follows the same three beats: check the allowlist, call a
    service, format a reply. None of them knows how the database is reached,
    how the API is called or how barcodes are decoded, because all of that
    arrived through the constructor. The debug flag from bot.py's global
    DEBUG_MODE is now plain instance state that /debug flips.
    """

    def __init__(
        self,
        settings: Settings,
        access: AccessControl,
        scans: ScanService,
        decoder: BarcodeDecoder,
        downloader: FileDownloader,
        replies: ReplyBuilder,
    ) -> None:
        self._settings = settings
        self._access = access
        self._scans = scans
        self._decoder = decoder
        self._downloader = downloader
        self._replies = replies
        self._debug = False

    def register(self, app: Application) -> None:
        """Attach every handler to the Application."""
        app.add_handler(CommandHandler("start", self.cmd_start))
        app.add_handler(CommandHandler("debug", self.cmd_debug))
        app.add_handler(CommandHandler("addsub", self.cmd_addsub))
        app.add_handler(CommandHandler("delsub", self.cmd_delsub))
        app.add_handler(CommandHandler("subs", self.cmd_subs))
        # Photos (compressed) and image documents (uncompressed).
        app.add_handler(MessageHandler(filters.PHOTO, self.handle_photo))
        app.add_handler(MessageHandler(filters.Document.IMAGE, self.handle_document))
        # Text — extract EAN-13 or show the hint.
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_text))

    def _gate(self, update: Update) -> bool:
        """True when the sender is authorised; strangers are ignored."""
        user = update.effective_user
        return bool(user and self._access.is_authorized(user.id))

    async def cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle /start: a welcome for friends, a lock screen for strangers."""
        user_id = update.effective_user.id
        if self._access.is_authorized(user_id):
            await update.effective_message.reply_text(self._replies.welcome())
        else:
            await update.effective_message.reply_text(self._replies.lock_screen())
            logger.info(
                "Unauthorized /start from user %s (%s)",
                user_id,
                update.effective_user.username or "no username",
            )

    async def cmd_debug(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Toggle debug mode: /debug, /debug on, /debug off."""
        args = context.args
        if args:
            arg = args[0].lower()
            if arg in ("on", "true", "1", "yes"):
                self._debug = True
            elif arg in ("off", "false", "0", "no"):
                self._debug = False
            else:
                await update.effective_message.reply_text(
                    self._replies.debug_usage(self._debug), parse_mode="Markdown"
                )
                return
        else:
            self._debug = not self._debug
        await update.effective_message.reply_text(self._replies.debug_state(self._debug))
        logger.info("Debug mode toggled to %s", self._debug)

    async def cmd_addsub(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Add an authorised user. Admin only, silently ignored otherwise."""
        if not self._access.is_admin(update.effective_user.id):
            return
        args = context.args
        if not args:
            await update.effective_message.reply_text(
                "Usage: `/addsub <user_id>`\n"
                "Ask the user to send /start to the bot, then check bot logs for their user ID.",
                parse_mode="Markdown",
            )
            return
        try:
            new_uid = int(args[0])
        except ValueError:
            await update.effective_message.reply_text("❌ User ID must be a number.")
            return
        if new_uid in self._access.members():
            await update.effective_message.reply_text(
                f"ℹ️ User `{new_uid}` is already authorized.", parse_mode="Markdown"
            )
            return
        self._access.add(new_uid)
        await update.effective_message.reply_text(
            f"✅ User `{new_uid}` added to authorized users.", parse_mode="Markdown"
        )
        logger.info("Added authorized user: %s", new_uid)

    async def cmd_delsub(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Remove an authorised user. Admin only, silently ignored otherwise."""
        if not self._access.is_admin(update.effective_user.id):
            return
        args = context.args
        if not args:
            await update.effective_message.reply_text("Usage: `/delsub <user_id>`", parse_mode="Markdown")
            return
        try:
            target_uid = int(args[0])
        except ValueError:
            await update.effective_message.reply_text("❌ User ID must be a number.")
            return
        if self._access.is_admin(target_uid):
            await update.effective_message.reply_text("❌ Cannot remove admin.")
            return
        if target_uid not in self._access.members():
            await update.effective_message.reply_text(
                f"ℹ️ User `{target_uid}` is not authorized.", parse_mode="Markdown"
            )
            return
        self._access.remove(target_uid)
        await update.effective_message.reply_text(
            f"✅ User `{target_uid}` removed from authorized users.", parse_mode="Markdown"
        )
        logger.info("Removed authorized user: %s", target_uid)

    async def cmd_subs(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """List every authorised user. Admin only, silently ignored otherwise."""
        if not self._access.is_admin(update.effective_user.id):
            return
        await update.effective_message.reply_text(
            self._replies.subs_list(self._access.members(), self._settings.admin_user_id),
            parse_mode="Markdown",
        )

    # -- Messages: photos, documents, text ------------------------------------

    async def handle_photo(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle compressed photos sent to the bot."""
        if not self._gate(update):
            return
        message = update.effective_message
        if not message or not message.photo:
            return

        photo = message.photo[-1]  # the highest-resolution copy
        status = await message.reply_text("📸 Photo received. Working on it…")
        try:
            buf = await self._downloader.download(context.bot, photo.file_id)
        except Exception as e:
            logger.error("Failed to download photo after retries: %s", e)
            await status.edit_text(
                "⚠️ Couldn't download the photo. Try again — the Pi can be slow reaching Telegram's servers."
            )
            return

        await status.edit_text("📥 Photo downloaded. Decoding barcodes…")
        await self._scan_image(
            buf,
            message,
            status,
            open_error="⚠️ Couldn't read that image. Try sending a clearer photo.",
            decode_error="⚠️ Something went wrong while decoding. Try again with a clearer photo.",
            no_result="🔍 No barcodes found in this photo.\n"
                      "Tips: make sure the barcode is well-lit, centered, and fills most of the frame.",
        )

    async def handle_document(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle uncompressed photos sent as image documents."""
        if not self._gate(update):
            return
        message = update.effective_message
        if not message or not message.document:
            return

        doc = message.document
        if not doc.mime_type or not doc.mime_type.startswith("image/"):
            return

        try:
            buf = await self._downloader.download(context.bot, doc.file_id)
        except Exception as e:
            logger.error("Failed to download document after retries: %s", e)
            await message.reply_text("⚠️ Couldn't download the file. Try again.")
            return

        status = await message.reply_text("📥 File downloaded. Decoding barcodes…")
        await self._scan_image(
            buf,
            message,
            status,
            open_error="⚠️ Couldn't read that image file.",
            decode_error="⚠️ Something went wrong while decoding.",
            no_result="🔍 No barcodes found. Try a clearer, better-lit photo.",
        )

    async def _scan_image(
        self,
        buf: io.BytesIO,
        message,
        status,
        *,
        open_error: str,
        decode_error: str,
        no_result: str,
    ) -> None:
        """The shared tail of both image paths: open, decode, process.

        bot.py had this pipeline duplicated in handle_photo and
        handle_document with only the error wording different; now the two
        handlers pass their wording in and share everything else.
        """
        try:
            image = Image.open(buf)
        except Exception as e:
            logger.error("Failed to open image: %s", e)
            await status.edit_text(open_error)
            return

        try:
            barcodes = self._decoder.decode(image)
        except Exception as e:
            logger.error("Decode error: %s", e)
            await status.edit_text(decode_error)
            return

        if not barcodes:
            await status.edit_text(no_result)
            return

        await self._process(barcodes, message, status)

    async def handle_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle text: extract 13-digit EAN codes, else show the hint."""
        if not self._gate(update):
            return
        message = update.effective_message
        if not message or not message.text:
            return

        matches = re.findall(r"\d{13}", message.text.strip())
        if not matches:
            await message.reply_text(self._replies.text_hint())
            return

        barcodes: list[Barcode] = []
        seen: set[str] = set()
        for match in matches:
            if match not in seen:
                seen.add(match)
                barcodes.append(Barcode(type="EAN13", data=match))

        status = await message.reply_text(f"🔢 Found {len(barcodes)} barcode(s) in text. Looking up…")
        await self._process(barcodes, message, status)

    # -- The scan flow ---------------------------------------------------------

    async def _process(self, barcodes: list[Barcode], message, status) -> None:
        """Look every barcode up, answer, then quietly backfill images.

        This is bot.py's 180-line _process_barcodes with the single- and
        multi-barcode branches collapsed into one path: the same ScanService
        call per barcode, one formatting method, and the Telegram side
        effects in a fixed order.
        """
        if not barcodes:
            await status.edit_text(
                "🔍 No barcodes found.\n"
                "Tips: make sure the barcode is well-lit, centered, and fills most of the frame."
            )
            return

        await status.edit_text("🔎 Looking up products…")
        results = [self._scans.perform(barcode) for barcode in barcodes]

        for r in results:
            await self._announce(r, message)

        # Product images already in the DB go out before the main reply.
        for r in results:
            if r.product and r.product.item_image:
                await self._send_product_image(message, r.product.item or "", r.product.item_image)

        reply = self._replies.scan_reply(results)
        await status.edit_text(reply, parse_mode="Markdown", disable_web_page_preview=True)

        # Image backfill runs after the reply so it never delays the answer.
        for r in results:
            if r.image_task is None:
                continue
            barcode_data, img_url = r.image_task
            stored = self._scans.backfill_image(barcode_data, img_url)
            if stored and self._debug:
                await message.reply_text(self._replies.image_saved(barcode_data, stored))

    async def _announce(self, r: ScanResult, message) -> None:
        """Send the per-barcode DB housekeeping messages, if any."""
        if r.action == ACTION_INSERTED:
            await message.reply_text(self._replies.saved_to_db(r.barcode, r.saved_offer))
        elif r.action == ACTION_INSERT_FAILED:
            await message.reply_text(self._replies.save_failed(r.barcode, r.saved_offer))
        elif r.action == ACTION_UPDATED and self._debug:
            await message.reply_text(self._replies.updated_db(r.barcode))
        elif r.action == ACTION_UPDATE_FAILED:
            await message.reply_text(self._replies.update_failed(r.barcode))

    @staticmethod
    async def _send_product_image(message, caption: str, image_bytes: bytes) -> None:
        try:
            await message.reply_photo(
                photo=io.BytesIO(image_bytes),
                caption=caption[:1024] if caption else None,
            )
        except Exception as e:
            logger.error("Failed to send product image: %s", e)


# ---------------------------------------------------------------------------
# Wiring — the composition root
# ---------------------------------------------------------------------------


def configure_logging() -> None:
    """Same logging set-up as bot.py, but called explicitly from main()."""
    logging.basicConfig(
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        level=logging.INFO,
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("telegram").setLevel(logging.INFO)


async def post_init(app: Application) -> None:
    """Called after Application.init, before polling starts."""
    me = await app.bot.get_me()
    logger.info("Barcode Scanner bot started: @%s (%s)", me.username, me.id)


def main() -> None:
    """Build the object graph, then hand control to python-telegram-bot."""
    configure_logging()
    logger.info("Starting Barcode Scanner bot...")

    settings = Settings.load()
    access = AccessControl(settings.admin_user_id, os.path.join(_script_dir(), "authorized_users.txt"))
    scans = ScanService(
        repository=ProductRepository(settings),
        prices=KiwiSquareClient(settings.api_url, settings.store_ids),
    )
    bot = BarcodeBot(
        settings=settings,
        access=access,
        scans=scans,
        decoder=BarcodeDecoder(),
        downloader=FileDownloader(max_retries=3),
        replies=ReplyBuilder(),
    )

    # Generous timeouts for the home server — file downloads can be slow.
    request = HTTPXRequest(connect_timeout=30, read_timeout=60, write_timeout=30, pool_timeout=30)
    get_updates_request = HTTPXRequest(connect_timeout=30, read_timeout=60, write_timeout=30, pool_timeout=30)

    app = (
        Application.builder()
        .token(settings.bot_token)
        .request(request)
        .get_updates_request(get_updates_request)
        .post_init(post_init)
        .build()
    )
    bot.register(app)

    logger.info("Bot is running. Send a photo with a barcode to decode.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
