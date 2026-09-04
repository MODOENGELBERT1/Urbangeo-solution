/* UrbanSanity v13 — Application Frontend */
'use strict';

// ── ÉTAT GLOBAL ────────────────────────────────────────────────────────────
const state = {
  map: null,
  drawControl: null,
  aoiGeoJSON: null,
  bbox: null,
  osmData: null,
  analysisResult: null,
  layers: {},
  wasteGrid: [],
  baseLayers: {},
  currentBasemap: 'carto-light',
  selectedBin: null,
  activeScenario: 'balanced',
  reportLang: 'fr',
};

// ── LANGUE DU RAPPORT PDF ─────────────────────────────────────────────────
function setReportLang(lang) {
  state.reportLang = (lang === 'en') ? 'en' : 'fr';
  // Toggle boutons header
  const bFr = document.getElementById('lang-fr');
  const bEn = document.getElementById('lang-en');
  if (bFr) bFr.classList.toggle('active', state.reportLang === 'fr');
  if (bEn) bEn.classList.toggle('active', state.reportLang === 'en');
  // Note dans le panel export
  const note = document.getElementById('export-lang-note');
  if (note) {
    note.textContent = state.reportLang === 'fr'
      ? 'Rapport PDF en Francais (FR)'
      : 'PDF Report in English (EN)';
  }
  toast(state.reportLang === 'fr' ? 'Rapport : Francais' : 'Report: English', 'info', 1400);
}

// ── FONDS DE CARTE ─────────────────────────────────────────────────────────
const BASEMAPS = {
  'carto-light':   { url: 'https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}{r}.png',  attr: '© OpenStreetMap contributors, © CartoDB' },
  'carto-dark':    { url: 'https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png',   attr: '© OpenStreetMap contributors, © CartoDB' },
  'osm':           { url: 'https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png',              attr: '© OpenStreetMap contributors' },
  'topo':          { url: 'https://{s}.tile.opentopomap.org/{z}/{x}/{y}.png',                attr: '© OpenTopoMap contributors' },
  'esri-imagery':  { url: 'https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}', attr: 'Tiles © Esri' },
  'esri-gray':     { url: 'https://server.arcgisonline.com/ArcGIS/rest/services/Canvas/World_Light_Gray_Base/MapServer/tile/{z}/{y}/{x}', attr: 'Tiles © Esri' },
};

// ── INITIALISATION — UN SEUL DOMContentLoaded ──────────────────────────────
document.addEventListener('DOMContentLoaded', () => {
  initMap();
  initLayerToggles();
  checkHealth();
  // Observer pour activer le panel Point Check quand OSM est chargé
  const btnAnalyze = document.getElementById('btn-analyze');
  if (btnAnalyze) {
    new MutationObserver(() => {
      if (!btnAnalyze.disabled) {
        const btnPlace = document.getElementById('btn-toggle-placement');
        if (btnPlace) btnPlace.disabled = false;
      }
    }).observe(btnAnalyze, { attributes: true, attributeFilter: ['disabled'] });
  }
});

// ── GÉOCODAGE INVERSE (Nominatim) ─────────────────────────────────────────
async function geocodeAOI(bbox) {
  const statusEl = document.getElementById('geocoding-status');
  if (!statusEl) return;
  statusEl.textContent = 'Géolocalisation…';
  try {
    const lat = (bbox.south + bbox.north) / 2;
    const lon = (bbox.west + bbox.east) / 2;
    const resp = await fetch(
      `https://nominatim.openstreetmap.org/reverse?lat=${lat}&lon=${lon}&format=json&zoom=14&addressdetails=1`,
      { headers: { 'Accept-Language': 'fr', 'User-Agent': 'UrbanSanity/13' } }
    );
    if (!resp.ok) throw new Error();
    const data = await resp.json();
    const addr = data.address || {};
    const parts = [
      addr.neighbourhood || addr.suburb || addr.city_district || addr.quarter,
      addr.city || addr.town || addr.village || addr.county,
      addr.country
    ].filter(Boolean).slice(0, 2);
    const locationName = parts.join(' — ');
    const inp = document.getElementById('location-name');
    if (inp && !inp.value && locationName) inp.value = locationName;
    statusEl.textContent = locationName ? ('📍 Localité : ' + locationName) : '';
    setTimeout(() => { statusEl.textContent = ''; }, 5000);
  } catch (e) {
    statusEl.textContent = '';
  }
}

// ── INITIALISATION CARTE ───────────────────────────────────────────────────
function initMap() {
  const bm = BASEMAPS['carto-light'];
  const baseLayer = L.tileLayer(bm.url, { attribution: bm.attr, maxZoom: 19 });
  state.baseLayers['carto-light'] = baseLayer;
  state.layers.basemap = baseLayer;

  state.map = L.map('map', {
    center: [3.848, 11.502],
    zoom: 14,
    layers: [baseLayer],
    zoomControl: true,
  });

  // Sélecteur de fond de carte
  const BasemapControl = L.Control.extend({
    options: { position: 'topleft' },
    onAdd() {
      const div = L.DomUtil.create('div', 'basemap-switcher leaflet-control');
      div.innerHTML = `<select id="basemap-select" onchange="changeBasemap(this.value)">
        <option value="carto-light">CartoDB Positron</option>
        <option value="carto-dark">CartoDB Sombre</option>
        <option value="osm">OSM Standard</option>
        <option value="topo">OpenTopoMap</option>
        <option value="esri-imagery">Esri Satellite</option>
        <option value="esri-gray">Esri Gris</option>
      </select>`;
      L.DomEvent.disableClickPropagation(div);
      return div;
    }
  });
  state.map.addControl(new BasemapControl());

  // Légende (toujours visible, tout en français)
  const LegendControl = L.Control.extend({
    options: { position: 'topleft' },
    onAdd() {
      const div = L.DomUtil.create('div', 'compact-legend leaflet-control');
      div.innerHTML = `
        <div class="legend-title">Légende de carte</div>
        <div class="legend-group-title">Bacs optimisés</div>
        <div class="legend-item"><span class="legend-dot" style="background:#e74c3c"></span><span>Classe A</span></div>
        <div class="legend-item"><span class="legend-dot" style="background:#f39c12"></span><span>Classe B</span></div>
        <div class="legend-item"><span class="legend-dot" style="background:#3498db"></span><span>Classe C</span></div>
        <div class="legend-group-title">Mode de collecte</div>
        <div class="legend-item"><span class="legend-dot" style="background:#2e86de"></span><span>Camion</span></div>
        <div class="legend-item"><span class="legend-dot" style="background:#f39c12"></span><span>Tricycle</span></div>
        <div class="legend-item"><span class="legend-dot" style="background:#27ae60"></span><span>À pied</span></div>
        <div class="legend-group-title">Couches de référence</div>
        <div class="legend-item"><span class="legend-dot" style="background:#566573"></span><span>Bacs OSM existants</span></div>
        <div class="legend-item"><span class="legend-dot" style="background:#7dcea0"></span><span>Grille demande déchets</span></div>
        <div class="legend-item"><span class="legend-dot" style="background:#c0392b"></span><span>Zones non couvertes</span></div>
        <div class="legend-item"><span class="legend-dot" style="background:#8e44ad"></span><span>Zone AOI</span></div>
        <div class="legend-group-title">Anneaux de service</div>
        <div class="legend-item"><span class="legend-dot" style="background:#e74c3c"></span><span>R1 — Proche</span></div>
        <div class="legend-item"><span class="legend-dot" style="background:#f39c12"></span><span>R2 — Intermédiaire</span></div>
        <div class="legend-item"><span class="legend-dot" style="background:#3498db"></span><span>R3 — Étendu</span></div>
        <div class="legend-group-title">Point Check</div>
        <div class="legend-item"><span class="legend-dot" style="background:#d97706;border-radius:2px"></span><span>Point manuel</span></div>`;
      L.DomEvent.disableClickPropagation(div);
      return div;
    }
  });
  state.map.addControl(new LegendControl());

  // Contrôle de dessin
  const drawnItems = new L.FeatureGroup();
  state.map.addLayer(drawnItems);
  state.layers.drawn = drawnItems;

  state.drawControl = new L.Control.Draw({
    draw: {
      polygon: { shapeOptions: { color: '#8e44ad', fillOpacity: 0.1, weight: 2 } },
      rectangle: false, circle: false, circlemarker: false, marker: false, polyline: false,
    },
    edit: { featureGroup: drawnItems, remove: true },
  });
  // CRITIQUE : ajouter le contrôle à la carte
  state.map.addControl(state.drawControl);

  state.map.on(L.Draw.Event.CREATED, (e) => {
    drawnItems.clearLayers();
    drawnItems.addLayer(e.layer);
    setAOI(e.layer.toGeoJSON());
  });
  
  // Force map to recalculate size after layout settles (fixes gray map on some configs)
  setTimeout(() => { state.map.invalidateSize(true); }, 200);
}

