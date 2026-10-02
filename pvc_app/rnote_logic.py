# pvc_app/rnote_logic.py

import os
import re
import requests
import pdfplumber
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor


def parse_rnote(pdf_path):
    """Extracts data from an R-Note PDF file."""
    lines = []
    try:
        with pdfplumber.open(pdf_path) as pdf:
            for page in pdf.pages:
                text = page.extract_text()
                if text:
                    lines.extend(text.split("\n"))
    except Exception:
        return {}

    data = {
        "rnote_no": None, "rnote_date": None, "po_no": None, "po_date": None,
        "po_sr_no": None, "allocation": None, "pl_no": None, "qty_invoiced": None,
        "qty_received": None, "acc_qty": None, "qty_rejected": None,
        "dp_extended": None, "sup_date": None, "ic_no": None, "ic_date": None,
    }
    DATE_RE = r'\d{2}/\d{2}/\d{2}'

    for i, line in enumerate(lines):
        l = line.strip()
        if re.match(r"\d{10}\s+\d{2}/\d{2}/\d{2}", l):
            parts = l.split()
            if len(parts) >= 6:
                data.update(rnote_no=parts[0], rnote_date=parts[1], po_no=parts[2],
                            po_date=parts[3], po_sr_no=parts[4], allocation=parts[5])
        elif "PL No" in l:
            m = re.search(r'PL\s*No\.?\s*:?\s*(\d+)', l)
            if m: data["pl_no"] = m.group(1)
        elif "Qty.Invoiced" in l:
            nums = []
            if i + 1 < len(lines):
                nums = re.findall(r'\d+\.\d+', lines[i + 1])
            if len(nums) < 4:
                nums = re.findall(r'\d+\.\d+', " ".join(lines[i:i + 5]))
            if len(nums) >= 4:
                data.update(qty_invoiced=nums[0], qty_received=nums[1],
                            acc_qty=nums[2], qty_rejected=nums[3])
        elif "Due Date of Delivery" in l: # This block needs to be before IC No to avoid conflicts
            due_idx, act_idx = l.find("Due Date of Delivery"), l.find("Actual Date of Supply")
            next_line = lines[i + 1].strip() if i + 1 < len(lines) else ""
            dates_pool = re.findall(DATE_RE, next_line) or re.findall(DATE_RE, l)
            if act_idx != -1:
                ordered_labels = sorted([("due", due_idx), ("act", act_idx)], key=lambda x: x[1])
                for (label, _), date_val in zip(ordered_labels, dates_pool):
                    if label == "due": data["dp_extended"] = date_val
                    else: data["sup_date"] = date_val
            elif dates_pool:
                data["dp_extended"] = dates_pool[0]
        elif "Actual Date of Supply" in l and "Due Date of Delivery" not in l:
            dates_pool = re.findall(DATE_RE, l) or (re.findall(DATE_RE, lines[i + 1]) if i + 1 < len(lines) else [])
            if dates_pool: data["sup_date"] = dates_pool[0]
        
        # --- IC Number Extraction (More Robust) ---
        # Flexible regex for "IC No" or "IC Number" label
        IC_LABEL_REGEX = r'IC\s*(?:No|Number)\.?'
        # Regex to capture IC number and optional date, assuming it follows the label on the same line
        IC_FULL_LINE_REGEX = rf'(?:{IC_LABEL_REGEX})\s*([A-Za-z0-9/]+)(?:\s+dated\s+([\d/A-Za-z]+))?'
        # Regex to capture IC number and optional date, assuming it's at the start of the next line
        IC_NEXT_LINE_REGEX = r'^([A-Za-z0-9/]+)(?:\s+dated\s+([\d/A-Za-z]+))?'

        # 1. Try to find IC number and date on the current line, following the label
        m = re.search(IC_FULL_LINE_REGEX, l, re.IGNORECASE)
        if m:
            data["ic_no"] = m.group(1)
            if m.group(2): data["ic_date"] = m.group(2)

        # 2. If the label is found on the current line, but the number wasn't captured above,
        #    try to find the IC number at the beginning of the next line.
        #    This handles cases where "IC No" is on one line and the number starts the next.
        elif re.search(IC_LABEL_REGEX, l, re.IGNORECASE):
            if i + 1 < len(lines):
                next_l = lines[i+1].strip()
                m_next = re.search(IC_NEXT_LINE_REGEX, next_l, re.IGNORECASE)
                if m_next:
                    data["ic_no"] = m_next.group(1)
                    if m_next.group(2): data["ic_date"] = m_next.group(2)

    return data
 
def parse_ic_pdf(pdf_path):
    """Extracts data from an IC PDF file."""
    data = {"po_no": "", "po_sr_no": "", "qty": "", "call_date": ""}
    text = ""
    try:
        with pdfplumber.open(pdf_path) as pdf:
            text = "\n".join(page.extract_text() for page in pdf.pages if page.extract_text())
    except Exception:
        return data

    patterns = {
        "intertek": {
            "po_no": r'PO Number:\s*(\d+)',
            "po_sr_no": r'PO Serial Number:\s*(\d+)',
            "qty": r'Qty Passed\s*(\d+)',
            "call_date": r'Call ID No\.\:\s*\d+\s*dated\s*([\d/]+)',
        },
        "clw": {
            "po_no": r'P\.O\./Contract No\.\:\s*(\d+)',
            "po_sr_no": r'PO Sr:\s*(\d+)',
            "qty": r'Quantity now passed\s*(\d+)',
            "call_date": r'Stores offered for inspection on\s*:\s*([\d/]+)',
        }
    }

    for p_type in ["intertek", "clw"]:
        for key, pattern in patterns[p_type].items():
            if not data[key]:
                m = re.search(pattern, text)
                if m:
                    data[key] = m.group(1)
    return data


