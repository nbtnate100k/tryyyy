"""Tiered raw lines per BIN: first vs second pile. Synced from web sorter."""

from __future__ import annotations

import json
import logging
import random
import shutil
from collections import defaultdict
from pathlib import Path

from data_paths import data_dir

logger = logging.getLogger(__name__)

LEADS_PATH = data_dir() / "bin_leads.json"


def _backup_sidecar_if_nonempty(path: Path) -> None:
    if not path.is_file() or path.stat().st_size == 0:
        return
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("not an object")
    except (json.JSONDecodeError, OSError, ValueError):
        logger.warning("Skip .bak backup — %s is missing or not valid JSON", path)
        return
    try:
        shutil.copy2(path, path.with_name(path.name + ".bak"))
    except OSError as e:
        logger.warning("Could not backup %s: %s", path, e)

# Fixed second-tier retail (first tier uses catalog price_per_bin, default 0.90)
SECONDHAND_PRICE_USD = 0.35


def _norm_bin(key: str) -> str | None:
    d = "".join(c for c in str(key) if c.isdigit())[:6]
    return d if len(d) == 6 else None


def card_brand_from_bin6(bin6: str) -> str | None:
    """
    Map a 6-digit BIN to card network for filtering.
    Returns lowercase: visa | mastercard | amex | discover, or None if unrecognized.
    """
    nb = _norm_bin(bin6)
    if not nb:
        return None
    if nb[0] == "4":
        return "visa"
    first2 = int(nb[:2])
    first3 = int(nb[:3])
    first4 = int(nb[:4])
    first6 = int(nb)
    if first2 in (34, 37):
        return "amex"
    if 51 <= first2 <= 55:
        return "mastercard"
    if 222100 <= first6 <= 272099:
        return "mastercard"
    if first4 == 6011:
        return "discover"
    if 622126 <= first6 <= 622925:
        return "discover"
    if 644 <= first3 <= 649:
        return "discover"
    if first2 == 65:
        return "discover"
    return None


def norm_stock_tier(t: str) -> str:
    s = str(t).strip().lower()
    if s in ("second", "2", "secondhand", "sh"):
        return "second"
    return "first"


def _tier_dict_normalize(obj) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    if not isinstance(obj, dict):
        return out
    for k, v in obj.items():
        nb = _norm_bin(k)
        if not nb:
            continue
        if isinstance(v, list):
            out[nb] = [str(x).strip() for x in v if str(x).strip()]
        elif isinstance(v, str) and v.strip():
            out[nb] = [v.strip()]
    return out


def _parse_file_raw(raw: dict) -> dict[str, dict[str, list[str]]]:
    """v2: {first:{bin:[]}, second:{}}  v1: {bin:[]} -> all first."""
    if not isinstance(raw, dict):
        return {"first": {}, "second": {}}
    if "first" in raw or "second" in raw:
        return {
            "first": _tier_dict_normalize(raw.get("first")),
            "second": _tier_dict_normalize(raw.get("second")),
        }
    return {"first": _tier_dict_normalize(raw), "second": {}}


def load_all_tiers() -> dict[str, dict[str, list[str]]]:
    LEADS_PATH.parent.mkdir(parents=True, exist_ok=True)
    if not LEADS_PATH.is_file():
        payload = {"first": {}, "second": {}}
        LEADS_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        return payload
    bak = LEADS_PATH.with_name(LEADS_PATH.name + ".bak")
    raw = None
    for path in (LEADS_PATH, bak):
        if not path.is_file():
            continue
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if path is bak:
                logger.warning(
                    "bin_leads.json was unreadable — loaded from bin_leads.json.bak"
                )
            break
        except (json.JSONDecodeError, OSError) as e:
            raw = None
            if path is LEADS_PATH:
                logger.error("bin_leads.json invalid (%s); trying .bak if present", e)
    if raw is None:
        logger.error(
            "bin_leads.json and .bak missing or corrupt — treating as empty piles "
            "(restore from backup if needed)"
        )
        return {"first": {}, "second": {}}
    data = _parse_file_raw(raw)
    # Persist migration from v1 → v2 once
    if raw and "first" not in raw and "second" not in raw:
        save_all_tiers(data)
    return data


