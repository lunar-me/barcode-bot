#!/usr/bin/env python3
"""Parity + behaviour smoke test for bot_oop.py (no network, no DB, no Telegram).

Cross-checks the refactored classes against the original bot.py's pure
formatting functions, and unit-tests the decision logic with fakes.
"""

import asyncio
import os
import pathlib
import re
import subprocess
import sys
import tempfile
from types import SimpleNamespace

from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

# bot.py raises at import when no token is set; give it one.
os.environ.setdefault("BARCODE_BOT_TOKEN", "test-token-for-import")


def _stub_pyzbar() -> None:
    """pyzbar needs the VC++ 2013 runtime (msvcr120.dll), absent on this box.

    Nothing below decodes a real image, so a stub is enough; the real decode
    path is exercised on the home server. Everything else (formatting parity,
    decision logic, handlers) runs for real.
    """
    import types

    pyzbar = types.ModuleType("pyzbar")
    pyzbar_py = types.ModuleType("pyzbar.pyzbar")
    pyzbar_py.decode = lambda image: []
    pyzbar.pyzbar = pyzbar_py
    sys.modules["pyzbar"] = pyzbar
    sys.modules["pyzbar.pyzbar"] = pyzbar_py


_stub_pyzbar()

import bot  # noqa: E402  (original module, used for byte-parity checks)
import bot_oop  # noqa: E402
from bot_oop import (  # noqa: E402
    ACTION_INSERTED,
    ACTION_INSERT_FAILED,
    ACTION_NONE,
    ACTION_UPDATED,
    ACTION_UPDATE_FAILED,
    AccessControl,
    Barcode,
    BarcodeDecoder,
    FileDownloader,
    KiwiSquareClient,
    PriceLookup,
    Product,
    ReplyBuilder,
    ScanResult,
    ScanService,
    Settings,
    StoreOffer,
)

failures = []


def check(name, got, want):
    if got != want:
        failures.append(name)
        print(f"FAIL {name}\n  got : {got!r}\n  want: {want!r}")
    else:
        print(f"ok   {name}")


# ---------------------------------------------------------------------------
# Shared fixture: one API response, parsed by both the old dict pipeline and
# the new dataclass pipeline, then formatted by both formatting layers.
# ---------------------------------------------------------------------------

API_ITEMS = [
    {
        "Title": "Pams Butter 500g",
        "Brand": "Pams",
        "StoreName": "New World",
        "Price": 7.99,
        "UnitPriceValue": 15.98,
        "UnitMeasure": "kg",
        "ImgUrl": "https://example.com/img.jpg",
        "Url": "https://example.com/p",
        "IsDiscounted": True,
        "OriginalPrice": 9.49,
        "IsActive": True,
        "Categories": ["Chilled & Dairy"],
    },
    {
        "Title": "Pams Butter 500g",
        "Brand": "Pams",
        "StoreName": "Countdown",
        "Price": None,
        "UnitPriceValue": None,
        "UnitMeasure": "",
        "ImgUrl": "",
        "Url": "https://example.com/c",
        "IsDiscounted": False,
        "OriginalPrice": None,
        "IsActive": False,
        "Categories": [],
    },
    {
        "Title": "Pams Butter 500g",
        "Brand": "Pams",
        "StoreName": "Pak'nSave",
        "Price": 6.49,
        "UnitPriceValue": 12.98,
        "UnitMeasure": "kg",
        "ImgUrl": "",
        "Url": "",
        "IsDiscounted": False,
        "OriginalPrice": None,
        "IsActive": False,
        "Categories": ["Chilled & Dairy"],
    },
]

# What bot.py's lookup_kiwisquare would produce for the same API payload.
BOT_KIWI_ITEMS = [
    {
        "title": i.get("Title") or "",
        "brand": i.get("Brand") or "",
        "store": i.get("StoreName") or "",
        "price": i.get("Price"),
        "unit_price": i.get("UnitPriceValue"),
        "unit_measure": i.get("UnitMeasure") or "",
        "img_url": i.get("ImgUrl") or "",
        "url": i.get("Url") or "",
        "is_discounted": i.get("IsDiscounted", False),
        "original_price": i.get("OriginalPrice"),
        "is_active": i.get("IsActive", False),
        "category": (i.get("Categories") or [""])[0],
    }
    for i in API_ITEMS
]

OFFERS = tuple(KiwiSquareClient._parse(i) for i in API_ITEMS)
OFFER_ACTIVE = OFFERS[0]  # the IsActive=True one
BARCODE = Barcode("EAN13", "4006381333931")

