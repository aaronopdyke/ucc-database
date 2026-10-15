/* Global UCC choropleth: ADM0/ADM1 toggle, occupancy selector, fixed
   log-spaced bins, hover tooltip, click popup. Values are baked into the
   TopoJSON feature properties as UCC_<OCCUPANCY> integers. */

(async function () {
  "use strict";

  const LEVELS = ["adm0", "adm1"];
  const OCCS = ["TOTAL", "RES", "COM", "IND"];
  const state = { level: "adm0", occ: "TOTAL" };
  const fromHash = UCC.hashState();
  if (LEVELS.includes(fromHash.level)) state.level = fromHash.level;
  if (OCCS.includes(fromHash.occ)) state.occ = fromHash.occ;

  // ---- data ----
  const [topo0, topo1, index, meta] = await Promise.all([
    UCC.json("data/boundaries_adm0.topojson"),
    UCC.json("data/boundaries_adm1.topojson"),
    UCC.json("data/countries_index.json"),
    UCC.json("data/meta.json").catch(() => ({})),
  ]);
  const costYear = meta.cost_year ? ` (${meta.cost_year} USD)` : "";
  const firstObj = (t) => t.objects[Object.keys(t.objects)[0]];
  const geo = {
    adm0: topojson.feature(topo0, firstObj(topo0)),
    adm1: topojson.feature(topo1, firstObj(topo1)),
  };
  const countryName = Object.fromEntries(index.map((c) => [c.iso3, c.name]));

  // ---- map ----
  const map = new maplibregl.Map({
    container: "map",
    style: UCC.basemapStyle(),
    center: [12, 22],
    zoom: 1.4,
    minZoom: 0.6,
    maxZoom: 9,
    attributionControl: false,
  });
  map.addControl(new maplibregl.NavigationControl({ showCompass: false }), "top-right");
  map.addControl(
    new maplibregl.AttributionControl({
      compact: true,
      customAttribution:
        'Data: <a href="https://github.com/gem/global_exposure_model">GEM</a> CC BY-NC-SA 4.0 · ' +
        'Boundaries: <a href="https://datacatalog.worldbank.org/search/dataset/0038272">World Bank</a> CC BY 4.0',
    })
  );
  map.dragRotate.disable();
  map.touchZoomRotate.disableRotation();

  const fillColor = (occ) => [
    "case",
    ["has", `UCC_${occ}`],
    [
      "step", ["get", `UCC_${occ}`],
      UCC.RAMP[0], UCC.BINS[0],
      UCC.RAMP[1], UCC.BINS[1],
      UCC.RAMP[2], UCC.BINS[2],
      UCC.RAMP[3], UCC.BINS[3],
      UCC.RAMP[4], UCC.BINS[4],
      UCC.RAMP[5], UCC.BINS[5],
      UCC.RAMP[6],
    ],
    UCC.NODATA,
  ];

  await new Promise((resolve) => (map.loaded() ? resolve() : map.once("load", resolve)));

  for (const level of LEVELS) {
    map.addSource(level, { type: "geojson", data: geo[level], generateId: true });
    map.addLayer({
      id: `fill-${level}`,
      type: "fill",
      source: level,
      layout: { visibility: level === state.level ? "visible" : "none" },
      paint: { "fill-color": fillColor(state.occ), "fill-opacity": 0.78 },
    });
    map.addLayer({
      id: `line-${level}`,
      type: "line",
      source: level,
      layout: { visibility: level === state.level ? "visible" : "none" },
      paint: {
        "line-color": "#fcfcfb",
        "line-width": level === "adm0" ? 0.6 : 0.4,
      },
    });
    map.addLayer({
      id: `hover-${level}`,
      type: "line",
      source: level,
      layout: { visibility: level === state.level ? "visible" : "none" },
      paint: {
        "line-color": "#0b0b0b",
        "line-width": 1.4,
        "line-opacity": ["case", ["boolean", ["feature-state", "hover"], false], 1, 0],
      },
    });
  }

  // ---- legend ----
  const legend = document.getElementById("legend");
  function renderLegend() {
    const bins = UCC.RAMP.map(
      (color, i) =>
        `<span class="bin"><span class="swatch" style="background:${color}"></span><span>${UCC.binLabel(i)}</span></span>`
    ).join("");
    legend.innerHTML =
      `<span class="legend-title">UCC USD/m²${costYear} — ${UCC.OCC_LABELS[state.occ]}</span>` +
      bins +
      `<span class="bin"><span class="swatch" style="background:${UCC.NODATA}"></span><span>No data</span></span>`;
  }

  // ---- interactions ----
  const tooltip = document.getElementById("tooltip");
  let hovered = null; // {level, id}

  function clearHover() {
    if (hovered) {
      map.setFeatureState({ source: hovered.level, id: hovered.id }, { hover: false });
      hovered = null;
    }
    tooltip.style.display = "none";
    map.getCanvas().style.cursor = "";
  }

  function featureLabel(props) {
    if (props.gid || props.iso3) {
      const country = countryName[props.iso3] || props.iso3;
      return props.name ? `${props.name} — ${country}` : country;
    }
    return props.name || "";
  }

  map.on("mousemove", (e) => {
    const layer = `fill-${state.level}`;
    if (!map.getLayer(layer)) return;
    const feats = map.queryRenderedFeatures(e.point, { layers: [layer] });
    if (!feats.length) {
      clearHover();
      return;
    }
    const f = feats[0];
    if (!hovered || hovered.id !== f.id || hovered.level !== state.level) {
      if (hovered) map.setFeatureState({ source: hovered.level, id: hovered.id }, { hover: false });
      hovered = { level: state.level, id: f.id };
      map.setFeatureState({ source: state.level, id: f.id }, { hover: true });
    }
    map.getCanvas().style.cursor = "pointer";
    const value = f.properties[`UCC_${state.occ}`];
    tooltip.innerHTML =
      `<div class="t-name">${featureLabel(f.properties)}</div>` +
      `<div class="t-val">${
        value != null ? `${UCC.fmtUcc.format(value)} USD/m² · ${UCC.OCC_LABELS[state.occ]}` : "No data"
      }</div>`;
    tooltip.style.display = "block";
    const pad = 14;
    const rect = map.getContainer().getBoundingClientRect();
    let x = e.point.x + pad, y = e.point.y + pad;
    if (x + tooltip.offsetWidth > rect.width - 8) x = e.point.x - tooltip.offsetWidth - pad;
    if (y + tooltip.offsetHeight > rect.height - 8) y = e.point.y - tooltip.offsetHeight - pad;
    tooltip.style.left = `${x}px`;
    tooltip.style.top = `${y}px`;
  });
  map.getContainer().addEventListener("mouseleave", clearHover);

  map.on("click", (e) => {
    const layer = `fill-${state.level}`;
    const feats = map.queryRenderedFeatures(e.point, { layers: [layer] });
    if (!feats.length) return;
    const props = feats[0].properties;
    const iso3 = props.iso3 || props.id;
    const rows = ["TOTAL", "RES", "COM", "IND", "NONRES"]
      .filter((occ) => props[`UCC_${occ}`] != null)
      .map(
        (occ) =>
          `<tr class="${occ === state.occ ? "hl" : ""}"><td>${UCC.OCC_LABELS[occ]}</td>` +
          `<td>${UCC.fmtUcc.format(props[`UCC_${occ}`])}</td></tr>`
      )
      .join("");
    const link = iso3 && countryName[iso3]
      ? `<p style="margin:0.45rem 0 0"><a href="browse.html#country=${iso3}">View tables for ${countryName[iso3]} →</a></p>`
      : "";
    const sub = props.gid
      ? `${props.gid} · ${countryName[props.iso3] || props.iso3}`
      : props.units
      ? `${countryName[props.iso3] || props.iso3} · area-weighted over ` +
        `${props.units.split(";").length} GEM region(s)`
      : props.natl
      ? `${countryName[props.iso3] || props.iso3} · national average ` +
        `(no admin-1 boundary match)`
      : props.id || "";
    new maplibregl.Popup({ closeButton: true, maxWidth: "300px" })
      .setLngLat(e.lngLat)
      .setHTML(
        `<h3>${props.name || countryName[iso3] || ""}</h3>` +
        `<div class="sub">${sub}</div>` +
        (rows
          ? `<table class="popup-table"><tbody>${rows}</tbody></table>` +
            `<div class="sub" style="margin-top:0.3rem">UCC in USD/m²${costYear}</div>`
          : `<p style="margin:0">No UCC data for this unit.</p>`) +
        link
      )
      .addTo(map);
  });

  // ---- controls ----
  const toggle = document.getElementById("level-toggle");
  toggle.addEventListener("click", (e) => {
    const btn = e.target.closest("button[data-level]");
    if (!btn || btn.dataset.level === state.level) return;
    clearHover();
    state.level = btn.dataset.level;
    for (const b of toggle.querySelectorAll("button")) {
      b.setAttribute("aria-pressed", String(b === btn));
    }
    for (const level of LEVELS) {
      const vis = level === state.level ? "visible" : "none";
      for (const prefix of ["fill", "line", "hover"]) {
        map.setLayoutProperty(`${prefix}-${level}`, "visibility", vis);
      }
    }
    UCC.setHash(state);
  });
  for (const b of toggle.querySelectorAll("button")) {
    b.setAttribute("aria-pressed", String(b.dataset.level === state.level));
  }

  const occSelect = document.getElementById("occ-select");
  occSelect.value = state.occ;
  occSelect.addEventListener("change", () => {
    state.occ = occSelect.value;
    for (const level of LEVELS) {
      map.setPaintProperty(`fill-${level}`, "fill-color", fillColor(state.occ));
    }
    renderLegend();
    UCC.setHash(state);
  });

  for (const level of LEVELS) {
    const vis = level === state.level ? "visible" : "none";
    for (const prefix of ["fill", "line", "hover"]) {
      map.setLayoutProperty(`${prefix}-${level}`, "visibility", vis);
    }
  }
  renderLegend();
  UCC.setHash(state);
})().catch((err) => {
  console.error(err);
  const el = document.getElementById("map");
  if (el) {
    el.innerHTML =
      '<p style="padding:2rem;color:#52514e">Failed to load map data. ' +
      "If you are viewing this locally, serve the site with an HTTP server " +
      "(e.g. <code>py -m http.server --directory docs</code>).</p>";
  }
});