def save_all_tiers(data: dict[str, dict[str, list[str]]]) -> None:
    LEADS_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "first": {k: v for k, v in data.get("first", {}).items() if v},
        "second": {k: v for k, v in data.get("second", {}).items() if v},
    }
    _backup_sidecar_if_nonempty(LEADS_PATH)
    LEADS_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def load_leads(tier: str = "first") -> dict[str, list[str]]:
    t = norm_stock_tier(tier)
    return dict(load_all_tiers().get(t, {}))


def clear_bin_leads() -> None:
    save_all_tiers({"first": {}, "second": {}})


def merge_groups_from_web(groups: dict, tier: str = "first") -> dict:
    """
    Merge groups into first or second pile + catalog BIN list.
    tier: 'first' | 'second'
    """
    from catalog_store import merge_bins_to_catalog

    t = norm_stock_tier(tier)
    all_t = load_all_tiers()
    data = dict(all_t.get(t, {}))
    bin_keys: list[str] = []
    lines_added = 0

    for key, raw_lines in groups.items():
        nb = _norm_bin(key)
        if not nb:
            continue
        bin_keys.append(nb)
        if not isinstance(raw_lines, list):
            continue
        if nb not in data:
            data[nb] = []
        seen = set(data[nb])
        for line in raw_lines:
            s = str(line).strip()
            if not s or s in seen:
                continue
            data[nb].append(s)
            seen.add(s)
            lines_added += 1

    all_t[t] = data
    save_all_tiers(all_t)
    merge_bins_to_catalog(bin_keys)
    return {
        "tier": t,
        "bins_touched": len(set(bin_keys)),
        "lines_added": lines_added,
        "total_bins_with_data": len(data),
    }


def get_lines_for_bin(bin6: str, tier: str = "first") -> list[str]:
    nb = _norm_bin(bin6)
    if not nb:
        return []
    return list(load_leads(tier).get(nb, []))


def state_from_line(line: str) -> str:
    parts = line.split("|")
    if len(parts) <= 7:
        return ""
    s = parts[7].strip().strip('"').strip()
    return s.upper() if s else ""


# Appended by BIN web tool on sync so Telegram can filter by issuer (HandyAPI).
META_BANK_SUFFIX = "|§b§"


def strip_lead_sync_suffix(line: str) -> str:
    """Strip trailing `|§b§Issuer` added on HTML sync; delivery should show raw lead only."""
    s = str(line).strip()
    if META_BANK_SUFFIX not in s:
        return s
    return s.rsplit(META_BANK_SUFFIX, 1)[0].rstrip()


def bank_from_line(line: str) -> str:
    """Issuer name if line was synced from HTML with HandyAPI bank tag; else empty."""
    s = str(line).strip()
    if META_BANK_SUFFIX not in s:
        return ""
    return s.rsplit(META_BANK_SUFFIX, 1)[-1].strip().strip('"').strip()


def city_from_line(line: str) -> str:
    parts = str(line).strip().split("|")
    if len(parts) <= 6:
        return ""
    return parts[6].strip().strip('"').strip()


def zip_from_line(line: str) -> str:
    """ZIP / postal field when present (pipe index 8: ...|city|state|zip|...)."""
    parts = str(line).strip().split("|")
    if len(parts) <= 8:
        return ""
    return parts[8].strip().strip('"').strip()


def _zip_digits(s: str) -> str:
    return "".join(c for c in str(s).strip() if c.isdigit())


def _zip_bucket_key(raw_zip: str) -> str:
    """Normalize ZIP for picklist keys and matching (US ZIP+4 → first 5 digits)."""
    d = _zip_digits(raw_zip)
    if len(d) >= 5:
        return d[:5]
    if len(d) >= 3:
        return d
    return ""


def _line_zip_matches(filter_key: str, line: str) -> bool:
    fk = _zip_bucket_key(filter_key)
    if not fk:
        return False
    ld = _zip_digits(zip_from_line(line))
    if not ld:
        return False
    return ld.startswith(fk) or fk.startswith(ld) or fk == ld


def _norm_match_token(s: str) -> str:
    return " ".join(str(s).split()).casefold()


