/* ═══════════════════════════════════════════════════════════════════════════
   UrbanSanity — couche d'interface (shell)
   N'altère AUCUNE logique métier : uniquement navigation, accordéon,
   repli du panneau et recherche rapide de lieu / couche.
   ═══════════════════════════════════════════════════════════════════════════ */

/* ── ACCORDÉON THÉMATIQUE ────────────────────────────────────────────────── */
function toggleThematic(id, forceOpen) {
  const el = document.getElementById(id);
  if (!el) return;
  if (forceOpen) {
    el.classList.remove('hidden');
    el.classList.add('open');
    el.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
  } else {
    el.classList.toggle('open');
  }
}

/* Compatibilité avec l'ancien balisage */
function toggleSection(id) {
  const body = document.getElementById(id);
  if (!body) return;
  const sec = body.closest('.thematic');
  if (sec) sec.classList.toggle('open');
  else body.style.display = body.style.display === 'none' ? '' : 'none';
}

/* ── REPLI DE LA BARRE LATÉRALE ──────────────────────────────────────────── */
function toggleSidebar() {
  document.body.classList.toggle('sidebar-collapsed');
  setTimeout(() => {
    if (window.state && state.map) state.map.invalidateSize(true);
  }, 260);
}

/* ── RECHERCHE RAPIDE : couches locales + lieux (Nominatim) ──────────────── */
let _searchTimer = null;

function quickSearch(q) {
  const box = document.getElementById('map-search-results');
  if (!box) return;
  q = (q || '').trim();
  if (q.length < 2) { box.classList.add('hidden'); box.innerHTML = ''; return; }

  // 1) Couches correspondantes (instantané)
  const layerHits = [];
  document.querySelectorAll('#layer-toggles .layer-toggle').forEach(row => {
    const lab = row.querySelector('label');
    if (lab && lab.textContent.toLowerCase().includes(q.toLowerCase())) {
      layerHits.push({ label: lab.textContent.trim(), id: lab.getAttribute('for') });
    }
  });

  render(layerHits, []);

  // 2) Lieux (débouncé)
  clearTimeout(_searchTimer);
  _searchTimer = setTimeout(async () => {
    try {
      const url = 'https://nominatim.openstreetmap.org/search?format=json&limit=5&q=' + encodeURIComponent(q);
      const r = await fetch(url, { headers: { 'Accept-Language': 'fr' } });
      const places = await r.json();
      render(layerHits, places);
    } catch { /* silencieux : la recherche de couches reste utilisable */ }
  }, 450);

  function render(layers, places) {
    let html = '';
    if (layers.length) {
      html += '<div class="msr-group">Couches</div>';
      layers.forEach(l => {
        html += `<div class="msr-item" onclick="focusLayer('${l.id}')">
          <span class="msr-ico">▦</span><span>${l.label}</span></div>`;
      });
    }
    if (places.length) {
      html += '<div class="msr-group">Lieux</div>';
      places.forEach(p => {
        const name = (p.display_name || '').replace(/'/g, "\\'");
        html += `<div class="msr-item" onclick="gotoPlace(${p.lat},${p.lon})">
          <span class="msr-ico">◎</span><span>${p.display_name}</span></div>`;
      });
    }
    if (!html) html = '<div class="msr-empty">Aucun résultat</div>';
    box.innerHTML = html;
    box.classList.remove('hidden');
  }
}

function quickSearchGo() {
  const first = document.querySelector('#map-search-results .msr-item');
  if (first) first.click();
}

function gotoPlace(lat, lon) {
  if (window.state && state.map) state.map.setView([lat, lon], 15);
  hideSearchResults();
}

function focusLayer(inputId) {
  const cb = document.getElementById(inputId);
  if (cb) {
    if (!cb.checked) cb.click();
    toggleThematic('th-layers', true);
    cb.closest('.layer-toggle')?.classList.add('flash');
    setTimeout(() => cb.closest('.layer-toggle')?.classList.remove('flash'), 1200);
  }
  hideSearchResults();
}

function hideSearchResults() {
  const box = document.getElementById('map-search-results');
  if (box) { box.classList.add('hidden'); box.innerHTML = ''; }
}

document.addEventListener('click', e => {
  if (!e.target.closest('.map-search')) hideSearchResults();
});

/* ── OUVERTURE AUTOMATIQUE DES PANNEAUX RÉVÉLÉS PAR L'ANALYSE ────────────── */
document.addEventListener('DOMContentLoaded', () => {
  ['panel-scenarios', 'panel-export'].forEach(id => {
    const el = document.getElementById(id);
    if (!el) return;
    new MutationObserver(() => {
      if (!el.classList.contains('hidden')) el.classList.add('open');
    }).observe(el, { attributes: true, attributeFilter: ['class'] });
  });

  // Compteur de couches dans le rail
  setTimeout(() => {
    const n = document.querySelectorAll('#layer-toggles .layer-toggle').length;
    const badge = document.getElementById('rail-layer-count');
    if (badge && n) badge.textContent = n;
  }, 600);
});
