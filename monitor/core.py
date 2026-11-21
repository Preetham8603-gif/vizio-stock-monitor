"""One monitoring cycle: fetch → verify → compare with saved state → notify."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from . import parse
from .notify import NotifyError

log = logging.getLogger("monitor.core")

# Arizona does not observe DST, so MST (UTC-7) is exact all year.
AZ = timezone(timedelta(hours=-7), "MST")


def az(dt: datetime) -> str:
    return dt.astimezone(AZ).strftime("%a %b %d, %Y %I:%M %p MST (Arizona)")


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def money(p: float | None) -> str:
    if p is None:
        return "n/a"
    return f"${p:,.0f}" if abs(p - round(p)) < 0.005 else f"${p:,.2f}"


def stock_alert(cfg: dict, key: str, r: parse.Result, now: datetime) -> str:
    rcfg = cfg["retailers"][key]
    lines = [
        "VIZIO TV BACK IN STOCK!",
        f"Retailer: {r.retailer}",
        f"Model: {cfg['expected_model']}",
        f"Price: {money(r.price)}",
        "Availability: Confirmed",
        "Fulfillment: " + "; ".join(c.describe(cfg["target_zip"]) for c in r.qualifying_channels),
        f"Checked: {az(now)}",
    ]
    if r.membership_required and rcfg.get("membership_note"):
        lines.append(f"Note: {rcfg['membership_note']}")
    lines.append(f"BUY NOW: {rcfg['buy_url']}")
    return "\n".join(lines)


def blank_retailer_state() -> dict:
    return {"state": None, "reason": "", "price": None, "last_checked": None, "last_healthy": None,
            "last_change": None, "consecutive_failures": 0, "failure_alerted": False,
            "alerted_methods": [], "channels": [], "location_postal": None}


def run_cycle(cfg: dict, state: dict, fetcher, notifier, now: datetime | None = None,
              checkers: dict | None = None) -> tuple[dict, list[str], int]:
    """Returns (new_state, log_lines, exit_code). exit_code=1 only when an
    alert that should have been delivered could not be delivered."""
    now = now or datetime.now(timezone.utc)
    out: list[str] = []
    delivery_failed = False
    state.setdefault("retailers", {})
    state.setdefault("runs", [])
    stats = state.setdefault("stats", {"alerts_sent": 0, "alert_failures": 0, "max_gap_minutes_24h": 0, "gaps": []})

    def send(text: str, silent: bool = False, critical: bool = False) -> bool:
        nonlocal delivery_failed
        try:
            mid = notifier.send(text, silent=silent)
            out.append(f"  telegram: sent message {mid}{' (silent)' if silent else ''}")
            return True
        except NotifyError as e:
            out.append(f"  telegram: FAILED: {e}")
            stats["alert_failures"] = stats.get("alert_failures", 0) + 1
            if critical:
                delivery_failed = True
            return False

    # ---- schedule-gap detection (scheduler delays / outages) --------------
    last_run = parse_iso(state.get("last_run"))
    if last_run:
        gap = (now - last_run).total_seconds() / 60
        if gap > cfg.get("schedule_gap_warning_minutes", 45):
            stats.setdefault("gaps", []).append({"at": iso(now), "minutes": round(gap)})
            out.append(f"WARNING: {gap:.0f} min since previous run (scheduler delay or outage)")
            if gap > 120:
                send(f"⚠️ VIZIO monitor: no check ran for {gap:.0f} minutes (GitHub scheduler delay/outage). "
                     f"Monitoring has resumed as of {az(now)}.", silent=True)
    stats["gaps"] = [g for g in stats.get("gaps", []) if parse_iso(g["at"]) > now - timedelta(days=1)]

    run_entry = {"t": iso(now)}
    for key, rcfg in cfg["retailers"].items():
        prev = state["retailers"].get(key) or blank_retailer_state()
        min_gap = rcfg.get("min_interval_minutes") or 0
        last_checked = parse_iso(prev.get("last_checked"))
        if min_gap and last_checked and (now - last_checked) < timedelta(minutes=min_gap - 2):
            out.append(f"[{rcfg['name']}] skipped (checked {((now - last_checked).total_seconds() / 60):.0f} min ago; "
                       f"interval {min_gap} min) – last state {prev.get('state')}")
            run_entry[key] = "SKIPPED"
            continue

        via = "page"
        if checkers and key in checkers:
            via = "api"
            try:
                r = checkers[key](cfg)
            except Exception as e:  # never let one retailer kill the cycle
                r = parse.Result(rcfg["name"], parse.ERROR, f"{type(e).__name__}: {e}")
            f = None
        else:
            f = fetcher(rcfg["url"])
            if f.body or f.status:
                r = parse.evaluate(key, f.body, cfg, f.status)
            else:
                r = parse.Result(rcfg["name"], parse.ERROR, f"fetch failed: {f.error}")
            if r.state in parse.HEALTHY_STATES and f.stale_seconds is not None and f.stale_seconds > 900:
                r = parse.Result(rcfg["name"], parse.UNKNOWN, f"response is stale ({f.stale_seconds:.0f}s old)")

        out.append(f"[{rcfg['name']}] {r.state} price={money(r.price)} loc={r.location_postal} stores={r.location_store_ids} "
                   f"via={via} http={r.http_status if f is None else f.status} :: {r.reason}")
        for c in r.channels:
            out.append(f"    {c.method:<8} {c.raw_status:<14} verified_location={c.location_verified} '{c.location_text}' {c.note}")

        cur = dict(prev)
        cur.update({"state": r.state, "reason": r.reason, "last_checked": iso(now),
                    "location_postal": r.location_postal,
                    "channels": [{"method": c.method, "status": c.raw_status, "verified": c.location_verified,
                                  "where": c.location_text} for c in r.channels]})
        if r.price is not None:
            cur["price"] = r.price
        if r.state != prev.get("state"):
            cur["last_change"] = iso(now)

        healthy = r.state in parse.HEALTHY_STATES
        if healthy:
            cur["last_healthy"] = iso(now)
            if prev.get("failure_alerted"):
                send(f"✅ VIZIO monitor recovered: {rcfg['name']} checks are working again ({az(now)}). Current status: {r.state}.", silent=True)
            cur["consecutive_failures"] = 0
            cur["failure_alerted"] = False
        else:
            cur["consecutive_failures"] = prev.get("consecutive_failures", 0) + 1
            n = cur["consecutive_failures"]
            if n >= cfg.get("failure_alert_after_consecutive", 3) and not prev.get("failure_alerted"):
                if send(f"⚠️ VIZIO monitor problem: {rcfg['name']} could not be checked reliably for {n} runs in a row.\n"
                        f"Status: {r.state}\nReason: {r.reason}\nSince: {az(parse_iso(prev.get('last_healthy')) or now)}\n"
                        f"Stock alerts for {rcfg['name']} cannot fire until this clears."):
                    cur["failure_alerted"] = True

        # ---- stock alerts with de-duplication ----------------------------
        alerted = set(prev.get("alerted_methods") or [])
        if r.state == parse.IN_STOCK:
            methods = {c.method for c in r.qualifying_channels}
            if not methods <= alerted:  # something new became available
                if send(stock_alert(cfg, key, r, now), critical=True):
                    cur["alerted_methods"] = sorted(alerted | methods)
                    stats["alerts_sent"] = stats.get("alerts_sent", 0) + 1
                else:
                    cur["alerted_methods"] = sorted(alerted)  # retry next run
            else:
                out.append("  duplicate in-stock observation: alert already sent, suppressed")
        elif r.state in (parse.OUT_OF_STOCK, parse.PRICE_ABOVE_TARGET):
            if alerted:
                send(f"VIZIO TV no longer available at {rcfg['name']} ({r.state.replace('_', ' ').lower()}; {money(r.price)}). "
                     f"Re-armed: you'll get a new alert if it comes back. {az(now)}")
            cur["alerted_methods"] = []
            if prev.get("state") in parse.HEALTHY_STATES and cfg.get("notify_price_changes", True):
                if prev.get("state") != r.state:
                    send(f"ℹ️ {rcfg['name']} VIZIO status changed: {prev['state']} → {r.state} ({money(r.price)}). {r.reason}", silent=True)
                elif prev.get("price") is not None and r.price is not None and abs(prev["price"] - r.price) > 0.005:
                    send(f"ℹ️ {rcfg['name']} VIZIO price changed {money(prev['price'])} → {money(r.price)} (still {r.state.replace('_', ' ').lower()}).", silent=True)
        # UNKNOWN / BLOCKED / ERROR: never alert, never re-arm (keeps dedupe intact across blips)

        state["retailers"][key] = cur
        run_entry[key] = r.state

    state["last_run"] = iso(now)
    state["runs"] = (state["runs"] + [run_entry])[-cfg.get("history_length", 170):]
    return state, out, 1 if delivery_failed else 0


def daily_summary(cfg: dict, state: dict, now: datetime) -> str:
    runs = [r for r in state.get("runs", []) if parse_iso(r["t"]) > now - timedelta(hours=24)]
    lines = [f"📋 VIZIO monitor daily health – {now.astimezone(AZ).strftime('%a %b %d, %Y')}",
             f"Checks in last 24h: {len(runs)} (expected ~144 at 10-min spacing)"]
    for key, rcfg in cfg["retailers"].items():
        rs = state.get("retailers", {}).get(key) or {}
        attempted = [r for r in runs if r.get(key) not in (None, "SKIPPED")]
        ok = sum(1 for r in attempted if r.get(key) in parse.HEALTHY_STATES)
        lines.append(f"• {rcfg['name']}: {rs.get('state')} at {money(rs.get('price'))} – {ok}/{len(attempted)} checks verified"
                     f"; last verified {az(parse_iso(rs['last_healthy'])) if rs.get('last_healthy') else 'never'}")
    gaps = state.get("stats", {}).get("gaps", [])
    if gaps:
        lines.append(f"Scheduler gaps >45 min: {len(gaps)} (longest {max(g['minutes'] for g in gaps)} min)")
    lines.append(f"Max price: {money(float(cfg['max_price']))} · ZIP {cfg['target_zip']} · alerts sent to date: {state.get('stats', {}).get('alerts_sent', 0)}")
    return "\n".join(lines)


def maybe_daily_summary(cfg: dict, state: dict, notifier, now: datetime, force: bool = False) -> list[str]:
    local = now.astimezone(AZ)
    today = local.strftime("%Y-%m-%d")
    if not force and (local.hour < cfg.get("daily_summary_hour_local", 8) or state.get("last_summary_date") == today):
        return []
    try:
        mid = notifier.send(daily_summary(cfg, state, now), silent=True)
        state["last_summary_date"] = today
        return [f"daily summary sent (message {mid})"]
    except NotifyError as e:
        return [f"daily summary FAILED: {e}"]
