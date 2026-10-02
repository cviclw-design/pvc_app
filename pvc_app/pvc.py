# pvc_app/pvc.py

import json
import os
import requests
import math
import re
import time
import urllib.request
from urllib.parse import urljoin
from io import BytesIO
from concurrent.futures import ThreadPoolExecutor, as_completed
from bs4 import BeautifulSoup
from datetime import date, datetime

import pandas as pd
from dateutil.relativedelta import relativedelta
from flask import (
    Blueprint,
    request,
    redirect,
    url_for,
    flash,
    render_template,
    send_file,
    send_from_directory,
    jsonify,
    make_response,
)
from flask_login import login_required, current_user

from . import db
from .models import Item, ItemIndex, PVCResult, TenderMaster, TenderVendor

GST_FACTOR = 1.18

pvc_bp = Blueprint("pvc", __name__)


# ───────────────────────────────────
# Generic helpers
# ───────────────────────────────────

def safe_float(x):
    try:
        return float(str(x or 0).replace(",", "").strip())
    except Exception:
        return 0.0


def safe_round(x, n=2):
    try:
        v = float(x)
        if math.isnan(v) or math.isinf(v):
            return None
        return round(v, n)
    except Exception:
        return None


def format_date_ddmmyyyy(d_str):
    if not d_str:
        return d_str
    try:
        parts = str(d_str).split("T")[0].split("-")
        if len(parts) == 3:
            return f"{parts[2]}-{parts[1]}-{parts[0]}"
    except Exception:
        pass
    return d_str


def nan_to_none(value):
    """
    Convert float NaN / Inf to None (maps to SQL NULL).
    Also replaces NaN / Infinity literals inside JSON strings with null.
    """
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        return None
    if isinstance(value, str):
        return (
            value
            .replace(": NaN",       ": null")
            .replace(":NaN",        ":null")
            .replace(": Infinity",  ": null")
            .replace(":Infinity",   ":null")
            .replace(": -Infinity", ": null")
            .replace(":-Infinity",  ":null")
        )
    return value


def to_month_start(d):
    if not d:
        return pd.NaT
    
    # FAST PATH: Skip expensive pd.to_datetime parsing if it's already a date/Timestamp
    if isinstance(d, pd.Timestamp):
        return pd.Timestamp(d.year, d.month, 1)
    if isinstance(d, date):
        return pd.Timestamp(d.year, d.month, 1)
        
    ts = pd.to_datetime(d, errors="coerce")
    if pd.isna(ts):
        return pd.NaT
    return pd.Timestamp(ts.year, ts.month, 1)


def previous_month(d, n=1):
    d = to_month_start(d)
    return pd.NaT if pd.isna(d) else d - pd.DateOffset(months=n)


# ───────────────────────────────────
# Index access helpers
# ───────────────────────────────────

def get_item_index_df(item):
    rows = (
        ItemIndex.query
        .filter_by(item_id=item.id)
        .order_by(ItemIndex.month.asc())
        .all()
    )
    if not rows:
        return pd.DataFrame()

    data = []
    for r in rows:
        row = {"date": pd.Timestamp(r.month.year, r.month.month, 1)}
        try:
            idx = json.loads(r.indices_json or "{}")
        except Exception:
            idx = {}
        row.update(idx)
        data.append(row)

    df = pd.DataFrame(data)
    if df.empty:
        return df
    return df.set_index("date").sort_index()


def ieema_row(df, dt, previous=False, lag=1):
    """Return the exact index row for the target month."""
    if df is None or df.empty:
        raise ValueError("No index data available for this item.")
        
    target = previous_month(dt, n=lag) if previous else to_month_start(dt)
    if pd.isna(target):
        return None
        
    if target not in df.index:
        month_str = target.strftime('%B %Y')
        raise ValueError(f"Indices for {month_str} not in data set. Please input indices for that required month.")
        
    row = df.loc[target]
    if isinstance(row, pd.DataFrame):
        return row.iloc[0]
    return row


def pvc_percent(base_date, current_date, idx_df, weights, lag=1):
    """Return total PVC % given two dates, an index DataFrame, and a weights dict."""
    base = ieema_row(idx_df, base_date, previous=False)
    curr = ieema_row(idx_df, current_date, previous=True, lag=lag)
    if base is None or curr is None:
        return 0.0

    total = 0.0
    for key, weight in weights.items():
        b = base.get(key)
        c = curr.get(key)
        if b not in (None, 0) and c is not None:
            total += float(weight) * (float(c) - float(b)) / float(b)
    return total


def pvc_percent_detailed(base_date, current_date, idx_df, scenario, weights, lag=1):
    """Return a dict with per-component breakdown of PVC % for one scenario."""
    base = ieema_row(idx_df, base_date, previous=False)
    curr = ieema_row(idx_df, current_date, previous=True, lag=lag)
    if base is None or curr is None:
        return None

    row = {
        "scenario":    scenario,
        "basemonth":   base.name.isoformat() if hasattr(base.name, "isoformat") else str(base.name),
        "currentmonth": curr.name.isoformat() if hasattr(curr.name, "isoformat") else str(curr.name),
        "pvcpercent":  0.0,
    }
    total = 0.0
    base_m_str = base.name.strftime('%b %Y') if hasattr(base.name, 'strftime') else str(base.name)
    curr_m_str = curr.name.strftime('%b %Y') if hasattr(curr.name, 'strftime') else str(curr.name)

    for key, weight in weights.items():
        b = base.get(key)
        c = curr.get(key)
        contrib = None
        if b not in (None, 0) and c is not None:
            contrib = round(float(weight) * (float(c) - float(b)) / float(b), 4)
            total += contrib
        row[f"{key}_base"]            = safe_round(b, 2) if b is not None else None
        row[f"{key}_current"]         = safe_round(c, 2) if c is not None else None
        row[f"{key}_weight"]          = weight
        row[f"{key}_contributionpct"] = contrib
        row[f"{key}_actualbasemonth"] = base_m_str
        row[f"{key}_actualcurrentmonth"] = curr_m_str
    row["pvcpercent"] = round(total, 4)
    return row


# ───────────────────────────────────
# IGBT-specific helpers
# ───────────────────────────────────

def igbt_index_values(idx_df, base_month, ref_month, currency):
    """
    Return (baseidx, curridx) dicts for the IGBT formula.

    Base  : all indices from base_month.
    Current:
      - C / AL / FE / IM / W / D  from  ref_month − 1
      - ER                         from  ref_month − 3
    """
    currency = (currency or "EUR").upper().strip()
    ercol = f"ER_{currency}"

    base_row = ieema_row(idx_df, base_month, previous=False)
    row_1m   = ieema_row(idx_df, previous_month(ref_month, 1), previous=False)
    row_3m   = ieema_row(idx_df, previous_month(ref_month, 3), previous=False)

    if base_row is None or row_1m is None or row_3m is None:
        raise ValueError("IGBT indices missing for one or more required months.")

    def get_val(row, col):
        v = row.get(col)
        if v is None:
            raise ValueError(f"Index column '{col}' is missing in the data.")
        return float(v)

    baseidx = {
        "C":  get_val(base_row, "C"),
        "AL": get_val(base_row, "AL"),
        "FE": get_val(base_row, "FE"),
        "IM": get_val(base_row, "IM"),
        "W":  get_val(base_row, "W"),
        "D":  get_val(base_row, "D"),
        "ER": get_val(base_row, ercol),
    }
    curridx = {
        "C":  get_val(row_1m, "C"),
        "AL": get_val(row_1m, "AL"),
        "FE": get_val(row_1m, "FE"),
        "IM": get_val(row_1m, "IM"),
        "W":  get_val(row_1m, "W"),
        "D":  get_val(row_1m, "D"),
        "ER": get_val(row_3m, ercol),   # ER uses 3-month lag
    }

    basedates = {col: base_row.name for col in ["C", "AL", "FE", "IM", "W", "D", "ER"]}
    currdates = {
        "C": row_1m.name, "AL": row_1m.name, "FE": row_1m.name,
        "IM": row_1m.name, "W": row_1m.name, "D": row_1m.name,
        "ER": row_3m.name
    }
    return baseidx, curridx, basedates, currdates


def igbt_p1_indigenous(baseidx, curridx, weights, indigenous_portion):
    """Compute P1 (indigenous component) PVC amount."""
    Fc  = float(weights.get("FIXED", 16))
    wC  = float(weights.get("C",  26))
    wAL = float(weights.get("AL", 13))
    wFE = float(weights.get("FE", 18))
    wIM = float(weights.get("IM",  9))
    wW  = float(weights.get("W",  18))

    factor = (
        Fc
        + wC  * curridx["C"]  / baseidx["C"]
        + wAL * curridx["AL"] / baseidx["AL"]
        + wFE * curridx["FE"] / baseidx["FE"]
        + wIM * curridx["IM"] / baseidx["IM"]
        + wW  * curridx["W"]  / baseidx["W"]
    )
    return indigenous_portion * (factor / 100.0) - indigenous_portion


def igbt_p2_cif(cif, baseidx, curridx):
    """Compute P2 (CIF / imported component) PVC amount."""
    ER0 = baseidx["ER"]
    ER1 = curridx["ER"]
    D0  = baseidx["D"]
    D1  = curridx["D"]
    return cif * (
        (ER1 * (100 + D1) / (100 * ER0))
        - ((100 + D0) / 100)
    )


