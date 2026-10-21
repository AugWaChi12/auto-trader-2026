"""
live_report.py — รายงานวิจัยจากไม้จริง (สร้างใหม่ทุกคืนหลังสรุปวัน · ส่ง Telegram ทุกวันอาทิตย์)

ตอบ 5 คำถามด้วยตัวเลข + ช่วงความเชื่อมั่น 95% (ไม่ใช่แค่ยอดรวม):
  1) ภาพรวม: ได้/เสียต่อไม้เท่าไหร่ ต่างจาก 0 อย่างมีนัยไหม
  2) แต่ละสาย: ผลจริงตรงกับที่งานวิจัยคาดไหม (live vs walk-forward $/ไม้)
  3) ตลาดแบบไหน / ช่วงเวลาไหน / ตามหรือสวนเทรนด์ ได้-เสีย
  4) ตัวกรองโหมดสังเกต: ไม้ที่ "จะบล็อก" แย่กว่าไม้อื่นจริงไหม (A/B บนไม้จริง)
  5) คำทำนายของกฎ vs Gemini: ทายถูกกี่ % (ช่วงความเชื่อมั่นแบบ Wilson)
→ state/live_report.md (หน้า /report) + state/live_report.json (หน้า 📈 ผลงาน)
"""
from __future__ import annotations

import json
import math
from collections import defaultdict
from datetime import datetime, timezone

from . import config as C
from . import allocator, close_score, dataset, shadow

MD_PATH = C.STATE_DIR / "live_report.md"
JSON_PATH = C.STATE_DIR / "live_report.json"
TH = {"trend": "เทรนด์", "range": "ออกข้าง", "mixed": "ไม่ชัดทาง", "asia": "เอเชีย", "europe": "ยุโรป", "us": "อเมริกา",
      "with": "ตามเทรนด์", "against": "สวนเทรนด์", "flat": "ไม่มีเทรนด์"}


def stats(x: list[float]) -> dict:
    n = len(x)
    if n == 0:
        return {"n": 0}
    m = sum(x) / n
    sd = math.sqrt(sum((v - m) ** 2 for v in x) / (n - 1)) if n > 1 else 0.0
    half = 1.96 * sd / math.sqrt(n) if n > 1 else None
    return {"n": n, "total": round(sum(x), 2), "avg": round(m, 2), "win": round(sum(v > 0 for v in x) / n * 100),
            "ci": [round(m - half, 2), round(m + half, 2)] if half is not None else None,
            "sig": "บวกชัด" if half is not None and m - half > 0 else "ลบชัด" if half is not None and m + half < 0 else "ยังไม่ชัด"}


def wilson(k: int, n: int) -> list[float] | None:
    if n == 0:
        return None
    p, z = k / n, 1.96
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return [round((c - h) * 100), round((c + h) * 100)]


def build() -> dict:
    rows = dataset.load()
    assigned = {b["bot"]: b for b in C.load_m5_bots()}
    out: dict = {"generated": datetime.now(timezone.utc).isoformat(timespec="minutes"), "trades": len(rows)}
    if rows:
        out["period"] = [min(r["open_time"] for r in rows)[:10], max(r["open_time"] for r in rows)[:10]]
    out["unit"] = "lot มาตรฐาน (ทอง 0.04 · NAS100 0.73 · BTC 0.17)"
    out["real_usd_total"] = round(sum(r["profit"] for r in rows), 2)
    out["overall"] = stats([r["profit_std"] for r in rows])
    fam: dict[str, list[float]] = defaultdict(list)
    for r in rows:
        fam[r["family"]].append(r["profit_std"])
    exp = {b["family"]: b.get("wf_usd_trade") for b in assigned.values()}
    out["by_family"] = {f: {**stats(x), "research_usd_trade": exp.get(f)} for f, x in sorted(fam.items())}
    for dim in ("regime", "session", "trend"):
        g: dict[str, list[float]] = defaultdict(list)
        for r in rows:
            if r.get(dim):
                g[r[dim]].append(r["profit_std"])
        out[f"by_{dim}"] = {k: stats(v) for k, v in sorted(g.items())}
    gates = {}
    for r in rows:
        if r["gate_mode"] == "observe" and r["gate_sig"]:
            gates.setdefault((r["bot"], r["gate_sig"]), {"flag": [], "other": []})["flag" if r["gate_flag"] else "other"].append(r["profit_std"])
    out["gates"] = [{"bot": b, "gate": sig, "would_block": stats(v["flag"]), "kept": stats(v["other"])}
                    for (b, sig), v in sorted(gates.items())]
    sb = shadow.scoreboard(days=3650)
    out["predictions"] = {src: {**x, "accuracy": round(x["right"] / x["calls"] * 100) if x["calls"] else None,
                                "ci": wilson(x["right"], x["calls"])} for src, x in sb.items()}
    bots: dict[str, list[float]] = defaultdict(list)
    for r in rows:
        bots[r["bot"]].append(r["profit_std"])
    out["by_bot"] = {b: stats(x) for b, x in sorted(bots.items())}
    out["closes"] = close_score.scoreboard(days=3650)
    out["alloc"] = allocator.summary()
    out["conclusions"] = conclusions(out)
    return out