// ── TOGGLES COUCHES ────────────────────────────────────────────────────────
function initLayerToggles() {
  const layerDefs = [
    { key: 'proposed',      label: 'Bacs optimisés',           color: '#e74c3c', default: true  },
    { key: 'existing_bins', label: 'Bacs OSM existants',        color: '#566573', default: true  },
    { key: 'waste_grid',    label: 'Grille demande déchets',    color: '#27ae60', default: true  },
    { key: 'underserved',   label: 'Zones non couvertes',       color: '#c0392b', default: true  },
    { key: 'buildings',     label: 'Bâtiments',                 color: '#95a5a6', default: false },
    { key: 'roads',         label: 'Routes',                    color: '#2980b9', default: false },
    { key: 'schools',       label: 'Écoles',                    color: '#9b59b6', default: true  },
    { key: 'hospitals',     label: 'Hôpitaux',                  color: '#e74c3c', default: true  },
    { key: 'hydro',         label: 'Eau / Drains',              color: '#3498db', default: true  },
    { key: 'ring_r1',       label: 'Anneau R1 (proche)',        color: '#e74c3c', default: true  },
    { key: 'ring_r2',       label: 'Anneau R2 (intermédiaire)', color: '#f39c12', default: true  },
    { key: 'ring_r3',       label: 'Anneau R3 (étendu)',        color: '#3498db', default: true  },
    { key: 'aoi',           label: 'Zone AOI',                  color: '#8e44ad', default: true  },
    { key: 'manual_points', label: 'Points check manuel',       color: '#d97706', default: true  },
  ];
  const container = document.getElementById('layer-toggles');
  container.innerHTML = '';
  layerDefs.forEach(def => {
    const row = document.createElement('div');
    row.className = 'layer-toggle';
    row.innerHTML = `
      <input type="checkbox" id="lyr-${def.key}" ${def.default ? 'checked' : ''} onchange="toggleLayer('${def.key}', this.checked)"/>
      <span class="layer-dot" style="background:${def.color}"></span>
      <label for="lyr-${def.key}">${def.label}</label>`;
    container.appendChild(row);
  });
  // Note d'état au bas du panneau couches
  const note = document.createElement('div');
  note.id = 'layers-empty-note';
  note.className = 'mini-note';
  note.style.marginTop = '8px';
  note.textContent = "Les couches s'affichent après chargement OSM et analyse.";
  container.appendChild(note);
}

// ── VÉRIFICATION API ───────────────────────────────────────────────────────
async function checkHealth() {
  try {
    const r = await fetch('/api/health');
    const d = await r.json();
    if (d.status === 'ok') toast(`UrbanSanity v${d.version} connecté ✓`, 'success', 2500);
  } catch {
    toast('API non disponible — vérifier le backend Docker', 'error');
  }
}

// ── GESTION AOI ────────────────────────────────────────────────────────────
function startDrawPolygon() {
  if (!state.map) return;
  new L.Draw.Polygon(state.map, { shapeOptions: { color: '#8e44ad', fillOpacity: 0.12, weight: 2 } }).enable();
  toast('Cliquez sur la carte pour dessiner votre zone. Double-clic pour terminer.', 'info', 5000);
}

function setAOI(geojson) {
  state.aoiGeoJSON = geojson;
  const coords = geojson.geometry.coordinates[0];
  const lats = coords.map(c => c[1]);
  const lons = coords.map(c => c[0]);
  state.bbox = {
    south: Math.min(...lats), north: Math.max(...lats),
    west:  Math.min(...lons), east:  Math.max(...lons),
  };
  if (state.layers.aoiPoly) state.map.removeLayer(state.layers.aoiPoly);
  state.layers.aoiPoly = L.geoJSON(geojson, {
    style: { color: '#8e44ad', fillOpacity: 0.08, weight: 2.5, dashArray: '6,4' }
  }).addTo(state.map);
  state.map.fitBounds(state.layers.aoiPoly.getBounds(), { padding: [20, 20] });

  const area = calcAreaKm2(state.bbox);
  const el = document.getElementById('aoi-info');
  el.classList.remove('hidden');
  el.innerHTML = `✅ Zone définie · Aire ≈ <b>${area.toFixed(3)} km²</b><br>Bbox : [${state.bbox.south.toFixed(3)}, ${state.bbox.west.toFixed(3)}, ${state.bbox.north.toFixed(3)}, ${state.bbox.east.toFixed(3)}]`;

  document.getElementById('btn-fetch').disabled = false;
  geocodeAOI(state.bbox);
}

function clearAOI() {
  state.aoiGeoJSON = null;
  state.bbox = null;
  if (state.layers.aoiPoly) { state.map.removeLayer(state.layers.aoiPoly); state.layers.aoiPoly = null; }
  if (state.layers.drawn) state.layers.drawn.clearLayers();
  document.getElementById('aoi-info').classList.add('hidden');
  document.getElementById('btn-fetch').disabled = true;
  document.getElementById('btn-analyze').disabled = true;
}

function calcAreaKm2(bbox) {
  const dlat = 111.32 * (bbox.north - bbox.south);
  const dlon = 111.32 * Math.cos((bbox.south + bbox.north) / 2 * Math.PI / 180) * (bbox.east - bbox.west);
  return dlat * dlon;
}

async function importFile(input) {
  const file = input.files[0];
  if (!file) return;
  const name = file.name.toLowerCase();
  if (name.endsWith('.geojson') || name.endsWith('.json')) {
    try {
      const geojson = JSON.parse(await file.text());
      let feat = geojson.type === 'FeatureCollection' ? geojson.features[0] : geojson;
      if (feat && feat.geometry) setAOI(feat);
      else toast('GeoJSON invalide', 'error');
    } catch { toast('Fichier non parsable', 'error'); }
  } else {
    toast('Format non supporté — utilisez GeoJSON (.geojson ou .json)', 'warning');
  }
  input.value = '';
}

// ── CHARGEMENT OSM ─────────────────────────────────────────────────────────
async function fetchOSM() {
  if (!state.bbox) { toast('Définir une zone AOI en premier', 'warning'); return; }
  setStatus('loading', 'Chargement OSM…');
  setProgress(20);
  const mode = document.getElementById('osm-mode').value;
  try {
    const resp = await fetch('/api/fetch_osm', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ bbox: state.bbox, mode, aoi: state.aoiGeoJSON }),
    });
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const data = await resp.json();
    state.osmData = data;
    setProgress(100);
    setStatus('done', 'OSM chargé');
    renderOSMLayers(data);
    const nb = (data.buildings?.features || []).length;
    const nr = (data.roads?.features || []).length;
    const ns = (data.schools?.features || []).length;
    const nh = (data.hospitals?.features || []).length;
    const nw = (data.waste_bins?.features || []).length;
    const el = document.getElementById('osm-summary');
    el.classList.remove('hidden', 'error');
    el.innerHTML = `${data.cached ? '📦 Cache' : '📡 Chargé'} (${data.source})<br>
      🏠 <b>${nb}</b> bâtiments · 🛣 <b>${nr}</b> routes · 🏫 <b>${ns}</b> écoles · 🏥 <b>${nh}</b> hôpitaux · 🗑 <b>${nw}</b> bacs existants` +
      (data.message ? `<br><span style="color:#b9770e">⚠ ${data.message}</span>` : '');
    document.getElementById('btn-analyze').disabled = false;
    // Activer le panel Point Check
    const btnPlace = document.getElementById('btn-toggle-placement');
    if (btnPlace) btnPlace.disabled = false;
    // Mettre à jour la note des couches
    const emptyNote = document.getElementById('layers-empty-note');
    if (emptyNote) emptyNote.textContent = "Couches OSM chargées — lancez l'analyse pour voir les bacs optimisés.";
    toast(`OSM chargé (${nb} bâtiments, ${nr} routes)`, 'success');
    setTimeout(() => setProgress(0), 1000);
  } catch (e) {
    setStatus('error', 'Erreur OSM');
    const el = document.getElementById('osm-summary');
    el.classList.remove('hidden');
    el.classList.add('error');
    el.innerHTML = `❌ Erreur : ${e.message}`;
    toast('Échec chargement OSM : ' + e.message, 'error');
  }
}

