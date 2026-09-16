/* Knoxville Land Scout ------------------------------------------------- */
'use strict';

const REPO   = 'msaade12/knoxville-land';
const BRANCH = 'main';
const HIDDEN_PATH = 'data/marks.json';
const OLD_HIDDEN_PATH = 'data/hidden.json';
const LS_HIDDEN = 'kls-marks-v1';
const LS_HIDDEN_OLD = 'kls-hidden-v2';
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
  hidden: {},          // id -> { h:bool, fav:bool, lists:[], hoa:bool, at:ISO }
  ownerEx: new Set(),  // ids ruled out by hand in data/owner-excluded.json
  markers: new Map(),
  showHidden: false,
  token: null,
  hiddenSha: null,
  lb: { list: [], i: 0 },
};

/* ───────────────────────────── hidden store ───────────────────────────── */

const loadLocalHidden = () => {
  try {
    const cur = JSON.parse(localStorage.getItem(LS_HIDDEN)) || {};
    const old = JSON.parse(localStorage.getItem(LS_HIDDEN_OLD)) || {};
    return mergeHidden(old, cur);            // old hides carry over, newer wins
  } catch { return {}; }
};
const isFav = id => !!state.hidden[id]?.fav;
const listsOf = id => state.hidden[id]?.lists || [];
const allLists = () => {
  const names = new Set();
  Object.values(state.hidden).forEach(m => (m.lists || []).forEach(n => names.add(n)));
  return [...names].sort((a, b) => a.localeCompare(b));
};
/** Change one mark record, keeping the other fields. */
function setMark(id, patch) {
  const cur = state.hidden[id] || {};
  state.hidden[id] = { ...cur, ...patch, at: new Date().toISOString() };
  saveLocalHidden();
  queuePush();
  apply();
}
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
  const get = async p => {
    try {
      const r = await fetch(`${p}?t=${Date.now()}`, { cache: 'no-store' });
      return r.ok ? await r.json() : null;
    } catch { return null; }
  };
  const marks = await get(HIDDEN_PATH);
  const old = await get(OLD_HIDDEN_PATH);
  if (!marks && !old) return null;
  return mergeHidden(old || {}, marks || {});
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
        message: `favorites, lists and hides — ${new Date().toISOString().slice(0, 10)}`,
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

