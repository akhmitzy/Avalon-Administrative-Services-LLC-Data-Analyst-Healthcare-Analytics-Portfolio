"""
Synthetic data generator for the Diagnostic Insights Analytics portfolio.

Produces a realistic (but entirely fictional) lab-benefit-management dataset:
  * 6 health-plan clients across Commercial, Medicare Advantage and Medicaid
  * ~54k members with eligibility spans (churn included)
  * ~600k+ outpatient lab claim lines, Jan 2024 - Jun 2026 (paid through 2026-06-30)
  * a lab network (in / out of network), CPT test catalog, ICD-10 -> clinical category map
  * finance general-ledger control totals for reconciliation
  * a lab-management program that goes live at different dates per client and
    changes ordering behaviour (fewer non-adherent tests, more in-network use)

Data-quality defects are injected on purpose so the pipeline has something real to catch:
exact duplicate lines, members not eligible on the date of service, invalid CPT codes,
unknown lab IDs, service dates after paid dates, and a claims batch missing from the extract.

No real patient, plan or provider data is used. All names are fictional.
"""
from __future__ import annotations

import argparse
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

SEED = 20260715
EXTRACT_DATE = pd.Timestamp("2026-06-30")  # claims paid through
MONTHS = pd.period_range("2024-01", "2026-06", freq="M")

ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "data" / "raw"
REF = ROOT / "data" / "reference"

# --------------------------------------------------------------------------------------
# Reference data
# --------------------------------------------------------------------------------------
CLIENTS = pd.DataFrame(
    [
        # id, name, line of business, members, go-live (None = not live), capture strength
        ("C01", "Suncoast Health Partners", "Commercial", 12000, "2025-01-01", 0.90),
        ("C02", "Bayview Medicare Advantage", "Medicare Advantage", 8000, "2025-04-01", 0.80),
        ("C03", "Heartland Community Care", "Medicaid", 14000, "2024-10-01", 0.95),
        ("C04", "Pinecrest Employer Health", "Commercial", 6000, "2025-07-01", 0.40),
        ("C05", "Lakeshore Senior Plan", "Medicare Advantage", 5000, None, 0.00),
        ("C06", "Prairie State Medicaid", "Medicaid", 9000, "2025-10-01", 0.85),
    ],
    columns=["client_id", "client_name", "line_of_business", "target_members", "go_live_date", "capture"],
)

CONTRACTS = pd.DataFrame(
    [
        # client, admin fee PMPM, guaranteed savings multiple of fees, % of fees at risk
        ("C01", 0.85, 2.0, 0.25),
        ("C02", 1.10, 2.0, 0.30),
        ("C03", 0.70, 2.5, 0.25),
        ("C04", 0.90, 2.0, 0.20),
        ("C05", None, None, None),  # prospect - pricing exercise
        ("C06", 0.70, 2.0, 0.25),
    ],
    columns=["client_id", "fee_pmpm", "guarantee_roi_multiple", "fees_at_risk_pct"],
)

