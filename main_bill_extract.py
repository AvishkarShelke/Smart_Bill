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
    s_clean = re.sub(r"[₹$€£]|INR|USD|EUR|GBP", "", s, flags=re.IGNORECASE)
    s_clean = re.sub(r"(?<=\d)\s+(?=\d)", "", s_clean)
    s_clean = re.sub(r"[^0-9,\.]", "", s_clean)
    s_clean = s_clean.replace(",", "")
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

def extract_total_amount(lines: List[str]) -> float:
    prioritized_keywords = [
        "grand total",
        "amount payable",
        "amount to be paid",
        "net payable",
        "total payable",
        "total amount",
        "balance due",
        "total bill amount"
        "bill amount"
        "amount payable from customer",
        "upi payment",
        "net amt"
        "net amount"
        "total",
    ]

    normalized = [ln for ln in lines]
    n_lines = len(normalized)
    amount_pattern = re.compile(r"[\d,]+(?:\.\d{1,2})?")

    def amounts_in_line(line: str):
        found = amount_pattern.findall(line.replace(" ", ""))
        parsed = [_parse_amount_str(f) for f in found]
        return [p for p in parsed if p is not None]

    for kw in prioritized_keywords:
        for idx, line in enumerate(normalized):
            low = line.lower()
            if kw in low and "sub" not in low:
                cands = amounts_in_line(line)
                if cands:
                    return max(cands)
                if idx + 1 < n_lines:
                    cands = amounts_in_line(normalized[idx + 1])
                    if cands:
                        return max(cands)
                if idx - 1 >= 0:
                    cands = amounts_in_line(normalized[idx - 1])
                    if cands:
                        return max(cands)

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
        return max(all_candidates)

    any_amounts = []
    for line in normalized:
        any_amounts.extend(amounts_in_line(line))
    if any_amounts:
        return max(any_amounts)

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

def get_safe_date(date_str: str):
    try:
        if not date_str or date_str in ["Not Found", "0", "0000-00-00"]:
            return datetime.today().strftime("%Y-%m-%d")
        datetime.strptime(date_str, "%Y-%m-%d")
        return date_str
    except:
        return datetime.today().strftime("%Y-%m-%d")

