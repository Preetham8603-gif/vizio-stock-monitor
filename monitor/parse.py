"""Turn a retailer product page into a verified inventory verdict.

Both walmart.com and samsclub.com server-render the product page with a
`__NEXT_DATA__` JSON blob containing the item, the selected offer, price,
and per-fulfillment-method availability *for the location the page was
rendered for*. We read only that public page data, the same thing a browser
receives. Nothing here guesses: every path that can't be verified ends in
UNKNOWN / BLOCKED / ERROR, never IN_STOCK.
"""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field

IN_STOCK = "IN_STOCK"
OUT_OF_STOCK = "OUT_OF_STOCK"
PRICE_ABOVE_TARGET = "PRICE_ABOVE_TARGET"
UNKNOWN = "UNKNOWN"
BLOCKED = "BLOCKED"
ERROR = "ERROR"

HEALTHY_STATES = {IN_STOCK, OUT_OF_STOCK, PRICE_ABOVE_TARGET}
# Retailer fulfillment statuses that mean "can be ordered now".
AVAILABLE_FULFILLMENT = {"IN_STOCK", "LIMITED_STOCK", "AVAILABLE"}

CHANNEL_LABEL = {"SHIPPING": "Shipping", "PICKUP": "Store pickup", "DELIVERY": "Local delivery"}

_NEXT_DATA_RE = re.compile(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S)
_BLOCK_MARKERS = ("Robot or human?", "not a robot", "blocked - redirecting", "/blocked?", "px-captcha", "Access Denied", "blocked-page", "Request unsuccessful")


@dataclass
class Channel:
    method: str                 # SHIPPING / PICKUP / DELIVERY
    raw_status: str             # retailer status string
    location_text: str
    location_verified: bool     # rendered for 85281 / an allowed nearby store
    available: bool             # raw_status says orderable
    note: str = ""

    @property
    def counts(self) -> bool:
        return self.available and self.location_verified

    def describe(self, target_zip: str) -> str:
        label = CHANNEL_LABEL.get(self.method, self.method.title())
        if self.method == "SHIPPING":
            return f"{label} to {target_zip}" + (f" (checked as {self.location_text})" if self.location_text and target_zip not in self.location_text else "")
        return f"{label} – {self.location_text}" if self.location_text else label


@dataclass
class Result:
    retailer: str
    state: str
    reason: str
    price: float | None = None
    title: str = ""
    model_verified: bool = False
    location_postal: str | None = None
    location_store_ids: list = field(default_factory=list)
    channels: list = field(default_factory=list)
    membership_required: bool = False
    http_status: int | None = None

    @property
    def qualifying_channels(self) -> list:
        return [c for c in self.channels if c.counts]

    def offer_key(self) -> str:
        """Identity of a qualifying offer, used for duplicate suppression."""
        methods = ",".join(sorted(c.method for c in self.qualifying_channels))
        return f"{self.retailer}|{methods}"

    def to_dict(self) -> dict:
        d = asdict(self)
        d["channels"] = [asdict(c) for c in self.channels]
        return d


def extract_next_data(html: str) -> dict | None:
    m = _NEXT_DATA_RE.search(html or "")
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except json.JSONDecodeError:
        return None


def looks_blocked(html: str, http_status: int | None) -> bool:
    if http_status in (403, 412, 429):
        return True
    head = (html or "")[:20000]
    return any(marker in head for marker in _BLOCK_MARKERS)


def _price_from(product: dict) -> tuple[float | None, str]:
    cur = ((product.get("priceInfo") or {}).get("currentPrice")) or {}
    price, pstr = cur.get("price"), cur.get("priceString") or ""
    if not isinstance(price, (int, float)) or isinstance(price, bool):
        return None, "price missing from page data"
    # Cross-check numeric price against the display string.
    m = re.search(r"\$?\s*([0-9][0-9,]*\.?[0-9]{0,2})", pstr)
    if m:
        shown = float(m.group(1).replace(",", ""))
        if abs(shown - float(price)) > 0.005:
            return None, f"price mismatch: numeric {price} vs display '{pstr}'"
    if not (20 <= float(price) <= 5000):
        return None, f"implausible price {price}"
    return round(float(price), 2), ""


