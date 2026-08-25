let webData = {
  last_updated: "",
  records: [],
  all_listings_by_model: {}
};

let currentTab = 'records';
let selectedBrand = 'ALL';
let searchQuery = '';

document.addEventListener("DOMContentLoaded", () => {
  fetchData();
  setupEvents();
});

async function fetchData() {
  try {
    const res = await fetch('data.json?v=' + Date.now());
    if (!res.ok) throw new Error("Impossibile caricare data.json");
    webData = await res.json();
    initUI();
  } catch (err) {
    console.error("Errore nel caricamento dati:", err);
    document.getElementById("records-grid").innerHTML = `
      <div class="empty-state" style="grid-column: 1 / -1;">
        <h3>Nessun dato disponibile ancora</h3>
        <p>Esegui <code>python main.py</code> per generare il primo report.</p>
      </div>
    `;
  }
}

function initUI() {
  if (webData.last_updated) {
    document.getElementById("last-updated-text").textContent = `Aggiornato: ${webData.last_updated}`;
  }
  renderStats();
  renderCurrentTab();
}

function renderStats() {
  const records = webData.records || [];
  document.getElementById("stat-models").textContent = webData.total_models_tracked || records.length;
  document.getElementById("stat-total-cars").textContent = webData.total_active_listings || 0;

  if (records.length > 0) {
    const prices = records.map(r => r.price).filter(p => p > 0);
    if (prices.length > 0) {
      const min = Math.min(...prices);
      document.getElementById("stat-min-price").textContent = formatPrice(min);
    }
  }
}

function formatPrice(val) {
  if (!val || val <= 0) return "N/D";
  return "€ " + val.toLocaleString('it-IT', { minimumFractionDigits: 0, maximumFractionDigits: 0 });
}

function formatMileage(val) {
  if (!val || val === 'N/D') return 'N/D';
  const num = parseInt(String(val).replace(/\D/g, ''));
  if (isNaN(num)) return 'N/D';
  return num.toLocaleString('it-IT') + " km";
}

function escapeHTML(str) {
  if (!str) return '';
  return String(str)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#039;');
}

function setupEvents() {
  document.getElementById("btn-tab-records").addEventListener("click", () => switchTab('records'));
  document.getElementById("btn-tab-all").addEventListener("click", () => switchTab('all'));

  document.getElementById("search-input").addEventListener("input", (e) => {
    searchQuery = e.target.value.toLowerCase().trim();
    renderCurrentTab();
  });

  document.querySelectorAll("#brand-chips .chip").forEach(chip => {
    chip.addEventListener("click", () => {
      document.querySelectorAll("#brand-chips .chip").forEach(c => c.classList.remove("active"));
      chip.classList.add("active");
      selectedBrand = chip.dataset.brand;
      renderCurrentTab();
    });
  });
}

function switchTab(tab) {
  currentTab = tab;
  const btnRec = document.getElementById("btn-tab-records");
  const btnAll = document.getElementById("btn-tab-all");
  const secRec = document.getElementById("tab-content-records");
  const secAll = document.getElementById("tab-content-all");

  btnRec.classList.toggle("active", tab === 'records');
  btnAll.classList.toggle("active", tab === 'all');
  secRec.style.display = tab === 'records' ? 'block' : 'none';
  secAll.style.display = tab === 'all' ? 'block' : 'none';
  renderCurrentTab();
}

function renderCurrentTab() {
  if (currentTab === 'records') {
    renderTabRecords();
  } else {
    renderTabAll();
  }
}

/* ============================================
   TAB 1: RECORD DI SEMPRE
   ============================================ */
function renderTabRecords() {
  const container = document.getElementById("records-grid");
  const records = webData.records || [];

  const filtered = records.filter(item => {
    const matchBrand = selectedBrand === 'ALL' || item.brand === selectedBrand;
    const text = `${item.brand} ${item.model} ${item.title}`.toLowerCase();
    const matchQuery = !searchQuery || text.includes(searchQuery);
    return matchBrand && matchQuery;
  });

  if (filtered.length === 0) {
    container.innerHTML = `
      <div class="empty-state" style="grid-column: 1 / -1;">
        <h3>Nessun record trovato</h3>
        <p>Prova a cambiare i filtri attivi.</p>
      </div>`;
    return;
  }

  container.innerHTML = filtered.map(item => createCardHTML(item, true)).join('');
}