function renderOSMLayers(data) {
  const filtered = clipDataToAOIForDisplay(filterDataToAOI(data));
  state.osmData = filtered;
  ['osmBuildings','osmRoads','osmSchools','osmHospitals','osmHydro','osmExistingBins'].forEach(k => {
    if (state.layers[k]) { state.map.removeLayer(state.layers[k]); state.layers[k] = null; }
  });
  if ((filtered.buildings?.features || []).length > 0) {
    state.layers.osmBuildings = L.geoJSON(filtered.buildings, {
      style: () => ({ color: '#7f8c8d', fillColor: '#bdc3c7', weight: 0.5, fillOpacity: 0.45 })
    });
    if (document.getElementById('lyr-buildings')?.checked) state.layers.osmBuildings.addTo(state.map);
  }
  if ((filtered.roads?.features || []).length > 0) {
    state.layers.osmRoads = L.geoJSON(filtered.roads, {
      style: f => {
        const hw = f.properties?.highway || '';
        const color = hw.includes('primary') ? '#e74c3c' : hw.includes('secondary') ? '#f39c12' : '#95a5a6';
        return { color, weight: hw.includes('primary') ? 2.5 : 1.4, opacity: 0.85 };
      }
    });
    if (document.getElementById('lyr-roads')?.checked) state.layers.osmRoads.addTo(state.map);
  }
  if ((filtered.schools?.features || []).length > 0) {
    state.layers.osmSchools = L.geoJSON(filtered.schools, { pointToLayer: (f, ll) => L.marker(ll, { icon: createModeIcon('E', '#9b59b6') }) });
    if (document.getElementById('lyr-schools')?.checked) state.layers.osmSchools.addTo(state.map);
  }
  if ((filtered.hospitals?.features || []).length > 0) {
    state.layers.osmHospitals = L.geoJSON(filtered.hospitals, { pointToLayer: (f, ll) => L.marker(ll, { icon: createModeIcon('H', '#c0392b') }) });
    if (document.getElementById('lyr-hospitals')?.checked) state.layers.osmHospitals.addTo(state.map);
  }
  if ((filtered.hydro?.features || []).length > 0) {
    state.layers.osmHydro = L.geoJSON(filtered.hydro, {
      style: f => ({ color: '#3498db', fillColor: '#85c1e9', weight: f.geometry?.type === 'LineString' ? 2 : 1, fillOpacity: 0.35, opacity: 0.8 })
    });
    if (document.getElementById('lyr-hydro')?.checked) state.layers.osmHydro.addTo(state.map);
  }
  if ((filtered.waste_bins?.features || []).length > 0) {
    state.layers.osmExistingBins = L.geoJSON(filtered.waste_bins, { pointToLayer: (f, ll) => L.marker(ll, { icon: createModeIcon('B', '#566573') }) });
    if (document.getElementById('lyr-existing_bins')?.checked) state.layers.osmExistingBins.addTo(state.map);
  }
}

// ── ANALYSE ────────────────────────────────────────────────────────────────
async function runAnalysis(adaptiveMode = false) {
  if (!state.osmData || !state.bbox) { toast('Charger les données OSM en premier', 'warning'); return; }
  setStatus('loading', adaptiveMode ? 'Analyse adaptative…' : 'Analyse spatiale…');
  setProgress(10);
  animateProgress();

  const params = {
    pph:                  parseFloat(document.getElementById('p-pph').value) || 5.0,
    waste_kg:             parseFloat(document.getElementById('p-waste').value) || 0.42,
    grid_m:               parseFloat(document.getElementById('p-grid').value) || 200,
    max_bins:             parseInt(document.getElementById('p-maxbins').value) || 30,
    r1_m:                 parseFloat(document.getElementById('p-r1').value) || 150,
    r2_m:                 parseFloat(document.getElementById('p-r2').value) || 300,
    r3_m:                 parseFloat(document.getElementById('p-r3').value) || 500,
    fill_threshold:       parseFloat(document.getElementById('p-fill').value) || 0.8,
    truck_access_m:       parseFloat(document.getElementById('p-truck-access').value) || 50,
    truck_min_road_w:     parseFloat(document.getElementById('p-truck-roadw').value) || 3.5,
    tricycle_access_m:    parseFloat(document.getElementById('p-tricycle-access').value) || 100,
    tricycle_min_road_w:  parseFloat(document.getElementById('p-tricycle-roadw').value) || 2.0,
    truck_bin_kg:         parseFloat(document.getElementById('p-cap-truck').value) || 240,
    tricycle_bin_kg:      parseFloat(document.getElementById('p-cap-tricycle').value) || 80,
    foot_capacity_kg:     parseFloat(document.getElementById('p-cap-foot').value) || 4,
    min_school_m:         parseFloat(document.getElementById('p-school').value) || 20,
    min_hospital_m:       parseFloat(document.getElementById('p-hospital').value) || 20,
    min_hydro_m:          parseFloat(document.getElementById('p-hydro').value) || 15,
    weight_waste:         parseFloat(document.getElementById('p-wgt-waste').value) || 0.45,
    weight_access:        parseFloat(document.getElementById('p-wgt-access').value) || 0.25,
    weight_sensitive:     parseFloat(document.getElementById('p-wgt-sensitive').value) || 0.15,
    weight_hydro:         parseFloat(document.getElementById('p-wgt-hydro').value) || 0.15,
    w1:                   parseFloat(document.getElementById('p-w1').value) || 0.60,
    w2:                   parseFloat(document.getElementById('p-w2').value) || 0.30,
    w3:                   parseFloat(document.getElementById('p-w3').value) || 0.10,
    min_bin_spacing_m:    parseFloat(document.getElementById('p-min-spacing').value) || 125,
    sel_weight_local:     parseFloat(document.getElementById('p-sel-local').value) || 0.35,
    sel_weight_coverage:  parseFloat(document.getElementById('p-sel-coverage').value) || 0.35,
    sel_weight_waste:     parseFloat(document.getElementById('p-sel-waste').value) || 0.20,
    sel_penalty_overlap:  parseFloat(document.getElementById('p-sel-overlap').value) || 0.10,
    adaptive_mode:        !!adaptiveMode,
    auto_adaptive:        true,
  };

  try {
    const resp = await fetch('/api/analyze', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ osm_data: state.osmData, bbox: state.bbox, params, aoi: state.aoiGeoJSON }),
    });
    if (!resp.ok) { const err = await resp.text(); throw new Error(`HTTP ${resp.status}: ${err}`); }
    const data = await resp.json();
    state.analysisResult = data;
    setProgress(100);
    setStatus('done', 'Analyse terminée');
    renderAnalysisResults(data);
    setTimeout(() => setProgress(0), 1000);
    const rb = data.rebalancing;
    const rbNote = rb?.performed ? ` · Rééquilibrage : ${rb.bins_relocated} bacs` : '';
    const adaptNote = data.summary?.adaptive_performed ? ' · Adaptatif auto' : '';
    toast(`✅ ${data.proposed_bins.length} bacs optimisés · Conf : ${data.confidence}/100${adaptNote}${rbNote}`, 'success');
  } catch (e) {
    setStatus('error', 'Erreur analyse');
    toast('Erreur analyse : ' + e.message, 'error');
    setProgress(0);
  }
}

let progressInterval = null;
function animateProgress() {
  let v = 15; clearInterval(progressInterval);
  progressInterval = setInterval(() => { v = Math.min(v + 3, 85); setProgress(v); if (v >= 85) clearInterval(progressInterval); }, 200);
}