def igbt_vendor_scenario(rate, vendor, base_month, ref_month, idx_df, weights, scenario_label, basic_rate_for_pvc=None, orig_base_month=None, cif_override=None):
    """
    Compute P1 / P2 and total PVC for one vendor in one scenario.
    Returns (summary_dict, indexdetails_list, total_pvc).

    BUG FIX: The ER index-detail lookup previously used `f"ER_{currency}"` as a key
    into baseidx/curridx, but those dicts store the ER value under the plain "ER" key.
    Now we always look up "ER" in those dicts while still labelling the row correctly.
    """
    if basic_rate_for_pvc is None:
        basic_rate_for_pvc = rate
    if orig_base_month is None:
        orig_base_month = base_month

    currency = (vendor.currency or "EUR").upper()
    baseidx, curridx, basedates, currdates = igbt_index_values(idx_df, base_month, ref_month, currency)

    # Fetch origidx for P2 (Fixed imported components must use original tender exchange rate)
    if orig_base_month != base_month:
        origidx, _, origdates, _ = igbt_index_values(idx_df, orig_base_month, ref_month, currency)
    else:
        origidx = baseidx
        origdates = basedates

    cif = safe_float(vendor.cif)
    if cif_override is not None:
        cif = safe_float(cif_override)

    baseduty          = cif * origidx["D"] / 100
    indigenous_portion = basic_rate_for_pvc - cif - baseduty

    p1        = igbt_p1_indigenous(baseidx, curridx, weights, indigenous_portion)
    p2        = igbt_p2_cif(cif, origidx, curridx)
    total_pvc = p1 + p2

    base_month_str = str(base_month.date()) if hasattr(base_month, "date") else str(base_month)
    curr_month_str = str(ref_month.date())  if hasattr(ref_month,  "date") else str(ref_month)
    orig_month_str = str(orig_base_month.date()) if hasattr(orig_base_month, "date") else str(orig_base_month)

    summary = {
        "scenario":     scenario_label,
        "vendor":       vendor.vendor_name,
        "pono":         vendor.po_no,
        "currency":     currency,
        "rate":         rate,
        "basicrate":    basic_rate_for_pvc,
        "cif":          cif,
        "baseduty":     baseduty,
        "indigenous":   indigenous_portion,
        "p1":           p1,
        "p2":           p2,
        "totalpvc":     total_pvc,
        "basemonth":    base_month_str,
        "currentmonth": curr_month_str,
    }

    # Index-detail rows (used in result.html IGBT tabs).
    # baseidx/curridx always use the plain key ("ER"); the label carries currency.
    index_columns = [
        ("C",  "Copper Index", False),
        ("AL", "Aluminium Index", False),
        ("FE", "Iron / Steel Index", False),
        ("IM", "Import Machine Index", False),
        ("W",  "Labour / Wage Index", False),
        ("D",  "Import Duty", True),
        ("ER", f"Exchange Rate {currency}", True),
    ]
    indexdetails = []
    for col, label, use_orig in index_columns:
        if use_orig:
            b_val = origidx.get(col)
            b_date = origdates[col]
            b_month_str = orig_month_str
        else:
            b_val = baseidx.get(col)
            b_date = basedates[col]
            b_month_str = base_month_str

        indexdetails.append({
            "scenario":     scenario_label,
            "vendor":       vendor.vendor_name,
            "parameter":    col if col != "ER" else f"ER_{currency}",
            "label":        label,
            "basemonth":    b_month_str,
            "actualbasemonth": b_date.strftime('%b %Y') if hasattr(b_date, 'strftime') else str(b_date),
            "basevalue":    safe_round(b_val, 4),
            "currentmonth": curr_month_str,
            "actualcurrentmonth": currdates[col].strftime('%b %Y') if hasattr(currdates[col], 'strftime') else str(currdates[col]),
            "currentvalue": safe_round(curridx.get(col), 4),
        })

    return summary, indexdetails, total_pvc


# ───────────────────────────────────
# IGBT Propulsion System calculation
# ───────────────────────────────────

