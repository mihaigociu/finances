'use strict';

const COLORS = {
  groceries:     '#22c55e',
  bakery:        '#f97316',
  restaurants:   '#ef4444',
  gas:           '#a855f7',
  health:        '#3b82f6',
  transport:     '#06b6d4',
  telecom:       '#6366f1',
  utilities:     '#78716c',
  subscriptions: '#f59e0b',
  shopping:      '#ec4899',
  education:     '#14b8a6',
  bookstore:     '#92400e',
  insurance:     '#64748b',
  government:    '#94a3b8',
  cash:          '#ca8a04',
  other:         '#9ca3af',
  income:        '#4ade80',
  transfer:      '#cbd5e1',
  loan:          '#fca5a5',
  bank_fees:     '#d8b4fe',
  credit_line:   '#7dd3fc',
  savings:       '#0ea5e9',
};

const SPENDING_CATS = [
  'groceries','bakery','restaurants','gas','health','transport',
  'telecom','utilities','subscriptions','shopping','education',
  'bookstore','insurance','government','cash','transfer','loan','bank_fees','other',
];

// Non-spending categories, appended so they remain filterable in the Transactions tab.
const ALL_CATS = [
  ...SPENDING_CATS, 'income','savings','credit_line',
];

// App state
const S = {
  card: '', year: '', tab: 'overview',
  search: '', txnCategory: '', txnMonth: '', txnView: '', txnPage: 1,
  summary: [], monthly: [], vendors: [],
  merchantSearch: '', merchantFilter: 'all', merchantsStale: false,
};

let donutChart = null, stackedChart = null, lineChart = null;
let lineActive = new Set(SPENDING_CATS);
let searchTimer = null;
let merchantTimer = null;
let merchantCats = [];

// ── Helpers ──────────────────────────────────────────────────────────────────

function fmt(n) {
  if (n == null) return '—';
  return Math.round(n).toLocaleString('ro-RO') + ' RON';
}

function fmtDate(d) {
  if (!d) return '—';
  const [y, m, day] = d.split('-');
  return `${day}.${m}.${y}`;
}

function fmtMonth(ym) {
  const [y, m] = ym.split('-');
  const names = ['Ian','Feb','Mar','Apr','Mai','Iun','Iul','Aug','Sep','Oct','Nov','Dec'];
  return `${names[+m - 1]} '${y.slice(2)}`;
}

function badge(cat) {
  const c = COLORS[cat] || '#9ca3af';
  return `<span class="badge" style="background:${c}">${cat}</span>`;
}

async function api(path) {
  const r = await fetch(path);
  if (!r.ok) throw new Error(`API ${r.status}: ${path}`);
  return r.json();
}

function qs() {
  const p = new URLSearchParams();
  if (S.card) p.set('card', S.card);
  if (S.year) p.set('year', S.year);
  return p;
}

// ── Init ─────────────────────────────────────────────────────────────────────

