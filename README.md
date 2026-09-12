# barcode-bot-oop

Point your phone at a barcode, get the product and live per-store prices back
in Telegram. This repo is the object-oriented rewrite of
[the original single-file barcode bot](https://luna-lab.mywire.org/blog/article51)
(article 51): same behaviour, byte-for-byte, rebuilt around nine small classes
with one job each. It has been running in production on a Debian home server
since September 2026, and the rewrite story — why and how — is told in
[article 52: *Same Bot, Cleaner Bones*](https://luna-lab.mywire.org/blog/article52).

## What it does

1. A Telegram user sends a photo of a product barcode (one or many).
2. Barcodes are decoded locally with `pyzbar` through a preprocessing
   strategy pipeline (plain, contrast boost, grayscale).
3. The product's prices are looked up live in the KiwiSquare price API for
   the configured stores.
4. The bot replies with one message: product name, brand, category, photo,
   and a per-store price table. Unknown products are saved to SQL Server and
   get their photo backfilled; known ones get their data refreshed.

## Architecture

`main()` is the only composition root — nothing happens at import time (no
token read, no allowlist loaded, no database touched), which is what makes
every class testable in isolation. Nine classes, one job each (from the
module's own docstring):

| Class | Job |
|---|---|
| `Settings` | all configuration, read once, passed everywhere |
| `AccessControl` | the file-backed allowlist of Telegram user IDs |
| `ProductRepository` | every SQL Server query in one door |
| `KiwiSquareClient` | the external price API |
| `BarcodeDecoder` | barcode decoding with a preprocessing strategy pipeline |
| `FileDownloader` | Telegram file downloads with retries |
| `ReplyBuilder` | pure message formatting: data in, markdown out |
| `ScanService` | one barcode in, a `ScanResult` out (plus DB housekeeping) |
| `BarcodeBot` | the Telegram façade with thin handlers |

## Configuration

All instance-specific values come from the environment or a `.env` file next
to `barcode-bot.py` — nothing sensitive is hardcoded. Copy `.env.example` to
`.env` and fill it in:

```ini
BARCODE_BOT_TOKEN=          # from @BotFather (or keep a token.txt beside the script)
BARCODE_BOT_ADMIN_ID=12345  # Telegram user ID allowed to run admin commands
KIWISQUARE_STORE_IDS=guid1,guid2
MSSQL_SERVER=sqlserver-host
MSSQL_USER=bot
MSSQL_PASSWORD=...
MSSQL_DATABASE=Barcodes     # default
```

Rules:

* **Real environment wins.** The built-in loader uses `setdefault`, so
  `run.sh` exports, systemd `EnvironmentFile=` and shell exports always beat
  `.env`. A missing `.env` is not an error.
* `BARCODE_BOT_TOKEN` may be empty in `.env` — the bot then falls back to a
  `token.txt` file next to it.
* **Fail fast:** if any required variable is missing, the bot exits at startup
  with a `RuntimeError` naming exactly which keys are missing and where to put
  them, instead of dying later on a database login.
* Values are never logged. `.env` is gitignored; keep it that way.

## Install & run

Python 3.10+ (tested on 3.11.2, Debian 12).

```bash
python3 -m venv venv && . venv/bin/activate   # or use the system interpreter
pip install -r requirements.txt
cp .env.example .env && $EDITOR .env
python barcode-bot.py
```

`pyzbar` needs the zbar native library:

* **Debian/Ubuntu:** `sudo apt install libzbar0`
* **Windows:** install [Visual C++ 2013 redistributable (msvcr120.dll)](https://www.microsoft.com/en-us/download/details.aspx?id=40784)
* **macOS:** `brew install zbar`

## Database

One table (`dbo.Item` in a `Barcodes` database). Create it with
[`items-table.sql`](items-table.sql). The bot runs exactly four statements
against it: one lookup by barcode, one insert of new products (full price-API
JSON kept in `RawJSON`), one refresh of product data, one refresh of the
product photo.

## Deployment (systemd user unit)

```ini
# ~/.config/systemd/user/barcode-bot.service
[Unit]
Description=Barcode Scanner Telegram Bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=%h/barcode-scanner-bot
ExecStart=/usr/bin/python3 barcode-bot.py
Restart=on-failure
RestartSec=10
StandardOutput=append:%h/barcode-scanner-bot/bot.log
StandardError=append:%h/barcode-scanner-bot/bot.log

[Install]
WantedBy=default.target
```

`systemctl --user enable --now barcode-bot.service` (enable lingering with
`loginctl enable-linger $USER` so the unit survives without a login session).
With `.env` in place the unit needs no `EnvironmentFile=`; anything you still
export there simply takes precedence over `.env`.

## Tests

```bash
python test_bot.py
```

The suite runs 59 checks, no network, no database, no Telegram:

* **parity checks** — the rewrite's formatting output is compared byte-for-byte
  against the original `bot.py`'s functions;
* **behaviour checks** — decision logic, handlers, decoder and the settings
  layer are unit-tested with fakes;
* **an import probe** — asserts `barcode-bot.py` imports side-effect-free even
  without a token.

The original `bot.py` is deliberately **not** in this repository (it carries
this instance's real credentials). The parity sections of the suite import it
at the top, so on a fresh clone the suite needs a legacy `bot.py` placed next
to it to run; that is by design — the parity battery is what kept the rewrite
honest during development.

## Safety notes

* Never commit `.env`, `token.txt` or your own `bot.py` — all three are in
  `.gitignore` already.
* The Telegram token can live in `.env` (`BARCODE_BOT_TOKEN=...`) or in a
  `token.txt` beside the script; whichever it finds first wins.

## License

[MIT](LICENSE)