// ── RENDU DES RÉSULTATS ────────────────────────────────────────────────────
function renderAnalysisResults(data) {
  clearInterval(progressInterval);
  const { proposed_bins, waste_grid, underserved_cells, summary, confidence, scenarios, comparison, recommended_scenario } = data;
  const beforeAfter = summary.before_after || {};
  const readiness   = summary.implementation_readiness || {};
  const confDrivers = summary.confidence_drivers || [];
  const topSites    = summary.top_priority_sites || [];
  const risks       = summary.risk_mitigation || [];
  const gaps        = summary.coverage_gaps || {};
  const rationale   = summary.decision_rationale || [];
  const rebalancing = data.rebalancing || {};

  ['wasteGridLayer','proposedLayer','ringLayer','underservedLayer'].forEach(k => {
    if (state.layers[k]) { state.map.removeLayer(state.layers[k]); state.layers[k] = null; }
  });

  if (document.getElementById('lyr-waste_grid')?.checked) renderWasteGrid(waste_grid);
  state.wasteGrid = waste_grid;
  if (document.getElementById('lyr-underserved')?.checked && underserved_cells?.features?.length) renderUnderservedCells(underserved_cells.features);
  renderProposedBins(proposed_bins);

  // Couleurs selon niveau
  const confColor = confidence >= 70 ? '#1A7A4A' : confidence >= 40 ? '#B87B00' : '#C0392B';
  const confLabel = confidence >= 70 ? 'Élevée' : confidence >= 40 ? 'Modérée' : 'Faible';
  const readColor = (readiness.score || 0) >= 70 ? '#1A7A4A' : (readiness.score || 0) >= 40 ? '#B87B00' : '#C0392B';

  const adaptivePerformed = !!summary.adaptive_performed;
  const strategyText = adaptivePerformed
    ? 'Analyse adaptative automatique : les bacs existants ont été réutilisés comme ancres et de nouveaux sites ajoutés pour améliorer la couverture spatiale.'
    : 'Réseau optimisé sur la base des critères multi-critères et de la couverture spatiale.';

  const adaptiveHtml = adaptivePerformed ? `
    <div class="adaptive-prompt">
      <div class="adaptive-prompt-header">✅ Analyse adaptative automatique</div>
      <div class="adaptive-prompt-body">${summary.adaptive_trigger_reason || 'Le réseau existant a servi de base. Les bacs les plus performants ont été retenus et de nouveaux sites ajoutés pour combler les lacunes.'}</div>
      <div class="adaptive-prompt-metrics">
        <div><b>Couverture R1 avant :</b> ${Math.round(beforeAfter.before_coverage_pop ?? 0).toLocaleString()} pers. (${beforeAfter.before_coverage_pct ?? 0}%)</div>
        <div><b>Couverture R1 après :</b> ${Math.round(beforeAfter.after_coverage_pop ?? 0).toLocaleString()} pers. (${beforeAfter.after_coverage_pct ?? 0}%)</div>
      </div>
    </div>` : '';

  const rebalanceHtml = rebalancing.performed ? `
    <div class="rebalance-box">
      <div class="rebalance-box-header">🔄 Rééquilibrage spatial automatique</div>
      <div class="rebalance-stat"><span class="rebalance-stat-label">Bacs redondants détectés</span><span class="rebalance-stat-value">${rebalancing.redundant_detected || 0}</span></div>
      <div class="rebalance-stat"><span class="rebalance-stat-label">Bacs relocalisés</span><span class="rebalance-stat-value">${rebalancing.bins_relocated || 0}</span></div>
      <div class="rebalance-stat"><span class="rebalance-stat-label">Gain couverture R1</span><span class="rebalance-stat-value">+${rebalancing.coverage_gain_pct || 0} pts</span></div>
    </div>` : '';

  const driversHtml = confDrivers.map(d => `<div class="driver-row"><span>${d.label}</span><span>${d.value}%</span></div>`).join('');
  const topHtml = topSites.length ? `<div class="comparison-box"><div><b>Top 5 sites prioritaires</b></div>${topSites.map((s,i) => `<div>${i+1}. <b>${s.id}</b> · Classe ${s.class} · ${s.mode} · ${s.waste_kg_day?.toFixed ? s.waste_kg_day.toFixed(1) : s.waste_kg_day} kg/j · ${s.pickups_per_week}×/sem</div>`).join('')}</div>` : '';
  const gapsHtml = `<div class="comparison-box"><div><b>Lacunes de couverture restantes</b></div>
    <div>Bien desservi R1 : ${gaps.well_served_pct ?? 0}%</div>
    <div>Mal desservi (anneaux ext.) : ${gaps.underserved_pct ?? 0}% · ${Math.round(gaps.underserved_pop ?? 0).toLocaleString()} pers.</div>
    <div>Hors service : ${gaps.no_service_pct ?? 0}% · ${Math.round(gaps.no_service_pop ?? 0).toLocaleString()} pers.</div>
    <div>Déchets sans service : ${(gaps.no_service_waste_kg_day ?? 0).toFixed ? gaps.no_service_waste_kg_day.toFixed(1) : gaps.no_service_waste_kg_day} kg/j</div>
    ${(gaps.top_gap_cells || []).length ? `<div class="mini-note" style="margin-top:5px">Hotspot principal : grille ${(gaps.top_gap_cells[0] || {}).grid_ref} · ${((gaps.top_gap_cells[0] || {}).waste_kg_day) || 0} kg/j</div>` : ''}
  </div>`;
  const risksHtml = risks.length ? `<div class="comparison-box"><div><b>Risques & mitigations</b></div>${risks.map(r => `<div><b>${r.risk} :</b> ${r.mitigation}</div>`).join('')}</div>` : '';
  const rationaleHtml = rationale.length ? `<div class="comparison-box"><div><b>Justification de décision</b></div>${rationale.map(r => `<div>• ${r}</div>`).join('')}</div>` : '';

  const resBox = document.getElementById('results-summary');
  resBox.classList.remove('hidden');
  resBox.innerHTML = `
    <div class="results-grid">
      <div class="result-kpi"><div class="result-kpi-val">${proposed_bins.length}</div><div class="result-kpi-lbl">Bacs optimisés</div></div>
      <div class="result-kpi"><div class="result-kpi-val">${(summary.total_pop || 0).toLocaleString()}</div><div class="result-kpi-lbl">Population estimée</div></div>
      <div class="result-kpi"><div class="result-kpi-val">${(summary.total_waste_kg_day || 0).toFixed(0)}</div><div class="result-kpi-lbl">kg déchets/jour</div></div>
      <div class="result-kpi"><div class="result-kpi-val">${summary.n_existing_bins || 0}</div><div class="result-kpi-lbl">Bacs OSM existants</div></div>
    </div>
    <div class="mini-note" style="margin:8px 0 6px">${summary.message || 'Optimisation basée sur le scoring multi-critères et la couverture spatiale.'}</div>
    <div class="recommendation-box" style="margin-top:8px">
      <div class="recommendation-title">Stratégie d'optimisation</div>
      <div class="recommendation-text">${strategyText}</div>
    </div>
    ${adaptiveHtml}${rebalanceHtml}
    <div style="font-size:11px;color:#5A6E84;margin:8px 0 3px">Confiance : ${confidence}/100 — ${confLabel}</div>
    <div class="confidence-bar"><div class="confidence-fill" style="width:${confidence}%;background:${confColor}"></div></div>
    <div class="comparison-box">
      <div><b>AOI :</b> ${summary.area_km2} km² · ${summary.n_roads} routes</div>
      <div><b>Distance médiane au bac existant :</b> ${comparison?.median_nearest_existing_m != null ? `${comparison.median_nearest_existing_m} m` : 'N/A'}</div>
    </div>
    <div class="comparison-box">
      <div><b>Couverture Avant / Après (R1) :</b></div>
      <div>Avant : ${beforeAfter.before_coverage_pct ?? 0}% · Après : ${beforeAfter.after_coverage_pct ?? 0}% · Gain : ${Number(beforeAfter.gain_pct ?? 0) >= 0 ? '+' : ''}${beforeAfter.gain_pct ?? 0} pts</div>
      <div>Population : ${Math.round(beforeAfter.before_coverage_pop ?? 0).toLocaleString()} → ${Math.round(beforeAfter.after_coverage_pop ?? 0).toLocaleString()}</div>
    </div>
    <div class="comparison-box">
      <div><b>Maturité d'implémentation :</b> <span style="color:${readColor}">${readiness.score || 0}/100 — ${readiness.label || '—'}</span></div>
      <div>Sites : A=${readiness.class_a || 0}, B=${readiness.class_b || 0}, C=${readiness.class_c || 0} · Faisabilité ${readiness.feasible_share_pct || 0}%</div>
    </div>
    <div class="comparison-box"><div><b>Indicateurs de confiance</b></div><div class="drivers-box">${driversHtml || '<div class="mini-note">Aucun indicateur disponible.</div>'}</div></div>
    ${topHtml}${gapsHtml}${rationaleHtml}${risksHtml}
    <div class="recommendation-box">
      <div class="recommendation-title">Recommandation</div>
      <div class="recommendation-text">${summary.recommendation || 'Priorisez les sites de Classe A et validez-les sur le terrain avant déploiement.'}</div>
      <div class="mini-note" style="margin-top:7px">Scénario recommandé : <b>${(recommended_scenario || 'balanced').replace('_', ' ')}</b></div>
    </div>`;

  renderScenarios(scenarios, recommended_scenario || 'balanced');
  document.getElementById('panel-scenarios').classList.remove('hidden');
  document.getElementById('panel-export').classList.remove('hidden');
  // Masquer la note "pas encore de données" une fois l'analyse terminée
  const emptyNote = document.getElementById('layers-empty-note');
  if (emptyNote) emptyNote.style.display = 'none';
}

function renderWasteGrid(grid) {
  const maxW = Math.max(...grid.map(c => c.waste_kg_day), 1);
  const circles = grid.map(cell => {
    const intensity = cell.waste_kg_day / maxW;
    return L.circleMarker([cell.lat, cell.lon], {
      radius: 6 + intensity * 8, color: 'transparent',
      fillColor: interpolateColor('#e8f8f5', '#1a5276', intensity), fillOpacity: 0.45,
    }).bindTooltip(`Cellule ${cell.i}-${cell.j}<br>${cell.waste_kg_day.toFixed(1)} kg/j<br>${Math.round(cell.population)} pers.<br>${cell.buildings} bâtiments`, { sticky: true });
  });
  state.layers.wasteGridLayer = L.layerGroup(circles).addTo(state.map);
}

function renderUnderservedCells(features) {
  const circles = features.map(feat => {
    const p = feat.properties || {};
    const [lon, lat] = feat.geometry?.coordinates || [0, 0];
    const noSvc = p.status === 'no_service';
    return L.circleMarker([lat, lon], {
      radius: noSvc ? 8 : 6, color: noSvc ? '#922b21' : '#c0392b',
      weight: 1, fillColor: noSvc ? '#e74c3c' : '#f5b7b1', fillOpacity: 0.55,
    }).bindTooltip(`Zone ${p.i}-${p.j}<br>Statut : ${p.status}<br>${Math.round(p.population || 0)} pers.<br>${(p.waste_kg_day || 0).toFixed ? p.waste_kg_day.toFixed(1) : p.waste_kg_day} kg/j`, { sticky: true });
  });
  state.layers.underservedLayer = L.layerGroup(circles).addTo(state.map);
}

function renderProposedBins(bins) {
  const modeLabels = { truck: 'Camion', tricycle: 'Tricycle', foot: 'À pied' };
  const modeIcons  = { truck: '🚚', tricycle: '🛺', foot: '🚶' };
  const modeColors = { truck: '#2e86de', tricycle: '#f39c12', foot: '#27ae60' };
  const markers = bins.map((bin, idx) => {
    const cls = bin.class || 'C';
    const color = modeColors[bin.collection_mode] || '#2e86de';
    const icon  = modeIcons[bin.collection_mode] || '🗑';
    const marker = L.marker([bin.lat, bin.lon], { icon: createModeIcon(`${icon}${cls}`, color) });
    marker.on('click', () => showBinDetails(bin, idx + 1));
    const nearTxt = bin.nearest_existing_bin_m != null ? `${bin.nearest_existing_bin_m} m du bac OSM le plus proche` : 'Aucun bac OSM dans la zone';
    marker.bindTooltip(`#${idx+1} · Classe ${cls}<br>${modeLabels[bin.collection_mode] || bin.collection_mode} · ${bin.pickups_per_week}×/sem<br>${nearTxt}<br>Gain couverture : ${bin.incremental_weighted_pop || 0} pers.`, { sticky: true });
    return marker;
  });
  state.layers.proposedLayer = L.layerGroup(markers).addTo(state.map);
}