async function init() {
  const [years, cards, cats] = await Promise.all([
    api('/api/years'), api('/api/cards'), api('/api/categories'),
  ]);
  merchantCats = cats;

  const yearSel = document.getElementById('year-filter');
  years.forEach(y => {
    const o = document.createElement('option');
    o.value = o.textContent = y;
    yearSel.appendChild(o);
  });

  const cardSel = document.getElementById('card-filter');
  cards.forEach(c => {
    const o = document.createElement('option');
    o.value = c; o.textContent = `**** ${c}`;
    cardSel.appendChild(o);
  });

  yearSel.addEventListener('change', () => { S.year = yearSel.value; refresh(); });
  cardSel.addEventListener('change', () => { S.card = cardSel.value; refresh(); });

  document.querySelectorAll('.tab-btn').forEach(btn => {
    btn.addEventListener('click', () => switchTab(btn.dataset.tab));
  });

  const catFilter = document.getElementById('cat-filter');
  ALL_CATS.forEach(c => {
    const o = document.createElement('option');
    o.value = c; o.textContent = c;
    catFilter.appendChild(o);
  });

  catFilter.addEventListener('change', () => {
    S.txnCategory = catFilter.value;
    renderTransactions(1);
  });

  const monthFilter = document.getElementById('month-filter');
  const monthNames = ['January','February','March','April','May','June',
                      'July','August','September','October','November','December'];
  monthNames.forEach((name, i) => {
    const o = document.createElement('option');
    o.value = String(i + 1).padStart(2, '0');
    o.textContent = name;
    monthFilter.appendChild(o);
  });
  monthFilter.addEventListener('change', () => {
    S.txnMonth = monthFilter.value;
    renderTransactions(1);
  });

  const viewSel = document.getElementById('txn-view');
  viewSel.addEventListener('change', () => {
    S.txnView = viewSel.value;
    renderTransactions(1);
  });

  const searchInput = document.getElementById('txn-search');
  searchInput.addEventListener('input', () => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => { S.search = searchInput.value; renderTransactions(1); }, 280);
  });

  const merchantSearch = document.getElementById('merchant-search');
  merchantSearch.addEventListener('input', () => {
    clearTimeout(merchantTimer);
    merchantTimer = setTimeout(() => { S.merchantSearch = merchantSearch.value; loadMerchants(); }, 280);
  });

  const merchantFilter = document.getElementById('merchant-filter');
  merchantFilter.addEventListener('change', () => {
    S.merchantFilter = merchantFilter.value;
    loadMerchants();
  });

  const importBtn = document.getElementById('import-btn');
  const importFile = document.getElementById('import-file');
  importBtn.addEventListener('click', () => importFile.click());
  importFile.addEventListener('change', () => uploadCsv(importFile, importBtn));

  await refresh();
}

// ── CSV import ─────────────────────────────────────────────────────────────────

function showToast(msg, ok = true) {
  const t = document.getElementById('toast');
  t.textContent = msg;
  t.className = 'toast ' + (ok ? 'ok' : 'err');
  t.hidden = false;
}

async function uploadCsv(input, btn) {
  const file = input.files[0];
  if (!file) return;
  btn.disabled = true;
  btn.textContent = 'Importing…';
  showToast(`Importing ${file.name}…`);
  try {
    const fd = new FormData();
    fd.append('file', file);
    const r = await fetch('/api/import', { method: 'POST', body: fd });
    let data = {};
    try { data = await r.json(); } catch { /* non-JSON error (e.g. 413) */ }
    if (!r.ok) throw new Error(data.error || `import failed (${r.status})`);
    showToast(`Imported ${data.filename}: ${data.inserted} new, ${data.skipped} already present. Reloading…`);
    setTimeout(() => location.reload(), 1800);
  } catch (e) {
    showToast(e.message, false);
    btn.disabled = false;
    btn.textContent = 'Import CSV';
  } finally {
    input.value = '';  // allow re-selecting the same file
  }
}

async function refresh() {
  const p = qs();
  const base = p.toString() ? '?' + p : '';
  const vendorP = new URLSearchParams(p); vendorP.set('limit', '20');

  const [summary, monthly, vendors] = await Promise.all([
    api('/api/summary' + base),
    api('/api/monthly' + base),
    api('/api/vendors?' + vendorP),
  ]);

  S.summary = summary;
  S.monthly = monthly;
  S.vendors = vendors;

  renderOverview();
  renderTrends();
  if (S.tab === 'transactions') renderTransactions(S.txnPage);
}

function switchTab(tab) {
  S.tab = tab;
  document.querySelectorAll('.tab-btn').forEach(b => b.classList.toggle('active', b.dataset.tab === tab));
  document.querySelectorAll('.tab-content').forEach(c => c.classList.toggle('active', c.id === `tab-${tab}`));
  if (tab === 'transactions') renderTransactions(1);
  if (tab === 'merchants') loadMerchants();
  // Category edits change the underlying data — refresh the analytics tabs when we leave Merchants.
  if (tab !== 'merchants' && S.merchantsStale) { S.merchantsStale = false; refresh(); }
}

// ── Overview ──────────────────────────────────────────────────────────────────