# CPT catalog: code, description, category, in-network allowed ($), policy group
TESTS = pd.DataFrame(
    [
        ("80053", "Comprehensive metabolic panel", "Routine", 11.0, None),
        ("85025", "Complete blood count w/ differential", "Routine", 8.0, None),
        ("80061", "Lipid panel", "Routine", 15.0, None),
        ("83036", "Hemoglobin A1c", "Routine", 11.0, "P02_HBA1C_FREQ"),
        ("84443", "Thyroid stimulating hormone", "Routine", 19.0, None),
        ("82306", "Vitamin D, 25-hydroxy", "Routine", 33.0, "P01_VITD_SCREEN"),
        ("84153", "Prostate specific antigen, total", "Routine", 20.0, None),
        ("81001", "Urinalysis, automated w/ microscopy", "Routine", 4.0, None),
        ("87086", "Urine culture, colony count", "Routine", 9.0, None),
        ("88305", "Surgical pathology, level IV", "Anatomic Pathology", 70.0, None),
        ("87798", "Infectious agent, amplified probe NOS", "Infectious Disease", 35.0, None),
        ("87633", "Respiratory pathogen panel, 12-25 targets", "Infectious Disease", 416.0, "P05_RESP_PANEL"),
        ("87637", "SARS-CoV-2 + influenza A/B + RSV", "Infectious Disease", 142.0, None),
        ("80307", "Drug screen, presumptive (instrument)", "Toxicology", 62.0, None),
        ("G0480", "Drug test, definitive, 1-7 classes", "Toxicology", 114.0, None),
        ("G0483", "Drug test, definitive, 22+ classes", "Toxicology", 246.0, "P04_DEF_DRUG"),
        ("81528", "Colorectal cancer screening, mt-sDNA", "Genetic & Molecular", 509.0, "P06_GENETIC_INDICATION"),
        ("81420", "Fetal aneuploidy (NIPT)", "Genetic & Molecular", 600.0, "P06_GENETIC_INDICATION"),
        ("81162", "BRCA1/BRCA2 full sequence + dup/del", "Genetic & Molecular", 1800.0, "P06_GENETIC_INDICATION"),
        ("81519", "Oncology, breast, 21-gene expression", "Genetic & Molecular", 3873.0, "P06_GENETIC_INDICATION"),
        ("0037U", "Targeted genomic sequence, solid tumor (CGP)", "Genetic & Molecular", 3500.0, "P06_GENETIC_INDICATION"),
        ("81445", "Targeted genomic panel, solid tumor 5-50 genes", "Genetic & Molecular", 597.0, "P06_GENETIC_INDICATION"),
        ("81225", "CYP2C19 pharmacogenomic", "Genetic & Molecular", 292.0, "P06_GENETIC_INDICATION"),
        ("81479", "Unlisted molecular pathology", "Genetic & Molecular", 900.0, "P06_GENETIC_INDICATION"),
    ],
    columns=["cpt_code", "test_description", "test_category", "in_network_rate", "policy_group"],
)

# ICD-10 sample codes -> clinical category used by policy logic
DX = pd.DataFrame(
    [
        ("Z00.00", "General adult exam", "screening"),
        ("Z13.21", "Screening for nutritional disorder", "screening"),
        ("Z12.11", "Screening for malignant neoplasm of colon", "crc_screening"),
        ("E55.9", "Vitamin D deficiency, unspecified", "vitd_deficiency"),
        ("M81.0", "Age-related osteoporosis", "vitd_deficiency"),
        ("E11.9", "Type 2 diabetes without complications", "diabetes"),
        ("R73.03", "Prediabetes", "prediabetes"),
        ("N18.30", "Chronic kidney disease, stage 3", "ckd"),
        ("Z34.90", "Supervision of normal pregnancy", "pregnancy"),
        ("C50.911", "Malignant neoplasm of right female breast", "oncology"),
        ("C34.90", "Malignant neoplasm of lung, unspecified", "oncology"),
        ("Z80.3", "Family history of breast cancer", "family_history_cancer"),
        ("F11.20", "Opioid dependence, uncomplicated", "substance_use"),
        ("Z79.891", "Long-term use of opiate analgesic", "substance_use"),
        ("J06.9", "Acute upper respiratory infection", "respiratory_infection"),
        ("D84.9", "Immunodeficiency, unspecified", "immunocompromised"),
        ("N39.0", "Urinary tract infection", "uti"),
        ("I25.10", "Atherosclerotic heart disease", "cardiovascular"),
        ("F32.A", "Depression, unspecified", "behavioral"),
        ("R53.83", "Other fatigue", "general"),
    ],
    columns=["icd10_code", "icd10_description", "clinical_category"],
)

# Clinical indications that make a genetic/molecular test policy-adherent
GENETIC_INDICATIONS = pd.DataFrame(
    [
        ("81528", "crc_screening"),
        ("81420", "pregnancy"),
        ("81162", "oncology"),
        ("81162", "family_history_cancer"),
        ("81519", "oncology"),
        ("0037U", "oncology"),
        ("81445", "oncology"),
        ("81225", "cardiovascular"),
        ("81225", "behavioral"),
        # 81479 (unlisted) has no automatic indication -> always requires clinical review
    ],
    columns=["cpt_code", "clinical_category"],
)

