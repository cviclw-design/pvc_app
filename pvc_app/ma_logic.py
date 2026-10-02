# pvc_app/ma_logic.py

import os
import re
import fitz  # PyMuPDF
import pandas as pd
import requests
import urllib.request
from datetime import datetime
from urllib.parse import urljoin
from openpyxl import load_workbook
from openpyxl.styles import PatternFill, Alignment, Border, Side


# ─── MA Downloader Helpers ──────────────────────────────────────────────────

def find_working_year_url(base_url):
    """Finds a working URL by trying different years."""
    match = re.search(r'20\d{2}', base_url)
    if not match:
        return None

    current_year = int(match.group())
    candidate_years = [y for y in range(2021, 2027) if y != current_year]
    test_urls = [base_url] + [re.sub(r'20\d{2}', str(year), base_url) for year in candidate_years]

    for test_url in test_urls:
        if not test_url.startswith(('http://', 'https://')):
            test_url = "https://" + test_url.strip()
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
        }
        try:
            r = requests.head(test_url, timeout=8, headers=headers)
            if r.status_code == 200:
                return test_url
        except Exception:
            continue
    return None


def download_file_utility(file_url, file_path):
    """Utility to download a single file."""
    try:
        urllib.request.urlretrieve(file_url, file_path)
        try:
            os.startfile(file_path)
        except Exception:
            pass # Fail silently if auto-open fails
        return f"Downloaded: {os.path.basename(file_path)}"
    except Exception as e:
        return f"Failed: {file_url} | {e}"


# ─── MA Extractor Helpers ───────────────────────────────────────────────────

def parse_date_safe(x):
    if pd.isna(x) or x is None: return pd.NaT
    x = str(x).strip()
    if not x: return pd.NaT
    fmts = [
        "%d-%m-%Y", "%d/%m/%Y", "%d.%m.%Y",
        "%d-%m-%y", "%d/%m/%y",
        "%Y-%m-%d", "%Y/%m/%d",
        "%d-%b-%y", "%d-%b-%Y",
        "%d-%B-%y", "%d-%B-%Y",
        "%d %b %Y", "%d %B %Y"
    ]
    for f in fmts:
        try:
            return datetime.strptime(x, f)
        except Exception:
            continue
    return pd.to_datetime(x, dayfirst=True, errors="coerce")


def derive_condition_from_remarks(remarks, po_sr, fallback=""):
    if remarks is None or str(remarks).strip() == "": return "No Remarks"
    if not po_sr: return fallback
    text = remarks.lower()
    sr_patterns = [f"po sr. no. {int(po_sr)}", f"po sr no. {int(po_sr)}", f"po sr. no.{int(po_sr)}", f"po sr no.{int(po_sr)}", f"po_sr_no {int(po_sr)}", f"po sr no {int(po_sr)}"]
    if not any(p in text for p in sr_patterns): return fallback
    lower_rate_phrases = ["comparison of ld and lower rate", "comparison of ld & lower rate", "whichever is beneficial", "lower rate will be applicable", "implication of ld charges or lower rate", "lower rate received"]
    if any(p in text for p in lower_rate_phrases): return "Lower rate comparison applicable"
    if "without ld" in text: return "without"
    if "with ld" in text or "full ld" in text: return "with ld"
    if "re-fixed" in text or "refixed" in text: return "Re-fixed"
    return fallback


def apply_excel_formatting(path, condition_column_name="Condition"):
    wb = load_workbook(path)
    ws = wb.active
    for col in ws.columns:
        max_len = 0
        col_letter = col[0].column_letter
        for cell in col:
            val = "" if cell.value is None else str(cell.value)
            max_len = max(max_len, len(val))
        ws.column_dimensions[col_letter].width = max_len + 2
    for row in ws.iter_rows():
        for cell in row:
            cell.alignment = Alignment(wrap_text=True, vertical="top")
    ws.freeze_panes = "A2"
    header_map = {str(c.value).strip().lower(): c.column for c in ws[1] if c.value}
    yellow, blue, green = PatternFill(start_color="FFFACD", fill_type="solid"), PatternFill(start_color="CCE5FF", fill_type="solid"), PatternFill(start_color="CCFFCC", fill_type="solid")
    if condition_column_name.lower() in header_map:
        idx = header_map[condition_column_name.lower()] - 1
        for row in ws.iter_rows(min_row=2):
            cond = str(row[idx].value or "").lower()
            if "start" in cond:
                for c in row: c.fill = yellow
            elif "new" in cond:
                for c in row: c.fill = blue
            elif "end" in cond:
                for c in row: c.fill = green
    thin = Side(style="thin")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)
    for row in ws.iter_rows():
        for cell in row:
            cell.border = border
    wb.save(path)


