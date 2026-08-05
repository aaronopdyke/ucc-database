"""Build web map boundaries from the World Bank Official Boundaries dataset.

Downloads the ADM0/ADM1 geometry published in the World Bank Data Catalog
(dataset 0038272, CC BY 4.0) via the catalog API, joins GEM admin units onto
the polygons (layered: HASC code transform -> name variants -> manual
overrides), bakes UCC values into feature properties, simplifies with
topology preservation, and writes TopoJSONs plus committed match reports.

Run rarely (boundaries change slowly); outputs are committed. The UCC CSVs
must exist first (run build_ucc.py).
"""

from __future__ import annotations

import argparse
import difflib
import json
import re
import sys
import unicodedata
from pathlib import Path

import pandas as pd
import requests

import config
from overrides import (ADM0_FROM_ADM1, ADM0_OVERRIDES, ADM1_GROUP_OVERRIDES,
                       ADM1_OVERRIDES)

HEADERS = {"User-Agent": "UCC-database-build/1.0 (github.com/aaronopdyke/ucc-database)"}

# Candidate property names, checked in order (WB field naming varies by layer).
ADM0_CODE_FIELDS = ["ISO_A3", "WB_A3", "ADM0CD", "ISO3", "ADM0_A3"]
ADM0_NAME_FIELDS = ["NAM_0", "WB_NAME", "NAME_EN", "ADM0_NAME", "NAME"]
ADM1_KEY_FIELDS = ["ADM1CD_c", "ADM1CD_C", "adm1cd_c"]
ADM1_ISO3_FIELDS = ["ISO_A3", "SOVEREIGN", "WB_A3"]
ADM1_NAME_FIELDS = ["NAM_1", "ADM1NM", "NAME_1"]
# Name-variant columns from the additional-attributes CSV used for matching.
ADM1_NAME_VARIANT_COLS = [
    "NAM_1", "ADM1NM", "NAM_1_GAUL", "NAM_1_STAT", "NAM_1_SRCE",
    "NAM_1_NTVE", "NAM_1_WIKI", "P_NAME_1",
]


# --------------------------------------------------------------------------
# Download helpers
# --------------------------------------------------------------------------

def download(url: str, dest: Path, min_bytes: int = 1024) -> Path:
    if dest.exists() and dest.stat().st_size >= min_bytes:
        print(f"  cached: {dest.name} ({dest.stat().st_size:,} bytes)")
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    print(f"  downloading {url}")
    with requests.get(url, headers=HEADERS, stream=True, timeout=1800) as resp:
        resp.raise_for_status()
        tmp = dest.with_suffix(dest.suffix + ".part")
        with open(tmp, "wb") as fh:
            for chunk in resp.iter_content(chunk_size=1 << 20):
                fh.write(chunk)
        tmp.replace(dest)
    print(f"  saved {dest.name} ({dest.stat().st_size:,} bytes)")
    return dest


def geojson_manifest() -> dict[str, str]:
    """{file_name: download_link} from the dataset's GeoJSON manifest CSV."""
    path = download(config.WB_GEOJSON_MANIFEST_URL, config.WB_CACHE / "geojson_manifest.csv")
    rows = pd.read_csv(path, encoding="utf-8")
    return dict(zip(rows["file_name"], rows["download_link"]))


# --------------------------------------------------------------------------
# GeoJSON helpers
# --------------------------------------------------------------------------

def load_geojson_features(path: Path) -> list[dict]:
    if path.suffix == ".zip":
        sys.exit(f"{path} is a zip; extract the member first (see ensure_geometry)")
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    feats = data["features"] if data.get("type") == "FeatureCollection" else None
    if feats is None:
        sys.exit(f"{path}: not a FeatureCollection")
    return feats


def pick_field(props: dict, candidates: list[str], label: str) -> str:
    for name in candidates:
        if name in props:
            return name
    sys.exit(f"None of {candidates} found for {label}; available: {sorted(props)[:40]}")


def norm_name(value: object) -> str:
    if not isinstance(value, str):
        return ""
    decomposed = unicodedata.normalize("NFKD", value)
    stripped = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return re.sub(r"[^a-z0-9]", "", stripped.casefold())


# Generic administrative words that differ between GEM and WB name forms
# ("Jaemtlands Laen" vs "Jämtland", "Zagrebacka Zupanija" vs "Zagrebačka").
ADMIN_WORDS = {
    "province", "provincia", "region", "regiao", "district", "state", "estado",
    "governorate", "prefecture", "county", "department", "departamento",
    "oblast", "kraj", "krai", "kray", "zupanija", "laen", "lan", "division",
    "territory", "municipality", "wilaya", "gouvernorat", "muhafazah",
    "viloyati", "welayaty", "aimag", "voblasts", "voivodeship", "canton",
    "distrito", "comune", "atoll", "island", "islands", "city",
    "respublika", "rep", "republic", "okrug", "avtonomnyy", "avtonomnaya",
    "autonomous", "gorod", "federal", "maakond",
}
ARABIC_ARTICLES = ("al", "el", "ad", "as", "ash", "at")


