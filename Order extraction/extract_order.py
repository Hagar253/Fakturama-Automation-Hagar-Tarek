"""Extract a structured order from an order image using Tesseract OCR.

Usage:
    python extract_order.py [image_path] [--out order.json]

Two-pass extraction:
  Pass 1 (low-res whole image): find section boundaries + header + payment + totals.
  Pass 2 (high-res region crops): OCR the customer block, the addresses (split into
  invoice/delivery columns) and the items table (split into column bands) separately,
  which is far more accurate than reading the whole page at once.
"""
import argparse
import json
import re

import pytesseract
from PIL import Image, ImageFilter

TESSERACT_CMD = r"C:\Program Files\Tesseract-OCR\tesseract.exe"

PASS1_WIDTH = 2400   # target width for the whole-image pass
PASS2_UPSCALE = 8    # how much to upscale region crops for the detail pass

# Fakturama payment-code mapping (kept in one place for OCR)
PAYMENT_CODE_MAP = {
    "Bank Transfer": "Credit transfer",
    "Credit Card": "Credit card",
    "SEPA Direct Debit": "SEPA direct debit",
    "Cash": "Cash",
}

# --------------------------------------------------------------------------
# 1) Preprocess
# --------------------------------------------------------------------------
def preprocess(image_path: str):
    """Return (original image, low-res upscaled image, scale factor)."""
    orig = Image.open(image_path).convert("RGB")
    scale = max(2, round(PASS1_WIDTH / orig.width))
    big = orig.resize((orig.width * scale, orig.height * scale), Image.LANCZOS)
    big = big.filter(ImageFilter.SHARPEN)
    return orig, big, scale


# --------------------------------------------------------------------------
# 2) OCR building blocks
# --------------------------------------------------------------------------
def ocr_words(image: Image.Image) -> list[dict]:
    data = pytesseract.image_to_data(
        image, output_type=pytesseract.Output.DICT, config="--psm 6 --oem 1"
    )
    words = []
    for i, raw in enumerate(data["text"]):
        t = (raw or "").strip()
        if not t:
            continue
        words.append(
            {
                "text": t,
                "left": data["left"][i],
                "top": data["top"][i],
                "width": data["width"][i],
                "height": data["height"][i],
                "conf": data["conf"][i],
            }
        )
    return words


def group_into_lines(words: list[dict], w_img: int) -> list[list[dict]]:
    tol = max(15, int(w_img * 0.012))
    ws = sorted(words, key=lambda w: (w["top"], w["left"]))
    lines, cur, cur_top = [], [], None
    for w in ws:
        if cur_top is None or abs(w["top"] - cur_top) <= tol:
            cur.append(w)
            cur_top = w["top"] if cur_top is None else min(cur_top, w["top"])
        else:
            lines.append(cur)
            cur, cur_top = [w], w["top"]
    if cur:
        lines.append(cur)
    for ln in lines:
        ln.sort(key=lambda w: w["left"])
    return lines


def ocr_region(orig: Image.Image, top_big: int, bottom_big: int,
               scale: int, upscale: int = PASS2_UPSCALE):
    """Crop a region from the original image, upscale, and OCR it."""
    y0 = max(0, int(top_big / scale))
    y1 = min(orig.height, int(bottom_big / scale))
    crop = orig.crop((0, y0, orig.width, y1))
    if crop.width < 200:
        upscale = max(upscale, 10)
    big_crop = crop.resize((crop.width * upscale, crop.height * upscale), Image.LANCZOS)
    words = ocr_words(big_crop)
    lines = group_into_lines(words, big_crop.width)
    return lines, big_crop.width