def _aggregate_zip_state_city() -> tuple[
    list[tuple[str, dict[str, int]]],
    list[tuple[str, dict[str, int]]],
    list[tuple[str, dict[str, int]]],
]:
    zips: dict[str, dict[str, int]] = defaultdict(lambda: {"first": 0, "second": 0})
    states: dict[str, dict[str, int]] = defaultdict(lambda: {"first": 0, "second": 0})
    cities: dict[str, dict[str, int]] = defaultdict(lambda: {"first": 0, "second": 0})
    for tier in ("first", "second"):
        pile = load_leads(tier)
        for lines in pile.values():
            for line in lines:
                zk = _zip_bucket_key(zip_from_line(line))
                if zk:
                    zips[zk][tier] += 1
                st = state_from_line(line)
                if st:
                    states[st][tier] += 1
                ct = city_from_line(line)
                if ct:
                    cities[ct][tier] += 1

    def sort_items(d: dict[str, dict[str, int]]) -> list[tuple[str, dict[str, int]]]:
        items = [(k, dict(v)) for k, v in d.items()]
        items.sort(
            key=lambda x: (-(x[1]["first"] + x[1]["second"]), x[0].casefold())
        )
        return items

    return sort_items(zips), sort_items(states), sort_items(cities)


def filter_pick_bins_merged() -> list[tuple[str, dict[str, int]]]:
    cf = bin_line_counts("first")
    cs = bin_line_counts("second")
    out: list[tuple[str, dict[str, int]]] = []
    for b in sorted(set(cf) | set(cs), key=lambda x: (-(cf.get(x, 0) + cs.get(x, 0)), x)):
        fc, sc = cf.get(b, 0), cs.get(b, 0)
        if fc + sc > 0:
            out.append((b, {"first": fc, "second": sc}))
    return out


def filter_dimension_picklists(
    *,
    max_cities: int = 200,
) -> dict[str, list[tuple[str, dict[str, int]]]]:
    z, s, c = _aggregate_zip_state_city()
    return {
        "zip": z,
        "state": s,
        "city": c[:max_cities],
        "bin": filter_pick_bins_merged(),
    }


def total_line_count(tier: str | None = None) -> int:
    if tier is None:
        return total_line_count("first") + total_line_count("second")
    return sum(len(v) for v in load_leads(tier).values())


def bin_line_counts(tier: str = "first") -> dict[str, int]:
    return {b: len(lines) for b, lines in load_leads(tier).items()}


def state_breakdown_for_bin(bin6: str, max_states: int = 6, *, tier: str = "first") -> str:
    lines = get_lines_for_bin(bin6, tier)
    hist: dict[str, int] = {}
    for ln in lines:
        st = state_from_line(ln)
        if st:
            hist[st] = hist.get(st, 0) + 1
    if not hist:
        return ""
    parts = sorted(hist.items(), key=lambda x: (-x[1], x[0]))[:max_states]
    return ", ".join(f"{s}×{c}" for s, c in parts)


def states_compact_for_bin(bin6: str, *, tier: str = "first") -> str:
    lines = get_lines_for_bin(bin6, tier)
    uniq: list[str] = []
    seen: set[str] = set()
    for ln in lines:
        st = state_from_line(ln)
        if st and st not in seen:
            seen.add(st)
            uniq.append(st)
    if not uniq:
        return "—"
    n_distinct = len({state_from_line(l) for l in lines if state_from_line(l)})
    if len(uniq) == 1:
        return uniq[0][:10]
    more = "+" if n_distinct > 2 else ""
    a, b = uniq[0][:5], uniq[1][:5]
    return f"{a}|{b}{more}"[:14]


def _remove_one_line(
    data: dict[str, list[str]], bin6: str, line: str
) -> None:
    lines = data.get(bin6, [])
    try:
        idx = lines.index(line)
    except ValueError:
        return
    lines.pop(idx)
    if not lines:
        del data[bin6]
    else:
        data[bin6] = lines


def restore_pairs_triples(pairs: list[tuple[str, str, str]]) -> None:
    """(bin, line, tier)"""
    if not pairs:
        return
    all_t = load_all_tiers()
    for b, line, tier in pairs:
        k = norm_stock_tier(tier)
        nb = _norm_bin(b)
        if not nb:
            continue
        s = str(line).strip()
        if not s:
            continue
        slot = all_t.setdefault(k, {})
        slot.setdefault(nb, []).append(s)
    save_all_tiers(all_t)


def pop_n_random_from_bin(
    bin6: str, n: int, tier: str = "first"
) -> list[tuple[str, str]] | None:
    t = norm_stock_tier(tier)
    nb = _norm_bin(bin6)
    if not nb or n < 1:
        return None
    all_t = load_all_tiers()
    data = dict(all_t.get(t, {}))
    lines = list(data.get(nb, []))
    if len(lines) < n:
        return None
    picks = random.sample(lines, n)
    for line in picks:
        _remove_one_line(data, nb, line)
    all_t[t] = data
    save_all_tiers(all_t)
    return [(nb, line) for line in picks]