# ---------------------------------------------------------------------------
# 1. Byte-level parity with bot.py's pure formatting functions
# ---------------------------------------------------------------------------

rb = ReplyBuilder()

check("parse parity (offers == bot dict fields)",
      OFFERS, tuple(StoreOffer(**d) for d in BOT_KIWI_ITEMS))

check("pricing parity (no indent)",
      rb.pricing_block(OFFERS), bot._format_pricing(BOT_KIWI_ITEMS))
check("pricing parity (indented)",
      rb.pricing_block(OFFERS, indent="   "), bot._format_pricing(BOT_KIWI_ITEMS, indent="   "))
check("pricing parity (empty)",
      rb.pricing_block(()), bot._format_pricing([]))

PRODUCT_DICT = {"item": "Pams Butter 500g", "brand": "Pams", "category": "Chilled & Dairy"}
PRODUCT = Product(item="Pams Butter 500g", brand="Pams", category="Chilled & Dairy", item_image=None)

check("product block parity (no indent)",
      rb.product_block(PRODUCT), bot._format_product(PRODUCT_DICT))
check("product block parity (indented)",
      rb.product_block(PRODUCT, indent="   "), bot._format_product(PRODUCT_DICT, indent="   "))
check("product not found parity",
      rb.product_block(None, indent="   "), bot._format_product_not_found(indent="   "))

check("active offer parity",
      PriceLookup(offers=OFFERS, raw_json=None).active_offer(),
      OFFER_ACTIVE)
check("active offer picks first IsActive item",
      PriceLookup(offers=OFFERS, raw_json=None).active_offer().store,
      bot._pick_active_kiwisquare_item(BOT_KIWI_ITEMS)["store"])
check("active offer empty -> None",
      PriceLookup(offers=(), raw_json=None).active_offer(), None)

# The separator bot.py inlines twice must match ReplyBuilder.SEPARATOR.
BOT_SRC = open(os.path.join(HERE, "bot.py"), encoding="utf-8").read()
RUNS = sorted({len(r) for r in re.findall(r"━+", BOT_SRC)})
check("separator glyph run lengths match bot.py",
      {len(ReplyBuilder.SEPARATOR)}, set(RUNS))

# ---------------------------------------------------------------------------
# 2. Full reply texts against bot.py's inline composition
# ---------------------------------------------------------------------------

SINGLE_EXPECTED = (
    "✅ **Barcode found**\n\n"
    "📋 Type: `EAN13`\n"
    "📝 Content: `4006381333931`"
    "\n\n" + bot._format_product(PRODUCT_DICT)
    + "\n\n" + ReplyBuilder.SEPARATOR
    + "\n\n" + bot._format_pricing(BOT_KIWI_ITEMS)
)
RESULT_SINGLE = ScanResult(
    barcode=BARCODE, product=PRODUCT,
    lookup=PriceLookup(offers=OFFERS, raw_json=None),
)
check("single scan reply parity", rb.scan_reply([RESULT_SINGLE]), SINGLE_EXPECTED)

RESULT_UNFOUND = ScanResult(
    barcode=Barcode("QRCODE", "https://example.com/xyz"),
    product=None,
    lookup=PriceLookup(offers=(), raw_json=None),
)
MULTI_EXPECTED = "\n".join([
    "✅ **2 barcodes found**\n",
    "**1.** Type: `EAN13`\n   Content: `4006381333931`",
    bot._format_product(PRODUCT_DICT, indent="   "),
    "   " + ReplyBuilder.SEPARATOR,
    bot._format_pricing(BOT_KIWI_ITEMS, indent="   "),
    "",
    "**2.** Type: `QRCODE`\n   Content: `https://example.com/xyz`",
    bot._format_product_not_found(indent="   "),
    "   " + ReplyBuilder.SEPARATOR,
    bot._format_pricing([], indent="   "),
    "",
])
check("multi scan reply parity", rb.scan_reply([RESULT_SINGLE, RESULT_UNFOUND]), MULTI_EXPECTED)

# ---------------------------------------------------------------------------
# 3. ScanService decision table (fakes for repo + price client)
# ---------------------------------------------------------------------------


