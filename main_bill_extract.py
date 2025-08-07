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

def extract_total_amount(lines):
    prioritized_keywords = [
        "amount to be paid", "grand total", "total amount", "net payable", "net amount", "total"
    ]
    for keyword in prioritized_keywords:
        for line in lines:
            if keyword in line.lower() and "sub" not in line.lower():
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

def extract_date_from_text(lines):
    date_patterns = [
        r"\b(\d{1,2}[-/\.\s]\d{1,2}[-/\.\s]\d{2,4})\b",
        r"\b(\d{4}[-/\.\s]\d{1,2}[-/\.\s]\d{1,2})\b",
        r"\b(\d{1,2} [A-Za-z]{3,9} \d{2,4})\b",
        r"\b([A-Za-z]{3,9} \d{1,2}, \d{4})\b"
    ]
    def parse_date_safe(date_str):
        formats = ["%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y", "%Y-%m-%d", "%d %B %Y", "%B %d, %Y", "%d/%m/%y", "%d-%m-%y"]
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
    keywords = ["invoice date", "bill date", "payment date", "txn date", "transaction date", "paid on", "date of payment", "date"]
    for date, context in valid_dates:
        if any(k in context for k in keywords):
            return date
    return valid_dates[0][0] if valid_dates else "Not Found"

def detect_purpose(text):
    text_upper = text.upper()
    if any(k in text_upper for k in ["PHARMACY", "DOCTOR", "CLINIC", "HOSPITAL", "SURGERY", "LAB"]):
        return "Medical Reimbursement"
    elif any(k in text_upper for k in ["DMART", "SHOPPING", "MALL", "FASHION"]):
        return "Shopping Expense"
    elif any(k in text_upper for k in ["FUEL", "PETROL", "DIESEL", "GAS STATION"]):
        return "Fuel Reimbursement"
    elif any(k in text_upper for k in ["HOTEL", "RESTAURANT", "FOOD", "ZOMATO", "SWIGGY"]):
        return "Food/Hotel Expense"
    elif any(k in text_upper for k in ["CAB", "TAXI", "TRAVEL", "OLA", "UBER"]):
        return "Travel Expense"
    elif any(k in text_upper for k in ["STATIONERY", "PRINTER", "TONER", "SUPPLIES"]):
        return "Office/Stationery Purchase"
    elif any(k in text_upper for k in ["GROCERY", "VEGETABLE", "KIRANA"]):
        return "Grocery Reimbursement"
    elif any(k in text_upper for k in ["LAPTOP", "MOBILE", "GADGET", "ELECTRONICS"]):
        return "Electronics Purchase"
    elif any(k in text_upper for k in ["BUS", "TRAIN", "TICKET", "RAILWAY"]):
        return "Commute or Transport Expense"
    return "General Reimbursement"

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
            "Purpose": detect_purpose(full_text),
            "ExpenseDate": expense_date,
            "SubmitReport": "Y"
        }

    except Exception as e:
        return JSONResponse(content={"error": str(e)}, status_code=500)