def extract_ma_only(input_folder):
    """Extract MA / Amendment details from PDFs and return a DataFrame."""
    records = []
    for root, _, files in os.walk(input_folder):
        for fname in files:
            if not fname.lower().endswith(".pdf") or "_" not in fname:
                continue

            pdf_path = os.path.join(root, fname)
            try:
                with fitz.open(pdf_path) as doc:
                    text = "\n".join(page.get_text("text") for page in doc)
            except Exception:
                continue

            po_no_m = re.search(r"P\.?O\.?\s*No[:.]?\s*([0-9A-Za-z\/\-]+)", text, re.IGNORECASE)
            po_no = po_no_m.group(1).strip() if po_no_m else ""

            remarks_m = re.search(r"Remarks\s*[:\-]?\s*(.*?)Copy\s*to", text, re.DOTALL | re.IGNORECASE)
            remarks = remarks_m.group(1).replace("\n", " ").strip() if remarks_m else ""

            qty_pairs = re.findall(r"PO\s*Sr\.?\s*No\.?\s*(\d+)\s*for\s*([0-9]+)", remarks, re.IGNORECASE)
            qty_map = {sr.zfill(3): qty for sr, qty in qty_pairs}

            old_pattern = re.findall(
                r"P\.?O\.?\s*Sr\.?\s*(\d+).*?"
                r"PLNo[-–]?\s*([0-9]+).*?"
                r"(\d{2}-[A-Za-z]{3}-\d{2,4}).*?"
                r"(\d{2}-[A-Za-z]{3}-\d{2,4}).*?"
                r"((?:with|without|re[-\s]?fixed|delivery).*?)",
                text, re.DOTALL | re.IGNORECASE
            )

            for sr_raw, plno, old_dp, revised_dp, cond in old_pattern:
                sr = sr_raw.zfill(3)
                final_cond = derive_condition_from_remarks(remarks, sr, cond.strip())
                records.append({
                    "File": fname, "PO No": po_no, "PO Sr No": sr, "PL No": plno,
                    "Old DP": old_dp, "Revised DP": revised_dp, "Condition": final_cond,
                    "Quantity (Sets)": qty_map.get(sr, ""), "Remarks": remarks
                })

            new_po = re.findall(
                r"New\s*P\.?O\.?\.?Sr\.?\.?No\.?\s*(\d+).*?"
                r"PLNo[-–]?\s*([0-9\s]+).*?"
                r"P\.?O\.?Qty[:.]?\s*([0-9.]+).*?"
                r"Delivery\s*by\s*[:.]?\s*([0-9]{2}/[0-9]{2}/[0-9]{2,4})",
                text, re.DOTALL | re.IGNORECASE
            )

            for sr_raw, plno, qty, revised_dp in new_po:
                sr = sr_raw.zfill(3)
                final_cond = derive_condition_from_remarks(remarks, sr, "New PO Sr Added")
                records.append({
                    "File": fname, "PO No": po_no, "PO Sr No": sr, "PL No": plno.replace(" ", ""),
                    "Old DP": revised_dp, "Revised DP": revised_dp, "Condition": final_cond,
                    "Quantity (Sets)": qty, "Remarks": remarks
                })

    if not records:
        return pd.DataFrame()

    df = pd.DataFrame(records)
    df["PO Sr No"] = df["PO Sr No"].astype(str).str.zfill(3)
    return df


def process_ma_extract_data(df):
    """
    Applies business logic to format and enhance the extracted MA data.
    - Unifies freight rates per PO.
    - Auto-fills 'OK' in remarks.
    - Calculates final delivery dates based on 'New' and 'Re-fixed' conditions.
    """
    if df.empty:
        return df

    # Ensure required columns exist, adding them if they don't to prevent errors
    required_cols = ["po no", "po sr no", "Condition", "Freight Rate (Per Unit)", "Basic Rate", "Remarks", "Revised DP", "Old DP"]
    for col in required_cols:
        if col not in df.columns:
            # Try to find case-insensitive match first
            found = next((c for c in df.columns if c.lower() == col.lower()), None)
            if found:
                df.rename(columns={found: col}, inplace=True)
            else:
                df[col] = None # Add missing column

    df["po no"] = df["po no"].astype(str).str.strip()
    df["po sr no"] = df["po sr no"].astype(str).str.zfill(3)
    df["Condition"] = df["Condition"].astype(str).str.strip()

    # Rule 1: Unify Freight Rate per PO
    freight_col = "Freight Rate (Per Unit)"
    for _, grp in df.groupby("po no"):
        valid_rates = grp[freight_col].dropna()
        valid_rates = valid_rates[valid_rates.astype(str).str.strip() != ""]
        if not valid_rates.empty:
            common_rate = valid_rates.iloc[0]
            df.loc[grp.index, freight_col] = common_rate

    # Rule 2: Auto-fill Remarks
    remarks_mask = (
        df["Basic Rate"].notna() &
        df[freight_col].notna() &
        (df["Remarks"].isna() | (df["Remarks"].astype(str).str.strip() == ""))
    )
    df.loc[remarks_mask, "Remarks"] = "OK"

    # Rule 3: Calculate Final Delivery Dates
    df["final_delivery_date"] = pd.NaT
    for (po_no, po_sr), grp in df.groupby(["po no", "po sr no"]):
        new_rows = grp[grp["Condition"].str.lower().str.startswith("new", na=False)]
        if new_rows.empty: continue

        new_index = new_rows.index[0]
        master_dt = parse_date_safe(df.at[new_index, "Revised DP"])
        if pd.isna(master_dt):
            master_dt = parse_date_safe(df.at[new_index, "Old DP"])
        if pd.isna(master_dt): continue

        for idx in grp.index:
            cond = str(df.at[idx, "Condition"]).strip().lower()
            new_date = parse_date_safe(df.at[idx, "Revised DP"]) if cond.startswith(("re-fixed", "refix", "without")) else master_dt
            df.at[idx, "final_delivery_date"] = new_date if pd.notna(new_date) else master_dt

    return df