class FakeRepo:
    def __init__(self, product=None, insert_ok=True, update_ok=True, store_ok=True):
        self.product = product
        self.insert_ok = insert_ok
        self.update_ok = update_ok
        self.store_ok = store_ok
        self.calls = []

    def find(self, barcode):
        self.calls.append("find")
        return self.product

    def insert(self, *args):
        self.calls.append("insert")
        return self.insert_ok

    def update_raw_json(self, *args):
        self.calls.append("update")
        return self.update_ok

    def store_image(self, *args):
        self.calls.append("store_image")
        return self.store_ok


class FakePrices:
    def __init__(self, result):
        self._result = result

    def lookup(self, barcode):
        return self._result


HIT = PriceLookup(offers=OFFERS, raw_json='{"Items": []}')
MISS = PriceLookup(offers=(), raw_json=None)

# a) insert path
repo = FakeRepo(product=None)
result = ScanService(repo, FakePrices(HIT)).perform(BARCODE)
check("insert: action", result.action, ACTION_INSERTED)
check("insert: saved offer", result.saved_offer, OFFER_ACTIVE)
check("insert: image task queued",
      result.image_task, (BARCODE.data, OFFER_ACTIVE.img_url))
check("insert: db calls", repo.calls, ["find", "insert"])

# b) insert fails
result = ScanService(FakeRepo(insert_ok=False), FakePrices(HIT)).perform(BARCODE)
check("insert-fail: action", result.action, ACTION_INSERT_FAILED)
check("insert-fail: no image task", result.image_task, None)

# c) update path
repo = FakeRepo(product=PRODUCT)
result = ScanService(repo, FakePrices(HIT)).perform(BARCODE)
check("update: action", result.action, ACTION_UPDATED)
check("update: backfill queued (product has no image)",
      result.image_task, (BARCODE.data, OFFER_ACTIVE.img_url))

# d) update path, product already has an image -> no backfill
WITH_IMAGE = Product(item="Pams Butter 500g", brand="Pams", category="Chilled & Dairy", item_image=b"jpeg-bytes")
result = ScanService(FakeRepo(product=WITH_IMAGE), FakePrices(HIT)).perform(BARCODE)
check("update: no backfill when image exists", result.image_task, None)

# e) update fails
result = ScanService(FakeRepo(product=PRODUCT, update_ok=False), FakePrices(HIT)).perform(BARCODE)
check("update-fail: action", result.action, ACTION_UPDATE_FAILED)

# f) API error: no DB write at all
repo = FakeRepo(product=None)
result = ScanService(repo, FakePrices(MISS)).perform(BARCODE)
check("api-miss: no action", result.action, ACTION_NONE)
check("api-miss: only find ran", repo.calls, ["find"])

# g) backfill_image: real fetch via file:// URL, store + failure paths
with tempfile.TemporaryDirectory() as td:
    img_path = os.path.join(td, "product.jpg")
    payload = b"\xff\xd8\xff\xe0-fake-jpeg"
    with open(img_path, "wb") as f:
        f.write(payload)
    repo = FakeRepo()
    stored = ScanService(repo, FakePrices(None)).backfill_image("123", pathlib.Path(img_path).as_uri())
    check("backfill: stored bytes", stored, len(payload))
    check("backfill: store_image called", repo.calls, ["store_image"])
    repo.store_ok = False
    check("backfill: store failure -> 0",
          ScanService(repo, FakePrices(None)).backfill_image("123", pathlib.Path(img_path).as_uri()), 0)
check("backfill: fetch failure -> 0",
      ScanService(FakeRepo(), FakePrices(None)).backfill_image("123", "file:///nonexistent-file-xyz.jpg"), 0)

# ---------------------------------------------------------------------------
# 4. Settings + AccessControl
# ---------------------------------------------------------------------------

os.environ["BARCODE_BOT_TOKEN"] = "tok-from-test"
os.environ["MSSQL_SERVER"] = "SRV2\\SQLEXPRESS"
os.environ["KIWISQUARE_STORE_IDS"] = "aa,bb"
os.environ["BARCODE_BOT_ADMIN_ID"] = "42"
s = Settings.load()
check("settings from env",
      (s.bot_token, s.db_server, s.store_ids, s.admin_user_id),
      ("tok-from-test", "SRV2\\SQLEXPRESS", ("aa", "bb"), 42))