def norm_name_loose(value: object) -> str:
    """Aggressive form: drop admin words/articles, fold Nordic digraphs."""
    if not isinstance(value, str):
        return ""
    decomposed = unicodedata.normalize("NFKD", value)
    stripped = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    words = re.split(r"[^a-zA-Z0-9]+", stripped.casefold())
    words = [w for w in words if w and w not in ADMIN_WORDS and w not in ARABIC_ARTICLES]
    text = "".join(words)
    for digraph, plain in (("ae", "a"), ("oe", "o"), ("ue", "u"), ("aa", "a")):
        text = text.replace(digraph, plain)
    return text


def canon_code(value: object) -> tuple[str, int] | None:
    """('DZ', 3) from 'DZ-03' / 'DZ003' / 'DZ.03' — for cross-scheme code joins."""
    if not isinstance(value, str):
        return None
    match = re.fullmatch(r"([A-Za-z]+)[\s._-]*0*(\d+)", value.strip())
    if not match:
        return None
    return (match.group(1).upper(), int(match.group(2)))


# --------------------------------------------------------------------------
# UCC lookups from the compiled CSVs
# --------------------------------------------------------------------------

def ucc_wide(df: pd.DataFrame, keys: list[str]) -> dict:
    """{key_tuple_or_iso: {occupancy: ucc_int}} from a long UCC frame."""
    out: dict = {}
    for row in df.itertuples(index=False):
        key = getattr(row, keys[0]) if len(keys) == 1 else tuple(getattr(row, k) for k in keys)
        ucc = getattr(row, config.UCC_ADJ_COL, None)
        if ucc is None or pd.isna(ucc):
            ucc = getattr(row, config.UCC_COL)
        if pd.isna(ucc):
            continue
        out.setdefault(key, {})[row.OCCUPANCY] = int(round(ucc))
    return out


# --------------------------------------------------------------------------
# Matching
# --------------------------------------------------------------------------

def match_adm0(features: list[dict], gem_adm0: pd.DataFrame) -> tuple[dict, list[dict]]:
    """Map GEM ISO3 -> ALL feature indices for that country; return report rows.

    The WB layer splits several countries across multiple features sharing one
    ISO code (Spain + Ceuta/Melilla, Portugal + Azores/Madeira, the UK + its
    base areas, Ukraine, ...). Every one of them must carry the country's UCC
    values, otherwise the map colours a fragment and greys out the mainland.
    """
    code_field = pick_field(features[0]["properties"], ADM0_CODE_FIELDS, "ADM0 code")
    name_field = pick_field(features[0]["properties"], ADM0_NAME_FIELDS, "ADM0 name")
    by_code: dict[str, list[int]] = {}
    for idx, feat in enumerate(features):
        code = str(feat["properties"].get(code_field, "")).strip().upper()
        if code and code != "-99":
            by_code.setdefault(code, []).append(idx)

    gem_countries = gem_adm0[["ID_0", "NAME_0"]].drop_duplicates()
    mapping: dict[str, list[int]] = {}
    methods: dict[str, str] = {}
    for row in gem_countries.itertuples(index=False):
        wb_code = ADM0_OVERRIDES.get(row.ID_0, row.ID_0)
        if wb_code in by_code:
            mapping[row.ID_0] = list(by_code[wb_code])
            methods[row.ID_0] = "override" if row.ID_0 in ADM0_OVERRIDES else "code"

    # Name fallback, restricted to features no code-matched country claimed.
    code_claimed = {idx for idxs in mapping.values() for idx in idxs}
    by_norm_name: dict[str, int] = {}
    for idx, feat in enumerate(features):
        if idx not in code_claimed:
            by_norm_name.setdefault(norm_name(feat["properties"].get(name_field)), idx)
    for row in gem_countries.itertuples(index=False):
        if row.ID_0 in mapping:
            continue
        hit = by_norm_name.get(norm_name(row.NAME_0))
        if hit is not None:
            mapping[row.ID_0] = [hit]
            methods[row.ID_0] = "name"

    report: list[dict] = []
    for row in gem_countries.itertuples(index=False):
        idxs = mapping.get(row.ID_0, [])
        primary = min(
            idxs,
            key=lambda i: (features[i]["properties"].get("WB_STATUS") != "Member State", i),
        ) if idxs else None
        props = features[primary]["properties"] if primary is not None else {}
        suffix = f" (+{len(idxs) - 1} features)" if len(idxs) > 1 else ""
        report.append({
            "SIDE": "gem" if idxs else "gem_only",
            "ID_0": row.ID_0, "GEM_NAME_0": row.NAME_0,
            "WB_CODE": props.get(code_field, ""),
            "WB_NAME": f"{props.get(name_field, '')}{suffix}",
            "MATCH_METHOD": methods.get(row.ID_0, "none"),
        })
    return mapping, report


ADM1_CODE_COLS = ["HASC_1", "P_CODE_1", "P_CODE_1_t", "P_CODE_1_c", "ADM1CD", "ADM1CD_t"]


