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


// ── SESSION UTILISATEUR (contrôle d'accès Geo Wakanda) ─────────────────────
(function () {
  // Session expirée pendant l'utilisation → retour à la page de connexion
  const _fetch = window.fetch.bind(window);
  window.fetch = async (...args) => {
    const r = await _fetch(...args);
    const url = String(args[0] && args[0].url ? args[0].url : args[0] || '');
    if (r.status === 401 && url.startsWith('/api/') && !url.startsWith('/api/auth/')) {
      location.href = '/login?next=' + encodeURIComponent(location.pathname);
    }
    return r;
  };

  async function loadUser() {
    try {
      const r = await _fetch('/api/auth/me', { credentials: 'same-origin' });
      if (!r.ok) return;
      const u = await r.json();
      if (u.auth_enabled === false) return;
      const menu = document.getElementById('user-menu');
      if (!menu) return;
      const initials = (u.name || u.email || '?').split(/\s+/).filter(Boolean).slice(0, 2).map(w => w[0].toUpperCase()).join('');
      document.getElementById('user-avatar').textContent = initials || '?';
      document.getElementById('user-name').textContent = (u.name || '').split(' ')[0];
      document.getElementById('ud-name').textContent = u.name || '';
      document.getElementById('ud-email').textContent = u.email || '';
      document.getElementById('ud-admin').hidden = u.role !== 'admin';
      menu.hidden = false;
    } catch (_) { /* hors ligne : on ignore */ }
  }

  document.addEventListener('click', async (e) => {
    const btn = document.getElementById('user-btn');
    const drop = document.getElementById('user-drop');
    if (!btn || !drop) return;
    if (e.target.closest('#user-btn')) {
      drop.hidden = !drop.hidden; btn.setAttribute('aria-expanded', String(!drop.hidden)); return;
    }
    if (e.target.closest('#ud-logout')) {
      await _fetch('/api/auth/logout', { method: 'POST', credentials: 'same-origin' });
      location.href = '/login'; return;
    }
    if (!e.target.closest('#user-drop')) drop.hidden = true;
  });

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', loadUser); else loadUser();
})();
