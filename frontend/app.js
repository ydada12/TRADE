/* 本地只读面板前端。
 *
 * 三件事：画 K 线、列出历史消息、把历史消息钉回 K 线上。
 * 所有时间由后端以北京时间字符串给出（本机时区不是北京时间，前端绝不自己 toLocaleString）。
 * 缺失值一律显示「数据源未提供」，前端不补任何数字。
 */

const state = {
  symbols: [],
  instId: null,
  bar: '5m',
  refreshSeconds: 60,
  autoRefresh: true,
  timer: null,
  kline: null,
  alerts: [],
  snapshotLevels: null,
  typeFilter: '',
  selectedIndex: null,
  chart: null,
  view: 'market',
  control: null,
};

const TYPE_CLASS = {
  '趋势': 'trend',
  '事件预警': 'event',
  '事件结果': 'event',
  '国际大事': 'world',
  '做空观察位': 'short',
  '盘中波动': 'session',
};
const TYPE_COLOR = {
  '趋势': '#3b82f6',
  '事件预警': '#f59e0b',
  '事件结果': '#f59e0b',
  '国际大事': '#22d3ee',
  '做空观察位': '#f6465d',
  '盘中波动': '#a855f7',
};
// 涨跌色对齐 OKX 暗色行情页
const UP = '#0ecb81';
const DOWN = '#f6465d';
// 图表里的中性色，与 style.css 的 CSS 变量保持同一套
const NEUTRAL = {
  border: '#2b2f36',
  split: '#262a30',
  muted: '#848e9c',
  text: '#eaecef',
  tooltipBg: 'rgba(31,35,41,.96)',
};
const MISSING = '数据源未提供';

// ---------------------------------------------------------------- 工具
function fmtNum(value) {
  if (value === null || value === undefined) return MISSING;
  const n = Number(value);
  if (!isFinite(n)) return MISSING;
  const abs = Math.abs(n);
  let digits = 8;
  if (abs >= 100) digits = 2;
  else if (abs >= 10) digits = 3;
  else if (abs >= 1) digits = 5;
  else if (abs >= 0.01) digits = 6;
  return String(parseFloat(n.toFixed(digits)));
}

function nowBeijing() {
  // 显式指定时区，避免本机时区不是北京时间时显示成别的时区
  const parts = new Intl.DateTimeFormat('zh-CN', {
    timeZone: 'Asia/Shanghai', hour12: false,
    hour: '2-digit', minute: '2-digit', second: '2-digit',
  }).format(new Date());
  return parts;
}

function el(id) { return document.getElementById(id); }

function showError(message) {
  const banner = el('error-banner');
  if (!message) { banner.classList.add('hidden'); banner.textContent = ''; return; }
  banner.textContent = message;
  banner.classList.remove('hidden');
}

async function fetchJson(url) {
  const res = await fetch(url, { cache: 'no-store' });
  let payload = null;
  try { payload = await res.json(); } catch (err) { payload = null; }
  if (!res.ok) {
    const detail = payload && payload.error ? payload.error : `HTTP ${res.status}`;
    throw new Error(detail);
  }
  return payload;
}

// 控制页的写接口：唯一允许写的地方，只改运行期偏好（不是交易接口）
async function postJson(url, payload) {
  const res = await fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
  let data = null;
  try { data = await res.json(); } catch (err) { data = null; }
  if (!res.ok) {
    const detail = data && data.error ? data.error : `HTTP ${res.status}`;
    throw new Error(detail);
  }
  return data;
}

