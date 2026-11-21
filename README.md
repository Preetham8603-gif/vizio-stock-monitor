# VIZIO VQD50X-10 stock monitor

A cloud job that watches the **VIZIO 50" Quantum 4K QLED (2026 Limited Edition), model VQD50X-10**. It checks
Walmart and Sam's Club for Tempe, AZ 85281, and sends a Telegram push when a **verified, orderable** offer is
**$128 or less**. It never buys anything.

## How it works

| | |
|---|---|
| **Host** | GitHub Actions in this public repo. It runs whether or not your computer is on. |
| **Schedule** | Cron `3-59/10 * * * *` (every 10 min). GitHub doesn't guarantee timing: runs are often 5–20 min late and occasionally skipped under load. Each run detects gaps and reports them. |
| **Walmart** | Official **Walmart Affiliate API** (walmart.io Product Lookup) with `zipCode=85281`. Requests are signed with your RSA key. Item `20255253279`. |
| **Sam's Club** | Public product page, fetched **at most once an hour** (item `20393771188`). |
| **State** | `state.json` on the `state` branch (dedupe flags, last result, history). It's committed every run. |
| **Alerts** | Telegram Bot API. Credentials are encrypted GitHub Actions secrets. |
| **Cost** | $0. Public repos get unlimited Actions minutes, and the Walmart API and Telegram are free. |

### What counts as "in stock"
An alert fires only when **all** of these hold:
- **Exact product:** item ID, UPC `194346693900`, model `VQD50X-10`, brand VIZIO, New condition (not Open Box or
  refurbished), and sold by Walmart.com / Sam's Club rather than a marketplace seller.
- **Price:** valid and ≤ max price. The numeric price is cross-checked against the displayed price.
- **Orderable, not just listed:** Walmart's API reports `stock=Available`, `availableOnline` is not false, and
  `isOrderable` is not N. On Sam's Club pages, the page must be IN_STOCK with add-to-cart enabled **and** a fulfillment
  method must be IN_STOCK.
- **Location:** the result must be for 85281 or a configured nearby ZIP or store. Stock elsewhere is ignored.

Every check ends in one of these states: `IN_STOCK`, `OUT_OF_STOCK`, `PRICE_ABOVE_TARGET`, `UNKNOWN`, `BLOCKED`, `ERROR`.
The last three **never** trigger a stock alert, and they never reset duplicate suppression.

### Notifications
- **Stock alert** (loud): retailer, model, price, fulfillment method, Arizona time, BUY NOW link. Sent once per
  availability episode. A new fulfillment method appearing counts as new information.
- **Sold out / back again:** after a confirmed out-of-stock result you get a "no longer available" note, and the alert
  re-arms.
- **Status and price changes** (silent) while not purchasable.
- **Monitoring failure warning:** after 3 consecutive unverifiable checks for a retailer. Sent once, plus a recovery
  message when checks work again.
- **Daily health summary** (silent) after 8 AM Arizona time: checks run, verified counts, scheduler gaps.
- **If Telegram is down:** the alert is retried every run and that run is marked **failed**, so GitHub also emails you
  its standard "workflow failed" notice.

## Controls

Install the GitHub CLI (`brew install gh`, then `gh auth login`) and run these from this folder:

```bash
./control.sh check          # run a stock check now
./control.sh status         # latest inventory status per retailer
./control.sh history        # recent runs (success/failure, timing)
./control.sh logs           # what the most recent run saw
./control.sh test           # send a Telegram test notification
./control.sh summary        # send the daily health summary now
./control.sh pause          # scheduled runs skip (manual runs still work)
./control.sh resume
./control.sh price 119.99   # change the maximum price
./control.sh disable        # stop permanently (undo: gh workflow enable monitor.yml)
```
You can do the same from the GitHub website: open **Actions → vizio-monitor → Run workflow** and pick a mode. For
pause and price, use **Settings → Secrets and variables → Actions → Variables** (`MONITOR_PAUSED`, `MAX_PRICE`).

## Setup (already done for this repo)
1. Create a bot with Telegram's @BotFather and send it `/start`. Then run `./setup_telegram.sh`: you enter the token in
   a hidden prompt, and it is stored as the `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` secrets.
2. Run `./setup_walmart_api.sh`. It generates an RSA key and stores the private half as `WALMART_PRIVATE_KEY`. Paste
   the printed public key into walmart.io (log in with a walmart.com account and create an application), then enter
   the Consumer ID it shows.

## Known limitations (read these)
- **Walmart store pickup isn't monitored.** The Affiliate API's `storeId` parameter needs separate approval from
  Walmart's business team. Walmart alerts cover **shipping to 85281** only.
- **Walmart's `zipCode` is taken on trust.** The API response doesn't echo the ZIP back, so if Walmart ignored the
  parameter we couldn't tell.
- **Sam's Club usually can't be checked.** It has no public API, and its bot protection blocked page requests from
  GitHub's servers and from a home connection after a few requests. When it does serve a page, the page is rendered for
  the server's location (e.g. Virginia), not 85281. The monitor then reports `UNKNOWN`/`BLOCKED` and **won't alert**.
  You'll see this in the daily summary. A Sam's Club alert can only fire if Sam's serves a verified Tempe-area result.
- **Not real-time.** Expect a 10–30 min delay between a restock and the alert. Fast sell-outs may be missed.
- **Schedule auto-disable.** GitHub disables scheduled workflows in repos with no activity for 60 days. The workflow
  pushes a keepalive commit monthly, but if you ever see a "scheduled workflow disabled" email, run
  `gh workflow enable monitor.yml`.
- **Page or API changes.** If Walmart changes its API or Sam's changes its page format, checks become `UNKNOWN`/`ERROR`
  and you get the failure warning rather than silence.

## Troubleshooting
| Symptom | What to do |
|---|---|
| "could not be checked reliably" for Walmart | `./control.sh logs`. HTTP 401/403 means the API key or Consumer ID is wrong (re-run `setup_walmart_api.sh`); 429 means you're rate-limited. |
| No daily summary | `./control.sh history`. Runs may be paused (`MONITOR_PAUSED`) or the workflow disabled. |
| No Telegram messages at all | `./control.sh test`. If it fails, re-run `./setup_telegram.sh` and make sure you haven't blocked the bot. |
| Want to test without alerts | **Run workflow → dry-run** prints what it would send. |

Run the tests locally with `python3 -m unittest discover -s tests -v`. There are 27 tests built from real captured
retailer pages, and they also run in CI on every push.