# AccessControl: admin implicit, persistence round-trip, admin never removed
with tempfile.TemporaryDirectory() as td:
    path = os.path.join(td, "authorized_users.txt")
    ac = AccessControl(999, path)
    check("access: admin implicit", ac.is_authorized(999), True)
    check("access: stranger", ac.is_authorized(1), False)
    ac.add(12345)
    check("access: member added", ac.is_authorized(12345), True)
    check("access: file persists members (not admin)",
          open(path, encoding="utf-8").read().split(), ["12345"])
    ac2 = AccessControl(999, path)  # fresh instance reloads from disk
    check("access: reload from file", ac2.is_authorized(12345), True)
    ac2.remove(12345)
    check("access: member removed", ac2.is_authorized(12345), False)
    ac2.remove(999)
    check("access: admin removal is a no-op", 999 in ac2.members(), True)
    check("access: subs sorted", ac2.subs(), [])

# ---------------------------------------------------------------------------
# 5. BarcodeBot: construction, registration, gating, command behaviour
# ---------------------------------------------------------------------------

from telegram.ext import CommandHandler, MessageHandler  # noqa: E402


class AppSpy:
    def __init__(self):
        self.handlers = []

    def add_handler(self, handler):
        self.handlers.append(handler)


with tempfile.TemporaryDirectory() as td:
    bot_obj = bot_oop.BarcodeBot(
        settings=s,
        access=AccessControl(999, os.path.join(td, "authorized_users.txt")),
        scans=SimpleNamespace(perform=lambda b: None, backfill_image=lambda *a: 0),
        decoder=BarcodeDecoder(),
        downloader=FileDownloader(),
        replies=rb,
    )

    spy = AppSpy()
    bot_obj.register(spy)
    check("register: handler count", len(spy.handlers), 8)
    check("register: 5 commands",
          sum(isinstance(h, CommandHandler) for h in spy.handlers), 5)
    check("register: 3 message handlers",
          sum(isinstance(h, MessageHandler) for h in spy.handlers), 3)


class FakeMessage:
    def __init__(self):
        self.sent = []

    async def reply_text(self, text, **kwargs):
        self.sent.append((text, kwargs))

    async def reply_photo(self, photo=None, caption=None, **kwargs):
        self.sent.append(("photo", caption))


class FakeStatus:
    def __init__(self):
        self.edits = []

    async def edit_text(self, text, **kwargs):
        self.edits.append((text, kwargs))


def run_cmd(method, user_id, args=(), debug=False):
    bot_obj._debug = debug
    msg = FakeMessage()
    upd = SimpleNamespace(
        effective_user=SimpleNamespace(id=user_id),
        effective_message=msg,
    )
    ctx = SimpleNamespace(args=list(args))
    asyncio.run(method(upd, ctx))
    return msg


# Non-admin cannot manage subscriptions, and gets no reply (same as bot.py).
with tempfile.TemporaryDirectory() as td:
    bot_obj._access = AccessControl(999, os.path.join(td, "authorized_users.txt"))
    msg = run_cmd(bot_obj.cmd_addsub, 12345)
    check("addsub: stranger ignored", msg.sent, [])
    # The admin is authorised implicitly, so members() always shows [999];
    # what must NOT happen is the stranger's ID appearing.
    check("addsub: stranger not stored", bot_obj._access.members(), [999])

    # Admin can add; the stored list is echoed back.
    run_cmd(bot_obj.cmd_addsub, 999, args=["777"])
    check("addsub: admin adds", bot_obj._access.members(), [777, 999])
    # The file must contain only the non-admin member.
    with open(os.path.join(td, "authorized_users.txt")) as f:
        check("addsub: file stores member only", f.read().split(), ["777"])

# /subs output must match bot.py's inline text.
admin_id = 999
subs = sorted([777, admin_id])
lines = ["📋 **Authorized users**\n"] + [
    f"• `{u}` (admin)" if u == admin_id else f"• `{u}`" for u in subs
]
check("subs text parity", rb.subs_list([777, 999], 999), "\n".join(lines))

# /debug toggles for anyone and echoes the state (bot.py has no admin gate here).
msg = run_cmd(bot_obj.cmd_debug, 12345)
check("debug: toggles for any user", bot_obj._debug, True)
check("debug: state text parity", msg.sent[0][0],
      "Mode: Debug 🔍\n\nVerbose — sends message for every DB update.")
msg = run_cmd(bot_obj.cmd_debug, 999, debug=True)
check("debug: explicit off", bot_obj._debug, False)
check("debug: state text parity (off)", msg.sent[0][0],
      "Mode: Normal ✅\n\nQuiet — only sends message for new products and errors.")

# ---------------------------------------------------------------------------
# 6. _process: message ordering and the no-barcode shortcut
# ---------------------------------------------------------------------------

