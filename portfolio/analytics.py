from __future__ import annotations

from .finance import external_cashflows


def portfolio_history_series(conn) -> dict:
    """Return complete snapshot history with cumulative net external capital.

    Net contributions are deposits minus withdrawals, valued in PLN using the
    transaction's saved FX rate or the historical rate resolver used by XIRR.
    If any external flow lacks an FX rate, the second series is withheld rather
    than displaying a misleadingly low amount.
    """
    snapshots = conn.execute("""SELECT snapshot_date,
        COALESCE(manual_total_value_pln,total_value_pln) AS portfolio_value_pln
        FROM portfolio_snapshots
        WHERE valuation_complete=1 OR manual_total_value_pln IS NOT NULL
        ORDER BY snapshot_date""").fetchall()
    required_flows = int(conn.execute("SELECT COUNT(*) FROM transactions WHERE type IN ('DEPOSIT','WITHDRAWAL')").fetchone()[0])
    flows = external_cashflows(conn)
    if len(flows) < required_flows:
        return {"series": [], "complete": False,
                "message": "Brakuje historycznego kursu waluty dla co najmniej jednej wpłaty lub wypłaty."}

    daily_net: dict[str, float] = {}
    for flow_day, signed_flow in flows:
        day = flow_day[:10]
        daily_net[day] = daily_net.get(day, 0.0) - float(signed_flow)
    flow_days = sorted(daily_net.items())

    result = []
    running = 0.0
    flow_index = 0
    for snap in snapshots:
        snap_day = snap["snapshot_date"]
        while flow_index < len(flow_days) and flow_days[flow_index][0] <= snap_day:
            running += flow_days[flow_index][1]
            flow_index += 1
        result.append({"snapshot_date": snap_day,
                       "portfolio_value_pln": snap["portfolio_value_pln"],
                       "net_contributions_pln": running})
    return {"series": result, "complete": True, "message": ""}