def calculate_igbt_propulsion(item, data, tender_id, idx_df, weights):
    """
    Full IGBT Propulsion System PVC calculation.

    Fixes applied vs. original:
      1. `lower_base_date` was undefined — replaced with `lower_base` throughout.
      2. `scheduled_date`  was undefined — replaced with `scheduleddp`.
      3. `pvcd1` was never assigned — now captured from run_scenario("D1", ...).
      4. `qty`  was undefined — replaced with `quantity`.
      5. `lowercontractual` was computed in the special-case block then
         unconditionally overwritten — removed the duplicate assignment.
    """
    tender = TenderMaster.query.get_or_404(int(tender_id))
    try:
        tender_id_int = int(float(tender_id))
    except (ValueError, TypeError):
        raise ValueError(f"Invalid Tender ID format: {tender_id}")
    tender = TenderMaster.query.get_or_404(tender_id_int)
    vendors = (
        TenderVendor.query
        .filter_by(tender_id=tender.id)
        .order_by(TenderVendor.id.asc())
        .all()
    )
    if not vendors:
        raise ValueError("No vendor rows configured for the selected tender.")

    selected_vendor_pono = None
    vendor_id = data.get("vendorid")
    if vendor_id:
        sv = next((v for v in vendors if str(v.id) == str(vendor_id)), None)
        if sv:
            selected_vendor_pono = sv.po_no

    if not selected_vendor_pono and vendors:
        selected_vendor_pono = vendors[0].po_no

    # ── Merge form data with tender defaults ────────────────────────────────
    basic_rate        = safe_float(data.get("basicrate")           or tender.basicrate)
    pvc_base_date     = data.get("pvcbasedate")                    or tender.pvcbasedate
    freight_per_unit  = safe_float(data.get("freightrateperunit")  or 0)
    quantity          = safe_float(data.get("quantity")            or 1)
    if quantity <= 0:
        quantity = 1.0

    # FIX: Write fallback values back to `data` so they are saved to the DB correctly.
    # Otherwise, `PVCResult` saves basicrate as 0, causing reports to only show PVC + GST.
    data["basicrate"]          = basic_rate
    data["pvcbasedate"]        = pvc_base_date
    data["freightrateperunit"] = freight_per_unit
    data["quantity"]           = quantity

    lower_rate        = safe_float(data.get("lowerrate") or 0)
    lower_base        = data.get("lowerbasicdate")
    lower_freight     = safe_float(data.get("lowerfreight") or 0)

    cal_date      = data.get("caldate")
    origdp        = data.get("origdp")
    refixeddp     = data.get("refixeddp")
    extendeddp    = data.get("extendeddp")
    supply_date   = data.get("supdate")
    rate_applied  = (data.get("rateapplied") or "").strip().lower()
    developmental = bool(data.get("developmentaltender"))

    b2_needed     = rate_applied not in ("supply before due date", "supply within original dp or refixed dp", "lower rate applicable within dp")

    # "Scheduled DP" — Extended is only for LD. PVC uses Refixed or Original.
    pvc_scheduleddp = refixeddp or origdp
    ld_scheduleddp  = extendeddp or refixeddp or origdp

    schedts  = pd.to_datetime(ld_scheduleddp,  errors="coerce")
    supplyts = pd.to_datetime(supply_date,   errors="coerce")

    basemonth        = to_month_start(pvc_base_date)
    calmonth         = to_month_start(cal_date)
    schedmonth       = to_month_start(pvc_scheduleddp)

    subsequent_tender_id = data.get("subsequent_tenderid")
    subsequent_vendors_map = {}
    if subsequent_tender_id:
        subsequent_tender = TenderMaster.query.get(int(subsequent_tender_id))
        if subsequent_tender:
            lower_rate        = safe_float(subsequent_tender.basicrate)
            lower_base        = subsequent_tender.pvcbasedate
            
            data["lowerrate"]      = lower_rate
            data["lowerbasicdate"] = lower_base
            
            sub_vendors = TenderVendor.query.filter_by(tender_id=subsequent_tender.id).all()
            for sv in sub_vendors:
                subsequent_vendors_map[sv.vendor_name] = sv

    lowerbase_month  = to_month_start(lower_base) if lower_base else None

    lowerrate_applicable = lower_rate > 0 and lowerbase_month is not None

    freight_total       = freight_per_unit * quantity
    lowerfreight_total  = lower_freight    * quantity

    # ── LD calculation ──────────────────────────────────────────────────────
    delaydays    = 0
    ldweeks      = 0
    ldratepct    = 0.0
    ldapplicable = False
    if pd.notna(schedts) and pd.notna(supplyts) and supplyts > schedts:
        delaydays    = int((supplyts - schedts).days)
        ldweeks      = math.ceil(delaydays / 7) if delaydays > 0 else 0
        ldratepct    = min(ldweeks * 0.5, 10.0)
        ldapplicable = True

    if developmental:
        ldapplicable = False
        ldratepct    = 0.0
        ldweeks      = 0

    # ── Run scenarios (returns min PVC per set across vendors) ───────────────
    all_vendor_summaries: list = []
    all_index_details:    list = []

    def run_scenario(label, rate, base_m, ref_m, basic_rate_for_pvc=None, orig_base_m=None):
        if not base_m or pd.isna(base_m):
            return None
        if not ref_m or pd.isna(ref_m):
            return None
        if basic_rate_for_pvc is None:
            basic_rate_for_pvc = rate
        if orig_base_m is None:
            orig_base_m = base_m
            
        sc_summaries, sc_indices = [], []
        
        vendors_to_run = list(vendors)
        if label in ("C1", "D1") and subsequent_vendors_map:
            existing_names = {v.vendor_name for v in vendors}
            for sv_name, sv in subsequent_vendors_map.items():
                if sv_name not in existing_names:
                    vendors_to_run.append(sv)

        for v in vendors_to_run:
            cif_to_use = safe_float(v.cif)
            if label in ("C1", "D1") and v.vendor_name in subsequent_vendors_map:
                cif_to_use = safe_float(subsequent_vendors_map[v.vendor_name].cif)
                
            s, idxrows, _ = igbt_vendor_scenario(
                rate, v, base_m, ref_m, idx_df, weights, label, basic_rate_for_pvc, orig_base_m, cif_override=cif_to_use
            )
            sc_summaries.append(s)
            sc_indices.extend(idxrows)
        all_vendor_summaries.extend(sc_summaries)
        all_index_details.extend(sc_indices)
        if not sc_summaries:
            return None
        valid = [s["totalpvc"] for s in sc_summaries if s["totalpvc"] is not None]
        return min(valid) if valid else None

    pvca2 = run_scenario("A2", basic_rate, basemonth,       calmonth,       basic_rate, basemonth)
    pvcb2 = run_scenario("B2", basic_rate, basemonth,       schedmonth,     basic_rate, basemonth) if b2_needed else None
    pvcc1 = run_scenario("C1", lower_rate, lowerbase_month, calmonth,       lower_rate, lowerbase_month)   if lowerrate_applicable else None
    
    d1_applicable = lowerrate_applicable
    if lowerrate_applicable:
        lbd_ts = pd.to_datetime(lower_base, errors="coerce")
        odp_ts = pd.to_datetime(origdp, errors="coerce")
        if pd.notna(lbd_ts) and pd.notna(odp_ts) and lbd_ts > odp_ts:
            d1_applicable = False

    pvcd1 = None
    if d1_applicable and b2_needed:
        pvcd1 = run_scenario("D1", lower_rate, lowerbase_month, schedmonth, lower_rate, lowerbase_month)

    # ── Amount calculation helper ────────────────────────────────────────────
    def with_freight_gst(base_rate, pvc_per_set, qty, frt):
        """(base_rate × qty + pvc_per_set × qty + freight) × GST"""
        if pvc_per_set is None:
            return None
        return (base_rate * qty + pvc_per_set * qty + frt) * GST_FACTOR

    pvcactual      = with_freight_gst(basic_rate, pvca2, quantity, freight_total)
    pvccontractual = with_freight_gst(basic_rate, pvcb2, quantity, freight_total)

    # ── LD deductions ────────────────────────────────────────────────────────
    ldamtactual      = (max(pvcactual,      0) * (ldratepct / 100.0)) if (ldapplicable and pvcactual      is not None) else 0.0
    ldamtcontractual = (max(pvccontractual, 0) * (ldratepct / 100.0)) if (ldapplicable and pvccontractual is not None) else 0.0

    pvcactuallessld      = (pvcactual      - ldamtactual)      if pvcactual      is not None else None
    pvccontractuallessld = (pvccontractual - ldamtcontractual) if pvccontractual is not None else None

    loweractual      = None
    lowercontractual = None
    if lowerrate_applicable:
        loweractual = with_freight_gst(lower_rate, pvcc1, quantity, lowerfreight_total)
    if d1_applicable:
        lowercontractual = with_freight_gst(lower_rate, pvcd1, quantity, lowerfreight_total)

    loweractuallessld = (loweractual - ldamtactual) if loweractual is not None else None
    lowercontractuallessld = (lowercontractual - ldamtcontractual) if lowercontractual is not None else None

    # ── Scenario selection ───────────────────────────────────────────────────
    candidates = _build_candidates(
        rate_applied,
        pvcactual, pvccontractual,
        pvcactuallessld, pvccontractuallessld,
        loweractual, lowercontractual,
        loweractuallessld, lowercontractuallessld,
        lowerrate_applicable,
    )
    candidates  = {k: v for k, v in candidates.items() if v is not None}
    selected    = min(candidates, key=candidates.get) if candidates else "A2"
    fairprice   = candidates.get(selected, pvcactual)

    scenarioamounts = {"A2": safe_round(pvcactuallessld)}
    if b2_needed:
        scenarioamounts["B2"] = safe_round(pvccontractuallessld)
    if lowerrate_applicable:
        scenarioamounts["C1"] = safe_round(loweractual)
    if d1_applicable and b2_needed:
        scenarioamounts["D1"] = safe_round(lowercontractual)

    # ── Scenario index details ───────────────────────────────────────────────
    scenariodetails: list = []
    ieema_scenarios = [
        ("A2", pvc_base_date, cal_date),
    ]
    if b2_needed:
        ieema_scenarios.append(("B2", pvc_base_date, pvc_scheduleddp))
    if lowerrate_applicable:
        ieema_scenarios.append(("C1", lower_base, cal_date))
    if d1_applicable and b2_needed:
        ieema_scenarios.append(("D1", lower_base, pvc_scheduleddp))
    for sc, bd, cd in ieema_scenarios:
        if bd and cd:
            det = pvc_percent_detailed(bd, cd, idx_df, sc, weights)
            if det:
                scenariodetails.append(det)

    return {
        "pvcactual":               safe_round(pvcactual),
        "pvccontractual":          safe_round(pvccontractual),
        "loweractual":             safe_round(loweractual)            if lowerrate_applicable else None,
        "lowercontractual":        safe_round(lowercontractual)       if lowerrate_applicable else None,
        "delaydays":               delaydays,
        "ldweeks":                 ldweeks,
        "ldratepct":               safe_round(ldratepct),
        "ldapplicable":            ldapplicable,
        "ldamtactual":             safe_round(ldamtactual),
        "ldamtcontractual":        safe_round(ldamtcontractual),
        "pvcactuallessldnew":      safe_round(pvcactuallessld),
        "pvccontractuallessldnew": safe_round(pvccontractuallessld),
        "loweractuallessld":       safe_round(loweractuallessld)      if loweractuallessld      is not None else None,
        "lowercontractuallessld":  safe_round(lowercontractuallessld) if lowercontractuallessld is not None else None,
        "fairpricenew":            safe_round(fairprice),
        "selectedscenarionew":     selected,
        "pvcperseta2":             safe_round(pvca2),
        "pvcpersetb2":             safe_round(pvcb2),
        "pvcpersetc1":             safe_round(pvcc1) if pvcc1 is not None else None,
        "pvcpersetd1":             safe_round(pvcd1) if pvcd1 is not None else None,
        "scenarioamounts":         scenarioamounts,
        "scenariodetails":         scenariodetails,
        "igbtvendordetails":       all_vendor_summaries,
        "igbtindexdetails":        all_index_details,
        "tenderno":                tender.tender_no,
        "pono":                    data.get("pono"),
    }


# ───────────────────────────────────
# IEEMA single-record calculation
# ───────────────────────────────────

