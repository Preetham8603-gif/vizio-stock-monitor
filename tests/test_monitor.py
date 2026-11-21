"""Scenario tests built from REAL retailer pages captured 2026-11-21 (both
listed at $128 and out of stock), mutated to simulate other situations."""
import copy
import json
import re
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from monitor import core, parse, walmart_api  # noqa: E402
from monitor.fetch import Fetched  # noqa: E402
from monitor.notify import NotifyError, Telegram  # noqa: E402

FIX = ROOT / "tests" / "fixtures"
REAL = {"walmart": (FIX / "walmart_live_oos_128.html").read_text(),
        "samsclub": (FIX / "samsclub_live_oos_128.html").read_text()}
ND_RE = re.compile(r'(<script id="__NEXT_DATA__"[^>]*>)(.*?)(</script>)', re.S)
CHALLENGE = "<html><head><title>Robot or human?</title></head><body><div id=px-captcha></div></body></html>"
SAMS_CHALLENGE = "<html><head><title>Let us know you're not a robot - Sam's Club</title></head></html>"
T0 = datetime(2026, 11, 21, 17, 0, tzinfo=timezone.utc)  # 10:00 AM Arizona


def cfg():
    c = json.loads((ROOT / "config.json").read_text())
    c["retailers"]["samsclub"]["nearby_store_ids"] = {"6000": "Tempe test club"}
    c["retailers"]["samsclub"]["min_interval_minutes"] = 0  # scenario tests check every cycle
    return c


def page(retailer, *, status=None, price=None, ship=None, pickup=None, delivery=None, atc=None,
         postal="85281", store=None, model=None, upc=None, offer_new=True, seller=None, price_string=None):
    """Return the real page for `retailer` with the product JSON mutated."""
    html = REAL[retailer]
    m = ND_RE.search(html)
    data = json.loads(m.group(2))
    p = data["props"]["pageProps"]["initialData"]["data"]["product"]
    store = store or ("5768" if retailer == "walmart" else "6000")
    p["location"].update({"postalCode": postal, "storeIds": [store], "pickupLocation": {"storeId": store}})
    if price is not None:
        p["priceInfo"]["currentPrice"]["price"] = price
        p["priceInfo"]["currentPrice"]["priceString"] = price_string or f"${price:.2f}"
    elif price_string:
        p["priceInfo"]["currentPrice"]["priceString"] = price_string
    for opt in p["fulfillmentOptions"]:
        want = {"SHIPPING": ship, "PICKUP": pickup, "DELIVERY": delivery}[opt["type"]]
        if want:
            opt["availabilityStatus"] = want
    if status:
        p["availabilityStatus"] = status
    if atc is not None:
        p["showAtc"] = atc
        p["transactableOfferCount"] = 1 if atc else 0
    if model:
        p["model"] = model
    if upc:
        p["upc"] = upc
    if seller:
        p["sellerDisplayName"] = seller
    if not offer_new and p.get("conditionOffers"):
        p["offerId"] = [o for o in p["conditionOffers"] if not o["isConditionNew"]][0]["offerId"]
    return html[:m.start(2)] + json.dumps(data) + html[m.end(2):]


def in_stock(retailer, **kw):
    base = dict(status="IN_STOCK", atc=True, ship="IN_STOCK")
    base.update(kw)
    return page(retailer, **base)


def oos(retailer, **kw):
    return page(retailer, **kw)


class FakeNotifier:
    def __init__(self, fail=False):
        self.sent, self.fail = [], fail

    def send(self, text, silent=False):
        if self.fail:
            raise NotifyError("simulated Telegram outage")
        self.sent.append((text, silent))
        return len(self.sent)

    @property
    def loud(self):
        return [t for t, s in self.sent if not s]

    @property
    def stock_alerts(self):
        return [t for t in self.loud if t.startswith("VIZIO TV BACK IN STOCK!")]


