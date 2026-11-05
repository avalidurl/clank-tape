#!/usr/bin/env python3
"""
Clank Tape - static snapshot & ASCII terminal renderer.

Fetches public Clanker endpoints (no limit= query param, paginated via cursor),
writes compact structured data to data/latest.json, and renders static index.html
with Looptape-style pure ASCII tables, ASCII bars, and histograms.
"""

from __future__ import annotations

import datetime as dt
import html
import json
import math
import pathlib
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from typing import Any

ROOT = pathlib.Path(__file__).resolve().parent.parent
DATA_PATH = ROOT / "data" / "latest.json"
HTML_PATH = ROOT / "index.html"

API_BASE = "https://www.clanker.world/api"
USER_AGENT = "ClankTape/0.1 (+https://avalidurl.github.io/clank-tape/)"
RECENT_PAGES = 8  # ~80 recent launches (do NOT pass limit=; default page size is 10)
BURN_ADDRS = {
    "0x000000000000000000000000000000000000dead",
    "0x0000000000000000000000000000000000000000",
}


def fetch_json(url: str, retries: int = 3) -> dict[str, Any]:
    last_err: Exception | None = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(
                url,
                headers={
                    "User-Agent": USER_AGENT,
                    "Accept": "application/json",
                },
            )
            with urllib.request.urlopen(req, timeout=25) as resp:
                raw = resp.read().decode("utf-8")
                return json.loads(raw)
        except Exception as exc:  # noqa: BLE001
            last_err = exc
            if attempt + 1 < retries:
                time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"Failed to fetch {url}: {last_err}")


def to_float(val: Any, default: float = 0.0) -> float:
    if val is None or val == "":
        return default
    try:
        f = float(val)
        return f if math.isfinite(f) else default
    except (ValueError, TypeError):
        return default


def short_addr(addr: str | None) -> str:
    if not addr or len(addr) < 12:
        return addr or "-"
    return f"{addr[:6]}..{addr[-4:]}"


def clean_str(val: Any, max_len: int = 16) -> str:
    if val is None:
        return "-"
    s = "".join(ch if ch.isprintable() else "" for ch in str(val)).strip()
    if not s:
        return "-"
    # Keep ASCII-width predictable inside box-drawing tables
    s_ascii = s.encode("ascii", "replace").decode("ascii")
    if len(s_ascii) > max_len:
        return s_ascii[: max_len - 1] + "~"
    return s_ascii


def fmt_usd(val: float) -> str:
    if val <= 0:
        return "$0"
    if val >= 1_000_000_000:
        return f"${val / 1_000_000_000:.2f}B"
    if val >= 1_000_000:
        return f"${val / 1_000_000:.2f}M"
    if val >= 1_000:
        return f"${val / 1_000:.1f}K"
    if val >= 1:
        return f"${val:,.0f}"
    return f"${val:.2f}"


def fmt_pct(val: float | None) -> str:
    if val is None:
        return "   -   "
    sign = "+" if val > 0 else ""
    return f"{sign}{val:.1f}%"


def ascii_bar(value: float, max_value: float, width: int = 12, log_scale: bool = False) -> str:
    if max_value <= 0 or value <= 0:
        return "." * width
    if log_scale:
        v = math.log10(max(value, 1.0))
        m = math.log10(max(max_value, 10.0))
        ratio = max(0.0, min(1.0, v / m if m > 0 else 0.0))
    else:
        ratio = max(0.0, min(1.0, value / max_value))
    filled = int(round(ratio * width))
    if value > 0 and filled == 0:
        filled = 1
    return ("#" * filled) + ("." * (width - filled))


def parse_iso_utc(ts: str | None) -> dt.datetime | None:
    if not ts:
        return None
    try:
        s = ts.replace("Z", "+00:00")
        d = dt.datetime.fromisoformat(s)
        if d.tzinfo is None:
            d = d.replace(tzinfo=dt.timezone.utc)
        return d.astimezone(dt.timezone.utc)
    except ValueError:
        return None


def fmt_age(deployed_dt: dt.datetime | None, now_dt: dt.datetime) -> str:
    if not deployed_dt:
        return "-"
    sec = max(0, int((now_dt - deployed_dt).total_seconds()))
    if sec < 60:
        return f"{sec}s"
    if sec < 3600:
        return f"{sec // 60}m"
    if sec < 86400:
        h = sec // 3600
        m = (sec % 3600) // 60
        return f"{h}h{m:02d}m"
    days = sec // 86400
    return f"{days}d"