// 把后端来的值安全地放进 HTML（备注里可能有引号之类）
function esc(value) {
  return String(value === null || value === undefined ? '' : value)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

// 告警时间落在哪一根 K 线上：最后一个「起始时间 <= 告警时间」的下标
function barIndexFor(times, ts) {
  if (!Array.isArray(times) || times.length === 0 || ts === null || ts === undefined) return -1;
  let lo = 0, hi = times.length - 1, found = -1;
  while (lo <= hi) {
    const mid = (lo + hi) >> 1;
    if (times[mid] <= ts) { found = mid; lo = mid + 1; } else { hi = mid - 1; }
  }
  return found;
}

// ---------------------------------------------------------------- 初始化
async function init() {
  state.chart = echarts.init(el('chart'), null, { renderer: 'canvas' });

  let health = { bars: ['5m', '15m', '1H', '4H', '1D', '1W'], default_bar: '5m', refresh_seconds: 60 };
  try { health = await fetchJson('/api/health'); } catch (err) { /* 用默认值也能跑 */ }
  state.refreshSeconds = Math.max(10, Number(health.refresh_seconds) || 60);
  state.bar = health.default_bar || '5m';

  el('bar-group').innerHTML = (health.bars || []).map(
    (bar) => `<button data-bar="${bar}" class="${bar === state.bar ? 'active' : ''}">${bar}</button>`
  ).join('');
  el('bar-group').addEventListener('click', (event) => {
    const button = event.target.closest('button[data-bar]');
    if (!button) return;
    state.bar = button.dataset.bar;
    [...el('bar-group').children].forEach((b) => b.classList.toggle('active', b === button));
    loadKline();
  });

  await loadSymbols();

  el('symbol-select').addEventListener('change', (event) => {
    state.instId = event.target.value;
    loadAll();
  });
  el('type-filter').addEventListener('change', (event) => {
    state.typeFilter = event.target.value;
    loadAlerts();
  });
  el('refresh-btn').addEventListener('click', () => loadAll());
  el('auto-refresh').addEventListener('change', (event) => {
    state.autoRefresh = event.target.checked;
    scheduleRefresh();
  });

  // 顶部标签：行情 / 控制
  el('tabs').addEventListener('click', (event) => {
    const tab = event.target.closest('button[data-view]');
    if (tab) switchView(tab.dataset.view);
  });

  window.addEventListener('resize', () => state.chart && state.chart.resize());

  // 首次进入页面就取一次数（否则要等下一次自动刷新或手动点「立即刷新」才有内容）
  await loadAll();
  state.chart.resize();
  scheduleRefresh();
}

function scheduleRefresh() {
  if (state.timer) { clearInterval(state.timer); state.timer = null; }
  if (!state.autoRefresh) return;
  // 只在行情页轮询；控制页不需要反复打行情接口
  state.timer = setInterval(() => {
    if (state.instId && state.view === 'market') loadAll(true);
  }, state.refreshSeconds * 1000);
}

function switchView(view) {
  state.view = view === 'control' ? 'control' : 'market';
  [...el('tabs').children].forEach((b) => b.classList.toggle('active', b.dataset.view === state.view));
  el('view-market').classList.toggle('hidden', state.view !== 'market');
  el('view-control').classList.toggle('hidden', state.view !== 'control');
  el('market-controls').classList.toggle('hidden', state.view !== 'market');
  if (state.view === 'control') {
    loadControl().catch((err) => showError(`加载控制项失败：${err.message}`));
  } else if (state.chart) {
    state.chart.resize();
  }
}

function renderSymbolOptions() {
  el('symbol-select').innerHTML = state.symbols.map((s) =>
    `<option value="${s.inst_id}"${s.enabled ? '' : ' data-off="1"'}>` +
    `${s.enabled ? '●' : '○'} ${s.short_name}　${s.name}${s.enabled ? '' : '（已停用）'}</option>`
  ).join('');
}

async function loadSymbols() {
  const payload = await fetchJson('/api/symbols');
  state.symbols = payload.symbols || [];
  renderSymbolOptions();
  const first = state.symbols.find((s) => s.enabled) || state.symbols[0];
  state.instId = first ? first.inst_id : null;
  if (state.instId) el('symbol-select').value = state.instId;
}

async function syncSymbolOptions() {
  // 控制页改过开关后同步行情页下拉，尽量保留当前选中的品种
  const payload = await fetchJson('/api/symbols');
  state.symbols = payload.symbols || [];
  renderSymbolOptions();
  if (!state.instId || !state.symbols.some((s) => s.inst_id === state.instId)) {
    const first = state.symbols.find((s) => s.enabled) || state.symbols[0];
    state.instId = first ? first.inst_id : null;
  }
  if (state.instId) el('symbol-select').value = state.instId;
}

// ---------------------------------------------------------------- 取数
async function loadAll(silent) {
  if (!state.instId) return;
  el('refresh-time').textContent = `刷新于 ${nowBeijing()}`;
  try {
    // 先拿快照：图上的支撑/压力横线依赖它，顺序反了会缺线
    await loadSnapshot();
    await Promise.all([loadKline(), loadAlerts()]);
    if (!silent) showError('');
  } catch (err) {
    showError(`取数失败：${err.message}`);
  }
}

async function loadSnapshot() {
  const snap = await fetchJson(`/api/snapshot?inst_id=${encodeURIComponent(state.instId)}`);
  renderSnapshot(snap);
}

async function loadKline() {
  const data = await fetchJson(
    `/api/kline?inst_id=${encodeURIComponent(state.instId)}&bar=${encodeURIComponent(state.bar)}`
  );
  state.kline = data;
  const symbol = state.symbols.find((s) => s.inst_id === state.instId);
  const name = symbol ? symbol.short_name : state.instId;
  el('chart-title').textContent = `${name} · ${data.bar} · ${data.count} 根`;
  const unconfirmed = data.confirmed && data.confirmed.length && !data.confirmed[data.confirmed.length - 1];
  el('chart-note').textContent =
    `${data.source_desc || ''}${unconfirmed ? '　最后一根为未收盘 K 线' : ''}`;
  renderChart();
  renderAlerts();
}

async function loadAlerts() {
  const params = [`inst_id=${encodeURIComponent(state.instId)}`, 'limit=500'];
  if (state.typeFilter) params.push(`signal_type=${encodeURIComponent(state.typeFilter)}`);
  const payload = await fetchJson(`/api/alerts?${params.join('&')}`);
  state.alerts = payload.alerts || [];
  renderAlerts();
}

// ---------------------------------------------------------------- 渲染：行情条
function renderQuote(snap) {
  const pct = snap.change_24h_pct;
  const hasPct = pct !== null && pct !== undefined;
  // 0 也算真实值（只是平），此时不染色；缺字段才显示「数据源未提供」
  const dir = !hasPct ? '' : (pct > 0 ? 'up' : (pct < 0 ? 'down' : ''));

  el('quote-symbol').textContent = snap.short_name || snap.inst_id || '—';
  const badge = el('quote-badge');
  badge.textContent = snap.enabled ? '' : '已停用';
  badge.classList.toggle('hidden', !!snap.enabled);

  const priceEl = el('quote-price');
  priceEl.textContent = fmtNum(snap.price);
  priceEl.classList.toggle('up', dir === 'up');
  priceEl.classList.toggle('down', dir === 'down');

  const chgEl = el('quote-change');
  chgEl.textContent = hasPct ? `${pct > 0 ? '+' : ''}${Number(pct).toFixed(2)}%` : MISSING;
  chgEl.classList.toggle('up', dir === 'up');
  chgEl.classList.toggle('down', dir === 'down');

  el('quote-high').textContent = fmtNum(snap.high_24h);
  el('quote-low').textContent = fmtNum(snap.low_24h);
  el('quote-vol').textContent = fmtNum(snap.vol_24h);
}

// ---------------------------------------------------------------- 渲染：快照
function renderSnapshot(snap) {
  state.snapshotLevels = snap;   // 图上的支撑/压力横线要用
  renderQuote(snap);
  const trend = snap.trend;
  const maText = trend && trend.ma_values
    ? Object.keys(trend.ma_values).map((k) => `MA${k} ${fmtNum(trend.ma_values[k])}`).join('　')
    : '数据源未提供';

  const rows = [
    ['24h 最高', fmtNum(snap.high_24h)],
    ['24h 最低', fmtNum(snap.low_24h)],
    ['最近支撑', fmtNum(snap.support)],
    ['最近压力', fmtNum(snap.resistance)],
    ['周线均线', maText],
    ['趋势状态', trend ? `${trend.label}` : '未记录'],
    ['状态更新', trend && trend.updated_at ? trend.updated_at : '未记录'],
  ];

  el('snapshot-body').innerHTML = `
    <div>
      <div class="snap-price">${fmtNum(snap.price)}</div>
      <div class="muted">${snap.name || snap.inst_id}${snap.enabled ? '' : '（已停用，仅查看）'}</div>
      <div class="muted">数据时间：${snap.ts_text || '数据源未提供'}</div>
    </div>
    <div class="snap-row">
      <span class="k">关键位出处</span>
      <span class="v">最近 ${snap.lookback_weeks ?? '—'} 周周K推导</span>
    </div>
    ${rows.map(([k, v]) => `<div class="snap-row"><span class="k">${k}</span><span class="v">${v}</span></div>`).join('')}
    <div class="snap-action">${snap.action_text || '建议人工查看/等待确认'}</div>
    <div class="snap-notes">${(snap.notes || []).join('<br>')}</div>
  `;
}

// ---------------------------------------------------------------- 渲染：图表
function renderChart() {
  const data = state.kline;
  if (!data) {
    state.chart.clear();
    state.chart.setOption({
      title: { text: MISSING, left: 'center', top: '45%', textStyle: { color: NEUTRAL.muted, fontSize: 13 } },
    });
    return;
  }

  const volumes = data.volumes.map((v, i) => ({
    value: v,
    itemStyle: { color: data.ohlc[i][1] >= data.ohlc[i][0] ? UP : DOWN, opacity: 0.55 },
  }));

  const marks = buildAlertMarks(data);

  state.chart.setOption({
    animation: false,
    backgroundColor: 'transparent',
    tooltip: {
      trigger: 'axis',
      axisPointer: { type: 'cross' },
      backgroundColor: NEUTRAL.tooltipBg,
      borderColor: NEUTRAL.border,
      textStyle: { color: NEUTRAL.text, fontSize: 12 },
      formatter: (items) => {
        if (!items || !items.length) return '';
        const idx = items[0].dataIndex;
        const o = data.ohlc[idx];
        const head = `<b>${data.labels[idx]}</b>（北京时间）`;
        const body = `开 ${fmtNum(o[0])}　高 ${fmtNum(o[3])}　低 ${fmtNum(o[2])}　收 ${fmtNum(o[1])}`;
        const hit = marks.byIndex[idx];
        const extra = hit ? `<br><span style="color:${TYPE_COLOR[hit.signal_type] || NEUTRAL.muted}">● ${hit.signal_type}</span> ${hit.title}` : '';
        return `${head}<br>${body}<br>量 ${fmtNum(data.volumes[idx])}${extra}`;
      },
    },
    grid: [
      // 右侧留出空间放现价价签
      { left: 58, right: 58, top: 16, height: '62%' },
      { left: 58, right: 58, top: '74%', height: '16%' },
    ],
    xAxis: [
      {
        type: 'category', data: data.labels, boundaryGap: true,
        axisLine: { lineStyle: { color: NEUTRAL.border } },
        axisLabel: { color: NEUTRAL.muted, fontSize: 10, hideOverlap: true },
      },
      {
        type: 'category', data: data.labels, gridIndex: 1, boundaryGap: true,
        axisLine: { lineStyle: { color: NEUTRAL.border } },
        axisLabel: { show: false }, axisTick: { show: false },
      },
    ],
    yAxis: [
      {
        scale: true, gridIndex: 0, position: 'left',
        splitLine: { lineStyle: { color: NEUTRAL.split } },
        axisLabel: { color: NEUTRAL.muted, fontSize: 10 },
      },
      {
        scale: true, gridIndex: 1, splitNumber: 2,
        splitLine: { show: false },
        axisLabel: { color: NEUTRAL.muted, fontSize: 10 },
      },
    ],
    dataZoom: [
      { type: 'inside', xAxisIndex: [0, 1], start: 60, end: 100 },
      { type: 'slider', xAxisIndex: [0, 1], bottom: 4, height: 16, start: 60, end: 100,
        borderColor: NEUTRAL.border, fillerColor: 'rgba(255,255,255,.10)',
        textStyle: { color: NEUTRAL.muted, fontSize: 10 } },
    ],
    series: [
      {
        name: 'K线', type: 'candlestick', data: data.ohlc,
        itemStyle: { color: UP, color0: DOWN, borderColor: UP, borderColor0: DOWN },
        markPoint: marks.points,
        markArea: marks.areas,
        markLine: marks.lines,
      },
      {
        name: '成交量', type: 'bar', xAxisIndex: 1, yAxisIndex: 1, data: volumes,
      },
    ],
  }, { notMerge: true });

  if (state.selectedIndex !== null) highlight(state.selectedIndex);
}

function buildAlertMarks(data) {
  const times = data.times;
  const byIndex = {};
  const points = [];

  state.alerts.forEach((alert) => {
    const idx = barIndexFor(times, alert.ts_ms);
    if (idx < 0 || idx >= times.length) return;
    if (alert.price === null || alert.price === undefined) return;
    byIndex[idx] = byIndex[idx] || alert;
    const color = TYPE_COLOR[alert.signal_type] || NEUTRAL.muted;
    points.push({
      name: alert.short_name || alert.signal_type,
      coord: [idx, alert.price],
      value: alert.price,
      symbol: 'pin',
      symbolSize: 32,
      symbolOffset: [0, -6],
      itemStyle: { color: color, opacity: alert.source === 'log_backfill' ? 0.55 : 0.95 },
      label: {
        show: true, color: '#16181c', fontSize: 10, fontWeight: 'bold',
        formatter: (alert.signal_type || '').slice(0, 2),
      },
    });
  });

  // 事件类额外标出「时间段」，一眼看出当时在等什么数据
  const areas = [];
  state.alerts.forEach((alert) => {
    if (!(alert.signal_type || '').startsWith('事件')) return;
    const idx = barIndexFor(times, alert.ts_ms);
    if (idx < 0 || idx >= times.length) return;
    const next = Math.min(idx + 1, times.length - 1);
    areas.push([
      { xAxis: data.labels[idx], itemStyle: { color: 'rgba(245,158,11,.10)' } },
      { xAxis: data.labels[next] },
    ]);
  });

  // 关键位横线（来自真实周K推导，缺失就不画）
  const lines = [];
  const snapLevels = state.snapshotLevels || null;
  if (snapLevels) {
    if (snapLevels.support !== null && snapLevels.support !== undefined) {
      lines.push({ yAxis: snapLevels.support, lineStyle: { color: UP, type: 'dashed', width: 1 },
        label: { formatter: `支撑 ${fmtNum(snapLevels.support)}`, color: UP, fontSize: 10, position: 'insideEndTop' } });
    }
    if (snapLevels.resistance !== null && snapLevels.resistance !== undefined) {
      lines.push({ yAxis: snapLevels.resistance, lineStyle: { color: DOWN, type: 'dashed', width: 1 },
        label: { formatter: `压力 ${fmtNum(snapLevels.resistance)}`, color: DOWN, fontSize: 10, position: 'insideEndBottom' } });
    }
    // 现价虚线 + 右轴价签：现价取不到就不画这条线，绝不拿旧值画一条假的
    if (snapLevels.price !== null && snapLevels.price !== undefined) {
      const chg = snapLevels.change_24h_pct;
      const tone = chg === null || chg === undefined ? NEUTRAL.muted : (chg >= 0 ? UP : DOWN);
      lines.push({
        yAxis: snapLevels.price,
        lineStyle: { color: tone, type: 'dashed', width: 1, opacity: .85 },
        label: {
          formatter: fmtNum(snapLevels.price),
          color: '#16181c', backgroundColor: tone,
          padding: [2, 5], borderRadius: 3, fontSize: 10, fontWeight: 'bold',
          position: 'end',
        },
      });
    }
  }

  return { points: { data: points, symbolSize: 32 }, areas: { data: areas, silent: true }, lines: { data: lines, silent: true, symbol: 'none' }, byIndex };
}

function highlight(idx) {
  if (!state.chart || idx === null) return;
  const total = state.kline ? state.kline.times.length : 0;
  const startValue = Math.max(0, idx - Math.round(total * 0.35));
  const endValue = Math.min(total - 1, idx + Math.round(total * 0.1));
  state.chart.dispatchAction({ type: 'dataZoom', startValue: startValue, endValue: endValue });
  state.chart.dispatchAction({ type: 'showTip', seriesIndex: 0, dataIndex: idx });
}

// ---------------------------------------------------------------- 渲染：历史消息
function renderAlerts() {
  const list = el('alerts-list');
  const data = state.kline;
  const times = data ? data.times : [];

  const backfill = state.alerts.filter((a) => a.source === 'log_backfill').length;
  const offChart = state.alerts.filter((a) => {
    const idx = barIndexFor(times, a.ts_ms);
    return idx < 0 || idx >= times.length;
  }).length;
  el('alerts-summary').textContent =
    `共 ${state.alerts.length} 条（日志回填 ${backfill} 条）` +
    (offChart ? `　其中 ${offChart} 条不在当前 K 线范围内` : '');

  if (!state.alerts.length) {
    list.innerHTML = '<li class="muted">这个品种还没有历史推送记录。</li>';
    return;
  }

  list.innerHTML = [...state.alerts].reverse().map((alert) => {
    const idx = barIndexFor(times, alert.ts_ms);
    const onChart = idx >= 0 && idx < times.length && alert.price !== null && alert.price !== undefined;
    const cls = TYPE_CLASS[alert.signal_type] || '';
    const back = alert.source === 'log_backfill' ? ' backfill' : '';
    const levels = [
      alert.support !== null && alert.support !== undefined ? `支撑 ${fmtNum(alert.support)}` : '',
      alert.resistance !== null && alert.resistance !== undefined ? `压力 ${fmtNum(alert.resistance)}` : '',
    ].filter(Boolean).join('　');
    return `<li class="alert-item ${cls}${back}" data-ts="${alert.ts_ms ?? ''}">
      <div class="alert-head">
        <span>${alert.ts_text || '时间缺失'}</span>
        <span>${alert.source_label}${alert.signal_type ? ' · ' + alert.signal_type : ''}</span>
      </div>
      <div class="alert-title">${alert.title || alert.what_happened || '(无标题)'}</div>
      <div class="alert-meta">
        <span>现价 ${fmtNum(alert.price)}</span>
        ${levels ? `<span>${levels}</span>` : ''}
        ${onChart ? '' : '<span class="alert-off">不在当前K线范围内</span>'}
        <span class="muted">点开看详情</span>
      </div>
      ${alertDetailHtml(alert)}
    </li>`;
  }).join('');

  list.querySelectorAll('.alert-item').forEach((item) => {
    item.addEventListener('click', () => {
      const ts = item.dataset.ts ? Number(item.dataset.ts) : null;
      const idx = barIndexFor(times, ts);
      const wasSelected = item.classList.contains('selected');
      // 先全部收起，再决定要不要展开当前这条（点第二次 = 收起）
      list.querySelectorAll('.alert-item').forEach((other) => {
        other.classList.remove('selected');
        const detail = other.querySelector('.alert-detail');
        if (detail) detail.classList.add('hidden');
      });
      if (wasSelected) { state.selectedIndex = null; showError(''); return; }

      item.classList.add('selected');
      const detail = item.querySelector('.alert-detail');
      if (detail) detail.classList.remove('hidden');

      if (idx < 0 || idx >= times.length) {
        state.selectedIndex = null;
        showError('详情已展开。这条消息不在当前 K 线范围内，换个更长的周期或往前拖动即可对齐到图上。');
        return;
      }
      showError('');
      state.selectedIndex = idx;
      highlight(idx);
    });
  });
}

// 单条消息的详情块（默认收起，点开才显示）。
// 全部字段都来自后端落盘的真实记录；没保存到的一律留空，不编造。
function alertDetailHtml(alert) {
  const rows = [
    ['类型', alert.signal_type || '未记录'],
    ['品种', alert.symbol || alert.inst_id || '未记录'],
    ['推送时间', alert.pushed_at || '未记录'],
    ['数据时间', alert.ts_text || '未记录'],
    ['参考价位', [
      `现价 ${fmtNum(alert.price)}`,
      alert.support !== null && alert.support !== undefined ? `支撑 ${fmtNum(alert.support)}` : '',
      alert.resistance !== null && alert.resistance !== undefined ? `压力 ${fmtNum(alert.resistance)}` : '',
    ].filter(Boolean).join('　')],
    ['数据来源', `${alert.source_label || '实时'}${alert.source === 'log_backfill' ? '（旧消息，从日志回填）' : ''}`],
  ];
  return `<div class="alert-detail hidden">
    <div class="alert-detail-title">${esc(alert.title || '(无标题)')}</div>
    ${rows.map(([k, v]) => `<div class="ad-row"><span class="k">${k}</span><span class="v">${esc(v)}</span></div>`).join('')}
    ${alert.what_happened ? `<div class="ad-happened">${esc(alert.what_happened)}</div>` : ''}
    ${alert.body
      ? `<div class="ad-label muted">推送原文</div><div class="ad-body">${esc(alert.body)}</div>`
      : '<p class="ad-note muted">这条是更早的记录，落盘时还没保存推送全文，只留了上面的结构化摘要。</p>'}
  </div>`;
}

// ---------------------------------------------------------------- 控制页
const TRACK_SHORT = {
  trend: '趋势', event: '事件', world: '国际大事',
  short: '做空观察', session: '盘中波动',
};
const WEEKDAY_NAMES = ['一', '二', '三', '四', '五', '六', '日'];

function weekdaysText(days) {
  if (!Array.isArray(days) || !days.length) return '每天';
  return '周' + days.map((d) => WEEKDAY_NAMES[d] || '').join('');
}

async function loadControl() {
  const data = await fetchJson('/api/control');
  state.control = data;
  renderControl(data);
}

function applyControl(control) {
  // 写接口都会回传最新快照，直接用返回值刷新，免得再打一次接口
  state.control = control;
  renderControl(control);
  syncSymbolOptions().then(() => {
    if (state.view === 'market' && state.instId) loadAll(true);
  }).catch(() => {});
}

function renderControl(data) {
  el('control-updated').textContent = data.updated_at ? `配置更新于 ${data.updated_at}` : '';

  // ---- 品种开关 ----
  const symbols = data.symbols || [];
  el('control-symbols').innerHTML = symbols.map((s) => `
    <li class="control-symbol">
      <label class="switch">
        <input type="checkbox" data-inst="${esc(s.inst_id)}" ${s.enabled ? 'checked' : ''}>
        <span class="slider"></span>
      </label>
      <div class="cs-main">
        <div class="cs-name">${esc(s.short_name)}　<span class="muted">${esc(s.name)}</span>
          ${s.priority ? '<span class="pill-priority">重点</span>' : ''}</div>
        <div class="muted small">${
          s.override === null
            ? `跟随配置（原为${s.config_enabled ? '启用' : '停用'}）`
            : (s.enabled ? '已手动开启' : '已手动关闭')
        }</div>
      </div>
    </li>`).join('');

  el('control-symbols').querySelectorAll('input[data-inst]').forEach((input) => {
    input.addEventListener('change', () => {
      saveSymbol(input.dataset.inst, input.checked)
        .catch((err) => { showError(`保存品种开关失败：${err.message}`); input.checked = !input.checked; });
    });
  });

  // ---- 免打扰 ----
  const dnd = data.dnd || { enabled: false, windows: [] };
  el('dnd-toggle').checked = !!dnd.enabled;
  el('dnd-windows').innerHTML = (dnd.windows || []).length
    ? dnd.windows.map((w, i) => `
      <div class="dnd-window">
        <span class="dw-name">${esc(w.name || '免打扰')}</span>
        <span class="muted">${esc(w.start)} – ${esc(w.end)}</span>
        <span class="muted small">${weekdaysText(w.weekdays)}</span>
        <button class="btn-mini" data-rm="${i}">删除</button>
      </div>`).join('')
    : '<p class="muted small">还没有时段，开启免打扰前请先加一条。</p>';
  el('dnd-windows').querySelectorAll('button[data-rm]').forEach((btn) => {
    btn.addEventListener('click', () => {
      const windows = (dnd.windows || []).filter((_, i) => i !== Number(btn.dataset.rm));
      saveDnd(!!dnd.enabled, windows).catch((err) => showError(`删除时段失败：${err.message}`));
    });
  });
  el('dnd-status').textContent = dnd.enabled
    ? (dnd.active_now ? '当前正处于免打扰时段：非重点消息会延迟补发。' : '免打扰已开启，当前不在任何时段内。')
    : '免打扰已关闭。';

  // ---- 重点提醒 ----
  const pri = data.priority || { symbols: [], tracks: [] };
  el('priority-symbols').innerHTML = symbols.map((s) => `
    <button class="chip ${pri.symbols.includes(s.inst_id) ? 'on' : ''}"
      data-pinst="${esc(s.inst_id)}">${esc(s.short_name)}</button>`).join('');
  el('priority-symbols').querySelectorAll('button[data-pinst]').forEach((btn) => {
    btn.addEventListener('click', () => {
      const inst = btn.dataset.pinst;
      const next = pri.symbols.includes(inst)
        ? pri.symbols.filter((x) => x !== inst)
        : pri.symbols.concat([inst]);
      savePriority(next, pri.tracks).catch((err) => showError(`保存重点品种失败：${err.message}`));
    });
  });
  el('priority-tracks').innerHTML = (data.tracks || []).map((t) => `
    <button class="chip ${pri.tracks.includes(t.key) ? 'on' : ''}"
      data-ptrack="${esc(t.key)}" title="${esc(t.label)}">${esc(TRACK_SHORT[t.key] || t.key)}</button>`).join('');
  el('priority-tracks').querySelectorAll('button[data-ptrack]').forEach((btn) => {
    btn.addEventListener('click', () => {
      const key = btn.dataset.ptrack;
      const next = pri.tracks.includes(key)
        ? pri.tracks.filter((x) => x !== key)
        : pri.tracks.concat([key]);
      savePriority(pri.symbols, next).catch((err) => showError(`保存重点轨道失败：${err.message}`));
    });
  });

  // ---- 持仓 ----
  el('position-forms').innerHTML = symbols.map((s) => {
    const p = s.position || {};
    const source = s.position_source === 'override' ? '控制台填写'
      : (s.position_source === 'config' ? '来自 config' : '未设置');
    return `<div class="pos-form" data-inst="${esc(s.inst_id)}">
      <div class="pos-head">
        <span class="cs-name">${esc(s.short_name)}</span>
        <span class="muted small">${source}</span>
      </div>
      <div class="pos-grid">
        <label>方向
          <select data-f="side">
            <option value=""${!p.side ? ' selected' : ''}>—</option>
            <option value="long"${p.side === 'long' ? ' selected' : ''}>多</option>
            <option value="short"${p.side === 'short' ? ' selected' : ''}>空</option>
          </select>
        </label>
        <label>开仓价<input type="number" step="any" data-f="entry_price" value="${esc(p.entry_price)}"></label>
        <label>保证金<input type="number" step="any" data-f="margin_usdt" value="${esc(p.margin_usdt)}"></label>
        <label>杠杆<input type="number" step="any" data-f="leverage" value="${esc(p.leverage)}"></label>
        <label class="pos-note">备注<input type="text" data-f="note" value="${esc(p.note)}"></label>
      </div>
      <div class="pos-actions">
        <button class="btn-mini primary" data-save="${esc(s.inst_id)}">保存</button>
        <button class="btn-mini" data-clear="${esc(s.inst_id)}">清除覆盖</button>
      </div>
    </div>`;
  }).join('');

  el('position-forms').querySelectorAll('.pos-form').forEach((form) => {
    const inst = form.dataset.inst;
    form.querySelector(`button[data-save="${inst}"]`).addEventListener('click', () => {
      savePosition(inst, readPositionForm(form))
        .catch((err) => showError(`保存持仓失败：${err.message}`));
    });
    form.querySelector(`button[data-clear="${inst}"]`).addEventListener('click', () => {
      savePosition(inst, { side: '', entry_price: '', margin_usdt: '', leverage: '', note: '' })
        .catch((err) => showError(`清除持仓失败：${err.message}`));
    });
  });
}

function readPositionForm(form) {
  const payload = {};
  form.querySelectorAll('[data-f]').forEach((input) => { payload[input.dataset.f] = input.value; });
  return payload;
}

async function saveSymbol(instId, enabled) {
  const out = await postJson('/api/control/symbol', { inst_id: instId, enabled: enabled });
  applyControl(out.control);
  showError('');
}

async function saveDnd(enabled, windows) {
  const out = await postJson('/api/control/dnd', { enabled: enabled, windows: windows });
  applyControl(out.control);
  showError('');
}

async function savePriority(symbols, tracks) {
  const out = await postJson('/api/control/priority', { symbols: symbols, tracks: tracks });
  applyControl(out.control);
  showError('');
}

async function savePosition(instId, payload) {
  const out = await postJson('/api/control/position',
    Object.assign({ inst_id: instId }, payload));
  applyControl(out.control);
  showError('');
}

function wireDndAdd() {
  el('dnd-add-btn').addEventListener('click', () => {
    const dnd = (state.control && state.control.dnd) || { enabled: false, windows: [] };
    const name = el('dnd-name').value.trim() || '免打扰';
    const weekdays = [...el('dnd-weekdays').querySelectorAll('input:checked')]
      .map((box) => Number(box.value));
    const window_ = {
      name: name,
      start: el('dnd-start').value || '23:00',
      end: el('dnd-end').value || '07:00',
      weekdays: weekdays,
    };
    saveDnd(!!dnd.enabled, (dnd.windows || []).concat([window_]))
      .then(() => { el('dnd-name').value = ''; })
      .catch((err) => showError(`添加免打扰时段失败：${err.message}`));
  });
  el('dnd-toggle').addEventListener('change', () => {
    const dnd = (state.control && state.control.dnd) || { enabled: false, windows: [] };
    saveDnd(el('dnd-toggle').checked, dnd.windows || [])
      .catch((err) => {
        showError(`保存免打扰开关失败：${err.message}`);
        el('dnd-toggle').checked = !el('dnd-toggle').checked;
      });
  });
}

document.addEventListener('DOMContentLoaded', () => {
  wireDndAdd();
  init().catch((err) => showError(`初始化失败：${err.message}`));
});