# --------------------------------------------------------------------------
# 3) Regex + number helpers
# --------------------------------------------------------------------------
RATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
WEB_RE = re.compile(r"\b[A-Z]{2,}[-_/]?\d{4}[-_/]?\d{4}[-_/]?[A-Z0-9]+\b")
CUST_RE = re.compile(r"\bCUST[- ]?\d+\b", re.I)
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
PHONE_RE = re.compile(r"\+?\d[\d\s()./-]{6,}\d")
CUR_RE = re.compile(r"\b(EUR|USD|GBP|EGP|SEK|DKK|NOK|PLN|CZK)\b", re.I)
PAY_RE = re.compile(r"\b(Bank Transfer|Credit Card|SEPA\s*Direct\s*Debit|Cash)\b", re.I)
MONEY_RE = re.compile(r"-?\d[\d.,]*\.\d{2}")
SKU_RE = re.compile(r"^[A-Z]{2,}[0-9A-Z][0-9A-Z/-]*$")
ALIAS_RE = re.compile(r"^[A-Z][A-Z0-9-]{4,}$")
LEGAL_RE = re.compile(r"\b(GmbH|AG|OHG|KG|Ltd|LLC|Co\.?|Inc\.?)\b", re.I)


def clean_number(s: str) -> float | None:
    s = s.strip().replace("€", "").replace("EUR", "").strip()
    if not s:
        return None
    if "," in s and "." in s:
        s = s.replace(".", "").replace(",", ".")
    elif "," in s:
        s = s.replace(",", ".")
    try:
        return round(float(s), 2)
    except ValueError:
        return None