def conclusions(r: dict) -> list[str]:
    c = []
    o = r["overall"]
    if o.get("n", 0) < 30:
        c.append(f"ไม้จริงยังน้อย ({o.get('n', 0)} ไม้) — ตัวเลขทุกส่วนยังเป็นเรื่องดวงได้มาก ต้องสะสมอย่างน้อย ~100 ไม้")
    elif o["sig"] == "ลบชัด":
        c.append(f"ภาพรวมขาดทุนอย่างมีนัย ({o['avg']:+.2f}$/ไม้, ช่วง 95% {o['ci'][0]:+.2f} ถึง {o['ci'][1]:+.2f}) — ยังไม่มี edge")
    elif o["sig"] == "บวกชัด":
        c.append(f"ภาพรวมกำไรอย่างมีนัย ({o['avg']:+.2f}$/ไม้) — น่าสนใจ แต่ต้องยืนยันต่อเนื่องอีกหลายสัปดาห์")
    else:
        c.append(f"ภาพรวม {o['avg']:+.2f}$/ไม้ แต่ยังแยกจาก 0 ไม่ได้ทางสถิติ")
    for f, s in r["by_family"].items():
        e = s.get("research_usd_trade")
        if s.get("n", 0) >= 30 and e is not None and s.get("ci") and not (s["ci"][0] <= e <= s["ci"][1]):
            c.append(f"{f}: ผลจริง {s['avg']:+.2f}$/ไม้ ต่างจากที่งานวิจัยคาด ({e:+.2f}) อย่างมีนัย — ตลาดจริงไม่เหมือนข้อมูลทดสอบ")
    for g in r["gates"]:
        wb, kp = g["would_block"], g["kept"]
        if wb.get("n", 0) >= 10 and kp.get("n", 0) >= 10:
            better = wb["avg"] < kp["avg"]
            c.append(f"ตัวกรอง {g['bot']} ({g['gate']}): ไม้ที่จะบล็อก {wb['avg']:+.2f}$/ไม้ vs ไม้อื่น {kp['avg']:+.2f}$/ไม้ — "
                     + ("ไปทางที่คาด ✅" if better else "ไม่เป็นอย่างที่คาด ❌"))
    for src, x in r.get("closes", {}).items():
        if x["n"] >= 10:
            c.append(f"การสั่งปิดไม้ของ {close_score.WHO.get(src, src)}: {x['n']} ครั้ง ดีกว่าปล่อย {x['helped']} ครั้ง · "
                     f"รวม {x['saved']:+.2f}$ " + ("— ช่วยพอร์ต ✅" if x["saved"] > 0 else "— ปิดเร็วไป ❌"))
    for src, p in r["predictions"].items():
        if p["calls"] >= 20 and p["ci"]:
            c.append(f"คำทำนายของ {src}: ถูก {p['accuracy']}% (95% ช่วง {p['ci'][0]}–{p['ci'][1]}%) จาก {p['calls']} ครั้ง"
                     + (" — ดีกว่าโยนเหรียญ" if p["ci"][0] > 50 else " — ยังไม่ต่างจากโยนเหรียญ"))
    return c


def _fmt(s: dict) -> str:
    if not s.get("n"):
        return "–"
    ci = f" [{s['ci'][0]:+.2f}, {s['ci'][1]:+.2f}]" if s.get("ci") else ""
    return f"{s['n']} ไม้ · {s['avg']:+.2f}$/ไม้{ci} · ชนะ {s['win']}% · รวม {s['total']:+.2f}$"


