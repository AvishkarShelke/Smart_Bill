from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Dict, List, Any
import re
from datetime import datetime

app = FastAPI()

# ✅ CORS Config: Only allow your Oracle APEX domain
app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://gccffb251970d0d-acseatpdbus.adb.us-ashburn-1.oraclecloudapps.com"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ✅ Handle preflight request (important for browsers)
@app.options("/extract-expense-info")
async def preflight():
    return JSONResponse(status_code=200)

# ✅ Input schema
class OCRRequest(BaseModel):
    pages: List[Dict[str, Any]]

# ✅ Helper: Group OCR words into lines based on Y-axis
def group_words_into_lines(words):
    lines = []
    current_line = []
    prev_y = None

    for word in sorted(words, key=lambda w: w["boundingPolygon"]["normalizedVertices"][0]["y"]):
        y = round(word["boundingPolygon"]["normalizedVertices"][0]["y"], 2)
        if prev_y is None or abs(y - prev_y) < 0.01:
            current_line.append(word["text"])
        else:
            lines.append(" ".join(current_line))
            current_line = [word["text"]]
        prev_y = y
    if current_line:
        lines.append(" ".join(current_line))
    return lines

# ✅ FIXED total extraction logic — avoids "sub total"
def extract_total_amount(lines):
    prioritized_keywords = [
        "amount to be paid",
        "grand total",
        "total amount",
        "net payable",
        "net amount",
        "total"
    ]

    for keyword in prioritized_keywords:
        for line in lines:
            line_lower = line.lower()
            if keyword in line_lower and "sub" not in line_lower:
                amounts = re.findall(r"\d{2,6}\.\d{2}", line)
                for amt in amounts:
                    val = float(amt)
                    if 50 <= val <= 99999:
                        return val

    fallback_amounts = [
        float(x)
        for line in lines if "sub total" not in line.lower()
        for x in re.findall(r"\d{2,6}\.\d{2}", line)
    ]
    return max(fallback_amounts) if fallback_amounts else 0.0

# ✅ IMPROVED: Smarter date extraction from entire text

def extract_date_from_text(lines):
    date_patterns = [
        r"\b(\d{1,2}[-/\.]\d{1,2}[-/\.]\d{2,4})\b",
        r"\b(\d{4}[-/\.]\d{1,2}[-/\.]\d{1,2})\b",
        r"\b(\d{1,2} [A-Za-z]{3,9} \d{2,4})\b",
        r"\b([A-Za-z]{3,9} \d{1,2}, \d{4})\b"
    ]

    keywords = [
        "invoice date", "bill date", "payment date", "txn date", "transaction date",
        "paid on", "date of payment", "date:", "date"
    ]

    def parse_date_safe(date_str):
        formats = ["%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y", "%Y-%m-%d",
                   "%d %B %Y", "%B %d, %Y", "%d/%m/%y", "%d-%m-%y"]
        for fmt in formats:
            try:
                dt = datetime.strptime(date_str, fmt)
                if dt.year < 2000:
                    dt = dt.replace(year=dt.year + 100)
                if dt.year >= 2010 and dt.year <= datetime.now().year + 1:
                    return dt.strftime("%Y-%m-%d")
            except:
                continue
        return None

    # STEP 1: Inline keyword + date
    for line in lines:
        lower = line.lower()
        if any(kw in lower for kw in keywords):
            for pattern in date_patterns:
                match = re.search(pattern, line)
                if match:
                    parsed = parse_date_safe(match.group(1))
                    if parsed:
                        return parsed

    # STEP 2: Nearby keyword (±1 line)
    for i, line in enumerate(lines):
        if any(kw in line.lower() for kw in keywords):
            nearby = [lines[i - 1] if i > 0 else "", lines[i + 1] if i + 1 < len(lines) else ""]
            for near in nearby:
                for pattern in date_patterns:
                    match = re.search(pattern, near)
                    if match:
                        parsed = parse_date_safe(match.group(1))
                        if parsed:
                            return parsed

    # STEP 3: Global fallback — best-looking valid date
    valid_dates = []
    for line in lines:
        for pattern in date_patterns:
            match = re.search(pattern, line)
            if match:
                parsed = parse_date_safe(match.group(1))
                if parsed:
                    valid_dates.append(parsed)

    if valid_dates:
        return valid_dates[0]  # could apply sorting or most recent logic

    return "Not Found"

