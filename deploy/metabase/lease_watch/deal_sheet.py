"""Make a PDF negotiation sheet for each numbered deal in the lease watch.

    python3 -I deal_sheet.py --data DIR --out DIR [--trade 10000] [--deal PL-001]

DIR holds the lease-watch CSVs (vehicles.csv, offers.csv, and optionally dealers.csv
with each dealer's contact details, matched on the dealer name). A deal is a specific
car with a deal_id (PL-001, ...). For each deal the newest "estimate" row in
offers.csv supplies the program assumptions (residual, money factor, lease
cash), and the newest vehicles.csv row supplies the car and dealer. The math
comes from lease_calc.py, so the sheets match the dashboard.

Needs reportlab.
"""

import argparse
import csv
import datetime as dt
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lease_calc import estimate, NJ_TAX  # noqa: E402

from reportlab.lib import colors  # noqa: E402
from reportlab.lib.enums import TA_LEFT  # noqa: E402
from reportlab.lib.pagesizes import letter  # noqa: E402
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet  # noqa: E402
from reportlab.lib.units import inch  # noqa: E402
from reportlab.platypus import (KeepTogether, Paragraph, SimpleDocTemplate,  # noqa: E402
                                Spacer, Table, TableStyle)

TERM = 36
MILES = 10000
ACQ_FEE = 650
REG_FEES = 350
TARGET_PAYMENT = 550
OPEN_DISCOUNT = 3000
TARGET_DISCOUNT = 2000

INK = colors.HexColor("#1f2933")
MUTED = colors.HexColor("#5f6b7a")
RULE = colors.HexColor("#d5dbe3")
BAND = colors.HexColor("#f2f5f8")
ACCENT = colors.HexColor("#1d4e89")
GOOD = colors.HexColor("#1e7b4f")
WARN = colors.HexColor("#a1471b")


def money(x, cents=False):
    if x is None:
        return "-"
    return f"${x:,.2f}" if cents else f"${x:,.0f}"


def num(v):
    v = (v or "").replace("$", "").replace(",", "").strip()
    return float(v) if v else None


def latest(rows, key):
    out = {}
    for r in rows:
        k = r.get(key)
        if k and (k not in out or r["check_date"] >= out[k]["check_date"]):
            out[k] = r
    return out


def breakdown(price, msrp, rpct, mf, trade, lease_cash, doc_fee):
    e = estimate(price, msrp, rpct, mf, trade, TERM, ACQ_FEE, doc_fee, REG_FEES, lease_cash)
    gross = price + ACQ_FEE - lease_cash
    adj = gross - e["cap_cost_reduction"]
    res = e["residual"]
    dep = (adj - res) / TERM
    rent = (adj + res) * mf
    due = e["monthly_payment"] + doc_fee + e["nj_sales_tax"] + REG_FEES + e["lfis"] + e["cap_cost_reduction"]
    return {**e, "price": price, "gross_cap": gross, "adj_cap": adj, "depreciation": dep,
            "rent": rent, "due_at_signing": due, "lease_cash": lease_cash,
            "trade_used": min(trade, due), "per_mf_tick": (adj + res) * 0.0001}


def max_price_for_payment(msrp, rpct, mf, trade, lease_cash, doc_fee, limit):
    lo, hi = msrp * 0.7, msrp * 1.3
    for _ in range(60):
        mid = (lo + hi) / 2
        e = estimate(mid, msrp, rpct, mf, trade, TERM, ACQ_FEE, doc_fee, REG_FEES, lease_cash)
        if e["monthly_payment"] <= limit and e["cash_at_signing"] == 0:
            lo = mid
        else:
            hi = mid
    return lo


def styles():
    s = getSampleStyleSheet()
    base = ParagraphStyle("base", parent=s["Normal"], fontName="Helvetica", fontSize=10,
                          leading=13.5, textColor=INK, alignment=TA_LEFT)
    return {
        "base": base,
        "small": ParagraphStyle("small", parent=base, fontSize=8.5, leading=11, textColor=MUTED),
        "title": ParagraphStyle("title", parent=base, fontName="Helvetica-Bold", fontSize=17,
                                leading=21, textColor=ACCENT),
        "h2": ParagraphStyle("h2", parent=base, fontName="Helvetica-Bold", fontSize=11.5,
                             leading=15, spaceBefore=10, spaceAfter=4, textColor=ACCENT),
        "cell": ParagraphStyle("cell", parent=base, fontSize=9.5, leading=12),
        "cellb": ParagraphStyle("cellb", parent=base, fontSize=9.5, leading=12, fontName="Helvetica-Bold"),
        "bullet": ParagraphStyle("bullet", parent=base, leftIndent=12, bulletIndent=2, spaceAfter=2),
    }


