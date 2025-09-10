from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Dict, List, Any
import re
from datetime import datetime

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


def group_words_into_lines(words):
    lines = []
    current_line = []
    prev_y = None

    # Only use words with bounding info
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
    """Cleans an OCR-captured amount string and returns float or None.
    Handles commas, currency symbols, and accidental spaces.
    """
    if not s:
        return None
    # Remove currency symbols and stray letters
    s_clean = re.sub(r"[₹$€£]|INR|USD|EUR|GBP", "", s, flags=re.IGNORECASE)
    # Remove spaces between digits (like 1 234)
    s_clean = re.sub(r"(?<=\d)\s+(?=\d)", "", s_clean)
    # Remove any characters except digits, comma and dot
    s_clean = re.sub(r"[^0-9,\.]", "", s_clean)
    # Remove thousands separators (commas)
    s_clean = s_clean.replace(",", "")
    if s_clean.count(".") > 1:
        # malformed number like 1.234.56 -> keep last two decimals
        parts = s_clean.split(".")
        s_clean = "".join(parts[:-1]) + "." + parts[-1]
    try:
        val = float(s_clean)
        # sensible bounds
        if val <= 0 or val > 10_000_000:
            return None
        return val
    except:
        return None


def extract_total_amount(lines: List[str]) -> float:
    """Improved total extraction logic.

    Strategy:
    1. Look for high-priority labels (in order): 'grand total', 'amount payable',
       'amount to be paid', 'net payable', 'total payable', 'total amount', 'balance due', 'total'.
       For each label, try to extract amount from same line, then neighbor lines.
    2. If none found, collect all candidate amounts while excluding lines that contain
       'subtotal', 'tax', 'discount', 'vat', 'cgst', 'sgst', 'change', etc. Return the largest candidate.
    3. If still none, return 0.0
    """
    prioritized_keywords = [
        "grand total",
        "amount payable",
        "amount to be paid",
        "net payable",
        "total payable",
        "total amount",
        "balance due",
        "total",
    ]

    # normalize lines for easier lookups
    normalized = [ln for ln in lines]
    n_lines = len(normalized)

    amount_pattern = re.compile(r"[\d,]+(?:\.\d{1,2})?")

    def amounts_in_line(line: str):
        found = amount_pattern.findall(line.replace(" ", ""))
        parsed = [_parse_amount_str(f) for f in found]
        return [p for p in parsed if p is not None]

    # 1) Search prioritized keywords in order
    for kw in prioritized_keywords:
        for idx, line in enumerate(normalized):
            low = line.lower()
            if kw in low and "sub" not in low:
                # try same line
                cands = amounts_in_line(line)
                if cands:
                    return max(cands)
                # try next line
                if idx + 1 < n_lines:
                    cands = amounts_in_line(normalized[idx + 1])
                    if cands:
                        return max(cands)
                # try previous line
                if idx - 1 >= 0:
                    cands = amounts_in_line(normalized[idx - 1])
                    if cands:
                        return max(cands)
                # no numeric candidate found next to keyword - continue searching other occurrences

    # 2) Fallback: gather all candidate amounts from all lines except excluded-label lines
    exclude_tokens = ["sub total", "subtotal", "cgst", "sgst", "vat", "tax", "taxes", "discount", "change"]
    all_candidates = []
    for line in normalized:
        low = line.lower()
        if any(tok in low for tok in exclude_tokens):
            continue
        cands = amounts_in_line(line)
        if cands:
            all_candidates.extend(cands)

    if all_candidates:
        # usually grand total is the maximum numeric value in the receipt
        return max(all_candidates)

    # 3) as last resort try to capture any amount that looks like a monetary figure
    any_amounts = []
    for line in normalized:
        any_amounts.extend(amounts_in_line(line))
    if any_amounts:
        return max(any_amounts)

    return 0.0