LABS = pd.DataFrame(
    [
        ("L001", "Meridian Reference Laboratories", "National reference", True),
        ("L002", "Coastal Diagnostics Network", "National reference", True),
        ("L003", "St. Brendan Hospital Outreach Lab", "Hospital outreach", True),
        ("L004", "Valley Regional Medical Center Lab", "Hospital outreach", True),
        ("L005", "Northgate Health System Lab", "Hospital outreach", True),
        ("L006", "Keystone Clinical Labs", "Regional independent", True),
        ("L007", "Bluewater Pathology Associates", "Regional independent", True),
        ("L008", "Helix Precision Genomics", "Genetic specialty", True),
        ("L009", "Apex Molecular Insights", "Genetic specialty", False),
        ("L010", "GenoPath Advanced Testing", "Genetic specialty", False),
        ("L011", "ClearScreen Toxicology", "Toxicology", False),
        ("L012", "Rapid Results Independent Lab", "Regional independent", False),
    ],
    columns=["lab_id", "lab_name", "lab_type", "in_network"],
)


def month_index(p: pd.Period) -> int:
    return (p.year - 2024) * 12 + (p.month - 1)


# Seasonality multipliers (index 0 = Jan)
SEASON_GENERAL = np.array([1.06, 1.02, 1.04, 1.01, 1.00, 0.96, 0.92, 0.98, 1.02, 1.03, 0.96, 0.86])
SEASON_RESP = np.array([2.6, 2.2, 1.5, 0.9, 0.5, 0.35, 0.3, 0.45, 0.7, 1.1, 1.7, 2.4])


def build_members(rng: np.random.Generator) -> pd.DataFrame:
    frames = []
    n_months = len(MONTHS)
    for c in CLIENTS.itertuples():
        n = c.target_members
        lob = c.line_of_business
        if lob == "Medicare Advantage":
            age = rng.normal(73, 6.5, n).clip(65, 98)
        elif lob == "Medicaid":
            age = rng.gamma(2.2, 11, n).clip(0, 64)
        else:
            age = rng.normal(39, 15, n).clip(0, 64)
        sex = rng.choice(["F", "M"], n, p=[0.53, 0.47])
        p = {
            "Commercial": dict(diab=0.08, ckd=0.02, onc=0.02, fam=0.04, sud=0.02, imm=0.02, cv=0.05, bh=0.08),
            "Medicare Advantage": dict(diab=0.26, ckd=0.12, onc=0.07, fam=0.04, sud=0.01, imm=0.04, cv=0.22, bh=0.07),
            "Medicaid": dict(diab=0.10, ckd=0.03, onc=0.01, fam=0.03, sud=0.08, imm=0.03, cv=0.05, bh=0.14),
        }[lob]
        # eligibility: 72% enrolled at window start, others join later; ~20% leave before end
        start = np.where(rng.random(n) < 0.72, 0, rng.integers(1, n_months, n))
        leaves = rng.random(n) < 0.20
        end = np.where(leaves, np.minimum(start + rng.integers(3, n_months, n), n_months - 1), n_months - 1)
        df = pd.DataFrame(
            {
                "client_id": c.client_id,
                "age_at_start": age.round().astype(int),
                "sex": sex,
                "diabetes": rng.random(n) < p["diab"],
                "ckd": rng.random(n) < p["ckd"],
                "oncology": rng.random(n) < p["onc"],
                "family_history_cancer": rng.random(n) < p["fam"],
                "substance_use": (rng.random(n) < p["sud"]) & (age >= 14),
                "immunocompromised": rng.random(n) < p["imm"],
                "cardiovascular": rng.random(n) < p["cv"],
                "behavioral": rng.random(n) < p["bh"],
                "start_idx": start,
                "end_idx": end,
            }
        )
        frames.append(df)
    m = pd.concat(frames, ignore_index=True)
    m.insert(0, "member_id", ["M" + str(i).zfill(7) for i in rng.permutation(len(m)) + 1000001])
    m["birth_year"] = 2024 - m["age_at_start"]
    return m


def expand_member_months(m: pd.DataFrame) -> pd.DataFrame:
    lens = (m["end_idx"] - m["start_idx"] + 1).to_numpy()
    idx = np.repeat(np.arange(len(m)), lens)
    offs = np.concatenate([np.arange(k) for k in lens])
    mm = m.iloc[idx].reset_index(drop=True)
    mm["m_idx"] = mm["start_idx"].to_numpy() + offs
    mm["age"] = mm["age_at_start"] + mm["m_idx"] // 12
    return mm