/* ============================================
   TAB 2: TUTTI GLI ANNUNCI PER MODELLO
   ============================================ */
function renderTabAll() {
  const container = document.getElementById("all-models-container");
  const grouped = webData.all_listings_by_model || {};

  let keys = Object.keys(grouped).filter(name => {
    const list = grouped[name] || [];
    if (list.length === 0) return false;
    const brand = list[0].brand;
    const matchBrand = selectedBrand === 'ALL' || brand === selectedBrand;
    const text = `${brand} ${name}`.toLowerCase();
    const matchQuery = !searchQuery || text.includes(searchQuery) ||
      list.some(i => i.title.toLowerCase().includes(searchQuery));
    return matchBrand && matchQuery;
  });

  if (keys.length === 0) {
    container.innerHTML = `
      <div class="empty-state">
        <h3>Nessun annuncio trovato</h3>
        <p>Prova a cambiare i filtri.</p>
      </div>`;
    return;
  }

  let html = '';
  keys.sort().forEach(name => {
    const listings = grouped[name] || [];
    const minPrice = listings[0] ? formatPrice(listings[0].price) : '';

    html += `
      <div class="model-section">
        <div class="model-header">
          <h2>🚗 ${name}</h2>
          <span class="model-count">${listings.length} annunci · Da ${minPrice}</span>
        </div>
        <div class="cards-grid">
          ${listings.map(item => createCardHTML(item, false)).join('')}
        </div>
      </div>`;
  });

  container.innerHTML = html;
}

/* ============================================
   CARD HTML
   ============================================ */
function createCardHTML(deal, isRecord) {
  const safeTitle = escapeHTML(deal.title);
  const safeBrand = escapeHTML(deal.brand);
  const safeUrl = deal.url ? escapeHTML(deal.url) : '#';
  const safeImageUrl = deal.image_url ? escapeHTML(deal.image_url) : null;

  const img = safeImageUrl
    ? `<img src="${safeImageUrl}" alt="${safeTitle}" loading="lazy" referrerpolicy="no-referrer" />`
    : `<div class="no-img">Nessuna Immagine</div>`;

  const superBadge = deal.is_super_price
    ? `<span class="badge badge-super">⚡ Super Prezzo</span>` : '';

  const recordBadge = isRecord
    ? `<span class="badge badge-record">🏆 Record</span>` : '';

  const recordDate = deal.record_date
    ? `<div class="record-date-info">📅 Record dal ${escapeHTML(deal.record_date)}</div>` : '';

  const price = deal.price > 0 ? formatPrice(deal.price) : (deal.price_formatted || 'N/D');
  const mileage = formatMileage(deal.mileage);
  const year = deal.year || 'N/D';

  return `
    <div class="car-card">
      <div class="card-img-wrapper">
        ${img}
        <span class="badge-brand">${safeBrand}</span>
        <div class="badge-overlay">
          ${recordBadge}
          ${superBadge}
        </div>
      </div>
      <div class="card-body">
        <h3 class="car-title">${safeTitle}</h3>
        <div class="price-tag">${price}</div>
        ${recordDate}
        <div class="specs-grid">
          <div class="spec-item">
            <div>
              <div class="spec-label">📅 Anno</div>
              <div class="spec-value">${escapeHTML(year)}</div>
            </div>
          </div>
          <div class="spec-item">
            <div>
              <div class="spec-label">🛣️ Km</div>
              <div class="spec-value">${escapeHTML(mileage)}</div>
            </div>
          </div>
          <div class="spec-item">
            <div>
              <div class="spec-label">⛽ Alim.</div>
              <div class="spec-value">${escapeHTML(deal.fuel || 'N/D')}</div>
            </div>
          </div>
          <div class="spec-item">
            <div>
              <div class="spec-label">⚙️ Cambio</div>
              <div class="spec-value">${escapeHTML(deal.transmission || 'Automatico')}</div>
            </div>
          </div>
        </div>
        <a href="${safeUrl}" target="_blank" rel="noopener noreferrer" class="btn-link">
          Vedi su AutoScout24 →
        </a>
      </div>
    </div>`;
}