def pop_n_random_any(n: int, tier: str = "first") -> list[tuple[str, str]] | None:
    t = norm_stock_tier(tier)
    if n < 1:
        return None
    all_t = load_all_tiers()
    data = dict(all_t.get(t, {}))
    pool: list[tuple[str, str]] = []
    for b, lines in data.items():
        for line in lines:
            pool.append((b, line))
    if len(pool) < n:
        return None
    chosen = random.sample(pool, n)
    for b, line in chosen:
        _remove_one_line(data, b, line)
    all_t[t] = data
    save_all_tiers(all_t)
    return chosen


def _line_matches_filters(
    bin_key: str,
    line: str,
    *,
    state: str | None = None,
    bin6: str | None = None,
    city: str | None = None,
    zip_code: str | None = None,
    brand: str | None = None,
) -> bool:
    if bin6:
        nb = _norm_bin(bin6)
        if nb and _norm_bin(bin_key) != nb:
            return False
    if state:
        st = state.strip().upper()
        if st and st != "ALL":
            ls = state_from_line(line)
            if ls != st:
                return False
    if city:
        fc = _norm_match_token(city)
        if fc and fc != "all":
            lc = _norm_match_token(city_from_line(line))
            if not lc:
                return False
            if fc not in lc and lc not in fc:
                return False
    if zip_code:
        zf = str(zip_code).strip()
        if zf and zf.upper() != "ALL":
            if not _line_zip_matches(zf, line):
                return False
    if brand:
        fb = str(brand).strip().lower()
        if fb and fb != "all":
            lb = card_brand_from_bin6(bin_key)
            if lb != fb:
                return False
    return True


def pop_n_random_filtered(
    n: int,
    tier: str = "first",
    *,
    state: str | None = None,
    bin6: str | None = None,
    city: str | None = None,
    zip_code: str | None = None,
    brand: str | None = None,
) -> list[tuple[str, str]] | None:
    """Random draw from tier pile, optionally filtered by state, BIN, city, ZIP, and/or card brand (from BIN)."""
    t = norm_stock_tier(tier)
    if n < 1:
        return None
    st_clean = (state or "").strip().upper() or None
    if st_clean == "ALL":
        st_clean = None
    nb_filter = _norm_bin(bin6) if bin6 else None
    ct_clean = (city or "").strip() or None
    if ct_clean and ct_clean.upper() == "ALL":
        ct_clean = None
    zc_clean = (zip_code or "").strip() or None
    if zc_clean and zc_clean.upper() == "ALL":
        zc_clean = None
    br_clean = (brand or "").strip().lower() or None
    if br_clean and br_clean.upper() == "ALL":
        br_clean = None

    all_t = load_all_tiers()
    data = dict(all_t.get(t, {}))
    pool: list[tuple[str, str]] = []
    for b, lines in data.items():
        for line in lines:
            if _line_matches_filters(
                b,
                line,
                state=st_clean,
                bin6=nb_filter,
                city=ct_clean,
                zip_code=zc_clean,
                brand=br_clean,
            ):
                pool.append((b, line))
    if len(pool) < n:
        return None
    chosen = random.sample(pool, n)
    for b, line in chosen:
        _remove_one_line(data, b, line)
    all_t[t] = data
    save_all_tiers(all_t)
    return chosen


def count_matching_lines(
    tier: str,
    *,
    state: str | None = None,
    bin6: str | None = None,
    city: str | None = None,
    zip_code: str | None = None,
    brand: str | None = None,
) -> int:
    t = norm_stock_tier(tier)
    st_clean = (state or "").strip().upper() or None
    if st_clean == "ALL":
        st_clean = None
    nb_filter = _norm_bin(bin6) if bin6 else None
    ct_clean = (city or "").strip() or None
    if ct_clean and ct_clean.upper() == "ALL":
        ct_clean = None
    zc_clean = (zip_code or "").strip() or None
    if zc_clean and zc_clean.upper() == "ALL":
        zc_clean = None
    br_clean = (brand or "").strip().lower() or None
    if br_clean and br_clean.upper() == "ALL":
        br_clean = None
    data = dict(load_all_tiers().get(t, {}))
    n = 0
    for b, lines in data.items():
        for line in lines:
            if _line_matches_filters(
                b,
                line,
                state=st_clean,
                bin6=nb_filter,
                city=ct_clean,
                zip_code=zc_clean,
                brand=br_clean,
            ):
                n += 1
    return n