def _get_ic_search_params(ic_ref):
    """Generates candidate URLs and filenames for an IC number."""
    year_match = re.search(r'(20\d{2})', ic_ref)
    year = int(year_match.group(1)) if year_match else None

    if year:
        months = [f"{m:02d}{year}" for m in range(1, 13)]
    else:
        months = []
        today = datetime.today()
        for y in range(today.year - 2, today.year + 2):
            for m in range(1, 13):
                months.append(f"{m:02d}{y}")

    simple_ref = ic_ref
    complex_ref = ic_ref.replace("/", "")
    if not complex_ref.endswith("_CS"):
        complex_ref += "_CS"

    urls = []
    for my in months:
        urls.append(f"https://www.ireps.gov.in/ireps/etender/ct/tpi/ic/{my}/{simple_ref}.pdf")
        urls.append(f"https://www.ireps.gov.in/ireps/etender/ct/im/ic/{my}/{complex_ref}.pdf")
    return urls


def download_and_parse_ic(ic_ref, download_dir):
    """Finds, downloads, and parses a single IC PDF."""
    if not ic_ref:
        return None

    # Correct common typo: replace 'dm' with 'ic' (case-insensitive)
    if "dm" in ic_ref.lower():
        ic_ref = re.sub(r'dm', 'ic', ic_ref, flags=re.IGNORECASE)

    urls_to_try = _get_ic_search_params(ic_ref)
    
    def fetch_url(url):
        try:
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
            }
            r = requests.get(url, timeout=8, headers=headers)
            if r.status_code == 200 and r.content.startswith(b"%PDF"):
                return r.content
        except requests.RequestException:
            return None
        return None

    with ThreadPoolExecutor(max_workers=10) as executor:
        future_to_url = {executor.submit(fetch_url, url): url for url in urls_to_try}
        for future in future_to_url:
            content = future.result()
            if content:
                filename = ic_ref.replace("/", "_") + ".pdf"
                pdf_path = os.path.join(download_dir, filename)
                with open(pdf_path, "wb") as f:
                    f.write(content)
                try:
                    os.startfile(pdf_path)
                except Exception:
                    pass  # Fail silently if auto-open doesn't work
                
                parsed_data = parse_ic_pdf(pdf_path)
                parsed_data["ic_no"] = ic_ref
                parsed_data["source_file"] = filename
                return parsed_data

    return {"ic_no": ic_ref, "po_no": "NOT FOUND", "po_sr_no": "", "qty": "", "call_date": ""}


def download_rnote_pdf(rnote_no, download_dir):
    """Downloads a single R-Note PDF, returning its path and filename."""
    # Define possible base URL patterns
    base_url_patterns = [
        "https://www.ireps.gov.in/ireps/etender/pdfdocs/MMIS/RN/{year}/21/{rnote_no}.pdf",  # Prioritize /RN/
        "https://www.ireps.gov.in/ireps/etender/pdfdocs/MMIS/RNOTE/{year}/21/{rnote_no}.pdf",  # Also try /RNOTE/
    ]
    filename = f"{rnote_no}.pdf"
    file_path = os.path.join(download_dir, filename)
    
    # --- Intelligent Year Guessing ---
    years_to_try = []
    try:
        # Handle prefixes like '4224...' -> 2024 or '0424...' -> 2024
        if len(rnote_no) >= 4 and (rnote_no.startswith('42') or rnote_no.startswith('04')):
            derived_year = 2000 + int(rnote_no[2:4])
            # Prioritize derived year and a small window around it
            years_to_try.extend([derived_year, derived_year - 1, derived_year + 1])
    except (ValueError, IndexError):
        pass # If derivation fails, proceed with the standard loop

    # --- Fallback Year Range ---
    current_year = datetime.now().year
    # Add a wider range of recent years, avoiding duplicates
    for year in range(current_year, current_year - 6, -1):
        if year not in years_to_try:
            years_to_try.append(year)

    # --- R-Note Number Variants ---
    rnote_variants = [rnote_no]
    if rnote_no.startswith('0') and len(rnote_no) == 10:
        rnote_variants.append(rnote_no[1:]) # Add 9-digit variant

    # --- URL Fetching Loop ---
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
    }
    for rn_variant in rnote_variants:
        for year in years_to_try:
            for base_pattern in base_url_patterns:
                url = base_pattern.format(year=year, rnote_no=rn_variant)
                try:
                    response = requests.get(url, timeout=15, headers=headers)
                    if response.status_code == 200 and response.headers.get("Content-Type") == "application/pdf":
                        with open(file_path, "wb") as f:
                            f.write(response.content)
                        try:
                            os.startfile(file_path)
                        except Exception:
                            pass # Fail silently if auto-open fails
                        return file_path, filename
                except requests.exceptions.RequestException:
                    continue

    return None, None # Should not be reached if years_to_try is populated