def norm(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def line_text(line: list[dict]) -> str:
    return norm(" ".join(w["text"] for w in line))


def lines_to_rows(lines: list[list[dict]]) -> list[dict]:
    rows = []
    for line in lines:
        words = [{"text": w["text"], "left": w["left"], "conf": w["conf"]} for w in line]
        rows.append({"top": line[0]["top"], "words": words, "text": line_text(line)})
    return rows


def find_row(rows, patterns):
    for i, r in enumerate(rows):
        if any(re.search(p, r["text"], re.I) for p in patterns):
            return i, r
    return None, None


def find_lines(rows, patterns, start=0, end=None):
    out = []
    for i, r in enumerate(rows[start:end or len(rows)], start):
        if any(re.search(p, r["text"], re.I) for p in patterns):
            out.append((i, r))
    return out


def empty_order() -> dict:
    return {
        "order_ref": None, "order_date": None, "customer_id": None, "currency": None,
        "customer": {"company": None, "contact": None, "alias": None, "email": None,
                     "phone": None, "billing_address": None, "delivery_address": None},
        "payment": {"method": None, "code": None, "status": None, "date": None},
        "items": [],
        "totals": {"net": None, "vat": None, "gross": None},
    }


# --------------------------------------------------------------------------
# 4) Pass 1: whole image -> section boundaries + header + payment + totals
# --------------------------------------------------------------------------
def pass1(orig, big, scale) -> dict:
    words = ocr_words(big)
    lines = group_into_lines(words, big.width)
    rows = lines_to_rows(lines)
    full = " ".join(r["text"] for r in rows)

    order = empty_order()

    m = WEB_RE.search(full)
    if m:
        order["order_ref"] = m.group(0)
    dates = RATE_RE.findall(full)
    if dates:
        order["order_date"] = dates[0]
    m = CUST_RE.search(full)
    if m:
        order["customer_id"] = m.group(0)
    m = CUR_RE.search(full)
    if m:
        order["currency"] = m.group(0).upper()

    # section boundaries
    _, cust_row = find_row(rows, [r"custom[ae]", r"cust[a-z]*\s+(and|und|&)\s+contact"])
    _, addr_row = find_row(rows, [r"address|adr?ess|adores|ddres"])
    _, pay_row = find_row(rows, [r"pay[a-z]*", r"paio", r"payw"])
    _, item_row = find_row(rows, [r"item", r"ittm"])

    # payment block (between PAYMENT header and ITEMS header)
    pay_idx = None
    for i, r in enumerate(rows):
        if re.search(r"pay[a-z]*|paio|payw", r["text"], re.I):
            pay_idx = i
            break
    if pay_idx is not None:
        end_idx = None
        for i in range(pay_idx, len(rows)):
            if re.search(r"item|ittm", rows[i]["text"], re.I):
                end_idx = i
                break
        pay_text = " ".join(r["text"] for r in rows[pay_idx:end_idx or None])
        m = PAY_RE.search(pay_text)
        if m:
            order["payment"]["method"] = m.group(0)
        if re.search(r"\bPAID\b", pay_text, re.I):
            order["payment"]["status"] = "PAID"
        elif re.search(r"\bUNPAID\b", pay_text, re.I):
            order["payment"]["status"] = "UNPAID"
        d = RATE_RE.findall(pay_text)
        if d:
            order["payment"]["date"] = d[-1]
        order["payment"]["code"] = PAYMENT_CODE_MAP.get(order["payment"]["method"])

    # totals: the row with 3+ money values located below the ITEMS header
    start_after = item_row["top"] if item_row else 0
    totals_val = None
    totals_top = None
    for i in range(len(rows) - 1, -1, -1):
        if rows[i]["top"] <= start_after:
            break
        vals = [n for n in (clean_number(m) for m in MONEY_RE.findall(rows[i]["text"])) if n is not None]
        if len(vals) >= 3:
            totals_val = vals
            totals_top = rows[i]["top"]
            break
    if totals_val and len(totals_val) >= 3:
        order["totals"] = {"net": round(totals_val[-3], 2),
                           "vat": round(totals_val[-2], 2),
                           "gross": round(totals_val[-1], 2)}

    # customer block (between CUSTOMER header and ADDRESSES header) - whole-image
    # read is more reliable here than the 8x crop.
    if cust_row and addr_row:
        c_start = None
        for i, r in enumerate(rows):
            if r is cust_row:
                c_start = i
                break
        block = [r for r in rows[c_start + 1:] if r["top"] < addr_row["top"]]
        # company: first non-label line with a legal-form word
        for r in block:
            t = re.sub(r"\b(CUSTOMER|AND|ANDO|UND|CONTACT|COMPANY|ID|ALIAS|NAME)\b",
                       " ", r["text"], flags=re.I)
            t = norm(t)
            if not t or len(r["words"]) < 2:
                continue
            legal = LEGAL_RE.search(t)
            if legal and order["customer"]["company"] is None:
                order["customer"]["company"] = t[:legal.end()].strip()
                rest = t[legal.end():].strip()
                if rest and order["customer"]["contact"] is None:
                    order["customer"]["contact"] = rest
                break
        if order["customer"]["company"] is None:
            for r in block:
                t = re.sub(r"\b(CUSTOMER|AND|ANDO|UND|CONTACT|COMPANY|ID|ALIAS|NAME)\b",
                           " ", r["text"], flags=re.I)
                t = norm(t)
                if not t or len(r["words"]) < 2 or re.search(r"@|NORTHSTAR|CUST", t, re.I):
                    continue
                order["customer"]["company"] = t
                break
        # alias: NORTHSTAR-XXXX style token
        for r in block:
            for w in r["words"]:
                raw = w["text"].rstrip("—_|:.,")
                if ALIAS_RE.match(raw) and raw.upper() not in ("EUR",) and raw.upper() != "NORTHSTAR":
                    if not re.search(r"CONTACT|COMPANY|CUSTOMER", raw, re.I):
                        order["customer"]["alias"] = raw
                        break
            if order["customer"]["alias"]:
                break
        # contact name fallback: "First Last" line
        if order["customer"]["contact"] is None:
            for r in block:
                t = r["text"]
                if re.search(r"@", t):
                    continue
                parts = t.split()
                if len(parts) == 2 and all(p[:1].isupper() for p in parts if p):
                    order["customer"]["contact"] = t
                    break
        # phone (whole text, strip noise chars)
        block_text = re.sub(r"[^\d+()/\- ]", " ", " ".join(r["text"] for r in block))
        phones = [p for p in PHONE_RE.findall(block_text) if not RATE_RE.fullmatch(p)]
        if phones:
            order["customer"]["phone"] = norm(phones[0])

    # record crop boundaries (big-image coordinates)
    bounds = {
        "cust_top": cust_row["top"] if cust_row else 0,
        "addr_top": addr_row["top"] if addr_row else (cust_row["top"] if cust_row else 0),
        "pay_top": pay_row["top"] if pay_row else None,
        "item_top": item_row["top"] if item_row else totals_top or None,
        "totals_top": totals_top,
    }
    return order, bounds


# --------------------------------------------------------------------------
# 5) Pass 2: high-res region crops
# --------------------------------------------------------------------------
def parse_customer_email(orig, bounds, scale, order) -> None:
    """Email is noisy at whole-image scale; try a tight crop of the contact line."""
    if order["customer"]["email"]:
        return
    top = bounds["cust_top"]
    lines, w = ocr_region(orig, max(0, top - 30), bounds["addr_top"] + 10, scale)
    rows = lines_to_rows(lines)
    text_all = " ".join(r["text"] for r in rows)
    cleaned = re.sub(r"\s*([@.])\s*", r"\1", text_all)
    m = EMAIL_RE.search(cleaned)
    if m:
        order["customer"]["email"] = m.group(0)


def parse_address_region(orig, bounds, scale, order) -> None:
    if bounds["pay_top"] is None:
        return
    lines, w = ocr_region(orig, bounds["addr_top"], bounds["pay_top"], scale)
    rows = lines_to_rows(lines)
    LABELS = ("ADORESS", "ADRESS", "LUNG", "AGOE", "AUVURES", "OTLR",
              "DELIVERY", "INVOICE", "BILL", "ADDRESS", "PAYMENT")

    def is_label(tokens: list) -> bool:
        joined = norm(" ".join(tokens))
        up = joined.upper()
        if any(lab in up for lab in LABELS):
            return True
        # a row that is one or two very short tokens and has no digits is a label
        if any(len(t) <= 2 for t in tokens if t) and not re.search(r"\d", joined):
            return len(tokens) <= 3
        return False

    left_lines, right_lines = [], []
    for r in rows:
        left = [x["text"] for x in r["words"] if x["left"] < w * 0.5]
        right = [x["text"] for x in r["words"] if x["left"] >= w * 0.5]
        if left and not is_label(left):
            lt = norm(" ".join(left))
            if len(re.findall(r"[A-Za-zÄÖÜäöü0-9€]", lt)) >= 4:
                left_lines.append(lt)
        if right and not is_label(right):
            rt = norm(" ".join(right))
            if len(re.findall(r"[A-Za-zÄÖÜäöü0-9€]", rt)) >= 4:
                right_lines.append(rt)
    if left_lines:
        order["customer"]["billing_address"] = norm(" ".join(left_lines))
    if right_lines:
        order["customer"]["delivery_address"] = norm(" ".join(right_lines))


def parse_items_region(orig, bounds, scale, order) -> None:
    top = bounds["item_top"]
    bottom = bounds["totals_top"] or (top + 600)
    lines, w = ocr_region(orig, top, bottom, scale)
    rows = lines_to_rows(lines)
    # column bands (relative to crop width)
    SKU_MAX = 0.24    # SKU cell
    DESC_MAX = 0.50   # description cell
    QTY_MAX = 0.78    # quantity cell
    # PRICE = beyond QTY_MAX

    for r in rows:
        t = r["text"]
        if re.search(r"\bTOTAL\b|\bNET\b|\bVAT\b", t, re.I) and not re.search(r"\d", t):
            continue
        toks = sorted(r["words"], key=lambda x: x["left"])
        if len(toks) < 2:
            continue
        cell = lambda lo, hi: norm(" ".join(x["text"] for x in toks
                                            if lo <= x["left"] / w < hi))
        sku_cell = cell(0, SKU_MAX)
        desc_cell = cell(SKU_MAX, DESC_MAX)
        qty_cell = cell(DESC_MAX, QTY_MAX)
        price_cell = cell(QTY_MAX, 1.1)

        # SKU: single token or joined fragments
        sku = None
        sku_frag = []
        for x in toks:
            if x["left"] / w < SKU_MAX and re.fullmatch(r"[A-Z]{2,}|[A-Z0-9]{2,}", x["text"]):
                sku_frag.append(x["text"])
        if any(SKU_RE.match(f) and len(f) >= 4 for f in sku_frag):
            sku = next(f for f in sku_frag if SKU_RE.match(f) and len(f) >= 4)
        elif len(sku_frag) >= 2:
            candidate = "-".join(sku_frag[:4])
            if re.fullmatch(r"[A-Z0-9][A-Z0-9-]{2,}", candidate):
                sku = candidate

        # price / line total from the far-right cell (may be split, e.g. "452" "00")
        price_text = re.sub(r"[^\d.,]", " ", price_cell)
        price_parts = [p for p in price_text.split() if re.fullmatch(r"\d{1,6}([.,]\d{1,3})?", p)]
        line_total = None
        if price_parts:
            last = price_parts[-1]
            if "," in last and "." not in last:
                last = last.replace(",", ".")
            if "." in last:
                line_total = clean_number(last)
            elif len(price_parts) >= 2:
                ints = [int(p) for p in price_parts[-2:]]
                if ints[1] < 100:
                    line_total = round(float(ints[0]) + ints[1] / 100, 2)
                else:
                    line_total = float(ints[0])
            else:
                line_total = float(int(last)) if last.isdigit() else None
        if line_total == 0.0:
            line_total = None  # "0.00" almost certainly an OCR miss, not a real price

        # quantity from the qty cell (may contain '4)' etc)
        qty = None
        mq = re.search(r"(\d{1,3})", qty_cell)
        if mq and int(mq.group(1)) > 0:
            qty = int(mq.group(1))

        unit_price = None
        if qty and line_total:
            unit_price = round(line_total / qty, 2)

        if sku or line_total:
            order["items"].append({
                "sku": sku,
                "description": desc_cell or None,
                "quantity": qty,
                "unit_price": unit_price,
                "line_total": line_total,
            })

# --------------------------------------------------------------------------
# 6) Validate
# --------------------------------------------------------------------------
def validate(order: dict) -> list[str]:
    issues = []
    if not order["order_ref"]:
        issues.append("Missing order reference")
    if not order["order_date"]:
        issues.append("Missing order date")
    if not order["customer"]["company"]:
        issues.append("Missing company")
    if not order["customer"]["email"]:
        issues.append("Missing email")
    if not order["payment"]["method"] or not order["payment"]["code"]:
        issues.append("Missing / unmapped payment method")
    t = order["totals"]
    if t["net"] is not None and t["vat"] is not None and t["gross"] is not None:
        if abs(t["net"] + t["vat"] - t["gross"]) > 0.05:
            issues.append(f"Totals mismatch: {t['net']}+{t['vat']} != {t['gross']}")
        if t["net"] > 0 and t["vat"] > 0:
            rate = round(t["vat"] / t["net"] * 100, 2)
            known = [0.0, 5.0, 7.0, 16.0, 19.0, 21.0, 22.0, 25.0, 27.0]
            if not any(abs(rate - k) < 0.5 for k in known):
                issues.append(f"Unusual implied VAT rate {rate}%")
    if not order["items"]:
        issues.append("No items detected")
    for it in order["items"]:
        if not it["sku"] and not it["line_total"]:
            issues.append("Item with neither SKU nor line total")
        if it["sku"] and it["line_total"] is None:
            issues.append(f"Item {it['sku']} missing line total")
        if it["sku"] and it["quantity"] is None:
            issues.append(f"Item {it['sku']} missing quantity")
    if t["net"] is not None and order["items"]:
        line_sum = sum(it["line_total"] for it in order["items"] if it["line_total"] is not None)
        if all(it["line_total"] is not None for it in order["items"]):
            if abs(line_sum - t["net"]) > 0.05:
                issues.append(f"Items reconcile: {line_sum:.2f} != net {t['net']:.2f}")
    return issues


# --------------------------------------------------------------------------
# 7) Entry
# --------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("image", nargs="?", default="input_image.png")
    ap.add_argument("--out", default="order.json")
    
    args = ap.parse_args()

    ipytesseract.pytesseract.tesseract_cmd = TESSERACT_CMD

    orig, big, scale = preprocess(args.image)

    order, bounds = pass1(orig, big, scale)

    parse_customer_email(orig, bounds, scale, order)
    parse_address_region(orig, bounds, scale, order)
    parse_items_region(orig, bounds, scale, order)

    print("[OCR extraction used]")

    issues = validate(order)

    print(json.dumps(order, indent=2, ensure_ascii=False))
    print("\n# validation issues:", len(issues))
    for i in issues:
        print(" -", i)

    with open(args.out, "w", encoding="utf-8") as f:
        json.dump({"order": order, "validation": {"ok": len(issues) == 0, "issues": issues}},
                  f, indent=2, ensure_ascii=False)
    print(f"\nSaved -> {args.out}")


if __name__ == "__main__":
    main()