def match_adm1(
    features: list[dict],
    addcols: pd.DataFrame,
    gem_adm1: pd.DataFrame,
) -> tuple[dict, list[dict]]:
    """Map (ID_0, ID_1) -> feature index, layered; return mapping + report rows.

    Stages (each 1:1-enforced, earlier stages win):
      override  manual fixups from overrides.py
      code      GEM ID_1 equals a WB code string (HASC/P-code/SALB, '.'->'-')
      code_num  same trailing number within the country ('DZ-03' vs 'DZ003'),
                accepted only when the number is unique on both sides
      name      normalized name equality across all WB name variants
      name_loose as above after dropping admin words/articles + digraph folding
    """
    props0 = features[0]["properties"]
    key_field = pick_field(props0, ADM1_KEY_FIELDS, "ADM1 primary key")
    iso_field = pick_field(props0, ADM1_ISO3_FIELDS, "ADM1 ISO3")
    name_field = pick_field(props0, ADM1_NAME_FIELDS, "ADM1 name")

    idx_col = "ADM1CD_c" if "ADM1CD_c" in addcols.columns else addcols.columns[0]
    add = addcols.set_index(idx_col)
    add = add[~add.index.duplicated()]

    by_key: dict[str, int] = {}
    by_country: dict[str, list[int]] = {}
    feat_codes: dict[int, set[str]] = {}
    feat_nums: dict[int, set[int]] = {}
    feat_strict: dict[int, set[str]] = {}
    feat_loose: dict[int, set[str]] = {}
    for idx, feat in enumerate(features):
        props = feat["properties"]
        key = str(props.get(key_field, "")).strip()
        iso = str(props.get(iso_field, "")).strip().upper()
        by_key[key] = idx
        by_country.setdefault(iso, []).append(idx)

        names = {props.get(name_field)}
        code_strings: set[str] = set()
        if key in add.index:
            extra = add.loc[key]
            for col in ADM1_CODE_COLS:
                if col in extra.index and isinstance(extra.get(col), str):
                    code_strings.add(extra.get(col))
            for col in ADM1_NAME_VARIANT_COLS:
                if col in extra.index:
                    names.add(extra.get(col))
        # WB names often embed alternates in parentheses: "Surt (sirte)".
        for name in [n for n in names if isinstance(n, str) and "(" in n]:
            names.update(part.strip(" )") for part in name.split("(") if part.strip(" )"))
        codes, nums = set(), set()
        for code in code_strings:
            cleaned = re.sub(r"[\s._]+", "-", code.strip().upper())
            codes.add(cleaned)
            canon = canon_code(code)
            if canon:
                nums.add(canon[1])
        feat_codes[idx] = codes
        feat_nums[idx] = nums
        feat_strict[idx] = {norm_name(n) for n in names if norm_name(n)}
        feat_loose[idx] = {norm_name_loose(n) for n in names if norm_name_loose(n)}

    mapping: dict[tuple[str, str], list[int]] = {}
    groups: dict[int, tuple[str, list[str]]] = {}  # feature idx -> (iso, gem ID_1s)
    group_members: set[tuple[str, str]] = set()
    methods: dict[tuple[str, str], str] = {}
    claimed: set[int] = set()
    gem_units = gem_adm1[["ID_0", "NAME_0", "ID_1", "NAME_1"]].drop_duplicates()
    gem_by_iso: dict[str, list] = {}
    for row in gem_units.itertuples(index=False):
        gem_by_iso.setdefault(row.ID_0, []).append(row)

    def claim(unit: tuple[str, str], idxs: list[int], method: str) -> None:
        # Group membership is non-exclusive: a unit aggregated onto an NDLSA
        # or region polygon may ALSO match its own country polygon directly.
        if unit in mapping or not idxs or any(i in claimed for i in idxs):
            return
        mapping[unit], methods[unit] = list(idxs), method
        claimed.update(idxs)

    def unresolved(row) -> bool:
        return (row.ID_0, row.ID_1) not in mapping

    # Stage -1: group overrides — one WB polygon painted with the aggregate of
    # several GEM units (WB ships coarser/older units than GEM there).
    for (iso, wb_key), gem_ids in ADM1_GROUP_OVERRIDES.items():
        idx = by_key.get(wb_key)
        if idx is None or idx in claimed:
            print(f"  WARNING: group override ({iso}, {wb_key}): WB feature unavailable")
            continue
        members = ([r.ID_1 for r in gem_by_iso.get(iso, [])]
                   if gem_ids == "*" else list(gem_ids))
        if not members:
            continue
        claimed.add(idx)
        groups[idx] = (iso, members)
        for gid in members:
            group_members.add((iso, gid))
            methods.setdefault((iso, gid), "group")

    def name_affinity(gem_name: str, idx: int) -> float:
        """Best fuzzy score of a GEM name against every WB name variant."""
        target = norm_name_loose(gem_name) or norm_name(gem_name)
        if not target:
            return 0.0
        best = 0.0
        for variant in feat_strict[idx] | feat_loose[idx]:
            if len(variant) >= 4 and len(target) >= 4 and (
                variant.startswith(target) or target.startswith(variant)
            ):
                best = max(best, 0.90)
            best = max(best, difflib.SequenceMatcher(None, target, variant).ratio())
        return best

    # Stage 0/1: overrides (may span several WB features for merged regions),
    # then exact code-string equality WITH name corroboration — HASC and ISO
    # abbreviations collide across schemes ('RU.SA' is Samara in HASC while
    # ISO 'RU-SA' is Sakha), so an exact code hit with an unrelated name is
    # rejected and left for the name stages.
    code_lookup: dict[str, list[int]] = {}
    for idx, codes in feat_codes.items():
        for code in codes:
            code_lookup.setdefault(code, []).append(idx)
    for row in gem_units.itertuples(index=False):
        unit = (row.ID_0, row.ID_1)
        override_val = ADM1_OVERRIDES.get(unit)
        if override_val is not None:
            keys = (override_val,) if isinstance(override_val, str) else tuple(override_val)
            idxs = [by_key[k] for k in keys if k in by_key]
            if len(idxs) != len(keys):
                print(f"  WARNING: override {unit} -> {keys}: some WB keys not found")
            claim(unit, idxs, "override")
            continue
        gem_code = re.sub(r"[\s._]+", "-", str(row.ID_1).strip().upper())
        hits = [i for i in code_lookup.get(gem_code, [])]
        if len(hits) == 1:
            aff = name_affinity(row.NAME_1, hits[0])
            siblings = by_country.get(ADM0_OVERRIDES.get(row.ID_0, row.ID_0), [])
            best_other = max(
                (name_affinity(row.NAME_1, j) for j in siblings if j != hits[0]),
                default=0.0,
            )
            # Require the code-matched feature to also be name-competitive:
            # 'RU-SA' (ISO: Sakha) equals Samara's HASC code, but Sakha's own
            # polygon scores far better on name — a collision, not a match.
            if aff >= 0.4 and aff >= best_other - 0.05:
                claim(unit, hits, "code")

    # Stage 2+ run per country. Name stages come BEFORE numeric-code matching:
    # several countries' WB P-codes number units alphabetically by WB's own
    # names, which disagrees with the official/ISO numbering GEM uses, so a
    # number-only join would silently attach the wrong polygons AND steal
    # features from units the name stages would have matched correctly.
    for iso, unit_rows in gem_units.groupby("ID_0", sort=False):
        wb_iso = ADM0_OVERRIDES.get(iso, iso)
        country_feats = by_country.get(wb_iso, [])

        # Stages 2 & 3: unambiguous name matching, strict then loose.
        for stage, norm_fn, feat_sets in (
            ("name", norm_name, feat_strict),
            ("name_loose", norm_name_loose, feat_loose),
        ):
            available = [i for i in country_feats if i not in claimed]
            pending = [r for r in unit_rows.itertuples(index=False) if unresolved(r)]
            tentative: dict[tuple[str, str], int] = {}
            for row in pending:
                target = norm_fn(row.NAME_1)
                if not target:
                    continue
                hits = [i for i in available if target in feat_sets[i]]
                if len(hits) == 1:
                    tentative[(row.ID_0, row.ID_1)] = hits[0]
            counts: dict[int, int] = {}
            for idx in tentative.values():
                counts[idx] = counts.get(idx, 0) + 1
            for unit, idx in tentative.items():
                if counts[idx] == 1:
                    claim(unit, [idx], stage)

        # Stage 4: unique trailing-number equality ('DZ-03' vs 'DZA003'),
        # accepted only with name corroboration (affinity >= 0.5) — numbering
        # schemes disagree in some countries (see comment above).
        available = [i for i in country_feats if i not in claimed]
        num_to_feat: dict[int, list[int]] = {}
        for i in available:
            for num in feat_nums[i]:
                num_to_feat.setdefault(num, []).append(i)
        pending = [r for r in unit_rows.itertuples(index=False) if unresolved(r)]
        gem_nums: dict[int, list] = {}
        for row in pending:
            canon = canon_code(row.ID_1)
            if canon:
                gem_nums.setdefault(canon[1], []).append(row)
        for num, rows in gem_nums.items():
            feats = num_to_feat.get(num, [])
            if len(rows) == 1 and len(feats) == 1 and feats[0] not in claimed:
                if name_affinity(rows[0].NAME_1, feats[0]) >= 0.5:
                    claim((rows[0].ID_0, rows[0].ID_1), feats, "code_num")

        # Stage 5: fuzzy, for transliteration tails ('Olgii'/'Olgiy') and
        # within-word suffixes ('Harju'/'Harjumaa'). Guarded three ways: high
        # score floor, clear margin over the runner-up, and reverse uniqueness.
        available = [i for i in country_feats if i not in claimed]
        pending = [r for r in unit_rows.itertuples(index=False) if unresolved(r)]
        tentative = {}
        for row in pending:
            target = norm_name_loose(row.NAME_1) or norm_name(row.NAME_1)
            if len(target) < 4:
                continue
            scored = sorted(((name_affinity(row.NAME_1, i), i) for i in available),
                            reverse=True)
            if scored and scored[0][0] >= 0.82 and (
                len(scored) == 1 or scored[0][0] - scored[1][0] >= 0.06
            ):
                tentative[(row.ID_0, row.ID_1)] = scored[0][1]
        counts = {}
        for idx in tentative.values():
            counts[idx] = counts.get(idx, 0) + 1
        for unit, idx in tentative.items():
            if counts[idx] == 1:
                claim(unit, [idx], "fuzzy")

    # Countries whose WB ADM1 layer is a single polygon: paint it with the
    # national aggregate instead of leaving the whole country gray.
    for iso, rows in gem_by_iso.items():
        feats = by_country.get(ADM0_OVERRIDES.get(iso, iso), [])
        if len(feats) != 1 or feats[0] in claimed:
            continue
        if any(not unresolved(r) for r in rows):
            continue
        idx = feats[0]
        claimed.add(idx)
        groups[idx] = (iso, [r.ID_1 for r in rows])
        for r in rows:
            group_members.add((iso, r.ID_1))
            methods[(iso, r.ID_1)] = "country"

    # National-average fallback: any WB polygon still unclaimed in a country
    # GEM covers is painted with the country aggregate, so no admin-1 area is
    # left without a value (marked 'natl' for transparency).
    fallback: dict[int, str] = {}
    for iso in gem_by_iso:
        for idx in by_country.get(ADM0_OVERRIDES.get(iso, iso), []):
            if idx not in claimed:
                claimed.add(idx)
                fallback[idx] = iso

    unit_group_idx = {(iso, gid): idx
                      for idx, (iso, members) in groups.items() for gid in members}
    report: list[dict] = []
    for row in gem_units.itertuples(index=False):
        unit = (row.ID_0, row.ID_1)
        idxs = mapping.get(unit, [])
        if not idxs and unit in unit_group_idx:
            idxs = [unit_group_idx[unit]]
        report.append({
            "SIDE": "gem" if idxs else "gem_only",
            "ID_0": row.ID_0, "GEM_ID_1": row.ID_1, "GEM_NAME_1": row.NAME_1,
            "WB_CODE": " + ".join(str(features[i]["properties"].get(key_field, ""))
                                  for i in idxs),
            "WB_NAME": " + ".join(str(features[i]["properties"].get(name_field, ""))
                                  for i in idxs),
            "MATCH_METHOD": methods.get(unit, "none"),
        })
    gem_isos = set(gem_units["ID_0"])
    matched_idx = {idx for idxs in mapping.values() for idx in idxs} | set(groups)
    for idx, feat in enumerate(features):
        props = feat["properties"]
        iso = str(props.get(iso_field, "")).strip().upper()
        if idx not in matched_idx and iso in gem_isos:
            report.append({
                "SIDE": "wb_only", "ID_0": iso, "GEM_ID_1": "", "GEM_NAME_1": "",
                "WB_CODE": props.get(key_field, ""), "WB_NAME": props.get(name_field, ""),
                "MATCH_METHOD": "country_avg" if idx in fallback else "none",
            })
    return mapping, groups, fallback, report


