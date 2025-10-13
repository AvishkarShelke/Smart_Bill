from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Dict, List, Any
import re
from datetime import datetime
from langdetect import detect

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://gccffb251970d0d-acseatpdbus.adb.us-ashburn-1.oraclecloudapps.com"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.options("/extract-expense-info")
async def preflight():
    return JSONResponse(status_code=200)

class OCRRequest(BaseModel):
    pages: List[Dict[str, Any]]


# -------------------- HELPER FUNCTIONS --------------------
def group_words_into_lines(words):
    lines, current_line, prev_y = [], [], None
    safe_words = []
    for w in words:
        vertices = w.get("boundingPolygon", {}).get("normalizedVertices", [])
        if vertices and "x" in vertices[0] and "y" in vertices[0]:
            w["__y"] = round(vertices[0]["y"], 2)
            w["__x"] = vertices[0]["x"]
            safe_words.append(w)
    sorted_words = sorted(safe_words, key=lambda w: (w["__y"], w["__x"]))
    for word in sorted_words:
        y = word["__y"]
        if prev_y is None or abs(y - prev_y) < 0.01:
            current_line.append(word["text"])
        else:
            lines.append(" ".join(current_line))
            current_line = [word["text"]]
        prev_y = y
    if current_line:
        lines.append(" ".join(current_line))
    return lines


def _parse_amount_str(s: str):
    if not s:
        return None
    s_clean = s.strip()
    # Handle Indian and European formatting
    if re.search(r"\d+,\d{2}$", s_clean):
        s_clean = s_clean.replace(".", "").replace(",", ".")
    else:
        s_clean = s_clean.replace(",", "")
    s_clean = re.sub(r"[₹$€£]|INR|USD|EUR|GBP|BRL|R\$|EUROS?", "", s_clean, flags=re.IGNORECASE)
    s_clean = re.sub(r"[^0-9\.]", "", s_clean)
    if s_clean.count(".") > 1:
        parts = s_clean.split(".")
        s_clean = "".join(parts[:-1]) + "." + parts[-1]
    try:
        val = float(s_clean)
        if val <= 0 or val > 10_000_000:
            return None
        return val
    except:
        return None


def extract_total_amount(lines: List[str], full_text_upper="", category=None) -> float:
    prioritized_keywords = [
        "grand total",
        "amount payable",
        "amount to be paid",
        "net payable",
        "total payable",
        "total amount",
        "balance due",
        "total bill amount",
        "bill amount",
        "amount payable from customer",
        "upi payment",
        "net amt",
        "net amount",
        "total",
        "total general",
        "importe total",
        "total a pagar",
        "monto total",
        "valor total",
        "total líquido",
        "total factura",
        "amount paid",      # Indian retail
        "bill total",       # Indian retail
    ]

    amount_pattern = re.compile(r"[\d,]+(?:\.\d{1,2})?")
    invoice_keywords = ["invoice", "inv no", "bill no", "receipt no", "voucher",
                        "factura", "recibo", "nota fiscal"]
    exclude_tokens = ["sub total", "subtotal", "cgst", "sgst", "vat", "tax", "discount",
                      "cambio", "impuesto", "descuento"]

    # For fuel bills ignore invoice numbers
    ignore_invoice_for_fuel = (category == "Fuel")

    def parse_amounts(line):
        found = amount_pattern.findall(line.replace(" ", ""))
        parsed = [_parse_amount_str(f) for f in found]
        return [p for p in parsed if p is not None]

    safe_candidates = []

    for idx, line in enumerate(lines):
        low = line.lower()
        if any(x in low for x in exclude_tokens):
            continue

        if ignore_invoice_for_fuel and any(x in low for x in invoice_keywords):
            continue

        if any(kw in low for kw in prioritized_keywords):
            next_lines = [lines[i] for i in range(idx, min(idx + 3, len(lines)))]
            for l in next_lines:
                amts = parse_amounts(l)
                for amt in amts:
                    if amt and amt > 0:
                        safe_candidates.append(amt)

    # Special handling for Indian retail bills (like DMART) if nothing matched
    if not safe_candidates:
        last_numeric = []
        for line in reversed(lines):
            amts = parse_amounts(line)
            for amt in amts:
                if amt and amt > 10:
                    last_numeric.append(amt)
            if last_numeric:
                break
        if last_numeric:
            return last_numeric[0]

    if safe_candidates:
        return max(safe_candidates)
    return 0.0


def extract_date_from_text(lines):
    date_patterns = [
        r"\b(\d{1,2}[-/\.\s][A-Za-z]{3}[-/\.\s]\d{2,4})\b",
        r"\b(\d{1,2}[-/\.\s]\d{1,2}[-/\.\s]\d{2,4})\b",
        r"\b(\d{4}[-/\.\s]\d{1,2}[-/\.\s]\d{1,2})\b",
        r"\b(\d{1,2} [A-Za-z]{3,9} \d{2,4})\b",
        r"\b([A-Za-z]{3,9} \d{1,2}, \d{4})\b"
    ]

    def parse_date_safe(date_str):
        formats = ["%d/%m/%Y", "%d-%m-%Y", "%Y-%m-%d", "%d %B %Y",
                   "%B %d, %Y", "%d/%m/%y", "%d-%m-%y", "%d-%b-%Y", "%d-%b-%y"]
        for fmt in formats:
            try:
                dt = datetime.strptime(date_str, fmt)
                if 2010 <= dt.year <= datetime.now().year + 1:
                    return dt.strftime("%Y-%m-%d")
            except:
                continue
        return None

    for line in lines:
        for pattern in date_patterns:
            matches = re.findall(pattern, line)
            for match in matches:
                parsed = parse_date_safe(match)
                if parsed:
                    return parsed
    return "Not Found"