function renderOverview() {
  const total = S.summary.reduce((s, r) => s + r.total, 0);
  const count = S.summary.reduce((s, r) => s + r.count, 0);
  const top = S.summary[0];
  const months = [...new Set(S.monthly.map(r => r.month))];
  const avg = months.length ? total / months.length : 0;

  document.getElementById('kpi-total').textContent = fmt(total);
  document.getElementById('kpi-top-cat').textContent = top ? top.category : '—';
  document.getElementById('kpi-top-amt').textContent = top ? fmt(top.total) : '';
  document.getElementById('kpi-count').textContent = count.toLocaleString('ro-RO');
  document.getElementById('kpi-avg').textContent = fmt(avg);
  document.getElementById('kpi-months').textContent = `over ${months.length} month${months.length !== 1 ? 's' : ''}`;

  renderDonut();
  renderVendors();
}

function renderDonut() {
  const canvas = document.getElementById('donut-chart');
  if (donutChart) { donutChart.destroy(); donutChart = null; }
  if (!S.summary.length) return;

  donutChart = new Chart(canvas, {
    type: 'doughnut',
    data: {
      labels: S.summary.map(r => r.category),
      datasets: [{
        data: S.summary.map(r => r.total),
        backgroundColor: S.summary.map(r => COLORS[r.category] || '#ccc'),
        borderWidth: 2,
        borderColor: '#fff',
      }],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      cutout: '58%',
      plugins: {
        legend: {
          position: 'right',
          labels: {
            font: { size: 12 },
            padding: 10,
            generateLabels(chart) {
              const total = chart.data.datasets[0].data.reduce((a, b) => a + b, 0);
              return chart.data.labels.map((label, i) => {
                const val = chart.data.datasets[0].data[i];
                const pct = (val / total * 100).toFixed(1);
                return {
                  text: `${label}  ${pct}%`,
                  fillStyle: chart.data.datasets[0].backgroundColor[i],
                  strokeStyle: '#fff',
                  lineWidth: 2,
                  index: i,
                };
              });
            },
          },
        },
        tooltip: {
          callbacks: {
            label: ctx => {
              const total = ctx.dataset.data.reduce((a, b) => a + b, 0);
              const pct = (ctx.parsed / total * 100).toFixed(1);
              return ` ${fmt(ctx.parsed)}  (${pct}%)`;
            },
          },
        },
      },
      onClick(evt, els) {
        if (!els.length) return;
        const cat = S.summary[els[0].index].category;
        S.txnCategory = cat;
        document.getElementById('cat-filter').value = cat;
        switchTab('transactions');
      },
    },
  });
}

function renderVendors() {
  const tbody = document.getElementById('vendors-body');
  tbody.innerHTML = '';
  if (!S.vendors.length) return;

  const grandTotal = S.summary.reduce((s, r) => s + r.total, 0);
  const maxTotal = S.vendors[0].total;

  S.vendors.forEach(v => {
    const pct = grandTotal ? (v.total / grandTotal * 100).toFixed(1) : '0.0';
    const barW = (v.total / maxTotal * 100).toFixed(0);
    const tr = document.createElement('tr');
    tr.innerHTML = `
      <td>
        <div style="font-weight:500;font-size:13px">${v.vendor_norm}</div>
        <div style="margin-top:3px;height:3px;border-radius:2px;background:${COLORS[v.category]||'#ccc'};width:${barW}%;opacity:.65"></div>
      </td>
      <td>${badge(v.category)}</td>
      <td style="color:var(--muted);font-size:12px;text-align:right">${v.count}×</td>
      <td style="text-align:right;font-variant-numeric:tabular-nums;font-weight:500;white-space:nowrap">${fmt(v.total)}</td>
      <td style="text-align:right;color:var(--muted);font-size:12px">${pct}%</td>
    `;
    tr.addEventListener('click', () => {
      S.txnCategory = v.category;
      document.getElementById('cat-filter').value = v.category;
      switchTab('transactions');
    });
    tbody.appendChild(tr);
  });
}

// ── Trends ───────────────────────────────────────────────────────────────────

function pivotMonthly() {
  const months = [...new Set(S.monthly.map(r => r.month))].sort();
  const catData = {};
  SPENDING_CATS.forEach(c => { catData[c] = new Array(months.length).fill(0); });
  S.monthly.forEach(r => {
    const i = months.indexOf(r.month);
    if (i >= 0 && catData[r.category] !== undefined) catData[r.category][i] = r.total;
  });
  return { months, catData };
}

