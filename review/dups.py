"""Double-counting checks on include_v1 buys."""
import re

import numpy as np
import pandas as pd

from review.common import load_bt, load_trades

pd.set_option("display.width", 250)
t = load_trades()
bt = load_bt().set_index("row_id")
b = t[t.include_v1 & (t.type == "buy") & ~t.short_related].copy()


def norm(name):
    toks = [x for x in re.sub(r"[^A-Za-z ]", " ", name).split() if x.lower() not in ("mr", "mrs", "ms", "dr", "hon", "jr", "sr", "ii", "iii")]
    toks = [x for x in toks if len(x) > 1]  # drop initials
    return (toks[0] + " " + toks[-1]).lower() if toks else name.lower()


b["mnorm"] = b.member.map(norm)
names = b.groupby("mnorm").member.unique()
multi = names[names.map(len) > 1]
print("member name variants mapping to same first+last:")
for k, v in multi.items():
    print("  ", k, list(v))

key = ["mnorm", "ticker", "transaction_date", "amount_low", "amount_high", "owner"]
# cross-document duplicates (same key, different doc_id)
g = b.groupby(key).doc_id.nunique()
cross = g[g > 1]
cd = b.set_index(key).loc[cross.index].reset_index()
print(f"\nbuy rows whose (normalised member, ticker, txn date, amount, owner) appears in >1 doc_id: {len(cd)} rows, {len(cross)} keys")
print(cd.groupby("filing_status").size())
print(cd[["row_id", "member", "doc_id", "filing_status", "ticker", "transaction_date", "filing_date", "owner", "amount_high"]].head(40).to_string())
cd["priced"] = cd.row_id.map(bt["entry_idx"].notna())
cd["w"] = cd.row_id.map(bt["weight_amount"])
cd["sample"] = cd.row_id.map(bt["sample"])
# rows beyond the first per key = extra copies
extra = cd.sort_values("filing_date").groupby(key).apply(lambda x: x.iloc[1:]).reset_index(drop=True)
print("\nextra copies (beyond first filing) priced:", int(extra.priced.sum()), "by sample", extra[extra.priced].groupby("sample").w.agg(["size", "sum"]).to_dict())
extra[["row_id"]].to_csv("review/_cross_doc_extra_rows.csv", index=False)

# same trade reported by different owner within member (self vs joint vs spouse), same doc, same amount
key2 = ["mnorm", "ticker", "transaction_date", "amount_low", "amount_high"]
g2 = b.groupby(key2).owner.nunique()
print("\nkeys (member,ticker,txn,amount) with >1 distinct owner:", int((g2 > 1).sum()))

# Mark Green specifically
mg = b[b.mnorm == "mark green"]
print("\nMark Green rows by spelling and year:")
print(mg.groupby(["member", "year"]).size().unstack(fill_value=0))
mgk = mg.groupby(["ticker", "transaction_date", "amount_high"]).member.nunique()
print("Mark Green (ticker, txn, amount) under both spellings:", int((mgk > 1).sum()))

# Amended filings: is the original also included?
am = b[b.filing_status == "Amended"]
print("\nAmended include_v1 buy rows:", len(am))
k3 = ["mnorm", "ticker", "transaction_date", "amount_high"]
orig = b[b.filing_status == "New"].set_index(k3).index
both = am.set_index(k3).index.isin(orig)
print("  of which an identical (member,ticker,txn,amount) New row is also included:", int(both.sum()))
x = am[both]
x = x.assign(priced=x.row_id.map(bt["entry_idx"].notna()), w=x.row_id.map(bt["weight_amount"]), sample=x.row_id.map(bt["sample"]))
print(x.groupby("sample").agg(n=("row_id", "size"), priced=("priced", "sum"), w=("w", "sum")))
x[["row_id"]].to_csv("review/_amended_dup_rows.csv", index=False)
print(x[["row_id", "member", "doc_id", "ticker", "transaction_date", "filing_date", "amount_high"]].head(15).to_string())
