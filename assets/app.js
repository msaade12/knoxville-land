/* Knoxville Land Scout ------------------------------------------------- */
'use strict';

const REPO   = 'msaade12/knoxville-land';
const BRANCH = 'main';
const HIDDEN_PATH = 'data/hidden.json';
const LS_HIDDEN = 'kls-hidden-v2';
const LS_TOKEN  = 'kls-ghtoken-v1';
const LS_LEGACY = 'ktl-hidden-v1';       // the old GitHub page's key, by URL

const KNOX = [35.9606, -83.9207];
const BANDS = [
  { max: 5000,     label: 'under $5k/ac', color: '#1F5C40' },
  { max: 8000,     label: '$5–8k/ac',     color: '#4F8F5E' },
  { max: 12000,    label: '$8–12k/ac',    color: '#C9A227' },
  { max: 17000,    label: '$12–17k/ac',   color: '#B4652A' },
  { max: Infinity, label: '$17k+/ac',     color: '#8C3B3B' },
];
const NEW_DAYS = 10;                     // "new" = firstSeen within N days

const $  = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const fmt$ = n => '$' + n.toLocaleString('en-US');
const fmtK = n => n >= 1000 ? '$' + Math.round(n / 1000) + 'k' : '$' + n;
const esc = s => String(s ?? '').replace(/[&<>"']/g,
  c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const milesFrom = (lat, lon) => {
  const R = 3958.8, rad = d => d * Math.PI / 180;
  const dLat = rad(lat - KNOX[0]), dLon = rad(lon - KNOX[1]);
  const a = Math.sin(dLat / 2) ** 2
          + Math.cos(rad(KNOX[0])) * Math.cos(rad(lat)) * Math.sin(dLon / 2) ** 2;
  return Math.round(2 * R * Math.asin(Math.sqrt(a)) * 10) / 10;
};
const daysAgo = iso => {
  const d = Date.parse(iso);
  return Number.isNaN(d) ? Infinity : Math.floor((Date.now() - d) / 86400000);
};

const state = {
  tracts: [],
  view: [],
  hidden: {},          // id -> { h:bool, at:ISO }
  markers: new Map(),
  showHidden: false,
  token: null,
  hiddenSha: null,
  lb: { list: [], i: 0 },
};

/* ───────────────────────────── hidden store ───────────────────────────── */

const loadLocalHidden = () => {
  try { return JSON.parse(localStorage.getItem(LS_HIDDEN)) || {}; }
  catch { return {}; }
};
const saveLocalHidden = () => {
  try { localStorage.setItem(LS_HIDDEN, JSON.stringify(state.hidden)); }
  catch { /* private mode */ }
};
const isHidden = id => !!state.hidden[id]?.h;

/** Merge two hidden-maps, newest timestamp per id wins. */
function mergeHidden(a, b) {
  const out = { ...a };
  for (const [id, rec] of Object.entries(b || {})) {
    if (!rec || typeof rec !== 'object') continue;
    if (!out[id] || String(rec.at || '') > String(out[id].at || '')) out[id] = rec;
  }
  return out;
}

/** One-time import of the old localStorage key, which was keyed by listing URL. */
function importLegacyHidden() {
  let old;
  try { old = JSON.parse(localStorage.getItem(LS_LEGACY)); } catch { return; }
  if (!old) return;
  const urls = new Set(Array.isArray(old) ? old : Object.keys(old));
  if (!urls.size) return;
  const at = new Date(0).toISOString();       // lowest priority in a merge
  let n = 0;
  for (const t of state.tracts) {
    if (urls.has(t.url) && !state.hidden[t.id]) {
      state.hidden[t.id] = { h: true, at }; n++;
    }
  }
  if (n) saveLocalHidden();
}

/* ───────────────────────────── GitHub sync ───────────────────────────── */

/** base64 -> UTF-8 string, without the deprecated escape()/unescape(). */
function b64decode(b64) {
  const bin = atob(String(b64).replace(/\s+/g, ''));
  const bytes = Uint8Array.from(bin, c => c.charCodeAt(0));
  return new TextDecoder().decode(bytes);
}
function b64encode(str) {
  const bytes = new TextEncoder().encode(str);
  let bin = '';
  bytes.forEach(b => { bin += String.fromCharCode(b); });
  return btoa(bin);
}

const gh = async (path, opts = {}) => {
  const r = await fetch(`https://api.github.com/repos/${REPO}/${path}`, {
    ...opts,
    headers: {
      Accept: 'application/vnd.github+json',
      ...(state.token ? { Authorization: `Bearer ${state.token}` } : {}),
      ...(opts.headers || {}),
    },
  });
  return r;
};

/** Read hidden.json from the public site (no token needed). */
async function pullHidden() {
  try {
    const r = await fetch(`${HIDDEN_PATH}?t=${Date.now()}`, { cache: 'no-store' });
    if (!r.ok) return null;
    return await r.json();
  } catch { return null; }
}

/** Write the merged map back to the repo. Needs a token. */
async function pushHidden() {
  if (!state.token) return { ok: false, msg: 'No token saved.' };
  try {
    // always re-read the sha so we don't clobber another device's write
    const head = await gh(`contents/${HIDDEN_PATH}?ref=${BRANCH}`);
    let sha = null, remote = {};
    if (head.ok) {
      const j = await head.json();
      sha = j.sha;
      try { remote = JSON.parse(b64decode(j.content)); } catch { remote = {}; }
    } else if (head.status !== 404) {
      return { ok: false, msg: `GitHub said ${head.status}.` };
    }
    state.hidden = mergeHidden(state.hidden, remote);
    saveLocalHidden();

    const body = JSON.stringify(state.hidden, null, 1);
    const put = await gh(`contents/${HIDDEN_PATH}`, {
      method: 'PUT',
      body: JSON.stringify({
        message: `hidden listings — ${new Date().toISOString().slice(0, 10)}`,
        content: b64encode(body),
        branch: BRANCH,
        ...(sha ? { sha } : {}),
      }),
    });
    if (!put.ok) {
      const t = await put.text();
      return { ok: false, msg: put.status === 403 || put.status === 401
        ? 'Token rejected — needs Contents: read & write on this repo.'
        : `GitHub said ${put.status}. ${t.slice(0, 90)}` };
    }
    return { ok: true, msg: `Synced ${Object.keys(state.hidden).length} entries.` };
  } catch (e) {
    return { ok: false, msg: 'Network error: ' + e.message };
  }
}

let pushTimer = null;
function queuePush() {
  if (!state.token) return;
  clearTimeout(pushTimer);
  pushTimer = setTimeout(async () => {
    const r = await pushHidden();
    setSyncState(r.ok ? 'Saved to GitHub.' : r.msg, r.ok);
  }, 2500);
}

const setSyncState = (msg, ok) => {
  const el = $('#syncState');
  if (!el) return;
  el.textContent = msg;
  el.className = 'syncstate ' + (ok ? 'ok' : msg ? 'err' : '');
};

/* ────────────────────────────────── map ──────────────────────────────── */

let map, markerLayer, countyLayer, ringLayer, baseLayers = {}, currentBase;

function initMap() {
  map = L.map('map', {
    center: [36.02, -84.05], zoom: 9, zoomControl: true,
    preferCanvas: false, worldCopyJump: false,
  });
  L.control.scale({ imperial: true, metric: false, position: 'bottomright' }).addTo(map);

  const esri = (svc, attr) => L.tileLayer(
    `https://server.arcgisonline.com/ArcGIS/rest/services/${svc}/MapServer/tile/{z}/{y}/{x}`,
    { maxZoom: 18, attribution: attr });

  baseLayers = {
    imagery: esri('World_Imagery', 'Imagery &copy; Esri, Maxar, Earthstar Geographics'),
    topo:    esri('World_Topo_Map', 'Tiles &copy; Esri'),
    street:  esri('World_Street_Map', 'Tiles &copy; Esri'),
    plain:   L.tileLayer('https://{s}.basemaps.cartocdn.com/rastertiles/voyager/{z}/{x}/{y}{r}.png', {
      subdomains: 'abcd', maxZoom: 19,
      attribution: '&copy; OpenStreetMap contributors &copy; CARTO',
    }),
  };
  // place labels sit on top of the satellite imagery, which has none
  const labels = L.tileLayer(
    'https://server.arcgisonline.com/ArcGIS/rest/services/Reference/World_Boundaries_and_Places/MapServer/tile/{z}/{y}/{x}',
    { maxZoom: 18, pane: 'shadowPane' });

  currentBase = baseLayers.imagery.addTo(map);
  labels.addTo(map);
  map.__labels = labels;

  ringLayer = L.layerGroup().addTo(map);
  [[13, '15 min'], [26, '30 min'], [38, '45 min']].forEach(([mi, label]) => {
    L.circle(KNOX, {
      radius: mi * 1609.34, fill: false, color: '#fff', weight: 1.4,
      opacity: .55, dashArray: '5 7', interactive: false,
    }).addTo(ringLayer);
    L.marker([KNOX[0] + (mi * 1609.34) / 111320, KNOX[1]], {
      interactive: false,
      icon: L.divIcon({
        className: '',
        html: `<span style="font:600 10px/1 'IBM Plex Mono',monospace;color:#fff;
               text-shadow:0 1px 3px rgba(0,0,0,.85);white-space:nowrap">${label}</span>`,
        iconSize: [46, 12], iconAnchor: [23, 6],
      }),
    }).addTo(ringLayer);
  });

  L.marker(KNOX, {
    interactive: false,
    icon: L.divIcon({
      className: '',
      html: `<span style="font:600 12px/1 Bitter,Georgia,serif;color:#fff;
             text-shadow:0 1px 4px rgba(0,0,0,.9)">Knoxville</span>`,
      iconSize: [70, 14], iconAnchor: [35, 7],
    }),
  }).addTo(map);

  markerLayer = L.layerGroup().addTo(map);

  $$('#basemaps button').forEach(b => b.addEventListener('click', () => {
    $$('#basemaps button').forEach(x => x.classList.toggle('on', x === b));
    map.removeLayer(currentBase);
    currentBase = baseLayers[b.dataset.base].addTo(map);
    currentBase.bringToBack();
    const imagery = b.dataset.base === 'imagery';
    if (imagery) map.__labels.addTo(map); else map.removeLayer(map.__labels);
  }));
}

function loadCounties() {
  fetch('data/counties.json').then(r => r.json()).then(gj => {
    countyLayer = L.geoJSON(gj, {
      interactive: false,
      style: f => f.properties.t === 1
        ? { color: '#fff', weight: 1.5, opacity: .65, fill: false }
        : { color: '#fff', weight: .6, opacity: .22, fill: false },
    }).addTo(map);
    countyLayer.bringToBack();
  }).catch(() => {});
}

const pinSize = a => Math.round(Math.max(28, Math.min(52, 28 + (a - 10) * 0.62)));

function makeIcon(t) {
  const d = pinSize(t.acres);
  const cls = ['pin'];
  if (t.geo !== 'parcel') cls.push('approx');
  if (isNew(t)) cls.push('isnew');
  const ink = t.bandIdx === 2 ? '#2A2208' : '#fff';
  return L.divIcon({
    className: '',
    html: `<div class="${cls.join(' ')}" data-id="${t.id}" style="width:${d}px;height:${d}px;
           background:${t.color};color:${ink};font-size:${d < 34 ? 11 : 13}px">${t.drive}</div>`,
    iconSize: [d, d], iconAnchor: [d / 2, d / 2], popupAnchor: [0, -d / 2],
  });
}

const isNew = t => !t.baseline && daysAgo(t.firstSeen) <= NEW_DAYS;

/** Plain-language terrain from the averaged hillside slope. */
const terrain = s => s == null ? null
  : s < 3  ? 'flat'
  : s < 6  ? 'gentle'
  : s < 10 ? 'rolling'
  : s < 15 ? 'hilly'
  : 'steep';
const priceCut = t => {
  const h = t.priceHistory || [];
  return h.length > 1 && h[h.length - 1].price < h[0].price;
};

function popupHtml(t) {
  const img = t.img
    ? `<img class="pop-photo" src="${esc(t.img)}" alt="${esc(t.address)}" loading="lazy" data-zoom="${t.id}">`
    : `<div class="pop-photo empty">no photo published</div>`;
  const cut = priceCut(t)
    ? `<span class="tag cut">cut ${fmtK(t.priceHistory[0].price - t.price)}</span>` : '';
  const nw = isNew(t) ? '<span class="tag new">new</span>' : '';
  const un = t.status !== 'active'
    ? '<span class="tag unconf">unconfirmed</span>' : '';
  const gmaps = `https://www.google.com/maps/search/?api=1&query=${encodeURIComponent(
    t.geo === 'parcel' ? `${t.lat},${t.lon}` : `${t.address}, ${t.town}, TN`)}`;
  return `
    ${img}
    <div class="pop-body">
      <div class="pop-head">
        <span class="pop-acres">${t.acres} ac</span>
        <span class="pop-price">${fmt$(t.price)}</span>
      </div>
      <div class="pop-where">${esc(t.town)}, ${esc(t.county)} County ${nw}${cut}${un}</div>
      <div class="pop-addr">${esc(t.address)}</div>
      <div class="pop-grid">
        <div><b>${fmt$(t.ppa)}</b><span>per acre</span></div>
        <div><b>${t.drive} min</b><span>${t.miles} mi straight line</span></div>
        <div><b>${t.daysListed ?? '–'}</b><span>days listed</span></div>
        <div><b>${t.geo === 'parcel' ? 'Parcel' : 'Town'}</b><span>pin accuracy</span></div>
        ${t.slope != null ? `<div><b>${terrain(t.slope)}</b><span>${t.slope}° slope${
          t.elev != null ? `, ${Math.round(t.elev * 3.281)} ft` : ''}</span></div>` : ''}
        ${t.groceryMin != null ? `<div style="grid-column:1/-1"><b>${t.groceryMin} min
          to ${esc(t.groceryName)}</b><span>nearest groceries (${t.groceryMi} mi)</span></div>` : ''}
      </div>
      <div class="pop-actions">
        <a class="primary" href="${esc(t.url)}" target="_blank" rel="noopener">Listing</a>
        <a href="${gmaps}" target="_blank" rel="noopener">Maps</a>
        <button data-hide="${t.id}">${isHidden(t.id) ? 'Unhide' : 'Hide'}</button>
      </div>
    </div>`;
}

/* ──────────────────────────────── cards ──────────────────────────────── */

function cardHtml(t) {
  const thumb = t.img
    ? `<img class="thumb" src="${esc(t.img)}" alt="" loading="lazy" decoding="async">`
    : `<div class="thumb empty">no<br>photo</div>`;
  const tags = [
    isNew(t) ? '<span class="tag new">new</span>' : '',
    priceCut(t) ? `<span class="tag cut">price cut</span>` : '',
    t.status !== 'active' ? '<span class="tag unconf">unconfirmed</span>' : '',
    `<span class="tag">${t.drive} min</span>`,
    t.groceryMin != null
      ? `<span class="tag groc" title="to ${esc(t.groceryName)}">${t.groceryMin} min shops</span>` : '',
    t.slope != null
      ? `<span class="tag terr" title="${t.slope}° average slope">${terrain(t.slope)}</span>` : '',
    `<span class="tag">${t.daysListed ?? '–'}d listed</span>`,
  ].join('');
  return `
    <article class="card${isNew(t) ? ' isnew' : ''}${t.status !== 'active' ? ' unconfirmed' : ''}${isHidden(t.id) ? ' hidden-row' : ''}"
             data-id="${t.id}" role="listitem" tabindex="0">
      ${thumb}
      <div class="cbody">
        <div class="cline1">
          <span class="cacres">${t.acres} ac</span>
          <span class="cppa" style="background:${t.color};${t.bandIdx === 2 ? 'color:#2A2208' : ''}">${fmt$(t.ppa)}/ac</span>
          <span class="cprice">${fmt$(t.price)}</span>
        </div>
        <div class="cwhere">${esc(t.town)}, ${esc(t.county)} County</div>
        <div class="caddr">${esc(t.address)}</div>
        <div class="cmeta">${tags}</div>
      </div>
      <button class="hidebtn" data-hide="${t.id}">${isHidden(t.id) ? 'Unhide' : 'Hide'}</button>
    </article>`;
}

/* ─────────────────────────────── filtering ───────────────────────────── */

function currentFilters() {
  return {
    drive: +$('#fDrive').value,
    groc: +$('#fGroc').value,
    price: +$('#fPrice').value,
    acres: +$('#fAcres').value,
    county: $('#fCounty').value,
    sort: $('#fSort').value,
    onlyNew: $('#fNew').checked,
    onlyCut: $('#fCut').checked,
    onlyPhoto: $('#fPhoto').checked,
    onlyConfirmed: $('#fConfirmed').checked,
    q: $('#search').value.trim().toLowerCase(),
  };
}

function apply() {
  const f = currentFilters();
  let rows = state.tracts.filter(t => {
    if (!state.showHidden && isHidden(t.id)) return false;
    if (t.drive > f.drive) return false;
    if (t.groceryMin != null && t.groceryMin > f.groc) return false;
    if (t.price > f.price) return false;
    if (t.acres < f.acres) return false;
    if (f.county && t.county !== f.county) return false;
    if (f.onlyNew && !isNew(t)) return false;
    if (f.onlyCut && !priceCut(t)) return false;
    if (f.onlyPhoto && !t.img) return false;
    if (f.onlyConfirmed && t.status !== 'active') return false;
    if (f.q) {
      const hay = `${t.town} ${t.county} ${t.address} ${t.zip || ''}`.toLowerCase();
      if (!hay.includes(f.q)) return false;
    }
    return true;
  });

  const cmp = {
    ppa: (a, b) => a.ppa - b.ppa,
    price: (a, b) => a.price - b.price,
    priceDesc: (a, b) => b.price - a.price,
    acres: (a, b) => b.acres - a.acres,
    drive: (a, b) => a.drive - b.drive,
    new: (a, b) => String(b.firstSeen).localeCompare(String(a.firstSeen)) || a.ppa - b.ppa,
  }[f.sort];
  rows.sort(cmp);
  state.view = rows;

  renderCards(rows);
  renderMarkers(rows);
  renderCounts(rows);
  renderNewBanner();
}

function renderNewBanner() {
  const el = $('#newBanner');
  const all = state.tracts.filter(isNew);
  if (!all.length) { el.hidden = true; return; }
  el.hidden = false;
  const on = $('#fNew').checked;
  el.classList.toggle('on', on);
  const when = all.map(t => t.firstSeen).sort().reverse()[0];
  const nice = new Date(when + 'T00:00:00').toLocaleDateString('en-US',
    { month: 'short', day: 'numeric' });
  el.innerHTML = on
    ? `Showing all <b>${all.length}</b> new since ${nice}
       <span class="nb-act">back to my filters</span>`
    : `<b>${all.length}</b> new since ${nice}
       <span class="nb-act">show only these</span>`;
}

function renderCounts(rows) {
  const nNew = rows.filter(isNew).length;
  const nHid = Object.values(state.hidden).filter(v => v.h).length;
  $('#resultCount').textContent =
    `${rows.length} of ${state.tracts.length} tracts` + (nNew ? ` · ${nNew} new` : '');
  $('#hideCount').textContent = `${nHid} hidden`;
  $('#showHidden').textContent = state.showHidden ? 'Hide hidden' : 'Show hidden';
}

function renderCards(rows) {
  const box = $('#cards');
  if (!rows.length) {
    box.innerHTML = `<div class="empty-state">No tracts match these filters.<br>
      Try widening drive time or clearing the search.</div>`;
    return;
  }
  box.innerHTML = rows.map(cardHtml).join('');
}

function renderMarkers(rows) {
  markerLayer.clearLayers();
  state.markers.clear();
  rows.forEach(t => {
    const m = L.marker([t.lat, t.lon], {
      icon: makeIcon(t),
      opacity: isHidden(t.id) ? 0.45 : 1,
      riseOnHover: true,
      zIndexOffset: isNew(t) ? 500 : 0,
    });
    m.bindPopup(() => popupHtml(t), { maxWidth: 300, autoPanPadding: [30, 30] });
    m.on('mouseover', () => highlight(t.id, true));
    m.on('mouseout',  () => highlight(t.id, false));
    m.addTo(markerLayer);
    state.markers.set(t.id, m);
  });
}

function highlight(id, on) {
  const card = $(`.card[data-id="${id}"]`);
  if (card) {
    card.classList.toggle('hot', on);
    if (on) card.scrollIntoView({ block: 'nearest' });
  }
  const m = state.markers.get(id);
  const el = m?.getElement()?.querySelector('.pin');
  if (el) el.classList.toggle('hot', on);
}

function focusTract(id) {
  const t = state.tracts.find(x => x.id === id);
  const m = state.markers.get(id);
  if (!t || !m) return;
  if (document.body.classList.contains('view-list')) setView('map');
  map.flyTo([t.lat, t.lon], Math.max(map.getZoom(), 12), { duration: .55 });
  setTimeout(() => m.openPopup(), 380);
}

/* ─────────────────────────────── lightbox ────────────────────────────── */

function openLightbox(id) {
  const list = state.view.filter(t => t.img);
  const i = list.findIndex(t => t.id === id);
  if (i < 0) return;
  state.lb = { list, i };
  paintLightbox();
  $('#lightbox').hidden = false;
}

function paintLightbox() {
  const t = state.lb.list[state.lb.i];
  if (!t) return;
  $('#lbImg').src = t.img;
  $('#lbImg').alt = `${t.acres} acres in ${t.town}, ${t.county} County`;
  $('#lbCap').innerHTML =
    `<b>${t.acres} ac · ${fmt$(t.price)}</b> · ${fmt$(t.ppa)}/ac<br>
     ${esc(t.address)} — ${esc(t.town)}, ${esc(t.county)} County ·
     ${t.drive} min from Knoxville<br>
     <a href="${esc(t.url)}" target="_blank" rel="noopener">Open listing ↗</a>
     &nbsp;·&nbsp; ${state.lb.i + 1} of ${state.lb.list.length}`;
}

const stepLightbox = d => {
  const n = state.lb.list.length;
  if (!n) return;
  state.lb.i = (state.lb.i + d + n) % n;
  paintLightbox();
};
const closeLightbox = () => { $('#lightbox').hidden = true; };

/* ──────────────────────────────── hides ──────────────────────────────── */

function toggleHide(id) {
  const now = new Date().toISOString();
  state.hidden[id] = { h: !isHidden(id), at: now };
  saveLocalHidden();
  queuePush();
  apply();
}

/* ──────────────────────────────── view ───────────────────────────────── */

function setView(v) {
  document.body.classList.toggle('view-map', v === 'map');
  document.body.classList.toggle('view-list', v === 'list');
  $('#tabMap').setAttribute('aria-selected', v === 'map');
  $('#tabList').setAttribute('aria-selected', v === 'list');
  if (v === 'map') setTimeout(() => map.invalidateSize(), 60);
}

/* ──────────────────────────────── wiring ─────────────────────────────── */

function wire() {
  ['#fDrive', '#fPrice', '#fAcres', '#fCounty', '#fSort',
   '#fNew', '#fCut', '#fPhoto', '#fConfirmed', '#fGroc'].forEach(sel =>
    $(sel).addEventListener('input', () => { syncOutputs(); apply(); }));

  let searchTimer;
  $('#search').addEventListener('input', () => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(apply, 140);
  });

  $('#newBanner').addEventListener('click', () => {
    const cb = $('#fNew');
    cb.checked = !cb.checked;
    if (cb.checked) {
      // Remember the filters, then widen everything so that "show only the new
      // ones" really shows all of them - a new tract 70 minutes out still counts.
      state.savedFilters = {
        drive: $('#fDrive').value, price: $('#fPrice').value,
        acres: $('#fAcres').value, county: $('#fCounty').value,
        sort: $('#fSort').value, confirmed: $('#fConfirmed').checked,
        groc: $('#fGroc').value,
        q: $('#search').value,
      };
      $('#fDrive').value = $('#fDrive').max;
      $('#fPrice').value = $('#fPrice').max;
      $('#fAcres').value = $('#fAcres').min;
      $('#fCounty').value = '';
      $('#fGroc').value = $('#fGroc').max;
      $('#fConfirmed').checked = false;
      $('#fSort').value = 'new';
      $('#search').value = '';
    } else if (state.savedFilters) {
      const f = state.savedFilters;
      $('#fDrive').value = f.drive; $('#fPrice').value = f.price;
      $('#fAcres').value = f.acres; $('#fCounty').value = f.county;
      $('#fSort').value = f.sort;   $('#fConfirmed').checked = f.confirmed;
      $('#fGroc').value = f.groc;
      $('#search').value = f.q;
      state.savedFilters = null;
    }
    syncOutputs();
    apply();
  });

  $('#resetFilters').addEventListener('click', () => {
    $('#fDrive').value = 45; $('#fPrice').value = 250000; $('#fAcres').value = 10;
    $('#fGroc').value = 15;
    $('#fCounty').value = ''; $('#fSort').value = 'ppa';
    $('#fNew').checked = false; $('#fCut').checked = false;
    $('#fPhoto').checked = false; $('#fConfirmed').checked = false;
    $('#search').value = '';
    syncOutputs(); apply();
  });

  $('#showHidden').addEventListener('click', () => {
    state.showHidden = !state.showHidden; apply();
  });
  $('#restoreAll').addEventListener('click', () => {
    const now = new Date().toISOString();
    for (const id of Object.keys(state.hidden)) state.hidden[id] = { h: false, at: now };
    saveLocalHidden(); queuePush(); apply();
  });

  // cards: hide button, hover sync, click to focus, thumbnail to lightbox
  const cards = $('#cards');
  cards.addEventListener('click', e => {
    const hb = e.target.closest('[data-hide]');
    if (hb) { e.stopPropagation(); toggleHide(hb.dataset.hide); return; }
    const card = e.target.closest('.card');
    if (!card) return;
    if (e.target.classList.contains('thumb')) { openLightbox(card.dataset.id); return; }
    focusTract(card.dataset.id);
  });
  cards.addEventListener('keydown', e => {
    if (e.key === 'Enter' && e.target.classList?.contains('card')) focusTract(e.target.dataset.id);
  });
  cards.addEventListener('mouseover', e => {
    const c = e.target.closest('.card'); if (c) highlight(c.dataset.id, true);
  });
  cards.addEventListener('mouseout', e => {
    const c = e.target.closest('.card'); if (c) highlight(c.dataset.id, false);
  });

  // popup buttons
  map.on('popupopen', ev => {
    const root = ev.popup.getElement();
    root.querySelector('[data-hide]')?.addEventListener('click', e =>
      toggleHide(e.target.dataset.hide));
    root.querySelector('[data-zoom]')?.addEventListener('click', e =>
      openLightbox(e.target.dataset.zoom));
  });

  // lightbox
  $('#lbClose').addEventListener('click', closeLightbox);
  $('#lbPrev').addEventListener('click', () => stepLightbox(-1));
  $('#lbNext').addEventListener('click', () => stepLightbox(1));
  $('#lightbox').addEventListener('click', e => {
    if (e.target.id === 'lightbox') closeLightbox();
  });
  document.addEventListener('keydown', e => {
    if ($('#lightbox').hidden) return;
    if (e.key === 'Escape') closeLightbox();
    if (e.key === 'ArrowLeft') stepLightbox(-1);
    if (e.key === 'ArrowRight') stepLightbox(1);
  });

  // view switch
  $('#tabMap').addEventListener('click', () => setView('map'));
  $('#tabList').addEventListener('click', () => setView('list'));
  $('#filterToggle').addEventListener('click', e => {
    const f = $('#filters');
    const open = f.style.display !== 'none';
    f.style.display = open ? 'none' : '';
    e.target.setAttribute('aria-expanded', String(!open));
  });

  // sync dialog
  const dlg = $('#syncDlg');
  $('#syncBtn').addEventListener('click', () => {
    $('#ghToken').value = state.token || '';
    setSyncState(state.token ? 'Token saved in this browser.' : '', !!state.token);
    dlg.showModal();
  });
  $('#syncClose').addEventListener('click', () => dlg.close());
  $('#syncClear').addEventListener('click', () => {
    state.token = null;
    try { localStorage.removeItem(LS_TOKEN); } catch {}
    $('#ghToken').value = '';
    setSyncState('Token forgotten. Hides stay in this browser.', true);
  });
  $('#syncSave').addEventListener('click', async () => {
    const v = $('#ghToken').value.trim();
    if (!v) { setSyncState('Paste a token first.', false); return; }
    state.token = v;
    try { localStorage.setItem(LS_TOKEN, v); } catch {}
    setSyncState('Syncing…', true);
    const r = await pushHidden();
    setSyncState(r.msg, r.ok);
    if (r.ok) apply();
  });
}

function syncOutputs() {
  $('#oDrive').textContent = $('#fDrive').value + ' min';
  $('#oPrice').textContent = fmtK(+$('#fPrice').value);
  $('#oAcres').textContent = $('#fAcres').value;
  $('#oGroc').textContent = $('#fGroc').value + ' min';
}

function buildLegend() {
  $('#lgBands').innerHTML = BANDS.map(b =>
    `<div class="lg-band"><span class="lg-sw" style="background:${b.color}"></span>${b.label}</div>`
  ).join('');
}

/* ───────────────────────────────── boot ──────────────────────────────── */

async function boot() {
  initMap();
  buildLegend();
  loadCounties();
  setView('map');

  try { state.token = localStorage.getItem(LS_TOKEN); } catch {}
  state.hidden = loadLocalHidden();

  let data;
  try {
    const r = await fetch(`data/tracts.json?t=${Date.now()}`, { cache: 'no-store' });
    data = await r.json();
  } catch {
    $('#cards').innerHTML =
      '<div class="empty-state">Could not load data/tracts.json.</div>';
    return;
  }

  state.tracts = (data.tracts || []).map(t => {
    const band = BANDS.findIndex(b => t.ppa < b.max);
    return {
      ...t,
      bandIdx: t.bandIdx ?? band,
      color: t.color || BANDS[band].color,
      miles: t.miles ?? milesFrom(t.lat, t.lon),
    };
  });

  importLegacyHidden();
  const remote = await pullHidden();
  if (remote) { state.hidden = mergeHidden(state.hidden, remote); saveLocalHidden(); }

  const maxDrive = Math.max(90, ...state.tracts.map(t => t.drive || 0));
  $('#fDrive').max = Math.ceil(maxDrive / 5) * 5;

  const counties = [...new Set(state.tracts.map(t => t.county))].sort();
  $('#fCounty').insertAdjacentHTML('beforeend',
    counties.map(c => `<option value="${esc(c)}">${esc(c)} County</option>`).join(''));

  const conf = state.tracts.filter(t => t.status === 'active').length;
  const nNew = state.tracts.filter(isNew).length;
  $('#tagline').textContent =
    `${state.tracts.length} tracts · ${conf} confirmed active` +
    (nNew ? ` · ${nNew} new` : '') +
    ` · swept ${data.date || '—'}`;

  if (window.matchMedia('(max-width:860px)').matches) $('#legend').open = false;

  wire();
  syncOutputs();
  apply();

  if (state.view.length) {
    const b = L.latLngBounds(state.view.map(t => [t.lat, t.lon])).extend(KNOX);
    map.fitBounds(b, { padding: [40, 40], maxZoom: 10 });
  }
}

boot();