def extract_fee_profile(token: dict[str, Any]) -> dict[str, Any]:
    """
    Normalize Clanker fee/tax telemetry across contract generations:
    - clanker_tax (extensions.tax): buyTaxRate / sellTaxRate in 1e-4 bps (1_000_000 = 100 bps = 1.00%).
      split shares sum to 100_000_000 (1e8 = 100.0%).
      admittedPolicy.protocolFeeConfiguration.protocolTaxShare in 1e8 (e.g. 10_000_000 = 10% protocol skim).
    - clanker_v4 static (extensions.fees.type == 'static'):
      clankerFee / pairedFee in 1e-2 bps (10_000 = 100 bps = 1.00%, 50_000 = 500 bps = 5.00%, 100 = 1 bps).
      recipients[].bps sum to 10_000 (100%).
    - clanker_v4 dynamic (extensions.fees.type == 'dynamic'):
      baseFee / maxFee in 1e-2 bps.
    """
    ext = token.get("extensions") or {}
    admin_lc = (token.get("admin") or "").lower()
    sniper = ext.get("sniperTax") or {}

    if "tax" in ext and isinstance(ext["tax"], dict):
        tx = ext["tax"]
        fees = tx.get("fees") or {}
        split = tx.get("split") or {}
        pol = (tx.get("admittedPolicy") or {}).get("protocolFeeConfiguration") or {}
        anti = (tx.get("admittedPolicy") or {}).get("antiSnipeConfiguration") or {}

        buy_raw = int(fees.get("buyTaxRate") or 0)
        sell_raw = int(fees.get("sellTaxRate") or 0)
        # 1_000_000 raw = 100 bps (1.00%) -> divide by 10_000 for bps
        buy_bps = round(buy_raw / 10_000, 1)
        sell_bps = round(sell_raw / 10_000, 1)

        creator_pct = round(int(split.get("creatorAllocationShare") or 0) / 1_000_000, 1)
        burn_pct = round(int(split.get("burnAllocationShare") or 0) / 1_000_000, 1)
        integ_pct = round(int(split.get("integratorAllocationShare") or 0) / 1_000_000, 1)
        div_pct = round(int(split.get("dividendAllocationShare") or 0) / 1_000_000, 1)
        proto_skim_pct = round(int(pol.get("protocolTaxShare") or 0) / 1_000_000, 1)

        recs = fees.get("recipients") or []
        rec_creator_bps = sum(int(r.get("bps") or 0) for r in recs if r.get("isCreator"))

        return {
            "regime": "tax_v0",
            "buy_bps": buy_bps,
            "sell_bps": sell_bps,
            "dynamic_max_bps": None,
            "creator_pct": creator_pct,
            "burn_pct": burn_pct,
            "integrator_pct": integ_pct,
            "dividend_pct": div_pct,
            "protocol_skim_pct": proto_skim_pct,
            "recipient_count": len(recs),
            "recipient_creator_bps": rec_creator_bps,
            "sniper_window_s": int(anti.get("windowSeconds") or 0) if tx.get("antiSnipeEnabled") else 0,
        }

    if "fees" in ext and isinstance(ext["fees"], dict):
        f = ext["fees"]
        ftype = str(f.get("type") or "lp")
        recs = f.get("recipients") or []

        creator_bps = 0
        burn_bps = 0
        other_bps = 0
        for r in recs:
            bps = int(r.get("bps") or 0)
            rec_addr = str(r.get("recipient") or "").lower()
            rec_admin = str(r.get("admin") or "").lower()
            if rec_addr in BURN_ADDRS:
                burn_bps += bps
            elif r.get("isCreator") or (admin_lc and (rec_addr == admin_lc or rec_admin == admin_lc)):
                creator_bps += bps
            else:
                other_bps += bps

        # If single recipient and no explicit match, check if recipient == r.admin
        if len(recs) == 1 and creator_bps == 0 and burn_bps == 0:
            r0 = recs[0]
            if str(r0.get("recipient") or "").lower() == str(r0.get("admin") or "").lower():
                creator_bps = int(r0.get("bps") or 0)
                other_bps = 0

        if ftype == "dynamic":
            base_bps = round(int(f.get("baseFee") or 0) / 100, 1)
            max_bps = round(int(f.get("maxFee") or 0) / 100, 1)
            buy_bps = base_bps
            sell_bps = base_bps
            dyn_max = max_bps
            regime = "v4_dyn"
        else:
            # clankerFee / pairedFee: 10_000 = 100 bps (1.00%), 100 = 1 bps (0.01%)
            c_bps = round(int(f.get("clankerFee") or 0) / 100, 1)
            p_bps = round(int(f.get("pairedFee") or 0) / 100, 1)
            buy_bps = p_bps
            sell_bps = c_bps
            dyn_max = None
            regime = "v4_lp"

        sniper_sec = int(sniper.get("secondsToDecay") or 0) if sniper else 0

        return {
            "regime": regime,
            "buy_bps": buy_bps,
            "sell_bps": sell_bps,
            "dynamic_max_bps": dyn_max,
            "creator_pct": round(creator_bps / 100, 1),
            "burn_pct": round(burn_bps / 100, 1),
            "integrator_pct": round(other_bps / 100, 1),
            "dividend_pct": 0.0,
            "protocol_skim_pct": 20.0,  # Clanker v4 LP protocol fee share convention
            "recipient_count": len(recs),
            "recipient_creator_bps": creator_bps,
            "sniper_window_s": sniper_sec,
        }

    return {
        "regime": str(token.get("type") or "legacy")[:8],
        "buy_bps": None,
        "sell_bps": None,
        "dynamic_max_bps": None,
        "creator_pct": None,
        "burn_pct": None,
        "integrator_pct": None,
        "dividend_pct": None,
        "protocol_skim_pct": None,
        "recipient_count": 0,
        "recipient_creator_bps": 0,
        "sniper_window_s": 0,
    }


def normalize_token(token: dict[str, Any], now_dt: dt.datetime) -> dict[str, Any]:
    mkt = (token.get("related") or {}).get("market") or {}
    dep_str = token.get("deployed_at") or token.get("created_at")
    dep_dt = parse_iso_utc(dep_str)
    mcap = to_float(mkt.get("marketCap")) or to_float(token.get("starting_market_cap"))
    vol24 = to_float(mkt.get("volume24h"))
    chg24 = mkt.get("priceChangePercent24h")
    chg1h = mkt.get("priceChangePercent1h")
    buys24 = int(mkt.get("buys24h") or 0)
    sells24 = int(mkt.get("sells24h") or 0)
    fee = extract_fee_profile(token)
    tags = token.get("tags") or {}
    soc = token.get("social_context") or {}

    return {
        "name": str(token.get("name") or "").strip(),
        "symbol": str(token.get("symbol") or "").strip(),
        "contract_address": token.get("contract_address") or "",
        "admin": token.get("admin") or token.get("msg_sender") or "",
        "deployed_at": dep_dt.strftime("%Y-%m-%dT%H:%M:%SZ") if dep_dt else (dep_str or ""),
        "age": fmt_age(dep_dt, now_dt),
        "age_sec": int((now_dt - dep_dt).total_seconds()) if dep_dt else None,
        "type": token.get("type") or "unknown",
        "pair": token.get("pair") or "WETH",
        "chain_id": int(token.get("chain_id") or 8453),
        "platform": soc.get("platform") or soc.get("interface") or "",
        "verified": bool(tags.get("verified")),
        "market_cap_usd": round(mcap, 2),
        "volume_24h_usd": round(vol24, 2),
        "price_usd": to_float(mkt.get("priceUsd") or token.get("priceUsd")),
        "chg_1h_pct": round(to_float(chg1h), 2) if chg1h is not None else None,
        "chg_24h_pct": round(to_float(chg24), 2) if chg24 is not None else None,
        "tx_24h": buys24 + sells24,
        "buys_24h": buys24,
        "sells_24h": sells24,
        "fee": fee,
    }