def markdown(r: dict) -> str:
    L = [f"# รายงานวิจัยจากไม้จริง (บัญชี DEMO)", "",
         f"สร้างเมื่อ {r['generated']} UTC · ไม้ทั้งหมด {r['trades']}" + (f" · ช่วง {r['period'][0]} ถึง {r['period'][1]}" if r.get("period") else ""),
         "", f"_ตัวเลข $/ไม้ ทุกส่วนคิดเป็น {r.get('unit', 'lot มาตรฐาน')} · เงินจริงรวม {r.get('real_usd_total', 0):+.2f}$_",
         "", "## ข้อสรุป", *[f"- {x}" for x in r["conclusions"]], "", "## ภาพรวม", f"- {_fmt(r['overall'])}", "",
         "## แยกตามสายกลยุทธ์ (ผลจริง vs งานวิจัยคาด)"]
    for f, s in r["by_family"].items():
        e = s.get("research_usd_trade")
        L.append(f"- **{f}**: {_fmt(s)}" + (f" · งานวิจัยคาด {e:+.2f}$/ไม้" if e is not None else ""))
    for dim, title in (("regime", "สภาพตลาดตอนเปิดไม้"), ("session", "ช่วงเวลา (UTC)"), ("trend", "ทิศเทียบเทรนด์ 2 ชม.")):
        L += ["", f"## แยกตาม{title}", *[f"- {TH.get(k, k)}: {_fmt(s)}" for k, s in r[f"by_{dim}"].items()]]
    L += ["", "## ตัวกรองโหมดสังเกต (A/B บนไม้จริง)"]
    L += [f"- {g['bot']} `{g['gate']}`: ไม้ที่จะบล็อก {_fmt(g['would_block'])} · ไม้อื่น {_fmt(g['kept'])}" for g in r["gates"]] or ["- ยังไม่มี"]
    L += ["", "## การสั่งปิดไม้ของผู้จัดการ (เทียบกับปล่อยจนชน SL/TP หรือครบ 24 ชม.)"]
    L += [f"- {close_score.WHO.get(src, src)}: {x['n']} ครั้ง · ดีกว่าปล่อย {x['helped']} · แย่กว่า {x['hurt']} · รวม {x['saved']:+.2f}$"
          for src, x in r.get("closes", {}).items()] or ["- ยังไม่มี"]
    L += ["", "## ขนาดไม้ที่ผู้จัดการตั้ง"]
    L += [f"- {b}: ×{a['scale']:g} ({a.get('by')}) — {a.get('reason')}" for b, a in sorted(r.get("alloc", {}).items())
          if a.get("scale") not in (None, 1.0)] or ["- ทุกตัวขนาดมาตรฐาน"]
    L += ["", "## คำทำนายของผู้จัดการ"]
    L += [f"- {src}: ถูก {p['right']}/{p['calls']}" + (f" ({p['accuracy']}%, 95% {p['ci'][0]}–{p['ci'][1]}%)" if p["ci"] else "")
          + f" · ถ้าเชื่อทั้งหมด {-p['missed']:+.2f}$" for src, p in r["predictions"].items()] or ["- ยังไม่มี"]
    L += ["", "_ช่วง [ล่าง, บน] = ช่วงความเชื่อมั่น 95% ของ $/ไม้ — ถ้าคร่อม 0 แปลว่ายังสรุปไม่ได้ว่าได้หรือเสียจริง_"]
    return "\n".join(L) + "\n"


def write() -> dict:
    from .live.fileio import atomic_write
    r = build()
    atomic_write(JSON_PATH, json.dumps(r, ensure_ascii=False, indent=1))
    atomic_write(MD_PATH, markdown(r))
    return r


def load() -> dict:
    try:
        return json.loads(JSON_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def telegram_summary(r: dict) -> str:
    o = r["overall"]
    bots = sorted(((b, s) for b, s in r.get("by_bot", {}).items() if s.get("n")), key=lambda x: x[1]["total"])
    L = [f"📑 รายงานวิจัยประจำสัปดาห์ · ไม้จริง {r['trades']} ไม้ · เงินจริงรวม {r.get('real_usd_total', 0):+.2f}$",
         f"ต่อไม้ (lot มาตรฐาน): {_fmt(o)}"]
    if bots:
        L.append("🏆 ดีสุด: " + ", ".join(f"{b} {s['total']:+.0f}$" for b, s in bots[::-1][:3])
                 + " · 🐢 แย่สุด: " + ", ".join(f"{b} {s['total']:+.0f}$" for b, s in bots[:3]))
    preds = [f"{shadow.WHO.get(s, s)} {p['right']}/{p['calls']}" for s, p in r.get("predictions", {}).items() if p.get("calls")]
    if preds:
        L.append("🔮 คำทำนายถูก: " + " · ".join(preds))
    closes = [f"{close_score.WHO.get(s, s)} {x['saved']:+.0f}$ ({x['n']} ครั้ง)" for s, x in r.get("closes", {}).items()]
    if closes:
        L.append("✋ สั่งปิดไม้ดีกว่าปล่อย: " + " · ".join(closes))
    sc = [f"{b} ×{a['scale']:g}" for b, a in sorted(r.get("alloc", {}).items()) if a.get("scale") not in (None, 1.0)]
    if sc:
        L.append("⚖️ ขนาดไม้: " + ", ".join(sc))
    L += [f"• {x}" for x in r["conclusions"][:6]]
    L.append("ฉบับเต็ม: หน้าเว็บ → รายงานวิจัย (/report)")
    return "\n".join(L)