function renderTrends() {
  renderStackedBar();
  renderLineToggles();
  renderLineChart();
}

function renderStackedBar() {
  const canvas = document.getElementById('stacked-chart');
  if (stackedChart) { stackedChart.destroy(); stackedChart = null; }
  if (!S.monthly.length) return;

  const { months, catData } = pivotMonthly();
  const catTotal = c => catData[c].reduce((a, b) => a + b, 0);
  const datasets = SPENDING_CATS
    .filter(c => catData[c].some(v => v > 0))
    .sort((a, b) => catTotal(b) - catTotal(a))  // largest spend first → bottom of each stack
    .map(c => ({
      label: c,
      data: catData[c],
      backgroundColor: COLORS[c],
      stack: 'stack',
      borderWidth: 0,
    }));

  stackedChart = new Chart(canvas, {
    type: 'bar',
    data: { labels: months.map(fmtMonth), datasets },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      interaction: { mode: 'index' },
      plugins: {
        legend: { display: false },
        tooltip: {
          itemSort: (a, b) => b.parsed.y - a.parsed.y,  // biggest category for that month first
          // Compact rows: a month with ~20 categories otherwise outgrows the chart
          // and the bottom of the tooltip gets clipped.
          padding: 6,
          bodySpacing: 0,
          bodyFont: { size: 11 },
          callbacks: {
            label: ctx => ctx.parsed.y > 0 ? ` ${ctx.dataset.label}: ${fmt(ctx.parsed.y)}` : null,
            // Total in the title (top), so it stays visible even if the tooltip is clipped.
            title: items => {
              const total = items.reduce((s, i) => s + i.parsed.y, 0);
              return [items[0].label, `Total: ${fmt(total)}`];
            },
          },
        },
      },
      scales: {
        x: { stacked: true, grid: { display: false } },
        y: {
          stacked: true,
          ticks: { callback: v => Math.round(v).toLocaleString('ro-RO') },
        },
      },
    },
  });
}

function renderLineToggles() {
  const container = document.getElementById('line-toggles');
  container.innerHTML = '';
  const catsWithData = new Set(S.monthly.map(r => r.category));

  SPENDING_CATS.filter(c => catsWithData.has(c)).forEach(c => {
    const on = lineActive.has(c);
    const color = COLORS[c] || '#ccc';
    const btn = document.createElement('button');
    btn.className = 'cat-toggle';
    btn.textContent = c;
    btn.style.borderColor = color;
    btn.style.color = on ? '#fff' : color;
    btn.style.backgroundColor = on ? color : 'transparent';
    btn.addEventListener('click', () => {
      if (lineActive.has(c)) lineActive.delete(c); else lineActive.add(c);
      renderLineToggles();
      renderLineChart();
    });
    container.appendChild(btn);
  });
}

function renderLineChart() {
  const canvas = document.getElementById('line-chart');
  if (lineChart) { lineChart.destroy(); lineChart = null; }
  if (!S.monthly.length) return;

  const { months, catData } = pivotMonthly();
  const datasets = SPENDING_CATS
    .filter(c => lineActive.has(c) && catData[c].some(v => v > 0))
    .map(c => ({
      label: c,
      data: catData[c],
      borderColor: COLORS[c],
      backgroundColor: COLORS[c] + '18',
      tension: 0.35,
      fill: false,
      pointRadius: 3,
      borderWidth: 2,
      pointHoverRadius: 5,
    }));

  lineChart = new Chart(canvas, {
    type: 'line',
    data: { labels: months.map(fmtMonth), datasets },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      interaction: { mode: 'index' },
      plugins: {
        legend: { display: false },
        tooltip: {
          callbacks: {
            label: ctx => ctx.parsed.y > 0 ? ` ${ctx.dataset.label}: ${fmt(ctx.parsed.y)}` : null,
          },
        },
      },
      scales: {
        x: { grid: { display: false } },
        y: { ticks: { callback: v => Math.round(v).toLocaleString('ro-RO') } },
      },
    },
  });
}

// ── Transactions ──────────────────────────────────────────────────────────────