class Sim:
    """Drive run_cycle with scripted retailer pages."""
    def __init__(self):
        self.cfg, self.state, self.now = cfg(), {}, T0

    def cycle(self, walmart, samsclub, notifier, status=200):
        pages = {self.cfg["retailers"]["walmart"]["url"]: walmart, self.cfg["retailers"]["samsclub"]["url"]: samsclub}
        fetcher = lambda url: Fetched(url, status, pages[url], attempts=1, stale_seconds=1)  # noqa: E731
        self.state, lines, code = core.run_cycle(self.cfg, self.state, fetcher, notifier, self.now)
        self.now += timedelta(minutes=10)
        return code, lines


class TestScenarios(unittest.TestCase):
    def test_real_live_pages_price_128_but_no_inventory_never_alert(self):
        """The real captured pages: $128 advertised, nothing purchasable."""
        c = cfg()
        w = parse.evaluate("walmart", REAL["walmart"], c)
        self.assertEqual(w.price, 128.0)
        self.assertTrue(w.model_verified)
        self.assertNotEqual(w.state, parse.IN_STOCK)
        s = parse.evaluate("samsclub", REAL["samsclub"], c)
        self.assertEqual(s.price, 128.0)
        self.assertNotEqual(s.state, parse.IN_STOCK)
        n = FakeNotifier()
        Sim().cycle(REAL["walmart"], REAL["samsclub"], n)
        self.assertEqual(n.stock_alerts, [])

    def test_01_unavailable_at_both(self):
        n = FakeNotifier()
        sim = Sim()
        sim.cycle(oos("walmart"), oos("samsclub"), n)
        self.assertEqual(sim.state["retailers"]["walmart"]["state"], parse.OUT_OF_STOCK)
        self.assertEqual(sim.state["retailers"]["samsclub"]["state"], parse.OUT_OF_STOCK)
        self.assertEqual(n.sent, [])

    def test_02_walmart_available_at_128(self):
        n = FakeNotifier()
        code, _ = Sim().cycle(in_stock("walmart", pickup="IN_STOCK"), oos("samsclub"), n)
        self.assertEqual(code, 0)
        self.assertEqual(len(n.stock_alerts), 1)
        msg = n.stock_alerts[0]
        for needle in ["Retailer: Walmart", "Model: VQD50X-10", "Price: $128", "Availability: Confirmed",
                       "Shipping to 85281", "Store pickup", "Checked: Sat Nov 21, 2026 10:00 AM MST (Arizona)",
                       "BUY NOW: https://www.walmart.com/ip/20255253279"]:
            self.assertIn(needle, msg)

    def test_03_samsclub_available_at_128(self):
        n = FakeNotifier()
        Sim().cycle(oos("walmart"), in_stock("samsclub"), n)
        self.assertEqual(len(n.stock_alerts), 1)
        self.assertIn("Retailer: Sam's Club", n.stock_alerts[0])
        self.assertIn("membership required", n.stock_alerts[0])
        self.assertIn("samsclub.com/ip/vizio-50-class-quantum-series-4k-qled-smart-tv/20393771188", n.stock_alerts[0])

    def test_04_price_above_target(self):
        n = FakeNotifier()
        sim = Sim()
        sim.cycle(in_stock("walmart", price=148.0), in_stock("samsclub", price=128.01), n)
        self.assertEqual(sim.state["retailers"]["walmart"]["state"], parse.PRICE_ABOVE_TARGET)
        self.assertEqual(sim.state["retailers"]["samsclub"]["state"], parse.PRICE_ABOVE_TARGET)
        self.assertEqual(n.stock_alerts, [])
        # under target still alerts
        self.assertEqual(parse.evaluate("walmart", in_stock("walmart", price=99.0), cfg()).state, parse.IN_STOCK)

    def test_05_different_model_or_offer_never_alerts(self):
        c = cfg()
        cases = [("walmart", in_stock("walmart", model="V50-J01")), ("walmart", in_stock("walmart", upc="845226000000")),
                 ("samsclub", in_stock("samsclub", upc="845226000000")), ("walmart", in_stock("walmart", offer_new=False)),
                 ("walmart", in_stock("walmart", seller="SomeMarketplaceSeller"))]
        for retailer, html in cases:
            r = parse.evaluate(retailer, html, c)
            self.assertEqual(r.state, parse.UNKNOWN, r.reason)
            self.assertIn("not the exact product", r.reason)

    def test_06_available_outside_85281_but_not_locally(self):
        c = cfg()
        # Page rendered for New York + a non-local store, shows stock there.
        r = parse.evaluate("walmart", in_stock("walmart", postal="10001", store="1234", pickup="IN_STOCK"), c)
        self.assertEqual(r.state, parse.UNKNOWN)
        self.assertIn("outside the 85281 area", r.reason)
        # Rendered for 85281: local channels out of stock, item in stock elsewhere.
        r = parse.evaluate("walmart", page("walmart", status="IN_STOCK", atc=True), c)
        self.assertEqual(r.state, parse.OUT_OF_STOCK)
        n = FakeNotifier()
        Sim().cycle(in_stock("walmart", postal="10001", store="1234"), oos("samsclub"), n)
        self.assertEqual(n.stock_alerts, [])

    def test_07_retailer_blocks_access(self):
        c = cfg()
        self.assertEqual(parse.evaluate("walmart", CHALLENGE, c).state, parse.BLOCKED)
        self.assertEqual(parse.evaluate("samsclub", SAMS_CHALLENGE, c).state, parse.BLOCKED)
        self.assertEqual(parse.evaluate("walmart", "", c, http_status=429).state, parse.BLOCKED)
        self.assertEqual(parse.evaluate("walmart", "<html>no data</html>", c).state, parse.UNKNOWN)
        self.assertEqual(parse.evaluate("walmart", "oops", c, http_status=503).state, parse.ERROR)
        sim, n = Sim(), FakeNotifier()
        for _ in range(5):
            sim.cycle(oos("walmart"), SAMS_CHALLENGE, n)
        warnings = [t for t in n.loud if "could not be checked reliably" in t]
        self.assertEqual(len(warnings), 1, "failure warning must be sent exactly once")
        self.assertIn("Sam's Club", warnings[0])
        self.assertEqual(n.stock_alerts, [])
        sim.cycle(oos("walmart"), oos("samsclub"), n)
        self.assertTrue(any("recovered" in t for t, _ in n.sent))

    def test_08_duplicate_availability_suppressed(self):
        sim, n = Sim(), FakeNotifier()
        for _ in range(4):
            sim.cycle(in_stock("walmart"), oos("samsclub"), n)
        self.assertEqual(len(n.stock_alerts), 1)
        # A block in the middle must not re-arm the alert.
        sim.cycle(CHALLENGE, oos("samsclub"), n)
        sim.cycle(in_stock("walmart"), oos("samsclub"), n)
        self.assertEqual(len(n.stock_alerts), 1)
        # A genuinely new fulfillment method is new information.
        sim.cycle(in_stock("walmart", pickup="IN_STOCK"), oos("samsclub"), n)
        self.assertEqual(len(n.stock_alerts), 2)

    def test_09_sells_out_then_returns(self):
        sim, n = Sim(), FakeNotifier()
        sim.cycle(in_stock("walmart"), oos("samsclub"), n)
        sim.cycle(oos("walmart"), oos("samsclub"), n)
        self.assertTrue(any("no longer available" in t for t in n.loud))
        sim.cycle(oos("walmart"), oos("samsclub"), n)
        sim.cycle(in_stock("walmart"), oos("samsclub"), n)
        self.assertEqual(len(n.stock_alerts), 2)

    def test_10_telegram_failure_retries_next_run(self):
        sim = Sim()
        code, lines = sim.cycle(in_stock("walmart"), oos("samsclub"), FakeNotifier(fail=True))
        self.assertEqual(code, 1, "run must fail so GitHub flags it")
        self.assertEqual(sim.state["retailers"]["walmart"]["alerted_methods"], [])
        n = FakeNotifier()
        code, _ = sim.cycle(in_stock("walmart"), oos("samsclub"), n)
        self.assertEqual(code, 0)
        self.assertEqual(len(n.stock_alerts), 1)

    def test_advertised_price_with_unorderable_page_is_not_stock(self):
        # Fulfillment claims IN_STOCK but page has no add-to-cart and is OOS.
        r = parse.evaluate("walmart", page("walmart", ship="IN_STOCK", atc=False, status="OUT_OF_STOCK"), cfg())
        self.assertEqual(r.state, parse.UNKNOWN)
        self.assertIn("not orderable", r.reason)

    def test_price_validation(self):
        r = parse.evaluate("walmart", in_stock("walmart", price=128.0, price_string="$228.00"), cfg())
        self.assertEqual(r.state, parse.UNKNOWN)
        self.assertIn("price mismatch", r.reason)
        r = parse.evaluate("walmart", in_stock("walmart", price=1.0, price_string="$1.00"), cfg())
        self.assertEqual(r.state, parse.UNKNOWN)

    def test_stale_response_is_unknown(self):
        c, s = cfg(), {}
        fetcher = lambda url: Fetched(url, 200, in_stock("walmart") if "walmart" in url else oos("samsclub"), attempts=1, stale_seconds=3600)  # noqa: E731
        n = FakeNotifier()
        s, lines, _ = core.run_cycle(c, s, fetcher, n, T0)
        self.assertEqual(s["retailers"]["walmart"]["state"], parse.UNKNOWN)
        self.assertEqual(n.stock_alerts, [])

    def test_daily_summary_once_per_day(self):
        sim, n = Sim(), FakeNotifier()
        sim.cycle(oos("walmart"), oos("samsclub"), n)
        self.assertEqual(len(core.maybe_daily_summary(sim.cfg, sim.state, n, T0)), 1)
        self.assertEqual(core.maybe_daily_summary(sim.cfg, sim.state, n, T0 + timedelta(hours=2)), [])
        self.assertIn("daily health", n.sent[-1][0])

    def test_schedule_gap_detected(self):
        sim, n = Sim(), FakeNotifier()
        sim.cycle(oos("walmart"), oos("samsclub"), n)
        sim.now += timedelta(minutes=180)
        _, lines = sim.cycle(oos("walmart"), oos("samsclub"), n)
        self.assertTrue(any("since previous run" in l for l in lines))
        self.assertTrue(any("no check ran" in t for t, _ in n.sent))