ANNOUNCE = rb.saved_to_db(BARCODE, OFFER_ACTIVE)


def canned_scan(debug_announce=True):
    """debug_announce=True -> insert path (announce); False -> update path (quiet)."""
    action = ACTION_UPDATED if not debug_announce else ACTION_INSERTED
    return SimpleNamespace(
        perform=lambda b: ScanResult(
            barcode=b, product=WITH_IMAGE,
            lookup=PriceLookup(offers=OFFERS, raw_json=None),
            action=action, saved_offer=OFFER_ACTIVE, image_task=None,
        ),
        backfill_image=lambda *a: 0,
    )


with tempfile.TemporaryDirectory() as td:
    bot_obj._access = AccessControl(999, os.path.join(td, "authorized_users.txt"))

    # Insert path: announce, then stored image, then the status reply.
    bot_obj._scans = canned_scan(debug_announce=True)
    bot_obj._debug = False
    msg, status = FakeMessage(), FakeStatus()
    asyncio.run(bot_obj._process([BARCODE], msg, status))
    check("process: announce comes first", msg.sent[0][0], ANNOUNCE)
    check("process: stored image goes out", msg.sent[1][0], "photo")
    check("process: reply is last", status.edits[-1][0], SINGLE_EXPECTED)

    # Update path with debug off: no per-update chatter, reply still sent.
    bot_obj._scans = canned_scan(debug_announce=False)
    bot_obj._debug = False
    msg, status = FakeMessage(), FakeStatus()
    asyncio.run(bot_obj._process([BARCODE], msg, status))
    check("process: debug off silences update chatter", msg.sent, [("photo", "Pams Butter 500g")])
    check("process: reply still sent", status.edits[-1][0], SINGLE_EXPECTED)

    # No barcodes at all: the framing-tip message edits the status directly.
    msg, status = FakeMessage(), FakeStatus()
    asyncio.run(bot_obj._process([], msg, status))
    check("process: none found", status.edits[0][0],
          "🔍 No barcodes found.\n"
          "Tips: make sure the barcode is well-lit, centered, and fills most of the frame.")

# ---------------------------------------------------------------------------
# 7. BarcodeDecoder: the pyzbar-result -> Barcode conversion logic
# ---------------------------------------------------------------------------


class FakeZbarResult:
    def __init__(self, btype, data):
        self.type = btype
        self.data = data


_real_decode = bot_oop.zbar_decode
bot_oop.zbar_decode = lambda image: [
    FakeZbarResult("EAN13", b"4006381333931"),
    FakeZbarResult("QRCODE", b"https://example.com/xyz"),
]
try:
    decoded = BarcodeDecoder().decode(Image.new("RGB", (2, 2)))
finally:
    bot_oop.zbar_decode = _real_decode
check("decoder: converts zbar results to Barcode",
      decoded,
      [Barcode("EAN13", "4006381333931"), Barcode("QRCODE", "https://example.com/xyz")])

bot_oop.zbar_decode = lambda image: []
try:
    nothing = BarcodeDecoder().decode(Image.new("RGB", (2, 2)))
finally:
    bot_oop.zbar_decode = _real_decode
check("decoder: no hits -> empty list", nothing, [])

# ---------------------------------------------------------------------------
# 8. Import-time side effects: the refactor must import clean, with no token,
#    no module-level mutable state, and no logging side effects.
# ---------------------------------------------------------------------------

env = {k: v for k, v in os.environ.items() if k != "BARCODE_BOT_TOKEN"}
probe = (
    "import sys, types; "
    "sys.modules['pyzbar'] = types.ModuleType('pyzbar'); "
    "m = types.ModuleType('pyzbar.pyzbar'); m.decode = lambda im: []; "
    "sys.modules['pyzbar.pyzbar'] = m; "
    "sys.path.insert(0, r'%s'); "
    "import bot_oop; "
    "print('import-ok' if not hasattr(bot_oop, 'AUTHORIZED_USERS') "
    "and not hasattr(bot_oop, 'DEBUG_MODE') and not hasattr(bot_oop, 'DB_CONFIG') "
    "else 'import-has-globals')" % HERE.replace("\\", "\\\\")
)
out = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, env=env)
check("import without token is side-effect free", out.stdout.strip(), "import-ok")

# ---------------------------------------------------------------------------

print()
if failures:
    print(f"{len(failures)} FAILURES: {failures}")
    sys.exit(1)
print("ALL CHECKS PASSED")
