"""
report.py — สรุปผล backtest

นับ "1 เทรด" = 1 สัญญาณ (group) แม้จะแตกเป็น partial + runner
R ของกลุ่ม = กำไรรวมของกลุ่ม / เงินที่เสี่ยงรวมของกลุ่ม

ตัวเลขที่ควรดูก่อนเชื่อผล:
  - trades       : น้อยกว่า ~100 ต่อกลยุทธ์ = ยังสรุปอะไรไม่ได้
  - expectancy R : ค่าเฉลี่ยต่อไม้ หลังหักต้นทุนแล้ว ต้อง > 0 ชัดเจน
  - max DD       : เตรียมใจไว้ว่าของจริงจะแย่กว่านี้ ~1.5–2 เท่า
  - รายปี         : ถ้ากำไรมาจากปีเดียว = โชค ไม่ใช่ edge
"""
from __future__ import annotations

import json
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path

import pandas as pd

from ..core.types import ClosedTrade
from .runner import BacktestResult


@dataclass
class GroupTrade:
    group_id: str
    tag: str
    symbol: str
    side: str
    open_time: str
    close_time: str
    pnl: float
    risk: float
    r: float
    exits: str


@dataclass
class Metrics:
    trades: int
    win_rate: float
    avg_win_r: float
    avg_loss_r: float
    expectancy_r: float
    total_r: float
    profit_factor: float
    net_pnl: float
    max_consec_losses: int
    best_r: float
    worst_r: float


def group_trades(trades: list[ClosedTrade]) -> list[GroupTrade]:
    groups: dict[str, list[ClosedTrade]] = defaultdict(list)
    for t in trades:
        groups[t.group_id].append(t)
    out = []
    for gid, ts in groups.items():
        pnl = sum(t.pnl for t in ts)
        risk = sum(t.risk_amount for t in ts)
        out.append(GroupTrade(
            group_id=gid, tag=ts[0].tag, symbol=ts[0].symbol, side=ts[0].side.name,
            open_time=min(t.open_time for t in ts).isoformat(), close_time=max(t.close_time for t in ts).isoformat(),
            pnl=round(pnl, 2), risk=round(risk, 2), r=round(pnl / risk, 3) if risk > 0 else 0.0,
            exits="+".join(f"{t.role}:{t.exit_reason}" for t in sorted(ts, key=lambda t: t.role)),
        ))
    out.sort(key=lambda g: g.open_time)
    return out


def metrics(groups: list[GroupTrade]) -> Metrics:
    if not groups:
        return Metrics(0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0)
    rs = [g.r for g in groups]
    wins = [r for r in rs if r > 0]
    losses = [r for r in rs if r <= 0]
    gross_win = sum(g.pnl for g in groups if g.pnl > 0)
    gross_loss = -sum(g.pnl for g in groups if g.pnl <= 0)
    streak = worst = 0
    for r in rs:
        streak = streak + 1 if r <= 0 else 0
        worst = max(worst, streak)
    return Metrics(
        trades=len(groups),
        win_rate=round(len(wins) / len(groups), 3),
        avg_win_r=round(sum(wins) / len(wins), 2) if wins else 0.0,
        avg_loss_r=round(sum(losses) / len(losses), 2) if losses else 0.0,
        expectancy_r=round(sum(rs) / len(rs), 3),
        total_r=round(sum(rs), 1),
        profit_factor=round(gross_win / gross_loss, 2) if gross_loss > 0 else float("inf"),
        net_pnl=round(sum(g.pnl for g in groups), 2),
        max_consec_losses=worst,
        best_r=round(max(rs), 2),
        worst_r=round(min(rs), 2),
    )


def max_drawdown(equity: list[tuple]) -> float:
    peak, dd = float("-inf"), 0.0
    for _, e in equity:
        peak = max(peak, e)
        dd = max(dd, 1 - e / peak)
    return dd


