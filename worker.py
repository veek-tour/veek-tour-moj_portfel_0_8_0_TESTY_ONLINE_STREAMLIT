from __future__ import annotations

import argparse
import time
from datetime import date

from portfolio.db import init_db, connect, backup_db_if_due
from portfolio.market import sync_market_data
from portfolio.snapshots import save_snapshot, fill_missing_snapshots
from portfolio.benchmark import sync_spy


def run_once(full_history: bool = False) -> dict:
    init_db()
    backup_db_if_due()
    with connect() as conn:
        result = sync_market_data(conn, days_back=14, full_history=full_history)
        try:
            result["spy_prices_saved"] = sync_spy(conn)
        except Exception as exc:
            result["errors"].append(f"Benchmark SPY: {exc}")
        snapshot = save_snapshot(conn, date.today().isoformat())
        result["snapshots_backfilled"] = fill_missing_snapshots(conn)
        result["snapshot_value_pln"] = snapshot["total_value_pln"]
        result["missing_prices"] = snapshot["missing_prices"]
        return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Aktualizacja wycen i migawki portfela")
    parser.add_argument("--once", action="store_true", help="Wykonaj jedną aktualizację")
    parser.add_argument("--full-history", action="store_true", help="Spróbuj pobrać pełną dostępną historię notowań")
    parser.add_argument("--interval-hours", type=float, default=6.0, help="Odstęp pracy w trybie ciągłym (krypto co 6h, pozostałe aktywa nie częściej niż raz dziennie)")
    args = parser.parse_args()
    while True:
        try:
            print(run_once(args.full_history), flush=True)
        except Exception as exc:
            # A bad historical operation or a temporary database/source issue
            # must not permanently kill the background updater.
            print({"worker_error": str(exc)}, flush=True)
        if args.once:
            break
        time.sleep(max(1.0, args.interval_hours * 3600))