# --------------------------------------------------------------------------
# Baking + TopoJSON
# --------------------------------------------------------------------------

def bake_adm0(features: list[dict], mapping: dict[str, list[int]], ucc: dict) -> list[dict]:
    inverse = {idx: iso for iso, idxs in mapping.items() for idx in idxs}
    code_field = pick_field(features[0]["properties"], ADM0_CODE_FIELDS, "ADM0 code")
    name_field = pick_field(features[0]["properties"], ADM0_NAME_FIELDS, "ADM0 name")
    matched_names = {
        norm_name(features[idxs[0]]["properties"].get(name_field))
        for idxs in mapping.values()
    }
    baked = []
    for idx, feat in enumerate(features):
        iso = inverse.get(idx)
        name = feat["properties"].get(name_field) or ""
        props = {"name": name}
        if iso:
            props["id"] = iso
            for occ, value in ucc.get(iso, {}).items():
                props[f"UCC_{occ}"] = value
        else:
            # A no-data territory that shares its sovereign's name (WB labels
            # Réunion 'France'): disambiguate with its own ISO code.
            own_code = str(feat["properties"].get(code_field, "")).strip().upper()
            if own_code and own_code != "-99" and norm_name(name) in matched_names:
                props["name"] = f"{name} ({own_code})"
        baked.append({"type": "Feature", "properties": props, "geometry": feat["geometry"]})
    return baked


