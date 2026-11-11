"""Unmatched web sign-ups: READ-ONLY report for the dashboard and a small JSON endpoint.

A web opt-in (Postgres table optin_signups, saved by POST /api/v1/optin) is flagged when:
  * "no phone match"          consent given, but its phone is on no Customers-tab row
  * "matched but not marked"  consent given, phone is on a Customers row, but that row's
                              Opt-In Status / Opt-In Method / Opt-In Date (L/M/N) aren't filled
  * "no consent"              the SMS box wasn't ticked: contact only, never opt them in

Only the Customers tab counts as a match. Import Review rows are shown as a hint only.
Nothing here writes to the Sheet or the database, and phones only ever leave as last 4.
Clyde confirms each match by hand.
"""
import difflib
import re
from datetime import datetime
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

CT = ZoneInfo("America/Chicago")
NAME_COLS = ("Customer Name", "Name")
PHONE_COLS = ("Phone (+1XXXXXXXXXX)", "Phone")
OPTIN_COLS = ("Opt-In Status", "Opt-In Method", "Opt-In Date")
REASON_NO_MATCH = "no phone match"
REASON_NOT_MARKED = "matched but not marked"
REASON_NO_CONSENT = "no consent"
NAME_RATIO = 0.84


def digits10(raw) -> str:
    """Comparable phone key: last 10 digits (+1, spaces, dashes, dots, parens ignored)."""
    d = re.sub(r"\D", "", str(raw if raw is not None else ""))
    if len(d) == 11 and d.startswith("1"):
        d = d[1:]
    return d if len(d) == 10 else ""


def last4(raw) -> str:
    d = re.sub(r"\D", "", str(raw if raw is not None else ""))
    return d[-4:] if len(d) >= 4 else ""


def _get(d, names):
    for n in names:
        v = d.get(n)
        if v not in (None, ""):
            return str(v).strip()
    return ""


def is_marked(row: dict) -> bool:
    """L/M/N all filled and L says Opted in."""
    status = str(row.get("Opt-In Status") or "").strip().lower()
    return status == "opted in" and all(str(row.get(c) or "").strip() for c in OPTIN_COLS[1:])


def _norm_name(s) -> str:
    s = re.sub(r"\(.*?\)", " ", str(s or "").lower())
    return " ".join(re.sub(r"[^a-z ]", " ", s).split())


def name_score(a, b) -> float:
    """0..1 fuzzy similarity of two person names (order-insensitive)."""
    a, b = _norm_name(a), _norm_name(b)
    if not a or not b:
        return 0.0
    ta, tb = a.split(), b.split()
    best = max(difflib.SequenceMatcher(None, a, b).ratio(),
               difflib.SequenceMatcher(None, " ".join(sorted(ta)), " ".join(sorted(tb))).ratio())
    # "Sydney" vs "Sydney Faucheaux": a one-word name that equals a word of the other name.
    if len(ta) == 1 and ta[0] in tb and len(ta[0]) >= 3:
        best = max(best, 0.85)
    if len(tb) == 1 and tb[0] in ta and len(tb[0]) >= 3:
        best = max(best, 0.85)
    # same last name + same first initial
    if len(ta) > 1 and len(tb) > 1 and ta[-1] == tb[-1] and ta[0][0] == tb[0][0]:
        best = max(best, 0.9)
    return best


def likely_matches(name, customers, limit=3):
    """Customers rows whose name looks like `name`: [{account, name, row, score}] (no phones)."""
    out = []
    for c in customers:
        cn = _get(c, NAME_COLS)
        sc = name_score(name, cn)
        if sc >= NAME_RATIO:
            out.append({"account": str(c.get("Account") or "").strip(), "name": cn,
                        "row": c.get("_row"), "score": round(sc, 2)})
    out.sort(key=lambda m: -m["score"])
    return out[:limit]


def page_tag(tech, page_url) -> str:
    """'blain · /optin/blain' (path only: no query string, no host)."""
    path = ""
    if page_url:
        try:
            path = urlparse(str(page_url)).path or ""
        except ValueError:
            path = ""
    return " · ".join(x for x in (str(tech or "").strip(), path[:80]) if x)


def _as_ct(ts):
    if isinstance(ts, str):
        ts = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    if ts is None:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=CT)
    return ts.astimezone(CT)


def find_unmatched(signups, customers, review_rows=()):
    """signups: dicts {id, created_at, name, phone, sms_consent, tech, page_url}
    customers: Customers-tab row dicts keyed by header (dashboard.fetch_rows()["rows"])
    review_rows: optional Import Review rows {source_tab, sheet_row, name, phone} (hint only)

    One entry per person (normalized phone); the latest submission supplies name/time/page,
    consent = yes if any of their submissions ticked the box. Returns flagged entries,
    newest first, each with phone_last4 only."""
    by_phone = {}
    for c in customers:
        k = digits10(_get(c, PHONE_COLS))
        if k:
            by_phone.setdefault(k, []).append(c)
    review_by_phone = {}
    for r in review_rows or ():
        k = digits10(r.get("phone"))
        if k:
            review_by_phone.setdefault(k, []).append(r)

    people = {}
    for s in signups:
        k = digits10(s.get("phone")) or ("raw:" + str(s.get("phone") or ""))
        people.setdefault(k, []).append(s)

    out = []
    for k, subs in people.items():
        subs = sorted(subs, key=lambda s: _as_ct(s.get("created_at")) or datetime.min.replace(tzinfo=CT))
        last = subs[-1]
        consent = any(bool(s.get("sms_consent")) for s in subs)
        hits = by_phone.get(k, [])
        reasons = []
        if not consent:
            reasons.append(REASON_NO_CONSENT)
        elif not hits:
            reasons.append(REASON_NO_MATCH)
        elif not any(is_marked(h) for h in hits):
            reasons.append(REASON_NOT_MARKED)
        if not reasons:
            continue
        when = _as_ct(last.get("created_at"))
        name = str(last.get("name") or "").strip()
        entry = {
            "ids": [s.get("id") for s in subs],
            "name": name,
            "phone_last4": last4(last.get("phone")),
            "submitted_ct": when.strftime("%Y-%m-%d %H:%M") if when else "",
            "submitted_label": when.strftime("%b %-d, %Y %-I:%M %p CT") if when else "",
            "submissions": len(subs),
            "consent": consent,
            "page": page_tag(last.get("tech"), last.get("page_url")),
            "reasons": reasons,
            "actionable": consent,  # consent given -> Clyde should fix the Sheet; no consent -> FYI only
            "matched_rows": [{"row": h.get("_row"), "account": str(h.get("Account") or "").strip(),
                              "name": _get(h, NAME_COLS),
                              "opt_in_status": str(h.get("Opt-In Status") or "").strip()} for h in hits],
            "likely_match": [] if hits else likely_matches(name, customers),
            "import_review_hint": [{"tab": r.get("source_tab"), "row": r.get("sheet_row"),
                                    "name": str(r.get("name") or "").strip()} for r in review_by_phone.get(k, [])],
        }
        out.append(entry)
    out.sort(key=lambda e: e["submitted_ct"], reverse=True)
    return out


def summary(entries) -> dict:
    return {"actionable": sum(1 for e in entries if e["actionable"]),
            "no_consent": sum(1 for e in entries if not e["consent"]),
            "total": len(entries)}