def get_safe_date(date_str: str):
    try:
        if not date_str or date_str == "Not Found":
            return datetime.today().strftime("%Y-%m-%d")
        datetime.strptime(date_str, "%Y-%m-%d")
        return date_str
    except:
        return datetime.today().strftime("%Y-%m-%d")


# -------------------- PURPOSE DETECTION --------------------
def detect_purpose(text, expense_date=None):
    text_upper = text.upper()

    # Category Detection
    purpose_map = {
        "Miscellaneous": [
            "HOSPITAL", "CLINIC", "PHARMACY", "MEDICINE", "TABLET", "INJECTION", "LAB", "DIAGNOSTIC",
            "PATHOLOGY", "XRAY", "SCAN", "MRI", "CHEMIST", "DOCTOR", "SURGERY"
        ],
        "Air": ["AIRLINES", "FLIGHT", "TICKET", "BOARDING"],
        "Taxi": ["CAB", "TAXI", "AUTO", "RIDE", "UBER", "OLA", "RAPIDO", "BOLT"],
        "Car Rental": ["RENTAL", "ZOOMCAR", "HERTZ", "CAR RENTAL", "SELF DRIVE"],
        "Parking": ["PARKING", "TOLL", "GARAGE"],
        "Fuel": ["FUEL", "PETROL", "DIESEL", "GAS STATION", "HP", "BPCL", "SHELL", "INDIANOIL"],
        "Hotel": ["ROOM", "LODGE", "HOTEL", "RESORT", "SUITE", "BOOKING", "EXPEDIA", "MAKEMYTRIP"],
        "Entertainment": ["MOVIE", "CINEMA", "THEATRE", "CONCERT", "SHOW", "EVENT", "GAMING"],
        "Supplies": ["STATIONERY", "OFFICE", "PAPER", "SUPPLY", "INK", "TONER", "CARTRIDGE", "DIARY", "FILE"]
    }

    for category, keywords in purpose_map.items():
        if any(k in text_upper for k in keywords):
            return category

    # Meal detection based on time
    meal_by_time = None
    if expense_date and expense_date != "Not Found":
        try:
            try:
                dt = datetime.strptime(expense_date, "%Y-%m-%d %H:%M:%S")
            except:
                dt = datetime.strptime(expense_date, "%Y-%m-%d")
            meal_by_time = "Lunch" if dt.hour < 17 else "Dinner"
        except:
            pass

    meal_keywords = {
        "Lunch": ["BREAKFAST", "MORNING MEAL", "LUNCH", "CAFÉ", "ALMUERZO", "ALMOÇO"],
        "Dinner": ["DINNER", "SUPPER", "EVENING MEAL", "CENA", "JANTAR"]
    }

    for meal, keywords in meal_keywords.items():
        if any(k in text_upper for k in keywords):
            return meal

    if meal_by_time:
        return meal_by_time

    return "Miscellaneous"


# -------------------- MAIN API --------------------
@app.post("/extract-expense-info")
async def extract_expense_info(payload: OCRRequest):
    try:
        words = []
        for page in payload.pages:
            words.extend(page.get("words", []))
        if not words:
            for page in payload.pages:
                words.extend(page.get("tokens", []))
            if not words:
                return JSONResponse(content={"error": "No OCR words or tokens found."}, status_code=400)

        lines = group_words_into_lines(words)
        full_text = " ".join([w.get("text", "") for w in words])
        full_text_upper = full_text.upper()

        # Language detection
        try:
            language = detect(full_text)
        except:
            language = "en"

        # Spanish/Portuguese manual fallback
        signals = ["IVA", "EUROS", "GRACIAS", "FACTURA", "TOTAL", "IMPORTE", "COBRO"]
        if any(sig in full_text_upper for sig in signals):
            language = "es"

        # Detect purpose first (needed for Fuel logic)
        category = detect_purpose(full_text)
        total = extract_total_amount(lines, full_text_upper, category)
        raw_date = extract_date_from_text(lines)
        expense_date = get_safe_date(raw_date)

        # -------------------- CURRENCY DETECTION --------------------
        text_no_space = full_text_upper.replace(" ", "")
        if any(sig in full_text_upper for sig in ["DMART", "INDIA", "GST", "BILL"]):
            currency = "INR"
        elif any(sym in text_no_space for sym in ["₹", "INR", " RS.", " RS "]):
            currency = "INR"
        elif any(sym in text_no_space for sym in ["USD", "US$", "US DOLLAR"]) or (
            "$" in text_no_space and not "₹" in text_no_space and not "RS" in text_no_space
        ):
            currency = "USD"
        elif any(sym in text_no_space for sym in ["EUR", "€", "EURO"]):
            currency = "EUR"
        elif any(sym in text_no_space for sym in ["GBP", "£", "POUND"]):
            currency = "GBP"
        elif any(sym in text_no_space for sym in ["BRL", "R$", "REAL"]):
            currency = "BRL"
        else:
            if "GST" in full_text_upper or "INDIA" in full_text_upper:
                currency = "INR"
            elif language.startswith("es"):
                currency = "EUR"
            elif language.startswith("pt"):
                currency = "BRL"
            elif language.startswith("en"):
                currency = "USD"
            else:
                currency = "INR"

        return {
            "DetectedLanguage": language,
            "ReimbursementCurrencyCode": currency,
            "ExpenseReportTotal": f"{total:.2f}",
            "Purpose": category,
            "ExpenseDate": expense_date,
            "SubmitReport": "Y"
        }

    except Exception as e:
        return JSONResponse(content={"error": "Failed to process OCR", "details": str(e)}, status_code=500)


