#!/usr/bin/env python3
"""founder_dossier.py — build a one-page research dossier per startup, public sources only.

Usage:
    SEC_USER_AGENT="Your Name you@example.com" \
        python scripts/founder_dossier.py --name "Acme" --domain acme.com
    SEC_USER_AGENT=... python scripts/founder_dossier.py --csv leads.csv --out dossiers/

leads.csv needs columns: name,domain

Sources (no LinkedIn, no logins):
  * Jina Reader   -> homepage / about / team / blog as Markdown
  * SEC EDGAR     -> Form D (amount raised, named officers/directors)
  * sloptrim      -> optional 0-100 "AI tells" score of the site copy (--sloptrim PATH)

The dossier leaves the outreach hook blank on purpose: the agent fills it in after reading.
"""

import argparse
import csv
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

JINA = "https://r.jina.ai/"
EDGAR_SEARCH = "https://efts.sec.gov/LATEST/search-index"
SITE_PATHS = ["", "/about", "/team", "/blog"]
DOMAIN_RE = re.compile(r"^(?=.{4,253}$)([a-z0-9-]+\.)+[a-z]{2,}$")
DATE_RE = re.compile(
    r"\b(20\d{2}-\d{2}-\d{2}|"
    r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.? \d{1,2},? 20\d{2})\b"
)


def http_get(url, headers=None, timeout=30):
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


def clean_domain(raw):
    d = raw.strip().lower()
    d = re.sub(r"^https?://", "", d).split("/")[0].removeprefix("www.")
    if not DOMAIN_RE.match(d):
        raise ValueError(f"not a valid domain: {raw!r}")
    return d


def slug(text):
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "lead"


def fetch_site(domain, pause=1.0):
    """Return {path: markdown or None} via Jina Reader."""
    pages = {}
    for path in SITE_PATHS:
        url = JINA + f"https://{domain}{path}"
        try:
            pages[path or "/"] = http_get(url, {"Accept": "text/markdown"})
        except Exception as exc:  # network/404: record and move on
            pages[path or "/"] = None
            print(f"  ! {domain}{path}: {exc}", file=sys.stderr)
        time.sleep(pause)
    return pages


def blog_dates(markdown):
    """Dates seen on the blog page, newest first (rough posting-cadence signal)."""
    return sorted(set(DATE_RE.findall(markdown or "")), reverse=True)


# ---- SEC Form D ---------------------------------------------------------------


def _local(tag):
    return tag.rsplit("}", 1)[-1]


def _find_text(root, name):
    for el in root.iter():
        if _local(el.tag) == name and el.text and el.text.strip():
            return el.text.strip()
    return None


def parse_form_d(xml_text):
    """Pull amounts, industry and named people out of a Form D primary_doc.xml."""
    root = ET.fromstring(xml_text)
    people = []
    for el in root.iter():
        if _local(el.tag) != "relatedPersonInfo":
            continue
        first = _find_text(el, "firstName") or ""
        last = _find_text(el, "lastName") or ""
        roles = [r.text.strip() for r in el.iter() if _local(r.tag) == "relationship" and r.text]
        if first or last:
            people.append({"name": f"{first} {last}".strip(), "roles": roles})

    def amount(name):
        val = _find_text(root, name)
        try:
            return int(float(val)) if val else None
        except ValueError:
            return None

    return {
        "issuer": _find_text(root, "entityName"),
        "offering": amount("totalOfferingAmount"),
        "sold": amount("totalAmountSold"),
        "industry": _find_text(root, "industryGroupType"),
        "people": people,
    }


def in_range(form_d, low, high):
    amt = form_d.get("sold") or form_d.get("offering")
    return amt is not None and low <= amt <= high


def edgar_form_d(name, user_agent, pause=0.2):
    """Latest Form D hit for a company name; returns parsed dict + filing url, or None."""
    headers = {"User-Agent": user_agent}
    query = urllib.parse.urlencode({"q": f'"{name}"', "forms": "D"})
    hits = json.loads(http_get(f"{EDGAR_SEARCH}?{query}", headers)).get("hits", {}).get("hits", [])
    time.sleep(pause)
    for hit in hits:
        src = hit.get("_source", {})
        names = [n.lower() for n in src.get("display_names", [])]
        if not any(name.lower() in n for n in names):
            continue  # skip fuzzy matches on other companies
        adsh = src.get("adsh", "")
        ciks = src.get("ciks") or []
        if not (adsh and ciks):
            continue
        url = (
            f"https://www.sec.gov/Archives/edgar/data/{int(ciks[0])}/"
            f"{adsh.replace('-', '')}/primary_doc.xml"
        )
        parsed = parse_form_d(http_get(url, headers))
        parsed.update(filed=src.get("file_date"), url=url)
        time.sleep(pause)
        return parsed
    return None