const TXN_HEAD_LIST = `
  <tr>
    <th>Date</th><th>Vendor / Party</th><th>Category</th><th>Card</th>
    <th style="text-align:right">Amount</th>
  </tr>`;
const TXN_HEAD_VENDOR = `
  <tr>
    <th>Vendor / Party</th><th>Category</th>
    <th style="text-align:right">Txns</th><th style="text-align:right">Total</th>
  </tr>`;

async function renderTransactions(page) {
  S.txnPage = page;
  const p = qs();
  if (S.txnCategory) p.set('category', S.txnCategory);
  if (S.txnMonth) p.set('month', S.txnMonth);
  if (S.search) p.set('search', S.search);

  if (S.txnView === 'vendor') {
    p.set('group', 'vendor');
    return renderTransactionsByVendor(p);
  }

  p.set('page', page);
  p.set('per_page', 50);

  document.getElementById('txn-head').innerHTML = TXN_HEAD_LIST;
  const data = await api('/api/transactions?' + p);

  const tbody = document.getElementById('txn-body');
  tbody.innerHTML = '';

  data.transactions.forEach(t => {
    const date = t.auth_date || t.settlement_date;
    const name = t.vendor_norm || t.beneficiary || t.type;
    const isDebit = t.direction === 'debit';
    const amtCls = isDebit ? 'amount-debit' : 'amount-credit';
    const sign = isDebit ? '−' : '+';
    const cat = t.category || '—';
    const isPOS = t.type === 'Cumparare POS' || t.type === 'Plata POS';

    const tr = document.createElement('tr');
    tr.innerHTML = `
      <td style="color:var(--muted);font-size:13px;white-space:nowrap">${fmtDate(date)}</td>
      <td style="max-width:220px">
        <div style="font-weight:500;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${escHtml(name || '—')}</div>
        ${!isPOS ? `<div style="font-size:11px;color:#94a3b8">${escHtml(t.type)}</div>` : ''}
      </td>
      <td>${badge(cat)}</td>
      <td style="color:var(--muted);font-size:12px">****&nbsp;${t.card || '—'}</td>
      <td class="${amtCls}" style="text-align:right;white-space:nowrap">${sign}${fmt(t.amount)}</td>
    `;
    tbody.appendChild(tr);
  });

  renderPagination(data.total, page, data.per_page);
}

async function renderTransactionsByVendor(p) {
  document.getElementById('txn-head').innerHTML = TXN_HEAD_VENDOR;
  const data = await api('/api/transactions?' + p);

  const tbody = document.getElementById('txn-body');
  tbody.innerHTML = '';

  data.vendors.forEach(v => {
    const name = v.vendor || '—';
    const tr = document.createElement('tr');
    tr.style.cursor = 'pointer';
    tr.title = 'Show transactions for this vendor';
    tr.innerHTML = `
      <td style="max-width:280px">
        <div style="font-weight:500;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${escHtml(name)}</div>
      </td>
      <td>${badge(v.category || '—')}</td>
      <td style="text-align:right;color:var(--muted);font-size:12px">${v.count}×</td>
      <td class="amount-debit" style="text-align:right;white-space:nowrap">−${fmt(v.total)}</td>
    `;
    // Drill in: click a vendor to see its individual transactions under the same filters.
    tr.addEventListener('click', () => {
      const searchInput = document.getElementById('txn-search');
      searchInput.value = name;
      S.search = name;
      const viewSel = document.getElementById('txn-view');
      viewSel.value = '';
      S.txnView = '';
      renderTransactions(1);
    });
    tbody.appendChild(tr);
  });

  // No server-side paging for the grouped view — clear the pager.
  document.getElementById('pagination').innerHTML = '';
}

function renderPagination(total, page, perPage) {
  const totalPages = Math.ceil(total / perPage);
  const el = document.getElementById('pagination');
  el.innerHTML = '';

  const prev = document.createElement('button');
  prev.className = 'page-btn';
  prev.textContent = '← Prev';
  prev.disabled = page <= 1;
  prev.addEventListener('click', () => renderTransactions(page - 1));

  const info = document.createElement('span');
  info.className = 'page-info';
  const from = (page - 1) * perPage + 1;
  const to = Math.min(page * perPage, total);
  info.textContent = `${from}–${to} of ${total.toLocaleString('ro-RO')}`;

  const next = document.createElement('button');
  next.className = 'page-btn';
  next.textContent = 'Next →';
  next.disabled = page >= totalPages;
  next.addEventListener('click', () => renderTransactions(page + 1));

  el.append(prev, info, next);
}