def format_notebook_text(bin6: str, lines: list[str]) -> str:
    nb = _norm_bin(bin6) or bin6
    n = len(lines)
    header = "══════════════════════════════════════════════════\n"
    header += f"  BIN: {nb}  |  total entries: {n}\n"
    header += "══════════════════════════════════════════════════\n\n"
    body = "\n".join(
        f"[{i + 1}] {strip_lead_sync_suffix(line)}" for i, line in enumerate(lines)
    )
    body += "\n\n—— end of group ——"
    return header + body


def format_sendout_tiers_block() -> str:
    """Telegram sendout: catalog BINs with per-tier counts."""
    from catalog_store import load_catalog

    cat = load_catalog()
    first_p = float(cat.get("price_per_bin", 0.90))
    bins: list[str] = cat.get("bins", [])
    cf = bin_line_counts("first")
    cs = bin_line_counts("second")
    lines = [
        "📤 BIN SENDOUT (two piles)",
        f"Firsthand: ${first_p:.2f}/lead · Secondhand: ${SECONDHAND_PRICE_USD:.2f}/lead",
        "",
    ]
    if not bins:
        lines.append("(no BINs in catalog)")
        return "\n".join(lines)

    lines.append("━━ FIRSTHAND ━━")
    for b in bins:
        n = cf.get(b, 0)
        if n:
            lines.append(f"  {b}  ×{n}")
    if not any(cf.get(b, 0) for b in bins):
        lines.append("  (no firsthand lines)")

    lines.append("")
    lines.append("━━ SECONDHAND ━━")
    for b in bins:
        n = cs.get(b, 0)
        if n:
            lines.append(f"  {b}  ×{n}")
    if not any(cs.get(b, 0) for b in bins):
        lines.append("  (no secondhand lines)")
    return "\n".join(lines)


def stock_tiers_api_payload() -> dict:
    from catalog_store import load_catalog

    cat = load_catalog()
    first_p = float(cat.get("price_per_bin", 0.90))
    bins = cat.get("bins", [])
    cf = bin_line_counts("first")
    cs = bin_line_counts("second")

    def chips(counts: dict[str, int]) -> list[dict]:
        out = []
        for b in bins:
            c = counts.get(b, 0)
            if c:
                out.append({"bin": b, "count": c})
        for b in sorted(counts.keys()):
            if b not in bins and counts[b]:
                out.append({"bin": b, "count": counts[b]})
        return out

    return {
        "first": {
            "price": first_p,
            "total_lines": total_line_count("first"),
            "bins": chips(cf),
        },
        "second": {
            "price": SECONDHAND_PRICE_USD,
            "total_lines": total_line_count("second"),
            "bins": chips(cs),
        },
        "catalog_bins": bins,
    }


def extract_bin_from_line(line: str) -> str | None:
    """First 6 digits of card field (before first |), same rules as the web BIN tool."""
    s = line.strip()
    if not s:
        return None
    first_pipe = s.find("|")
    if first_pipe == -1:
        return None
    card_raw = s[:first_pipe].strip().strip('"').strip()
    digits_only = "".join(c for c in card_raw if c.isdigit())
    if len(digits_only) < 6:
        return None
    return digits_only[:6]


def groups_from_raw_paste(text: str) -> dict[str, list[str]]:
    """Group non-empty lines by BIN for merge_groups_from_web."""
    groups: dict[str, list[str]] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        b = extract_bin_from_line(line)
        if not b:
            continue
        groups.setdefault(b, []).append(line)
    return groups


def try_restore_leads_from_bak() -> tuple[bool, str]:
    """Copy last good stock from bin_leads.json.bak over bin_leads.json (admin recovery)."""
    bak = LEADS_PATH.with_name(LEADS_PATH.name + ".bak")
    if not bak.is_file():
        return False, "No bin_leads.json.bak on disk (backup is created on each successful save)."
    try:
        raw = json.loads(bak.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        return False, f"bin_leads.json.bak unreadable: {e}"
    if not isinstance(raw, dict):
        return False, "bin_leads.json.bak is not a JSON object."
    data = _parse_file_raw(raw)
    save_all_tiers(data)
    return True, "Restored bin_leads.json from bin_leads.json.bak."