function showBinDetails(bin, idx) {
  const classColor = bin.class === 'A' ? '#e74c3c' : bin.class === 'B' ? '#f39c12' : '#3498db';
  const modeColor  = { truck: '#2e86de', tricycle: '#f39c12', foot: '#27ae60' }[bin.collection_mode] || '#2e86de';
  const modeLabel  = { truck: 'Camion', tricycle: 'Tricycle', foot: 'À pied' }[bin.collection_mode] || bin.collection_mode;
  const nearTxt    = bin.nearest_existing_bin_m != null ? `${bin.nearest_existing_bin_m} m` : 'Aucun dans la zone';

  document.getElementById('info-panel-title').innerHTML = `Bac #${idx} — <span style="color:${classColor}">Classe ${bin.class}</span>`;
  document.getElementById('info-panel-body').innerHTML = `
    <div class="info-row"><span class="info-label">Mode de collecte</span><span class="info-value" style="color:${modeColor}">${modeLabel}</span></div>
    <div class="info-row"><span class="info-label">Score MCDA local</span><span class="info-value">${bin.score.toFixed(4)}</span></div>
    <div class="info-row"><span class="info-label">Objectif sélection</span><span class="info-value">${(bin.selection_objective || 0).toFixed ? bin.selection_objective.toFixed(4) : bin.selection_objective}</span></div>
    <div class="info-row"><span class="info-label">Coordonnées</span><span class="info-value">${bin.lat.toFixed(5)}, ${bin.lon.toFixed(5)}</span></div>
    <div class="info-row"><span class="info-label">Route la plus proche</span><span class="info-value">${bin.road_dist_m} m (${bin.road_type}, ${bin.road_width_m} m)</span></div>
    <div class="info-row"><span class="info-label">Bac existant le plus proche</span><span class="info-value">${nearTxt}</span></div>
    <div class="info-row"><span class="info-label">École / Hôpital / Eau</span><span class="info-value">${bin.school_dist_m} / ${bin.hospital_dist_m} / ${bin.hydro_dist_m} m</span></div>
    <div class="info-section-title">Zone d'influence</div>
    <div class="info-row"><span class="info-label">R1</span><span class="info-value">${bin.population_r1} pers. · ${bin.waste_r1_kg_day} kg/j · ${bin.r1_pickups_per_week}×/sem</span></div>
    <div class="info-row"><span class="info-label">R2</span><span class="info-value">${bin.population_r2} pers. · ${bin.waste_r2_kg_day} kg/j · ${bin.r2_pickups_per_week}×/sem</span></div>
    <div class="info-row"><span class="info-label">R3</span><span class="info-value">${bin.population_r3} pers. · ${bin.waste_r3_kg_day} kg/j · ${bin.r3_pickups_per_week}×/sem</span></div>
    <div class="info-row"><span class="info-label">Demande pondérée</span><span class="info-value">${bin.weighted_pop} pers. · ${bin.weighted_waste_kg_day} kg/j</span></div>
    <div class="info-row"><span class="info-label">Gain de couverture incrémental</span><span class="info-value">${bin.incremental_weighted_pop || 0} pers. · ${bin.incremental_weighted_waste || 0} kg/j</span></div>
    <div class="info-row"><span class="info-label">Capacité assignée</span><span class="info-value">${bin.assigned_capacity_kg} kg-éq.</span></div>
    <div class="info-row"><span class="info-label">Fréquence recommandée</span><span class="info-value">${bin.pickups_per_week}×/sem · toutes ${bin.days_between_pickups} jours</span></div>
    <div class="recommendation-box" style="margin-top:10px">
      <div class="recommendation-title">Recommandation site</div>
      <div class="recommendation-text">${bin.recommendation || 'Valider le site sur le terrain et confirmer l\'accès, la sécurité et l\'acceptation communautaire avant installation.'}</div>
    </div>`;
  document.getElementById('info-panel').classList.remove('hidden');

  ['ringR1','ringR2','ringR3'].forEach(k => { if (state.layers[k]) { state.map.removeLayer(state.layers[k]); state.layers[k] = null; } });
  const r1 = parseFloat(document.getElementById('p-r1').value) || 150;
  const r2 = parseFloat(document.getElementById('p-r2').value) || 300;
  const r3 = parseFloat(document.getElementById('p-r3').value) || 500;
  state.layers.ringR1 = L.circle([bin.lat, bin.lon], { radius: r1, color: '#e74c3c', fillColor: '#e74c3c', fillOpacity: 0.05, weight: 1.5, dashArray: '4,3' }).bindTooltip(`R1 · ${bin.population_r1} pers. · ${bin.waste_r1_kg_day} kg/j`, { sticky: true });
  state.layers.ringR2 = L.circle([bin.lat, bin.lon], { radius: r2, color: '#f39c12', fillColor: '#f39c12', fillOpacity: 0.04, weight: 1.5, dashArray: '4,3' }).bindTooltip(`R2 · ${bin.population_r2} pers. · ${bin.waste_r2_kg_day} kg/j`, { sticky: true });
  state.layers.ringR3 = L.circle([bin.lat, bin.lon], { radius: r3, color: '#3498db', fillColor: '#3498db', fillOpacity: 0.03, weight: 1.5, dashArray: '4,3' }).bindTooltip(`R3 · ${bin.population_r3} pers. · ${bin.waste_r3_kg_day} kg/j`, { sticky: true });
  if (document.getElementById('lyr-ring_r1')?.checked) state.layers.ringR1.addTo(state.map);
  if (document.getElementById('lyr-ring_r2')?.checked) state.layers.ringR2.addTo(state.map);
  if (document.getElementById('lyr-ring_r3')?.checked) state.layers.ringR3.addTo(state.map);
}

function closeInfoPanel() {
  document.getElementById('info-panel').classList.add('hidden');
  ['ringR1','ringR2','ringR3'].forEach(k => { if (state.layers[k]) { state.map.removeLayer(state.layers[k]); state.layers[k] = null; } });
}

function renderScenarios(scenarios, recommended = 'balanced') {
  const labels = {
    balanced:     { name: '⚖️ Équilibré',        desc: 'Score et accès à part égale' },
    walk_first:   { name: '🚶 Priorité marche',  desc: 'Maximise la couverture piétonne' },
    access_first: { name: '🚛 Priorité accès',   desc: 'Optimise la collecte mécanique' },
  };
  const body = document.getElementById('scenarios-body');
  body.innerHTML = '';
  Object.entries(scenarios).forEach(([key, sc]) => {
    const info = labels[key] || { name: key, desc: '' };
    const isRec = key === recommended;
    const card = document.createElement('div');
    card.className = `scenario-card${isRec ? ' scenario-recommended' : ''}`;
    card.setAttribute('data-scenario', key);
    card.onclick = () => selectScenario(key);
    card.innerHTML = `
      <div class="scenario-name">${info.name}</div>
      <div style="font-size:10px;color:#5A6E84;margin-bottom:6px">${info.desc}</div>
      <div class="scenario-stats">
        <div class="sc-stat"><div class="sc-val">${sc.bin_count}</div><div class="sc-lbl">bacs</div></div>
        <div class="sc-stat"><div class="sc-val">${sc.coverage_pct}%</div><div class="sc-lbl">couverture</div></div>
        <div class="sc-stat"><div class="sc-val">${sc.avg_pickups}×</div><div class="sc-lbl">ramassages/sem</div></div>
      </div>`;
    body.appendChild(card);
  });
}

function selectScenario(key) {
  document.querySelectorAll('.scenario-card').forEach(c => c.classList.remove('active'));
  document.querySelector(`[data-scenario="${key}"]`)?.classList.add('active');
  state.activeScenario = key;
  toast(`Scénario "${key}" sélectionné`, 'info', 1500);
}

// ── TOGGLES COUCHES ────────────────────────────────────────────────────────
function toggleLayer(key, visible) {
  const layerMap = {
    proposed: 'proposedLayer', existing_bins: 'osmExistingBins',
    waste_grid: 'wasteGridLayer', underserved: 'underservedLayer',
    buildings: 'osmBuildings', roads: 'osmRoads', schools: 'osmSchools',
    hospitals: 'osmHospitals', hydro: 'osmHydro',
    ring_r1: 'ringR1', ring_r2: 'ringR2', ring_r3: 'ringR3',
    aoi: 'aoiPoly', manual_points: 'manualPointsLayer',
  };
  const lyrKey = layerMap[key];
  if (!lyrKey) return;
  const lyr = state.layers[lyrKey];
  if (!lyr) {
    if (key === 'waste_grid' && state.wasteGrid.length > 0 && visible) renderWasteGrid(state.wasteGrid);
    if (key === 'underserved' && state.analysisResult?.underserved_cells?.features?.length && visible) renderUnderservedCells(state.analysisResult.underserved_cells.features);
    return;
  }
  if (visible) { if (!state.map.hasLayer(lyr)) lyr.addTo(state.map); }
  else { if (state.map.hasLayer(lyr)) state.map.removeLayer(lyr); }
}