def group_ucc(gem_adm1: pd.DataFrame, iso: str, members: list[str],
              factor: float = 1.0) -> dict:
    """Area-weighted UCC per occupancy across several GEM ADM1 units."""
    sub = gem_adm1[(gem_adm1["ID_0"] == iso) & (gem_adm1["ID_1"].isin(members))]
    out = {}
    for occ, grp in sub.groupby("OCCUPANCY", observed=True):
        area = grp["TOTAL_AREA_SQM"].sum()
        if area > 0:
            out[f"UCC_{occ}"] = int(round(grp["BLDG_REPL_COST_USD"].sum() / area * factor))
    return out


def bake_adm1(features: list[dict], mapping: dict, groups: dict,
              fallback: dict, gem_adm1: pd.DataFrame, ucc: dict,
              ucc_country: dict, factor: float = 1.0) -> list[dict]:
    props0 = features[0]["properties"]
    iso_field = pick_field(props0, ADM1_ISO3_FIELDS, "ADM1 ISO3")
    name_field = pick_field(props0, ADM1_NAME_FIELDS, "ADM1 name")
    gem_names = {(r.ID_0, r.ID_1): r.NAME_1
                 for r in gem_adm1[["ID_0", "ID_1", "NAME_1"]].drop_duplicates().itertuples(index=False)}
    inverse = {idx: unit for unit, idxs in mapping.items() for idx in idxs}
    baked = []
    for idx, feat in enumerate(features):
        unit = inverse.get(idx)
        if unit:
            props = {"gid": unit[1], "iso3": unit[0], "name": gem_names.get(unit, "")}
            props.update({f"UCC_{occ}": v for occ, v in ucc.get(unit, {}).items()})
        elif idx in groups:
            iso, members = groups[idx]
            # 'units' makes the aggregation self-describing: the popup can say
            # what it spans and a data-only refresh can recompute the values.
            props = {
                "iso3": iso,
                "name": feat["properties"].get(name_field) or "",
                "units": ";".join(members),
            }
            props.update(group_ucc(gem_adm1, iso, members, factor))
        elif idx in fallback:
            iso = fallback[idx]
            props = {
                "iso3": iso,
                "name": feat["properties"].get(name_field) or "",
                "natl": 1,
            }
            props.update({f"UCC_{occ}": v for occ, v in ucc_country.get(iso, {}).items()})
        else:
            props = {
                "iso3": str(feat["properties"].get(iso_field, "")).strip().upper(),
                "name": feat["properties"].get(name_field) or "",
            }
        baked.append({"type": "Feature", "properties": props, "geometry": feat["geometry"]})
    return baked