def calc_single_record(data, idx_df, weights, item_code=""):
    """
    Standard IEEMA PVC calculation (non-IGBT items).

    Fix: `lowercontractual` was computed correctly inside the D1 special-case
    block but then unconditionally overwritten by a plain formula a few lines
    below.  The plain formula now only runs in the normal (else) branch.
    """
    lag = 2 if item_code == "TRACTIONMOTOR" else 1

    pvc_base_date   = data.get("pvcbasedate")
    cal_date        = data.get("caldate")
    origdp          = data.get("origdp")
    refixeddp       = data.get("refixeddp")
    extendeddp      = data.get("extendeddp")
    supply_date     = data.get("supdate")
    lower_base_date = data.get("lowerbasicdate")

    pvc_scheduled_date = refixeddp or origdp
    ld_scheduled_date  = extendeddp or refixeddp or origdp

    qty               = safe_float(data.get("quantity"))
    if qty <= 0:
        qty = 1.0
        data["quantity"] = 1.0
    basic_rate        = safe_float(data.get("basicrate"))
    freight_per_unit  = safe_float(data.get("freightrateperunit"))
    lower_rate        = safe_float(data.get("lowerrate"))
    lower_freight     = safe_float(data.get("lowerfreight"))
    developmental     = bool(data.get("developmentaltender"))
    rate_applied      = (data.get("rateapplied") or "").strip().lower()

    b2_needed         = rate_applied not in ("supply before due date", "supply within original dp or refixed dp", "lower rate applicable within dp")

    freight_total      = freight_per_unit * qty
    lowerfreight_total = lower_freight    * qty

    # Trust the 'rateapplied' field if it indicates a lower rate scenario.
    # This ensures bulk calculations, which now correctly set this field, are processed properly.
    is_lower_rate_scenario = "lower rate" in rate_applied
    lowerrate_applicable = is_lower_rate_scenario or (lower_rate > 0 and bool(lower_base_date))
    lowerrate_applicable = lower_rate > 0 and bool(lower_base_date)

    # ── Scenario percentages ────────────────────────────────────────────────
    pct_a2 = pvc_percent(pvc_base_date,   cal_date,       idx_df, weights, lag=lag)
    pct_b2 = pvc_percent(pvc_base_date,   pvc_scheduled_date, idx_df, weights, lag=lag) if b2_needed else None
    pct_c1 = pvc_percent(lower_base_date, cal_date,       idx_df, weights, lag=lag) if lowerrate_applicable else None

    pct_d1 = None
    lowercontractual = None
    pvcpersetd1 = None

    d1_applicable = lowerrate_applicable
    if lowerrate_applicable:
        lbd_ts = pd.to_datetime(lower_base_date, errors="coerce")
        odp_ts = pd.to_datetime(origdp, errors="coerce")
        if pd.notna(lbd_ts) and pd.notna(odp_ts) and lbd_ts > odp_ts:
            d1_applicable = False

    if d1_applicable and b2_needed:
        pct_d1 = pvc_percent(lower_base_date, pvc_scheduled_date, idx_df, weights, lag=lag)
        pvcpersetd1 = lower_rate * (pct_d1 or 0) / 100.0
        lowercontractual = (lower_rate * qty + pvcpersetd1 * qty + lowerfreight_total) * GST_FACTOR

    # ── Gross amounts & LD ───────────────────────────────────────────────────
    pvcactual = (
        basic_rate * qty * (1 + pct_a2 / 100.0) + freight_total
    ) * GST_FACTOR if pct_a2 is not None else None

    pvccontractual = (
        basic_rate * qty * (1 + pct_b2 / 100.0) + freight_total
    ) * GST_FACTOR if pct_b2 is not None else None

    delaydays    = 0
    ldweeks      = 0
    ldratepct    = 0.0
    ldapplicable = False
    duets    = pd.to_datetime(ld_scheduled_date, errors="coerce")
    supplyts = pd.to_datetime(supply_date,    errors="coerce")
    if pd.notna(duets) and pd.notna(supplyts) and supplyts > duets:
        delaydays    = int((supplyts - duets).days)
        ldweeks      = math.ceil(delaydays / 7) if delaydays > 0 else 0
        ldratepct    = min(ldweeks * 0.5, 10.0)
        ldapplicable = True

    if developmental:
        ldapplicable = False
        ldratepct    = 0.0
        ldweeks      = 0

    ldamtactual      = pvcactual      * (ldratepct / 100.0) if (ldapplicable and pvcactual is not None) else 0.0
    ldamtcontractual = pvccontractual * (ldratepct / 100.0) if (ldapplicable and pvccontractual is not None) else 0.0

    pvcactuallessld      = (pvcactual      - ldamtactual)      if pvcactual      is not None else None
    pvccontractuallessld = (pvccontractual - ldamtcontractual) if pvccontractual is not None else None

    loweractual = None
    pvcpersetc1 = None
    if lowerrate_applicable:
        pvcpersetc1 = lower_rate * (pct_c1 or 0) / 100.0
        loweractual = (
            lower_rate * qty + pvcpersetc1 * qty + lowerfreight_total
        ) * GST_FACTOR

    loweractuallessld = (loweractual - ldamtactual) if loweractual is not None else None
    lowercontractuallessld = (lowercontractual - ldamtcontractual) if lowercontractual is not None else None

    # ── Scenario selection ───────────────────────────────────────────────────
    candidates = _build_candidates(
        rate_applied,
        pvcactual, pvccontractual,
        pvcactuallessld, pvccontractuallessld,
        loweractual, lowercontractual,
        loweractuallessld, lowercontractuallessld,
        lowerrate_applicable,
    )
    candidates  = {k: v for k, v in candidates.items() if v is not None}
    selected    = min(candidates, key=candidates.get) if candidates else "A2"
    fairprice   = candidates.get(selected, pvcactual)

    # FIX: scenarioamounts now consistently uses the less-LD values for A2/B2
    # and gross values for C1/D1 (matching the original intent — LD is only
    # deducted from the basic-rate scenarios A2/B2, not from lower-rate ones).
    scenarioamounts = {
        "A2": safe_round(pvcactuallessld),
    }
    if b2_needed:
        scenarioamounts["B2"] = safe_round(pvccontractuallessld)
    if lowerrate_applicable:
        scenarioamounts["C1"] = safe_round(loweractual)
    if d1_applicable and b2_needed:
        scenarioamounts["D1"] = safe_round(lowercontractual)

    scenariodetails: list = []
    _sc_list = [
        ("A2", pvc_base_date,   cal_date),
    ]
    if b2_needed:
        _sc_list.append(("B2", pvc_base_date,   pvc_scheduled_date))
    if lowerrate_applicable:
        _sc_list.append(("C1", lower_base_date, cal_date))
    if d1_applicable and b2_needed:
        _sc_list.append(("D1", lower_base_date, pvc_scheduled_date))
    for sc, bd, cd in _sc_list:
        if bd and cd:
            det = pvc_percent_detailed(bd, cd, idx_df, sc, weights, lag=lag)
            if det:
                scenariodetails.append(det)

    return {
        "pvcactual":               safe_round(pvcactual),
        "pvccontractual":          safe_round(pvccontractual),
        "loweractual":             safe_round(loweractual)      if loweractual      is not None else None,
        "lowercontractual":        safe_round(lowercontractual) if lowercontractual is not None else None,
        "delaydays":               delaydays,
        "ldweeks":                 ldweeks,
        "ldratepct":               safe_round(ldratepct),
        "ldapplicable":            ldapplicable,
        "ldamtactual":             safe_round(ldamtactual),
        "ldamtcontractual":        safe_round(ldamtcontractual),
        "pvcactuallessldnew":      safe_round(pvcactuallessld),
        "pvccontractuallessldnew": safe_round(pvccontractuallessld),
        "loweractuallessld":       safe_round(loweractuallessld)      if loweractuallessld      is not None else None,
        "lowercontractuallessld":  safe_round(lowercontractuallessld) if lowercontractuallessld is not None else None,
        "fairpricenew":            safe_round(fairprice),
        "selectedscenarionew":     selected,
        "pvcperseta2":             safe_round(basic_rate * pct_a2 / 100.0) if pct_a2 is not None else None,
        "pvcpersetb2":             safe_round(basic_rate * pct_b2 / 100.0) if pct_b2 is not None else None,
        "pvcpersetc1":             safe_round(pvcpersetc1) if lowerrate_applicable else None,
        "pvcpersetd1":             safe_round(pvcpersetd1) if pvcpersetd1 is not None else None,
        "scenarioamounts":         scenarioamounts,
        "scenariodetails":         scenariodetails,
        "igbtvendordetails":       [],
        "igbtindexdetails":        [],
        "tenderno":                None,
        "pono":                    data.get("pono"),
    }


# ───────────────────────────────────
# Shared scenario-selection helper
# ───────────────────────────────────

def _build_candidates(
    rate_applied,
    pvcactual, pvccontractual,
    pvcactuallessld, pvccontractuallessld,
    loweractual, lowercontractual,
    loweractuallessld, lowercontractuallessld,
    lowerrate_applicable,
):
    """
    Return the candidate dict for fair-price selection based on `rate_applied`.
    Extracted to avoid duplication between calc_single_record and
    calculate_igbt_propulsion.
    """
    if rate_applied in ("supply before due date", "supply within original dp or refixed dp"):
        return {"A2": pvcactual}

    if rate_applied == "lower rate applicable within dp":
        d = {"A2": pvcactual}
        if lowerrate_applicable:
            d["C1"] = loweractual
        return d

    if rate_applied in ("supply after due date", "supply beyond original dp or refixed dp"):
        return {"A2": pvcactuallessld, "B2": pvccontractuallessld}

    if rate_applied in ("lower rate applicable", "lower rate applicable beyond dp without ld"):
        d = {"A2": pvcactual, "B2": pvccontractual}
        if lowerrate_applicable:
            d["C1"] = loweractual
            d["D1"] = lowercontractual
        return d

    if rate_applied in ("lower rate and ld comparison", "lower rate applicable beyond dp with ld"):
        d = {"A2": pvcactuallessld, "B2": pvccontractuallessld}
        if lowerrate_applicable:
            d["C1"] = loweractual
            d["D1"] = lowercontractual
        return d

    if rate_applied in ("lower rate with ld in further extension", "lower rate applicable in further dp extension with ld"):
        d = {"A2": pvcactuallessld, "B2": pvccontractuallessld}
        if lowerrate_applicable:
            d["C1"] = loweractuallessld
            d["D1"] = lowercontractuallessld
        return d

    # Default: all applicable scenarios
    d = {"A2": pvcactual, "B2": pvccontractual}
    if lowerrate_applicable:
        d["C1"] = loweractual
        d["D1"] = lowercontractual
    return d


# ───────────────────────────────────
# Dispatcher
# ───────────────────────────────────

def calculate_for_item(item, data, idx_df, weights):
    code = (item.pvc_formula_code or "").replace(" ", "").upper()
    if code == "IGBTPROPULSIONSYSTEM":
        tender_id = data.get("tenderid")
        if not tender_id:
            raise ValueError("Tender is required for IGBT Propulsion System.")
        return calculate_igbt_propulsion(item, data, tender_id, idx_df, weights)
    return calc_single_record(data, idx_df, weights, item_code=code)


# ───────────────────────────────────
# PVC input + result wrappers
# ───────────────────────────────────