# ---------------- detect_purpose (reprioritized) ----------------
def detect_purpose(text, expense_date=None):
    text_upper = text.upper()

    # Store-based detection (priority: Supplies & Shopping)
    store_keywords = {
        "Supplies": ["DMART", "BIG BAZAAR", "RELIANCE", "METRO", "SHOPPER STOP", "LIFESTYLE", "RELIANCE TRENDS"],
        "Shopping": ["AMAZON", "FLIPKART", "MYNTRA", "AJIO"]
    }
    for category, keywords in store_keywords.items():
        if any(k in text_upper for k in keywords):
            return category

    # Travel-related
    if any(k in text_upper for k in ["AIRLINES", "FLIGHT", "AIR TICKET", "BOARDING PASS", "INDIGO", "SPICEJET", "VISTARA", "GOFIRST", "AKASA", "EMIRATES", "QATAR AIRWAYS", "JET", "AIRPORT"]):
        return "Air"
    if any(k in text_upper for k in ["CAB", "TAXI", "AUTO", "RIDE", "OLA", "UBER", "RAPIDO", "MERU", "CNG RICKSHAW"]):
        return "Taxi"
    if any(k in text_upper for k in ["CAR RENTAL", "ZOOMCAR", "REVV", "HERTZ", "AVIS", "ENTERPRISE RENTAL", "SELF DRIVE", "VEHICLE HIRE"]):
        return "Car Rental"
    if any(k in text_upper for k in ["PARKING", "TOLL", "GARAGE", "CAR PARK", "VEHICLE PARKING", "MALL PARKING", "HIGHWAY PARKING"]):
        return "Parking"
    if any(k in text_upper for k in ["FUEL", "PETROL", "DIESEL", "GAS STATION", "HP", "INDIANOIL", "BPCL", "SHELL", "REFUEL"]):
        return "Fuel"

    # Accommodation
    if any(k in text_upper for k in ["ROOM NO", "RESORT", "LODGE", "INN", "INN TIME","OUT TIME","MOTEL", "SUITE", "ROOM CHARGE", "STAY", "ACCOMMODATION", "GUEST HOUSE", "BOOKING.COM", "EXPEDIA", "MAKEMYTRIP"]):
        return "Hotel"

    # Entertainment
    if any(k in text_upper for k in ["MOVIE", "CINEMA", "THEATRE", "PVR", "INOX", "BOOKMYSHOW", "NETFLIX", "PRIME", "HOTSTAR", "SPOTIFY", "CONCERT", "EVENT", "SHOW", "GAMING", "SHOPPING", "MALL", "FASHION", "CLOTHES", "GARMENTS", "FOOTWEAR"]):
        return "Entertainment"

    # Office & Medical
    if any(k in text_upper for k in ["STATIONERY", "OFFICE SUPPLY", "PENS", "PRINTER", "CARTRIDGE", "INK", "TONER", "PAPER", "DIARY", "REGISTER", "FILE", "MARKER", "WHITEBOARD", "LAPTOP", "DESKTOP", "MONITOR", "KEYBOARD", "MOUSE", "SCANNER", "HEADPHONES", "EARPHONES", "SPEAKER", "CHARGER", "BATTERY", "ROUTER", "USB", "SSD", "HDD", "MOBILE", "TABLET", "CABLES", "PROJECTOR", "CAMERA", "ELECTRONIC BILL", "ELECTRONIC INVOICE"]):
        return "Supplies"
    if any(k in text_upper for k in ["HOSPITAL", "PHARMACY", "DOCTOR", "CLINIC", "SURGERY", "MEDICINE", "TABLET", "INJECTION", "LAB", "DIAGNOSTIC", "PATHOLOGY", "XRAY", "SCAN", "MRI", "CHEMIST"]):
        return "Miscellaneous"

    # Meals (lowest priority)
    meal_keywords = {
        "Lunch": ["MORNING MEAL", "TEA", "COFFEE", "SNACKS", "CAFE", "IDLI", "DOSA", "POHA", "BREAD",
                  "MILK", "JUICE", "PANCAKE", "OMELETTE", "BREAKFAST", "BREAKFAST COMBO",
                  "THALI", "MEAL", "MIDDAY", "CAFETERIA", "BUFFET", "VEG", "NON-VEG", "LUNCH BOX",
                  "RESTAURANT BILL", "SUBWAY", "KFC", "PIZZA HUT", "DOMINOS"],
        "Dinner": ["SUPPER", "NIGHT MEAL", "DINNER BUFFET", "RESTAURANT", "EVENING MEAL", "DINNER COMBO",
                   "FINE DINE", "FOOD COURT", "ZOMATO", "SWIGGY"]
    }

    meal_by_time = None
    if expense_date and expense_date != "Not Found":
        try:
            try:
                dt = datetime.strptime(expense_date, "%Y-%m-%d %H:%M:%S")
            except:
                dt = datetime.strptime(expense_date, "%Y-%m-%d")
            hour = dt.hour
            if hour < 17:
                meal_by_time = "Lunch"
            else:
                meal_by_time = "Dinner"
        except:
            pass

    for meal, keywords in meal_keywords.items():
        if any(k in text_upper for k in keywords):
            return meal

    if "RESTAURANT" in text_upper or "FOOD" in text_upper or "MEAL" in text_upper:
        if meal_by_time:
            return meal_by_time
        return "Lunch"

    if meal_by_time:
        return meal_by_time

    return "Miscellaneous"

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
        full_text = " ".join([w.get("text", "").upper() for w in words])

        total = extract_total_amount(lines)

        raw_expense_date = extract_date_from_text(lines)
        expense_date = get_safe_date(raw_expense_date)

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
            "Purpose": detect_purpose(full_text, expense_date),
            "ExpenseDate": expense_date,
            "SubmitReport": "Y"
        }

    except Exception as e:
        return JSONResponse(content={"error": str(e)}, status_code=500)