def ring_area_deg2(ring: list) -> float:
    """Approximate ring area in cos-lat-corrected square degrees (shoelace)."""
    import math

    if len(ring) < 4:
        return 0.0
    area2 = 0.0
    lat_sum = 0.0
    for (x1, y1), (x2, y2) in zip(ring, ring[1:]):
        area2 += x1 * y2 - x2 * y1
        lat_sum += y1
    mean_lat = lat_sum / (len(ring) - 1)
    return abs(area2) / 2.0 * math.cos(math.radians(mean_lat))


def presnap(features: list[dict], grid: int, min_area: float = 0.0) -> list[dict]:
    """Snap coords to a grid near the target output resolution and drop the
    resulting consecutive duplicates BEFORE topology construction.

    Identical input coordinates round identically, so shared borders stay
    vertex-identical (no slivers), while vertex counts collapse enough that
    junction detection is cheap — without this it eats >14 GB of RAM on the
    full-resolution WB layers. Features too small for the grid keep their
    original geometry (microstates must not vanish from the map).
    """
    kx = grid / 360.0
    ky = grid / 180.0

    def snap_ring(ring: list) -> list | None:
        snapped: list[tuple[int, int]] = []
        for x, y in ring:
            q = (round(x * kx), round(y * ky))
            if not snapped or q != snapped[-1]:
                snapped.append(q)
        if len(snapped) > 1 and snapped[0] == snapped[-1]:
            snapped.pop()
        if len(snapped) < 3:
            return None
        pts = [[qx / kx, qy / ky] for qx, qy in snapped]
        pts.append(pts[0])
        return pts

    def snap_polygon(rings: list) -> list | None:
        outer = snap_ring(rings[0])
        if outer is None:
            return None  # part smaller than the grid (an islet): drop it
        holes = [h for h in (snap_ring(r) for r in rings[1:]) if h]
        return [outer] + holes

    result = []
    for feat in features:
        geom = feat["geometry"]
        if geom["type"] == "Polygon":
            polys = [p for p in [snap_polygon(geom["coordinates"])] if p]
            # A microstate smaller than the grid keeps its original outline.
            if not polys:
                polys = [geom["coordinates"]]
        elif geom["type"] == "MultiPolygon":
            polys = [p for p in (snap_polygon(q) for q in geom["coordinates"]) if p]
            if min_area > 0 and polys:
                # Cartographic islet filter: thousands of small island rings
                # dominate file size at world scale. Always keep the largest
                # part so archipelago units never vanish.
                big = [p for p in polys if ring_area_deg2(p[0]) >= min_area]
                polys = big or [max(polys, key=lambda p: ring_area_deg2(p[0]))]
            # Keep the largest original part if the whole feature collapsed.
            if not polys:
                polys = [max(geom["coordinates"], key=lambda p: len(p[0]))]
        else:
            result.append(feat)
            continue
        new_geom = (
            {"type": "Polygon", "coordinates": polys[0]}
            if len(polys) == 1
            else {"type": "MultiPolygon", "coordinates": polys}
        )
        result.append({"type": "Feature", "properties": feat["properties"],
                       "geometry": new_geom})
    return result


