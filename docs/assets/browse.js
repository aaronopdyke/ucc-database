/* Country browser: stat tiles + sortable/filterable ADM1 and taxonomy tables. */

(async function () {
  "use strict";

  const fmtCompact = new Intl.NumberFormat("en-US", {
    notation: "compact",
    maximumFractionDigits: 1,
  });
  const fmtUcc = new Intl.NumberFormat("en-US", { maximumFractionDigits: 1 });
  const OCC_ORDER = ["TOTAL", "RES", "COM", "IND", "NONRES"];

  const [index, meta] = await Promise.all([
    UCC.json("data/countries_index.json"),
    UCC.json("data/meta.json").catch(() => ({})),
  ]);
  const costYear = meta.cost_year ? ` (${meta.cost_year} USD)` : "";
  const costYearShort = meta.cost_year ? `, ${meta.cost_year} USD` : "";
  const UCC_KEY = "UCC_ADJ_USD_PER_SQM";
  const uccOf = (row) => (row[UCC_KEY] != null ? row[UCC_KEY] : row.UCC_USD_PER_SQM);
  const select = document.getElementById("country-select");
  const regions = [...new Set(index.map((c) => c.region))].sort();
  for (const region of regions) {
    const group = document.createElement("optgroup");
    group.label = region;
    for (const c of index.filter((x) => x.region === region)) {
      const opt = document.createElement("option");
      opt.value = c.iso3;
      opt.textContent = c.name;
      group.appendChild(opt);
    }
    select.appendChild(group);
  }

  const toObjects = (table) =>
    table.rows.map((row) =>
      Object.fromEntries(table.columns.map((col, i) => [col, row[i]]))
    );

  /* Generic sortable table. cols: [{label, key, num, fmt, cls}] */
  function makeTable(el, cols, allRows, countEl) {
    let rows = allRows;
    let sortKey = null;
    let sortDir = 1;

    function render() {
      const data = [...rows];
      if (sortKey != null) {
        const col = cols.find((c) => c.key === sortKey);
        data.sort((a, b) => {
          const va = a[sortKey], vb = b[sortKey];
          if (va == null) return 1;
          if (vb == null) return -1;
          return col.num
            ? (va - vb) * sortDir
            : String(va).localeCompare(String(vb)) * sortDir;
        });
      }
      const head =
        "<thead><tr>" +
        cols
          .map(
            (c) =>
              `<th class="${c.num ? "num" : ""}" data-key="${c.key}">${c.label}` +
              (sortKey === c.key ? ` <span class="arrow">${sortDir > 0 ? "▲" : "▼"}</span>` : "") +
              "</th>"
          )
          .join("") +
        "</tr></thead>";
      const body =
        "<tbody>" +
        data
          .map(
            (row) =>
              "<tr>" +
              cols
                .map((c) => {
                  const v = row[c.key];
                  const text = v == null ? "–" : c.fmt ? c.fmt(v) : v;
                  const title = c.title && v != null ? ` title="${c.title(v)}"` : "";
                  return `<td class="${[c.num ? "num" : "", c.cls || ""].join(" ").trim()}"${title}>${text}</td>`;
                })
                .join("") +
              "</tr>"
          )
          .join("") +
        "</tbody>";
      el.innerHTML = head + body;
      if (countEl) countEl.textContent = `${data.length} rows`;
      el.querySelectorAll("th").forEach((th) =>
        th.addEventListener("click", () => {
          const key = th.dataset.key;
          if (sortKey === key) sortDir *= -1;
          else { sortKey = key; sortDir = 1; }
          render();
        })
      );
    }

    render();
    return {
      setRows(next) { rows = next; render(); },
    };
  }

  function fillOccSelect(el, present, withAll, preferred) {
    el.innerHTML = "";
    const opts = [];
    if (withAll) opts.push(["", "All"]);
    for (const occ of OCC_ORDER.filter((o) => present.includes(o))) {
      opts.push([occ, UCC.OCC_LABELS[occ]]);
    }
    for (const [value, label] of opts) {
      const opt = document.createElement("option");
      opt.value = value;
      opt.textContent = label;
      el.appendChild(opt);
    }
    el.value = opts.some(([v]) => v === preferred) ? preferred : opts[0][0];
  }

  const numCols = (keys) => [
    { label: "Buildings", key: "BUILDINGS", num: true, fmt: (v) => UCC.fmtInt.format(v) },
    {
      label: "Floor area (m²)", key: "TOTAL_AREA_SQM", num: true,
      fmt: (v) => fmtCompact.format(v), title: (v) => UCC.fmtInt.format(v) + " m²",
    },
    {
      label: "Repl. cost (USD)", key: "BLDG_REPL_COST_USD", num: true,
      fmt: (v) => "$" + fmtCompact.format(v), title: (v) => "$" + UCC.fmtInt.format(v),
    },
    { label: `UCC (USD/m²${costYearShort})`, key: UCC_KEY, num: true, fmt: (v) => fmtUcc.format(v) },
    { label: "Contents (USD/m²)", key: "CONTENTS_USD_PER_SQM", num: true, fmt: (v) => fmtUcc.format(v) },
  ];

  /* ---- inline country map (ADM1 choropleth) ---- */
  const mapOcc = document.getElementById("map-occ");
  const mapNote = document.getElementById("map-note");
  const mapEl = document.getElementById("country-map");
  let countryMap = null, mapReady = null, adm1GeoPromise = null;

  function loadAdm1Features() {
    if (!adm1GeoPromise) {
      adm1GeoPromise = UCC.json("data/boundaries_adm1.topojson").then((topo) => {
        const obj = topo.objects[Object.keys(topo.objects)[0]];
        return topojson.feature(topo, obj).features;
      });
    }
    return adm1GeoPromise;
  }

  /* Per-country dynamic scale: a spread under $50 renders as one uniform
     class (no false variation); larger spreads stretch the ramp across the
     country's own range with rounded equal-interval breaks. */
  let currentFeatures = [];

  function countryScale(features, occ) {
    const vals = features
      .map((f) => f.properties[`UCC_${occ}`])
      .filter((v) => v != null);
    if (!vals.length) return null;
    const min = Math.min(...vals), max = Math.max(...vals);
    if (max - min < 50) return { uniform: true, min, max };
    const target = 7;
    const rawStep = (max - min) / target;
    const mag = Math.pow(10, Math.floor(Math.log10(rawStep)));
    const step = [1, 2, 2.5, 5, 10].map((m) => m * mag).find((v) => v >= rawStep) || rawStep;
    const breaks = [];
    for (let b = Math.ceil(min / step) * step; b < max && breaks.length < target - 1; b += step) {
      breaks.push(Math.round(b));
    }
    if (!breaks.length) return { uniform: true, min, max };
    return { breaks, min, max };
  }

  function scaleColors(n) {
    if (n <= 1) return [UCC.RAMP[3]];
    return Array.from({ length: n }, (_, i) =>
      UCC.RAMP[Math.round((i * (UCC.RAMP.length - 1)) / (n - 1))]);
  }

  function countryFillExpr(occ, scale) {
    if (!scale) return UCC.fillExpr(occ);
    if (scale.uniform) {
      return ["case", ["has", `UCC_${occ}`], UCC.RAMP[3], UCC.NODATA];
    }
    const colors = scaleColors(scale.breaks.length + 1);
    const stepExpr = ["step", ["get", `UCC_${occ}`], colors[0]];
    scale.breaks.forEach((b, i) => stepExpr.push(b, colors[i + 1]));
    return ["case", ["has", `UCC_${occ}`], stepExpr, UCC.NODATA];
  }

  function renderMapLegend(scale) {
    const el = document.getElementById("map-legend");
    const fmt = (v) => v.toLocaleString("en-US");
    if (scale && scale.uniform) {
      el.innerHTML =
        `<span class="bin"><span class="swatch" style="background:${UCC.RAMP[3]}"></span>` +
        `<span>≈ uniform (${fmt(Math.round(scale.min))}–${fmt(Math.round(scale.max))} USD/m²)</span></span>` +
        `<span class="bin"><span class="swatch" style="background:${UCC.NODATA}"></span><span>No data</span></span>`;
      return;
    }
    if (scale && scale.breaks) {
      const colors = scaleColors(scale.breaks.length + 1);
      const label = (i) =>
        i === 0 ? `< ${fmt(scale.breaks[0])}`
        : i === scale.breaks.length ? `≥ ${fmt(scale.breaks[i - 1])}`
        : `${fmt(scale.breaks[i - 1])}–${fmt(scale.breaks[i])}`;
      el.innerHTML =
        `<span class="legend-title">country scale</span>` +
        colors.map((c, i) =>
          `<span class="bin"><span class="swatch" style="background:${c}"></span><span>${label(i)}</span></span>`
        ).join("") +
        `<span class="bin"><span class="swatch" style="background:${UCC.NODATA}"></span><span>No data</span></span>`;
      return;
    }
    el.innerHTML = UCC.RAMP.map(
      (color, i) =>
        `<span class="bin"><span class="swatch" style="background:${color}"></span><span>${UCC.binLabel(i)}</span></span>`
    ).join("") +
      `<span class="bin"><span class="swatch" style="background:${UCC.NODATA}"></span><span>No data</span></span>`;
  }

  function applyCountryScale() {
    if (!countryMap || !countryMap.getLayer("c-fill")) return;
    const scale = countryScale(currentFeatures, mapOcc.value);
    countryMap.setPaintProperty("c-fill", "fill-color", countryFillExpr(mapOcc.value, scale));
    renderMapLegend(scale);
  }

  function ensureMap() {
    if (mapReady) return mapReady;
    countryMap = new maplibregl.Map({
      container: "country-map",
      style: UCC.basemapStyle(),
      attributionControl: false,
    });
    countryMap.addControl(new maplibregl.NavigationControl({ showCompass: false }), "top-right");
    countryMap.addControl(new maplibregl.AttributionControl({
      compact: true,
      customAttribution: "GEM CC BY-NC-SA 4.0 · World Bank CC BY 4.0",
    }));
    countryMap.dragRotate.disable();
    countryMap.touchZoomRotate.disableRotation();
    mapReady = new Promise((resolve) =>
      countryMap.loaded() ? resolve() : countryMap.once("load", resolve)
    ).then(() => {
      countryMap.addSource("c", {
        type: "geojson",
        data: { type: "FeatureCollection", features: [] },
      });
      countryMap.addLayer({
        id: "c-fill", type: "fill", source: "c",
        paint: { "fill-color": UCC.fillExpr(mapOcc.value), "fill-opacity": 0.78 },
      });
      countryMap.addLayer({
        id: "c-line", type: "line", source: "c",
        paint: { "line-color": "#fcfcfb", "line-width": 0.8 },
      });
      const pop = new maplibregl.Popup({ closeButton: false, closeOnClick: false });
      countryMap.on("mousemove", "c-fill", (e) => {
        const p = e.features[0].properties;
        const v = p[`UCC_${mapOcc.value}`];
        countryMap.getCanvas().style.cursor = "pointer";
        pop
          .setLngLat(e.lngLat)
          .setHTML(
            `<strong>${p.name || ""}</strong><br>` +
            `${v != null ? UCC.fmtUcc.format(v) + " USD/m²" : "No data"}` +
            `${p.units ? `<br><span style="color:#898781">aggregate of ${p.units.split(";").length} regions</span>` : ""}` +
            `${p.natl ? '<br><span style="color:#898781">national average</span>' : ""}`
          )
          .addTo(countryMap);
      });
      countryMap.on("mouseleave", "c-fill", () => {
        countryMap.getCanvas().style.cursor = "";
        pop.remove();
      });
      renderMapLegend(null);
    });
    return mapReady;
  }

  function bboxOf(features) {
    let minX = 180, minY = 90, maxX = -180, maxY = -90;
    const scan = (c) => {
      if (typeof c[0] === "number") {
        if (c[0] < minX) minX = c[0];
        if (c[0] > maxX) maxX = c[0];
        if (c[1] < minY) minY = c[1];
        if (c[1] > maxY) maxY = c[1];
      } else c.forEach(scan);
    };
    features.forEach((f) => f.geometry && scan(f.geometry.coordinates));
    return [[minX, minY], [maxX, maxY]];
  }

  async function showCountryMap(iso3) {
    const [features] = await Promise.all([loadAdm1Features(), ensureMap()]);
    const mine = features.filter((f) => f.properties.iso3 === iso3);
    if (!mine.length) {
      mapEl.style.display = "none";
      mapNote.hidden = false;
      return;
    }
    mapEl.style.display = "";
    mapNote.hidden = true;
    currentFeatures = mine;
    countryMap.getSource("c").setData({ type: "FeatureCollection", features: mine });
    applyCountryScale();
    countryMap.resize();
    countryMap.fitBounds(bboxOf(mine), { padding: 28, duration: 0, maxZoom: 8 });
  }

  mapOcc.addEventListener("change", applyCountryScale);

  /* Plain-language descriptions of GEM macro-taxonomy groups. */
  const MACRO_INFO = {
    "MUR": ["Unreinforced masonry", "Brick, concrete-block or stone walls without steel reinforcement."],
    "MR|MCF": ["Reinforced / confined masonry", "Masonry strengthened with steel bars, or confined by reinforced-concrete tie-columns and tie-beams."],
    "ADO|ST|E": ["Adobe, stone or earth", "Sun-dried mud brick (adobe), rubble or dressed stone, and rammed-earth construction."],
    "CR-": ["Concrete, lower seismic design", "Reinforced concrete frames or walls with little or no seismic detailing (non-ductile / low design code)."],
    "CR+": ["Concrete, higher seismic design", "Reinforced concrete designed to moderate or high seismic codes, with ductile detailing."],
    "S": ["Steel", "Steel moment frames, braced frames and other steel structural systems."],
    "W": ["Wood / timber", "Light wood frames, timber post-and-beam, and bamboo construction."],
    "HYB": ["Hybrid / mixed", "More than one primary structural material in the same building."],
    "OT": ["Other / unclassified", "Informal construction, metal sheeting and materials not covered by the other groups."],
  };

  function renderTaxGuide(taxRows) {
    const el = document.getElementById("tax-guide");
    if (!el) return;
    // Rows are TOTAL-settlement or URBAN/RURAL per (occupancy, taxonomy);
    // sum TOTAL rows, plus split rows only where no TOTAL row exists.
    const hasTotal = new Set(
      taxRows.filter((r) => r.SETTLEMENT === "TOTAL")
             .map((r) => `${r.OCCUPANCY}|${r.TAXONOMY}`)
    );
    const byMacro = new Map();
    for (const row of taxRows) {
      const counted = row.SETTLEMENT === "TOTAL" ||
        !hasTotal.has(`${row.OCCUPANCY}|${row.TAXONOMY}`);
      if (!counted) continue;
      byMacro.set(row.MACRO_TAXONOMY,
        (byMacro.get(row.MACRO_TAXONOMY) || 0) + (row.BUILDINGS || 0));
    }
    const counts = [...byMacro.entries()];
    const all = counts.reduce((a, [, n]) => a + n, 0) || 1;
    counts.sort((a, b) => b[1] - a[1]);
    el.innerHTML = counts.map(([code, n]) => {
      const info = MACRO_INFO[code] || [code, "See the GEM Building Taxonomy documentation."];
      const share = (100 * n) / all;
      return `<tr><td><code>${code}</code></td><td><strong>${info[0]}</strong> — ${info[1]}</td>` +
        `<td class="num">${UCC.fmtInt.format(n)}</td>` +
        `<td class="num">${share >= 1 ? share.toFixed(0) : "<1"}%</td></tr>`;
    }).join("");
  }

  const view = document.getElementById("country-view");
  let adm1Table = null, taxTable = null, current = null;

  async function show(iso3) {
    if (!iso3) { view.hidden = true; return; }
    const country = await UCC.json(`data/countries/${iso3}.json`);
    current = country;
    view.hidden = false;
    select.value = iso3;
    document.getElementById("country-meta").textContent =
      `${country.region} · ${index.find((c) => c.iso3 === iso3)?.n_adm1 ?? 0} admin-1 regions`;

    // Stat tiles from the ADM0 rows.
    const adm0 = toObjects(country.adm0);
    document.getElementById("tiles").innerHTML = OCC_ORDER
      .map((occ) => adm0.find((r) => r.OCCUPANCY === occ))
      .filter(Boolean)
      .map(
        (r) => `
        <div class="tile">
          <div class="k">${UCC.OCC_LABELS[r.OCCUPANCY]}</div>
          <div class="v">${fmtUcc.format(uccOf(r))} <span class="unit">USD/m²${costYear}</span></div>
          <div class="s">${UCC.fmtInt.format(r.BUILDINGS)} buildings · ${fmtCompact.format(r.TOTAL_AREA_SQM)} m²</div>
        </div>`
      )
      .join("");

    document.getElementById("downloads").innerHTML =
      `<a class="btn" href="data/countries/${iso3}.json" download="ucc_${iso3}.json">Download ${country.name} (JSON)</a>` +
      `<a class="btn" href="index.html#level=adm1&occ=TOTAL">Open world map</a>`;

    showCountryMap(iso3).catch(console.error);

    // ADM1 table.
    const adm1Rows = toObjects(country.adm1);
    const adm1Occ = document.getElementById("adm1-occ");
    fillOccSelect(adm1Occ, [...new Set(adm1Rows.map((r) => r.OCCUPANCY))], false, "TOTAL");
    const adm1Cols = [
      { label: "Code", key: "ID_1" },
      { label: "Name", key: "NAME_1" },
      ...numCols(),
    ];
    const adm1Filtered = () => adm1Rows.filter((r) => r.OCCUPANCY === adm1Occ.value);
    adm1Table = makeTable(
      document.getElementById("adm1-table"), adm1Cols, adm1Filtered(),
      document.getElementById("adm1-count")
    );
    adm1Occ.onchange = () => adm1Table.setRows(adm1Filtered());

    // Taxonomy table.
    const taxRows = toObjects(country.taxonomy);
    const taxOcc = document.getElementById("tax-occ");
    const taxSet = document.getElementById("tax-set");
    const taxSearch = document.getElementById("tax-search");
    fillOccSelect(taxOcc, [...new Set(taxRows.map((r) => r.OCCUPANCY))], true, "");
    const settlements = [...new Set(taxRows.map((r) => r.SETTLEMENT))];
    taxSet.innerHTML = "";
    for (const [value, label] of [["", "All"], ...settlements.map((s) => [s, s])]) {
      const opt = document.createElement("option");
      opt.value = value;
      opt.textContent = label;
      taxSet.appendChild(opt);
    }
    taxSet.value = "";  // "All": RES rows are URBAN/RURAL-only in many countries
    taxSearch.value = "";

    const taxCols = [
      { label: "Occupancy", key: "OCCUPANCY" },
      { label: "Macro", key: "MACRO_TAXONOMY" },
      { label: "Taxonomy", key: "TAXONOMY", cls: "tax" },
      { label: "Settlement", key: "SETTLEMENT" },
      ...numCols(),
    ];
    const taxFiltered = () => {
      const needle = taxSearch.value.trim().toUpperCase();
      return taxRows.filter(
        (r) =>
          (!taxOcc.value || r.OCCUPANCY === taxOcc.value) &&
          (!taxSet.value || r.SETTLEMENT === taxSet.value) &&
          (!needle ||
            r.TAXONOMY.toUpperCase().includes(needle) ||
            String(r.MACRO_TAXONOMY).toUpperCase().includes(needle))
      );
    };
    taxTable = makeTable(
      document.getElementById("tax-table"), taxCols, taxFiltered(),
      document.getElementById("tax-count")
    );
    taxOcc.onchange = () => taxTable.setRows(taxFiltered());
    taxSet.onchange = () => taxTable.setRows(taxFiltered());
    taxSearch.oninput = () => taxTable.setRows(taxFiltered());

    renderTaxGuide(taxRows);

    UCC.setHash({ country: iso3 });
    document.title = `${country.name} — UCC Database`;
  }

  select.addEventListener("change", () => show(select.value).catch(console.error));
  window.addEventListener("hashchange", () => {
    const iso3 = UCC.hashState().country;
    if (iso3 && iso3 !== current?.iso3) show(iso3).catch(console.error);
  });

  const initial = UCC.hashState().country;
  if (initial && index.some((c) => c.iso3 === initial)) await show(initial);
})().catch((err) => {
  console.error(err);
  document.querySelector(".content").insertAdjacentHTML(
    "beforeend",
    '<p class="note">Failed to load data. If viewing locally, serve the site ' +
      "with an HTTP server (e.g. <code>py -m http.server --directory docs</code>).</p>"
  );
});
