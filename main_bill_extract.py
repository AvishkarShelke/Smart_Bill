from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from typing import Dict, List, Any
import re

app = FastAPI()

class OCRRequest(BaseModel):
    pages: List[Dict[str, Any]]

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

def extract_total_amount(lines):
    total_keywords = ["grand total", "net amount", "total", "net payable", "amount to be paid"]
    potential_amounts = []

    for line in lines:
        line_lower = line.lower()
        if any(kw in line_lower for kw in total_keywords):
            amounts = re.findall(r"\d{2,6}\.\d{2}", line)
            for amt in amounts:
                val = float(amt)
                if 50 <= val <= 99999:
                    potential_amounts.append(val)

    if potential_amounts:
        return max(potential_amounts)

    # Fallback: use max float in all lines
    fallback_amounts = re.findall(r"\d{2,6}\.\d{2}", " ".join(lines))
    if fallback_amounts:
        return max([float(x) for x in fallback_amounts])
    return 0.0

def detect_purpose(text):
    if "PHARMACY" in text:
        return "Medical Reimbursement"
    elif "DMART" in text:
        return "DMart Shopping"
    elif any(k in text for k in ["FUEL", "PETROL", "HPCL", "IOC"]):
        return "Fuel Reimbursement"
    elif any(k in text for k in ["HOTEL", "RESTAURANT", "FOOD"]):
        return "Food/Hotel Expense"
    return "General Reimbursement"

@app.post("/extract-expense-info")
async def extract_expense_info(payload: OCRRequest):
    try:
        words = payload.pages[0].get("words", [])
        lines = group_words_into_lines(words)
        full_text = " ".join([w["text"].upper() for w in words])

        total = extract_total_amount(lines)

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
            "SubmitReport": "Y"
        }

    except Exception as e:
        return JSONResponse(content={"error": str(e)}, status_code=500)