def extract_date_from_text(lines):
    date_patterns = [
        r"\b(\d{1,2}[-/\.\s][A-Za-z]{3}[-/\.\s]\d{2,4})\b",  # 12-DEC-2020
        r"\b(\d{1,2}[-/\.\s]\d{1,2}[-/\.\s]\d{2,4})\b",
        r"\b(\d{4}[-/\.\s]\d{1,2}[-/\.\s]\d{1,2})\b",
        r"\b(\d{1,2} [A-Za-z]{3,9} \d{2,4})\b",
        r"\b([A-Za-z]{3,9} \d{1,2}, \d{4})\b"
    ]

    def parse_date_safe(date_str):
        formats = [
            "%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y", "%Y-%m-%d",
            "%d %B %Y", "%B %d, %Y", "%d/%m/%y", "%d-%m-%y",
            "%d-%b-%Y", "%d-%b-%y"
        ]
        for fmt in formats:
            try:
                dt = datetime.strptime(date_str, fmt)
                if dt.year < 2000:
                    dt = dt.replace(year=dt.year + 100)
                if 2010 <= dt.year <= datetime.now().year + 1:
                    return dt.strftime("%Y-%m-%d")
            except:
                continue
        return None

    valid_dates = []
    for line in lines:
        for pattern in date_patterns:
            matches = re.findall(pattern, line)
            for match in matches:
                parsed = parse_date_safe(match)
                if parsed:
                    valid_dates.append((parsed, line.lower()))

    keywords = [
        "invoice date", "bill date", "payment date", "txn date",
        "transaction date", "paid on", "date of payment", "date"
    ]
    for date, context in valid_dates:
        if any(k in context for k in keywords):
            return date

    return valid_dates[0][0] if valid_dates else "Not Found"

# ---------------- UPDATED FUNCTION: detect_purpose ----------------
# Breakfast merged into Lunch — only two meal categories: Lunch and Dinner