def draw(rng, lam):
    return rng.poisson(np.clip(lam, 0, None))


def generate_claims(rng: np.random.Generator, members: pd.DataFrame) -> pd.DataFrame:
    mm = expand_member_months(members)
    cl = CLIENTS.set_index("client_id")
    golive_idx = {
        r.client_id: (month_index(pd.Period(r.go_live_date, "M")) if r.go_live_date else 10_000)
        for r in CLIENTS.itertuples()
    }
    mm["lob"] = mm["client_id"].map(cl["line_of_business"])
    mm["post"] = mm["m_idx"].to_numpy() >= mm["client_id"].map(golive_idx).to_numpy()
    mm["capture"] = mm["client_id"].map(cl["capture"]).to_numpy() * mm["post"]
    moy = mm["m_idx"].to_numpy() % 12
    yrs = mm["m_idx"].to_numpy() / 12.0
    sg = SEASON_GENERAL[moy]
    sr = SEASON_RESP[moy]
    cap = mm["capture"].to_numpy()
    age = mm["age"].to_numpy()
    female = (mm["sex"] == "F").to_numpy()
    lob = mm["lob"].to_numpy()
    lob_mult = np.select([lob == "Medicare Advantage", lob == "Medicaid"], [1.7, 1.05], 1.0)
    pediatric = age < 18
    adult = ~pediatric
    D = {k: mm[k].to_numpy() for k in ["diabetes", "ckd", "oncology", "family_history_cancer",
                                       "substance_use", "immunocompromised", "cardiovascular", "behavioral"]}

    def eff(reduction):
        """Program effect: multiplicative reduction applied post go-live, scaled by client capture."""
        return 1 - reduction * cap

    pieces = []  # list of (row_index array, cpt, icd10 choice array or str, flags)

    def add(cpt, lam_annual, icd, kind="routine", oon_p=None):
        lam = lam_annual / 12.0
        k = draw(rng, lam)
        rows = np.repeat(np.arange(len(mm)), k)
        if len(rows) == 0:
            return
        if callable(icd):
            icd_arr = icd(rows)
        else:
            icd_arr = np.full(len(rows), icd, dtype=object)
        pieces.append(pd.DataFrame({"r": rows, "cpt_code": cpt, "icd10_code": icd_arr, "kind": kind}))

    def pick(rows, options, probs):
        return rng.choice(options, len(rows), p=probs)

    # ---- Routine chemistry / hematology ----------------------------------------------
    base = sg * lob_mult * (0.35 + 0.65 * adult)
    add("80053", 0.95 * base * (1 + 0.8 * D["diabetes"] + 1.0 * D["ckd"]),
        lambda r: pick(r, ["Z00.00", "R53.83", "E11.9", "N18.30", "I25.10"], [0.45, 0.2, 0.15, 0.1, 0.1]))
    add("85025", 0.9 * base, lambda r: pick(r, ["Z00.00", "R53.83", "J06.9"], [0.5, 0.35, 0.15]))
    add("80061", 0.45 * base * adult * (1 + 0.8 * D["diabetes"] + 0.8 * D["cardiovascular"]),
        lambda r: pick(r, ["Z00.00", "I25.10", "E11.9"], [0.6, 0.25, 0.15]))
    add("84443", 0.38 * base * adult * (1 + 0.6 * female), lambda r: pick(r, ["Z00.00", "R53.83"], [0.5, 0.5]))
    add("81001", 0.40 * base, lambda r: pick(r, ["Z00.00", "N39.0", "E11.9"], [0.55, 0.35, 0.10]))
    add("87086", 0.15 * base * (1 + 0.8 * female), "N39.0")
    add("84153", 0.30 * sg * (~female) * (age >= 50) * eff(0.10), "Z00.00")
    add("88305", 0.10 * sg * lob_mult * adult * (1 + 3 * D["oncology"]),
        lambda r: pick(r, ["C50.911", "R53.83", "Z00.00"], [0.3, 0.4, 0.3]))
    add("87798", 0.05 * sg * lob_mult, lambda r: pick(r, ["J06.9", "N39.0"], [0.6, 0.4]))

    # ---- P01 Vitamin D: appropriate (deficiency/CKD/osteoporosis) vs screening -------
    add("82306", (0.06 + 0.9 * D["ckd"]) * sg * adult * lob_mult,
        lambda r: pick(r, ["E55.9", "M81.0", "N18.30"], [0.5, 0.25, 0.25]))
    add("82306", 0.20 * sg * adult * lob_mult * eff(0.65), lambda r: pick(r, ["Z13.21", "Z00.00", "R53.83"], [0.4, 0.4, 0.2]))

    # ---- P02 HbA1c: diabetics up to 4/yr appropriate; over-testing removed by program --
    add("83036", D["diabetes"] * (3.2 + 1.3 * eff(0.60)) * sg, "E11.9")
    add("83036", (~D["diabetes"]) * adult * (0.16 + 0.10 * eff(0.55)) * sg,
        lambda r: pick(r, ["R73.03", "Z00.00"], [0.4, 0.6]))

    # ---- P05 Respiratory: large panel vs targeted panel -------------------------------
    add("87633", 0.022 * sr * lob_mult * eff(0.62), lambda r: pick(r, ["J06.9", "D84.9"], [0.88, 0.12]))
    add("87633", 0.25 * sr * D["immunocompromised"], "D84.9")
    add("87637", (0.06 + 0.04 * cap) * sr * lob_mult, "J06.9")

    # ---- Toxicology / P04 -------------------------------------------------------------
    sud = D["substance_use"]
    add("80307", sud * 6.0 * sg, lambda r: pick(r, ["F11.20", "Z79.891"], [0.6, 0.4]))
    add("G0480", sud * (0.8 + 1.4 * cap) * sg, "F11.20", kind="tox_def")
    add("G0483", sud * 2.2 * eff(0.55) * sg, "F11.20", kind="tox_def_22")

    # ---- Genetic & molecular / P06 -----------------------------------------------------
    gtrend = 1.08 ** yrs  # utilization trend in genomics
    crc_age = (age >= 45) & (age <= 75)
    add("81528", 0.11 * crc_age * gtrend, "Z12.11", kind="genetic")
    add("81528", 0.012 * ((age >= 30) & (age < 45) | (age > 85)) * gtrend * eff(0.75), "Z00.00", kind="genetic")
    preg_age = female & (age >= 18) & (age <= 44)
    add("81420", 0.035 * preg_age * gtrend, "Z34.90", kind="genetic")
    add("81162", (0.08 * D["oncology"] + 0.03 * D["family_history_cancer"]) * gtrend,
        lambda r: np.where(D["oncology"][r], "C50.911", "Z80.3"), kind="genetic")
    add("81162", 0.0025 * adult * gtrend * eff(0.70), "R53.83", kind="genetic")
    add("81519", 0.05 * D["oncology"] * female * gtrend, "C50.911", kind="genetic")
    add("0037U", 0.04 * D["oncology"] * gtrend, "C34.90", kind="genetic")
    add("81445", 0.03 * D["oncology"] * gtrend, "C34.90", kind="genetic")
    add("81445", 0.0012 * adult * gtrend * eff(0.70), "R53.83", kind="genetic")
    add("81225", (0.01 * D["cardiovascular"] + 0.008 * D["behavioral"]) * gtrend,
        lambda r: np.where(D["cardiovascular"][r], "I25.10", "F32.A"), kind="genetic")
    add("81225", 0.0035 * adult * gtrend * eff(0.70), "Z00.00", kind="genetic")
    add("81479", 0.0025 * adult * gtrend * eff(0.65), "R53.83", kind="genetic")

    cl_df = pd.concat(pieces, ignore_index=True)
    src = mm.iloc[cl_df["r"].to_numpy()].reset_index(drop=True)
    cl_df = pd.concat([cl_df.drop(columns="r"), src[["member_id", "client_id", "m_idx", "post", "capture", "lob"]]], axis=1)

    # service date within month
    first = pd.to_datetime([MONTHS[i].start_time for i in range(len(MONTHS))])
    dim = np.array([MONTHS[i].days_in_month for i in range(len(MONTHS))])
    midx = cl_df["m_idx"].to_numpy()
    cl_df["service_date"] = first[midx] + pd.to_timedelta(rng.integers(0, dim[midx]), unit="D")

    # P04: presumptive screen on same day for a share of definitive 22+ tests (higher post program)
    g83 = cl_df[cl_df["kind"] == "tox_def_22"]
    p_pres = 0.30 + 0.45 * g83["capture"].to_numpy()
    pres = g83[rng.random(len(g83)) < p_pres].copy()
    pres["cpt_code"] = "80307"
    pres["kind"] = "routine"
    cl_df = pd.concat([cl_df, pres], ignore_index=True)

    # ---- Lab assignment (in vs out of network) ----------------------------------------
    cat = cl_df["cpt_code"].map(TESTS.set_index("cpt_code")["test_category"]).to_numpy()
    oon_base = np.select(
        [cat == "Genetic & Molecular", cat == "Toxicology", cat == "Infectious Disease"], [0.20, 0.26, 0.06], 0.035
    )
    p_oon = oon_base * (1 - 0.6 * cl_df["capture"].to_numpy())
    is_oon = rng.random(len(cl_df)) < p_oon
    lab = np.empty(len(cl_df), dtype=object)
    gen = cat == "Genetic & Molecular"
    tox = cat == "Toxicology"
    # in-network
    lab[~is_oon & gen] = rng.choice(["L008", "L001", "L002"], (~is_oon & gen).sum(), p=[0.55, 0.25, 0.20])
    rest_in = ~is_oon & ~gen
    lab[rest_in] = rng.choice(["L001", "L002", "L003", "L004", "L005", "L006", "L007"], rest_in.sum(),
                              p=[0.28, 0.22, 0.12, 0.10, 0.08, 0.12, 0.08])
    # out-of-network
    lab[is_oon & gen] = rng.choice(["L009", "L010"], (is_oon & gen).sum(), p=[0.55, 0.45])
    lab[is_oon & tox] = "L011"
    rest_oon = is_oon & ~gen & ~tox
    lab[rest_oon] = "L012"
    cl_df["lab_id"] = lab

    # ---- P03 same-day duplicates (distinct claims, different lab) ---------------------
    routine = (cat == "Routine")
    p_dup = 0.016 * (1 - 0.7 * cl_df["capture"].to_numpy())
    dup = cl_df[routine & (rng.random(len(cl_df)) < p_dup)].copy()
    dup["lab_id"] = rng.choice(["L003", "L004", "L005", "L012"], len(dup))
    cl_df = pd.concat([cl_df, dup], ignore_index=True)

    # ---- Pricing ----------------------------------------------------------------------
    rate = cl_df["cpt_code"].map(TESTS.set_index("cpt_code")["in_network_rate"]).to_numpy()
    cat = cl_df["cpt_code"].map(TESTS.set_index("cpt_code")["test_category"]).to_numpy()
    yrs = cl_df["m_idx"].to_numpy() / 12.0
    unit_trend = np.where(cat == "Genetic & Molecular", 1.05, 1.02) ** yrs
    lob_fee = cl_df["lob"].map({"Commercial": 1.25, "Medicare Advantage": 1.0, "Medicaid": 0.82}).to_numpy()
    in_net = cl_df["lab_id"].map(LABS.set_index("lab_id")["in_network"]).to_numpy().astype(bool)
    oon_mult = np.where(in_net, 1.0, rng.uniform(1.5, 2.2, len(cl_df)))
    noise = rng.lognormal(0, 0.04, len(cl_df))
    allowed = rate * unit_trend * lob_fee * oon_mult * noise
    billed = allowed * rng.uniform(2.2, 3.5, len(cl_df))
    cost_share = cl_df["lob"].map({"Commercial": 0.16, "Medicare Advantage": 0.04, "Medicaid": 0.0}).to_numpy()
    paid = allowed * (1 - cost_share * rng.uniform(0.5, 1.5, len(cl_df)))
    cl_df["billed_amount"] = billed.round(2)
    cl_df["allowed_amount"] = allowed.round(2)
    cl_df["paid_amount"] = np.clip(paid, 0, None).round(2)
    cl_df["units"] = 1
    cl_df["place_of_service"] = rng.choice(["81", "11", "22"], len(cl_df), p=[0.62, 0.30, 0.08])

    # paid lag (days): lognormal, median ~21 days, long tail
    lag = np.clip(rng.lognormal(3.05, 0.65, len(cl_df)), 3, 240).astype(int)
    cl_df["paid_date"] = cl_df["service_date"] + pd.to_timedelta(lag, unit="D")
    cl_df = cl_df[cl_df["paid_date"] <= EXTRACT_DATE].copy()  # IBNR: not yet paid at extract

    cl_df = cl_df.sample(frac=1.0, random_state=7).reset_index(drop=True)
    cl_df["claim_id"] = ["CLM" + str(i).zfill(9) for i in range(1, len(cl_df) + 1)]
    cl_df["line_no"] = 1
    cl_df["claim_status"] = "P"
    return cl_df


