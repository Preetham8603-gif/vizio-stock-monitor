"""CLI entry point.

  python -m monitor check          run one monitoring cycle (default)
  python -m monitor check --dry-run   fetch + evaluate, print, no Telegram, no state write
  python -m monitor test-notify    send a Telegram test message
  python -m monitor summary        send the daily health summary now
  python -m monitor status         print saved state
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import core
from .fetch import fetch
from .notify import NotifyError, Telegram

ROOT = Path(__file__).resolve().parent.parent


def load_config(path: Path) -> dict:
    cfg = json.loads(path.read_text())
    if os.environ.get("MAX_PRICE"):
        cfg["max_price"] = float(os.environ["MAX_PRICE"])
    return cfg


def load_state(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError:
        logging.error("state file corrupt; starting fresh (dedupe history lost)")
        return {}


def save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=1, sort_keys=True) + "\n")
    tmp.replace(path)


class DryRunNotifier:
    def send(self, text, silent=False):
        print("---- would send to Telegram" + (" (silent)" if silent else "") + " ----\n" + text + "\n----")
        return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="monitor")
    ap.add_argument("command", nargs="?", default="check", choices=["check", "test-notify", "summary", "status"])
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--config", default=str(ROOT / "config.json"))
    ap.add_argument("--state", default=str(ROOT / "state" / "state.json"))
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    cfg = load_config(Path(args.config))
    state_path = Path(args.state)
    state = load_state(state_path)
    now = datetime.now(timezone.utc)
    notifier = DryRunNotifier() if args.dry_run else Telegram()

    if args.command == "status":
        print(json.dumps({k: v for k, v in state.items() if k != "runs"}, indent=1))
        print("recent runs:")
        for r in state.get("runs", [])[-12:]:
            print("  ", r)
        return 0

    if args.command == "test-notify":
        msg = (f"✅ VIZIO stock monitor test notification\nWatching: {cfg['expected_model']} at "
               + ", ".join(r["name"] for r in cfg["retailers"].values())
               + f"\nMax price: {core.money(float(cfg['max_price']))} · ZIP {cfg['target_zip']}\nSent: {core.az(now)}")
        try:
            mid = notifier.send(msg)
        except NotifyError as e:
            print(f"Telegram test FAILED: {e}")
            return 1
        print(f"Telegram test delivered (message_id={mid})")
        return 0

    if args.command == "summary":
        for line in core.maybe_daily_summary(cfg, state, notifier, now, force=True):
            print(line)
        if not args.dry_run:
            save_state(state_path, state)
        return 0

    # check
    if not args.dry_run and not getattr(notifier, "configured", True):
        print("ERROR: Telegram secrets missing; refusing to run a live cycle that could not alert.")
        return 1
    state, lines, code = core.run_cycle(cfg, state, fetch, notifier, now)
    for line in lines:
        print(line)
    for line in core.maybe_daily_summary(cfg, state, notifier, now):
        print(line)
    if not args.dry_run:
        save_state(state_path, state)
    if code:
        print("ERROR: a stock alert could not be delivered; it will be retried next run.")
    return code


if __name__ == "__main__":
    sys.exit(main())