def collect_snapshot() -> dict[str, Any]:
    now_dt = dt.datetime.now(dt.timezone.utc)

    # 1. Paginated recent deploys (NO limit= param)
    recent_raw: list[dict[str, Any]] = []
    seen_addrs: set[str] = set()
    cursor: str | None = None
    total_deployed = 0

    for _ in range(RECENT_PAGES):
        url = f"{API_BASE}/tokens?sortBy=deployed-at&sort=desc"
        if cursor:
            url += "&cursor=" + urllib.parse.quote(cursor, safe="")
        page = fetch_json(url)
        if page.get("tokensDeployed"):
            total_deployed = max(total_deployed, int(page["tokensDeployed"]))
        elif page.get("total"):
            total_deployed = max(total_deployed, int(page["total"]))

        batch = page.get("data") or []
        for item in batch:
            ca = str(item.get("contract_address") or "").lower()
            if ca and ca not in seen_addrs:
                seen_addrs.add(ca)
                recent_raw.append(item)
        cursor = page.get("cursor")
        if not cursor or not batch:
            break

    # 2. Trending endpoint
    trending_resp = fetch_json(f"{API_BASE}/tokens/trending")
    trending_list = trending_resp.get("trending") or []
    trending_tokens_map: dict[str, Any] = trending_resp.get("tokens") or {}
    # Case-insensitive lookup map for trending tokens
    tok_by_lc = {k.lower(): v for k, v in trending_tokens_map.items()}

    trending_norm: list[dict[str, Any]] = []
    for item in trending_list:
        attrs = item.get("attributes") or {}
        rel_base = (((item.get("relationships") or {}).get("base_token") or {}).get("data") or {}).get("id") or ""
        token_ca = rel_base.replace("base_", "") if rel_base.startswith("base_") else ""
        tok_obj = tok_by_lc.get(token_ca.lower()) or {}

        base_norm = normalize_token(tok_obj, now_dt) if tok_obj else {
            "name": str(attrs.get("name") or "").split("/")[0].strip(),
            "symbol": str(attrs.get("name") or "").split("/")[0].strip(),
            "contract_address": token_ca,
            "admin": "",
            "deployed_at": attrs.get("pool_created_at") or "",
            "age": fmt_age(parse_iso_utc(attrs.get("pool_created_at")), now_dt),
            "type": "pool",
            "pair": "WETH",
            "chain_id": 8453,
            "fee": extract_fee_profile({}),
        }

        chg_obj = attrs.get("price_change_percentage") or {}
        tx_h24 = (attrs.get("transactions") or {}).get("h24") or {}
        mcap_attr = to_float(attrs.get("market_cap_usd") or attrs.get("fdv_usd"))
        vol_attr = to_float(attrs.get("h24_volume_usd"))
        chg24_attr = chg_obj.get("h24")
        chg1h_attr = chg_obj.get("h1")

        if mcap_attr > 0:
            base_norm["market_cap_usd"] = round(mcap_attr, 2)
        if vol_attr > 0:
            base_norm["volume_24h_usd"] = round(vol_attr, 2)
        if chg24_attr is not None:
            base_norm["chg_24h_pct"] = round(to_float(chg24_attr), 2)
        if chg1h_attr is not None:
            base_norm["chg_1h_pct"] = round(to_float(chg1h_attr), 2)
        if tx_h24:
            buys = int(tx_h24.get("buys") or 0)
            sells = int(tx_h24.get("sells") or 0)
            base_norm["buys_24h"] = buys
            base_norm["sells_24h"] = sells
            base_norm["tx_24h"] = buys + sells
        base_norm["pool_address"] = attrs.get("address") or tok_obj.get("pool_address") or ""
        base_norm["reserve_usd"] = round(to_float(attrs.get("reserve_in_usd")), 2)
        trending_norm.append(base_norm)

    # 3. Top market cap page
    mcap_resp = fetch_json(f"{API_BASE}/tokens?sortBy=market-cap&sort=desc")
    top_mcap_norm = [normalize_token(t, now_dt) for t in (mcap_resp.get("data") or [])]

    recent_norm = [normalize_token(t, now_dt) for t in recent_raw]

    # Aggregations for Creator PnL Radar (v0 stub)
    creator_groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for t in recent_norm:
        adm = (t.get("admin") or "").lower()
        if adm and adm != "0x0000000000000000000000000000000000000000":
            creator_groups[adm].append(t)

    creators_summary = []
    for adm_lc, items in creator_groups.items():
        display_addr = items[0]["admin"]
        syms = []
        seen_s = set()
        for it in items:
            s = clean_str(it["symbol"], 10)
            if s not in seen_s:
                seen_s.add(s)
                syms.append(s)
        total_mcap = sum(it["market_cap_usd"] for it in items)
        total_vol = sum(it["volume_24h_usd"] for it in items)
        regimes = Counter(it["fee"]["regime"] for it in items)
        top_regime = regimes.most_common(1)[0][0] if regimes else "-"
        avg_buy_bps_vals = [it["fee"]["buy_bps"] for it in items if it["fee"]["buy_bps"] is not None]
        avg_buy_bps = round(sum(avg_buy_bps_vals) / len(avg_buy_bps_vals), 1) if avg_buy_bps_vals else None

        creators_summary.append({
            "admin": display_addr,
            "launch_count": len(items),
            "unique_symbols": len(syms),
            "symbols": syms[:6],
            "sum_mcap_usd": round(total_mcap, 2),
            "sum_vol_24h_usd": round(total_vol, 2),
            "primary_regime": top_regime,
            "avg_buy_bps": avg_buy_bps,
            "latest_deployed_at": items[0]["deployed_at"],
        })

    creators_summary.sort(key=lambda x: (-x["launch_count"], -x["sum_mcap_usd"]))

    # Cohort buckets by UTC hour (recent sample) + Trending vintage by month/quarter
    hour_buckets: dict[str, dict[str, Any]] = {}
    for t in recent_norm:
        dep_dt = parse_iso_utc(t["deployed_at"])
        if not dep_dt:
            continue
        hk = dep_dt.strftime("%Y-%m-%d %H:00Z")
        b = hour_buckets.setdefault(hk, {
            "bucket_utc": hk,
            "count": 0,
            "tax_v0": 0,
            "v4_lp": 0,
            "other": 0,
            "active_vol_count": 0,
            "sum_vol_usd": 0.0,
        })
        b["count"] += 1
        reg = t["fee"]["regime"]
        if reg == "tax_v0":
            b["tax_v0"] += 1
        elif reg in ("v4_lp", "v4_dyn"):
            b["v4_lp"] += 1
        else:
            b["other"] += 1
        if t["volume_24h_usd"] > 0:
            b["active_vol_count"] += 1
            b["sum_vol_usd"] += t["volume_24h_usd"]

    cohort_hours = sorted(hour_buckets.values(), key=lambda x: x["bucket_utc"], reverse=True)

    # Also build 15-minute micro-buckets over the last 3 hours for granular cadence
    qtr_buckets: dict[str, int] = Counter()
    for t in recent_norm:
        dep_dt = parse_iso_utc(t["deployed_at"])
        if not dep_dt:
            continue
        m_floor = (dep_dt.minute // 15) * 15
        qk = dep_dt.strftime(f"%H:{m_floor:02d}Z")
        qtr_buckets[qk] += 1

    # Fee stats across recent sample
    regime_counts = Counter(t["fee"]["regime"] for t in recent_norm)
    tax_tokens = [t for t in recent_norm if t["fee"]["regime"] == "tax_v0"]
    v4_tokens = [t for t in recent_norm if t["fee"]["regime"] in ("v4_lp", "v4_dyn")]

    return {
        "generated_at": now_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": "https://www.clanker.world/api",
        "chain": {"id": 8453, "name": "Base"},
        "stats": {
            "tokens_deployed_total": total_deployed,
            "recent_sample_size": len(recent_norm),
            "recent_window_oldest_utc": recent_norm[-1]["deployed_at"] if recent_norm else "",
            "recent_window_newest_utc": recent_norm[0]["deployed_at"] if recent_norm else "",
            "trending_24h_vol_usd": round(sum(t["volume_24h_usd"] for t in trending_norm), 2),
            "trending_sum_mcap_usd": round(sum(t["market_cap_usd"] for t in trending_norm), 2),
            "unique_creators_in_sample": len(creator_groups),
            "repeat_creators_in_sample": sum(1 for c in creators_summary if c["launch_count"] > 1),
            "regime_counts": dict(regime_counts),
            "tax_v0_count": len(tax_tokens),
            "v4_lp_count": len(v4_tokens),
        },
        "trending": trending_norm,
        "recent": recent_norm,
        "top_mcap": top_mcap_norm,
        "creators_v0": creators_summary[:20],
        "cohorts_hourly_v0": cohort_hours,
        "cohorts_15m_v0": [{"slot": k, "count": v} for k, v in sorted(qtr_buckets.items(), reverse=True)[:12]],
    }


# ---------------------------------------------------------------------------
# ASCII TABLE & CHART RENDERERS
# ---------------------------------------------------------------------------

def pad(text: str, width: int, align: str = "left") -> str:
    if len(text) > width:
        text = text[: width - 1] + "~" if width > 1 else text[:width]
    if align == "right":
        return text.rjust(width)
    if align == "center":
        return text.center(width)
    return text.ljust(width)


def build_box_table(
    headers: list[tuple[str, int, str]],
    rows: list[list[tuple[str, str | None]]],
) -> str:
    """
    Pure ASCII table (+ - |) so every glyph is one advance wide in monospace fonts.
    Unicode box-drawing often has wrong metrics and makes columns look glitched.
    Cell tuple: (plain_ascii_str, optional css class OR "href:URL").
    """
    widths = [w for _, w, _ in headers]
    aligns = [a for _, _, a in headers]

    def rule(sep: str) -> str:
        return "+" + sep.join("-" * (w + 2) for w in widths) + "+"

    top = rule("+")
    mid = rule("+")
    bot = rule("+")

    head_cells = []
    for (title, w, a) in headers:
        cell = html.escape(pad(title, w, a))
        head_cells.append(f" <span class=\"c-dim\">{cell}</span> ")
    head_line = "|" + "|".join(head_cells) + "|"

    out = [top, head_line, mid]
    for r in rows:
        rendered_cells = []
        for idx, (plain, rich) in enumerate(r):
            w = widths[idx]
            a = aligns[idx]
            # Force ASCII-only cell text so widths stay honest
            plain_ascii = "".join(ch if ord(ch) < 128 else "?" for ch in str(plain))
            padded = pad(plain_ascii, w, a)
            escaped_padded = html.escape(padded)
            if rich is None:
                rendered_cells.append(f" {escaped_padded} ")
            elif rich.startswith("href:"):
                url = html.escape(rich[5:], quote=True)
                stripped = padded.strip()
                esc_strip = html.escape(stripped)
                lead = len(padded) - len(padded.lstrip(" "))
                trail = len(padded) - len(padded.rstrip(" "))
                cell_html = (" " * lead) + f"<a href=\"{url}\" target=\"_blank\" rel=\"noopener\">{esc_strip}</a>" + (" " * trail)
                rendered_cells.append(f" {cell_html} ")
            else:
                rendered_cells.append(f" <span class=\"{rich}\">{escaped_padded}</span> ")
        out.append("|" + "|".join(rendered_cells) + "|")
    out.append(bot)
    return "\n".join(out)


def chg_cls(val: float | None) -> str:
    if val is None or val == 0:
        return "c-dim"
    return "c-grn" if val > 0 else "c-red"


def render_marquee_items(trending: list[dict[str, Any]]) -> str:
    chunks = []
    for t in trending[:16]:
        sym = html.escape(clean_str(t.get("symbol"), 12))
        vol = fmt_usd(to_float(t.get("volume_24h_usd")))
        mcap = fmt_usd(to_float(t.get("market_cap_usd")))
        chg = t.get("chg_24h_pct")
        chg_str = fmt_pct(chg).strip()
        cls = "tick-flat"
        arrow = "·"
        if chg is not None:
            if chg > 0:
                cls = "tick-up"
                arrow = "^"
            elif chg < 0:
                cls = "tick-dn"
                arrow = "v"
        chunks.append(
            f'<span class="tick-item">'
            f'<span class="tick-sym">${sym}</span> '
            f'<span class="{cls}">{arrow}{chg_str}</span> '
            f'<span class="c-dim">MC:{mcap} V24:{vol}</span>'
            f'<span class="tick-sep">|</span>'
            f'</span>'
        )
    joined = "".join(chunks)
    # Duplicate track for seamless 50% CSS marquee loop
    return joined + joined


def render_trending_ascii(trending: list[dict[str, Any]]) -> str:
    items = trending[:15]
    max_vol = max((to_float(t.get("volume_24h_usd")) for t in items), default=1.0)
    max_mcap = max((to_float(t.get("market_cap_usd")) for t in items), default=1.0)

    headers = [
        ("#", 2, "right"),
        ("SYMBOL", 11, "left"),
        ("MCAP", 8, "right"),
        ("MCAP (LOG10)", 12, "left"),
        ("24H VOL", 8, "right"),
        ("VOL BAR", 12, "left"),
        ("24H%", 8, "right"),
        ("TX24H", 6, "right"),
        ("AGE", 6, "right"),
        ("CA (BASE)", 11, "left"),
    ]
    rows = []
    for i, t in enumerate(items, 1):
        sym = clean_str(t.get("symbol"), 11)
        mcap = to_float(t.get("market_cap_usd"))
        vol = to_float(t.get("volume_24h_usd"))
        chg = t.get("chg_24h_pct")
        txc = int(t.get("tx_24h") or 0)
        ca = t.get("contract_address") or ""
        link = f"href:https://www.clanker.world/clanker/{ca}" if ca else None

        rows.append([
            (f"{i:02d}", "c-dim"),
            (sym, "c-bld"),
            (fmt_usd(mcap), None),
            (ascii_bar(mcap, max_mcap, 12, log_scale=True), "c-amb"),
            (fmt_usd(vol), None),
            (ascii_bar(vol, max_vol, 12, log_scale=False), "c-grn"),
            (fmt_pct(chg).strip(), chg_cls(chg)),
            (f"{txc:,}" if txc else "-", "c-dim" if not txc else None),
            (clean_str(t.get("age"), 6), "c-dim"),
            (short_addr(ca), link),
        ])
    return build_box_table(headers, rows)


def render_recent_ascii(recent: list[dict[str, Any]]) -> str:
    items = recent[:22]
    max_mcap = max((to_float(t.get("market_cap_usd")) for t in items), default=1.0)

    headers = [
        ("UTC TIME", 8, "left"),
        ("AGE", 6, "right"),
        ("SYMBOL", 12, "left"),
        ("REGIME", 8, "left"),
        ("BUY/SELL", 11, "right"),
        ("MCAP", 8, "right"),
        ("DEPTH", 8, "left"),
        ("V24H", 6, "right"),
        ("CREATOR", 11, "left"),
        ("CA", 11, "left"),
    ]
    rows = []
    for t in items:
        dep = t.get("deployed_at") or ""
        hhmmss = dep[11:19] if len(dep) >= 19 else "-"
        sym = clean_str(t.get("symbol"), 12)
        fee = t.get("fee") or {}
        reg = clean_str(fee.get("regime"), 8)
        b_bps = fee.get("buy_bps")
        s_bps = fee.get("sell_bps")
        if b_bps is not None and s_bps is not None:
            bs_str = f"{int(round(b_bps))}/{int(round(s_bps))}bp"
        else:
            bs_str = "-"
        mcap = to_float(t.get("market_cap_usd"))
        vol = to_float(t.get("volume_24h_usd"))
        adm = t.get("admin") or ""
        ca = t.get("contract_address") or ""

        reg_cls = "c-amb" if reg == "tax_v0" else ("c-grn" if reg.startswith("v4") else "c-dim")
        rows.append([
            (hhmmss, "c-dim"),
            (clean_str(t.get("age"), 6), "c-dim"),
            (sym, "c-bld"),
            (reg, reg_cls),
            (bs_str, "c-amb" if (b_bps or 0) >= 300 else None),
            (fmt_usd(mcap), None),
            (ascii_bar(mcap, max_mcap, 8, log_scale=True), "c-grn"),
            (fmt_usd(vol), "c-grn" if vol > 0 else "c-mut"),
            (short_addr(adm), f"href:https://basescan.org/address/{adm}" if adm else None),
            (short_addr(ca), f"href:https://www.clanker.world/clanker/{ca}" if ca else None),
        ])
    return build_box_table(headers, rows)


def render_fee_reality_ascii(snapshot: dict[str, Any]) -> str:
    recent = snapshot.get("recent") or []
    trending = snapshot.get("trending") or []
    top_mcap = snapshot.get("top_mcap") or []

    # Combine unique tokens with explicit fee/tax telemetry, prioritizing tax_v0 & split examples + active volume
    by_ca: dict[str, dict[str, Any]] = {}
    for pool in (recent + trending + top_mcap):
        ca = (pool.get("contract_address") or "").lower()
        if ca and ca not in by_ca:
            by_ca[ca] = pool

    with_fees = [t for t in by_ca.values() if (t.get("fee") or {}).get("buy_bps") is not None]
    # Sort so we show: tax_v0 tokens with splits/varied bps + v4 tokens with multi-recipient/burn splits + active volume
    def fee_sort_key(t: dict[str, Any]) -> tuple[int, int, float, float]:
        f = t["fee"]
        non_trivial_split = 1 if ((f.get("burn_pct") or 0) > 0 or (f.get("integrator_pct") or 0) > 0 or f.get("regime") == "v4_dyn") else 0
        active_or_tax = 1 if (to_float(t.get("volume_24h_usd")) > 0 or f.get("regime") == "tax_v0") else 0
        return (non_trivial_split, active_or_tax, to_float(t.get("volume_24h_usd")), to_float(f.get("sell_bps")))

    selected = sorted(with_fees, key=fee_sort_key, reverse=True)[:18]

    # Regime distribution summary box
    reg_counts = Counter((t.get("fee") or {}).get("regime", "unknown") for t in recent)
    total_r = max(1, len(recent))

    tax_recent = [t["fee"] for t in recent if (t.get("fee") or {}).get("regime") == "tax_v0" and t["fee"].get("buy_bps") is not None]
    v4_recent = [t["fee"] for t in recent if (t.get("fee") or {}).get("regime") in ("v4_lp", "v4_dyn") and t["fee"].get("buy_bps") is not None]

    med_tax_buy = sorted(f["buy_bps"] for f in tax_recent)[len(tax_recent) // 2] if tax_recent else 0
    med_tax_sell = sorted(f["sell_bps"] for f in tax_recent)[len(tax_recent) // 2] if tax_recent else 0
    med_v4_lp = sorted(f["buy_bps"] for f in v4_recent)[len(v4_recent) // 2] if v4_recent else 0

    sum_headers = [
        (f"REGIME (N={len(recent)})", 15, "left"),
        ("SHARE", 12, "right"),
        ("DISTRIBUTION", 16, "left"),
        ("MECHANICS & PROTOCOL SKIM", 56, "left"),
    ]
    sum_rows = []
    for label, key, desc in [
        ("v4_lp / v4_dyn", ("v4_lp", "v4_dyn"), "Uni v4 hook LP fee (10000 raw = 100 bps); ~20% protocol"),
        ("tax_v0", ("tax_v0",), "Token buy/sell tax (1000000 raw = 100 bps); 10% protocol"),
        ("legacy / proxy", None, "v3.1 / v3 / proxy pools without v4/tax extension block"),
    ]:
        if key is None:
            cnt = sum(v for k, v in reg_counts.items() if k not in ("v4_lp", "v4_dyn", "tax_v0"))
        else:
            cnt = sum(reg_counts.get(k, 0) for k in key)
        pct = (cnt / total_r) * 100
        sum_rows.append([
            (label, "c-bld" if cnt > 0 else "c-dim"),
            (f"{cnt} ({pct:4.1f}%)", None),
            (ascii_bar(cnt, total_r, 16), "c-amb" if cnt > 0 else "c-mut"),
            (desc, "c-dim"),
        ])
    med_banner = (
        f"  MEDIAN RATES » tax_v0: {int(med_tax_buy)} bps buy / {int(med_tax_sell)} bps sell"
        f"  ·  v4_lp: {int(med_v4_lp)} bps clanker/paired  ·  100 bps = 1.00%"
    )
    summary_lines = [build_box_table(sum_headers, sum_rows), f"<span class=\"c-grn\">{html.escape(med_banner)}</span>"]

    headers = [
        ("SYMBOL", 12, "left"),
        ("REGIME", 7, "left"),
        ("BUY BPS", 7, "right"),
        ("SELL BPS", 8, "right"),
        ("CREATOR%", 8, "right"),
        ("BURN%", 6, "right"),
        ("INTEG/OTH%", 10, "right"),
        ("PROTO%", 6, "right"),
        ("SNIPE", 5, "right"),
        ("CA", 11, "left"),
    ]
    rows = []
    for t in selected:
        f = t["fee"]
        ca = t.get("contract_address") or ""
        b_bps = f.get("buy_bps")
        s_bps = f.get("sell_bps")
        dyn_max = f.get("dynamic_max_bps")
        sell_txt = f"{int(round(s_bps))}->{int(round(dyn_max))}" if dyn_max else (f"{int(round(s_bps))}" if s_bps is not None else "-")
        buy_txt = f"{int(round(b_bps))}" if b_bps is not None else "-"

        c_pct = f"{f['creator_pct']:.0f}%" if f.get("creator_pct") is not None else "-"
        brn_val = f.get("burn_pct") or 0.0
        int_val = f.get("integrator_pct") or 0.0
        brn_txt = f"{brn_val:.0f}%" if f.get("burn_pct") is not None else "-"
        int_txt = f"{int_val:.0f}%" if f.get("integrator_pct") is not None else "-"
        prt_txt = f"{f['protocol_skim_pct']:.0f}%" if f.get("protocol_skim_pct") is not None else "-"
        snp = f.get("sniper_window_s") or 0
        snp_txt = f"{snp}s" if snp > 0 else "-"

        rows.append([
            (clean_str(t.get("symbol"), 12), "c-bld"),
            (clean_str(f.get("regime"), 7), "c-amb" if f.get("regime") == "tax_v0" else "c-grn"),
            (buy_txt, "c-amb" if (b_bps or 0) >= 300 else None),
            (sell_txt, "c-amb" if (s_bps or 0) >= 300 else None),
            (c_pct, "c-grn" if (f.get("creator_pct") or 0) >= 80 else None),
            (brn_txt, "c-cyn" if brn_val > 0 else "c-mut"),
            (int_txt, "c-amb" if int_val > 0 else "c-mut"),
            (prt_txt, "c-dim"),
            (snp_txt, "c-dim"),
            (short_addr(ca), f"href:https://www.clanker.world/clanker/{ca}" if ca else None),
        ])

    return "\n".join(summary_lines) + "\n\n" + build_box_table(headers, rows)


def render_creators_ascii(snapshot: dict[str, Any]) -> str:
    creators = snapshot.get("creators_v0") or []
    max_launches = max((int(c.get("launch_count") or 0) for c in creators), default=1)

    headers = [
        ("ADMIN / DEPLOYER", 16, "left"),
        ("LAUNCHES", 8, "right"),
        ("CADENCE", 10, "left"),
        ("REGIME", 7, "left"),
        ("AVG BUY", 7, "right"),
        ("SUM MCAP", 9, "right"),
        ("SUM V24H", 8, "right"),
        ("RECENT SYMBOLS (SAMPLE)", 28, "left"),
    ]
    rows = []
    for c in creators[:16]:
        adm = c.get("admin") or ""
        cnt = int(c.get("launch_count") or 0)
        syms = ", ".join(c.get("symbols") or [])
        avg_b = c.get("avg_buy_bps")
        avg_str = f"{int(round(avg_b))}bp" if avg_b is not None else "-"
        vol = to_float(c.get("sum_vol_24h_usd"))
        rows.append([
            (short_addr(adm), f"href:https://basescan.org/address/{adm}" if adm else None),
            (str(cnt), "c-amb" if cnt > 1 else None),
            (ascii_bar(cnt, max_launches, 10), "c-amb" if cnt > 1 else "c-dim"),
            (clean_str(c.get("primary_regime"), 7), "c-dim"),
            (avg_str, None),
            (fmt_usd(to_float(c.get("sum_mcap_usd"))), None),
            (fmt_usd(vol), "c-grn" if vol > 0 else "c-mut"),
            (clean_str(syms, 28), "c-bld" if cnt > 1 else None),
        ])
    return build_box_table(headers, rows)


def render_cohorts_ascii(snapshot: dict[str, Any]) -> str:
    hourly = snapshot.get("cohorts_hourly_v0") or []
    qtr = snapshot.get("cohorts_15m_v0") or []
    max_h = max((int(b.get("count") or 0) for b in hourly), default=1)

    headers = [
        ("UTC HOUR BUCKET", 17, "left"),
        ("DEPLOYS", 7, "right"),
        ("HOURLY LAUNCH HISTOGRAM", 24, "left"),
        ("V4_LP", 5, "right"),
        ("TAX_V0", 6, "right"),
        ("VOL>0", 5, "right"),
        ("COHORT V24H", 11, "right"),
    ]
    rows = []
    for b in hourly[:12]:
        cnt = int(b.get("count") or 0)
        vol = to_float(b.get("sum_vol_usd"))
        act = int(b.get("active_vol_count") or 0)
        rows.append([
            (b["bucket_utc"], "c-bld"),
            (str(cnt), "c-grn"),
            (ascii_bar(cnt, max_h, 24), "c-grn"),
            (str(b.get("v4_lp", 0)), "c-dim"),
            (str(b.get("tax_v0", 0)), "c-amb" if b.get("tax_v0", 0) > 0 else "c-mut"),
            (f"{act}/{cnt}", "c-grn" if act > 0 else "c-mut"),
            (fmt_usd(vol), "c-grn" if vol > 0 else "c-mut"),
        ])

    max_q = max((int(x.get("count") or 0) for x in qtr), default=1)
    q_headers = [
        ("15M UTC SLOT", 14, "left"),
        ("DEPLOYS", 9, "right"),
        ("15-MINUTE MICRO-CADENCE HISTOGRAM", 36, "left"),
    ]
    q_rows = [
        [
            (str(x["slot"]), "c-bld"),
            (str(int(x["count"])), "c-amb"),
            (ascii_bar(int(x["count"]), max_q, 36), "c-amb"),
        ]
        for x in qtr[:10]
    ]
    q_lines = [build_box_table(q_headers, q_rows)]

    return build_box_table(headers, rows) + "\n\n" + "\n".join(q_lines)


def render_roadmap_ascii() -> str:
    return "\n".join([
        "+------+----------------------------+--------------+-----------------------------------------------------------+",
        "| <span class=\"c-dim\">STAT</span> | <span class=\"c-dim\">MODULE</span>                     | <span class=\"c-dim\">RELEASE</span>      | <span class=\"c-dim\">SCOPE &amp; NEXT MILESTONE</span>                                    |",
        "+------+----------------------------+--------------+-----------------------------------------------------------+",
        "| <span class=\"c-grn\">[OK]</span> | <span class=\"c-bld\">01 · Daily Tape</span>            | <span class=\"c-grn\">LIVE (v0.1)</span>  | Paginated recent deploys + 24h trending + ASCII depth bars|",
        "| <span class=\"c-grn\">[OK]</span> | <span class=\"c-bld\">02 · Fee Reality Card</span>      | <span class=\"c-grn\">LIVE (v0.1)</span>  | Normalized v4 LP &amp; tax_v0 bps + creator/burn/integ splits |",
        "| <span class=\"c-amb\">[~]</span>  | <span class=\"c-bld\">03 · Creator PnL Radar</span>     | <span class=\"c-amb\">STUB (v0)</span>    | Live admin launch clustering -> next: Base fee-claim logs |",
        "| <span class=\"c-amb\">[~]</span>  | <span class=\"c-bld\">04 · Launch Cohort Board</span>   | <span class=\"c-amb\">STUB (v0)</span>    | UTC hourly/15m histograms -> next: 24h/7d survival curves  |",
        "+------+----------------------------+--------------+-----------------------------------------------------------+",
        "| <span class=\"c-dim\">[ ]</span>  | 05 · Realized Fee Cashflow | NEXT         | Reconcile stated bps vs on-chain hook/escrow WETH claims  |",
        "| <span class=\"c-dim\">[ ]</span>  | 06 · Sniper Decay Audit    | NEXT         | Window-0 sniper tax capture vs post-decay organic flow    |",
        "| <span class=\"c-dim\">[ ]</span>  | 07 · Historical Tape Diffs | NEXT         | Rolling multi-day archive in git for cohort retention     |",
        "+------+----------------------------+--------------+-----------------------------------------------------------+",
    ])


def render_html(snapshot: dict[str, Any]) -> str:
    gen_at = html.escape(snapshot["generated_at"])
    st = snapshot["stats"]
    total_dep = f"{st['tokens_deployed_total']:,}" if st.get("tokens_deployed_total") else "-"
    tr_vol = fmt_usd(to_float(st.get("trending_24h_vol_usd")))
    tr_mcap = fmt_usd(to_float(st.get("trending_sum_mcap_usd")))
    sample_n = st.get("recent_sample_size", 0)
    tax_n = st.get("tax_v0_count", 0)
    v4_n = st.get("v4_lp_count", 0)
    uniq_c = st.get("unique_creators_in_sample", 0)
    rep_c = st.get("repeat_creators_in_sample", 0)

    marquee_html = render_marquee_items(snapshot.get("trending") or [])
    trending_pre = render_trending_ascii(snapshot.get("trending") or [])
    recent_pre = render_recent_ascii(snapshot.get("recent") or [])
    fee_pre = render_fee_reality_ascii(snapshot)
    creators_pre = render_creators_ascii(snapshot)
    cohorts_pre = render_cohorts_ascii(snapshot)
    roadmap_pre = render_roadmap_ascii()

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>CLANK TAPE · Clanker Trader Analytics Board</title>
  <meta name="description" content="Clank Tape: ASCII terminal analytics board for Clanker launches on Base - Daily Tape, Fee Reality Card, Creator PnL Radar, and Launch Cohort Board." />
  <meta property="og:title" content="CLANK TAPE · Clanker Trader Analytics Board" />
  <meta property="og:description" content="Continuous ASCII tape for Clanker deploys, fee/tax reality in bps, creator launch clustering, and UTC launch cohorts on Base." />
  <meta name="theme-color" content="#08090a" />
  <link rel="preconnect" href="https://fonts.googleapis.com" />
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin />
  <link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500;600&display=swap" rel="stylesheet" />
  <link rel="stylesheet" href="style.css" />
</head>
<body>
  <div class="scanlines" aria-hidden="true"></div>

  <!-- Continuous Ticker Ribbon -->
  <div class="tape-ribbon" role="region" aria-label="Trending Clanker ticker tape">
    <span class="tape-badge">CLANK TAPE // BASE·8453</span>
    <div class="marquee-viewport">
      <div class="marquee-track" id="marquee-track">{marquee_html}</div>
    </div>
  </div>

  <div class="shell">
    <header class="masthead" id="top">
      <div class="masthead-top">
        <div class="brand-block">
          <h1 class="brand-title">CLANK TAPE</h1>
          <span class="brand-sub">clanker trader analytics board · base mainnet (8453)</span>
        </div>
        <div class="status-cluster">
          <span><span class="dot-live"></span>SNAPSHOT: <strong id="snap-ts" class="c-grn">{gen_at}</strong></span>
          <span>RAW: <a href="data/latest.json">data/latest.json</a></span>
        </div>
      </div>

      <div class="nav-bar">
        <nav class="nav-links" aria-label="Board sections">
          <a href="#tape">[01] tape</a>
          <span class="sep">/</span>
          <a href="#fees">[02] fees</a>
          <span class="sep">/</span>
          <a href="#creators">[03] creators</a>
          <span class="sep">/</span>
          <a href="#cohorts">[04] cohorts</a>
          <span class="sep">/</span>
          <a href="#roadmap">[05] roadmap</a>
          <span class="sep">/</span>
          <a href="docs/">[docs]</a>
          <span class="sep">/</span>
          <a href="skill/skill.md">[skill]</a>
          <span class="sep">/</span>
          <a href="#buy">[buy]</a>
        </nav>
        <div class="refresh-controls">
          <span id="refresh-note">static build · auto-updated via gh actions</span>
          <button type="button" class="btn-term" id="btn-check-json" title="Re-read committed data/latest.json">[check snapshot]</button>
        </div>
      </div>
    </header>

    <section class="panel moat-strip" id="moat" aria-label="Moat">
      <pre class="ascii-wrap">MOAT · Clanker-native fee decode + deploy tape + creator cadence + launch cohorts - not another candle chart.</pre>
    </section>

    
    <section class="panel stake-cta" id="buy" aria-label="Support the roadmap">
      <div class="panel-head">
        <span class="panel-tag">[$]</span>
        <h2>BUY CTAPE</h2>
        <span class="panel-meta">support the roadmap</span>
      </div>
      <pre class="ascii-wrap">BUY <a href="https://clanker.world/clanker/0xc06E100b577013248e094D44B77dd436fb4DCCcC" target="_blank" rel="noopener">$CTAPE</a> TO SUPPORT THE ROADMAP
-> fee reality · creator radar · cohort board · next milestones
-> <a href="https://clanker.world/clanker/0xc06E100b577013248e094D44B77dd436fb4DCCcC" target="_blank" rel="noopener">clanker.world/clanker/0xc06E...CCcC</a></pre>
      <p class="cta-row" style="margin:12px 0 0;"><a class="btn-term" href="https://clanker.world/clanker/0xc06E100b577013248e094D44B77dd436fb4DCCcC" target="_blank" rel="noopener">[ BUY CTAPE -> ]</a></p>
    </section>

<!-- Dense Terminal KPI Strip -->
    <section class="kpi-strip" aria-label="Snapshot summary metrics">
      <div class="kpi-cell">
        <span class="kpi-label">Total Clanker Indexed</span>
        <span class="kpi-val" id="kpi-total">{total_dep}</span>
        <span class="kpi-sub">tokensDeployed via API</span>
      </div>
      <div class="kpi-cell">
        <span class="kpi-label">Trending 24h Volume</span>
        <span class="kpi-val" id="kpi-tvol">{tr_vol}</span>
        <span class="kpi-sub">top 20 trending pools</span>
      </div>
      <div class="kpi-cell">
        <span class="kpi-label">Trending Market Cap</span>
        <span class="kpi-val amber" id="kpi-tmcap">{tr_mcap}</span>
        <span class="kpi-sub">combined FDV / mcap</span>
      </div>
      <div class="kpi-cell">
        <span class="kpi-label">Recent Tape Sample</span>
        <span class="kpi-val" id="kpi-sample">{sample_n} launches</span>
        <span class="kpi-sub">{v4_n} v4_lp · {tax_n} tax_v0</span>
      </div>
      <div class="kpi-cell">
        <span class="kpi-label">Active Deployers</span>
        <span class="kpi-val amber" id="kpi-creators">{uniq_c} admins</span>
        <span class="kpi-sub">{rep_c} repeat launchers in window</span>
      </div>
    </section>

    <!-- 01: DAILY TAPE -->
    <section id="tape" class="panel">
      <div class="panel-head">
        <h2 class="panel-title"><span class="idx">01 //</span>DAILY TAPE - TRENDING &amp; FRESH DEPLOYS</h2>
        <span class="tag tag-live">LIVE · STATIC SNAPSHOT</span>
      </div>
      <p class="panel-note">
        Two-track tape baked from Clanker public endpoints: top 24h volume movers (linear volume bars, log10 market-cap bars) followed by newest Base deployments paginated by cursor.
      </p>
      <div class="sub-head">A · TRENDING RUNNERS (24H VOLUME &amp; MCAP DEPTH)</div>
      <pre class="ascii-wrap" aria-label="Trending Clanker tokens table">{trending_pre}</pre>
      <div class="sub-head">B · RECENT DEPLOY WIRE (LATEST PAGINATED LAUNCHES, UTC)</div>
      <pre class="ascii-wrap" aria-label="Recent Clanker launches table">{recent_pre}</pre>
    </section>

    <!-- 02: FEE REALITY CARD -->
    <section id="fees" class="panel">
      <div class="panel-head">
        <h2 class="panel-title"><span class="idx">02 //</span>FEE REALITY CARD - TAX &amp; HOOK BPS BREAKDOWN</h2>
        <span class="tag tag-live">FIRST CUT · LIVE</span>
      </div>
      <p class="panel-note">
        Normalized fee extraction across Clanker contract regimes. <code>tax_v0</code> encodes rates in 10<sup>-4</sup> bps (<code>1,000,000</code> = 100 bps = 1.00%) with explicit creator / burn / integrator allocation shares; <code>v4_lp</code> encodes static or dynamic hook fees in 10<sup>-2</sup> bps (<code>10,000</code> = 100 bps = 1.00%) split across recipient bps slots (including <code>0x...dEaD</code> burn routing).
      </p>
      <pre class="ascii-wrap" aria-label="Fee reality summary and per-token bps breakdown">{fee_pre}</pre>
    </section>

    <!-- 03: CREATOR PNL RADAR -->
    <section id="creators" class="panel">
      <div class="panel-head">
        <h2 class="panel-title"><span class="idx">03 //</span>CREATOR PNL RADAR - DEPLOYER CLUSTERING</h2>
        <span class="tag tag-v0">v0 STUB · REAL ADMIN DATA</span>
      </div>
      <p class="panel-note">
        <strong>v0 stub toward full Creator PnL:</strong> aggregates the current recent-launch window by <code>admin</code> / deployer wallet - surfacing serial launchers, fee regime preference, and combined market cap / 24h volume. Full realized WETH/token fee-claim PnL from Base hook &amp; FeeEscrow logs lands in v1.
      </p>
      <pre class="ascii-wrap" aria-label="Creator launch aggregation table">{creators_pre}</pre>
    </section>

    <!-- 04: LAUNCH COHORT BOARD -->
    <section id="cohorts" class="panel">
      <div class="panel-head">
        <h2 class="panel-title"><span class="idx">04 //</span>LAUNCH COHORT BOARD - UTC DEPLOY CADENCE</h2>
        <span class="tag tag-v0">v0 STUB · HOURLY &amp; 15M HISTOGRAM</span>
      </div>
      <p class="panel-note">
        <strong>v0 stub toward Cohort Survival:</strong> buckets the recent paginated sample into UTC hours and 15-minute slots, splitting <code>v4_lp</code> vs <code>tax_v0</code> mix and tracking how many launches in each bucket have non-zero 24h volume. Multi-day rolling retention curves arrive once archived snapshots accumulate.
      </p>
      <pre class="ascii-wrap" aria-label="Launch cohort hourly and 15-minute ASCII histograms">{cohorts_pre}</pre>
    </section>

    <!-- 05: ROADMAP -->
    <section id="roadmap" class="panel">
      <div class="panel-head">
        <h2 class="panel-title"><span class="idx">05 //</span>ANALYTICS ROADMAP</h2>
        <span class="tag">SHIP LOG</span>
      </div>
      <p class="panel-note">
        Clank Tape ships incrementally as a zero-dependency static board regenerated on schedule from verifiable public telemetry.
      </p>
      <pre class="ascii-wrap" aria-label="Product roadmap status table">{roadmap_pre}</pre>
    </section>

    <footer class="footer">
      <div>
        <p><strong>CLANK TAPE</strong> · independent trader analytics board for Clanker on Base (chain_id 8453).</p>
        <p>Data sourced from public <code>clanker.world/api</code> endpoints · Not affiliated with or endorsed by Clanker.</p>
      </div>
      <div>
        <p>All timestamps in <strong>UTC</strong> · Snapshot baked at <code>{gen_at}</code></p>
        <p>Source: <a href="https://github.com/avalidurl/clank-tape" target="_blank" rel="noopener">github.com/avalidurl/clank-tape</a> · JSON: <a href="data/latest.json">data/latest.json</a></p>
      </div>
    </footer>
  </div>

  <script>
    (function () {{
      var btn = document.getElementById('btn-check-json');
      var note = document.getElementById('refresh-note');
      var snapTs = document.getElementById('snap-ts');
      if (!btn) return;
      btn.addEventListener('click', function () {{
        note.textContent = 'checking data/latest.json...';
        fetch('data/latest.json?t=' + Date.now(), {{ cache: 'no-store' }})
          .then(function (r) {{
            if (!r.ok) throw new Error('HTTP ' + r.status);
            return r.json();
          }})
          .then(function (d) {{
            var remoteTs = d.generated_at || '';
            var curTs = (snapTs.textContent || '').trim();
            if (remoteTs && remoteTs !== curTs) {{
              note.textContent = 'newer snapshot (' + remoteTs + ') - reloading...';
              window.location.reload();
            }} else {{
              var st = d.stats || {{}};
              note.textContent = 'up to date · ' + remoteTs + ' (' + (st.recent_sample_size || 0) + ' recent)';
            }}
          }})
          .catch(function (e) {{
            note.textContent = 'static snapshot active (' + e.message + ')';
          }});
      }});
    }})();
  </script>
</body>
</html>
"""


def main() -> int:
    print("Fetching live Clanker snapshot...", file=sys.stderr)
    snapshot = collect_snapshot()

    DATA_PATH.parent.mkdir(parents=True, exist_ok=True)
    DATA_PATH.write_text(json.dumps(snapshot, indent=2, sort_keys=False) + "\n", encoding="utf-8")
    print(f"Wrote {DATA_PATH} ({DATA_PATH.stat().st_size:,} bytes)", file=sys.stderr)

    rendered = render_html(snapshot)
    HTML_PATH.write_text(rendered, encoding="utf-8")
    print(f"Wrote {HTML_PATH} ({HTML_PATH.stat().st_size:,} bytes)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