def api_item(**kw):
    item = {"itemId": 20255253279, "name": "VIZIO 50\" Class Quantum 4K QLED ... VQD50X-10", "salePrice": 128.0,
            "upc": "194346693900", "modelNumber": "VQD50X-10", "brandName": "VIZIO", "sellerInfo": "Walmart.com",
            "stock": "Available", "availableOnline": True, "attributes": {"isOrderable": "Y"}, "offerType": "ONLINE_AND_STORE"}
    item.update(kw)
    return {"items": [item]}


class TestWalmartApi(unittest.TestCase):
    def ev(self, payload, status=200, err=""):
        return walmart_api.evaluate(payload, status, err, cfg())

    def test_in_stock_at_target(self):
        r = self.ev(api_item())
        self.assertEqual(r.state, parse.IN_STOCK)
        self.assertEqual([c.method for c in r.qualifying_channels], ["SHIPPING"])
        self.assertIn("85281", r.qualifying_channels[0].describe("85281"))

    def test_out_of_stock_and_not_orderable(self):
        self.assertEqual(self.ev(api_item(stock="Not available")).state, parse.OUT_OF_STOCK)
        self.assertEqual(self.ev(api_item(availableOnline=False)).state, parse.OUT_OF_STOCK)
        self.assertEqual(self.ev(api_item(attributes={"isOrderable": "N"})).state, parse.OUT_OF_STOCK)

    def test_price_above(self):
        self.assertEqual(self.ev(api_item(salePrice=149.99)).state, parse.PRICE_ABOVE_TARGET)

    def test_wrong_product_or_seller(self):
        for kw in [dict(upc="000000000000"), dict(modelNumber="V50-J01"), dict(brandName="TCL"),
                   dict(sellerInfo="Random Marketplace Co"), dict(upc=None, modelNumber=None)]:
            r = self.ev(api_item(**kw))
            self.assertEqual(r.state, parse.UNKNOWN, kw)

    def test_errors(self):
        self.assertEqual(self.ev(None, 401, "HTTP 401").state, parse.ERROR)
        self.assertEqual(self.ev(None, 429, "HTTP 429").state, parse.BLOCKED)
        self.assertEqual(self.ev(None, None, "timeout").state, parse.ERROR)
        self.assertEqual(self.ev({"items": []}).state, parse.UNKNOWN)
        self.assertEqual(self.ev(api_item(stock=None)).state, parse.UNKNOWN)
        self.assertEqual(self.ev(api_item(salePrice=None)).state, parse.UNKNOWN)

    def test_cycle_uses_api_checker_and_dedupes(self):
        sim, n = Sim(), FakeNotifier()
        checkers = {"walmart": lambda c: walmart_api.evaluate(api_item(), 200, "", c)}
        pages = {sim.cfg["retailers"]["samsclub"]["url"]: SAMS_CHALLENGE}
        fetcher = lambda url: Fetched(url, 200, pages[url], attempts=1)  # noqa: E731
        for _ in range(3):
            sim.state, _, code = core.run_cycle(sim.cfg, sim.state, fetcher, n, sim.now, checkers=checkers)
            sim.now += timedelta(minutes=10)
        self.assertEqual(len(n.stock_alerts), 1)
        self.assertIn("Shipping to 85281", n.stock_alerts[0])

    def test_signature_verifies_with_public_key(self):
        import subprocess, tempfile, base64, os
        with tempfile.TemporaryDirectory() as d:
            priv, pub = os.path.join(d, "p.pem"), os.path.join(d, "pub.pem")
            subprocess.run(["openssl", "genpkey", "-algorithm", "RSA", "-pkeyopt", "rsa_keygen_bits:2048", "-out", priv], check=True, capture_output=True)
            subprocess.run(["openssl", "rsa", "-in", priv, "-pubout", "-out", pub], check=True, capture_output=True)
            msg = walmart_api.canonical("cid-123", "1700000000000", "1")
            self.assertEqual(msg, "cid-123\n1700000000000\n1\n")  # ID, INTIMESTAMP, KEY_VERSION order
            sig = walmart_api.sign(open(priv).read(), msg)
            sf = os.path.join(d, "sig"); open(sf, "wb").write(base64.b64decode(sig))
            ok = subprocess.run(["openssl", "dgst", "-sha256", "-verify", pub, "-signature", sf], input=msg.encode(), capture_output=True)
            self.assertEqual(ok.returncode, 0, ok.stdout + ok.stderr)