class PVCInput:
    _KNOWN = {
        "itemid", "item_id", "basicrate", "quantity", "freightrateperunit",
        "pvcbasedate", "origdp", "refixeddp", "extendeddp", "caldate", "supdate",
        "lowerrate", "lowerfreight", "lowerbasicdate", "rateapplied",
        "tenderid", "tender_id", "vendorid", "vendor_id", "developmentaltender",
    }

    def __init__(self, form):
        self.user_id               = None
        self.username              = None
        self.item_id               = int(form.get("itemid") or form.get("item_id") or 0)
        self.basic_rate            = safe_float(form.get("basicrate"))
        self.quantity              = safe_float(form.get("quantity"))
        self.freight_rate_per_unit = safe_float(form.get("freightrateperunit"))
        self.pvc_base_date         = form.get("pvcbasedate")
        self.original_dp           = form.get("origdp")
        self.refixed_dp            = form.get("refixeddp")
        self.extended_dp           = form.get("extendeddp")
        self.cal_date              = form.get("caldate")
        self.supply_date           = form.get("supdate")
        self.lower_rate            = safe_float(form.get("lowerrate"))
        self.lower_freight         = safe_float(form.get("lowerfreight"))
        self.lower_basic_date      = form.get("lowerbasicdate")
        self.rate_applied          = form.get("rateapplied")
        self.tender_id             = form.get("tenderid") or form.get("tender_id")
        self.vendor_id             = form.get("vendorid") or form.get("vendor_id")
        self.developmental_tender  = bool(form.get("developmentaltender"))
        self.subsequent_tenderid   = form.get("subsequent_tenderid")
        self.extra_data = {
            k: form.get(k)
            for k in form.keys()
            if k not in self._KNOWN
        }

    def to_dict(self):
        d = {
            "basicrate":           self.basic_rate,
            "quantity":            self.quantity,
            "freightrateperunit":  self.freight_rate_per_unit,
            "pvcbasedate":         self.pvc_base_date,
            "origdp":              self.original_dp,
            "refixeddp":           self.refixed_dp,
            "extendeddp":          self.extended_dp,
            "caldate":             self.cal_date,
            "supdate":             self.supply_date,
            "lowerrate":           self.lower_rate,
            "lowerfreight":        self.lower_freight,
            "lowerbasicdate":      self.lower_basic_date,
            "rateapplied":         self.rate_applied,
            "tenderid":            self.tender_id,
            "vendorid":            self.vendor_id,
            "developmentaltender": self.developmental_tender,
            "subsequent_tenderid": self.subsequent_tenderid,
        }
        d.update(self.extra_data)
        return d


class ResultObj:
    def __init__(self, payload):
        self.data              = payload
        self.scenarioamounts   = payload.get("scenarioamounts",   {})
        self.scenariodetails   = payload.get("scenariodetails",   [])
        self.igbtvendordetails = payload.get("igbtvendordetails", [])
        self.igbtindexdetails  = payload.get("igbtindexdetails",  [])


# ───────────────────────────────────
# Routes
# ───────────────────────────────────

@pvc_bp.route("/calculator")
@login_required
def calculator():
    items = Item.query.order_by(Item.name.asc()).all()
    tenders = TenderMaster.query.order_by(TenderMaster.tender_no.asc()).all()
    resp = make_response(render_template("index.html", items=items, tenders=tenders))
    resp.set_cookie("last_calc_id", "", expires=0)
    return resp

@pvc_bp.route("/bulk_calculator")
@login_required
def bulk_calculator():
    items = Item.query.order_by(Item.name.asc()).all()
    return render_template("bulk_pvc_calculator.html", items=items)

@pvc_bp.route("/bulk_calculate", methods=["POST"])
@login_required
def bulk_calculate():
    data_file = request.files.get("data_file")

    if not data_file:
        flash("A data file is required.", "danger")
        return redirect(url_for("pvc.bulk_calculator"))

    try:
        input_df = pd.read_excel(data_file)
        input_df.columns = [c.lower().replace(" ", "") for c in input_df.columns]
    except Exception as e:
        flash(f"Error reading the data file: {e}", "danger")
        return redirect(url_for("pvc.bulk_calculator"))

    if 'tenderno' not in input_df.columns:
        flash("The uploaded Excel file must contain a 'tenderno' column.", "danger")
        return redirect(url_for("pvc.bulk_calculator"))

    # Pre-fetch all tenders and vendors to reduce database queries inside the loop
    tenders_map = {t.tender_no: t for t in TenderMaster.query.all()}
    vendors_map = {}
    for v in TenderVendor.query.all():
        vendors_map.setdefault(v.tender_id, []).append(v)

    results = []
    for index, row in input_df.iterrows():
        data = row.to_dict()
        # Convert date columns from Excel
        for date_col in ['pvcbasedate', 'origdp', 'refixeddp', 'extendeddp', 'caldate', 'supdate', 'lowerbasicdate']:
            if date_col in data and pd.notna(data[date_col]):
                if isinstance(data[date_col], datetime):
                    data[date_col] = data[date_col].strftime('%Y-%m-%d')

        try:
            tender_no = data.get('tenderno')
            if not tender_no:
                raise ValueError("Row is missing a 'tenderno'.")

            tender = tenders_map.get(str(tender_no).strip())
            if not tender:
                raise ValueError(f"Tender No '{tender_no}' not found in the database.")

            item = tender.item
            if not item:
                raise ValueError(f"Tender '{tender_no}' is not linked to a valid item.")

            data['tenderid'] = tender.id

            # If pono is provided, find the specific vendor and set vendorid
            pono = data.get('pono')
            if pono:
                pono_str = str(pono).strip()
                tender_vendors = vendors_map.get(tender.id, [])
                matching_vendor = next((v for v in tender_vendors if v.po_no == pono_str), None)

                if matching_vendor:
                    data['vendorid'] = matching_vendor.id
                    data['vendorname'] = matching_vendor.vendor_name
            
            # --- Data Enrichment and Scenario Detection ---

            # Step 1: Check if a lower rate is applicable and enrich data if so.
            # This must happen BEFORE rateapplied detection.
            is_lower_rate_applicable = str(data.get('lowerrateapplicable', '')).lower() in ('1', 'true', 'yes')
            if is_lower_rate_applicable:
                data['lowerrateapplicable'] = True # Set a clean boolean flag for downstream functions
            # If lower rate is applicable, find subsequent_tenderid from subsequent_tenderno
            # The 'lowerrateapplicable' column can be 1, "1", "true", "TRUE", etc.
            is_lower_applicable = str(data.get('lowerrateapplicable', '')).lower() in ('1', 'true', 'yes')
            if is_lower_applicable:
                subsequent_tenderno = data.get('subsequenttenderno')
                if subsequent_tenderno:
                    subsequent_tender = tenders_map.get(str(subsequent_tenderno).strip())
                    if subsequent_tender:
                        data['subsequent_tenderid'] = subsequent_tender.id
                        data['lowerrateapplicable'] = True # Explicitly set for downstream functions
                        # Also populate the lower rate fields for the calculation function
                        if 'lowerrate' not in data or pd.isna(data['lowerrate']):
                            data['lowerrate'] = subsequent_tender.basicrate
                        if 'lowerbasicdate' not in data or pd.isna(data['lowerbasicdate']):
                            data['lowerbasicdate'] = subsequent_tender.pvcbasedate
                        

                        # For IGBT items, we also need to get the freight from the matching vendor in the subsequent tender.
                        is_igbt = (item.pvc_formula_code or "").replace(" ", "").upper() == "IGBTPROPULSIONSYSTEM"
                        if is_igbt and data.get('vendorname'):
                            sub_vendors = vendors_map.get(subsequent_tender.id, [])
                            sub_vendor = next((v for v in sub_vendors if v.vendor_name == data['vendorname']), None)
                            if sub_vendor and ('lowerfreight' not in data or pd.isna(data['lowerfreight'])):
                                data['lowerfreight'] = sub_vendor.freightrateperunit

            # Step 2: Now that data is enriched, auto-detect the 'rateapplied' scenario if it's missing.
            
            # --- Auto-detect rateapplied if not provided ---
            if not data.get('rateapplied') or pd.isna(data.get('rateapplied')):
                supdate = pd.to_datetime(data.get('supdate'), errors='coerce')
                origdp = pd.to_datetime(data.get('origdp'), errors='coerce')
                refixeddp = pd.to_datetime(data.get('refixeddp'), errors='coerce')
                
                latest_dp = refixeddp if pd.notna(refixeddp) else origdp

                if pd.notna(supdate) and pd.notna(latest_dp):
                    # Check the flag we set in Step 1.
                    is_lower_rate = data.get('lowerrateapplicable') is True or safe_float(data.get('lowerrate')) > 0
                    data['rateapplied'] = "Lower Rate Applicable Within DP" if is_lower_rate and supdate <= latest_dp else \
                                          "Lower Rate Applicable Beyond DP With LD" if is_lower_rate and supdate > latest_dp else \
                                          "Supply Beyond Original DP or Refixed DP" if supdate > latest_dp else \
                                          "Supply Within Original DP or Refixed DP"
                    is_lower_rate = data.get('lowerrateapplicable') is True
                    if supdate <= latest_dp:
                        data['rateapplied'] = "Lower Rate Applicable Within DP" if is_lower_rate else "Supply Within Original DP or Refixed DP"
                    else:
                        data['rateapplied'] = "Lower Rate Applicable Beyond DP With LD" if is_lower_rate else "Supply Beyond Original DP or Refixed DP"


            # Get item-specific context for this row
            idx_df = get_item_index_df(item)
            if idx_df.empty:
                raise ValueError(f"Indices not configured for item '{item.name}'.")

            try:
                weights = json.loads(item.weights_json or "{}")
            except Exception:
                weights = {}

            result_row = calculate_for_item(item, data, idx_df, weights)
            # Combine input data with result data
            combined_row = {**data, **result_row}
            results.append(combined_row)
        except Exception as e:
            # Add error to the row and continue
            results.append({**data, 'calculation_error': str(e)})

    if not results:
        flash("No records were processed from the input file.", "warning")
        return redirect(url_for("pvc.bulk_calculator"))

    output_df = pd.DataFrame(results)

    if 'fairpricenew' not in output_df.columns:
        output_df['fairpricenew'] = None

    # If the input file contains a 'passed_amount' column, calculate the difference.
    if 'passed_amount' in output_df.columns:
        # Ensure columns are numeric before calculation, filling non-numeric values with 0
        output_df['passed_amount'] = pd.to_numeric(output_df['passed_amount'], errors='coerce').fillna(0)
        output_df['fairpricenew'] = pd.to_numeric(output_df['fairpricenew'], errors='coerce').fillna(0)

        # Calculate the difference
        output_df['difference'] = output_df['passed_amount'] - output_df['fairpricenew']

    output = BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        output_df.to_excel(writer, index=False, sheet_name="Bulk_PVC_Results")
    output.seek(0)

    return send_file(
        output,
        as_attachment=True,
        download_name=f"Bulk_PVC_Report_{item.name.replace(' ', '_')}_{datetime.now().strftime('%Y%m%d')}.xlsx",
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )

@pvc_bp.route("/calculate", methods=["POST"])
@login_required
def calculate():
    itemid = request.form.get("itemid") or request.form.get("item_id")
    if not itemid:
        flash("Please select an item.", "danger")
        return redirect(url_for("pvc.calculator"))

    item   = Item.query.get_or_404(int(itemid))
    idx_df = get_item_index_df(item)
    if idx_df.empty:
        flash(
            "Indices not configured for this item. "
            "Please add them via Admin → Item Indices.",
            "danger",
        )
        return redirect(url_for("pvc.calculator"))

    try:
        weights = json.loads(item.weights_json or "{}")
    except Exception:
        weights = {}

    pvc_input          = PVCInput(request.form)
    pvc_input.user_id  = current_user.id
    pvc_input.username = current_user.username
    data = pvc_input.to_dict()

    try:
        result_row = calculate_for_item(item, data, idx_df, weights)
    except ValueError as e:
        flash(str(e), "danger")
        return redirect(url_for("pvc.calculator"))
    except Exception:
        flash("Calculation failed. Please check your inputs and index data.", "danger")
        return redirect(url_for("pvc.calculator"))

    payload = {
        "pvcactual":               result_row.get("pvcactual",               0.0),
        "pvccontractual":          result_row.get("pvccontractual",          0.0),
        "loweractual":             result_row.get("loweractual"),
        "lowercontractual":        result_row.get("lowercontractual"),
        "ldamtactual":             result_row.get("ldamtactual",             0.0),
        "ldamtcontractual":        result_row.get("ldamtcontractual",        0.0),
        "fairprice":               result_row.get("fairpricenew",            0.0),
        "pvcactuallessldnew":      result_row.get("pvcactuallessldnew"),
        "pvccontractuallessldnew": result_row.get("pvccontractuallessldnew"),
        "loweractuallessld":       result_row.get("loweractuallessld"),
        "lowercontractuallessld":  result_row.get("lowercontractuallessld"),
        "delaydays":               result_row.get("delaydays",               0),
        "ldweeks":                 result_row.get("ldweeks",                 0),
        "ldratepct":               result_row.get("ldratepct",               0.0),
        "ldapplicable":            result_row.get("ldapplicable",            False),
        "selectedscenario":        result_row.get("selectedscenarionew"),
        "pvcperseta2":             result_row.get("pvcperseta2"),
        "pvcpersetb2":             result_row.get("pvcpersetb2"),
        "pvcpersetc1":             result_row.get("pvcpersetc1"),
        "pvcpersetd1":             result_row.get("pvcpersetd1"),
        "tenderno":                result_row.get("tenderno"),
        "pono":                    result_row.get("pono"),
        "scenarioamounts":         result_row.get("scenarioamounts",         {}),
        "scenariodetails":         result_row.get("scenariodetails",         []),
        "igbtvendordetails":       result_row.get("igbtvendordetails",       []),
        "igbtindexdetails":        result_row.get("igbtindexdetails",        []),
        "developmentaltender":     data.get("developmentaltender",           False),
    }

    # Sanitize NaN / Inf → None before touching the DB
    payload = {k: nan_to_none(v) for k, v in payload.items()}

    # JSON-encode list/dict sub-fields and sanitize NaN literals within them
    scenarioamounts_json     = nan_to_none(json.dumps(payload["scenarioamounts"]))
    scenariodetails_json     = nan_to_none(json.dumps(payload["scenariodetails"]))
    igbt_vendor_details_json = nan_to_none(json.dumps(payload["igbtvendordetails"]))

    calc = None
    last_calc_id = request.cookies.get("last_calc_id")
    if last_calc_id and last_calc_id.isdigit():
        calc = PVCResult.query.filter_by(id=int(last_calc_id), user_id=current_user.id, item_id=item.id).first()

    if calc:
        calc.basicrate = data.get("basicrate", 0)
        calc.quantity = data.get("quantity", 0)
        calc.freightrateperunit = data.get("freightrateperunit", 0)
        calc.pvcbasedate = data.get("pvcbasedate")
        calc.origdp = data.get("origdp")
        calc.refixeddp = data.get("refixeddp")
        calc.extendeddp = data.get("extendeddp")
        calc.caldate = data.get("caldate")
        calc.supdate = data.get("supdate")
        calc.rateapplied = data.get("rateapplied")
        calc.pvcactual = payload["pvcactual"]
        calc.pvccontractual = payload["pvccontractual"]
        calc.loweractual = payload["loweractual"]
        calc.lowercontractual = payload["lowercontractual"]
        calc.ldamtactual = payload["ldamtactual"]
        calc.ldamtcontractual = payload["ldamtcontractual"]
        calc.fairprice = payload["fairprice"]
        calc.selectedscenario = payload["selectedscenario"]
        calc.pvcactuallessldnew = payload["pvcactuallessldnew"]
        calc.pvccontractuallessldnew = payload["pvccontractuallessldnew"]
        calc.loweractuallessld = payload["loweractuallessld"]
        calc.lowercontractuallessld = payload["lowercontractuallessld"]
        calc.delaydays = payload["delaydays"]
        calc.ldweeksnew = payload["ldweeks"]
        calc.ldratepctnew = payload["ldratepct"]
        calc.ldapplicable = payload["ldapplicable"]
        calc.pvcperseta2 = payload["pvcperseta2"]
        calc.pvcpersetb2 = payload["pvcpersetb2"]
        calc.pvcpersetc1 = payload["pvcpersetc1"]
        calc.pvcpersetd1 = payload["pvcpersetd1"]
        calc.tenderno = payload["tenderno"]
        calc.pono = payload["pono"]
        calc.scenarioamounts_json = scenarioamounts_json
        calc.scenariodetails_json = scenariodetails_json
        calc.igbt_vendor_details_json = igbt_vendor_details_json
        calc.created_at = datetime.utcnow()
    else:
        calc = PVCResult(
            user_id=current_user.id,
            username=current_user.username,
            item_id=item.id,
            basicrate=data.get("basicrate",              0),
            quantity=data.get("quantity",                0),
            freightrateperunit=data.get("freightrateperunit", 0),
            pvcbasedate=data.get("pvcbasedate"),
            origdp=data.get("origdp"),
            refixeddp=data.get("refixeddp"),
            extendeddp=data.get("extendeddp"),
            caldate=data.get("caldate"),
            supdate=data.get("supdate"),
            rateapplied=data.get("rateapplied"),
            pvcactual=payload["pvcactual"],
            pvccontractual=payload["pvccontractual"],
            loweractual=payload["loweractual"],
            lowercontractual=payload["lowercontractual"],
            ldamtactual=payload["ldamtactual"],
            ldamtcontractual=payload["ldamtcontractual"],
            fairprice=payload["fairprice"],
            selectedscenario=payload["selectedscenario"],
            pvcactuallessldnew=payload["pvcactuallessldnew"],
            pvccontractuallessldnew=payload["pvccontractuallessldnew"],
            loweractuallessld=payload["loweractuallessld"],
            lowercontractuallessld=payload["lowercontractuallessld"],
            delaydays=payload["delaydays"],
            ldweeksnew=payload["ldweeks"],
            ldratepctnew=payload["ldratepct"],
            ldapplicable=payload["ldapplicable"],
            pvcperseta2=payload["pvcperseta2"],
            pvcpersetb2=payload["pvcpersetb2"],
            pvcpersetc1=payload["pvcpersetc1"],
            pvcpersetd1=payload["pvcpersetd1"],
            tenderno=payload["tenderno"],
            pono=payload["pono"],
            scenarioamounts_json=scenarioamounts_json,
            scenariodetails_json=scenariodetails_json,
            igbt_vendor_details_json=igbt_vendor_details_json,
        )
        db.session.add(calc)

    db.session.commit()

    # Format dates for the UI report
    data["pvcbasedate"] = format_date_ddmmyyyy(data.get("pvcbasedate"))
    data["origdp"] = format_date_ddmmyyyy(data.get("origdp"))
    data["refixeddp"] = format_date_ddmmyyyy(data.get("refixeddp"))
    data["extendeddp"] = format_date_ddmmyyyy(data.get("extendeddp"))
    data["caldate"] = format_date_ddmmyyyy(data.get("caldate"))
    data["supdate"] = format_date_ddmmyyyy(data.get("supdate"))
    data["lowerbasicdate"] = format_date_ddmmyyyy(data.get("lowerbasicdate"))

    action = request.form.get("action")
    result = ResultObj(payload)
    resp = make_response(render_template(
        "result.html",
        item=item.name,
        itemobj=item,
        data=data,
        result=result,
        calcid=calc.id,
        auto_summary=(action == "summary"),
    ))
    resp.set_cookie("last_calc_id", str(calc.id), max_age=3600)
    return resp