// ── CHANGEMENT FOND DE CARTE ───────────────────────────────────────────────
function changeBasemap(key) {
  const bm = BASEMAPS[key];
  if (!bm || !state.map) return;
  if (state.layers.basemap && state.map.hasLayer(state.layers.basemap)) state.map.removeLayer(state.layers.basemap);
  let layer = state.baseLayers[key];
  if (!layer) { layer = L.tileLayer(bm.url, { attribution: bm.attr, maxZoom: 19 }); state.baseLayers[key] = layer; }
  state.layers.basemap = layer;
  layer.addTo(state.map);
  if (typeof layer.bringToBack === 'function') layer.bringToBack();
  state.currentBasemap = key;
  const sel = document.getElementById('basemap-select');
  if (sel && sel.value !== key) sel.value = key;
  toast(`Fond de carte : ${sel?.selectedOptions?.[0]?.text || key}`, 'info', 1200);
}

// ── EXPORT ─────────────────────────────────────────────────────────────────
function exportGeoJSON() {
  if (!state.analysisResult) { toast('Lancer l\'analyse en premier', 'warning'); return; }
  const fc = {
    type: 'FeatureCollection',
    features: state.analysisResult.proposed_bins.map((b, i) => ({
      type: 'Feature',
      geometry: { type: 'Point', coordinates: [b.lon, b.lat] },
      properties: { id: i+1, classe: b.class, score: b.score, mode_collecte: b.collection_mode, ramassages_sem: b.pickups_per_week, capacite_kg: b.assigned_capacity_kg, population_r1: b.population_r1, dechets_r1_kg_j: b.waste_r1_kg_day },
    }))
  };
  downloadFile('urbansanity_bacs.geojson', JSON.stringify(fc, null, 2), 'application/json');
  toast('GeoJSON téléchargé', 'success', 2000);
}

function exportCSV() {
  if (!state.analysisResult) { toast('Lancer l\'analyse en premier', 'warning'); return; }
  const bins = state.analysisResult.proposed_bins;
  const header = ['id','lat','lon','classe','score','mode_collecte','ramassages_sem','capacite_kg','population_r1','dechets_r1_kgj','population_r2','dechets_r2_kgj','population_r3','dechets_r3_kgj','dist_route_m','type_route','dist_ecole_m','dist_hopital_m','dist_eau_m','bac_existant_m'];
  const rows = bins.map((b, i) => [i+1, b.lat.toFixed(6), b.lon.toFixed(6), b.class, b.score.toFixed(4), b.collection_mode, b.pickups_per_week, b.assigned_capacity_kg, b.population_r1, b.waste_r1_kg_day, b.population_r2, b.waste_r2_kg_day, b.population_r3, b.waste_r3_kg_day, b.road_dist_m, b.road_type, b.school_dist_m, b.hospital_dist_m, b.hydro_dist_m, b.nearest_existing_bin_m ?? '']);
  downloadFile('urbansanity_bacs.csv', [header, ...rows].map(r => r.join(',')).join('\n'), 'text/csv');
  toast('CSV téléchargé', 'success', 2000);
}

async function exportPDF() {
  if (!state.analysisResult) { toast('Lancer l\'analyse en premier', 'warning'); return; }
  setStatus('loading', 'Génération PDF…');
  toast('Génération du rapport PDF…', 'info', 3000);
  try {
    const lang = state.reportLang || 'fr';   // set via boutons FR/EN en haut
    const city = document.getElementById('location-name')?.value?.trim() || (lang === 'fr' ? 'Zone analysée' : 'Analysis area');
    const resp = await fetch('/api/report', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        analysis: state.analysisResult,
        bbox: state.bbox,
        city_name: city,
        report_lang: lang,
        aoi: state.aoiGeoJSON,
        manual_check_result: manualState.analysisResult || null,
      })
    });
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const blob = await resp.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url; a.download = `UrbanSanity_Rapport_${lang.toUpperCase()}.pdf`;
    a.click();
    URL.revokeObjectURL(url);
    setStatus('done', 'PDF généré');
    toast('Rapport PDF téléchargé ✓', 'success');
  } catch (e) {
    setStatus('error', 'Erreur PDF');
    toast('Erreur PDF : ' + e.message, 'error');
  }
}

// ── MODAL MÉTHODE ──────────────────────────────────────────────────────────
async function showHowItWorks() {
  document.getElementById('modal-hiw').classList.remove('hidden');
  const body = document.getElementById('modal-hiw-body');
  body.innerHTML = '<p style="color:#999">Chargement…</p>';
  try {
    const d = await (await fetch('/api/how-it-works')).json();
    let html = '';
    d.steps.forEach(s => { html += `<div class="hiw-step"><div class="hiw-num">${s.id}</div><div class="hiw-content"><h4>${s.title}</h4><p>${s.desc}</p></div></div>`; });
    html += '<hr style="margin:14px 0;border-color:#eee"/><div style="font-weight:700;color:#00467F;margin-bottom:8px">Références</div><ul class="ref-list">';
    d.references.forEach(r => { html += `<li>• ${r}</li>`; });
    html += '</ul>';
    body.innerHTML = html;
  } catch { body.innerHTML = '<p style="color:red">Impossible de charger la méthodologie.</p>'; }
}

function closeModal(id) { document.getElementById(id).classList.add('hidden'); }

function togglePanel(id) {
  const el = document.getElementById(id);
  if (el) el.style.display = el.style.display === 'none' ? '' : 'none';
}

// ── HELPERS ────────────────────────────────────────────────────────────────
function setStatus(type, text) {
  const badge = document.getElementById('status-badge');
  badge.textContent = text;
  badge.className = `badge badge-${type === 'loading' ? 'loading' : type === 'done' ? 'done' : type === 'error' ? 'error' : 'idle'}`;
}

function setProgress(pct) {
  document.getElementById('analyze-progress').classList.toggle('hidden', pct <= 0);
  document.getElementById('progress-fill').style.width = pct + '%';
}

function toast(msg, type = 'info', duration = 4000) {
  const c = document.getElementById('toast-container');
  const el = document.createElement('div');
  el.className = `toast toast-${type}`;
  el.textContent = msg;
  c.appendChild(el);
  setTimeout(() => { if (el.parentNode) c.removeChild(el); }, duration);
}

function downloadFile(name, content, mime) {
  const url = URL.createObjectURL(new Blob([content], { type: mime }));
  const a = document.createElement('a');
  a.href = url; a.download = name; a.click();
  URL.revokeObjectURL(url);
}

function createModeIcon(symbol, color) {
  return L.divIcon({
    html: `<div style="min-width:30px;height:30px;background:${color};border-radius:15px;display:flex;align-items:center;justify-content:center;color:#fff;font-weight:800;font-size:11px;padding:0 7px;border:2px solid #fff;box-shadow:0 2px 6px rgba(0,0,0,.28)">${symbol}</div>`,
    iconSize: [34, 30], iconAnchor: [17, 15], className: ''
  });
}

function interpolateColor(hex1, hex2, t) {
  const r1=parseInt(hex1.slice(1,3),16), g1=parseInt(hex1.slice(3,5),16), b1=parseInt(hex1.slice(5,7),16);
  const r2=parseInt(hex2.slice(1,3),16), g2=parseInt(hex2.slice(3,5),16), b2=parseInt(hex2.slice(5,7),16);
  const r=Math.round(r1+(r2-r1)*t), g=Math.round(g1+(g2-g1)*t), b=Math.round(b1+(b2-b1)*t);
  return `#${r.toString(16).padStart(2,'0')}${g.toString(16).padStart(2,'0')}${b.toString(16).padStart(2,'0')}`;
}

// ── FILTRAGE AOI ───────────────────────────────────────────────────────────
function filterDataToAOI(data) {
  if (!state.aoiGeoJSON) return data;
  const ring = getAoiRing(state.aoiGeoJSON);
  if (!ring) return data;
  const out = { ...data };
  ['buildings','roads','schools','hospitals','hydro','waste_bins'].forEach(key => {
    const fc = data[key];
    if (!fc?.features) return;
    out[key] = { type: 'FeatureCollection', features: fc.features.filter(f => featureIntersectsRing(f, ring)) };
  });
  return out;
}

function clipDataToAOIForDisplay(data) {
  if (!state.aoiGeoJSON) return data;
  const ring = getAoiRing(state.aoiGeoJSON);
  if (!ring) return data;
  const out = { ...data };
  ['buildings','roads','schools','hospitals','hydro','waste_bins'].forEach(key => {
    const fc = data[key];
    if (!fc?.features) return;
    out[key] = { type: 'FeatureCollection', features: fc.features.map(f => clipFeatureApprox(f, ring)).filter(Boolean) };
  });
  return out;
}