class TestHourlySams(unittest.TestCase):
    def test_sams_checked_at_most_hourly(self):
        c = cfg()
        c["retailers"]["samsclub"]["min_interval_minutes"] = 60
        hits = []
        def fetcher(url):
            hits.append(url)
            return Fetched(url, 200, oos("walmart") if "walmart" in url else SAMS_CHALLENGE, attempts=1)
        state, now, n = {}, T0, FakeNotifier()
        for _ in range(12):  # two hours of 10-minute runs
            state, _, _ = core.run_cycle(c, state, fetcher, n, now)
            now += timedelta(minutes=10)
        self.assertEqual(sum("samsclub" in u for u in hits), 2)
        self.assertEqual(sum("walmart" in u for u in hits), 12)
        self.assertEqual(state["retailers"]["samsclub"]["consecutive_failures"], 2)


class TestTelegram(unittest.TestCase):
    def test_retries_then_succeeds(self):
        calls = []
        def sender(payload):
            calls.append(payload)
            if len(calls) < 3:
                return {"ok": False, "error_code": 429, "parameters": {"retry_after": 1}}
            return {"ok": True, "result": {"message_id": 42}}
        t = Telegram("123:SECRET", "999", sender=sender, sleep=lambda s: None)
        self.assertEqual(t.send("hi"), 42)
        self.assertEqual(len(calls), 3)

    def test_bad_token_fails_fast_without_leaking(self):
        def sender(payload):
            return {"ok": False, "error_code": 401, "description": "Unauthorized for 123:SECRET"}
        t = Telegram("123:SECRET", "999", sender=sender, sleep=lambda s: None)
        with self.assertRaises(NotifyError) as cm:
            t.send("hi")
        self.assertNotIn("SECRET", str(cm.exception))

    def test_missing_credentials(self):
        with self.assertRaises(NotifyError):
            Telegram("", "", sleep=lambda s: None).send("x")


if __name__ == "__main__":
    unittest.main()