def assign_batches(claims: pd.DataFrame) -> pd.DataFrame:
    """Claims arrive in weekly files per client; batch id = client + paid year-month + week."""
    pdt = pd.to_datetime(claims["paid_date"])
    claims["source_batch_id"] = (
        "B" + claims["client_id"].str[1:] + pdt.dt.strftime("%Y%m") + "W" + ((pdt.dt.day - 1) // 7 + 1).astype(str)
    )
    return claims


def add_reversals(rng, claims: pd.DataFrame) -> pd.DataFrame:
    """~1.2% of claims are reversed (adjudication correction) and re-paid at a corrected amount."""
    pick_idx = rng.random(len(claims)) < 0.012
    orig = claims[pick_idx]
    rev = orig.copy()
    rev["line_no"] = 2
    rev["claim_status"] = "R"
    for c in ["billed_amount", "allowed_amount", "paid_amount"]:
        rev[c] = -rev[c]
    rev["paid_date"] = rev["paid_date"] + pd.to_timedelta(rng.integers(10, 45, len(rev)), unit="D")
    rep = orig.copy()
    rep["line_no"] = 3
    rep["claim_status"] = "P"
    adj = rng.uniform(0.6, 0.95, len(rep))
    for c in ["allowed_amount", "paid_amount"]:
        rep[c] = (rep[c] * adj).round(2)
    rep["paid_date"] = rev["paid_date"]
    out = pd.concat([claims, rev, rep], ignore_index=True)
    out = out[out["paid_date"] <= EXTRACT_DATE]
    return out


def gl_control_totals(claims: pd.DataFrame) -> pd.DataFrame:
    """Finance books lab spend on a paid basis. GL = true net paid by client x paid month."""
    g = claims.assign(paid_month=claims["paid_date"].dt.to_period("M").dt.to_timestamp())
    gl = g.groupby(["client_id", "paid_month"], as_index=False)["paid_amount"].sum()
    gl = gl.rename(columns={"paid_amount": "gl_paid_amount"})
    gl["gl_paid_amount"] = gl["gl_paid_amount"].round(2)
    gl["gl_account"] = "5410-OUTPATIENT-LAB"
    return gl


def inject_dq_defects(rng, claims: pd.DataFrame, members: pd.DataFrame) -> tuple[pd.DataFrame, dict]:
    log = {}
    n = len(claims)
    # 1) a weekly batch for C02 never landed in the claims extract (paid in Mar 2026)
    lost = claims["source_batch_id"] == "B0220260" + "3W2"
    log["missing_batch_rows"] = int(lost.sum())
    log["missing_batch_paid"] = round(float(claims.loc[lost, "paid_amount"].sum()), 2)
    claims = claims[~lost].copy()
    # 2) exact duplicate lines (file re-sent)
    dups = claims.sample(frac=0.004, random_state=11)
    log["exact_duplicates"] = len(dups)
    # 3) invalid / missing CPT
    bad_cpt = rng.choice(claims.index, int(n * 0.0008), replace=False)
    claims.loc[bad_cpt[: len(bad_cpt) // 2], "cpt_code"] = None
    claims.loc[bad_cpt[len(bad_cpt) // 2:], "cpt_code"] = "8O053"  # letter O typo
    log["invalid_cpt"] = len(bad_cpt)
    # 4) unknown lab id
    bad_lab = rng.choice(claims.index, int(n * 0.0006), replace=False)
    claims.loc[bad_lab, "lab_id"] = "L099"
    log["unknown_lab"] = len(bad_lab)
    # 5) service date after paid date (keying error)
    bad_dt = rng.choice(claims.index[claims["line_no"] == 1], int(n * 0.0004), replace=False)
    claims.loc[bad_dt, "service_date"] = claims.loc[bad_dt, "paid_date"] + pd.Timedelta(days=30)
    log["service_after_paid"] = len(bad_dt)
    # 6) member not eligible on date of service: reassign to members whose span ended earlier
    #    (member termed in 2024 billed on a 2025+ service; client and dollars unchanged, so
    #     finance totals still tie - this is an eligibility problem, not a money problem)
    late_claims = claims.index[pd.to_datetime(claims["service_date"]) >= pd.Timestamp("2025-01-01")]
    bad_el = rng.choice(late_claims, int(n * 0.003), replace=False)
    termed = members[members["end_idx"] < 11]
    for cid, idx in claims.loc[bad_el].groupby("client_id").groups.items():
        pool = termed.loc[termed["client_id"] == cid, "member_id"].to_numpy()
        claims.loc[idx, "member_id"] = rng.choice(pool, len(idx))
    log["ineligible_member"] = len(bad_el)
    claims = pd.concat([claims, dups], ignore_index=True).sample(frac=1.0, random_state=3)
    return claims, log


def main(out_raw: Path = RAW, out_ref: Path = REF) -> None:
    rng = np.random.default_rng(SEED)
    out_raw.mkdir(parents=True, exist_ok=True)
    out_ref.mkdir(parents=True, exist_ok=True)

    members = build_members(rng)
    claims = generate_claims(rng, members)
    claims = add_reversals(rng, claims)
    claims = assign_batches(claims)
    gl = gl_control_totals(claims)  # finance truth before extract defects
    claims, dq_log = inject_dq_defects(rng, claims, members)

    elig = members.assign(
        eff_date=[MONTHS[i].start_time.date() for i in members["start_idx"]],
        term_date=[MONTHS[i].end_time.date() for i in members["end_idx"]],
    )
    elig["term_date"] = np.where(members["end_idx"] == len(MONTHS) - 1, pd.Timestamp("9999-12-31").date(), elig["term_date"])

    member_out = members[["member_id", "client_id", "birth_year", "sex"]].copy()
    elig_out = elig[["member_id", "client_id", "eff_date", "term_date"]].copy()
    # clinical conditions are NOT shipped as flags: policy logic must use claim diagnoses
    claims_out = claims[[
        "claim_id", "line_no", "claim_status", "client_id", "member_id", "service_date", "paid_date",
        "cpt_code", "units", "icd10_code", "lab_id", "place_of_service", "billed_amount",
        "allowed_amount", "paid_amount", "source_batch_id",
    ]].copy()
    claims_out["service_date"] = pd.to_datetime(claims_out["service_date"]).dt.date
    claims_out["paid_date"] = pd.to_datetime(claims_out["paid_date"]).dt.date

    con = duckdb.connect()
    for name, df in {
        "claims_lab": claims_out, "members": member_out, "eligibility": elig_out,
    }.items():
        con.register("df", df)
        con.execute(f"COPY (SELECT * FROM df) TO '{out_raw / (name + '.parquet')}' (FORMAT PARQUET)")
        con.unregister("df")
    gl.to_csv(out_raw / "finance_gl_lab_paid.csv", index=False)

    CLIENTS.drop(columns=["capture", "target_members"]).to_csv(out_ref / "clients.csv", index=False)
    CONTRACTS.to_csv(out_ref / "client_contracts.csv", index=False)
    TESTS.to_csv(out_ref / "test_catalog.csv", index=False)
    DX.to_csv(out_ref / "icd10_clinical_map.csv", index=False)
    GENETIC_INDICATIONS.to_csv(out_ref / "genetic_test_indications.csv", index=False)
    LABS.to_csv(out_ref / "lab_network.csv", index=False)

    pd.Series(dq_log).to_json(out_raw / "_injected_defects.json", indent=2)
    print(f"members={len(member_out):,} claim_lines={len(claims_out):,}")
    print("injected defects:", dq_log)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.parse_args()
    main()