function getAoiRing(gj) {
  if (!gj) return null;
  let obj = gj;
  if (obj.type === 'FeatureCollection') obj = obj.features?.[0];
  if (obj?.type === 'Feature') obj = obj.geometry;
  if (!obj) return null;
  if (obj.type === 'Polygon') return obj.coordinates?.[0] || null;
  if (obj.type === 'MultiPolygon') return obj.coordinates?.[0]?.[0] || null;
  return null;
}

function pointInRing(lon, lat, ring) {
  let inside = false;
  for (let i = 0, j = ring.length - 1; i < ring.length; j = i++) {
    const xi = ring[i][0], yi = ring[i][1], xj = ring[j][0], yj = ring[j][1];
    if (((yi > lat) !== (yj > lat)) && (lon < ((xj - xi) * (lat - yi) / ((yj - yi) || 1e-12) + xi))) inside = !inside;
  }
  return inside;
}

function featureIntersectsRing(f, ring) {
  const g = f?.geometry; if (!g) return false;
  const c = g.coordinates || [];
  if (g.type === 'Point') return pointInRing(c[0], c[1], ring);
  if (g.type === 'LineString') return c.some(p => pointInRing(p[0], p[1], ring));
  if (g.type === 'Polygon') { const p = c[0] || []; return p.some(pt => pointInRing(pt[0], pt[1], ring)) || ring.some(pt => pointInRing(pt[0], pt[1], p)); }
  return false;
}

function clipFeatureApprox(feature, ring) {
  if (!feature?.geometry) return null;
  const g = feature.geometry, props = feature.properties || {};
  if (g.type === 'Point') return pointInRing(g.coordinates[0], g.coordinates[1], ring) ? feature : null;
  if (g.type === 'LineString') { const inside = (g.coordinates || []).filter(pt => pointInRing(pt[0], pt[1], ring)); return inside.length >= 2 ? { type: 'Feature', geometry: { type: 'LineString', coordinates: inside }, properties: props } : null; }
  if (g.type === 'Polygon') {
    const rc = (g.coordinates || [[]])[0].filter(pt => pointInRing(pt[0], pt[1], ring));
    if (rc.length < 3) return null;
    const closed = (rc[0][0] === rc[rc.length-1][0] && rc[0][1] === rc[rc.length-1][1]) ? rc : [...rc, rc[0]];
    return closed.length >= 4 ? { type: 'Feature', geometry: { type: 'Polygon', coordinates: [closed] }, properties: props } : null;
  }
  return null;
}

// ══════════════════════════════════════════════════════════════════════════
// POINT CHECK MANUEL — Complètement isolé de l'analyse principale
// ══════════════════════════════════════════════════════════════════════════
const manualState = {
  points: [], placementActive: false,
  analysisResult: null, layer: null, counter: 0,
};

function enableManualPanel() {
  const btn = document.getElementById('btn-toggle-placement');
  if (btn) btn.disabled = false;
}

function toggleManualPlacement() {
  manualState.placementActive = !manualState.placementActive;
  const btn = document.getElementById('btn-toggle-placement');
  const indicator = document.getElementById('placement-indicator');
  const mapEl = document.getElementById('map');
  if (manualState.placementActive) {
    if (btn)       { btn.classList.add('active'); btn.innerHTML = '⏹ Arrêter le placement'; }
    if (indicator) indicator.classList.remove('hidden');
    if (mapEl)     mapEl.classList.add('manual-placing-mode');
    state.map.on('click', _onManualMapClick);
    document.addEventListener('keydown', _onEscCancel);
    toast("Cliquez sur la carte pour placer un point d'analyse", 'info', 3500);
  } else {
    _stopPlacement();
  }
}

function _stopPlacement() {
  manualState.placementActive = false;
  const btn = document.getElementById('btn-toggle-placement');
  if (btn) { btn.classList.remove('active'); btn.innerHTML = '📍 Activer le placement'; }
  document.getElementById('placement-indicator')?.classList.add('hidden');
  document.getElementById('map')?.classList.remove('manual-placing-mode');
  state.map.off('click', _onManualMapClick);
  document.removeEventListener('keydown', _onEscCancel);
}

function _onEscCancel(e) { if (e.key === 'Escape') _stopPlacement(); }

function _onManualMapClick(e) {
  const { lat, lng } = e.latlng;
  manualState.counter++;
  const id = `M${manualState.counter}`;
  const pt = { id, lat: parseFloat(lat.toFixed(6)), lon: parseFloat(lng.toFixed(6)) };
  manualState.points.push(pt);
  _renderMarker(pt);
  _updatePointList();
  document.getElementById('btn-clear-manual').disabled = false;
  document.getElementById('btn-run-manual').disabled = false;
  toast(`Point ${id} placé (${lat.toFixed(4)}, ${lng.toFixed(4)})`, 'info', 2000);
}

function _renderMarker(pt) {
  if (!manualState.layer) {
    manualState.layer = L.layerGroup().addTo(state.map);
    state.layers.manualPointsLayer = manualState.layer;
  }
  const icon = L.divIcon({
    html: `<div style="width:32px;height:32px;background:#d97706;border:2.5px solid #fff;border-radius:6px;display:flex;align-items:center;justify-content:center;color:#fff;font-weight:800;font-size:11px;box-shadow:0 2px 8px rgba(0,0,0,.32)">${pt.id}</div>`,
    iconSize: [32, 32], iconAnchor: [16, 16], className: ''
  });
  const marker = L.marker([pt.lat, pt.lon], { icon })
    .bindTooltip(`${pt.id} — ${pt.lat.toFixed(4)}, ${pt.lon.toFixed(4)}`, { sticky: true });
  marker.on('click', () => _showPointResult(pt.id));
  pt._marker = marker;
  manualState.layer.addLayer(marker);
}

function _updatePointList() {
  const container = document.getElementById('manual-point-list');
  container.innerHTML = '';
  manualState.points.forEach(pt => {
    const row = document.createElement('div');
    row.className = 'manual-point-item';
    row.innerHTML = `<span><span class="point-badge">${pt.id}</span><span class="point-coords">${pt.lat.toFixed(4)}, ${pt.lon.toFixed(4)}</span></span><button class="btn-remove-point" onclick="removeManualPoint('${pt.id}')" title="Supprimer">✕</button>`;
    container.appendChild(row);
  });
}

function removeManualPoint(id) {
  const idx = manualState.points.findIndex(p => p.id === id);
  if (idx === -1) return;
  const pt = manualState.points[idx];
  if (pt._marker && manualState.layer) manualState.layer.removeLayer(pt._marker);
  manualState.points.splice(idx, 1);
  _updatePointList();
  if (manualState.points.length === 0) {
    document.getElementById('btn-clear-manual').disabled = true;
    document.getElementById('btn-run-manual').disabled = true;
    document.getElementById('manual-results').classList.add('hidden');
    manualState.analysisResult = null;
  }
}

function clearManualPoints() {
  manualState.points.forEach(pt => { if (pt._marker && manualState.layer) manualState.layer.removeLayer(pt._marker); });
  manualState.points = []; manualState.analysisResult = null;
  _updatePointList();
  document.getElementById('btn-clear-manual').disabled = true;
  document.getElementById('btn-run-manual').disabled = true;
  document.getElementById('manual-results').classList.add('hidden');
  ['manualRingR1','manualRingR2','manualRingR3'].forEach(k => { if (state.layers[k]) { state.map.removeLayer(state.layers[k]); state.layers[k] = null; } });
}

