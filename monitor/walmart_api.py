"""Walmart Affiliate API (walmart.io) – the authorized way to read Walmart
price/stock from a cloud server. Docs: walmart.io/docs/affiliates/v1/product-lookup

Auth: headers WM_CONSUMER.ID, WM_CONSUMER.INTIMESTAMP, WM_SEC.KEY_VERSION and
WM_SEC.AUTH_SIGNATURE = base64(SHA256withRSA(private_key, canonical)), where
canonical is the three header values ordered by header name, each followed
by "\\n". Signatures expire after 180 s, so we sign per request.
Signing uses the `openssl` CLI (present on GitHub runners and macOS), so the
project stays dependency-free.
"""
from __future__ import annotations

import base64
import json
import os
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

from . import parse
from .fetch import _ssl_context

ENDPOINT = "https://developer.api.walmart.com/api-proxy/service/affil/product/v2/items"


def configured() -> bool:
    return all(os.environ.get(k) for k in ("WALMART_CONSUMER_ID", "WALMART_PRIVATE_KEY"))


def canonical(consumer_id: str, timestamp: str, key_version: str) -> str:
    headers = {"WM_CONSUMER.ID": consumer_id, "WM_CONSUMER.INTIMESTAMP": timestamp, "WM_SEC.KEY_VERSION": key_version}
    return "".join(headers[k].strip() + "\n" for k in sorted(headers))


def sign(private_key_pem: str, message: str) -> str:
    with tempfile.TemporaryDirectory() as d:
        kp = os.path.join(d, "k.pem")
        fd = os.open(kp, os.O_WRONLY | os.O_CREAT, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(private_key_pem)
        out = subprocess.run(["openssl", "dgst", "-sha256", "-sign", kp], input=message.encode(),
                             capture_output=True, timeout=20)
    if out.returncode != 0:
        raise RuntimeError("openssl signing failed (bad WALMART_PRIVATE_KEY?)")
    return base64.b64encode(out.stdout).decode()


def auth_headers() -> dict:
    cid = os.environ["WALMART_CONSUMER_ID"].strip()
    kv = (os.environ.get("WALMART_KEY_VERSION") or "1").strip()
    ts = str(int(time.time() * 1000))
    return {"WM_CONSUMER.ID": cid, "WM_CONSUMER.INTIMESTAMP": ts, "WM_SEC.KEY_VERSION": kv,
            "WM_SEC.AUTH_SIGNATURE": sign(os.environ["WALMART_PRIVATE_KEY"], canonical(cid, ts, kv)),
            "Accept": "application/json"}


def request(item_id: str, zip_code: str, retries: int = 3, sleep=time.sleep) -> tuple[int | None, dict | None, str]:
    url = ENDPOINT + "?" + urllib.parse.urlencode({"ids": item_id, "zipCode": zip_code})
    last = ""
    for attempt in range(1, retries + 1):
        try:
            req = urllib.request.Request(url, headers=auth_headers())
            with urllib.request.urlopen(req, timeout=20, context=_ssl_context()) as r:
                return r.status, json.loads(r.read().decode()), ""
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="replace")[:300]
            if e.code in (400, 401, 403, 404, 429):
                return e.code, None, f"HTTP {e.code}: {body}"
            last = f"HTTP {e.code}"
        except Exception as e:
            last = f"{type(e).__name__}: {e}"
        if attempt < retries:
            sleep(3 * 2 ** (attempt - 1))
    return None, None, last


def evaluate(payload: dict | None, status: int | None, err: str, cfg: dict) -> parse.Result:
    """Map an Affiliate API response to a verified Result (shipping to target ZIP)."""
    rcfg = cfg["retailers"]["walmart"]
    name = rcfg["name"]
    if status == 429:
        return parse.Result(name, parse.BLOCKED, "Walmart API rate limit (HTTP 429)", http_status=status)
    if status in (401, 403):
        return parse.Result(name, parse.ERROR, f"Walmart API rejected credentials: {err}", http_status=status)
    if payload is None:
        return parse.Result(name, parse.ERROR, f"Walmart API request failed: {err}", http_status=status)
    items = payload.get("items") or []
    item = next((i for i in items if str(i.get("itemId")) == rcfg["item_id"]), None)
    if item is None:
        return parse.Result(name, parse.UNKNOWN, f"API returned no item {rcfg['item_id']} ({str(payload)[:150]})", http_status=status)

    res = parse.Result(name, parse.UNKNOWN, "", title=item.get("name") or "", http_status=status,
                       location_postal=cfg["target_zip"])
    problems = []
    if str(item.get("upc") or "") not in ("", cfg["expected_upc"]):
        problems.append(f"UPC {item.get('upc')}")
    if not item.get("upc") and not item.get("modelNumber"):
        problems.append("no UPC/model to verify")
    model = item.get("modelNumber")
    if model and str(model).upper() != cfg["expected_model"].upper():
        problems.append(f"model {model}")
    if (item.get("brandName") or "").upper() != "VIZIO":
        problems.append(f"brand {item.get('brandName')}")
    seller = item.get("sellerInfo")
    if seller and seller != rcfg.get("require_seller_display_name", "Walmart.com"):
        problems.append(f"seller {seller}")
    if item.get("marketplace") is True:
        problems.append("marketplace (third-party) offer")
    if problems:
        res.reason = "not the exact product/offer: " + "; ".join(problems)
        return res
    res.model_verified = True

    price = item.get("salePrice")
    if not isinstance(price, (int, float)) or isinstance(price, bool) or not (20 <= float(price) <= 5000):
        res.reason = f"invalid/missing salePrice {price!r}"
        return res
    res.price = round(float(price), 2)

    stock = str(item.get("stock") or "")
    orderable_attr = ((item.get("attributes") or {}).get("isOrderable"))
    available_online = item.get("availableOnline")
    available = stock.lower() in ("available", "limited supply", "last few items") \
        and available_online is not False and orderable_attr not in ("N", "n", False)
    raw = stock or "UNKNOWN"
    res.channels = [parse.Channel("SHIPPING", raw, f"ZIP {cfg['target_zip']} (Walmart API)", True, available,
                                  "" if orderable_attr is not None else "isOrderable not provided")]
    if not stock:
        res.reason = "API response has no stock field"
        return res
    if available:
        if res.price > float(cfg["max_price"]) + 1e-9:
            res.state, res.reason = parse.PRICE_ABOVE_TARGET, f"${res.price:.2f} > max ${float(cfg['max_price']):.2f}"
        else:
            res.state, res.reason = parse.IN_STOCK, f"API: stock={stock}, availableOnline={available_online}, isOrderable={orderable_attr}"
        return res
    res.state = parse.OUT_OF_STOCK
    res.reason = f"API: stock={stock}, availableOnline={available_online}, isOrderable={orderable_attr} for ZIP {cfg['target_zip']}"
    return res


def check(cfg: dict) -> parse.Result:
    status, payload, err = request(cfg["retailers"]["walmart"]["item_id"], cfg["target_zip"])
    return evaluate(payload, status, err, cfg)