def grid(data, widths, header=True, bold_rows=(), zebra=True, align_right_from=1):
    t = Table(data, colWidths=widths, hAlign="LEFT")
    cmds = [("FONT", (0, 0), (-1, -1), "Helvetica", 9.5),
            ("TEXTCOLOR", (0, 0), (-1, -1), INK),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("TOPPADDING", (0, 0), (-1, -1), 2.2), ("BOTTOMPADDING", (0, 0), (-1, -1), 2.2),
            ("LINEBELOW", (0, 0), (-1, -1), 0.4, RULE),
            ("ALIGN", (align_right_from, 0), (-1, -1), "RIGHT")]
    if header:
        cmds += [("FONT", (0, 0), (-1, 0), "Helvetica-Bold", 9.5),
                 ("TEXTCOLOR", (0, 0), (-1, 0), ACCENT),
                 ("LINEBELOW", (0, 0), (-1, 0), 0.9, ACCENT)]
    for r in bold_rows:
        cmds += [("FONT", (0, r), (-1, r), "Helvetica-Bold", 9.5), ("BACKGROUND", (0, r), (-1, r), BAND)]
    if zebra:
        start = 1 if header else 0
        for i in range(start, len(data)):
            if i not in bold_rows and (i - start) % 2 == 1:
                cmds.append(("BACKGROUND", (0, i), (-1, i), colors.HexColor("#fafbfc")))
    t.setStyle(TableStyle(cmds))
    return t