function escHtml(s) {
  return String(s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

// ── Merchants (manual categorization) ──────────────────────────────────────────

async function loadMerchants() {
  const p = new URLSearchParams();
  if (S.merchantSearch) p.set('search', S.merchantSearch);
  if (S.merchantFilter) p.set('filter', S.merchantFilter);
  const merchants = await api('/api/merchants?' + p);
  renderMerchants(merchants);
}

function renderMerchants(merchants) {
  const tbody = document.getElementById('merchant-body');
  tbody.innerHTML = '';

  document.getElementById('merchant-count').textContent =
    `${merchants.length.toLocaleString('ro-RO')} merchant${merchants.length !== 1 ? 's' : ''}`;

  if (!merchants.length) {
    tbody.innerHTML = '<tr><td colspan="5" style="text-align:center;color:var(--muted);padding:24px">No merchants match.</td></tr>';
    return;
  }

  merchants.forEach(m => {
    const tr = document.createElement('tr');

    const nameTd = document.createElement('td');
    nameTd.style.maxWidth = '260px';
    nameTd.innerHTML = `<div style="font-weight:500;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${escHtml(m.vendor_norm)}</div>`;

    // Effective category = an explicit merchant tag if present, else the category
    // the transactions already carry (assigned by transaction type at import).
    const effective = m.category || m.txn_category;

    const catTd = document.createElement('td');
    const sel = document.createElement('select');
    sel.className = 'merchant-cat';
    sel.add(new Option(effective ? '— untag —' : 'Choose category…', ''));
    merchantCats.forEach(c => sel.add(new Option(c, c, false, c === effective)));
    if (effective) sel.value = effective;
    sel.style.borderColor = effective ? (COLORS[effective] || 'var(--border)') : 'var(--border)';
    sel.addEventListener('change', () => saveMerchant(m, sel.value, tr, sel, statusTd));
    catTd.appendChild(sel);

    const statusTd = document.createElement('td');
    statusTd.style.textAlign = 'center';
    statusTd.innerHTML = statusMarkup(m);

    const txnTd = document.createElement('td');
    txnTd.style.cssText = 'text-align:right;color:var(--muted);font-size:12px';
    txnTd.textContent = `${m.count}×`;

    const totalTd = document.createElement('td');
    totalTd.style.cssText = 'text-align:right;font-variant-numeric:tabular-nums;font-weight:500;white-space:nowrap';
    totalTd.textContent = fmt(m.total);

    tr.append(nameTd, catTd, statusTd, txnTd, totalTd);
    tbody.appendChild(tr);
  });
}

function statusMarkup(m) {
  if (m.category) return '<span class="status-pill tagged">✓ tagged</span>';
  if (m.txn_category) return '<span class="status-pill auto">auto · by type</span>';
  return '<span style="color:var(--muted);font-size:12px">untagged</span>';
}

async function saveMerchant(m, category, tr, sel, statusTd) {
  if (!category) return;  // blank option = no-op (untagging not supported server-side)
  sel.disabled = true;
  try {
    const r = await fetch('/api/merchants/tag', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ vendor_norm: m.vendor_norm, category }),
    });
    if (!r.ok) throw new Error(`save failed (${r.status})`);
    m.category = category;
    m.txn_category = category;
    sel.style.borderColor = COLORS[category] || 'var(--border)';
    statusTd.innerHTML = statusMarkup(m);
    S.merchantsStale = true;  // overview/trends need re-fetch when we leave this tab
    tr.style.transition = 'background .15s';
    tr.style.background = '#ecfdf5';
    setTimeout(() => { tr.style.background = ''; }, 600);
  } catch (e) {
    alert(e.message);
    sel.value = m.category || '';
  } finally {
    sel.disabled = false;
  }
}

// ── Boot ─────────────────────────────────────────────────────────────────────

window.addEventListener('DOMContentLoaded', init);