let map, markerLayer, countyLayer, ringLayer, floodLayer, baseLayers = {}, currentBase;

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
      opacity: .55, dashArray: '5 7', interactive: false, className: 'ring',
    }).addTo(ringLayer);
    L.marker([KNOX[0] + (mi * 1609.34) / 111320, KNOX[1]], {
      interactive: false,
      icon: L.divIcon({
        className: '',
        html: `<span class="maplabel ring-label">${label}</span>`,
        iconSize: [46, 12], iconAnchor: [23, 6],
      }),
    }).addTo(ringLayer);
  });

  L.marker(KNOX, {
    interactive: false,
    icon: L.divIcon({
      className: '',
      html: `<span class="maplabel city-label">Knoxville</span>`,
      iconSize: [70, 14], iconAnchor: [35, 7],
    }),
  }).addTo(map);

  markerLayer = L.layerGroup().addTo(map);

  // FEMA National Flood Hazard Layer, drawn per tile from the official
  // dynamic map service (layer 28 = flood hazard zones). Web-Mercator bbox
  // in, transparent PNG out - no plugin needed.
  const Nfhl = L.TileLayer.extend({
    getTileUrl(c) {
      const n = 2 ** c.z, R = 20037508.342789244;
      const x0 = -R + (c.x / n) * 2 * R, x1 = -R + ((c.x + 1) / n) * 2 * R;
      const y1 = R - (c.y / n) * 2 * R, y0 = R - ((c.y + 1) / n) * 2 * R;
      return 'https://hazards.fema.gov/arcgis/rest/services/public/NFHL/MapServer/export'
        + `?bbox=${x0},${y0},${x1},${y1}&bboxSR=3857&imageSR=3857&size=256,256`
        + '&layers=show:28&transparent=true&format=png32&f=image';
    },
  });
  // FEMA draws this layer only past 1:36,111 - zoom 14 and closer. Wider
  // tiles come back blank, so don't even ask for them.
  floodLayer = new Nfhl('', { opacity: .65, minZoom: 14, maxZoom: 18, pane: 'shadowPane',
    attribution: 'Flood zones: FEMA NFHL' });
  const floodHint = () => {
    const h = $('#floodHint');
    if (!h) return;
    const z = map.getZoom();
    h.textContent = z < 14
      ? `Zoom in to street level to see the zones — FEMA draws them only from zoom 14 (you are at ${z}). Blue-ringed pins are tracts already known to sit in a flood zone.`
      : 'Drawing from FEMA — a few seconds per tile.';
  };
  $('#floodToggle').addEventListener('click', e => {
    const on = !map.hasLayer(floodLayer);
    if (on) floodLayer.addTo(map); else map.removeLayer(floodLayer);
    e.currentTarget.classList.toggle('on', on);
    document.body.classList.toggle('flood-on', on);
    $('#floodKey').hidden = !on;
    floodHint();
  });
  map.on('zoomend', floodHint);

  $$('#basemaps button').forEach(b => b.addEventListener('click', () => {
    $$('#basemaps button').forEach(x => x.classList.toggle('on', x === b));
    map.removeLayer(currentBase);
    currentBase = baseLayers[b.dataset.base].addTo(map);
    currentBase.bringToBack();
    const imagery = b.dataset.base === 'imagery';
    if (imagery) map.__labels.addTo(map); else map.removeLayer(map.__labels);
    setOverlayTheme(imagery ? 'dark' : 'light');
  }));
}

/** White overlays read on satellite imagery; ink ones on the light basemaps. */
function setOverlayTheme(theme) {
  const ink = theme === 'light' ? '#2f3f3a' : '#fff';
  document.body.classList.toggle('light-map', theme === 'light');
  ringLayer.eachLayer(l => l.setStyle && l.setStyle({ color: ink, opacity: theme === 'light' ? .75 : .55 }));
  if (countyLayer) countyLayer.setStyle(f => f.properties.t === 1
    ? { color: ink, weight: 1.5, opacity: theme === 'light' ? .55 : .65, fill: false }
    : { color: ink, weight: .6, opacity: theme === 'light' ? .25 : .22, fill: false });
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
  else if (priceCut(t) && recentCut(t)) cls.push('iscut');
  if (t.flood === 'sfha') cls.push('insfha');
  const ink = t.bandIdx === 2 ? '#2A2208' : '#fff';
  return L.divIcon({
    className: '',
    html: `<div class="${cls.join(' ')}" data-id="${t.id}" style="width:${d}px;height:${d}px;
           background:${t.color};color:${ink};font-size:${d < 34 ? 11 : 13}px">${t.drive}</div>`,
    iconSize: [d, d], iconAnchor: [d / 2, d / 2], popupAnchor: [0, -d / 2],
  });
}

const isNew = t => !t.baseline && daysAgo(t.firstSeen) <= NEW_DAYS;

/** Every local picture we hold for a tract - the gallery, else the one photo. */
const pics = t => {
  if (t.imgs && t.imgs.length) return t.imgs;
  const g = (t.gallery || []).filter(Boolean);
  if (g.length) return t.img ? [t.img, ...g.slice(1)] : g;   // local primary first
  return t.img ? [t.img] : [];
};

/** Nearest real shopping: routed time to an anchor store, else the old estimate. */
const located = t => t.geo === 'parcel';
const shop = t => !located(t) ? null : t.shopMin != null
  ? { min: t.shopMin, mi: t.shopMi, to: t.shopName + (t.shopCity ? ', ' + t.shopCity : ''), real: true }
  : t.groceryMin != null
    ? { min: t.groceryMin, mi: t.groceryMi, to: t.groceryName, real: false }
    : null;

