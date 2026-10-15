/* Shared helpers: formatting, fetch, footer/meta wiring. */

const UCC = {
  BINS: [100, 200, 400, 800, 1600, 3200],
  RAMP: ["#cde2fb", "#9ec5f4", "#6da7ec", "#3987e5", "#256abf", "#184f95", "#0d366b"],
  NODATA: "#e1e0d9",
  OCC_LABELS: {
    TOTAL: "All occupancies",
    RES: "Residential",
    COM: "Commercial",
    IND: "Industrial",
    NONRES: "Non-residential",
  },

  // CARTO basemap key (https://carto.com/basemaps/apikey). It ships in every
  // tile URL, so it is public by design; restrict it to this site's domain in
  // the CARTO dashboard. Without a valid key CARTO serves watermarked tiles.
  CARTO_KEY: "cb1_4log_1_5afcc37ed146da738507be13",

  // Light, label-free CARTO raster basemap shared by every map on the site.
  basemapStyle() {
    return {
      version: 8,
      sources: {
        basemap: {
          type: "raster",
          tiles: [`https://basemaps.cartocdn.com/light_nolabels/{z}/{x}/{y}.png?key=${UCC.CARTO_KEY}`],
          tileSize: 256,
          attribution: "&copy; <a href=\"https://www.openstreetmap.org/copyright\">OpenStreetMap</a> contributors &copy; <a href=\"https://carto.com/attributions\">CARTO</a>",
        },
      },
      layers: [
        { id: "bg", type: "background", paint: { "background-color": "#f9f9f7" } },
        { id: "basemap", type: "raster", source: "basemap",
          paint: { "raster-opacity": 0.85 } },
      ],
    };
  },

  fmtInt: new Intl.NumberFormat("en-US", { maximumFractionDigits: 0 }),
  fmtUcc: new Intl.NumberFormat("en-US", { maximumFractionDigits: 0 }),

  async json(url) {
    const resp = await fetch(url);
    if (!resp.ok) throw new Error(`${url}: HTTP ${resp.status}`);
    return resp.json();
  },

  fillExpr(occ) {
    const step = ["step", ["get", `UCC_${occ}`], UCC.RAMP[0]];
    UCC.BINS.forEach((edge, i) => step.push(edge, UCC.RAMP[i + 1]));
    return ["case", ["has", `UCC_${occ}`], step, UCC.NODATA];
  },

  binLabel(i) {
    const b = UCC.BINS;
    if (i === 0) return `< ${b[0]}`;
    if (i === b.length) return `≥ ${b[b.length - 1].toLocaleString("en-US")}`;
    return `${b[i - 1].toLocaleString("en-US")}–${b[i].toLocaleString("en-US")}`;
  },

  hashState() {
    const out = {};
    for (const part of location.hash.replace(/^#/, "").split("&")) {
      const [k, v] = part.split("=");
      if (k && v) out[k] = decodeURIComponent(v);
    }
    return out;
  },

  setHash(state) {
    const parts = Object.entries(state)
      .filter(([, v]) => v != null && v !== "")
      .map(([k, v]) => `${k}=${encodeURIComponent(v)}`);
    history.replaceState(null, "", parts.length ? `#${parts.join("&")}` : location.pathname);
  },

  async fillFooter() {
    const el = document.querySelector("[data-meta-line]");
    if (!el) return;
    try {
      const meta = await UCC.json("data/meta.json");
      const date = (meta.generated_utc || "").slice(0, 10);
      const sha = (meta.gem_commit || "").slice(0, 7);
      el.textContent = `GEM commit ${sha} · built ${date}`;
    } catch {
      el.textContent = "";
    }
  },
};

document.addEventListener("DOMContentLoaded", () => {
  const here = location.pathname.split("/").pop() || "index.html";
  document.querySelectorAll(".site-header nav a").forEach((a) => {
    if (a.getAttribute("href") === here) a.classList.add("active");
  });
  UCC.fillFooter();
});