def evaluate(retailer_key: str, html: str, cfg: dict, http_status: int | None = 200) -> Result:
    rcfg = cfg["retailers"][retailer_key]
    name = rcfg["name"]

    if looks_blocked(html, http_status):
        return Result(name, BLOCKED, f"retailer returned a bot challenge / block (HTTP {http_status})", http_status=http_status)
    if http_status is None or http_status >= 400:
        return Result(name, ERROR, f"HTTP {http_status}", http_status=http_status)

    data = extract_next_data(html)
    if data is None:
        return Result(name, UNKNOWN, "page had no readable product data (__NEXT_DATA__ missing)", http_status=http_status)

    page_props = (data.get("props") or {}).get("pageProps") or {}
    if page_props.get("hasItemResiliencyCacheHeader") or page_props.get("isItemCaching"):
        return Result(name, UNKNOWN, "retailer served a cached/resiliency copy of the page (possibly stale)", http_status=http_status)

    product = (((page_props.get("initialData") or {}).get("data")) or {}).get("product")
    if not product:
        return Result(name, UNKNOWN, "no product object in page data (item removed or page changed)", http_status=http_status)

    res = Result(name, UNKNOWN, "", title=product.get("name") or "", http_status=http_status)
    loc = product.get("location") or {}
    res.location_postal = loc.get("postalCode")
    store_ids = [str(s) for s in (loc.get("storeIds") or [])]
    pickup_store = str(((loc.get("pickupLocation") or {}).get("storeId")) or "")
    res.location_store_ids = store_ids

    # ---- exact product identity --------------------------------------
    problems = []
    if str(product.get("usItemId")) != rcfg["item_id"]:
        problems.append(f"item id {product.get('usItemId')} != {rcfg['item_id']}")
    if str(product.get("upc") or "") != cfg["expected_upc"]:
        problems.append(f"UPC {product.get('upc')} != {cfg['expected_upc']}")
    model = product.get("model") or product.get("manufacturerProductId")
    if model and str(model).upper() != cfg["expected_model"].upper():
        problems.append(f"model {model} != {cfg['expected_model']}")
    if (product.get("brand") or "").upper() != "VIZIO":
        problems.append(f"brand {product.get('brand')}")
    if product.get("isPreowned") or (product.get("conditionType") not in (None, "New")):
        problems.append(f"condition {product.get('conditionType')} / preowned={product.get('isPreowned')}")
    if product.get("sellerType") not in (None, "INTERNAL"):
        problems.append(f"third-party seller ({product.get('sellerType')})")
    want_seller = rcfg.get("require_seller_display_name")
    if want_seller and product.get("sellerDisplayName") != want_seller:
        problems.append(f"seller '{product.get('sellerDisplayName')}' != '{want_seller}'")
    # Walmart lists other conditions (e.g. Open Box) on the same page: the
    # selected offer must be the New one.
    new_offers = [o for o in (product.get("conditionOffers") or []) if o.get("isConditionNew")]
    if product.get("conditionOffers") and product.get("offerId") not in {o.get("offerId") for o in new_offers}:
        problems.append("selected offer is not the New-condition offer")
    if problems:
        res.reason = "not the exact product/offer: " + "; ".join(problems)
        return res
    res.model_verified = True
    res.membership_required = retailer_key == "samsclub"

    price, perr = _price_from(product)
    if price is None:
        res.reason = perr
        return res
    res.price = price

    # ---- per-fulfillment availability, location-checked -----------------
    allowed_zips = set(cfg.get("accepted_shipping_zips") or [cfg["target_zip"]])
    nearby = set((rcfg.get("nearby_store_ids") or {}).keys())
    for opt in product.get("fulfillmentOptions") or []:
        method = opt.get("type") or "?"
        status = opt.get("availabilityStatus") or "UNKNOWN"
        text = opt.get("locationText") or ""
        if method == "SHIPPING":
            verified = res.location_postal in allowed_zips
            note = "" if verified else f"rendered for ZIP {res.location_postal}, not the 85281 area"
        elif method in ("PICKUP", "DELIVERY"):
            store = pickup_store or (store_ids[0] if store_ids else "")
            verified = bool(store) and store in nearby
            note = "" if verified else f"store {store or '?'} is not one of the configured stores near {cfg['target_zip']}"
        else:
            verified, note = False, "unrecognised fulfillment method"
        res.channels.append(Channel(method, status, text, verified, status in AVAILABLE_FULFILLMENT, note))

    page_status = product.get("availabilityStatus")
    orderable = bool(product.get("showAtc")) or (product.get("transactableOfferCount") or 0) > 0
    qualifying = res.qualifying_channels

    if qualifying:
        if not (page_status == "IN_STOCK" and orderable):
            # A fulfillment option claims stock but the page won't let anyone
            # add to cart. Treat as unverified rather than in stock.
            res.reason = f"fulfillment option claims stock but item is not orderable (status={page_status}, addToCart={orderable})"
            return res
        if price > float(cfg["max_price"]) + 1e-9:
            res.state, res.reason = PRICE_ABOVE_TARGET, f"${price:.2f} > max ${float(cfg['max_price']):.2f}"
            return res
        res.state, res.reason = IN_STOCK, "orderable at/below target price for " + ", ".join(c.method for c in qualifying)
        return res

    verified_channels = [c for c in res.channels if c.location_verified]
    available_elsewhere = [c for c in res.channels if c.available and not c.location_verified]
    if available_elsewhere and not verified_channels:
        res.reason = "available only for a location outside the 85281 area: " + "; ".join(f"{c.method} {c.note}" for c in available_elsewhere)
        return res
    if not verified_channels:
        res.reason = "could not verify any fulfillment method for the 85281 area: " + "; ".join(f"{c.method} {c.note}" for c in res.channels)
        return res
    res.state = OUT_OF_STOCK
    res.reason = "no orderable fulfillment for the 85281 area (" + ", ".join(f"{c.method}={c.raw_status}" for c in verified_channels) + ")"
    if price > float(cfg["max_price"]) + 1e-9:
        res.reason += f"; also ${price:.2f} > max"
    return res