const stars = n => '★'.repeat(n) + '☆'.repeat(5 - n);
const fmtPop = n => n == null ? '' : n >= 1000 ? (n / 1000).toFixed(n >= 10000 ? 0 : 1) + 'k' : String(n);
const miTxt = (label, mi) => mi == null ? null : `${label} ${mi} mi`;

/** The convenience facts as one readable line. */
function convenienceLine(t) {
  const bits = [
    t.shopMin != null ? `shopping ${t.shopMin} min` : null,
    miTxt('pharmacy', t.pharmacyMi),
    t.hospitalMi != null ? `hospital ${t.hospitalMi} mi` : miTxt('clinic', t.clinicMi),
    miTxt('hardware', t.hardwareMi) || miTxt('farm supply', t.farmMi),
    miTxt('fuel', t.fuelMi),
    t.restaurants10 != null ? `${t.restaurants10} restaurants within 10 mi` : null,
  ].filter(Boolean);
  return bits.join(' · ');
}

/** Plain-language terrain from the averaged hillside slope. */
const terrain = s => s == null ? null
  : s < 3  ? 'flat'
  : s < 6  ? 'gentle'
  : s < 10 ? 'rolling'
  : s < 15 ? 'hilly'
  : 'steep';
const recentCut = t => {
  const h = t.priceHistory || [];
  return h.length > 1 && daysAgo(h[h.length - 1].date) <= NEW_DAYS;
};
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
        ${!located(t) ? `<div style="grid-column:1/-1"><b>Not located yet</b><span>pin is at the town centre — drive, shopping and terrain unknown until the parcel is found</span></div>` : `
        <div><b>${t.drive} min</b><span>to Knoxville${t.driveReal ? ' by road' : ', estimated'}</span></div>
        <div><b>${t.daysListed ?? '–'}</b><span>days listed</span></div>
        <div><b>${t.geo === 'parcel' ? 'Parcel' : 'Town'}</b><span>pin accuracy</span></div>
        ${t.townName ? `<div style="grid-column:1/-1"><b>Nearest town: ${esc(t.townName)}</b><span>${esc(t.townKind || 'place')}, pop. ${t.townPop != null ? t.townPop.toLocaleString('en-US') : 'n/a'} · ${t.townMi} mi</span></div>` : ''}
        ${t.convenience ? `<div style="grid-column:1/-1"><b>Convenience ${stars(t.convenience)} ${esc(t.convenienceLabel)}</b><span>${esc(convenienceLine(t))}</span></div>` : ''}
        ${t.flood ? `<div style="grid-column:1/-1"><b>${t.flood === 'sfha' ? 'In a FEMA flood zone' : t.flood === 'x500' ? 'FEMA 500-year zone' : 'Outside FEMA flood zones'}</b><span>${esc(t.floodZone || '')}${t.floodSub ? ' — ' + esc(t.floodSub.toLowerCase()) : ''} · at the pin, per NFHL</span></div>` : ''}
        ${t.slope != null ? `<div><b>${terrain(t.slope)}</b><span>${t.slope}° slope${
          t.elev != null ? `, ${Math.round(t.elev * 3.281)} ft` : ''}</span></div>` : ''}
        ${shop(t) ? `<div style="grid-column:1/-1"><b>${shop(t).min} min to ${esc(shop(t).to)}</b>
          <span>nearest real shopping, ${shop(t).real ? 'by road' : 'estimated'} (${shop(t).mi} mi)</span></div>` : ''}`}
      </div>
      <div class="pop-actions">
        <a class="primary" href="${esc(t.url)}" target="_blank" rel="noopener">Listing</a>
        <a href="${gmaps}" target="_blank" rel="noopener">Maps</a>
        <button data-hide="${t.id}">${isHidden(t.id) ? 'Unhide' : 'Hide'}</button>
      </div>
      <div class="pop-marks">
        <button class="${isFav(t.id) ? 'on' : ''}" data-fav="${t.id}">${isFav(t.id) ? '★ Favorited' : '☆ Favorite'}</button>
        ${allLists().map(n => `<button class="${listsOf(t.id).includes(n) ? 'on' : ''}" data-list="${esc(n)}" data-id="${t.id}">${esc(n)}</button>`).join('')}
        <button data-newlist="${t.id}">+ New list</button>
        <button class="warn" data-hoa="${t.id}" title="Rule this one out for good — the daily sweep will drop it">Has HOA</button>
      </div>
    </div>`;
}

/* ──────────────────────────────── cards ──────────────────────────────── */

function cardHtml(t) {
  const thumb = t.img
    ? `<span class="thumbwrap"><img class="thumb" src="${esc(t.img)}" alt="" loading="lazy" decoding="async">${
        pics(t).length > 1 ? `<span class="thumbn">${pics(t).length} photos</span>` : ''}</span>`
    : `<div class="thumb empty">no<br>photo</div>`;
  const tags = [
    isNew(t) ? '<span class="tag new">new</span>' : '',
    priceCut(t) ? `<span class="tag cut">price cut</span>` : '',
    t.status !== 'active' ? '<span class="tag unconf">unconfirmed</span>' : '',
    t.geo !== 'parcel' ? '<span class="tag unconf" title="Placed at the town centre — exact parcel not yet located">approx pin</span>' : '',
    located(t) ? `<span class="tag">${t.drive} min</span>` : '',
    t.convenience ? `<span class="tag conv" title="${esc(t.convenienceLabel)} — ${esc(convenienceLine(t))}">${stars(t.convenience)}</span>` : '',
    shop(t)
      ? `<span class="tag groc" title="to ${esc(shop(t).to)}">${shop(t).min} min shops</span>` : '',
    t.hoaKnown === false || (t.hoaKnown == null && t.source === 'Redfin' && t.hoa == null)
      ? '<span class="tag unconf" title="Neither Redfin\'s export nor Zillow stated whether there is an HOA">HOA ?</span>' : '',
    t.flood === 'sfha' ? '<span class="tag flood" title="FEMA: inside the 100-year floodplain (Special Flood Hazard Area)">flood zone</span>'
      : t.flood === 'x500' ? '<span class="tag flood2" title="FEMA: 500-year floodplain / moderate hazard">500-yr flood</span>' : '',
    located(t) && t.slope != null
      ? `<span class="tag terr" title="${t.slope}° average slope">${terrain(t.slope)}</span>` : '',
    `<span class="tag">${t.daysListed ?? '–'}d listed</span>`,
    ...listsOf(t.id).map(n => `<span class="tag list">${esc(n)}</span>`),
  ].join('');
  return `
    <article class="card${isNew(t) ? ' isnew' : ''}${isFav(t.id) ? ' isfav' : ''}${t.status !== 'active' ? ' unconfirmed' : ''}${isHidden(t.id) ? ' hidden-row' : ''}"
             data-id="${t.id}" role="listitem" tabindex="0">
      ${thumb}
      <div class="cbody">
        <div class="cline1">
          <span class="cacres">${t.acres} ac</span>
          <span class="cppa" style="background:${t.color};${t.bandIdx === 2 ? 'color:#2A2208' : ''}">${fmt$(t.ppa)}/ac</span>
          <span class="cprice">${fmt$(t.price)}</span>
        </div>
        <div class="cwhere">${esc(t.town)}, ${esc(t.county)} County${
        t.townName ? ` <span class="pop">· ${esc(t.townName)} ${fmtPop(t.townPop) ? `(pop. ${fmtPop(t.townPop)})` : ''} ${t.townMi} mi</span>` : ''}</div>
        <div class="caddr">${esc(t.address)}</div>
        <div class="cmeta">${tags}</div>
      </div>
      <button class="favbtn${isFav(t.id) ? ' on' : ''}" data-fav="${t.id}" title="Favorite">${isFav(t.id) ? '★' : '☆'}</button>
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
    noFlood: $('#fFlood').checked,
    hoaKnown: $('#fHoa').checked,
    onlyFav: $('#fFav').checked,
    list: $('#fList').value,
    q: $('#search').value.trim().toLowerCase(),
  };
}