async function runManualCheck() {
  if (!state.osmData || !state.bbox || manualState.points.length === 0) { toast('Chargez OSM et placez au moins un point', 'warning'); return; }
  setStatus('loading', 'Analyse points…');
  document.getElementById('btn-run-manual').disabled = true;
  document.getElementById('btn-run-manual').textContent = '⏳ Analyse en cours…';
  const params = {
    pph: parseFloat(document.getElementById('p-pph').value)||5.0, waste_kg: parseFloat(document.getElementById('p-waste').value)||0.42,
    grid_m: parseFloat(document.getElementById('p-grid').value)||200, r1_m: parseFloat(document.getElementById('p-r1').value)||150,
    r2_m: parseFloat(document.getElementById('p-r2').value)||300, r3_m: parseFloat(document.getElementById('p-r3').value)||500,
    fill_threshold: parseFloat(document.getElementById('p-fill').value)||0.8,
    truck_access_m: parseFloat(document.getElementById('p-truck-access').value)||50, truck_min_road_w: parseFloat(document.getElementById('p-truck-roadw').value)||3.5,
    tricycle_access_m: parseFloat(document.getElementById('p-tricycle-access').value)||100, tricycle_min_road_w: parseFloat(document.getElementById('p-tricycle-roadw').value)||2.0,
    truck_bin_kg: parseFloat(document.getElementById('p-cap-truck').value)||240, tricycle_bin_kg: parseFloat(document.getElementById('p-cap-tricycle').value)||80,
    foot_capacity_kg: parseFloat(document.getElementById('p-cap-foot').value)||4,
    min_school_m: parseFloat(document.getElementById('p-school').value)||20, min_hospital_m: parseFloat(document.getElementById('p-hospital').value)||20,
    min_hydro_m: parseFloat(document.getElementById('p-hydro').value)||15,
    weight_waste: parseFloat(document.getElementById('p-wgt-waste').value)||0.45, weight_access: parseFloat(document.getElementById('p-wgt-access').value)||0.25,
    weight_sensitive: parseFloat(document.getElementById('p-wgt-sensitive').value)||0.15, weight_hydro: parseFloat(document.getElementById('p-wgt-hydro').value)||0.15,
    w1: parseFloat(document.getElementById('p-w1').value)||0.60, w2: parseFloat(document.getElementById('p-w2').value)||0.30, w3: parseFloat(document.getElementById('p-w3').value)||0.10,
  };
  try {
    const resp = await fetch('/api/manual_check', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ points: manualState.points.map(p => ({ id: p.id, lat: p.lat, lon: p.lon })), osm_data: state.osmData, bbox: state.bbox, params, aoi: state.aoiGeoJSON }),
    });
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const data = await resp.json();
    manualState.analysisResult = data;
    setStatus('done', 'Points analysés');
    _renderManualResults(data);
    data.points.forEach(pt => {
      const orig = manualState.points.find(p => p.id === pt.id);
      if (orig?._marker) {
        const color = pt.viability === 'high' ? '#059669' : pt.viability === 'medium' ? '#d97706' : '#dc2626';
        orig._marker.setIcon(L.divIcon({ html: `<div style="width:32px;height:32px;background:${color};border:2.5px solid #fff;border-radius:6px;display:flex;align-items:center;justify-content:center;color:#fff;font-weight:800;font-size:11px;box-shadow:0 2px 8px rgba(0,0,0,.32)">${pt.id}</div>`, iconSize: [32,32], iconAnchor: [16,16], className: '' }));
      }
    });
    toast(`✅ ${data.points.length} points analysés`, 'success', 3000);
  } catch(e) {
    setStatus('error', 'Erreur'); toast('Erreur analyse points : ' + e.message, 'error');
  } finally {
    document.getElementById('btn-run-manual').disabled = false;
    document.getElementById('btn-run-manual').innerHTML = '🔍 Analyser ces points';
  }
}

function _renderManualResults(data) {
  const container = document.getElementById('manual-results');
  container.classList.remove('hidden');
  const { points, collective } = data;
  const vibColors = { high: '#1A7A4A', medium: '#B87B00', low: '#C0392B' };
  const vibPct    = { high: 85, medium: 55, low: 25 };
  const vibLabels = { high: 'Élevée', medium: 'Modérée', low: 'Faible' };
  const modeIcons = { truck: '🚚', tricycle: '🛺', foot: '🚶' };

  const cardsHtml = points.map(pt => {
    const vc = vibColors[pt.viability] || '#B87B00';
    const vp = vibPct[pt.viability] || 55;
    const vl = vibLabels[pt.viability] || 'Modérée';
    const mi = modeIcons[pt.collection_mode] || '🗑';
    const warns = [];
    if (pt.school_dist_m < 30) warns.push(`École à ${pt.school_dist_m}m`);
    if (pt.hospital_dist_m < 30) warns.push(`Hôpital à ${pt.hospital_dist_m}m`);
    if (pt.hydro_dist_m < 20) warns.push(`Eau à ${pt.hydro_dist_m}m`);
    return `<div class="manual-point-card">
      <div class="manual-point-card-header" onclick="toggleManualCard('mc-${pt.id}'); _showManualRings(${JSON.stringify(pt)})">
        <div style="display:flex;align-items:center;gap:8px">
          <span class="point-id">${pt.id}</span>
          <span style="font-size:11px;color:#5A6E84">${mi} ${pt.collection_mode} · ${pt.pickups_per_week}×/sem</span>
        </div>
        <span class="point-score-chip" style="background:${vc}">${vl} · ${pt.score.toFixed(2)}</span>
      </div>
      <div id="mc-${pt.id}" class="manual-point-card-body" style="display:none">
        <div class="viability-bar-wrap"><div class="viability-bar-fill" style="width:${vp}%;background:${vc}"></div></div>
        ${warns.length ? `<div style="font-size:11px;color:#8a5a00;margin-bottom:6px">⚠ ${warns.join(' · ')}</div>` : ''}
        <div class="info-row"><span class="info-label">Coordonnées</span><span class="info-value" style="font-family:monospace;font-size:10.5px">${pt.lat.toFixed(5)}, ${pt.lon.toFixed(5)}</span></div>
        <div class="info-row"><span class="info-label">Score MCDA</span><span class="info-value">${pt.score.toFixed(4)}</span></div>
        <div class="info-row"><span class="info-label">Route la plus proche</span><span class="info-value">${pt.road_dist_m}m (${pt.road_type}, ${pt.road_width_m}m)</span></div>
        <div class="info-row"><span class="info-label">École / Hôpital / Eau</span><span class="info-value">${pt.school_dist_m} / ${pt.hospital_dist_m} / ${pt.hydro_dist_m}m</span></div>
        <div class="info-section-title">Zone d'influence</div>
        <div class="info-row"><span class="info-label">R1 (${pt.r1_m}m)</span><span class="info-value">${pt.population_r1} pers. · ${pt.waste_r1_kg_day}kg/j · ${pt.r1_pickups_per_week}×/sem</span></div>
        <div class="info-row"><span class="info-label">R2 (${pt.r2_m}m)</span><span class="info-value">${pt.population_r2} pers. · ${pt.waste_r2_kg_day}kg/j</span></div>
        <div class="info-row"><span class="info-label">R3 (${pt.r3_m}m)</span><span class="info-value">${pt.population_r3} pers. · ${pt.waste_r3_kg_day}kg/j</span></div>
        <div class="info-row"><span class="info-label">Demande pondérée</span><span class="info-value">${pt.weighted_pop} pers. · ${pt.weighted_waste_kg_day}kg/j</span></div>
        <div class="info-row"><span class="info-label">Mode collecte</span><span class="info-value">${mi} ${pt.collection_mode} · ${pt.assigned_capacity_kg}kg</span></div>
        <div class="info-row"><span class="info-label">Fréquence</span><span class="info-value">${pt.pickups_per_week}×/sem · toutes ${pt.days_between_pickups}j</span></div>
        <div class="mini-note" style="margin-top:7px;font-size:10.5px">${pt.note || 'Résultat indicatif — valider sur le terrain avant toute décision.'}</div>
      </div>
    </div>`;
  }).join('');

  container.innerHTML = `
    <div class="manual-indicative-banner">Résultats indicatifs — points manuels</div>
    ${cardsHtml}
    <div class="manual-collective-box">
      <div class="manual-collective-title">Synthèse collective — ${points.length} point${points.length > 1 ? 's' : ''}</div>
      <div class="manual-kpi-grid">
        <div class="manual-kpi"><div class="manual-kpi-val">${collective.total_pop_r1.toLocaleString()}</div><div class="manual-kpi-lbl">pop. zone R1</div></div>
        <div class="manual-kpi"><div class="manual-kpi-val">${collective.total_waste_r1.toFixed(1)}</div><div class="manual-kpi-lbl">kg/jour R1</div></div>
        <div class="manual-kpi"><div class="manual-kpi-val">${collective.coverage_pct}%</div><div class="manual-kpi-lbl">couverture AOI</div></div>
      </div>
      <div style="font-size:11px;color:#3D5166;margin-top:7px">${collective.note}</div>
    </div>`;
}

function toggleManualCard(id) { const el = document.getElementById(id); if (el) el.style.display = el.style.display === 'none' ? '' : 'none'; }

function _showManualRings(pt) {
  ['manualRingR1','manualRingR2','manualRingR3'].forEach(k => { if (state.layers[k]) { state.map.removeLayer(state.layers[k]); state.layers[k] = null; } });
  const r1 = pt.r1_m || 150, r2 = pt.r2_m || 300, r3 = pt.r3_m || 500;
  state.layers.manualRingR1 = L.circle([pt.lat, pt.lon], { radius: r1, color: '#d97706', fillColor:'#d97706', fillOpacity: 0.04, weight: 1.5, dashArray: '6,4' }).bindTooltip(`R1 · ${pt.population_r1} pers. · ${pt.waste_r1_kg_day}kg/j`, { sticky: true }).addTo(state.map);
  state.layers.manualRingR2 = L.circle([pt.lat, pt.lon], { radius: r2, color: '#f59e0b', fillColor:'#f59e0b', fillOpacity: 0.03, weight: 1.2, dashArray: '4,4' }).bindTooltip(`R2 · ${pt.population_r2} pers. · ${pt.waste_r2_kg_day}kg/j`, { sticky: true }).addTo(state.map);
  state.layers.manualRingR3 = L.circle([pt.lat, pt.lon], { radius: r3, color: '#fbbf24', fillColor:'#fbbf24', fillOpacity: 0.02, weight: 1, dashArray: '3,5' }).bindTooltip(`R3 · ${pt.population_r3} pers. · ${pt.waste_r3_kg_day}kg/j`, { sticky: true }).addTo(state.map);
}

function _showPointResult(id) {
  if (!manualState.analysisResult) return;
  const pt = manualState.analysisResult.points.find(p => p.id === id);
  if (!pt) return;
  _showManualRings(pt);
  const card = document.getElementById(`mc-${id}`);
  if (card) card.style.display = '';
  document.getElementById('manual-results').scrollIntoView({ behavior: 'smooth' });
}