def detect_purpose(text, expense_date=None):
    text_upper = text.upper()

    # --- 1️⃣ Store/vendor-based categories first ---
    store_keywords = {
        "Supplies": ["DMART", "BIG BAZAAR", "RELIANCE", "METRO", "SHOPPER STOP", "LIFESTYLE", "RELIANCE TRENDS"],
        "Shopping": ["AMAZON", "FLIPKART", "MYNTRA", "AJIO"]
    }

    for category, keywords in store_keywords.items():
        if any(k in text_upper for k in keywords):
            return category

    # --- 2️⃣ Meal keywords next (BREAKFAST merged into LUNCH) ---
    meal_keywords = {
        "LUNCH": [
            "MORNING MEAL", "TEA", "COFFEE", "SNACKS", "CAFE", "IDLI", "DOSA", "POHA", "BREAD",
            "MILK", "JUICE", "PANCAKE", "OMELETTE", "BREAKFAST", "BREAKFAST COMBO",
            "THALI", "MEAL", "MIDDAY", "CAFETERIA", "BUFFET", "VEG", "NON-VEG", "LUNCH BOX",
            "RESTAURANT BILL", "SUBWAY", "KFC", "PIZZA HUT", "DOMINOS"
        ],
        "DINNER": [
            "SUPPER", "NIGHT MEAL", "DINNER BUFFET", "RESTAURANT", "EVENING MEAL", "DINNER COMBO",
            "FINE DINE", "FOOD COURT", "ZOMATO", "SWIGGY"
        ]
    }

    # Extract hour if available in expense_date
    meal_by_time = None
    if expense_date and expense_date != "Not Found":
        try:
            try:
                dt = datetime.strptime(expense_date, "%Y-%m-%d %H:%M:%S")
            except:
                dt = datetime.strptime(expense_date, "%Y-%m-%d")
            hour = dt.hour
            # Merge breakfast into lunch: any hour before 17 treated as Lunch
            if hour < 17:
                meal_by_time = "Lunch"
            else:
                meal_by_time = "Dinner"
        except:
            pass

    # Match meal keywords first
    for meal, keywords in meal_keywords.items():
        if any(k in text_upper for k in keywords):
            return meal.capitalize()

    # If it's a restaurant/food bill, fallback to time-of-day classification
    if "RESTAURANT" in text_upper or "FOOD" in text_upper or "MEAL" in text_upper:
        if meal_by_time:
            return meal_by_time
        return "Lunch"  # safer default (breakfast merged into lunch)

    if meal_by_time:
        return meal_by_time

    # --- 3️⃣ Other categories (same as before) ---
    if any(k in text_upper for k in ["CAB", "TAXI", "AUTO", "RIDE", "OLA", "UBER", "RAPIDO", "MERU", "CNG RICKSHAW"]):
        return "Taxi"
    elif any(k in text_upper for k in ["PARKING", "TOLL", "GARAGE", "CAR PARK", "VEHICLE PARKING", "MALL PARKING", "HIGHWAY PARKING"]):
        return "Parking"
    elif any(k in text_upper for k in ["HOTEL", "RESORT", "LODGE", "INN", "MOTEL", "SUITE", "ROOM CHARGE", "STAY", "ACCOMMODATION", "GUEST HOUSE", "BOOKING.COM", "EXPEDIA", "MAKEMYTRIP"]):
        return "Hotel"
    elif any(k in text_upper for k in ["AIRLINES", "FLIGHT", "AIR TICKET", "BOARDING PASS", "INDIGO", "SPICEJET", "VISTARA", "GOFIRST", "AKASA", "EMIRATES", "QATAR AIRWAYS", "JET", "AIRPORT"]):
        return "Air"
    elif any(k in text_upper for k in ["CAR RENTAL", "ZOOMCAR", "REVV", "HERTZ", "AVIS", "ENTERPRISE RENTAL", "SELF DRIVE", "VEHICLE HIRE"]):
        return "Car Rental"
    elif any(k in text_upper for k in ["MOVIE", "CINEMA", "THEATRE", "PVR", "INOX", "BOOKMYSHOW", "NETFLIX", "PRIME", "HOTSTAR", "SPOTIFY", "CONCERT", "EVENT", "SHOW", "GAMING", "SHOPPING", "MALL", "FASHION", "CLOTHES", "GARMENTS", "FOOTWEAR"]):
        return "Entertainment"
    elif any(k in text_upper for k in ["FUEL", "PETROL", "DIESEL", "GAS STATION", "HP", "INDIANOIL", "BPCL", "SHELL", "REFUEL"]):
        return "Fuel"
    elif any(k in text_upper for k in ["STATIONERY", "OFFICE SUPPLY", "PENS", "PRINTER", "CARTRIDGE", "INK", "TONER", "PAPER", "DIARY", "REGISTER", "FILE", "MARKER", "WHITEBOARD", "LAPTOP", "DESKTOP", "MONITOR", "KEYBOARD", "MOUSE", "SCANNER", "HEADPHONES", "EARPHONES", "SPEAKER", "CHARGER", "BATTERY", "ROUTER", "USB", "SSD", "HDD", "MOBILE", "TABLET", "CABLES", "PROJECTOR", "CAMERA", "ELECTRONIC BILL", "ELECTRONIC INVOICE"]):
        return "Supplies"
    elif any(k in text_upper for k in ["HOSPITAL", "PHARMACY", "DOCTOR", "CLINIC", "SURGERY", "MEDICINE", "TABLET", "INJECTION", "LAB", "DIAGNOSTIC", "PATHOLOGY", "XRAY", "SCAN", "MRI", "CHEMIST"]):
        return "Miscellaneous"

    # --- Fallback ---
    return "Miscellaneous"


# --------------------------------------------------

@app.post("/extract-expense-info")
async def extract_expense_info(payload: OCRRequest):
    try:
        words = []
        # ✅ Collect all words from all pages
        for page in payload.pages:
            words.extend(page.get("words", []))

        # ✅ Fallback if "words" not found
        if not words:
            for page in payload.pages:
                words.extend(page.get("tokens", []))
            if not words:
                return JSONResponse(content={"error": "No OCR words or tokens found."}, status_code=400)

        lines = group_words_into_lines(words)
        full_text = " ".join([w.get("text", "").upper() for w in words])

        total = extract_total_amount(lines)
        expense_date = extract_date_from_text(lines)

        # ✅ Currency detection
        if any(cur in full_text for cur in ["INR", "₹", "RS"]):
            currency = "INR"
        elif any(cur in full_text for cur in ["USD", "$"]):
            currency = "USD"
        elif any(cur in full_text for cur in ["EUR", "€"]):
            currency = "EUR"
        else:
            currency = "INR"

        return {
            "ReimbursementCurrencyCode": currency,
            "ExpenseReportTotal": f"{total:.2f}",
            "Purpose": detect_purpose(full_text, expense_date),  # ✅ Updated call
            "ExpenseDate": expense_date,
            "SubmitReport": "Y"
        }

    except Exception as e:
        return JSONResponse(content={"error": str(e)}, status_code=500)