def write_topojson(features: list[dict], dest: Path, eps: float, budget: int,
                   grid: int = 30_000, min_area: float = 0.0) -> None:
    import topojson
    from shapely.geometry import mapping, shape

    n_before = len(features)
    features = presnap(features, grid, min_area)
    assert len(features) == n_before, "presnap must never drop features"

    # Simplify per feature (Douglas-Peucker, C-speed) BEFORE topology
    # construction: junction detection on the full vertex load needs >14 GB of
    # RAM. Neighbouring polygons are simplified independently, so shared
    # borders can drift by up to the tolerance — the map strokes every border
    # in near-background colour, which masks those micro-offsets.
    def dp(tol: float) -> list[dict]:
        out = []
        for feat in features:
            geometry = feat["geometry"]
            try:
                simple = shape(geometry).simplify(tol, preserve_topology=True)
                if not simple.is_empty:
                    geometry = mapping(simple)
            except Exception:
                pass  # keep the original geometry for pathological rings
            out.append({"type": "Feature", "properties": feat["properties"],
                        "geometry": geometry})
        return out

    tol = eps
    for attempt in range(5):
        simplified = dp(tol)
        size_in = sum(len(json.dumps(f["geometry"])) for f in simplified)
        print(f"  building topology for {dest.name} ({len(features):,} features, "
              f"dp={tol:.4f}, input {size_in / 1e6:.0f} MB) ...", flush=True)
        topo = topojson.Topology(
            {"type": "FeatureCollection", "features": simplified},
            prequantize=100_000, shared_coords=True,
        )
        text = topo.to_json()
        size = len(text.encode("utf-8"))
        print(f"  {dest.name}: {size:,} bytes at tolerance {tol:.4f}", flush=True)
        if size <= budget:
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(text, encoding="utf-8")
            return
        tol *= max(1.5, (size / budget) ** 0.8)
    sys.exit(f"{dest.name} exceeds budget {budget:,} bytes even after {attempt + 1} attempts")


# --------------------------------------------------------------------------
# Geometry acquisition
# --------------------------------------------------------------------------

ADM0_LAYER = "World Bank Official Boundaries - Admin 0.geojson"
ADM1_LAYER = "World Bank Official Boundaries - Admin 1.geojson"
NDLSA_LAYER = "World Bank Official Boundaries - NDLSA.geojson"


def ensure_geometry() -> tuple[Path, Path, Path, dict]:
    """Download (or reuse cached) ADM0/ADM1/NDLSA GeoJSON; return paths + provenance."""
    config.WB_CACHE.mkdir(parents=True, exist_ok=True)
    manifest = geojson_manifest()
    for layer in (ADM0_LAYER, ADM1_LAYER, NDLSA_LAYER):
        if layer not in manifest:
            sys.exit(f"Manifest is missing '{layer}'; has: {sorted(manifest)}")
    adm0_path = download(manifest[ADM0_LAYER], config.WB_CACHE / "wb_admin0.geojson",
                         min_bytes=1_000_000)
    adm1_path = download(manifest[ADM1_LAYER], config.WB_CACHE / "wb_admin1.geojson",
                         min_bytes=1_000_000)
    ndlsa_path = download(manifest[NDLSA_LAYER], config.WB_CACHE / "wb_ndlsa.geojson",
                          min_bytes=10_000)
    provenance = {
        "dataset": config.WB_DATASET_WEB,
        "manifest": config.WB_GEOJSON_MANIFEST_URL,
        "layers": [ADM0_LAYER, ADM1_LAYER, NDLSA_LAYER],
        "license": config.WB_LICENSE,
    }
    return adm0_path, adm1_path, ndlsa_path, provenance