# ---- optional slop score --------------------------------------------------------


def slop_score(sloptrim_dir, text):
    """Score text with a local sloptrim checkout. Returns int or None."""
    detect = Path(sloptrim_dir) / "scripts" / "detect.py"
    if not detect.is_file() or not text:
        return None
    with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as tmp:
        tmp.write(text)
    try:
        out = subprocess.run(
            [sys.executable, "-I", str(detect), tmp.name],
            capture_output=True, text=True, timeout=60, check=True,
        ).stdout
        return json.loads(out).get("score")
    except Exception as exc:
        print(f"  ! sloptrim failed: {exc}", file=sys.stderr)
        return None
    finally:
        os.unlink(tmp.name)


# ---- dossier ----------------------------------------------------------------------


def money(n):
    return f"${n:,}" if n else "unknown"


def excerpt(markdown, limit=1200):
    return (markdown or "").strip()[:limit] or "_not fetched_"


def build_dossier(name, domain, pages, form_d, score, low, high):
    lines = [f"# {name}  ({domain})", ""]
    lines += ["## Funding (SEC Form D)"]
    if form_d:
        fit = "YES" if in_range(form_d, low, high) else "no"
        lines += [
            f"- In target range (${low:,}-${high:,}): **{fit}**",
            f"- Offering: {money(form_d['offering'])} · Sold: {money(form_d['sold'])}",
            f"- Filed: {form_d.get('filed')} · Industry: {form_d.get('industry')}",
            f"- Filing: {form_d['url']}",
            "- Named people on the filing:",
        ]
        lines += [f"  - {p['name']} — {', '.join(p['roles']) or 'role n/a'}" for p in form_d["people"]]
    else:
        lines += ["- No Form D found (may be a non-US raise, a different legal name, or unfunded)."]
    lines += ["", "## Site"]
    lines += [f"- Slop score (sloptrim, 0-100): {score if score is not None else 'not run'}"]
    dates = blog_dates(pages.get("/blog"))
    lines += [f"- Blog dates seen: {', '.join(dates[:5]) or 'none found'}"]
    for path, md in pages.items():
        lines += ["", f"### {path}", excerpt(md)]
    lines += ["", "## Outreach hook", "_agent: fill in after reading the above; cite something specific._", ""]
    return "\n".join(lines)


def run_one(name, domain, args, user_agent):
    domain = clean_domain(domain)
    pages = fetch_site(domain)
    form_d = None
    if user_agent:
        try:
            form_d = edgar_form_d(name, user_agent)
        except Exception as exc:
            print(f"  ! EDGAR lookup failed for {name}: {exc}", file=sys.stderr)
    copy = "\n\n".join(md for p, md in pages.items() if md and p != "/blog")
    score = slop_score(args.sloptrim, copy) if args.sloptrim else None
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"{slug(name)}.md"
    path.write_text(build_dossier(name, domain, pages, form_d, score, args.low, args.high))
    print(f"wrote {path}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--name")
    ap.add_argument("--domain")
    ap.add_argument("--csv", help="CSV with name,domain columns")
    ap.add_argument("--out", default="dossiers")
    ap.add_argument("--low", type=int, default=3_000_000)
    ap.add_argument("--high", type=int, default=20_000_000)
    ap.add_argument("--sloptrim", help="path to a local sloptrim checkout (optional)")
    args = ap.parse_args()

    user_agent = os.environ.get("SEC_USER_AGENT")
    if not user_agent:
        print("SEC_USER_AGENT not set -> skipping EDGAR (set it to 'Your Name you@example.com').",
              file=sys.stderr)

    if args.csv:
        with open(args.csv, newline="") as fh:
            leads = [(r["name"], r["domain"]) for r in csv.DictReader(fh)]
    elif args.name and args.domain:
        leads = [(args.name, args.domain)]
    else:
        ap.error("give --name and --domain, or --csv")
    for name, domain in leads:
        print(f"== {name}")
        run_one(name, domain, args, user_agent)


if __name__ == "__main__":
    main()