def build_report(res: BacktestResult) -> dict:
    groups = group_trades(res.trades)
    by_tag: dict[str, list[GroupTrade]] = defaultdict(list)
    for g in groups:
        by_tag[g.tag].append(g)

    yearly: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for g in groups:
        yearly[g.tag][g.open_time[:4]] += g.r

    skipped = Counter(s.outcome.split(":")[0] for s in res.stats.signals if s.outcome != "sent")
    return {
        "initial_balance": res.initial_balance,
        "final_balance": round(res.final_balance, 2),
        "return_pct": round((res.final_balance / res.initial_balance - 1) * 100, 2),
        "max_drawdown_pct": round(max_drawdown(res.equity) * 100, 2),
        "period": [res.equity[0][0].isoformat(), res.equity[-1][0].isoformat()] if res.equity else None,
        "killed": res.killed,
        "overall": asdict(metrics(groups)),
        "by_strategy": {tag: asdict(metrics(gs)) for tag, gs in sorted(by_tag.items())},
        "r_by_year": {tag: {y: round(v, 1) for y, v in sorted(ys.items())} for tag, ys in sorted(yearly.items())},
        "exit_reasons": dict(Counter(t.exit_reason for t in res.trades)),
        "signals_not_sent": dict(skipped),
        "risk_events": [f"{t.isoformat()} {e}" for t, e in res.stats.events],
    }


def print_report(rep: dict) -> None:
    print()
    print(f"ช่วงเวลา      : {rep['period'][0][:10]} → {rep['period'][1][:10]}" if rep["period"] else "ไม่มีข้อมูล")
    print(f"ทุน           : {rep['initial_balance']:,.2f} → {rep['final_balance']:,.2f}  ({rep['return_pct']:+.2f}%)")
    print(f"Max drawdown  : {rep['max_drawdown_pct']:.2f}%")
    if rep["killed"]:
        print(f"!! KILL SWITCH: {rep['killed']}")
    print()
    cols = ["trades", "win_rate", "expectancy_r", "total_r", "profit_factor", "avg_win_r", "avg_loss_r",
            "max_consec_losses", "net_pnl"]
    rows = {**rep["by_strategy"], "== รวม ==": rep["overall"]}
    df = pd.DataFrame(rows).T[cols]
    with pd.option_context("display.width", 200, "display.max_columns", 20):
        print(df.to_string())
    print()
    if rep["r_by_year"]:
        print("R รายปี (ต้องเป็นบวกหลายปี ไม่ใช่ปีเดียว):")
        print(pd.DataFrame(rep["r_by_year"]).fillna(0).to_string())
        print()
    print("เหตุผลที่ปิดไม้   :", rep["exit_reasons"])
    print("สัญญาณที่ไม่ได้ส่ง:", rep["signals_not_sent"])
    if rep["risk_events"]:
        print(f"risk events     : {len(rep['risk_events'])} ครั้ง (ดูใน summary.json)")


def save_report(res: BacktestResult, rep: dict, out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    (out / "summary.json").write_text(json.dumps(rep, indent=2, ensure_ascii=False), encoding="utf-8")
    pd.DataFrame([asdict(g) for g in group_trades(res.trades)]).to_csv(out / "trades.csv", index=False)
    eq = pd.DataFrame(res.equity, columns=["time", "equity"])
    eq.to_csv(out / "equity.csv", index=False)
    pd.DataFrame([asdict(s) for s in res.stats.signals]).to_csv(out / "signals.csv", index=False)
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        return
    fig, ax = plt.subplots(figsize=(12, 5))
    ax.plot(eq["time"], eq["equity"], linewidth=1)
    ax.set_title(f"Equity  return {rep['return_pct']:+.1f}%  maxDD {rep['max_drawdown_pct']:.1f}%")
    ax.grid(alpha=0.3)
    fig.tight_layout()
    fig.savefig(out / "equity.png", dpi=110)
    plt.close(fig)
