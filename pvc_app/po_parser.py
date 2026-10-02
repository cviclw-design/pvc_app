# pvc_app/po_parser.py

import fitz  # PyMuPDF
import pandas as pd
import re
from datetime import datetime
import os
import logging
from pandas.errors import OutOfBoundsDatetime


def extract_value(pattern, text, default=""):
    """Extracts a single value using regex."""
    match = re.search(pattern, text, re.IGNORECASE | re.S)
    return match.group(1).strip() if match else default


def normalize_date(date_str):
    """Normalizes various date string formats to DD-MM-YYYY."""
    if not date_str:
        return ""
    date_str = date_str.strip().upper()
    formats = [
        "%d/%m/%y", "%d/%m/%Y", "%d-%m-%y", "%d-%m-%Y",
        "%d-%b-%y", "%d-%b-%Y", "%d %b %y", "%d %b %Y", "%Y-%m-%d"
    ]
    for fmt in formats:
        try:
            return datetime.strptime(date_str, fmt).strftime("%d-%m-%Y")
        except ValueError:
            continue
    return date_str


def to_excel_date(val):
    """Converts a DD-MM-YYYY string to a pandas datetime object for Excel."""
    try:
        return pd.to_datetime(val, format="%d-%m-%Y", errors="coerce")
    except (ValueError, OutOfBoundsDatetime):
        return pd.NaT


def extract_freight_rate(block_text):
    """Extracts freight charges per unit from a block of text."""
    match = re.search(
        r"Freight\s*Charges.*?@Rs\.?\s*([\d,\.]+)\s*per\s*Unit",
        block_text, re.IGNORECASE | re.S
    )
    return match.group(1).replace(",", "") if match else "0"


def extract_data_from_pdf(pdf_path, filename):
    """Extracts structured data from a single PO PDF file."""
    try:
        with fitz.open(pdf_path) as doc:
            text = "".join(page.get_text("text") for page in doc)
    except Exception as e:
        logging.error(f"Error reading PDF {filename}: {e}")
        return []

    # === Header extraction ===
    po_no = extract_value(r"P\.?O\.?\.?No\.?:\s*([\dA-Za-z]+)", text)
    po_date = normalize_date(extract_value(r"dated\s+([\d\-A-Z/ ]+)", text))
    supplier = extract_value(r"To,\s*(.+?)\n", text, "UNKNOWN SUPPLIER")
    total_value = extract_value(r"Total Order Value:\s*Rs\.([\d,\.]+)", text).replace(",", "")
    hsn_code = extract_value(r"HSN Code\s+(\d+)", text)
    inspection = extract_value(r"Inspection agency\s*:\s*(.+)", text)
    pvc_base_date = normalize_date(extract_value(r"PVC base date:\s*([\d/ \-A-Z]+)", text))
    signed_by = extract_value(r"Digitally Signed by\s*(.+?),", text)
    basic_rate = extract_value(r"Basic Rate:\s*Rs\.\s*([\d,\.]+)", text).replace(",", "")
    payment_terms = extract_value(r"Payment Terms\s*(.+?)(?=\n\d|\Z)", text)
    bank_ac = extract_value(r"Bank Account No:\s*(\d+)", text)
    bank_ifsc = extract_value(r"IFSC Code:\s*([\w]+)", text)
    bank_details = f"A/c No: {bank_ac}, IFSC: {bank_ifsc}" if bank_ac else ""
    po_key = extract_value(r"PO\.?Key:\s*([\d]+)", text)

    # === Split PL blocks and extract line items ===
    pl_blocks = re.split(r"PL No\s*:", text)[1:]
    records = []

    for block in pl_blocks:
        pl_no_match = re.match(r"(\S+)", block.strip())
        if not pl_no_match:
            continue

        pl_no = pl_no_match.group(1)
        total_item_qty = extract_value(r"Item Qty\s*:\s*([\d\.]+)", block)
        desc = " ".join(extract_value(r"Description\s*:\s*(.+?)(?=\( Warranty|Basic Rate|Consignee)", block, "").split())
        freight_rate = extract_freight_rate(block)

        sr_pattern = re.compile(r"(\d{3})\s*\n\s*(\d{2}/\d{2}/\d{2})\s*(\d{2}/\d{2}/\d{2})\s*([\d\.]+)", re.M)

        for sr_no, start_raw, end_raw, ordered_qty in sr_pattern.findall(block):
            freight_total = float(freight_rate) * float(ordered_qty) if freight_rate != "0" else 0

            records.append({
                "PO Key": po_key, "PO No": po_no, "PO Date": po_date,
                "PO Sr No": sr_no, "PL No": pl_no, "Item Description": desc,
                "Ordered Qty": ordered_qty, "Total Item Qty": total_item_qty,
                "Basic Rate": basic_rate, "Freight Rate (Per Unit)": freight_rate,
                "Freight Basis": "PER UNIT" if freight_rate != "0" else "NA",
                "Freight Applicable": "YES" if freight_rate != "0" else "NO",
                "Freight Total": freight_total, "Total PO Value": total_value,
                "PVC Base Date": pvc_base_date,
                "Consignee": extract_value(r"Consignee\s*\n\s*(.+)", block, "N/A"),
                "Delivery Start": normalize_date(start_raw),
                "Delivery End": normalize_date(end_raw),
                "Inspection Agency": inspection, "Payment Terms": payment_terms,
                "Supplier": supplier, "Buyer": "Chittaranjan Locomotive Works (CLW)",
                "HSN Code": hsn_code, "Bank Details": bank_details,
                "Signed By": signed_by, "Source File": filename,
                "Source Folder": os.path.basename(os.path.dirname(pdf_path))
            })
    return records