function apply() {
  const f = currentFilters();
  let rows = state.tracts.filter(t => {
    if (!state.showHidden && isHidden(t.id) && !isFav(t.id)) return false;
    if (located(t) && t.drive > f.drive) return false;      // unknown is not "too far"
    { const sh = shop(t); if (sh && sh.min > f.groc) return false; }
    if (t.price > f.price) return false;
    if (t.acres < f.acres) return false;
    if (f.county && t.county !== f.county) return false;
    if (f.onlyNew && !isNew(t)) return false;
    if (f.onlyCut && !priceCut(t)) return false;
    if (f.onlyPhoto && !t.img) return false;
    if (f.onlyConfirmed && (t.status !== 'active' || t.geo !== 'parcel')) return false;
    if (f.noFlood && t.flood === 'sfha') return false;
    if (state.hidden[t.id]?.hoa || state.ownerEx.has(t.id)) return false;   // ruled out for good
    if (f.hoaKnown && !(t.hoaKnown === true)) return false;
    if (f.onlyFav && !isFav(t.id)) return false;
    if (f.list && !listsOf(t.id).includes(f.list)) return false;
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
    conv: (a, b) => (b.convenience || 0) - (a.convenience || 0) || (a.shopMin ?? 99) - (b.shopMin ?? 99),
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
  const all = state.view.filter(isNew);
  if (!all.length && $('#fNew').checked) {
    const any = state.tracts.filter(isNew).length;
    el.hidden = false; el.classList.add('on');
    el.innerHTML = any
      ? `No new listing passes your filters <span class="nb-act">back to everything</span>`
      : `Nothing new <span class="nb-act">back to everything</span>`;
    return;
  }
  if (!all.length) { el.hidden = true; return; }
  el.hidden = false;
  const on = $('#fNew').checked;
  el.classList.toggle('on', on);
  const when = all.map(t => t.firstSeen).sort().reverse()[0];
  const nice = new Date(when + 'T00:00:00').toLocaleDateString('en-US',
    { month: 'short', day: 'numeric' });
  el.innerHTML = on
    ? `Showing the <b>${all.length}</b> new since ${nice}
       <span class="nb-act">back to everything</span>`
    : `<b>${all.length}</b> new since ${nice}
       <span class="nb-act">show only these</span>`;
}

function renderCounts(rows) {
  const nNew = rows.filter(isNew).length;
  const nHid = Object.values(state.hidden).filter(v => v.h).length;
  const nFav = Object.values(state.hidden).filter(v => v.fav).length;
  $('#favCount').textContent = nFav ? `★ ${nFav}` : '';
  refreshListFilter();
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
  const list = [];
  state.view.forEach(t => pics(t).forEach((src, k) => list.push({ t, src, k, n: pics(t).length })));
  const i = list.findIndex(e => e.t.id === id);
  if (i < 0) return;
  state.lb = { list, i };
  paintLightbox();
  $('#lightbox').hidden = false;
}

function paintLightbox() {
  const e = state.lb.list[state.lb.i];
  if (!e) return;
  const t = e.t;
  $('#lbImg').src = e.src;
  $('#lbImg').alt = `${t.acres} acres in ${t.town}, ${t.county} County`;
  $('#lbCap').innerHTML =
    `<b>${t.acres} ac · ${fmt$(t.price)}</b> · ${fmt$(t.ppa)}/ac<br>
     ${esc(t.address)} — ${esc(t.town)}, ${esc(t.county)} County ·
     ${t.drive} min from Knoxville<br>
     <a href="${esc(t.url)}" target="_blank" rel="noopener">Open listing ↗</a>
     ${e.n > 1 ? `&nbsp;·&nbsp; photo ${e.k + 1} of ${e.n}` : ''}
     &nbsp;·&nbsp; ${state.lb.i + 1} of ${state.lb.list.length}`;
}

const stepLightbox = d => {
  const n = state.lb.list.length;
  if (!n) return;
  state.lb.i = (state.lb.i + d + n) % n;
  paintLightbox();
};
const closeLightbox = () => { $('#lightbox').hidden = true; };

/* ─────────────────────────────── changes ─────────────────────────────── */

function renderChanges(r) {
  const body = $('#chBody');
  if (!r) { body.innerHTML = '<p class="quiet">No run report yet.</p>'; return; }
  const when = new Date(r.generated);
  $('#chWhen').textContent = `Last run ${when.toLocaleString('en-US',
    { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' })} · next run every morning ~7:15 ET`;
  const row = (x, why, go) => `<li>${go ? `<span class="go" data-go="${x.id}">` : ''}${x.acres} ac · ${fmt$(x.price)} · ${esc(x.town)}${go ? '</span>' : ''}
      ${x.url ? `<a href="${esc(x.url)}" target="_blank" rel="noopener">↗</a>` : ''}
      <span class="why">${why || ''}</span></li>`;
  const sec = (title, items, f) => items && items.length
    ? `<h3>${title} (${items.length})</h3><ul>${items.map(f).join('')}</ul>` : '';
  let html = '';
  html += sec('New listings', r.new, x => row(x, fmt$(Math.round(x.price / x.acres)) + '/ac', true));
  html += sec('Price cuts', r.priceCuts, x => row(x, `${fmt$(x.from)} → ${fmt$(x.to)}`, true));
  html += sec('Price increases', r.priceIncreases, x => row(x, `${fmt$(x.from)} → ${fmt$(x.to)}`, true));
  html += sec('Gone — sold or withdrawn', r.gone, x => row(x, x.status));
  html += sec('Dropped — too far by road', r.tooFar, x => row(x, `Knoxville ${x.drive} min · shops ${x.shopMin} min`));
  html += sec('Dropped — too steep', r.tooSteep, x => row(x, `${x.slope}°`));
  html += sec('Dropped — no longer meets criteria', r.rejected, x => row(x, ''));
  html += sec('Not returned this run (kept, unconfirmed)', r.unconfirmed, x => row(x, `${x.missCount}/3 misses`, true));
  html += sec('Retired after 3 misses', r.retired, x => row(x, ''));
  body.innerHTML = html || `<p class="quiet">Nothing changed on ${r.date}. A quiet day.</p>`;
  const n = (r.new?.length || 0) + (r.priceCuts?.length || 0) + (r.gone?.length || 0);
  const b = $('#changesN'); b.hidden = !n; b.textContent = n;
}

async function loadChanges() {
  try {
    const r = await fetch(`data/report.json?t=${Date.now()}`, { cache: 'no-store' });
    renderChanges(r.ok ? await r.json() : null);
  } catch { renderChanges(null); }
}

function refreshPopup(id) {
  const m = state.markers.get(id);
  const t = state.tracts.find(x => x.id === id);
  if (m && t && m.isPopupOpen()) m.setPopupContent(popupHtml(t));
  refreshListFilter();
}

function refreshListFilter() {
  const sel = $('#fList'); const cur = sel.value;
  sel.innerHTML = '<option value="">Any list</option>' +
    allLists().map(n => `<option value="${esc(n)}">${esc(n)}</option>`).join('');
  sel.value = allLists().includes(cur) ? cur : '';
}

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
   '#fNew', '#fCut', '#fPhoto', '#fConfirmed', '#fGroc', '#fFlood', '#fFav', '#fList', '#fHoa'].forEach(sel =>
    $(sel).addEventListener('input', () => { syncOutputs(); apply(); }));

  let searchTimer;
  $('#search').addEventListener('input', () => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(apply, 140);
  });

  $('#newBanner').addEventListener('click', () => {
    const cb = $('#fNew');
    cb.checked = !cb.checked;
    if (cb.checked) $('#fSort').value = 'new';
    apply();
  });

  $('#resetFilters').addEventListener('click', () => {
    $('#fDrive').value = 60; $('#fPrice').value = 250000; $('#fAcres').value = 10;
    $('#fGroc').value = 15;
    $('#fCounty').value = ''; $('#fSort').value = 'ppa';
    $('#fNew').checked = false; $('#fCut').checked = false;
    $('#fPhoto').checked = false; $('#fConfirmed').checked = false; $('#fFlood').checked = false;
    $('#fFav').checked = false; $('#fList').value = ''; $('#fHoa').checked = false;
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
    const fb = e.target.closest('[data-fav]');
    if (fb) { e.stopPropagation(); setMark(fb.dataset.fav, { fav: !isFav(fb.dataset.fav) }); return; }
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
    root.querySelector('[data-fav]')?.addEventListener('click', e => {
      const id = e.target.dataset.fav; setMark(id, { fav: !isFav(id) }); refreshPopup(id);
    });
    root.querySelectorAll('[data-list]').forEach(b => b.addEventListener('click', e => {
      const { id, list } = e.target.dataset;
      const cur = listsOf(id);
      setMark(id, { lists: cur.includes(list) ? cur.filter(x => x !== list) : [...cur, list] });
      refreshPopup(id);
    }));
    root.querySelector('[data-hoa]')?.addEventListener('click', e => {
      const id = e.target.dataset.hoa;
      if (!confirm('Mark this listing as having an HOA? It will be removed from the map and the daily sweep will drop it.')) return;
      setMark(id, { hoa: true, h: true });
      map.closePopup();
    });
    root.querySelector('[data-newlist]')?.addEventListener('click', e => {
      const id = e.target.dataset.newlist;
      const name = (prompt('Name for the new list:') || '').trim().slice(0, 40);
      if (!name) return;
      setMark(id, { lists: [...new Set([...listsOf(id), name])] });
      refreshPopup(id);
    });
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

  // changes drawer
  $('#changesBtn').addEventListener('click', () => { $('#changes').hidden = !$('#changes').hidden; });
  $('#chClose').addEventListener('click', () => { $('#changes').hidden = true; });
  $('#chBody').addEventListener('click', e => {
    const g = e.target.closest('[data-go]');
    if (!g) return;
    if (!state.markers.get(g.dataset.go)) {           // it may be filtered out - widen
      $('#fNew').checked = false; $('#fConfirmed').checked = false; apply();
    }
    focusTract(g.dataset.go);
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
  $('#oAcres').textContent = (+$('#fAcres').value).toString();
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

  try {
    const r = await fetch(`data/owner-excluded.json?t=${Date.now()}`, { cache: 'no-store' });
    if (r.ok) state.ownerEx = new Set(Object.keys(await r.json()));
  } catch {}
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
  const unver = state.tracts.length - state.tracts.filter(t => t.status === 'active' && t.geo === 'parcel').length;
  const upd = data.generated ? new Date(data.generated) : null;
  const updTxt = upd && !Number.isNaN(upd)
    ? `Updated ${upd.toLocaleString('en-US', { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' })}`
    : `Updated ${data.date || '—'}`;
  $('#tagline').textContent = `${updTxt} · ` +
    `${state.tracts.length - unver} verified` + (unver ? ` · ${unver} approx pin` : '') +
    (nNew ? ` · ${nNew} new` : '');

  if (window.matchMedia('(max-width:860px)').matches) $('#legend').open = false;

  wire();
  loadChanges();
  syncOutputs();
  apply();

  if (state.view.length) {
    const b = L.latLngBounds(state.view.map(t => [t.lat, t.lon])).extend(KNOX);
    map.fitBounds(b, { padding: [40, 40], maxZoom: 10 });
  }
}

boot();