@pvc_bp.route("/calc/<int:calcid>")
@login_required
def view_calc(calcid):
    calc = PVCResult.query.filter_by(id=calcid, user_id=current_user.id).first_or_404()
    item = calc.item

    # NOTE: PVCResult does not persist lower_rate or lower_freight as columns.
    # We reconstruct what we can; display templates should handle None gracefully.
    data = {
        "basicrate":           round(calc.basicrate, 2) if calc.basicrate else 0,
        "quantity":            round(calc.quantity, 2) if calc.quantity else 0,
        "freightrateperunit":  round(calc.freightrateperunit, 2) if calc.freightrateperunit else 0,
        "pvcbasedate":         format_date_ddmmyyyy(calc.pvcbasedate),
        "origdp":              format_date_ddmmyyyy(calc.origdp),
        "refixeddp":           format_date_ddmmyyyy(calc.refixeddp),
        "extendeddp":          format_date_ddmmyyyy(calc.extendeddp),
        "caldate":             format_date_ddmmyyyy(calc.caldate),
        "supdate":             format_date_ddmmyyyy(calc.supdate),
        "pvcbasedate":         calc.pvcbasedate,
        "origdp":              calc.origdp,
        "refixeddp":           calc.refixeddp,
        "extendeddp":          calc.extendeddp,
        "caldate":             calc.caldate,
        "supdate":             calc.supdate,
        "rateapplied":         calc.rateapplied,
        "lowerrate":           None,   # not stored; templates must tolerate None
        "lowerfreight":        0,
        "lowerbasicdate":      None,
        "developmentaltender": False,
    }

    payload = {
        "pvcactual":               calc.pvcactual,
        "pvccontractual":          calc.pvccontractual,
        "loweractual":             calc.loweractual,
        "lowercontractual":        calc.lowercontractual,
        "ldamtactual":             calc.ldamtactual,
        "ldamtcontractual":        calc.ldamtcontractual,
        "fairprice":               calc.fairprice,
        "selectedscenario":        calc.selectedscenario,
        "ldapplicable":            calc.ldapplicable,
        "pvcactuallessldnew":      calc.pvcactuallessldnew,
        "pvccontractuallessldnew": calc.pvccontractuallessldnew,
        "loweractuallessld":       calc.loweractuallessld,
        "lowercontractuallessld":  calc.lowercontractuallessld,
        "delaydays":               calc.delaydays,
        "ldweeks":                 calc.ldweeksnew,
        "ldratepct":               calc.ldratepctnew,
        "pvcperseta2":             calc.pvcperseta2,
        "pvcpersetb2":             calc.pvcpersetb2,
        "pvcpersetc1":             calc.pvcpersetc1,
        "pvcpersetd1":             calc.pvcpersetd1,
        "tenderno":                calc.tenderno,
        "pono":                    calc.pono,
        "scenarioamounts":         json.loads(calc.scenarioamounts_json     or "{}"),
        "scenariodetails":         json.loads(calc.scenariodetails_json     or "[]"),
        "igbtvendordetails":       json.loads(calc.igbt_vendor_details_json or "[]"),
        "igbtindexdetails":        [],   # not persisted; only available at calculation time
        "developmentaltender":     False,
    }

    result = ResultObj(payload)
    return render_template(
        "result.html",
        item=item.name if item else "",
        itemobj=item,
        data=data,
        result=result,
        calcid=calc.id,
    )


@pvc_bp.route("/process_rnote/<rnote_no>")
@login_required
def process_rnote_and_ic(rnote_no):
    """
    Orchestrates the full R-Note, IC, PO, and MA processing workflow.
    1. Downloads the R-Note PDF.
    2. Extracts data from it, including the PO number and IC number.
    3. Downloads and parses the corresponding IC PDF.
    4. Downloads the PO PDF based on the extracted PO number.
    5. Extracts data from the PO PDF.
    6. Downloads all related MA (Material Amendment) documents for that PO.
    7. Extracts and processes data from the MA documents.
    8. Generates and serves a single Excel file with all extracted data.
    """
    from flask import current_app
    from .rnote_logic import download_rnote_pdf, parse_rnote, download_and_parse_ic
    from .po_parser import extract_data_from_pdf

    rnote_no = (rnote_no or "").strip()
    if not rnote_no.isdigit():
        flash("Invalid R-Note Number format. It should contain only digits.", "danger")
        return redirect(url_for("main.index"))

    # Pad with leading zeros if less than 10 digits
    if len(rnote_no) < 10:
        rnote_no = rnote_no.zfill(10)

    # --- Directory Configuration ---
    rnote_download_dir = os.path.join(current_app.root_path, "..", "downloads", "RNotes")
    ic_download_dir = os.path.join(current_app.root_path, "..", "downloads", "IC")
    po_download_dir = os.path.join(current_app.root_path, "..", "downloads", "PO")
    ma_download_dir = os.path.join(current_app.root_path, "..", "downloads", "MA")
    os.makedirs(rnote_download_dir, exist_ok=True)
    os.makedirs(ic_download_dir, exist_ok=True)
    os.makedirs(po_download_dir, exist_ok=True)
    os.makedirs(ma_download_dir, exist_ok=True)

    # === STEP 1 & 2: Download and Parse R-Note ===
    pdf_path, filename = download_rnote_pdf(rnote_no, rnote_download_dir)
    if not pdf_path:
        flash(f"R-Note '{rnote_no}' could not be found on the IREPS server.", "warning")
        return redirect(url_for("main.index"))
    flash(f"Step 1/5: R-Note '{filename}' downloaded.", "info")

    rnote_data = parse_rnote(pdf_path)
    ic_no = rnote_data.get("ic_no")
    po_no_from_rnote = rnote_data.get("po_no")
    flash(f"Step 2/5: Parsed R-Note. Found IC No: '{ic_no}' and PO No: '{po_no_from_rnote}'.", "info")

    # === STEP 3: Download and Parse IC PDF (if found) ===
    ic_data = {}
    if ic_no:
        ic_data = download_and_parse_ic(ic_no, ic_download_dir)
        if not ic_data or ic_data.get("po_no") == "NOT FOUND":
            flash(f"Could not find or download the IC document for '{ic_no}'.", "warning")
        else:
            flash("Step 3/5: IC document downloaded and data extracted.", "info")
    else:
        flash("Step 3/5: No IC Number found in R-Note, skipping IC processing.", "warning")

    # === STEP 4: Process PO and MAs (if PO number was found) ===
    po_df, processed_ma_df = pd.DataFrame(), pd.DataFrame()
    if po_no_from_rnote and po_no_from_rnote.isdigit():
        flash(f"Step 4/5: Starting processing for PO '{po_no_from_rnote}'.", "info")
        po_pdf_path, po_filename = _download_po_pdf(po_no_from_rnote, po_download_dir)
        if po_pdf_path:
            extracted_records = extract_data_from_pdf(po_pdf_path, po_filename)
            if extracted_records:
                po_df = pd.DataFrame(extracted_records)
                links_df = _generate_po_view_links(po_df)
                if not links_df.empty:
                    _download_ma_documents(links_df, ma_download_dir)
                    raw_ma_extract_df = _extract_ma_data(ma_download_dir)
                    if not raw_ma_extract_df.empty:
                        processed_ma_df = _process_ma_data(raw_ma_extract_df)
        flash("Step 4/5: Finished PO and MA document processing.", "info")
    else:
        flash("Step 4/5: No valid PO number in R-Note, skipping PO/MA processing.", "warning")

    # === STEP 5: Generate and serve combined Excel file ===
    output = BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        pd.DataFrame([rnote_data]).to_excel(writer, index=False, sheet_name="R-Note Data")
        if ic_data:
            pd.DataFrame([ic_data]).to_excel(writer, index=False, sheet_name="IC Data")
        if not po_df.empty:
            po_df.to_excel(writer, index=False, sheet_name="PO_Data_Extract")
        if not processed_ma_df.empty:
            processed_ma_df.to_excel(writer, index=False, sheet_name="MA_Extract_Formatted")

    output.seek(0)
    flash("Step 5/5: Final combined Excel file generated. Download starting.", "success")
    return send_file(output, as_attachment=True, download_name=f"Full_Extract_{rnote_no}.xlsx",
                     mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

@pvc_bp.route("/process_po/<po_no>")
@login_required
def process_po_and_ma(po_no):
    """
    Orchestrates the entire PO and MA processing workflow:
    1. Downloads the PO PDF.
    2. Extracts data from the PO PDF into a DataFrame.
    3. Generates PO View links from the extracted data.
    4. Downloads all related MA documents.
    5. Extracts data from the MA documents.
    6. Generates and serves the final 'maextract.xlsx' file.
    """
    from flask import current_app
    from .po_parser import extract_data_from_pdf

    po_no = (po_no or "").strip()
    if not po_no.isdigit():
        flash("Invalid PO Number format. It should contain only digits.", "danger")
        return redirect(url_for("main.index"))

    # --- Configuration ---
    download_dir = os.path.join(current_app.root_path, "..", "downloads", "PO")
    ma_download_dir = os.path.join(current_app.root_path, "..", "downloads", "MA")
    os.makedirs(download_dir, exist_ok=True)
    os.makedirs(ma_download_dir, exist_ok=True)

    # === STEP 1 & 2: Download PO and Extract Data ===
    pdf_path, filename = _download_po_pdf(po_no, download_dir)
    if not pdf_path:
        # _download_po_pdf already flashed the error
        return redirect(url_for("main.index"))

    extracted_records = extract_data_from_pdf(pdf_path, filename)
    if not extracted_records:
        flash(f"PO '{filename}' was downloaded, but no structured data could be extracted.", "warning")
        return redirect(url_for("main.index"))

    po_df = pd.DataFrame(extracted_records)
    flash(f"Step 1/5: PO '{filename}' downloaded and data extracted.", "info")

    # === STEP 3: Generate PO View Links (in memory) ===
    links_df = _generate_po_view_links(po_df)
    if links_df.empty:
        flash("Could not generate PO View links from the extracted data.", "danger")
        return redirect(url_for("main.index"))
    flash("Step 2/5: Generated PO View links for MA document search.", "info")

    # === STEP 4: Download MA Documents ===
    _download_ma_documents(links_df, ma_download_dir)
    flash("Step 3/5: Finished searching for and downloading MA documents.", "info")

    # === STEP 5 & 6: Extract from MA files and generate Excel ===
    raw_ma_extract_df = _extract_ma_data(ma_download_dir)
    if raw_ma_extract_df.empty:
        flash("Step 4/5: MA documents processed, but no amendment data was found to extract.", "warning")
        return redirect(url_for("main.index"))

    flash("Step 4/5: Extracted data from MA documents.", "info")

    # === NEW STEP: Apply formatting rules from the user's script ===
    processed_ma_df = _process_ma_data(raw_ma_extract_df)
    flash("Step 5/6: Applied final formatting and business rules to the extracted data.", "info")

    output = BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl", date_format="DD-MM-YYYY", datetime_format="DD-MM-YYYY") as writer:
        po_df.to_excel(writer, index=False, sheet_name="PO_Data_Extract")
        processed_ma_df.to_excel(writer, index=False, sheet_name="MA_Extract_Formatted")

    output.seek(0)
    flash("Step 6/6: Final Formatted MA Extract Excel file generated. Download starting.", "success")
    return send_file(
        output,
        as_attachment=True,
        download_name=f"MA_Extract_{po_no}.xlsx",
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


def _download_po_pdf(po_no, download_dir):
    """Downloads a single PO PDF, returning its path and filename."""
    max_year = 2026
    base_url = "https://ireps.gov.in/ireps/etender/pdfdocs/MMIS/PO/{year}/21/{po}.pdf"

    try:
        base_year = 2000 + int(po_no[2:4])
    except (ValueError, IndexError):
        flash(f"Could not determine year from PO number '{po_no}'.", "danger")
        return None, None

    for year in range(base_year, max_year + 1):
        filename = f"{po_no}_{year}.pdf"
        file_path = os.path.join(download_dir, filename)

        if os.path.exists(file_path):
            return file_path, filename

        url = base_url.format(year=year, po=po_no)
        try:
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
            }
            response = requests.get(url, timeout=20, headers=headers)
            if response.status_code == 200 and response.headers.get("Content-Type") == "application/pdf":
                with open(file_path, "wb") as f:
                    f.write(response.content)
                # This was already implemented in a previous change.
                # The user is re-confirming this requirement.
                # The code to open the file is already present.
                os.startfile(file_path)
                return file_path, filename
        except requests.exceptions.RequestException:
            continue

    flash(f"Purchase Order '{po_no}' could not be found on the IREPS server.", "warning")
    return None, None


def _generate_po_view_links(df):
    """Generates PO View links from a DataFrame of extracted PO data."""
    PO_NO_COL = "PO No"
    POKEY_COL = "PO Key"
    POVIEW_TEMPLATE = "https://ireps.gov.in/ireps/etender/pdfdocs/MMIS/POVIEW/{year}/21/PO{pokey}.html"

    if PO_NO_COL not in df.columns or POKEY_COL not in df.columns:
        return pd.DataFrame()

    po_df = df[[PO_NO_COL, POKEY_COL]].dropna().groupby(PO_NO_COL)[POKEY_COL].first().reset_index()

    def extract_po_year(po_no):
        try:
            return 2000 + int(str(po_no)[2:4])
        except Exception:
            return ""

    rows = []
    for _, row in po_df.iterrows():
        po_no, po_key = row[PO_NO_COL], row[POKEY_COL]
        po_year = extract_po_year(po_no)
        link = POVIEW_TEMPLATE.format(year=po_year, pokey=po_key) if po_year else ""
        rows.append({"PO_NO": po_no, "Link": link})

    return pd.DataFrame(rows)


def _download_ma_documents(links_df, main_download_dir):
    """Scrapes PO View pages and downloads related MA documents."""
    from .ma_logic import find_working_year_url, download_file_utility

    records = links_df[['PO_NO', 'Link']].dropna().astype(str).values.tolist()

    for po_no, base_url in records:
        working_url = find_working_year_url(base_url)
        if not working_url:
            continue

        safe_folder = re.sub(r'[\\/*?:"<>|]', '_', f"PO_{po_no}")
        link_folder = os.path.join(main_download_dir, safe_folder)
        os.makedirs(link_folder, exist_ok=True)

        try:
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
            }
            response = requests.get(working_url, timeout=12, headers=headers)
            response.raise_for_status()
            soup = BeautifulSoup(response.text, 'html.parser')

            download_tasks = []
            for tag in soup.find_all('a', href=True):
                file_url = urljoin(working_url, tag['href'])
                file_name = os.path.basename(file_url.split('?')[0])

                if file_name.upper().startswith("MAV"): continue
                if "_" not in file_name: continue
                if not file_name.lower().endswith(('.pdf', '.xls', '.xlsx', '.doc', '.zip')): continue

                file_path = os.path.join(link_folder, file_name)
                download_tasks.append((file_url, file_path))

            if not download_tasks: continue

            with ThreadPoolExecutor(max_workers=5) as executor:
                list(executor.map(lambda p: download_file_utility(*p), download_tasks))

        except Exception:
            # Log error if needed
            pass
        time.sleep(1)