def ndlsa_as_features(path: Path) -> list[dict]:
    """NDLSA polygons reshaped to slot into both admin layers.

    WB carves these out of every country polygon, so unhandled they are holes
    in the map. Each gets a stable synthetic key (NDL_<normalized name>) that
    overrides can reference; unmatched ones render gray with their name.
    """
    features = []
    for feat in load_geojson_features(path):
        name = str(feat["properties"].get("NAM_0") or "").strip()
        if not name:
            continue
        features.append({
            "type": "Feature",
            "properties": {
                "ADM1CD_c": f"NDL_{norm_name(name)}",
                "ISO_A3": "",
                "NAM_0": name,
                "NAM_1": name,
                "WB_STATUS": "Non-determined legal status area",
            },
            "geometry": feat["geometry"],
        })
    return features


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--download-only", action="store_true",
                        help="fetch the WB files into cache/ and exit")
    parser.add_argument("--match-only", action="store_true",
                        help="run the joins and write match reports, skip TopoJSON")
    parser.add_argument("--eps-adm0", type=float, default=0.01)
    parser.add_argument("--eps-adm1", type=float, default=0.02)
    args = parser.parse_args()

    adm0_path, adm1_path, ndlsa_path, provenance = ensure_geometry()
    if args.download_only:
        print("Download-only mode: done.")
        return

    gem_adm0 = pd.read_csv(config.DATA_DIR / "ucc_adm0.csv")
    gem_adm1 = pd.read_csv(config.DATA_DIR / "ucc_adm1.csv")
    addcols = pd.read_csv(
        download(config.WB_ADM1_ADDCOLS_URL, config.WB_CACHE / "adm1_additional_columns.csv"),
        encoding="utf-8",
    )

    print("Loading WB geometry ...")
    adm0_features = load_geojson_features(adm0_path)
    adm1_features = load_geojson_features(adm1_path)
    ndlsa = ndlsa_as_features(ndlsa_path)
    adm0_features.extend(ndlsa)
    adm1_features.extend(ndlsa)
    print(f"  appended {len(ndlsa)} NDLSA polygons to both layers")
    print(f"  ADM0 features: {len(adm0_features):,}; ADM1 features: {len(adm1_features):,}")
    print(f"  ADM0 fields: {sorted(adm0_features[0]['properties'])}")
    print(f"  ADM1 fields: {sorted(adm1_features[0]['properties'])}")

    map0, report0 = match_adm0(adm0_features, gem_adm0)
    map1, groups1, fallback1, report1 = match_adm1(adm1_features, addcols, gem_adm1)

    # Countries with no WB ADM0 polygon whose geometry exists as an ADM1
    # feature (e.g. Taiwan): promote it to a country feature, appended last so
    # it repaints its host country's polygon on the map.
    adm1_key_field = pick_field(adm1_features[0]["properties"], ADM1_KEY_FIELDS,
                                "ADM1 primary key")
    for iso, adm1_key in ADM0_FROM_ADM1.items():
        if iso in map0:
            continue
        hit = next((f for f in adm1_features
                    if str(f["properties"].get(adm1_key_field, "")).strip() == adm1_key), None)
        names = gem_adm0.loc[gem_adm0["ID_0"] == iso, "NAME_0"]
        if hit is None or names.empty:
            print(f"  WARNING: ADM0_FROM_ADM1 {iso} -> {adm1_key}: not found")
            continue
        adm0_features.append({
            "type": "Feature",
            "properties": {"ISO_A3": iso, "NAM_0": names.iloc[0]},
            "geometry": hit["geometry"],
        })
        map0[iso] = [len(adm0_features) - 1]
        for row in report0:
            if row["ID_0"] == iso:
                row.update(SIDE="gem", WB_CODE=adm1_key,
                           WB_NAME=hit["properties"].get("NAM_1", ""),
                           MATCH_METHOD="adm1_geometry")

    n_gem0 = gem_adm0["ID_0"].nunique()
    gem_units = gem_adm1[["ID_0", "ID_1"]].drop_duplicates()
    print(f"ADM0 matched {len(map0)}/{n_gem0} GEM countries")
    group_units = {(iso, gid) for iso, members in groups1.values() for gid in members}
    n_resolved = len(set(map1) | group_units)
    n_grouped = len(group_units)
    print(f"ADM1 resolved {n_resolved}/{len(gem_units)} GEM units "
          f"({100 * n_resolved / len(gem_units):.1f}%) — "
          f"{len(map1)} direct, {n_grouped} via {len(groups1)} aggregated polygons; "
          f"{len(fallback1)} polygons painted with national averages")
    method_counts = pd.Series([r["MATCH_METHOD"] for r in report1
                               if r["SIDE"] == "gem"]).value_counts().to_dict()
    print(f"ADM1 match methods: {method_counts}")

    pd.DataFrame(report0).to_csv(config.DATA_DIR / "boundary_match_report_adm0.csv",
                                 index=False, encoding="utf-8", lineterminator="\n")
    pd.DataFrame(report1).to_csv(config.DATA_DIR / "boundary_match_report_adm1.csv",
                                 index=False, encoding="utf-8", lineterminator="\n")

    if args.match_only:
        print("Match-only mode: reports written, skipping TopoJSON build.")
        return

    meta_path = config.DATA_DIR / "source_metadata.json"
    meta_now = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else {}
    cpi_factor = float((meta_now.get("cost_adjustment") or {}).get("factor", 1.0))

    ucc0 = ucc_wide(gem_adm0, ["ID_0"])
    ucc1 = ucc_wide(gem_adm1, ["ID_0", "ID_1"])
    baked0 = bake_adm0(adm0_features, map0, ucc0)
    baked1 = bake_adm1(adm1_features, map1, groups1, fallback1, gem_adm1, ucc1,
                       ucc0, cpi_factor)

    write_topojson(baked0, config.DOCS_DATA_DIR / "boundaries_adm0.topojson",
                   args.eps_adm0, config.ADM0_TOPOJSON_BUDGET, min_area=0.02)
    write_topojson(baked1, config.DOCS_DATA_DIR / "boundaries_adm1.topojson",
                   args.eps_adm1, config.ADM1_TOPOJSON_BUDGET, min_area=0.002)

    meta_path = config.DATA_DIR / "source_metadata.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else {}
    meta["world_bank_boundaries"] = provenance | {
        "adm0_matched": f"{len(map0)}/{n_gem0}",
        "adm1_matched": f"{n_resolved}/{len(gem_units)}",
        "adm1_match_methods": method_counts,
    }
    meta_path.write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
    print("Boundary build complete.")


if __name__ == "__main__":
    main()