def sheet(deal_id, v, o, trade, out_dir, generated, contact=None):
    st = styles()
    msrp = num(v.get("msrp")) or num(o.get("msrp"))
    doc_fee = num(v.get("doc_fee")) or 0
    rpct = num(o.get("residual_pct"))
    mf = num(o.get("money_factor"))
    lc = num(o.get("lease_cash")) or 0
    year, vin = v.get("model_year", ""), v.get("vin", "")
    dealer = v.get("dealer", "")

    opening = breakdown(msrp - OPEN_DISCOUNT, msrp, rpct, mf, trade, lc, doc_fee)
    target = breakdown(msrp - TARGET_DISCOUNT, msrp, rpct, mf, trade, lc, doc_fee)
    at_msrp = breakdown(msrp, msrp, rpct, mf, trade, lc, doc_fee)

    slug = re.sub(r"[^A-Za-z0-9]+", "-", dealer).strip("-")
    path = Path(out_dir) / f"{deal_id}_{slug}_{year}_{vin[-6:]}.pdf"
    doc = SimpleDocTemplate(str(path), pagesize=letter, leftMargin=0.65 * inch, rightMargin=0.65 * inch,
                            topMargin=0.6 * inch, bottomMargin=0.6 * inch,
                            title=f"Deal {deal_id} negotiation sheet", author="Palisade lease watch")
    W = letter[0] - 1.3 * inch
    story = []

    story.append(Paragraph(f"Deal {deal_id} &nbsp;&middot;&nbsp; Negotiation sheet", st["title"]))
    story.append(Paragraph(
        f"{year} Palisade Hybrid {v.get('trim', '')}, " + (f"{v['seats']} seats, " if v.get("seats") else "") +
        f"{v.get('exterior', '')} / {v.get('interior', '')} &nbsp;&middot;&nbsp; {dealer}, {v.get('location', '')}",
        st["base"]))
    story.append(Paragraph(
        f"Prepared {generated:%b %d, %Y} from listings seen {v.get('check_date', '')}. "
        "<b>These are estimates, not a dealer offer.</b> Replace them with the dealer's written worksheet.",
        st["small"]))
    if (v.get("notes") or "").strip():
        story.append(Paragraph(f"<b>Note:</b> {v['notes'].strip()}".replace("&", "&amp;").replace("&amp;nbsp;", "&nbsp;"),
                               ParagraphStyle("note", parent=st["small"], textColor=WARN)))
    story.append(Spacer(1, 8))

    # Car and terms side by side
    car = [["The car", ""],
           ["VIN", vin], ["Dealer", f"{dealer}, {v.get('location', '')} ({v.get('miles_from_nb', '')} mi)"],
           ["MSRP", money(msrp)], ["Advertised price", money(num(v.get("advertised_price")))],
           ["Doc fee", money(doc_fee)], ["Listing status", v.get("status", "")]]
    terms = [["Your terms", ""],
             ["Lease", f"{TERM} months, {MILES:,} miles/yr"],
             ["Payment goal", f"{money(TARGET_PAYMENT)}/mo or less"],
             ["Cash at signing", "$0 beyond the trade"],
             ["Trade-in", "2020 Santa Fe, paid off"], ["Trade value used", money(trade)],
             ["Program ends", o.get("expires", "") or "-"]]
    left = grid(car, [1.15 * inch, 2.1 * inch], header=True, zebra=False, align_right_from=2)
    right = grid(terms, [1.2 * inch, 2.0 * inch], header=True, zebra=False, align_right_from=2)
    story.append(Table([[left, right]], colWidths=[W / 2, W / 2], hAlign="LEFT",
                       style=[("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 0)]))

    story.append(Paragraph("Dealer contact", st["h2"]))
    c = contact or {}
    if c:
        email = c.get("contact_email", "")
        if email and c.get("email_note"):
            email += f" ({c['email_note']})"
        person = ", ".join(x for x in (c.get("contact_name"), c.get("contact_title")) if x)
        rows = [["Address", Paragraph(c.get("address", "") or "-", st["cell"]),
                 "Sales phone", c.get("sales_phone", "") or "-"],
                ["Contact", Paragraph(person or "-", st["cell"]),
                 "Direct", c.get("contact_phone", "") or c.get("main_phone", "") or "-"],
                ["Email", Paragraph(email or "Use the website contact form", st["cell"]),
                 "Sales hours", Paragraph(c.get("sales_hours", "") or "-", st["cell"])],
                ["Also", Paragraph(c.get("other_contacts", "") or "-", st["cell"]),
                 "Website", Paragraph((c.get("contact_page") or c.get("website") or "-").replace("&", "&amp;"), st["cell"])]]
        ct = grid(rows, [0.7 * inch, 3.1 * inch, 0.9 * inch, W - 4.7 * inch], header=False, zebra=False,
                  align_right_from=9)
        ct.setStyle(TableStyle([("FONT", (0, 0), (0, -1), "Helvetica-Bold", 9.5),
                                ("FONT", (2, 0), (2, -1), "Helvetica-Bold", 9.5),
                                ("VALIGN", (0, 0), (-1, -1), "TOP")]))
        story.append(ct)
        story.append(Paragraph(f"Contact details from the dealer's website, checked {c.get('checked_date', '') or '-'}.",
                               st["small"]))
    else:
        story.append(Paragraph("Not looked up yet: see the dealer's website.", st["small"]))

    story.append(Paragraph("Program assumptions", st["h2"]))
    apr = mf * 2400
    pa = grid([
        ["Residual", f"{rpct:.0f}% = {money(at_msrp['residual'])} (implied)",
         "Lease cash", f"{money(lc)}, ends {o.get('expires', '') or '-'}"],
        ["Money factor", f"{mf:.5f}, about {apr:.1f}% APR (implied)", "Acquisition fee", money(ACQ_FEE)],
        ["NJ sales tax", f"{NJ_TAX * 100:.3f}%, trade value deducted", "Luxury surcharge", "0.4% of price"],
        ["Registration", f"{money(REG_FEES)} (estimate)", "Doc fee", money(doc_fee)],
    ], [1.0 * inch, 2.55 * inch, 1.15 * inch, W - 4.7 * inch], header=False, zebra=False, align_right_from=9)
    pa.setStyle(TableStyle([("FONT", (0, 0), (0, -1), "Helvetica-Bold", 9.5),
                            ("FONT", (2, 0), (2, -1), "Helvetica-Bold", 9.5)]))
    story.append(pa)

    story.append(Paragraph("The numbers at three prices", st["h2"]))
    cols = [opening, target, at_msrp]

    def row(label, key, cents=False, fmt=None):
        return [label] + [fmt(c) if fmt else money(c[key], cents) for c in cols]

    table = [
        ["", f"Opening offer\n({money(OPEN_DISCOUNT)} off)", f"Target\n({money(TARGET_DISCOUNT)} off)",
         "Walk-away\n(no discount)"],
        row("Selling price", "price"),
        row("- Lease cash", "lease_cash"),
        row("+ Acquisition fee", None, fmt=lambda c: money(ACQ_FEE)),
        row("Gross cap cost", "gross_cap"),
        row("- Trade applied to cap cost", "cap_cost_reduction"),
        row("Adjusted cap cost", "adj_cap"),
        row("Residual value", "residual"),
        row("Depreciation / month", "depreciation", True),
        row("Rent charge / month", "rent", True),
        row("Monthly payment (Santa Fe applied)", "monthly_payment", True),
        row("First payment", "monthly_payment", True),
        row("Doc fee", "doc_fee"),
        row("NJ sales tax (paid up front)", "nj_sales_tax", True),
        row("NJ luxury surcharge", "lfis", True),
        row("Registration / title", "reg_fees"),
        row("Total due at signing (incl. cap reduction)", "due_at_signing"),
        row("Paid by the Santa Fe", "trade_used"),
        row("Extra cash from you", "cash_at_signing"),
        row("Total cost over 36 months", "total_cost"),
        row("Total lease cost / 36 (incl. Santa Fe)", "effective_monthly", True),
    ]
    bold = (6, 10, 18, 20)
    t = grid(table, [2.75 * inch] + [(W - 2.75 * inch) / 3] * 3, bold_rows=bold)
    t.setStyle(TableStyle([("FONT", (0, 0), (-1, 0), "Helvetica-Bold", 9),
                           ("TEXTCOLOR", (1, 10), (-1, 10), GOOD if at_msrp["monthly_payment"] <= TARGET_PAYMENT else WARN)]))
    story.append(t)
    story.append(Paragraph(
        f"The Santa Fe covers the fees and about {money(at_msrp['cap_cost_reduction'])} of the price up front. The last "
        f"line spreads everything, Santa Fe included, over 36 months, for comparing deals.",
        st["small"]))

    # Page 2
    story.append(Paragraph("What moves the payment", st["h2"]))
    mfs = [0.00200, mf, mf + 0.0004]
    sens = [["Selling price"] + [f"MF {m:.5f}" + (" implied" if m == mf else " marked up" if m > mf else " (low)")
                                 for m in mfs]]
    for d in (0, 1000, 2000, 3000, 4000):
        cells = []
        for m in mfs:
            e = estimate(msrp - d, msrp, rpct, m, trade, TERM, ACQ_FEE, doc_fee, REG_FEES, lc)
            cells.append(f"{money(e['monthly_payment'])} / {money(e['effective_monthly'])}")
        sens.append([f"{money(msrp - d)}" + (" (MSRP)" if d == 0 else f" (-{money(d)})")] + cells)
    story.append(grid(sens, [1.9 * inch] + [(W - 1.9 * inch) / 3] * 3))
    story.append(Paragraph("Each cell: monthly payment / total lease cost / 36, with the $10,000 Santa Fe applied and $0 extra cash.",
                           st["small"]))
    story.append(Spacer(1, 6))
    tr = [["Santa Fe value"] + [money(x) for x in (8000, 9000, 10000, 11000, 12000)]]
    tr.append([f"Monthly payment, {money(TARGET_DISCOUNT)} off"] + [
        money(estimate(msrp - TARGET_DISCOUNT, msrp, rpct, mf, x, TERM, ACQ_FEE, doc_fee, REG_FEES, lc)["monthly_payment"])
        for x in (8000, 9000, 10000, 11000, 12000)])
    tr.append(["Total lease cost / 36"] + [
        money(estimate(msrp - TARGET_DISCOUNT, msrp, rpct, mf, x, TERM, ACQ_FEE, doc_fee, REG_FEES, lc)["effective_monthly"])
        for x in (8000, 9000, 10000, 11000, 12000)])
    story.append(grid(tr, [1.9 * inch] + [(W - 1.9 * inch) / 5] * 5))
    story.append(Paragraph(
        f"Every $1,000 off the price saves about {money(1000 / TERM + 1000 * mf, True)}/month. Every 0.0001 of "
        f"money-factor markup costs about {money(target['per_mf_tick'], True)}/month "
        f"({money(target['per_mf_tick'] * TERM)} over the lease).", st["small"]))

    story.append(Paragraph("Negotiation plan", st["h2"]))
    plan = [
        f"<b>Price first, by itself.</b> Agree the selling price before talking payment or trade. Open at "
        f"<b>{money(opening['price'])}</b>, aim for <b>{money(target['price'])}</b>, and walk away above "
        f"<b>{money(msrp)}</b> or if add-ons appear.",
        f"<b>Money factor.</b> Ask for the money factor and residual in writing. Expect about {mf:.5f} "
        f"({apr:.1f}% APR) and {rpct:.0f}%. Anything higher is dealer markup: ask for the buy rate.",
        f"<b>Lease cash.</b> Confirm the {money(lc)} Hyundai lease cash is applied (program ends "
        f"{o.get('expires', '') or '-'}). A '$750 HMF Dealer Choice' or other financing-only bonus does not apply "
        "to a lease, so it can't count as your discount.",
        f"<b>Trade separately.</b> Get CarMax / Carvana offers first. Trading at the dealer saves NJ tax "
        f"(about {money(trade * NJ_TAX)} on {money(trade)}), so a dealer allowance within that of the best outside "
        f"offer is fine. Below {money(trade)}, rerun the numbers.",
        f"<b>Fees.</b> Doc fee {money(doc_fee)} (published), acquisition fee {money(ACQ_FEE)} (no markup). "
        "Decline add-ons: protection packages, nitrogen, VIN etching, tint, wheel and tire plans.",
        f"<b>Close only on paper.</b> Sign only when the worksheet shows {TERM} months, {MILES:,} miles, $0 extra cash, "
        f"and a payment at or below the target column ({money(target['monthly_payment'])}).",
    ]
    for p in plan:
        story.append(Paragraph(p, st["bullet"], bulletText="•"))

    items = [("Selling price", money(target["price"])),
             ("Lease cash applied", money(lc)),
             ("Money factor", f"{mf:.5f}"),
             ("Residual (% and $)", f"{rpct:.0f}% / {money(at_msrp['residual'])}"),
             ("Acquisition fee", money(ACQ_FEE)),
             ("Doc fee", money(doc_fee)),
             ("Santa Fe trade allowance", money(trade)),
             ("NJ tax, surcharge, reg.", money(target["nj_sales_tax"] + target["lfis"] + REG_FEES)),
             ("Monthly payment", money(target["monthly_payment"], True)),
             ("Extra cash at signing", "$0")]
    half = (W - 0.2 * inch) / 2

    def checklist(part):
        return grid([["Item", "Expect", "Dealer's"]] + [[f"[  ] {a}", b, ""] for a, b in part],
                    [half * 0.56, half * 0.24, half * 0.20], zebra=False, align_right_from=9)
    story.append(KeepTogether([
        Paragraph("Ask the dealer to put in writing", st["h2"]),
        Table([[checklist(items[:5]), checklist(items[5:])]], colWidths=[half + 0.1 * inch, half + 0.1 * inch],
              hAlign="LEFT", style=[("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 0)]),
    ]))
    story.append(Spacer(1, 8))
    story.append(Paragraph(
        "Estimates from Hyundai's advertised November program; residual and money factor are implied, not confirmed. "
        "NJ tax per NJ Division of Taxation rules. Not financial advice: the decision is yours.",
        st["small"]))

    def footer(canvas, d):
        canvas.saveState()
        canvas.setFont("Helvetica", 8)
        canvas.setFillColor(MUTED)
        canvas.drawString(0.65 * inch, 0.4 * inch, f"Deal {deal_id}  |  VIN {vin}  |  Palisade lease watch")
        canvas.drawRightString(letter[0] - 0.65 * inch, 0.4 * inch, f"Page {d.page}")
        canvas.restoreState()

    doc.build(story, onFirstPage=footer, onLaterPages=footer)
    return path


def main():
    ap = argparse.ArgumentParser(description="Make PDF negotiation sheets for numbered deals")
    ap.add_argument("--data", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--trade", type=float, default=10000)
    ap.add_argument("--deal", action="append", help="only these deal ids")
    a = ap.parse_args()
    data = Path(a.data)
    vehicles = latest([r for r in csv.DictReader(open(data / "vehicles.csv", encoding="utf-8-sig"))
                       if r.get("deal_id")], "deal_id")
    offers = latest([r for r in csv.DictReader(open(data / "offers.csv", encoding="utf-8-sig"))
                     if r.get("deal_id") and r.get("kind") in ("estimate", "dealer_quote")], "deal_id")
    dealers = {}
    if (data / "dealers.csv").exists():
        dealers = {r["dealer"].strip().lower(): r
                   for r in csv.DictReader(open(data / "dealers.csv", encoding="utf-8-sig")) if r.get("dealer")}
    Path(a.out).mkdir(parents=True, exist_ok=True)
    for deal_id in sorted(vehicles):
        if a.deal and deal_id not in a.deal:
            continue
        if deal_id not in offers or vehicles[deal_id].get("status") in ("gone", "sold"):
            print(f"skip {deal_id}: no current estimate or the car is gone")
            continue
        contact = dealers.get(vehicles[deal_id].get("dealer", "").strip().lower())
        print(sheet(deal_id, vehicles[deal_id], offers[deal_id], a.trade, a.out, dt.date.today(), contact))


if __name__ == "__main__":
    main()