def _extract_ma_data(input_folder):
    """Extracts data from MA PDFs and returns a DataFrame."""
    from .ma_logic import extract_ma_only
    return extract_ma_only(input_folder)

def _process_ma_data(df):
    """Applies post-processing rules to the MA extract DataFrame."""
    from .ma_logic import process_ma_extract_data
    return process_ma_extract_data(df)


@pvc_bp.route("/calc/<int:calcid>/excel")
@login_required
def export_calc_excel(calcid):
    calc = PVCResult.query.filter_by(id=calcid, user_id=current_user.id).first_or_404()
    item = calc.item

    sc_map = {"A2": "Actual PVC", "B2": "Contractual PVC", "C1": "Lower Actual PVC", "D1": "Lower Contractual PVC"}

    maindata = {
        "Calc ID":            calc.id,
        "User":               calc.username,
        "Item":               item.name if item else "",
        "Basic Rate":         calc.basicrate,
        "Quantity":           calc.quantity,
        "FreightUnit":        calc.freightrateperunit,
        "PVC Base Date":      format_date_ddmmyyyy(calc.pvcbasedate),
        "Original DP":        format_date_ddmmyyyy(calc.origdp),
        "Refixed DP":         format_date_ddmmyyyy(calc.refixeddp),
        "Extended DP":        format_date_ddmmyyyy(calc.extendeddp),
        "Call Date":          format_date_ddmmyyyy(calc.caldate),
        "Supply Date":        format_date_ddmmyyyy(calc.supdate),
        "PVC Base Date":      calc.pvcbasedate,
        "Original DP":        calc.origdp,
        "Refixed DP":         calc.refixeddp,
        "Extended DP":        calc.extendeddp,
        "Call Date":          calc.caldate,
        "Supply Date":        calc.supdate,
        "Rate Applied":       calc.rateapplied,
        "PVC Actual":         calc.pvcactual,
        "PVC Contractual":    calc.pvccontractual,
        "Lower Actual":       calc.loweractual,
        "Lower Contractual":  calc.lowercontractual,
        "LD Days":            calc.delaydays,
        "LD Weeks":           calc.ldweeksnew,
        "LD Rate":            calc.ldratepctnew,
        "LD Amt Actual":      calc.ldamtactual,
        "LD Amt Contractual": calc.ldamtcontractual,
        "PVC Actual-LD":      calc.pvcactuallessldnew,
        "PVC Contractual-LD": calc.pvccontractuallessldnew,
        "Lower Actual-LD":    calc.loweractuallessld,
        "Lower Contr-LD":     calc.lowercontractuallessld,
        "Fair Price":         calc.fairprice,
        "Selected Scenario":  sc_map.get(calc.selectedscenario, calc.selectedscenario),
        "Tender No":          calc.tenderno,
        "PO No":              calc.pono,
    }

    sadata = [
        {"Scenario": sc_map.get(sc, sc), "Amount": amount}
        for sc, amount in (json.loads(calc.scenarioamounts_json or "{}") or {}).items()
    ]

    sddata = []
    for det in json.loads(calc.scenariodetails_json or "[]"):
        row = {
            "Scenario":      sc_map.get(det.get("scenario"), det.get("scenario")),
            "Base Month":    det.get("basemonth"),
            "Current Month": det.get("currentmonth"),
            "PVC":           det.get("pvcpercent"),
        }
        for k, v in det.items():
            if k not in ("scenario", "basemonth", "currentmonth", "pvcpercent"):
                row[k] = v
        sddata.append(row)

    igbtvendors = json.loads(calc.igbt_vendor_details_json or "[]")
    for v in igbtvendors:
        if "scenario" in v:
            v["scenario"] = sc_map.get(v["scenario"], v["scenario"])

    output = BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        pd.DataFrame([maindata]).to_excel(writer, index=False, sheet_name="PVC Result")
        if sadata:
            pd.DataFrame(sadata).to_excel(writer, index=False, sheet_name="Scenario Amounts")
        if sddata:
            pd.DataFrame(sddata).to_excel(writer, index=False, sheet_name="Index Details")
        if igbtvendors:
            pd.DataFrame(igbtvendors).to_excel(writer, index=False, sheet_name="IGBT Vendors")

    output.seek(0)
    return send_file(
        output,
        as_attachment=True,
        download_name=f"PVC_Calc_{calc.id}.xlsx",
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
