"""New Jersey lease estimate with an owned trade-in, for the Palisade lease watch.

Used by the weekly lease-watch run so every estimate is worked out the same way.
Standard library only.

    python lease_calc.py --price 52125 --msrp 52125 --residual-pct 72 --mf 0.00238 --trade 10000

The trade equity pays everything due at signing first (first payment, doc fee,
NJ sales tax, registration, the 0.4% luxury surcharge); whatever is left becomes
a capitalized cost reduction. With an owned trade, NJ lets the trade value
reduce the lease tax base (Division of Taxation, publication S&U-12).

"effective_monthly" is what the lease really costs per month: the trade equity
you hand over plus the remaining payments, spread over the term. It is the
number to compare deals on; the monthly payment alone hides the equity.
"""

import argparse
import json

NJ_TAX = 0.06625
LFIS_RATE = 0.004          # luxury surcharge, sales/lease price >= $45,000 before trade
LFIS_THRESHOLD = 45000


def payment(cap_cost: float, residual: float, mf: float, term: int) -> float:
    return (cap_cost - residual) / term + (cap_cost + residual) * mf


def estimate(price, msrp, residual_pct, mf, trade, term=36, acq_fee=650, doc_fee=695,
             reg_fees=350, lease_cash=0.0):
    """Return the monthly payment and how the trade equity is used.

    price      negotiated selling price before lease cash (MSRP if none)
    msrp       used for the residual
    lease_cash manufacturer lease incentive, taken off the cap cost
    """
    residual = round(msrp * residual_pct / 100, 2)
    gross_cap = price + acq_fee - lease_cash
    lfis = round(price * LFIS_RATE, 2) if price >= LFIS_THRESHOLD else 0.0

    def due_and_payment(ccr):
        p = payment(gross_cap - ccr, residual, mf, term)
        taxable = p * term + ccr + doc_fee - trade
        tax = max(0.0, taxable) * NJ_TAX
        return p, tax, p + doc_fee + tax + reg_fees + lfis + ccr

    # Find the cap cost reduction that uses up the trade equity exactly.
    lo, hi = 0.0, max(0.0, trade)
    p0, _, due0 = due_and_payment(0.0)
    if due0 >= trade:                      # equity doesn't even cover the fees
        ccr = 0.0
    else:
        for _ in range(60):
            mid = (lo + hi) / 2
            if due_and_payment(mid)[2] > trade:
                hi = mid
            else:
                lo = mid
        ccr = lo
    p, tax, due = due_and_payment(ccr)
    cash_at_signing = max(0.0, due - trade)
    total_cost = trade + cash_at_signing + p * (term - 1)
    return {
        "monthly_payment": round(p, 2),
        "monthly_payment_no_trade": round(p0, 2),
        "cap_cost_reduction": round(ccr, 2),
        "nj_sales_tax": round(tax, 2),
        "lfis": lfis,
        "doc_fee": doc_fee,
        "reg_fees": reg_fees,
        "residual": residual,
        "cash_at_signing": round(cash_at_signing, 2),
        "total_cost": round(total_cost, 2),
        "effective_monthly": round(total_cost / term, 2),
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--price", type=float, required=True)
    ap.add_argument("--msrp", type=float, required=True)
    ap.add_argument("--residual-pct", type=float, required=True)
    ap.add_argument("--mf", type=float, required=True)
    ap.add_argument("--trade", type=float, default=10000)
    ap.add_argument("--term", type=int, default=36)
    ap.add_argument("--acq-fee", type=float, default=650)
    ap.add_argument("--doc-fee", type=float, default=695)
    ap.add_argument("--reg-fees", type=float, default=350)
    ap.add_argument("--lease-cash", type=float, default=0)
    a = ap.parse_args()
    print(json.dumps(estimate(a.price, a.msrp, a.residual_pct, a.mf, a.trade, a.term,
                              a.acq_fee, a.doc_fee, a.reg_fees, a.lease_cash), indent=2))


if __name__ == "__main__":
    main()