# ✅ Detect purpose from full text
def detect_purpose(text):
    text_upper = text.upper()

    medical_keywords = [
        "PHARMACY", "DOCTOR", "DR.", "CLINIC", "HOSPITAL", "SURGERY",
        "NURSING HOME", "MEDICAL CENTER", "LAB", "MBBS", "MD", "DIAGNOSTIC"
    ]
    shopping_keywords = ["DMART", "BIG BAZAAR", "RELIANCE RETAIL", "SHOPPING", "MALL", "FASHION", "APPAREL"]
    fuel_keywords = ["FUEL", "PETROL", "DIESEL", "HPCL", "IOC", "INDIAN OIL", "BPCL", "GAS STATION"]
    food_keywords = ["HOTEL", "RESTAURANT", "FOOD", "DINING", "CAFE", "MEAL", "ZOMATO", "SWIGGY"]
    travel_keywords = ["CAB", "TAXI", "OLA", "UBER", "TRAVEL", "BOOKING.COM", "MAKEMYTRIP", "GOIBIBO", "TRIP"]
    office_keywords = ["STATIONERY", "PRINTER", "TONER", "PAPER", "OFFICE DEPOT", "SUPPLIES", "NOTEBOOK", "PEN", "XEROX"]
    grocery_keywords = ["GROCERY", "PROVISION", "VEGETABLE", "FRUITS", "FOOD BAZAAR", "KIRANA"]
    electronics_keywords = ["LAPTOP", "MOBILE", "ELECTRONICS", "GADGET", "TV", "MONITOR", "CHARGER", "CABLE"]
    commute_keywords = ["BUS", "TRAIN", "TICKET", "RAILWAY", "TRAVEL CARD", "PASS"]

    if any(k in text_upper for k in medical_keywords):
        return "Medical Reimbursement"
    elif any(k in text_upper for k in shopping_keywords):
        return "Shopping Expense"
    elif any(k in text_upper for k in fuel_keywords):
        return "Fuel Reimbursement"
    elif any(k in text_upper for k in food_keywords):
        return "Food/Hotel Expense"
    elif any(k in text_upper for k in travel_keywords):
        return "Travel Expense"
    elif any(k in text_upper for k in office_keywords):
        return "Office/Stationery Purchase"
    elif any(k in text_upper for k in grocery_keywords):
        return "Grocery Reimbursement"
    elif any(k in text_upper for k in electronics_keywords):
        return "Electronics Purchase"
    elif any(k in text_upper for k in commute_keywords):
        return "Commute or Transport Expense"
    return "General Reimbursement"

# ✅ Main API route
@app.post("/extract-expense-info")
async def extract_expense_info(payload: OCRRequest):
    try:
        words = payload.pages[0].get("words", [])
        lines = group_words_into_lines(words)
        full_text = " ".join([w["text"].upper() for w in words])

        total = extract_total_amount(lines)
        expense_date = extract_date_from_text(lines)

        if "INR" in full_text or "₹" in full_text or "RS" in full_text:
            currency = "INR"
        elif "USD" in full_text or "$" in full_text:
            currency = "USD"
        elif "EUR" in full_text or "€" in full_text:
            currency = "EUR"
        else:
            currency = "INR"

        return {
            "ReimbursementCurrencyCode": currency,
            "ExpenseReportTotal": f"{total:.2f}",
            "Purpose": detect_purpose(full_text),
            "ExpenseDate": expense_date,
            "SubmitReport": "Y"
        }

    except Exception as e:
        return JSONResponse(content={"error": str(e)}, status_code=500)










