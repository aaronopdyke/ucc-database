# UCC Database

Unit construction cost (UCC) values for 215 countries and territories, compiled
from the [GEM Global Exposure Model](https://github.com/gem/global_exposure_model)
into three lookup lists and an interactive world map.

**Browse the data and map: <https://aaronopdyke.github.io/ucc-database/>**

UCC is the average **replacement cost per square metre of built floor area
(USD/m²)** — structural + non-structural components, excluding contents — as
modelled in GEM's national exposure models:
`UCC = BLDG_REPL_COST_USD / TOTAL_AREA_SQM`.

## The three lists (`data/`)

| File | Grain | Rows |
|---|---|---|
| [`ucc_adm0.csv`](data/ucc_adm0.csv) | country × occupancy | ~860 |
| [`ucc_adm1.csv`](data/ucc_adm1.csv) | country × admin-1 region × occupancy | ~14,000 |
| [`ucc_taxonomy.csv`](data/ucc_taxonomy.csv) | country × occupancy × GEM taxonomy × settlement | ~36,000 |

Columns: `REGION, ID_0, NAME_0, [ID_1, NAME_1,] OCCUPANCY, [MACRO_TAXONOMY,
TAXONOMY, SETTLEMENT,] BUILDINGS, TOTAL_AREA_SQM, BLDG_REPL_COST_USD,
UCC_USD_PER_SQM, GEM_COMMIT`.

- `OCCUPANCY`: `RES`, `COM`, `IND`, plus a computed `TOTAL` row
  (Σ cost ÷ Σ area). New Zealand reports non-residential stock as `NONRES`.
- `ID_0` is ISO3; `ID_1` is GEM's ISO 3166-2-style admin-1 code.
- `TAXONOMY` is a [GEM Building Taxonomy](https://github.com/gem/gem_taxonomy)
  string. Rows GEM subdivides by HAZUS sub-occupancy (USA/Canada/US
  territories) are aggregated to one row per taxonomy string.
- `GEM_COMMIT` pins every value to the exact GEM source commit
  (see `data/source_metadata.json`).

## The map

The [site](https://aaronopdyke.github.io/ucc-database/) plots UCC as a world
choropleth with a **country (ADM0) ⇄ admin-1 (ADM1) toggle** and an occupancy
selector, plus a per-country browser with sortable ADM1/taxonomy tables and CSV
downloads. Boundaries are the
[World Bank Official Boundaries](https://datacatalog.worldbank.org/search/dataset/0038272)
(CC BY 4.0), pulled from the World Bank Data Catalog and simplified to
TopoJSON. GEM admin units are joined to World Bank polygons by layered
code/name matching plus reviewed overrides; every join decision is recorded in
[`data/boundary_match_report_adm1.csv`](data/boundary_match_report_adm1.csv)
(unmatched units render gray on the map but are always in the CSVs).

## Rebuilding

```powershell
py -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt

py scripts/run_all.py                    # refresh data from GEM main (no geometry rebuild)
py scripts/run_all.py --with-boundaries  # also re-download WB boundaries + rebuild TopoJSON
py -m http.server 8000 --directory docs  # preview the site
```

The pipeline sparse-checkouts only the GEM summary CSVs (~7 MB of a ~600 MB
repo), validates against known values, and regenerates `data/` and
`docs/data/`. A GitHub Action ([`refresh-data.yml`](.github/workflows/refresh-data.yml))
runs monthly and opens a PR when GEM's data changes.

## License & citation

**CC BY-NC-SA 4.0** (same terms as the GEM source data) — see [LICENSE](LICENSE).
Boundary geometry © World Bank, CC BY 4.0.

If you use these values, cite the underlying model:

> Yepes-Estrada, C., Calderon, A., Costa, C., Crowley, H., Dabbeek, J., Hoyos,
> M.C., Martins, L., Paul, N., Rao, A., Silva, V. (2023). Global Building
> Exposure Model for Earthquake Risk Assessment. *Earthquake Spectra*, 39(4).
> <https://doi.org/10.1177/87552930231194048>

and this compilation as: Opdyke, A. *UCC Database — unit construction costs
from the GEM Global Exposure Model.*
<https://aaronopdyke.github.io/ucc-database/>
