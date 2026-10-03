const $ = s => document.querySelector(s);
function fmt(n, d=2){ if(n==null||isNaN(n)) return '—'; return Number(n).toLocaleString('zh-CN',{minimumFractionDigits:d,maximumFractionDigits:d}); }
function pnlCls(v){ return v>0?'up':(v<0?'down':''); }
function esc(s){ return (s==null?'':String(s)); }

function renderCards(s){
  const C = s.capital||{};
  const cap = [
    {label:'总权益', v:fmt(C.equity), h:'初始 '+fmt(C.init_capital)+' 元', cls:(C.total_pnl<0?'down':'up')},
    {label:'累计盈亏', v:fmt(C.total_pnl), h:fmt(C.total_pnl_pct)+'%', cls:pnlCls(C.total_pnl)},
    {label:'持仓市值', v:fmt(C.market_value), h:'仓位 '+fmt((C.market_value??0)/(C.equity||1)*100,1)+'%'},
    {label:'可用现金', v:fmt(C.cash), h:'现金占比 '+fmt((C.cash??0)/(C.equity||1)*100,1)+'%'},
    {label:'已实现盈亏', v:fmt(C.realized), h:'—'},
    {label:'持仓数量', v:(C.open_positions??0)+' 只', h:'目标 '+fmt(s.targets?.length||0)+' 只'},
  ];
  $('#cards').innerHTML = cap.map(c=>`
    <div class="card">
      <div class="lbl">${c.label}</div>
      <div class="val ${c.cls||''}">${c.v}</div>
      <div class="hint">${c.h}</div>
    </div>`).join('');
}

function renderPos(positions){
  if(!positions||!positions.length){ $('#posBody').innerHTML='<tr><td colspan="10" class="empty">暂无持仓</td></tr>'; return; }
  const rows = positions.map(p=>{
    const locked = p.locked_qty||0;
    const sellable = p.sellable_qty!=null?p.sellable_qty:(p.qty-locked);
    const luTxt = p.to_limit_up_pct!=null?fmt(p.to_limit_up_pct,1)+'%':'—';
    const ldTxt = p.to_limit_down_pct!=null?fmt(p.to_limit_down_pct,1)+'%':'—';
    let sb='<span class="sBadge ok">可交易</span>';
    const ts=(p.trade_state||'').toLowerCase();
    if(ts.indexOf('涨停')>=0) sb='<span class="sBadge bad">涨停·不可买</span>';
    else if(ts.indexOf('跌停')>=0) sb='<span class="sBadge bad">跌停·不可卖</span>';
    else if(ts.indexOf('停牌')>=0) sb='<span class="sBadge bad">停牌</span>';
    else if(ts.indexOf('t+1')>=0) sb='<span class="sBadge warn">T+1锁定</span>';
    return `<tr>
    <td><div class="name"><span>${p.name||p.canon}</span><span class="code">${p.canon}</span></div></td>
    <td>${fmt(p.qty,0)}</td>
    <td>${fmt(p.avg_cost)}</td>
    <td>${fmt(p.last_price,3)}</td>
    <td>${fmt(p.mv)}</td>
    <td class="${pnlCls(p.pnl_pct)}">${fmt(p.pnl_pct,1)}%</td>
    <td><div style="white-space:nowrap">${fmt((p.weight??0)*100,1)}%<div class="prog" style="width:70px;margin-top:4px"><i style="width:${Math.min((p.weight??0)*100/15*100,100)}%"></i></div></div></td>
    <td style="white-space:nowrap">${fmt(sellable,0)} <span class="muted">/</span> ${locked>0?'<span class="lock">'+fmt(locked,0)+'锁</span>':'<span class="muted">0</span>'}</td>
    <td style="white-space:nowrap"><span class="up">${luTxt}</span> <span class="muted">/</span> <span class="down">${ldTxt}</span></td>
    <td>${sb}</td>
  </tr>`;
  }).join('');
  $('#posBody').innerHTML = rows;
}

function renderRules(s){
  const R = s.rules||{};
  if(!Object.keys(R).length){ $('#rulesBody').innerHTML='<span class="muted">无规则配置</span>'; return; }
  const items = [
    {k:'起始资金', v:fmt(R.init_capital,0)+' 元'},
    {k:'目标持仓数', v:R.max_stocks+' 只'},
    {k:'最大仓位 / 现金底线', v:fmt((R.max_pos_ratio||0)*100,0)+'% / '+fmt((R.cash_cushion||0)*100,1)+'%'},
    {k:'成交单位(整手)', v:R.lot_size+' 股'},
    {k:'T+1 当日锁仓', v:R.tplus1?'生效':'关闭', cls:(R.tplus1?'ok':'bad')},
    {k:'滑点', v:fmt((R.slippage||0)*100,2)+'%(买/卖方向)'},
    {k:'佣金 / 印花税 / 过户', v:fmt((R.commission||0)*10000,1)+'‱ / '+fmt((R.stamp_tax||0)*10000,1)+'‱ / '+fmt((R.transfer_fee||0)*10000,1)+'‱'},
    {k:'止损线', v:'-'+fmt((R.stop_loss_pct||0)*100,0)+'%'},
    {k:'涨跌幅限制', v:Object.values(R.board_limits||{}).join(' / '), note:'主板/创业板/北交所'},
  ];
  $('#rulesBody').innerHTML = items.map(it=>`
    <div class="ruleItem"><div class="k">${it.k}${it.note?'<span class="muted" style="font-weight:400">('+it.note+')</span>':''}</div>
      <div class="v ${it.cls?'down':''}">${it.v}</div></div>`).join('');
}

function renderBacktest(bt){
  if(!bt||!bt.curve||!bt.curve.length){
    $('#btRange').textContent=''; $('#btBody').innerHTML='<span class="empty">尚未运行连续交易日回放(可在终端执行 python backtest_engine.py --days 10)</span>'; return;
  }
  $('#btRange').textContent = `区间 ${bt.start||''} ~ ${bt.end||''} · ${bt.trade_days||bt.curve.length} 个交易日`;
  const maxE = Math.max(...bt.curve.map(c=>c.equity));
  const minE = Math.min(...bt.curve.map(c=>c.equity));
  const span = (maxE-minE)||1;
  const bars = bt.curve.map(c=>{
    const h = 8 + ((c.equity-minE)/span)*82;
    const d = String(c.day||'').slice(5);
    return `<div style="height:${h}%" title="${c.day} ${fmt(c.equity,2)}"><i>${d}</i></div>`;
  }).join('');
  const last = bt.curve[bt.curve.length-1];
  const cls = last.pnl_pct>=0?'up':'down';
  $('#btBody').innerHTML = `
    <div style="display:flex;gap:24px;flex-wrap:wrap;margin-bottom:6px">
      <span>初始资金 <b>${fmt(bt.init_capital,0)}</b> 元</span>
      <span>期末权益 <b>${fmt(bt.final_equity,2)}</b></span>
      <span>总收益 <b class="${cls}">${fmt(bt.total_return,2)}%</b></span>
      <span>最大回撤 <b>${fmt(bt.max_drawdown_pct,2)}%</b></span>
      <span>总成交 <b>${fmt(bt.total_trades,0)}</b> 笔</span>
    </div>
    <div class="btBars">${bars}</div>
    <div class="muted" style="margin-top:22px;font-size:12px">${bt.note||''}</div>`;
}

// 成交历史翻页状态 (模块级)
var _tState = { all: [], filtered: [], page: 1, perPage: 10 };

function _renderTradePage(){
  const s = _tState;
  const body = $('#tradeBody'); if (!body) return;
  const total = s.filtered.length;
  const totalPages = Math.max(1, Math.ceil(total / s.perPage));
  if (s.page > totalPages) s.page = totalPages;
  if (s.page < 1) s.page = 1;
  const start = (s.page - 1) * s.perPage;
  const pageRows = s.filtered.slice(start, start + s.perPage);
  if (!total) {
    body.innerHTML = '<tr><td colspan="8" class="empty">无成交</td></tr>';
  } else {
    body.innerHTML = pageRows.map(t=>`<tr>
      <td>${t.date||''}</td>
      <td>${t.time||''}</td>
      <td><span class="tag ${t.type}">${t.type==='buy'?'买入':'卖出'}</span></td>
      <td>${t.canon}</td><td>${fmt(t.qty,0)}</td><td>${fmt(t.price)}</td><td>${fmt(t.fee,2)}</td>
      <td class="${t.pnl!=null?(t.pnl>0?'up':(t.pnl<0?'down':'')):''}">${t.pnl!=null?fmt(t.pnl,2):'—'}</td>
    </tr>`).join('');
  }
  // 翻页控件
  const pager = $('#tradePager'); if (!pager) return;
  pager.innerHTML =
    `<span class="muted" style="font-size:12px">${total}笔·第${s.page}/${totalPages}页</span>` +
    `<button class="pgbtn" data-act="first" ${s.page<=1?'disabled':''} title="首页">⟪</button>` +
    `<button class="pgbtn" data-act="prev" ${s.page<=1?'disabled':''} title="上一页">‹</button>` +
    `<input type="number" id="tradePgInput" min="1" max="${totalPages}" value="${s.page}" ` +
    `style="width:48px;text-align:center;font-size:12px;padding:1px 2px" title="跳转页码">` +
    `<button class="pgbtn" data-act="next" ${s.page>=totalPages?'disabled':''} title="下一页">›</button>` +
    `<button class="pgbtn" data-act="last" ${s.page>=totalPages?'disabled':''} title="末页">⟫</button>`;
}

function renderTrades(tradesHistory, tradesToday){
  // tradesHistory: {date: [trade, ...]}  跨日累积; tradesToday: 当日 list (备援)
  // 1) 拍平为数组 + 按 date 倒序
  let all = [];
  if (tradesHistory && typeof tradesHistory === 'object') {
    const dates = Object.keys(tradesHistory).sort().reverse();
    for (const d of dates) {
      for (const t of (tradesHistory[d] || [])) {
        all.push(Object.assign({}, t, { date: d }));
      }
    }
  }
  // 兼容旧数据: 若 history 为空, 用 tradesToday 兜底 (date 用今日)
  if (!all.length && Array.isArray(tradesToday) && tradesToday.length) {
    const today = new Date().toISOString().slice(0,10);
    all = tradesToday.map(t => Object.assign({}, t, { date: t.date || today }));
  }
  // 2) 重建日期过滤选项
  const filter = $('#tradeDateFilter');
  const datesSet = Array.from(new Set(all.map(t => t.date))).sort().reverse();
  const prev = filter.value;
  filter.innerHTML = '<option value="all">全部</option>' +
    datesSet.map(d => `<option value="${d}">${d}${d===datesSet[0]?' (今日)':''}</option>`).join('');
  if (prev && (prev === 'all' || datesSet.includes(prev))) filter.value = prev;
  // 3) 应用过滤
  const sel = filter.value;
  _tState.all = all;
  _tState.filtered = sel === 'all' ? all : all.filter(t => t.date === sel);
  _tState.page = 1;  // 数据刷新后回到第一页
  // 4) 渲染当前页
  _renderTradePage();
  // 5) 计数
  const buyN = _tState.filtered.filter(t=>t.type==='buy').length;
  const sellN = _tState.filtered.filter(t=>t.type==='sell').length;
  const pnlSum = _tState.filtered.reduce((a,t)=>a+(t.pnl||0),0);
  const pnlCls = pnlSum > 0 ? 'up' : (pnlSum < 0 ? 'down' : '');
  const pnlStr = `${pnlSum>=0?'+':''}${fmt(pnlSum,2)}`;
  $('#tradeCount').innerHTML =
    `共 <b>${_tState.filtered.length}</b> 笔 · 买 ${buyN} · 卖 ${sellN} · 累计已实现 <span class="${pnlCls}">${pnlStr} 元</span>`;
}

function renderTargets(targets){
  if(!targets||!targets.length){ $('#targetBody').innerHTML='<tr><td colspan="4" class="empty">—</td></tr>'; return; }
  $('#targetBody').innerHTML = targets.map(t=>`<tr><td>${t.canon}</td><td>${t.name||'—'}</td><td>${fmt(t.score)}</td><td class="${pnlCls(t.signal)}">${fmt(t.signal,3)}</td></tr>`).join('');
}

function renderMidday(m){
  const tag = $('#middayTag');
  if(!tag) return;
  if(!m || !m.active){
    tag.textContent = ''; return;
  }
  const t = m.result ? (m.result.time||'') : '';
  const n = m.targets ? m.targets.length : 0;
  tag.innerHTML = '<span style="color:hsl(var(--bear));font-weight:600">已午间重选' + (t?'  '+t:'') + (n?' · '+n+'只':'') + '</span>';
  if(m.result && m.result.top){ tag.innerHTML += '<span class="muted" style="margin-left:6px">→ ' + m.result.top.join(',') + '</span>'; }
}

// ---------- 市场情绪 ----------
function sentiCls(v){ if(v>=60) return 'hot'; if(v<40) return ''; return ''; }
function renderMarket(days){
  if(!days || !days.length){ $('#sentiBar').innerHTML='<span class="empty">暂无市场情绪数据</span>'; return; }
  $('#mkRange').textContent = days[0].day + ' ~ ' + days[days.length-1].day + ' · ' + days.length + ' 个交易日';
  // 情绪分柱状图
  const maxV = 100;
  $('#sentiBar').innerHTML = days.map(d=>{
    const v = d.sentiment!=null?d.sentiment:0;
    const h = Math.max(4, (v/maxV)*100);
    return `<div class="${v>=60?'hot':''}" style="height:${h}%" title="${d.day} 情绪${v}">`+
           `<em>${Math.round(v)}</em><i>${String(d.day).slice(5)}</i></div>`;
  }).join('');
  // 概览卡片
  const last = days[days.length-1];
  const w = last.width||{}; const l = last.limit||{}; const b = last.breadth||{};
  const tv = last.turnover||{};
  const cards = [
    {k:'最新情绪分', v:last.sentiment!=null?Math.round(last.sentiment)+'/100':'—', cls:last.sentiment>=60?'up':(last.sentiment<40?'down':'')},
    {k:'上涨/下跌家数', v:(w.up||0)+' / '+(w.down||0), h:'涨占比 '+(w.up_ratio!=null?w.up_ratio+'%':'—')},
    {k:'涨停 / 跌停', v:(l.limit_up||0)+' / '+(l.limit_down||0), h:'涨停占比 '+(l.limit_up_ratio!=null?l.limit_up_ratio+'%':'—')},
    {k:'中位数涨幅', v:b.median_chg!=null?fmt(b.median_chg,2)+'%':'—', cls:b.median_chg>0?'up':(b.median_chg<0?'down':'')},
    {k:'两市成交额', v:tv.total_amount_yi!=null?fmt(tv.total_amount_yi,0)+' 亿':'—'},
    {k:'强上涨(≥3%)', v:(b.strong_up_count||0)+' 只', h:'占比 '+(b.strong_up_ratio!=null?b.strong_up_ratio+'%':'—')},
  ];
  $('#mkCards').innerHTML = cards.map(c=>`<div class="miniCard"><div class="k">${c.k}</div>`+
    `<div class="v ${c.cls||''}">${c.v}</div>${c.h?`<div class="muted" style="font-size:11px;margin-top:3px">${c.h}</div>`:''}</div>`).join('');
  renderDonut(last);
  renderSector(last);
}

function renderDonut(last){
  const w = last.width||{};
  const up=w.up||0, down=w.down||0, flat=w.flat||0, tot=w.total||1;
  const upP=up/tot*100, downP=down/tot*100, flatP=flat/tot*100;
  const red='#ff5b6a', green='#2fe6a6', gray='#5a6b8c';
  // 环形: 红=上涨, 绿=下跌, 灰=平盘
  const donut = document.getElementById('upDonut');
  let acc=0;
  const segs=[
    {c:red, p:upP},{c:green,p:downP},{c:gray,p:flatP}
  ].filter(s=>s.p>0.05);
  const stops = segs.map(s=>{ const a=acc.toFixed(2)+'%'; acc+=s.p; const b=acc.toFixed(2)+'%'; return `${s.c} ${a} ${b}`; }).join(',');
  donut.style.background = `conic-gradient(${stops})`;
  donut.innerHTML = `<center><span class="bigNum">${upP.toFixed(1)}%</span><span class="muted" style="font-size:11px">上涨</span></center>`;
  $('#upLegend').innerHTML =
    `<div style="margin-bottom:6px"><span class="legendDot" style="background:${red}"></span>上涨 ${up} 只 (${upP.toFixed(1)}%)</div>`+
    `<div style="margin-bottom:6px"><span class="legendDot" style="background:${green}"></span>下跌 ${down} 只 (${downP.toFixed(1)}%)</div>`+
    `<div><span class="legendDot" style="background:${gray}"></span>平盘 ${flat} 只 (${flatP.toFixed(1)}%)</div>`;
}

function renderSector(last){
  const s = last.sector||{};
  const day = last.day||'';
  $('#secDay').textContent = day;
  const names={'SH':'沪市','SZ':'深市','BJ':'北交所'};
  const keys = Object.keys(s).filter(k=>s[k]!=null);
  if(!keys.length){ $('#secBody').innerHTML='暂无板块成交数据'; return; }
  const maxV = Math.max(...keys.map(k=>s[k]||0),1);
  $('#secBody').innerHTML = keys.map(k=>{
    const v = s[k]||0;
    return `<div style="display:flex;align-items:center;gap:10px;margin-bottom:10px">
      <span style="width:46px">${names[k]||k}</span>
      <div class="prog" style="flex:1"><i style="width:${(v/maxV*100).toFixed(1)}%"></i></div>
      <b style="min-width:70px;text-align:right">${fmt(v,1)} 亿</b>
    </div>`;
  }).join('');
}

// ---------- 绩效归因 ----------
function renderCommentary(c, meta){
  const $box = $('#pfCommentary'), $ai = $('#pfAiMeta');
  if(!c || typeof c !== 'object' || !c.commentary){
    $box.innerHTML = '<div class="muted">尚无 LLM 点评 — 盘后 run_daily 完成后会在此显示 (每30秒刷新)</div>';
    if($ai) $ai.textContent = '';
    return;
  }
  const pr = c.performance_review || {};
  const nx = c.next_session_brief || {};
  const stanceCls = nx.stance === '加仓' ? 'up' : (nx.stance === '减仓' ? 'down' : '');
  const driversHtml = (pr.drivers||[]).map(s=>`<li>${esc(s)}</li>`).join('') || '<li class="muted">无</li>';
  const risksHtml   = (pr.risks||[]).map(s=>`<li>${esc(s)}</li>`).join('') || '<li class="muted">无</li>';
  const watchHtml   = (nx.watchlist||[]).map(s=>`<li>${esc(s)}</li>`).join('') || '<li class="muted">无</li>';
  const actHtml     = (nx.action_items||[]).map(s=>`<li>${esc(s)}</li>`).join('') || '<li class="muted">无</li>';
  const scoreVal = Number(pr.score||0);
  const scoreCls = scoreVal >= 60 ? 'up' : (scoreVal <= 35 ? 'down' : '');
  const conf = Number(c.confidence||0);
  const confLow = conf <= 0.3;

  $box.innerHTML = `
    <div style="font-size:14px;font-weight:600;margin-bottom:10px;line-height:1.6">${esc(c.commentary)}</div>
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:14px;font-size:13px;line-height:1.7">
      <div>
        <div style="color:hsl(var(--fg-muted));margin-bottom:4px">净值解读</div>
        <div style="margin-bottom:6px">${esc(pr.headline||'证据不足')}</div>
        <div style="display:flex;gap:14px;margin-bottom:8px">
          <span>综合评分 <b class="${scoreCls}">${fmt(scoreVal,0)}</b>/100</span>
          <span>置信度 <b class="${confLow?'down':''}">${fmt(conf*100,0)}</b>%${confLow?' <span class="muted">(证据不足)</span>':''}</span>
        </div>
        <div style="color:hsl(var(--fg-muted));margin-bottom:4px">关键驱动</div>
        <ul style="margin:0 0 6px 16px;padding:0">${driversHtml}</ul>
        <div style="color:hsl(var(--fg-muted));margin-bottom:4px">主要风险</div>
        <ul style="margin:0;padding:0 0 0 16px">${risksHtml}</ul>
      </div>
      <div>
        <div style="color:hsl(var(--fg-muted));margin-bottom:4px">次日建议</div>
        <div style="margin-bottom:8px">仓位倾向 <b class="${stanceCls}">${esc(nx.stance||'观望')}</b></div>
        <div style="color:hsl(var(--fg-muted));margin-bottom:4px">关注要点</div>
        <ul style="margin:0 0 6px 16px;padding:0">${watchHtml}</ul>
        <div style="color:hsl(var(--fg-muted));margin-bottom:4px">建议操作</div>
        <ul style="margin:0 0 8px 16px;padding:0">${actHtml}</ul>
        <div style="color:hsl(var(--fg-muted));margin-bottom:4px">情绪解读</div>
        <div>${esc(c.sentiment_narrative||'证据不足')}</div>
      </div>
    </div>
  `;
  if($ai && meta){
    const lat = meta.latency_seconds ? `${meta.latency_seconds}s` : '';
    const tok = (meta.tokens_in!=null && meta.tokens_out!=null)
      ? `${meta.tokens_in}+${meta.tokens_out} tokens` : '';
    $ai.textContent = `${meta.model||'LLM'} · ${meta.generated_at||''}${lat?' · '+lat:''}${tok?' · '+tok:''}`;
  }
}

function renderPerf(r){
  if(!r || !r.ok){ $('#pfMetrics').innerHTML='<span class="empty">'+(r&&r.error?r.error:'暂无绩效数据')+'</span>'; return; }
  const pd=r.period||{};
  $('#pfRange').textContent = pd.start + ' ~ ' + pd.end + ' · ' + pd.n_days + ' 个交易日（每30秒刷新）';
  // LLM 盘后点评 (MiniMax-M3 归因)
  renderCommentary(r.llm_commentary, r.llm_commentary_meta);
  const m = r.metrics||{};
  const cards=[
    {k:'累计收益', v:fmt(m.total_return,2)+'%', cls:m.total_return>0?'up':(m.total_return<0?'down':'')},
    {k:'年化收益', v:fmt(m.annual_return,2)+'%', cls:m.annual_return>0?'up':(m.annual_return<0?'down':'')},
    {k:'最大回撤', v:fmt(m.max_drawdown,2)+'%', cls:'down'},
    {k:'夏普(年化)', v:m.sharpe_annual!=null?fmt(m.sharpe_annual,2):'—'},
    {k:'索提诺(年化)', v:m.sortino_annual!=null?fmt(m.sortino_annual,2):'—'},
    {k:'Calmar', v:m.calmar!=null?fmt(m.calmar,2):'—'},
    {k:'期末权益', v:fmt(m.final_equity,0), cls:'up'},
  ];
  const b = r.benchmark||{};
  if(b.bench_total_ret!=null){ cards.push({k:'基准超额', v:fmt(b.excess_total!=null?b.excess_total:0,2)+'%', cls:(b.excess_total||0)>=0?'up':'down'}); }
  $('#pfMetrics').innerHTML = cards.map(c=>`<div class="miniCard"><div class="k">${c.k}</div><div class="v ${c.cls||''}">${c.v}</div></div>`).join('');
  // 净值曲线
  const rows = (b.daily&&b.daily.length)?b.daily:null;
  if(rows && rows.length){
    const cum = rows.map(x=>x.port_cum!=null?x.port_cum:0);
    const minC=Math.min(...cum), maxC=Math.max(...cum,0); const span=(maxC-minC)||1;
    $('#pfEquity').innerHTML = cum.map((v,i)=>{
      const h=4+((v-minC)/span)*96;
      return `<div style="height:${h}%" title="${rows[i].day} ${fmt((v)*100,2)}%"><i>${String(rows[i].day).slice(5)}</i></div>`;
    }).join('');
  } else {
    $('#pfEquity').innerHTML='<span class="empty">回执样本不足，无法绘制净值曲线</span>';
  }
  // 持仓归因
  const att = r.attribution||[];
  if(att.length){
    $('#pfAttr').innerHTML = att.map(x=>`<tr>
      <td>${x.canon}</td><td>${fmt(x.qty,0)}</td><td>${fmt(x.avg_cost)}</td>
      <td>${fmt(x.last_price,3)}</td><td>${fmt(x.market_value,2)}</td>
      <td>${fmt((x.weight||0)*100,2)}%</td>
      <td class="${pnlCls(x.unrealized_pnl)}">${fmt(x.unrealized_pnl,2)}</td>
      <td class="${pnlCls(x.unrealized_pct)}">${fmt(x.unrealized_pct,2)}%</td>
    </tr>`).join('');
  } else { $('#pfAttr').innerHTML='<tr><td colspan="8" class="empty">暂无持仓</td></tr>'; }
  // IC监控
  const ic = r.ic_summary||{};
  const keys=Object.keys(ic);
  if(keys.length){
    $('#pfIc').innerHTML = keys.map(k=>`<tr>
      <td>${k}</td><td>${ic[k].n}</td>
      <td class="${ic[k].ic_mean>=0?'up':'down'}">${fmt(ic[k].ic_mean,4)}</td>
      <td>${ic[k].icir!=null?fmt(ic[k].icir,3):'—'}</td>
      <td class="${ic[k].recent_mean20>=0?'up':'down'}">${fmt(ic[k].recent_mean20,4)}</td>
    </tr>`).join('');
  } else { $('#pfIc').innerHTML='<tr><td colspan="5" class="empty">无 IC 数据</td></tr>'; }
  // 因子权重自适应 (异步拉取 /api/weights)
  renderWeights();
  // DRL 微调 (异步拉取 /api/drl)
  loadDrl();
  // 退化监控 (异步拉取 /api/degradation)
  loadDegradation();
}

async function loadDegradation(){
  const $box = $('#pfDegradation');
  if (!$box) return;
  try{
    const r = await fetch('/api/degradation'); const d = await r.json();
    renderDegradation(d);
  }catch(e){ $box.innerHTML = '<div class="empty">退化监控加载失败: '+e+'</div>'; }
}

function _lvlBadge(lvl){
  if (!lvl || lvl === 'OK') return '<span class="lvl OK">OK</span>';
  if (lvl === 'P0') return '<span class="lvl P0">P0 严重</span>';
  if (lvl === 'P1') return '<span class="lvl P1">P1 高</span>';
  if (lvl === 'P2') return '<span class="lvl P2">P2 中</span>';
  if (lvl === 'P3') return '<span class="lvl P3">P3 低</span>';
  return '<span class="lvl">'+esc(lvl)+'</span>';
}

function renderDegradation(d){
  const $box = $('#pfDegradation');
  if (!d || d.error){ $box.innerHTML = '<div class="empty">'+(d && d.error ? d.error : '暂无退化数据')+'</div>'; return; }
  const idx = d.index || {};
  const spc = d.spc || [];
  const alerts = d.alerts || [];
  const inc = d.incremental_learn;
  const rc = d.reward_config || {};

  // 退化综合分 (环形进度)
  const score = Number(idx.overall_score || 0);
  const worst = idx.worst_level || 'OK';
  const components = idx.components || [];

  let html = '<div style="display:grid;grid-template-columns:1fr 1fr;gap:18px">';

  // 左: 退化指数 + 维度
  html += '<div>';
  html += `<div style="display:flex;align-items:center;gap:14px;margin-bottom:10px">
    <div style="position:relative;width:90px;height:90px">
      <svg viewBox="0 0 36 36" style="width:90px;height:90px;transform:rotate(-90deg)">
        <circle cx="18" cy="18" r="15" fill="none" stroke="#1f2a3a" stroke-width="3"/>
        <circle cx="18" cy="18" r="15" fill="none" stroke="${score>=70?'#2fe6a6':(score>=40?'#f59e0b':'#ff5b6a')}"
          stroke-width="3" stroke-dasharray="${(score/100)*94.2} 94.2" stroke-linecap="round"/>
      </svg>
      <div style="position:absolute;inset:0;display:flex;align-items:center;justify-content:center;font-size:18px;font-weight:700">${Math.round(score)}</div>
    </div>
    <div>
      <div style="font-size:13px;color:hsl(var(--fg-muted))">综合退化指数 (越高越健康)</div>
      <div style="margin-top:4px">最差维度 ${_lvlBadge(worst)}</div>
    </div>
  </div>`;
  // 维度
  if (components.length){
    html += '<div style="margin-top:8px">';
    for (const c of components){
      const s = Number(c.score || 0);
      html += `<div style="display:flex;align-items:center;gap:8px;margin-bottom:4px;font-size:12px">
        <span style="width:120px">${esc(c.name)}</span>
        ${_lvlBadge(c.level)}
        <span style="flex:1;background:hsl(var(--elevated));border-radius:3px;height:6px;position:relative;overflow:hidden">
          <span style="position:absolute;left:0;top:0;height:100%;width:${Math.max(2,s)}%;background:${s>=70?'#2fe6a6':(s>=40?'#f59e0b':'#ff5b6a')}"></span>
        </span>
        <span style="width:36px;text-align:right">${s}</span>
      </div>`;
    }
    html += '</div>';
  }
  html += '</div>';

  // 右: SPC 告警 + 增量学习
  html += '<div>';
  // SPC 卡片
  if (spc.length){
    html += '<div style="margin-bottom:12px"><div style="color:hsl(var(--fg-muted));font-size:12px;margin-bottom:6px">SPC 过程控制 (近 40 日)</div>';
    for (const s of spc){
      const lvl = s.level || 'OK';
      const cls = lvl === 'P0' ? '#ff5b6a' : (lvl === 'P1' ? '#f59e0b' : (lvl === 'P2' ? '#3b82f6' : '#2fe6a6'));
      html += `<div style="display:flex;align-items:center;gap:8px;padding:6px 8px;margin-bottom:4px;border-left:3px solid ${cls};background:hsl(var(--elevated));border-radius:3px">
        ${_lvlBadge(lvl)}
        <span style="width:140px">${esc(s.indicator)}</span>
        <span style="flex:1;font-size:12px;color:hsl(var(--fg-secondary))">${esc((s.violations||[])[0]||s.message||'OK')}</span>
      </div>`;
    }
    html += '</div>';
  }
  // reward_config
  html += `<div style="margin-bottom:8px;font-size:12px;color:hsl(var(--fg-muted))">
    DRL reward 权重: vnpy <b>${(rc.vnpy_weight||0.6).toFixed(2)}</b>
    / ic <b>${(rc.ic_weight||0.4).toFixed(2)}</b>
  </div>`;
  // 增量学习
  if (inc && inc._day_dir){
    const opt = inc.optimization || {};
    const ar = opt.action_recommendation || {};
    html += `<div style="padding:8px;background:hsl(var(--elevated));border-radius:5px;border-left:3px solid #8b5cf6">
      <div style="font-size:12px;color:hsl(var(--fg-muted));margin-bottom:4px">LLM 策略优化 (${esc(inc._day_dir)})</div>
      <div style="font-size:13px;margin-bottom:4px"><b>${esc(opt.diagnosis_summary||'—')}</b></div>
      <div style="font-size:12px;margin-bottom:4px">操作: ${_lvlBadge(ar.priority||'OK')} ${esc(ar.primary||'hold')}</div>
      ${(opt.root_causes||[]).length ? `<div style="font-size:11px;color:hsl(var(--fg-secondary));margin-top:4px">根因: ${esc((opt.root_causes||[]).slice(0,3).join('; '))}</div>` : ''}
    </div>`;
  } else {
    html += '<div style="font-size:12px;color:hsl(var(--fg-muted))">增量学习: 未触发 (无 P0/P1 退化)</div>';
  }
  html += '</div>';

  html += '</div>';  // 闭合 grid
  $box.innerHTML = html;
}

async function loadDrl(){
  const $box = $('#pfDrl');
  if (!$box) return;
  try{
    const r = await fetch('/api/drl'); const d = await r.json();
    renderDrl(d);
  }catch(e){ $box.innerHTML = '<div class="empty">DRL 数据加载失败: '+e+'</div>'; }
}

function _sentBar(label, v, lo, hi){
  const norm = lo < 0 ? (v - lo) / (hi - lo) : 0.5;
  const pct = Math.max(0, Math.min(100, norm * 100));
  const cls = v > 0.05 ? 'up' : (v < -0.05 ? 'down' : '');
  return `<div style="margin-bottom:6px">
    <span style="display:inline-block;width:120px">${label}</span>
    <span style="display:inline-block;width:60px;text-align:right" class="${cls}">${v>=0?'+':''}${Number(v).toFixed(2)}</span>
    <span style="display:inline-block;width:200px;vertical-align:middle">
      <span style="display:inline-block;width:${pct}%;height:6px;background:#2fe6a6;border-radius:3px"></span>
    </span>
  </div>`;
}

function _weightBar(label, base, prior){
  const final = prior; // 仅展示 base -> prior, 避免与 PPO 最终权重混淆
  const maxV = Math.max(base, prior, 0.05);
  const baseW = Math.max(2, base / maxV * 100);
  const priorW = Math.max(2, prior / maxV * 100);
  return `<tr>
    <td>${label}</td>
    <td style="width:40%">
      <div style="display:flex;gap:8px;font-size:11px;color:hsl(var(--fg-muted));margin-bottom:2px">
        <span>base ${(base*100).toFixed(1)}%</span>
        <span>prior ${(prior*100).toFixed(1)}%</span>
      </div>
      <div style="background:hsl(var(--elevated));border-radius:3px;overflow:hidden;height:14px;position:relative">
        <div style="position:absolute;left:0;top:0;height:100%;width:${baseW}%;background:hsl(var(--fg-muted));opacity:0.5"></div>
        <div style="position:absolute;left:0;top:0;height:100%;width:${priorW}%;background:#1f9d55"></div>
      </div>
    </td>
  </tr>`;
}

function renderDrl(d){
  const $box = $('#pfDrl');
  if (!d || !d.days || !d.days.length){
    $box.innerHTML = '<div class="muted">尚无 DRL 训练产物 — 盘后 run_daily 完成后会在此显示</div>';
    return;
  }
  const day = d.days[0];
  const m = day.meta || {};
  const b = day.brief || {};
  const sf = b.sentiment_factors || {};
  const fr = b.factor_recommendations || {};
  const bw = m.base_weights || {};
  const pw = m.prior_weights || {};

  const stanceCls = b.stance === '加仓' ? 'up' : (b.stance === '减仓' ? 'down' : '');
  const regimeStr = b.regime || '—';
  const deltaScale = (m.llm_brief && m.llm_brief.delta_scale) || 1.0;
  const conf = b.confidence || 0;

  let briefHtml = '';
  if (b.market_summary || b.stance){
    briefHtml = `
      <div style="margin-bottom:10px;padding:10px;background:hsl(var(--elevated));border-radius:6px;border-left:3px solid #1f9d55">
        <div style="font-size:13px;line-height:1.6;margin-bottom:6px">${esc(b.market_summary||'证据不足')}</div>
        <div style="display:flex;flex-wrap:wrap;gap:14px;font-size:12px;color:hsl(var(--fg-secondary))">
          <span>regime <b>${esc(regimeStr)}</b></span>
          <span>stance <b class="${stanceCls}">${esc(b.stance||'—')}</b></span>
          <span>置信度 <b>${(conf*100).toFixed(0)}</b>%</span>
          <span>扰动幅度 ×${deltaScale.toFixed(2)}</span>
        </div>
      </div>
    `;
  } else {
    briefHtml = '<div class="muted">未找到 pre_drl_brief 产物 (本次 DRL 未引用 LLM brief)</div>';
  }

  // 情绪因子条
  const sentHtml = (sf.risk_on_off !== undefined) ? `
    <div style="display:grid;grid-template-columns:repeat(2,1fr);gap:8px;margin-bottom:12px">
      ${_sentBar('风险偏好', sf.risk_on_off ?? 0, -1, 1)}
      ${_sentBar('轮动强度', sf.rotation_intensity ?? 0, -1, 1)}
      ${_sentBar('流动性(反转)', sf.liquidity_stress ?? 0, -1, 1)}
      ${_sentBar('政策催化', sf.policy_catalyst ?? 0, -1, 1)}
    </div>
  ` : '';

  // 先验权重 vs 基础权重
  const factors = Object.keys(bw).filter(k => pw[k] != null);
  let weightsHtml = '';
  if (factors.length){
    weightsHtml = `
      <table class="tbl" style="margin-bottom:10px">
        <thead><tr><th style="width:90px">因子</th><th>基础 vs LLM先验权重</th></tr></thead>
        <tbody>${factors.map(k => _weightBar(k, bw[k], pw[k])).join('')}</tbody>
      </table>
    `;
  }

  // 训练概要
  const finalW = m.final_weights || {};
  const topFactors = Object.entries(finalW).sort((a, b) => b[1] - a[1]).slice(0, 3);
  const metaHtml = `
    <div style="display:grid;grid-template-columns:repeat(4,1fr);gap:8px;margin-bottom:8px;font-size:12px">
      <div><div class="muted">Timesteps</div><b>${m.total_timesteps || '—'}</b></div>
      <div><div class="muted">Mean Reward</div><b class="${(m.mean_reward||0)>=0?'up':'down'}">${fmt(m.mean_reward, 4)}</b></div>
      <div><div class="muted">Obs Dim</div><b>${(m.llm_brief && m.llm_brief.obs_dim) || '—'}</b></div>
      <div><div class="muted">vnpy Reward</div><b class="${(m.vnpy_reward||0)>=0?'up':'down'}">${fmt(m.vnpy_reward, 4)}</b></div>
    </div>
    <div style="font-size:12px;color:hsl(var(--fg-secondary))">
      Top-3 因子权重: ${topFactors.map(([k, v]) => `${k} ${(v*100).toFixed(1)}%`).join(' · ')}
    </div>
  `;

  $box.innerHTML = `
    <div style="margin-bottom:8px;color:hsl(var(--fg-muted));font-size:12px">交易日 ${day.day}</div>
    ${briefHtml}
    ${sentHtml}
    ${weightsHtml}
    ${metaHtml}
  `;
}

async function renderWeights(){
  const $w = $('#pfWeights'), $m = $('#pfWmeta');
  let d;
  try{ const resp = await fetch('/api/weights'); d = await resp.json(); }
  catch(e){ $w.innerHTML='<span class="empty">权重数据读取失败</span>'; return; }
  if(!d || !d.ok || !Object.keys(d.weights||{}).length){
    $w.innerHTML='<span class="empty">尚无自适应权重 (weight_optimizer 未运行)</span>';
    $m.textContent=''; return;
  }
  const meta = d.meta||{};
  const icir = meta.icir||{};
  // 权重卡片(含 ICIR 依据标注)
  const wt = d.weights||{};
  const cards = Object.keys(wt).map(k=>{
    const base = (meta.static_base||{})[k];
    const g = icir[k];
    const isAlpha = !!g;
    const hintParts=[];
    if(isAlpha){
      hintParts.push('ICIR '+(g.icir!=null?fmt(g.icir,3):'—'));
      hintParts.push('|ICIR| '+(g.icir!=null?fmt(Math.abs(g.icir),3):'—'));
    }
    hintParts.push('静态 '+fmt((base!=null?base:0)*100,1)+'%');
    let cls = '';
    if(isAlpha && g.icir!=null) cls = g.icir<0?'down':'up';
    return `<div class="miniCard"><div class="k">${k}${isAlpha?' <span class="sBadge alpha">α</span>':''}</div>
      <div class="v ${cls}">${fmt(wt[k]*100,2)}%</div>
      <div class="hint">${hintParts.join(' · ')}</div></div>`;
  }).join('');
  $w.innerHTML = cards;
  // 说明行
  const updated = d.updated||meta.generated||'';
  $m.innerHTML = '方法: ' + (meta.note||'') + ' · 窗口 ' + (meta.window||'—')
    + ' 日 / alpha 占比 ' + fmt((meta.alpha_share!=null?meta.alpha_share*100:0),0) + '%'
    + (updated?(' · 更新 ' + updated):'');
}

function apply(s){
  window._lastState = s;   // 缓存: 成交历史日期过滤 change 时复用
  // 安全写入: 任一元素不存在或缺数据时不中断整体渲染
  const _setTxt = (id, v) => { const el = document.getElementById(id); if (el) el.textContent = v; };
  _setTxt('sub', '更新 ' + (s.updated||'—'));
  _setTxt('bDay', '交易日 ' + (s.day||'—'));
  const m = s.mode||'';
  const bm = document.getElementById('bMode');
  if (bm) { bm.textContent = m; bm.className='badge '+(s.in_session?'live':'idle'); }
  const bs = document.getElementById('bSrc');
  if (bs) bs.textContent = '数据源: ' + (s.live_source||'—');
  _setTxt('bTick', 'tick #' + (s.tick||'-'));
  try {
    if(s.feed_error){ if(bs) bs.textContent += ' ⚠'; _setTxt('sub', '更新 ' + (s.updated||'—') + ' · 实时告警:' + s.feed_error.slice(0,40)); }
    const _tryRender = (name, fn) => { try{ fn(); }catch(e){ console.error('render '+name+' err:', e); } };
    _tryRender('Cards',    () => renderCards(s));
    _tryRender('Pos',      () => renderPos(s.positions));
    _tryRender('Trades',   () => renderTrades(s.trades_history, s.trades_today));
    _tryRender('Targets',  () => renderTargets(s.targets));
    _tryRender('Midday',   () => renderMidday(s.midday));
    _tryRender('Rules',    () => renderRules(s));
  } catch(err) {
    if (typeof console !== 'undefined') console.error('apply err:', err);
    // 把错误显式渲染到 #cards, 避免静默失败
    const cards = document.getElementById('cards');
    if(cards) cards.innerHTML = '<div style="color:#f55;padding:8px;border:1px solid #f55;border-radius:4px">渲染异常: '+err+'</div>';
  }
}

let logPos = 0; let logBuf = []; let logErrCnt = 0;
function renderLogs(){
  const box = $('#logBox'); if(!box) return;
  box.innerHTML = logBuf.map(l=>{
    let cls='', tag='';
    if(l.indexOf('[daemon]')>=0) tag='tag-daemon';
    else if(l.indexOf('[daily]')>=0) tag='tag-daily';
    else if(/\[sub\]/.test(l)) tag='tag-sub';
    if(/error|异常|失败|卡死/i.test(l)) cls='err';
    // 提取时间戳(前19字符)与剩余内容
    const m = l.match(/^(\S+\s+\S+)\s+(.*)$/);
    return `<div class="logLine ${cls}">${m?`<span class="ts">${m[1]}</span>${m[2].replace(/\[(daemon|daily|sub)\]/, (mm,tg)=>`<span class="tag-${tg}">[${tg}]</span>`)}`:l}</div>`;
  }).join('');
  // 自动滚到底
  if(box.scrollTop + box.clientHeight >= box.scrollHeight - 30 || box._follow===undefined){
    box.scrollTop = box.scrollHeight; box._follow=1;
  }
  if(logErrCnt>5){ $('#logDot').textContent='● 守护离线'; $('#logDot').className='sBadge bad'; }
}
async function loadLogs(){
  try{
    const r = await fetch('/api/logs?pos='+logPos);
    const d = await r.json();
    logErrCnt=0; $('#logDot').textContent='● 守护在线'; $('#logDot').className='sBadge ok';
    if(d && d.lines && d.lines.length){
      logBuf = logBuf.concat(d.lines);
      if(logBuf.length>500) logBuf = logBuf.slice(-500);
      renderLogs();
    }
    if(d && d.pos){ logPos = d.pos; }
  }catch(e){
    logErrCnt++; $('#logDot').textContent='● 守护离线'; $('#logDot').className='sBadge bad';
    return false;
  }
  return true;
}

function renderFreezeStatus(d){
  const rail = document.getElementById('freezeRail');
  if(!rail) return;
  const state = (d && d.status) || 'unavailable';
  const labels = {
    ready: '冻结就绪',
    missing: '未生成快照',
    invalid: '快照格式异常',
    tampered: '校验失败 · 拒绝使用',
    unavailable: '状态不可用'
  };
  rail.dataset.state = state;
  const set = (id, value) => { const el=document.getElementById(id); if(el) el.textContent=value; };
  set('freezeStatus', labels[state] || state);
  set('freezeMode', String((d && d.mode) || 'shadow').toUpperCase());
  set('freezeAt', d && d.generated_at ? String(d.generated_at).replace('T',' ') : (d && d.reason ? String(d.reason) : '尚无已验证时间戳'));
  set('freezeHash', d && d.snapshot_hash ? 'hash '+String(d.snapshot_hash).slice(0,8) : 'hash —');
  set('freezeLate', '迟到 '+((d && d.late_count) == null ? '—' : d.late_count));
  set('freezeUnexplained', '未解释 '+((d && d.unexplained_count) == null ? '—' : d.unexplained_count));
}
async function loadFreezeStatus(){
  try{
    const d = await fetch('/api/signal-freeze', {cache:'no-store'}).then(r=>r.json());
    renderFreezeStatus(d);
  }catch(e){
    renderFreezeStatus({status:'unavailable', mode:'shadow', reason:'看板无法读取冻结状态'});
    return false;
  }
  return true;
}

async function load(){
  let liveOk = true;
  try{
    const r = await fetch('/api/live'); const s = await r.json();
    if(s){ apply(s); }
  }catch(e){
    liveOk = false;
    const sub = document.getElementById('sub');
    if(sub) sub.textContent='连接失败: '+e;
    // 把诊断信息也写到面板上, 避免静默错误
    const cards = document.getElementById('cards');
    if(cards) cards.innerHTML = '<div style="color:#f55">fetch /api/live 异常: '+e+'</div>';
    if(typeof console !== 'undefined') console.error('load /api/live err:', e);
  }
  try{
    const rb = await fetch('/api/backtest'); const bt = await rb.json();
    renderBacktest(bt);
  }catch(e){ /* 忽略回放加载错误 */ }
  return liveOk;
}

// 页面隐藏时停止轮询；接口失败时指数退避，恢复可见后立即刷新。
// 这层只负责调度，不改变各 API 的刷新周期和数据契约。
function adaptivePoll(task, baseMs, maxMs){
  let timer = null;
  let delay = baseMs;
  let running = false;
  let disposed = false;
  const schedule = (ms) => {
    if(disposed || document.hidden) return;
    timer = setTimeout(run, ms);
  };
  const run = async () => {
    timer = null;
    if(disposed || document.hidden || running) return;
    running = true;
    let ok = false;
    try{ ok = (await task()) !== false; }catch(e){ ok = false; }
    running = false;
    delay = ok ? baseMs : Math.min(maxMs, Math.max(baseMs, delay * 2));
    schedule(delay);
  };
  const onVisibility = () => {
    if(document.hidden){
      if(timer !== null) clearTimeout(timer);
      timer = null;
      return;
    }
    delay = baseMs;
    if(timer === null && !running) run();
  };
  document.addEventListener('visibilitychange', onVisibility);
  run();
  return () => {
    disposed = true;
    if(timer !== null) clearTimeout(timer);
    document.removeEventListener('visibilitychange', onVisibility);
  };
}

adaptivePoll(load, 3000, 30000);
adaptivePoll(loadLogs, 3000, 30000);
adaptivePoll(loadFreezeStatus, 10000, 60000);
adaptivePoll(loadOverview, 15000, 60000);
loadBacktestHistory();  // 回测历史面板首屏即加载 (与用户是否切 tab 无关)
// 系统健康 · 门控 与 风控事件流 (对应规范"系统状态"与"风险风控"面板)
async function loadOverview(){
  try{
    const d = await fetch('/api/overview').then(r=>r.json());
    const G = document.getElementById('sysGate');
    if(G && d.gate){
      const g = d.gate;
      if(g.error){ G.innerHTML='<div class="muted">门控数据缺失: '+esc(g.error)+'</div>'; }
      else{
        const col = g.regime==='risk' ? '#f66' : (g.regime==='caution' ? '#fa0' : '#2d7');
        const dot = n => { const v=(d.procs||{})[n];
          if(!v||v.alive===null) return '<span style="color:#888">●</span>';
          return v.alive?'<span style="color:#2d7">●</span>':'<span style="color:#f66">●</span>'; };
        const dt = d.data||{};
        const stale = (dt.stale&&dt.stale.length) ? dt.stale.join(', ') : '无';
        const hb = d.engine_heartbeat||{};
        G.innerHTML =
          '<div style="display:flex;flex-wrap:wrap;gap:8px 22px;align-items:center">'
          +'<span>IC门控 <b style="color:'+col+'">'+esc(g.regime||'—')+'</b></span>'
          +'<span>暴露 ×'+fmt(g.exposure_mult!=null?g.exposure_mult:1,2)+'</span>'
          +'<span>调仓间隔 '+esc(g.interval_days||'—')+'日</span>'
          +'<span>IC均值 '+fmt(g.ic_mean,4)+' (as_of '+esc(g.ic_as_of||'—')+')</span>'
          +'<span>冻结新买 '+(g.freeze_new_buys?'<b style="color:#f66">是</b>':'否')+'</span>'
          +'</div>'
          +'<div style="margin-top:8px;font-size:12px">'
          +'守护 '+dot('daemon')+' 引擎 '+dot('engine')+' 仪表台 '+dot('dashboard')
          +' <span class="muted">|</span> 引擎心跳 '+esc(hb.updated||'—')
          +' <span class="muted">|</span> 模式 '+esc(hb.mode||'—')
          +'<br>数据巡检 '+esc(dt.report_ts||'未生成')+' · 基准 '+esc(dt.base||'—')
          +' · 规范表 '+((dt.checks)?dt.checks.ok+'/'+dt.checks.total+' 通过':'—')
          +' · 滞后表: <b style="color:'+(stale==='无'?'#2d7':'#f66')+'">'+esc(stale)+'</b>'
          +'</div>';
      }
    }
    const R = document.getElementById('riskEvents');
    if(R){
      const ev = d.risk_events||[];
      if(!ev.length){ R.innerHTML='<div class="muted">暂无风控/门控/异常事件</div>'; }
      else{
        R.innerHTML = ev.map(e=>
          '<div style="font-size:12px;padding:3px 0;border-bottom:1px dashed rgba(255,255,255,.08)">'
          +'<span class="muted">'+esc(e.ts||'')+'</span> [<b>'+esc(e.src)+'</b>] '+esc(e.line)+'</div>'
        ).join('');
      }
    }
  }catch(e){ /* 概览面板加载失败不阻塞主流程 */ return false; }
  return true;
}

// tab 徽章数: 启动后延迟 800ms 首次刷新, 此后每 15s 一次
setTimeout(refreshTabBadges, 800);
setInterval(refreshTabBadges, 15000);

// 成交历史日期过滤 -> 切换时仅重渲染 tradeBody (无需等下次状态拉取)
(function(){
  const f = $('#tradeDateFilter');
  if (f) f.addEventListener('change', () => {
    // 重新触发一次过滤渲染: 用上一次的 trades_history / trades_today
    if (window._lastState) renderTrades(window._lastState.trades_history, window._lastState.trades_today);
  });
})();

// 成交历史翻页控件事件委托 (控件每次重建, 用 document 委托避免重复绑定)
document.addEventListener('click', (ev) => {
  const btn = ev.target.closest && ev.target.closest('.pgbtn');
  if (!btn || btn.disabled) return;
  const act = btn.dataset.act;
  const s = _tState;
  const totalPages = Math.max(1, Math.ceil(s.filtered.length / s.perPage));
  let target = s.page;
  if (act === 'prev') target = s.page - 1;
  else if (act === 'next') target = s.page + 1;
  else if (act === 'first') target = 1;
  else if (act === 'last') target = totalPages;
  if (target < 1 || target > totalPages) return;
  if (target !== s.page) { s.page = target; _renderTradePage(); }
});
document.addEventListener('change', (ev) => {
  if (ev.target && ev.target.id === 'tradePgInput') {
    const s = _tState;
    const totalPages = Math.max(1, Math.ceil(s.filtered.length / s.perPage));
    let v = parseInt(ev.target.value, 10);
    if (isNaN(v)) v = s.page;
    v = Math.min(Math.max(v, 1), totalPages);
    if (v !== s.page) { s.page = v; _renderTradePage(); }
  }
});

// ---------- Tab 切换与二级加载 ----------
let mkLoaded=false, pfLoaded=false;
let mbLoaded=false;
let btLoaded=false, cptLoaded=false, indLoaded=false;
let monLoaded=false, regLoaded=false, abnLoaded=false, dbpLoaded=false;

// 工具: 给 tab 按钮的 badge-count 写值, 0 / 异常时不显示
function _setBadge(name, val){
  const el = document.getElementById('cnt-' + name);
  if (!el) return;
  if (val == null || val === 0 || isNaN(val)) el.textContent = '·';
  else if (val > 999) el.textContent = '999+';
  else el.textContent = String(val);
}

// 异步刷新所有 tab 的徽章数 (基于 /api 各端点的真实数据)
async function refreshTabBadges(){
  // 异动 (涨停数)
  try {
    const r = await fetch('/api/abnormal?limit=100').then(r=>r.json());
    let upCount = 0;
    if (Array.isArray(r)) upCount = r.filter(x => (x.change_pct || x.pct_chg || 0) >= 9.5).length;
    else if (r && Array.isArray(r.limit_up)) upCount = r.limit_up.length;
    else if (r && Array.isArray(r.top_change)) upCount = r.top_change.filter(x => (x.change_pct || 0) >= 9.5).length;
    else if (r && Array.isArray(r.rows)) upCount = r.rows.filter(x => (x.change_pct || x.pct_chg || 0) >= 9.5).length;
    _setBadge('abnormal', upCount);
  } catch(e) { _setBadge('abnormal', null); }
  // 回测历史
  try {
    const r = await fetch('/api/backtest/history').then(r=>r.json());
    _setBadge('backtest', (r.rows || []).length);
  } catch(e) { _setBadge('backtest', null); }
  // 监控中心
  try {
    const r = await fetch('/api/monitor').then(r=>r.json());
    const n = (r.alerts || r.items || []).length;
    _setBadge('monitor', n);
  } catch(e) { _setBadge('monitor', null); }
  // 数据板块 (表数量)
  try {
    const r = await fetch('/api/db_meta').then(r=>r.json());
    const n = (r.tables || []).length;
    _setBadge('dbpanel', n);
  } catch(e) { _setBadge('dbpanel', null); }
  // 市场看板 (指数数)
  try {
    const r = await fetch('/api/marketboard').then(r=>r.json());
    let n = 0;
    if (Array.isArray(r)) n = r.length;
    else if (r && Array.isArray(r.indices)) n = r.indices.length;
    else if (r && Array.isArray(r.kpi)) n = r.kpi.length;
    else if (r && r.kpi) n = Object.keys(r.kpi).length;
    _setBadge('dashboard', n);
  } catch(e) { _setBadge('dashboard', null); }
  // 市场情绪
  try {
    const r = await fetch('/api/market').then(r=>r.json());
    let n = 0;
    if (Array.isArray(r)) n = r.length;
    else if (r && Array.isArray(r.rows)) n = r.rows.length;
    else if (r && r.up_count != null) n = r.up_count + (r.down_count || 0);
    else if (r && r.series) n = r.series.length;
    _setBadge('market', n);
  } catch(e) { _setBadge('market', null); }
  // 概念
  try {
    const r = await fetch('/api/concept').then(r=>r.json());
    let n = 0;
    if (Array.isArray(r)) n = r.length;
    else if (r && Array.isArray(r.concepts)) n = r.concepts.length;
    else if (r && Array.isArray(r.items)) n = r.items.length;
    _setBadge('concept', n);
  } catch(e) { _setBadge('concept', null); }
  // 行业
  try {
    const r = await fetch('/api/industry').then(r=>r.json());
    let n = 0;
    if (Array.isArray(r)) n = r.length;
    else if (r && Array.isArray(r.industries)) n = r.industries.length;
    else if (r && Array.isArray(r.items)) n = r.items.length;
    _setBadge('industry', n);
  } catch(e) { _setBadge('industry', null); }
}

// ===================== 深度分析 (K线/净值/月收益/持仓/导出) =====================
const UP='#ff6b6b', DOWN='#3dd68c', AXIS='#8b98b3', GRID='rgba(255,255,255,.07)';
const MA_COL={ma5:'#f0b400',ma10:'#4f8cff',ma20:'#c678dd',ma60:'#26d0ce'};
let deepLoaded=false;

function deepColors(){
  const cs=getComputedStyle(document.documentElement);
  const r=cs.getPropertyValue('--base')||'224 232 245';
  return 'hsl('+r+')';
}
function axisLabel(ctx,x,y,txt,align){
  ctx.fillStyle=AXIS; ctx.font='10px ui-monospace,Consolas,monospace'; ctx.textAlign=align||'center';
  ctx.fillText(txt,x,y);
}
function gridLines(ctx,vals,x0,x1,y,y0){
  ctx.strokeStyle=GRID; ctx.lineWidth=1;
  for(const v of vals){ const yy=y0+(v-y0)/1; ctx.beginPath(); ctx.moveTo(x0, yy); ctx.lineTo(x1, yy); ctx.stroke(); }
}
function niceMinMax(a,b,n){
  const pad=(b-a)*0.08||1; let lo=a-pad, hi=b+pad;
  return [lo,hi];
}

// ---------- 组合净值·回撤 ----------
let eqCache=null;
async function loadEq(){
  try{ const d=await fetch('/api/curve').then(r=>r.json()); eqCache=d; drawEq(ksRange); }
  catch(e){ const t=document.getElementById('eqTip'); if(t)t.textContent='净值加载失败: '+e; }
}
let ksRange='all';
function drawEq(range, hoverIdx){
  hoverIdx = (hoverIdx==null)?-1:hoverIdx;
  const cv=document.getElementById('eqCanvas'); if(!cv||!eqCache||!eqCache.ok){return;}
  const ctx=cv.getContext('2d'); const W=cv.width,H=cv.height;
  ctx.clearRect(0,0,W,H);
  let rows=eqCache.rows;
  const nMax = range==='all'?rows.length:(+range);
  rows=rows.slice(-nMax);
  if(rows.length<2){ const t=document.getElementById('eqTip'); if(t)t.textContent='回执样本不足, 暂无法绘制'; return; }
  const L=70,R=16,T=14,B=24, M=T+14;
  const mainBtm=H-B-((H-T-B)*0.26);
  // 上:净值; 下:回撤面积
  const navs=rows.map(r=>r.nav), dds=rows.map(r=>r.dd);
  const [lo,hi]=niceMinMax(Math.min.apply(0,navs),Math.max.apply(0,navs),10);
  const x=i=>L+i*(W-L-R)/(rows.length-1);
  const yN=v=>M+(1-(v-lo)/(hi-lo))*(mainBtm-M);
  // 回撤区 (负数)
  const yD0=H-B-8, ddTop=H-B-(H-T-B)*0.26;
  // grid & axis nav
  const gridN=6;
  for(let i=0;i<=gridN;i++){ const v=lo+(hi-lo)*i/gridN; const yy=yN(v);
    ctx.strokeStyle=GRID; ctx.beginPath(); ctx.moveTo(L,yy); ctx.lineTo(W-R,yy); ctx.stroke();
    axisLabel(ctx,L-6,yy+3,v.toFixed(3),'right'); }
  // 净值线
  ctx.strokeStyle='#4f8cff'; ctx.lineWidth=1.6; ctx.beginPath();
  rows.forEach((r,i)=>{ const px=x(i),py=yN(r.nav); i?ctx.lineTo(px,py):ctx.moveTo(px,py); });
  ctx.stroke();
  // 首日基准虚线
  ctx.strokeStyle='rgba(255,255,255,.25)'; ctx.setLineDash([4,4]); ctx.beginPath();
  ctx.moveTo(L,yN(1)); ctx.lineTo(W-R,yN(1)); ctx.stroke(); ctx.setLineDash([]);
  axisLabel(ctx,L+4,yN(1)-4,'1.00 (基准)','left');
  // 回撤填充(底部区)
  const dLo=Math.min.apply(0,dds), dHi=0;
  ctx.fillStyle='rgba(255,107,107,.20)'; ctx.beginPath();
  ctx.moveTo(x(0),yD0);
  dds.forEach((v,i)=>ctx.lineTo(x(i), yD0-(v-dLo)/((dHi-dLo)||1)*(yD0-ddTop)));
  ctx.lineTo(x(rows.length-1),yD0); ctx.closePath(); ctx.fill();
  // 回撤轴
  for(let i=0;i<=3;i++){ const v=dLo+(0-dLo)*i/3; const yy=yD0-(v-dLo)/((dHi-dLo)||1)*(yD0-ddTop);
    axisLabel(ctx,L-6,yy+3,v.toFixed(1)+'%','right'); }
  axisLabel(ctx,L,ddTop-4,'回撤%','left');
  // x 轴日期
  const step=Math.max(1,Math.floor(rows.length/8));
  for(let i=0;i<rows.length;i+=step){ axisLabel(ctx,x(i),H-6,rows[i].day.slice(2),'center'); }
  axisLabel(ctx,L,10,'净值','left');
  // 十字光标
  if(hoverIdx>=0 && hoverIdx<rows.length){
    const px=x(hoverIdx);
    ctx.strokeStyle='rgba(255,255,255,.45)'; ctx.lineWidth=1;
    ctx.beginPath(); ctx.moveTo(px, T); ctx.lineTo(px, H-B); ctx.stroke();
    const py=yN(rows[hoverIdx].nav);
    ctx.beginPath(); ctx.moveTo(L, py); ctx.lineTo(W-R, py); ctx.stroke();
    ctx.fillStyle='#4f8cff';
    ctx.beginPath(); ctx.arc(px, py, 3.2, 0, Math.PI*2); ctx.fill();
  }
  // hover
  cv._rows=rows; cv._xf=x; cv._yf=yN; cv._type='eq';
}
function fmtPct(v){ return (v>=0?'+':'')+fmt(v,2)+'%'; }

// ---------- K线 · 技术指标 ----------
const ks={bars:[],winStart:0,winLen:110,ind:'macd'};
async function loadK(sym){
  const code=(sym||document.getElementById('kSymInput').value||'600519').trim();
  if(!/^\d{6}$/.test(code.split('.')[0])){ return; }
  try{
    const d=await fetch('/api/kline?sym='+encodeURIComponent(code)+'&days=260').then(r=>r.json());
    if(!d.ok){ const t=document.getElementById('kTip'); if(t)t.textContent=d.error||'K线加载失败'; return; }
    ks.bars=d.bars; ks.winLen=Math.min(ks.winLen,d.bars.length);
    ks.winStart=Math.max(0,d.bars.length-ks.winLen);
    drawK(-1);
  }catch(e){ const t=document.getElementById('kTip'); if(t)t.textContent='K线加载失败: '+e; }
}
function drawK(hoverIdx){
  const cv=document.getElementById('kCanvas'); if(!cv||!ks.bars.length){return;}
  const ctx=cv.getContext('2d'); const W=cv.width,H=cv.height;
  ctx.clearRect(0,0,W,H);
  const bars=ks.bars.slice(ks.winStart, ks.winStart+ks.winLen);
  if(!bars.length) return;
  const L=70,R=16,T=12,B=20;
  const topH=Math.round((H-T-B)*0.52), volTop=T+topH+6, volH=Math.round((H-T-B)*0.14),
        indTop=volTop+volH+8, indH=H-B-indTop-4;
  // 主区
  const hs=bars.map(b=>b.h).filter(v=>v!=null), ls=bars.map(b=>b.l).filter(v=>v!=null);
  let mn=Math.min.apply(0,ls), mx=Math.max.apply(0,hs);
  const [lo,hi]=niceMinMax(mn,mx,12);
  const cw=(W-L-R)/bars.length, bw=Math.max(1,Math.min(10,cw*0.62));
  const x=i=>L+i*cw+cw/2, yP=v=>T+(1-(v-lo)/(hi-lo))*topH;
  // grid & price axis
  for(let i=0;i<=6;i++){ const v=lo+(hi-lo)*i/6; const yy=yP(v);
    ctx.strokeStyle=GRID; ctx.beginPath(); ctx.moveTo(L,yy); ctx.lineTo(W-R,yy); ctx.stroke();
    axisLabel(ctx,L-6,yy+3,v.toFixed(2),'right'); }
  // candles
  bars.forEach((b,i)=>{
    if(b.o==null||b.c==null) return;
    const up=b.c>=b.o, col=up?UP:DOWN;
    const yy=yP;
    ctx.strokeStyle=col; ctx.fillStyle=col;
    ctx.beginPath(); ctx.moveTo(x(i),yP(b.h)); ctx.lineTo(x(i),yP(b.l)); ctx.stroke();
    const yO=yP(b.o),yC=yP(b.c),yy0=Math.min(yO,yC),hh=Math.max(1,Math.abs(yO-yC));
    ctx.fillRect(x(i)-bw/2, yy0, bw, hh);
  });
  // MA lines (主区)
  for(const k of ['ma5','ma10','ma20','ma60']){
    const col=MA_COL[k]; ctx.strokeStyle=col; ctx.lineWidth=1.1; ctx.beginPath(); let started=false;
    bars.forEach((b,i)=>{ const v=b[k]; if(v==null){started=false;return;} const px=x(i),py=yP(v);
      if(!started){ctx.moveTo(px,py);started=true;} else ctx.lineTo(px,py); });
    ctx.stroke();
  }
  // 成交量
  let vmax=0; bars.forEach(b=>{ if(b.v!=null && b.v>vmax) vmax=b.v; });
  bars.forEach((b,i)=>{
    if(b.o==null||b.c==null) return;
    const up=b.c>=b.o; const col=up?UP:DOWN;
    const hh=b.v!=null? (b.v/vmax)*volH : 0;
    ctx.fillStyle=up?col:col;
    ctx.globalAlpha=.55; ctx.fillRect(x(i)-bw/2, volTop+volH-hh, bw, hh); ctx.globalAlpha=1;
  });
  axisLabel(ctx,L,volTop-4,'量','left');
  // 副图指标
  axisLabel(ctx,L,indTop-4,ks.ind.toUpperCase(),'left');
  if(ks.ind==='macd'){
    const vs=bars.map(b=>[b.dif,b.dea,b.macd]).flat().filter(v=>v!=null);
    let mi=Math.min.apply(0,vs), ma=Math.max.apply(0,vs); const pad=(ma-mi)*.1||1; mi-=pad; ma+=pad;
    const yy=v=>indTop+indH-(v-mi)/(ma-mi)*indH;
    ctx.strokeStyle=GRID; ctx.beginPath(); ctx.moveTo(L,yy(0)); ctx.lineTo(W-R,yy(0)); ctx.stroke();
    bars.forEach((b,i)=>{ if(b.macd==null)return; const c=b.macd>=0?UP:DOWN;
      ctx.fillStyle=c; const y0=yy(0),ym=yy(b.macd); ctx.fillRect(x(i)-bw/2,Math.min(y0,ym),bw,Math.max(1,Math.abs(ym-y0))); });
    for(const k of ['dif','dea']){ const col=k==='dif'?'#f0b400':'#4f8cff'; ctx.strokeStyle=col; ctx.lineWidth=1.1;
      ctx.beginPath(); let s=false; bars.forEach((b,i)=>{ const v=b[k]; if(v==null){s=false;return;} const px=x(i),py=yy(v); if(!s){ctx.moveTo(px,py);s=true;} else ctx.lineTo(px,py); }); ctx.stroke(); }
  } else { // rsi
    const yy=v=>indTop+(1-v/100)*indH;
    ctx.strokeStyle=GRID; [30,70].forEach(v=>{ ctx.beginPath(); ctx.moveTo(L,yy(v)); ctx.lineTo(W-R,yy(v)); ctx.stroke(); });
    ctx.strokeStyle='#4f8cff'; ctx.lineWidth=1.3; ctx.beginPath(); let s=false;
    bars.forEach((b,i)=>{ const v=b.rsi; if(v==null){s=false;return;} const px=x(i),py=yy(v); if(!s){ctx.moveTo(px,py);s=true;} else ctx.lineTo(px,py); }); ctx.stroke();
  }
  // x 轴日期
  const step=Math.max(1,Math.floor(bars.length/8));
  for(let i=0;i<bars.length;i+=step) axisLabel(ctx,x(i),H-6,bars[i].date.slice(2),'center');
  // 图例
  let leg='<span style="color:#8b98b3">MA5</span> ';
  for(const k of ['ma5','ma10','ma20','ma60']) leg+='<span style="color:'+MA_COL[k]+'">'+k.toUpperCase()+'</span> ';
  ctx.fillStyle='#e6edf7'; ctx.font='11px sans-serif'; ctx.fillText('MA5 MA10 MA20 MA60', L+4, T+2);
  // 十字光标 (hover)
  if(hoverIdx>=0 && hoverIdx<bars.length){
    const px=x(hoverIdx), b=bars[hoverIdx];
    ctx.strokeStyle='rgba(255,255,255,.4)'; ctx.lineWidth=1;
    ctx.beginPath(); ctx.moveTo(px, T); ctx.lineTo(px, H-B); ctx.stroke();
    if(b.c!=null){ const py=yP(b.c);
      ctx.beginPath(); ctx.moveTo(L, py); ctx.lineTo(W-R, py); ctx.stroke(); }
  }
  cv._bars=bars; cv._x=x; cv._info={
    lo,hi,topH,T,L,R,W,B, top:topH, volTop,volTop2:volTop+volH, indTop, indH, indBottom:indTop+indH
  };
}
function kTipText(b){
  if(!b) return '';
  const chg = (b.c!=null && b.o!=null && b.o!==0)? (b.c/b.o-1)*100 : null;
  return esc(b.date)+'  O '+fmt(b.o,2)+'  H '+fmt(b.h,2)+'  L '+fmt(b.l,2)+'  C '+fmt(b.c,2)
    +'  涨跌 '+(chg==null?'—':'<b style="color:'+(chg>=0?UP:DOWN)+'">'+fmtPct(chg)+'</b>')
    +'  量 '+fmt((b.v||0)/1e6,2)+'M  MA20 '+fmt(b.ma20,2)
    +'  DIF '+fmt(b.dif,3)+' DEA '+fmt(b.dea,3)+' MACD '+fmt(b.macd,3)+' RSI '+fmt(b.rsi,1);
}
function bindDeep(){
  const eq=document.getElementById('eqCanvas'), kv=document.getElementById('kCanvas');
  const eqTip=document.getElementById('eqTip'), kTip=document.getElementById('kTip');
  const ratio=cv=>{const r=cv.getBoundingClientRect(); return r.width?cv.width/r.width:1;};
  if(eq){
    eq.addEventListener('mousemove',e=>{
      const r=eq.getBoundingClientRect(), rx=(e.clientX-r.left)*ratio(eq);
      if(!eq._rows||!eq._rows.length) return; const rows=eq._rows, xf=eq._xf;
      const i=Math.round((rx-xf(0))/(xf(rows.length-1)-xf(0))*(rows.length-1));
      if(i<0||i>=rows.length) return; const row=rows[i];
      drawEq(ksRange, i);  // 重绘带十字
      eqTip.innerHTML='<span style="color:#4f8cff">'+esc(row.day)+'</span> 权益 '+fmt(row.equity,2)
        +'  净值 '+fmt(row.nav,4)+'  回撤 <b style="color:'+(row.dd<0?UP:AXIS)+'">'+fmt(row.dd,2)+'%</b>';
    });
    eq.addEventListener('mouseleave',()=>{ drawEq(ksRange, -1); });
  }
  if(kv){
    kv.addEventListener('mousemove',e=>{
      const r=kv.getBoundingClientRect(), rx=(e.clientX-r.left)*ratio(kv);
      const bars=kv._bars, xf=kv._x; if(!bars||!bars.length) return;
      const i=Math.round((rx-xf(0))/(xf(bars.length-1)-xf(0))*(bars.length-1));
      if(i<0||i>=bars.length) return;
      drawK(i);
      kTip.innerHTML=kTipText(bars[i]);
    });
    kv.addEventListener('mouseleave',()=>{ drawK(-1); kTip.innerHTML=''; });
  }
  const one=(id,fn)=>{const el=document.getElementById(id); if(el) el.addEventListener('click',fn);};
  one('kPrev',()=>{ ks.winStart=Math.max(0,ks.winStart-Math.round(ks.winLen*0.8)); drawK(-1); });
  one('kNext',()=>{ ks.winStart=Math.min(Math.max(0,ks.bars.length-ks.winLen),ks.winStart+Math.round(ks.winLen*0.8)); drawK(-1); });
  one('kZoomIn',()=>{ ks.winLen=Math.max(20,Math.floor(ks.winLen*0.7)); ks.winStart=Math.min(ks.winStart,Math.max(0,ks.bars.length-ks.winLen)); drawK(-1); });
  one('kZoomOut',()=>{ ks.winLen=Math.min(ks.bars.length,Math.ceil(ks.winLen*1.4)); ks.winStart=Math.min(ks.winStart,Math.max(0,ks.bars.length-ks.winLen)); drawK(-1); });
  one('kGo',()=>loadK(''));
  const kSel=document.getElementById('kSymSel'), kInd=document.getElementById('kInd');
  if(kSel) kSel.addEventListener('change',()=>loadK(kSel.value));
  if(kInd) kInd.addEventListener('change',()=>{ ks.ind=kInd.value; drawK(-1); });
  const indInput=document.getElementById('kSymInput');
  if(indInput) indInput.addEventListener('keydown',e=>{ if(e.key==='Enter') loadK(''); });
  const rangeBtns=document.querySelectorAll('#eqRange button');
  rangeBtns.forEach(b=>b.addEventListener('click',()=>{
    rangeBtns.forEach(x=>x.style.opacity=x===b?1:.5);
    ksRange=b.dataset.r; drawEq(ksRange);
  }));
  one('btnExport', exportMd);
}
async function loadHolds(){
  let st=window._lastState;
  try{
    if(!st){ st=await fetch('/api/live').then(r=>r.json()); }
    const pos=(st.positions||[]).filter(p=>p&&p.qty>0);
    const box=document.getElementById('holdPie'), bar=document.getElementById('holdBar');
    if(!pos.length){ if(box)box.innerHTML='<div class="muted">暂无持仓</div>'; if(bar)bar.innerHTML=''; return; }
    const tot=pos.reduce((a,p)=>a+(p.mv||0),0);
    if(box){
      let ang=-Math.PI/2, svg='<svg viewBox="0 0 200 200" style="max-width:210px;display:block;margin:0 auto">';
      const pal=['#4f8cff','#26d0ce','#f0b400','#c678dd','#ff6b6b','#3dd68c','#f88c4f'];
      pos.forEach((p,i)=>{
        const frac=(p.mv||0)/tot, a2=ang+frac*Math.PI*2;
        const x1=100+88*Math.cos(ang), y1=100+88*Math.sin(ang), x2=100+88*Math.cos(a2), y2=100+88*Math.sin(a2);
        svg+='<path d="M100 100 L'+x1.toFixed(1)+' '+y1.toFixed(1)+' A88 88 0 '+(frac>0.5?1:0)+' 1 '+x2.toFixed(1)+' '+y2.toFixed(1)+' Z" fill="'+pal[i%pal.length]+'" opacity=".85"><title>'+esc(p.canon)+' '+(frac*100).toFixed(1)+'%</title></path>';
        ang=a2;
      });
      svg+='</svg>';
      box.innerHTML=svg+'<div style="text-align:center;font-size:11px;color:#8b98b3;margin-top:4px">持仓市值 '+fmt(tot,0)+' (共'+pos.length+'只)</div>';
    }
    if(bar){
      pos.sort((a,b)=>(b.pnl_amt||0)-(a.pnl_amt||0));
      bar.innerHTML=pos.map(p=>{
        const w=Math.max(4,Math.min(100,Math.abs((p.pnl_amt||0)/Math.max(1,Math.max.apply(0,pos.map(x=>Math.abs(x.pnl_amt||0))))*100)));
        const c=(p.pnl_amt||0)>=0?UP:DOWN;
        return '<div style="margin:5px 0"><div style="display:flex;justify-content:space-between;font-size:12px"><span>'+esc(p.name||p.canon)+'</span><span style="color:'+c+'">'+fmt(p.pnl_amt||0,0)+' ('+fmt(p.pnl_pct||0,2)+'%)</span></div>'
          +'<div style="background:rgba(255,255,255,.08);height:6px;border-radius:3px"><div style="background:'+c+';width:'+w+'%;height:6px;border-radius:3px"></div></div></div>';
      }).join('');
    }
  }catch(e){ /* 忽略 */ }
}
async function loadProfile(){
  const indBox=document.getElementById('holdInd'), capBox=document.getElementById('holdCap');
  if(!indBox&&!capBox) return;
  const none=b=>{ if(b) b.innerHTML='<div class="muted">暂无持仓</div>'; };
  try{
    const d=await fetch('/api/holdings_profile').then(r=>r.json());
    if(!d.ok||!d.positions||!d.positions.length){ none(indBox); none(capBox); return; }
    if(indBox){
      const inds=d.industries||[];
      if(!inds.length){ indBox.innerHTML='<div class="muted">无行业映射</div>'; }
      else indBox.innerHTML=inds.map(x=>{
        return '<div style="margin:7px 0"><div style="display:flex;justify-content:space-between;font-size:12px">'
          +'<span>'+esc(x.name)+' <span class="muted" style="font-size:11px">'+esc(x.codes.join(' '))+'</span></span>'
          +'<span>'+fmt(x.weight_pct,1)+'%</span></div>'
          +'<div style="background:rgba(255,255,255,.08);height:8px;border-radius:4px">'
          +'<div style="width:'+Math.max(2,Math.min(100,x.weight_pct))+'%;height:8px;border-radius:4px;background:#4f8cff"></div></div></div>';
      }).join('');
    }
    if(capBox){
      const caps=(d.caps||[]).filter(c=>c.count>0);
      const pal={大盘:'#c678dd',中盘:'#f0b400',小盘:'#26d0ce'};
      if(!caps.length){ capBox.innerHTML='<div class="muted">市值数据缺失(估值快照滞后)</div>'; }
      else capBox.innerHTML=caps.map(c=>{
        const sub=(c.positions||[]).map(p=>p.canon.split('.')[0]).join(' ');
        return '<div style="margin:7px 0"><div style="display:flex;justify-content:space-between;font-size:12px">'
          +'<span style="color:'+pal[c.bucket]+'">'+c.bucket+'</span><span>'+c.count+' 只 · '+fmt(c.weight_pct,1)+'%</span></div>'
          +'<div style="background:rgba(255,255,255,.08);height:8px;border-radius:4px">'
          +'<div style="width:'+Math.max(2,Math.min(100,c.weight_pct))+'%;height:8px;border-radius:4px;background:'+pal[c.bucket]+'"></div></div>'
          +'<div class="muted" style="font-size:11px">'+esc(sub)+'</div></div>';
      }).join('');
    }
  }catch(e){ const b=document.getElementById('holdInd'); if(b) b.innerHTML='<div class="muted">加载失败</div>'; }
}
async function loadMonthly(){
  const box=document.getElementById('mHeat'); if(!box)return;
  try{
    const d=await fetch('/api/monthly_returns').then(r=>r.json());
    if(!d.ok||!d.cells){ box.innerHTML='<div class="muted">回执样本不足(需跨月数据)</div>'; return; }
    const months=Object.keys(d.cells).sort();
    let html='<table class="tbl"><thead><tr><th>月份</th><th>收益</th><th>色阶</th></tr></thead><tbody>';
    months.forEach(m=>{
      const v=d.cells[m]; const abs=Math.min(2,Math.abs(v||0)/2);
      const col=v>=0?'rgba(255,80,80,'+(0.15+abs*0.75)+')':'rgba(80,220,140,'+(0.15+abs*0.75)+')';
      html+='<tr><td>'+esc(m)+'</td><td style="color:'+(v>=0?UP:DOWN)+'">'+fmtPct(v)+'</td>'
        +'<td style="background:'+col+'"></td></tr>';
    });
    html+='</tbody></table>';
    box.innerHTML=html;
  }catch(e){ box.innerHTML='<div class="muted">加载失败</div>'; }
}
async function exportMd(){
  try{
    const [live,perf,ov]=await Promise.all([
      fetch('/api/live').then(r=>r.json()).catch(()=>null),
      fetch('/api/perf').then(r=>r.json()).catch(()=>null),
      fetch('/api/overview').then(r=>r.json()).catch(()=>null)]);
    const C=(live&&live.capital)||{};
    const rows=(eqCache&&eqCache.rows)||[];
    const nav=(rows.length?rows[rows.length-1].nav:null);
    const dds=rows.map(r=>r.dd||0);
    const md=[];
    md.push('# A股轮动 · 模拟盘复盘', '');
    md.push('生成时间: '+new Date().toLocaleString('zh-CN'), '');
    md.push('## 账户概览');
    md.push('| 权益 | 现金 | 仓位 | 持仓 | 累计收益 |');
    md.push('|---|---|---|---|---|');
    md.push('| '+fmt(C.equity,2)+' | '+fmt(C.cash,2)+' | '+(C.cash_ratio!=null?fmt((1-C.cash_ratio)*100,1)+'%':'—')+' | '+(C.open_positions||0)+' 只 | '+(C.total_pnl_pct!=null?fmtPct(C.total_pnl_pct):'—')+' |','');
    md.push('## 持仓');
    const pos=(live&&live.positions||[]).filter(p=>p&&p.qty>0);
    if(pos.length){
      md.push('| 代码 | 数量 | 成本 | 现价 | 浮盈 | 仓位 |');
      md.push('|---|---|---|---|---|---|');
      pos.forEach(p=>md.push('| '+esc(p.canon)+' | '+p.qty+' | '+fmt(p.avg_cost,3)+' | '+fmt(p.last_price,2)+' | '+fmt(p.pnl_amt,1)+' ('+fmt(p.pnl_pct,2)+'%) | '+fmt((p.weight||0)*100,1)+'% |'));
    } else md.push('(空仓)');
    md.push('','## 绩效');
    if(perf&&perf.ok!==false&&perf.metrics){
      const m=perf.metrics;
      md.push('| 累计收益 | 年化 | 夏普 | 最大回撤 |');
      md.push('|---|---|---|---|');
      md.push('| '+(m.total_return!=null?fmtPct(m.total_return):'—')+' | '+(m.cagr!=null?fmt(m.cagr,2)+'%':'—')+' | '+(m.sharpe_annual!=null?fmt(m.sharpe_annual,2):'—')+' | '+(m.max_drawdown!=null?fmt(m.max_drawdown,2)+'%':'—')+' |');
    } else {
      md.push('最新净值: '+(nav?fmt(nav,4):'—')+' | 当前回撤: '+fmtPct(dds.length?dds[dds.length-1]:0));
    }
    md.push('','## 门控与系统');
    const g=(ov&&ov.gate)||{};
    md.push('- IC 门控: '+(g.regime||'—')+' | 暴露 ×'+(g.exposure_mult!=null?g.exposure_mult:1)+' | IC均值 '+fmt(g.ic_mean,4)+(g.ic_as_of?' (as_of '+esc(g.ic_as_of)+')':''));
    const dt=(ov&&ov.data)||{};
    if(dt.stale) md.push('- 数据滞后表: '+(dt.stale.length?dt.stale.join(', '):'无'));
    if(g.reasons&&g.reasons.length) md.push('- 门控原因: '+g.reasons.join('; '));
    md.push('','*本报告由系统自动生成, 仅供研究复盘, 不构成投资建议.*');
    const blob=new Blob(['\ufeff'+md.join('\n')],{type:'text/markdown;charset=utf-8'});
    const a=document.createElement('a');
    a.href=URL.createObjectURL(blob); a.download='复盘_'+new Date().toISOString().slice(0,10)+'.md';
    document.body.appendChild(a); a.click(); setTimeout(()=>{URL.revokeObjectURL(a.href);a.remove();},300);
  }catch(e){ if(typeof console!=='undefined') console.error('export err',e); }
}
function initDeep(){
  bindDeep();
  loadEq(); loadMonthly(); loadHolds(); loadProfile();
  // K线初始标的: 持仓第一只, 否则固定示例
  const st=window._lastState||{};
  const opts=(st.positions||[]).filter(p=>p.qty>0).map(p=>p.canon);
  const tgt=(st.targets||st.top_targets||[]).map(t=>typeof t==='string'?t:(t&&t.canon));
  const all=[...new Set([...(opts||[]),...(tgt||[]).filter(Boolean)])].slice(0,12);
  const sel=document.getElementById('kSymSel');
  if(sel){
    sel.innerHTML=all.map(s=>'<option value="'+esc(s)+'">'+esc(s)+'</option>').join('')
      +'<option value="600519">600519 贵州茅台</option><option value="000001">000001 平安银行</option>';
    if(!all.length) sel.innerHTML='<option value="600519">600519 贵州茅台</option>';
  }
  loadK(all[0]||'600519');
}
// ===================== 风控告警 (riskview) =====================
let rvLoaded=false, _pushBound=false;
function kpiCard(label, val, color){
  return '<div style="background:hsl(var(--elevated));border:1px solid hsl(var(--border));border-radius:var(--radius-sm);padding:10px 12px">'
    +'<div class="muted" style="font-size:11px;margin-bottom:3px">'+esc(label)+'</div>'
    +'<div style="font-size:19px;font-weight:600;color:'+(color||'#e6edf7')+'">'+val+'</div></div>';
}
async function loadRiskview(){
  const box=document.getElementById('riskKpis'); if(!box) return;
  try{
    const [rk,al]=await Promise.all([
      fetch('/api/riskops').then(r=>r.json()),
      fetch('/api/alerts').then(r=>r.json())]);
    const m=rk.metrics||{};
    const fmtp=v=>v==null?'—':fmtPct(v);
    const fmtn=v=>v==null?'—':fmt(v,2);
    const up=v=>v!=null&&v>=0?'#3dd68c':'#ff6b6b';
    let html='';
    html+=kpiCard('今日盈亏', fmtp(m.daily_pnl_pct), up(m.daily_pnl_pct));
    html+=kpiCard('今日盈亏额', m.daily_pnl!=null?fmt(m.daily_pnl,0):'—', up(m.daily_pnl));
    html+=kpiCard('VaR95 (日)', fmtp(m.var95), '#f0b400');
    html+=kpiCard('VaR99 (日)', fmtp(m.var99), '#ff6b6b');
    html+=kpiCard('滚动Sharpe(60日)', fmtn(m.sharpe_w60), m.sharpe_w60!=null&&m.sharpe_w60>=0?'#3dd68c':'#ff6b6b');
    html+=kpiCard('Sharpe(近20日)', fmtn(m.sharpe_w20), m.sharpe_w20!=null&&m.sharpe_w20>=0?'#3dd68c':'#ff6b6b');
    html+=kpiCard('Sortino(年化)', fmtn(m.sortino), '#e6edf7');
    html+=kpiCard('年化波动', m.vol_annual!=null?fmt(m.vol_annual,2)+'%':'—', '#e6edf7');
    html+=kpiCard('最大回撤', m.max_dd!=null?fmt(m.max_dd,2)+'%':'—', '#ff6b6b');
    html+=kpiCard('胜率', m.win_rate!=null?fmt(m.win_rate,1)+'%':'—', '#e6edf7');
    html+=kpiCard('盈亏比', fmtn(m.pl_ratio), '#e6edf7');
    html+=kpiCard('持仓敞口/杠杆', (m.exposure_pct!=null?fmt(m.exposure_pct,1)+'%':'—')+'<br><span style="font-size:11px;color:#8b98b3">现货无融资 · 杠杆 ×1.0</span>', '#e6edf7');
    const op=rk.ops||{};
    html+=kpiCard('错误计数(24h)', op.err_24h!=null?op.err_24h:'—', (op.err_24h||0)>0?'#ff6b6b':'#3dd68c');
    const tt=op.today_trades||{};
    html+=kpiCard('今日成交', (tt.buy!=null?('买'+tt.buy+' 卖'+tt.sell):'—')+(tt.fee?' (费'+tt.fee+')':''), '#e6edf7');
    const tm=op.tick_ms;
    html+=kpiCard('引擎tick延迟(ms)', tm&&tm.p50!=null
      ?('p50 '+fmt(tm.p50,0)+' · p90 '+fmt(tm.p90,0)+' · p95 '+fmt(tm.p95,0)+' · p99 '+fmt(tm.p99,0)+'<br><span style="font-size:11px;color:#8b98b3">最近 '+fmt(tm.last,0)+' ms · n='+tm.n+'</span>')
      :'— (引擎重启后生效)', '#e6edf7');
    box.innerHTML=html;
    const note=document.getElementById('riskNote');
    if(note) note.innerHTML=(rk.note?('⚠ '+esc(rk.note)+' '):'')+'(回执 '+(rk.n_days||0)+' 个交易日)';
    // 告警
    const ab=document.getElementById('alertBox'); if(ab){
      const al_=al.alerts||[];
      if(!al_.length){ ab.innerHTML='<div style="color:#3dd68c">● 无活跃告警 · 系统运行正常</div>'; }
      else{
        ab.innerHTML=al_.map(a=>{
          const c=a.level==='critical'?'#ff6b6b':(a.level==='warn'?'#f0b400':'#e6edf7');
          return '<div style="display:flex;gap:8px;padding:5px 0;border-bottom:1px dashed rgba(255,255,255,.08);align-items:center">'
            +'<span style="color:'+c+';flex:0 0 52px;font-size:11px">['+a.level.toUpperCase()+']</span>'
            +'<b style="flex:0 0 130px">'+esc(a.rule)+'</b><span class="muted" style="flex:1">'+esc(a.detail)+'</span>'
            +'<span class="muted" style="font-size:11px">'+esc(a.ts)+'</span></div>';
        }).join('');
      }
    }
    window._alertsNow=al;
  }catch(e){ box.innerHTML='<div class="muted">加载失败: '+esc(String(e).slice(0,80))+'</div>'; }
}
async function sendPush(channel, msg){
  const box=document.getElementById('pushResult'); if(box) box.innerHTML='发送中...';
  try{
    const r=await fetch('/api/push',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({channel:channel,message:msg})}).then(r=>r.json());
    if(box) box.innerHTML=r.ok?('✅ 已发送 ('+(r.status||r.body||'')+')')
      :('<b style="color:#f66">发送失败:</b> '+esc(r.error||''));
  }catch(e){ if(box) box.innerHTML='<b style="color:#f66">请求失败:</b> '+esc(String(e).slice(0,120)); }
}
function bindPush(){
  if(_pushBound) return; _pushBound=true;
  const ch=()=>{const s=document.getElementById('pushChannel'); return s?s.value:'dingtalk';};
  const bt=document.getElementById('btnPushTest'), bn=document.getElementById('btnPushNow');
  if(bt) bt.addEventListener('click',()=>sendPush(ch(),'【测试】A股轮动仪表台告警通道测试 '+new Date().toLocaleString('zh-CN')));
  if(bn) bn.addEventListener('click',()=>{
    const al=(window._alertsNow&&window._alertsNow.alerts)||[];
    const msg = al.length?('【A股轮动告警】当前 '+al.length+' 条:\n'+al.map(a=>'['+a.level+'] '+a.rule+' - '+a.detail).join('\n'))
      :('【A股轮动】当前无活跃告警, 系统运行正常。');
    sendPush(ch(), msg);
  });
}
// ===================== 因子实验室 (flab) =====================
let flLoaded=false, flCache=null, flHidden={}, flRange=120, flHorizon='h5';
const FCOL={vol:'#f0b400',mom_20:'#4f8cff',reversal:'#c678dd'};
async function loadF(){
  const cv=document.getElementById('flabCanvas'); if(!cv) return;
  try{
    const d=await fetch('/api/factor_lab?days='+flRange).then(r=>r.json());
    flCache=d;
    // stats 文本 + legend
    const st=document.getElementById('flabStats');
    if(st){
      let html='';
      (d.factors||[]).forEach(f=>{
        const s=(f.stats||{})[flHorizon]||{};
        html+='<span style="font-size:12px;cursor:pointer;opacity:'+(flHidden[f.key]?'.35':'1')+'" data-f="'+f.key+'" class="flLeg" '
          +'style2="">'
          +'<b style="color:'+FCOL[f.key]+'">'+esc(f.zh)+'</b> '
          +'近IC '+(s.last!=null?fmt(s.last,3):'—')+' | 均值 '+((s.mean!=null)?fmt(s.mean,3):'—')
          +' | 胜率 '+((s.win!=null)?fmt(s.win,0)+'%':'—')+'</span>';
      });
      st.innerHTML=html;
      st.querySelectorAll('.flLeg').forEach(el=>el.addEventListener('click',()=>{
        const k=el.dataset.f; flHidden[k]=!flHidden[k]; loadF(); }));
    }
    // quantiles
    const qb=document.getElementById('flabQ');
    if(qb){
      const qs=d.quantiles||[];
      if(!qs.length) qb.innerHTML='<div class="muted">五分位数据不可用(视图最新日)</div>';
      else{
        let h='<table class="tbl"><thead><tr><th>因子</th><th>Q1(低)</th><th>Q2</th><th>Q3</th><th>Q4</th><th>Q5(高)</th></tr></thead><tbody>';
        qs.forEach(f=>{
          const cells=[0,1,2,3,4].map(qi=>{
            const b=f.bins.find(x=>x.q===qi);
            if(!b) return '<td>—</td>';
            const col=b.mean>=0?'rgba(61,214,140,'+(0.15+Math.min(.55,Math.abs(b.mean))*0.5)+')'
                            :'rgba(255,107,107,'+(0.15+Math.min(.55,Math.abs(b.mean))*0.5)+')';
            return '<td style="background:'+col+'">'+fmt(b.mean,2)+'<br><span class="muted" style="font-size:10px">n='+b.n+' ['+fmt(b.lo,2)+','+fmt(b.hi,2)+']</span></td>';
          });
          h+='<tr><td>'+esc(f.zh)+'<br><span class="muted" style="font-size:10px">'+esc(String(f.date))+'</span></td>'+cells.join('')+'</tr>';
        });
        h+='</tbody></table>';
        qb.innerHTML=h;
      }
    }
    drawF(-1);
  }catch(e){ const t=document.getElementById('flabTip'); if(t)t.textContent='因子数据加载失败: '+e; }
}
function drawF(hoverIdx){
  const cv=document.getElementById('flabCanvas'); if(!cv||!flCache) return;
  const ctx=cv.getContext('2d'); const W=cv.width,H=cv.height;
  ctx.clearRect(0,0,W,H);
  const L=70,R=16,T=14,B=22;
  const data=[];
  (flCache.factors||[]).forEach(f=>{ if(!flHidden[f.key]) data.push({f:f,h:flHorizon}); });
  if(!data.length){ return; }
  const all=[];
  data.forEach(d=>d.f.series.forEach(s=>{const v=s[d.h]; if(v!=null) all.push(v);}));
  if(!all.length) return;
  let lo=Math.min.apply(0,all), hi=Math.max.apply(0,all); const pad=(hi-lo)*.1||.05; lo-=pad; hi+=pad;
  const series=data[0].f.series;
  const x=i=>L+i*(W-L-R)/(series.length-1), y=v=>T+(1-(v-lo)/(hi-lo))*(H-B-T);
  ctx.strokeStyle=GRID; [0].forEach(v=>{ const yy=y(v); ctx.beginPath(); ctx.moveTo(L,yy); ctx.lineTo(W-R,yy); ctx.stroke(); });
  for(let i=0;i<=5;i++){ const v=lo+(hi-lo)*i/5; const yy=y(v);
    ctx.strokeStyle=GRID; ctx.beginPath(); ctx.moveTo(L,yy); ctx.lineTo(W-R,yy); ctx.stroke();
    axisLabel(ctx,L-6,yy+3,v.toFixed(2),'right'); }
  const step=Math.max(1,Math.floor(series.length/8));
  for(let i=0;i<series.length;i+=step) axisLabel(ctx,x(i),H-4,series[i].d.slice(2),'center');
  data.forEach(d=>{
    const col=FCOL[d.f.key]||'#4f8cff'; ctx.strokeStyle=col; ctx.lineWidth=1.4; ctx.beginPath();
    let started=false;
    d.f.series.forEach((s,i)=>{ const v=s[d.h]; if(v==null){started=false;return;}
      const px=x(i),py=y(v); if(!started){ctx.moveTo(px,py);started=true;} else ctx.lineTo(px,py); });
    ctx.stroke();
  });
  ctx.fillStyle='#e6edf7'; ctx.font='11px sans-serif';
  ctx.fillText(data.map(d=>d.f.zh+'('+d.h.toUpperCase()+')').join('  '), L+4, T+2);
  if(hoverIdx>=0 && hoverIdx<series.length){
    const px=x(hoverIdx);
    ctx.strokeStyle='rgba(255,255,255,.4)'; ctx.beginPath(); ctx.moveTo(px,T); ctx.lineTo(px,H-B); ctx.stroke();
  }
  cv._fs=series; cv._fx=x; cv._fd=data;
}
function flTipText(i){
  if(!flCache) return '';
  const series=flCache.factors[0].series; if(i<0||i>=series.length) return '';
  let t='<span style="color:#4f8cff">'+esc(series[i].d)+'</span>';
  flCache.factors.forEach(f=>{ const s=f.series[i]; const v=s?s[flHorizon]:null;
    if(v!=null) t+='  <span style="color:'+FCOL[f.key]+'">'+esc(f.zh)+' '+fmt(v,4)+'</span>'; });
  return t;
}
function bindFlab(){
  const cv=document.getElementById('flabCanvas'), tip=document.getElementById('flabTip');
  const ratio=cv2=>{const r=cv2.getBoundingClientRect(); return r.width?cv2.width/r.width:1;};
  if(cv) cv.addEventListener('mousemove',e=>{
    const r=cv.getBoundingClientRect(), rx=(e.clientX-r.left)*ratio(cv);
    const s=cv._fs, xf=cv._fx; if(!s||!s.length) return;
    const i=Math.round((rx-xf(0))/(xf(s.length-1)-xf(0))*(s.length-1));
    drawF(i); if(tip) tip.innerHTML=flTipText(i);
  });
  const hz=document.getElementById('flabHorizon'), rg=document.getElementById('flabRange');
  if(hz) hz.addEventListener('change',()=>{ flHorizon=hz.value; loadF(); });
  if(rg) rg.addEventListener('change',()=>{ flRange=parseInt(rg.value,10)||120; loadF(); });
}
function initRiskview(){ bindPush(); loadRiskview(); setInterval(loadRiskview,15000); }
function initF(){ bindFlab(); loadF(); }
function switchTab(name){
  document.querySelectorAll('.tabBtn').forEach(b=>b.classList.toggle('active', b.dataset.tab===name));
  document.querySelectorAll('.tabView').forEach(v=>v.classList.toggle('active', v.id==='view-'+name));
  if(name==='market' && !mkLoaded){ mkLoaded=true; loadMarket(); }
  if(name==='perf' && !pfLoaded){ pfLoaded=true; loadPerf(); loadTargetPlan(); loadHealth(); loadViews(); }
  if(name==='dashboard' && !mbLoaded){ mbLoaded=true; loadMarketBoard(); }
  if(name==='backtest' && !btLoaded){ btLoaded=true; loadBacktestHistory(); }
  if(name==='concept' && !cptLoaded){
    cptLoaded=true;
    // 绑定控件事件 (仅首次)
    const cSearch = document.getElementById('conceptSearch');
    const cSort = document.getElementById('conceptSort');
    const cTop = document.getElementById('conceptTop');
    const cRef = document.getElementById('conceptRefresh');
    if(cSearch){ cSearch.addEventListener('input', debounce(loadConcept, 300)); }
    if(cSort){ cSort.addEventListener('change', loadConcept); }
    if(cTop){ cTop.addEventListener('change', loadConcept); }
    if(cRef){ cRef.addEventListener('click', loadConcept); }
    loadConcept();
  }
  if(name==='industry' && !indLoaded){
    indLoaded=true;
    const iSearch = document.getElementById('industrySearch');
    const iSort = document.getElementById('industrySort');
    const iTop = document.getElementById('industryTop');
    const iRef = document.getElementById('industryRefresh');
    if(iSearch){ iSearch.addEventListener('input', debounce(loadIndustry, 300)); }
    if(iSort){ iSort.addEventListener('change', loadIndustry); }
    if(iTop){ iTop.addEventListener('change', loadIndustry); }
    if(iRef){ iRef.addEventListener('click', loadIndustry); }
    loadIndustry();
  }
  if(name==='monitor' && !monLoaded){ monLoaded=true; loadMonitor(); }
  if(name==='regime' && !regLoaded){ regLoaded=true; loadRegime(); }
  if(name==='abnormal' && !abnLoaded){ abnLoaded=true; loadAbnormal(); }
  if(name==='dbpanel' && !dbpLoaded){ dbpLoaded=true; loadDbPanel(); }
  if(name==='deep' && !deepLoaded){ deepLoaded=true; initDeep(); }
  if(name==='riskview' && !rvLoaded){ rvLoaded=true; initRiskview(); }
  if(name==='flab' && !flLoaded){ flLoaded=true; initF(); }
  if(name==='pscan' && !psLoaded){ psLoaded=true; initScan(); }
  if(name==='dbmon' && !dbmLoaded){ dbmLoaded=true; initDbmon(); }
}
document.querySelectorAll('.tabBtn').forEach(b=>b.addEventListener('click',()=>switchTab(b.dataset.tab)));

// ===================== DB 监控 (dbmon) =====================
let dbmLoaded=false, _dbmTimer=null;
async function dbmFetch(url, opt){
  const r = await fetch(url, opt||{}); return r.json();
}
function dbmRelAge(v){
  if(!v) return {label:'—', cls:'muted'};
  const s = new Date(v.replace(' ','T')).getTime();
  if(isNaN(s)) return {label:String(v), cls:''};
  const h = (Date.now()-s)/3600000;
  if(h>24) return {label:h.toFixed(0)+'h前', cls:'warnText'};
  return {label:(h<1?Math.round(h*60)+'min前':h.toFixed(1)+'h前'), cls:''};
}
function dbmSvg(points, w, h, color){
  if(!points||points.length<2) return '<span class="muted">—</span>';
  const mn=Math.min(...points), mx=Math.max(...points), rng=(mx-mn)||1;
  const step=w/(points.length-1);
  const d=points.map((p,i)=>{
    const x=i*step, y=h-2-((p-mn)/rng)*(h-6);
    return (i?'L':'M')+x.toFixed(1)+' '+y.toFixed(1);
  }).join(' ');
  return '<svg width="'+w+'" height="'+h+'" style="display:block"><path d="'+d+'" fill="none" stroke="'+(color||'#5aa9ff')+'" stroke-width="1.4"/></svg>';
}
function dbmonStatus(upd){
  if(!upd || upd.running===undefined) return '空闲';
  if(upd.running) return upd.current?('更新中: '+upd.current):'更新中...';
  const fin = upd.finished?upd.finished.slice(11,19):'';
  const okn = upd.detail?Object.values(upd.detail).filter(d=>d&&d.ok).length:0;
  const tot = upd.detail?Object.keys(upd.detail).length:0;
  return '上次完成 '+fin+'  ok '+okn+'/'+tot+(upd.elapsed_s?(' · '+upd.elapsed_s+'s'):'');
}
function dbmonRender(d){
  const last = d.last||{}, hist = d.history||{};
  // 表状态表
  const box=document.getElementById('dbmonTable');
  let html='<table class="tbl"><thead><tr><th>表/文件</th><th>行数</th><th>最后更新</th><th>空值率</th><th>重复对%</th></tr></thead><tbody>';
  Object.keys(last).forEach(nm=>{
    const s=last[nm]||{}, age=dbmRelAge(s.last_day);
    const na=Object.entries(s.na||{}).map(([c,v])=>c+':'+(v==null?'—':(v>10?'<b style="color:#ff6b6b">'+v+'%</b>':v+'%'))).join(' ');
    html+='<tr><td>'+esc(nm)+'</td><td>'+(s.rows==null?'—':fmt(s.rows,0))+'</td>'
      +'<td class="'+age.cls+'">'+esc(age.label)+(s.last_day?'<br><span class="muted" style="font-size:11px">'+esc(s.last_day)+'</span>':'')+'</td>'
      +'<td class="muted" style="font-size:12px">'+(na||'—')+'</td>'
      +'<td>'+(s.dup_pairs!=null?(s.dup_pairs>0?'<b style="color:#f0b400">'+s.dup_pairs+'%</b>':'0%'):'—')+'</td></tr>';
  });
  box.innerHTML=html+'</tbody></table>';
  // 趋势
  const tr=document.getElementById('dbmonTrend');
  let th='';
  Object.keys(hist).forEach(nm=>{
    const pts=hist[nm].map(p=>p.rows).filter(v=>v!=null);
    if(!pts.length) return;
    const cur=pts[pts.length-1];
    th+='<div style="display:flex;gap:10px;align-items:center;padding:3px 0;border-bottom:1px dashed rgba(255,255,255,.06)">'
      +'<span style="flex:0 0 150px;font-size:12px">'+esc(nm)+'</span>'
      +'<span style="flex:0 0 90px;text-align:right;font-size:12px">'+fmt(cur,0)+'</span>'
      +dbmSvg(pts.slice(-60), 160, 26)+'</div>';
  });
  tr.innerHTML=th||'<div class="muted">暂无趋势数据</div>';
  // 质量与告警
  const al=document.getElementById('dbmonAlerts');
  let ah='';
  const now=Date.now();
  Object.keys(last).forEach(nm=>{
    const s=last[nm]||{};
    const age=dbmRelAge(s.last_day);
    const issues=[];
    if(age.cls==='warnText') issues.push('最后更新超过24h');
    Object.entries(s.na||{}).forEach(([c,v])=>{ if(v!=null&&v>10) issues.push(c+'空值率'+v+'%'); });
    if(s.dup_pairs!=null&&s.dup_pairs>0) issues.push('重复对'+s.dup_pairs+'%');
    if(!issues.length) return;
    const c=age.cls==='warnText'?'#ff6b6b':'#f0b400';
    ah+='<div style="padding:4px 0;border-bottom:1px dashed rgba(255,255,255,.07);color:'+c+'">'
      +'<b>'+esc(nm)+'</b>: '+esc(issues.join('; '))+'</div>';
  });
  al.innerHTML=ah||'<div style="color:#3dd68c">● 无异常 · 各表数据健康</div>';
  // 更新状态
  const ue=document.getElementById('dbmonUpd');
  if(ue) ue.innerHTML='<b>更新:</b> '+esc(dbmonStatus(d.update||{}));
  const rdn=document.getElementById('dbmonRd');
  if(rdn){
    const rd=d.run_daily||{};
    let rtxt='run_daily: '+(rd.running
      ?'运行中 · '+esc(rd.stage||'')+(rd.day?' ('+esc(rd.day)+' '+esc(rd.mode||'')+')':'')
      :(rd.finished?('上次 '+(rd.finished||'').slice(0,16)+' ok='+rd.ok+(rd.elapsed_s?(' · '+rd.elapsed_s+'s'):'')):'空闲'));
    if(rd.tail) rtxt+=' <span style="color:#8b98b3">· '+esc(String(rd.tail).slice(-110))+'</span>';
    rdn.innerHTML=rtxt;
  }
  const busy=(d.update&&d.update.running)||(d.run_daily&&d.run_daily.running);
  if(busy&&!_dbmTimer){
    _dbmTimer=setInterval(loadDbmon,5000);
  } else if(!busy&&_dbmTimer){ clearInterval(_dbmTimer); _dbmTimer=null; }
}
async function loadDbmon(){
  try{
    const d=await dbmFetch('/api/db_stats/latest');
    if(d.ok) dbmonRender(d);
  }catch(e){ const b=document.getElementById('dbmonTable'); if(b) b.innerHTML='<div class="muted">加载失败 '+esc(String(e).slice(0,80))+'</div>'; }
}
function initDbmon(){
  const r=document.getElementById('btnDbStatsRefresh'), m=document.getElementById('btnDbStatsManual');
  if(r) r.addEventListener('click', async ()=>{ r.textContent='采集中...'; try{ await dbmFetch('/api/db_stats/refresh'); loadDbmon(); }finally{ r.textContent='⟳ 刷新统计'; } });
  if(m) m.addEventListener('click', async ()=>{ m.textContent='投递中...'; try{ const d=await dbmFetch('/api/db_stats/manual',{method:'POST'}); const ue=document.getElementById('dbmonUpd'); if(ue) ue.innerHTML='<b>更新:</b> '+esc(d.msg||(d.error||''))+(d.ok&&!d.async?'(同步完成)':''); loadDbmon(); }finally{ m.textContent='⏻ 手动全量更新'; } });
  const rd=document.getElementById('btnDbStatsRunDaily');
  if(rd) rd.addEventListener('click', async ()=>{ rd.textContent='投递中...'; try{ const d=await dbmFetch('/api/db_stats/run_daily',{method:'POST'}); const ue=document.getElementById('dbmonUpd'); if(ue) ue.innerHTML='<b>run_daily:</b> '+esc(d.msg||(d.error||'')); loadDbmon(); }finally{ rd.textContent='▶ 异步 run_daily'; } });
  loadDbmon();
  setInterval(loadDbmon, 15000);
}

// ===================== 8 个新面板渲染函数 =====================

// ---- 回测 ----
async function loadBacktestHistory(){
  try{ const d = await fetch('/api/backtest/history').then(r=>r.json()); renderBacktestHistory(d); }
  catch(e){ const _b = document.getElementById('btHistoryBody'); if(_b) _b.innerHTML='<span class="empty">加载失败: '+e+'</span>'; }
}
function renderBacktestHistory(d){
  const body = $('#btHistoryBody');
  if(!d.ok){ body.innerHTML='<span class="empty">'+esc(d.error||'error')+'</span>'; return; }
  const rows = d.rows || [];
  if(!rows.length){ body.innerHTML='<div class="muted">暂无回测历史</div>'; return; }
  let html = `<div class="muted" style="margin-bottom:8px;font-size:12px">共 ${rows.length} 条记录</div>`;
  html += '<table class="tbl"><thead><tr><th>来源</th><th>日</th><th>标签</th><th>总收益</th><th>年化Sharpe</th><th>最大回撤</th><th>起始</th><th>结束</th><th>交易日</th><th>交易笔数</th></tr></thead><tbody>';
  rows.forEach(r=>{
    // 容错: 数据来源有三种 schema
    //   A. backtest_latest.json -> 扁平字段 total_return / total_trades / trade_days
    //   B. vnpy_backtest/summary.json -> 嵌套 stats.* (annual_return, sharpe_ratio 等)
    //   C. performance_report.json -> 嵌套 metrics.* + period.*
    const stats = r.stats || {};
    const m = r.metrics || {};
    const period = r.period || {};
    // 单位约定(2026-09-12 统一): performance_report.metrics / vnpy stats / BacktestRunner
    // 的收益与回撤字段均为"百分数"(*_pct 亦然), 展示层不再做 x100 启发式换算。
    let totalRet = r.total_return;
    if(totalRet==null) totalRet = stats.annual_return != null ? stats.annual_return : m.total_return;
    // Sharpe: vnpy sharpe_ratio; 其它 sharpe_annual
    const sharpe = stats.sharpe_ratio != null ? stats.sharpe_ratio : m.sharpe_annual;
    // 最大回撤: vnpy max_ddpercent / BacktestRunner max_drawdown_pct / perf max_drawdown 均为百分数
    let maxDd = stats.max_ddpercent != null ? stats.max_ddpercent : (r.max_drawdown_pct != null ? r.max_drawdown_pct : (m.max_drawdown != null ? m.max_drawdown : null));
    // 交易日 / 笔数
    const tradeDays = stats.total_days != null ? stats.total_days : r.trade_days;
    const totalTrades = r.total_trades != null ? r.total_trades : (stats.total_trade_count != null ? stats.total_trade_count : m.trade_count);
    // 起止日期
    const startD = r.start || period.start || stats.start_date;
    const endD = r.end || period.end || stats.end_date;
    // 来源
    const src = (r._source||'').replace(/^.*[\\/]/,'');
    html += `<tr>
      <td><code style="font-size:10px">${esc(src)}</code></td>
      <td>${esc(r._day||'')}</td>
      <td>${esc(r.tag||'')}</td>
      <td class="${(totalRet||0)>=0?'up':'down'}">${totalRet!=null?fmt(totalRet,2)+'%':'—'}</td>
      <td>${sharpe!=null?fmt(sharpe,3):'—'}</td>
      <td class="down">${maxDd!=null?fmt(maxDd,2)+'%':'—'}</td>
      <td>${esc(startD||'')}</td>
      <td>${esc(endD||'')}</td>
      <td>${tradeDays||''}</td>
      <td>${totalTrades||''}</td>
    </tr>`;
  });
  html += '</tbody></table>';
  body.innerHTML = html;
}

// ---- 概念分析 ----
function debounce(fn, ms){ let t; return (...a)=>{ clearTimeout(t); t=setTimeout(()=>fn(...a), ms); }; }
let conceptLastData = null;
async function loadConcept(){
  const body = $('#conceptBody');
  body.innerHTML = '<div class="muted">加载中...</div>';
  const tag = $('#conceptSearch') ? $('#conceptSearch').value.trim() : '';
  const sort = $('#conceptSort') ? $('#conceptSort').value : 'strength';
  const top  = $('#conceptTop') ? $('#conceptTop').value : '50';
  try{
    const u = `/api/concept?top=${top}&sort=${encodeURIComponent(sort)}${tag?`&tag=${encodeURIComponent(tag)}`:''}`;
    const d = await fetch(u).then(r=>r.json());
    conceptLastData = d;
    renderConcept(d);
  }catch(e){ body.innerHTML='<span class="empty">加载失败: '+e+'</span>'; }
}
function renderConcept(d){
  const body = $('#conceptBody');
  if(!d.ok){ body.innerHTML='<span class="empty">'+esc(d.error||'error')+'</span>'; return; }
  const items = d.items || [];
  let html = `<div class="muted" style="margin-bottom:6px;font-size:12px">
    源: ${esc(d.source||'')} · ext_gn_ths 抓取: ${esc(d.fetched_at||'')} ·
    共 <b style="color:#fff">${d.total_concepts||items.length}</b> 个概念 · 覆盖 ${d.total_symbols||0} 只标的 · 排序: ${esc(d.sort_by||'')}${d.tag_filter?` · 过滤: ${esc(d.tag_filter)}`:''}
  </div>`;
  if(!items.length){ html += '<div class="muted">无匹配概念</div>'; body.innerHTML = html; return; }
  html += `<div style="overflow-x:auto"><table class="tbl" style="white-space:nowrap">
    <thead><tr>
      <th>概念名 (点击展开)</th>
      <th>成分数</th>
      <th>当日参与</th>
      <th>涨停</th>
      <th>强势</th>
      <th>平均涨幅%</th>
      <th>中位涨幅%</th>
      <th>总成交额(亿)</th>
      <th>平均换手%</th>
      <th>热度</th>
      <th>前 3 龙头</th>
    </tr></thead><tbody id="conceptTbody">`;
  items.forEach(it=>{
    const leaders = (it.leaders || []).map(l => {
      const cls = (l.change_pct||0) >= 0 ? 'up' : 'down';
      return `<span class="badge" style="font-size:10px;margin-right:4px" title="${esc(l.name||'')}">${esc(l.symbol)} <span class="${cls}">${fmt(l.change_pct,2)}%</span></span>`;
    }).join('');
    const cls = (it.avg_change_pct||0) >= 0 ? 'up' : 'down';
    const sign = (it.avg_change_pct||0) >= 0 ? '+' : '';
    html += `<tr data-name="${esc(it.name)}" style="cursor:pointer">
      <td><b style="color:#fff">${esc(it.name)}</b></td>
      <td>${it.count}</td>
      <td>${it.today||0}</td>
      <td class="up">${it.limit_up_count||0}</td>
      <td class="up">${it.strong_up_count||0}</td>
      <td class="${cls}">${it.avg_change_pct!=null?sign+fmt(it.avg_change_pct,2):'—'}</td>
      <td class="${cls}">${it.median_change_pct!=null?sign+fmt(it.median_change_pct,2):'—'}</td>
      <td>${fmt((it.total_amount||0)/1e8, 2)}</td>
      <td>${it.avg_turnover!=null?fmt(it.avg_turnover,2):'—'}</td>
      <td><b style="color:#fff">${fmt(it.heat_score,1)}</b></td>
      <td>${leaders||'<span class="muted">—</span>'}</td>
    </tr>`;
  });
  html += '</tbody></table></div>';
  body.innerHTML = html;
  // 行点击展开详情
  body.querySelectorAll('#conceptTbody tr').forEach(tr=>{
    tr.addEventListener('click', ()=>{
      const name = tr.dataset.name;
      showConceptDetail(name);
    });
  });
}
async function showConceptDetail(name){
  const det = $('#conceptDetail');
  det.innerHTML = '<div class="muted">查询 '+esc(name)+' 成分股...</div>';
  try{
    const r = await fetch(`/api/concept?tag=${encodeURIComponent(name)}&top=1`).then(r=>r.json());
    const item = (r.items||[]).find(x=>x.name===name) || (r.items||[])[0];
    if(!item){ det.innerHTML = '<span class="empty">无详情</span>'; return; }
    const symRes = await fetch(`/api/concept/symbols?name=${encodeURIComponent(name)}`).then(r=>r.json());
    const symbols = symRes.symbols || [];
    const related = symRes.related_tags || [];
    det.innerHTML = `<div class="panel" style="margin-top:6px;background:rgba(255,255,255,.02)">
      <h4 style="color:#fff">${esc(name)} <span class="muted" style="font-weight:400;font-size:12px">成分股 ${symbols.length} 只 · 平均 ${fmt(item.avg_change_pct||0,2)}% · 成交 ${fmt((item.total_amount||0)/1e8,2)} 亿</span></h4>
      <div style="font-family:monospace;font-size:11px;color:#0f0;max-height:200px;overflow-y:auto">${symbols.slice(0,300).join(' · ')}</div>
      ${related.length?`<div style="margin-top:6px;font-size:11px" class="muted">相关标签: ${esc(related.slice(0,8).join(' · '))}</div>`:''}
    </div>`;
  }catch(e){ det.innerHTML = '<span class="empty">查询失败: '+e+'</span>'; }
}

// ---- 行业分析 ----
let industryLastData = null;
async function loadIndustry(){
  const body = $('#industryBody');
  body.innerHTML = '<div class="muted">加载中...</div>';
  const tag = $('#industrySearch') ? $('#industrySearch').value.trim() : '';
  const sort = $('#industrySort') ? $('#industrySort').value : 'strength';
  const top  = $('#industryTop') ? $('#industryTop').value : '50';
  try{
    const u = `/api/industry?top=${top}&sort=${encodeURIComponent(sort)}${tag?`&tag=${encodeURIComponent(tag)}`:''}`;
    const d = await fetch(u).then(r=>r.json());
    industryLastData = d;
    renderIndustry(d);
  }catch(e){ body.innerHTML='<span class="empty">加载失败: '+e+'</span>'; }
}
function renderIndustry(d){
  const body = $('#industryBody');
  if(!d.ok){ body.innerHTML='<span class="empty">'+esc(d.error||'error')+'</span>'; return; }
  const items = d.items || [];
  let html = `<div class="muted" style="margin-bottom:6px;font-size:12px">
    源: ${esc(d.source||'')} · ext_hy_ths 抓取: ${esc(d.fetched_at||'')} ·
    共 <b style="color:#fff">${d.total_industries||items.length}</b> 个行业(含一级-二级-三级) · 覆盖 ${d.total_symbols||0} 只标的 · 排序: ${esc(d.sort_by||'')}${d.tag_filter?` · 过滤: ${esc(d.tag_filter)}`:''}
  </div>`;
  if(!items.length){ html += '<div class="muted">无匹配行业</div>'; body.innerHTML = html; return; }
  html += `<div style="overflow-x:auto"><table class="tbl" style="white-space:nowrap">
    <thead><tr>
      <th>行业 (一级-二级-三级, 点击展开)</th>
      <th>成分数</th>
      <th>当日参与</th>
      <th>涨停</th>
      <th>强势</th>
      <th>平均涨幅%</th>
      <th>中位涨幅%</th>
      <th>总成交额(亿)</th>
      <th>平均换手%</th>
      <th>热度</th>
      <th>前 3 龙头</th>
    </tr></thead><tbody id="industryTbody">`;
  items.forEach(it=>{
    const leaders = (it.leaders || []).map(l => {
      const cls = (l.change_pct||0) >= 0 ? 'up' : 'down';
      return `<span class="badge" style="font-size:10px;margin-right:4px" title="${esc(l.name||'')}">${esc(l.symbol)} <span class="${cls}">${fmt(l.change_pct,2)}%</span></span>`;
    }).join('');
    const cls = (it.avg_change_pct||0) >= 0 ? 'up' : 'down';
    const sign = (it.avg_change_pct||0) >= 0 ? '+' : '';
    html += `<tr data-name="${esc(it.name)}" style="cursor:pointer">
      <td><b style="color:#fff">${esc(it.name)}</b></td>
      <td>${it.count}</td>
      <td>${it.today||0}</td>
      <td class="up">${it.limit_up_count||0}</td>
      <td class="up">${it.strong_up_count||0}</td>
      <td class="${cls}">${it.avg_change_pct!=null?sign+fmt(it.avg_change_pct,2):'—'}</td>
      <td class="${cls}">${it.median_change_pct!=null?sign+fmt(it.median_change_pct,2):'—'}</td>
      <td>${fmt((it.total_amount||0)/1e8, 2)}</td>
      <td>${it.avg_turnover!=null?fmt(it.avg_turnover,2):'—'}</td>
      <td><b style="color:#fff">${fmt(it.heat_score,1)}</b></td>
      <td>${leaders||'<span class="muted">—</span>'}</td>
    </tr>`;
  });
  html += '</tbody></table></div>';
  body.innerHTML = html;
  body.querySelectorAll('#industryTbody tr').forEach(tr=>{
    tr.addEventListener('click', ()=>{
      const name = tr.dataset.name;
      showIndustryDetail(name);
    });
  });
}
async function showIndustryDetail(name){
  const det = $('#industryDetail');
  det.innerHTML = '<div class="muted">查询 '+esc(name)+' 成分股...</div>';
  try{
    const r = await fetch(`/api/industry?tag=${encodeURIComponent(name)}&top=1`).then(r=>r.json());
    const item = (r.items||[]).find(x=>x.name===name) || (r.items||[])[0];
    if(!item){ det.innerHTML = '<span class="empty">无详情</span>'; return; }
    // 反查 symbol: 利用 concept/symbol API 按symbol查 (不可, 这里逐symbol查太慢 -> 用后端新增 endpoint 更稳)
    const symbols = await fetchIndustrySymbols(name);
    det.innerHTML = `<div class="panel" style="margin-top:6px;background:rgba(255,255,255,.02)">
      <h4 style="color:#fff">${esc(name)} <span class="muted" style="font-weight:400;font-size:12px">成分股 ${symbols.length} 只 · 平均 ${fmt(item.avg_change_pct||0,2)}% · 成交 ${fmt((item.total_amount||0)/1e8,2)} 亿</span></h4>
      <div style="font-family:monospace;font-size:11px;color:#0f0;max-height:200px;overflow-y:auto">${symbols.slice(0,300).join(' · ')}</div>
    </div>`;
  }catch(e){ det.innerHTML = '<span class="empty">查询失败: '+e+'</span>'; }
}
async function fetchIndustrySymbols(name){
  // 通过新增后端路由直接拿 symbol 列表 (按 name 过滤)
  try{
    const j = await fetch(`/api/industry/symbols?name=${encodeURIComponent(name)}`).then(r=>r.json());
    return j.symbols || [];
  }catch(_){ return []; }
}

// ---- 监控中心 ----
async function loadMonitor(){
  try{ const d = await fetch('/api/monitor').then(r=>r.json()); renderMonitor(d); }
  catch(e){ $('#monitorBody').innerHTML='<span class="empty">加载失败: '+e+'</span>'; }
}
function renderMonitor(d){
  const body = $('#monitorBody');
  if(!d.ok){ body.innerHTML='<span class="empty">'+esc(d.error||'error')+'</span>'; return; }
  const c = d.components || {};
  let html = '<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:14px">';
  // 1) 盘前健康
  html += '<div class="card"><div class="lbl">盘前健康检查</div>';
  const h = c.health || {};
  if(h.ok !== undefined){
    const cls = h.level === 'OK' ? 'up' : (h.level === 'DEGRADED' ? 'warn' : 'down');
    html += `<div class="val ${cls}">${esc(h.level||'?')}</div><div class="hint">${esc(h.summary||'')}</div>`;
    if(h.checks){
      html += '<div style="margin-top:6px;font-size:11px">';
      h.checks.forEach(ch=>{ html += `<span class="badge ${ch.status==='OK'?'up':(ch.status==='WARN'?'warn':'down')}" style="margin:1px">${esc(ch.name)}=${esc(ch.status)}</span>`; });
      html += '</div>';
    }
  } else { html += '<div class="hint">'+esc(h.error||'未生成')+'</div>'; }
  html += '</div>';
  // 2) 策略退化
  html += '<div class="card"><div class="lbl">策略退化指数</div>';
  const dg = c.degradation || {};
  if(dg.index){
    html += `<div class="val">${fmt(dg.index.overall_score,1)}</div>
      <div class="hint">最差维度: <span class="${dg.index.worst_level==='P0'?'down':(dg.index.worst_level==='P1'?'warn':'')}">${esc(dg.index.worst_level||'-')}</span></div>`;
  } else { html += '<div class="hint">'+esc(dg.error||'未生成')+'</div>'; }
  html += '</div>';
  // 3) reward config
  html += '<div class="card"><div class="lbl">增量学习奖励权重</div>';
  const rc = c.reward_config || {};
  if(rc.vnpy_weight !== undefined){
    html += `<div class="val">vnpy ${fmt(rc.vnpy_weight,2)} / ic ${fmt(rc.ic_weight,2)}</div>
      <div class="hint">来源: ${esc(rc.source||'-')}</div>`;
  } else { html += '<div class="hint">'+esc(rc.error||'默认 0.6/0.4')+'</div>'; }
  html += '</div>';
  // 4) DRL plan ready
  html += '<div class="card"><div class="lbl">DRL 目标计划</div>';
  const dp = c.drl_plan || {};
  if(dp.ok){
    const p = dp.data || {};
    html += `<div class="val up">READY</div>
      <div class="hint">top ${p.top_n?p.top_n.length:0} · universe ${p.universe_size||0} · ${esc((p.generated_at||'').slice(0,16))}</div>`;
  } else { html += '<div class="val down">NOT READY</div><div class="hint">'+esc(dp.error||'')+'</div>'; }
  html += '</div>';
  // 5) daemon
  html += '<div class="card"><div class="lbl">守护进程</div>';
  const dm = c.daemon || {};
  if(dm.running_day){
    html += `<div class="val">${esc(dm.running_day||'-')}</div>
      <div class="hint">engine_pid: ${esc(String(dm.engine_pid||'-'))} · last_close: ${esc(dm.last_close_day||'-')}</div>`;
  } else { html += '<div class="hint">'+esc(dm.error||'未启动')+'</div>'; }
  html += '</div>';
  html += '</div>';
  body.innerHTML = html;
}

// ---- 市场环境 ----
async function loadRegime(){
  try{ const d = await fetch('/api/regime').then(r=>r.json()); renderRegime(d); }
  catch(e){ $('#regimeBody').innerHTML='<span class="empty">加载失败: '+e+'</span>'; }
}
function renderRegime(d){
  const body = $('#regimeBody');
  if(!d.ok){ body.innerHTML='<span class="empty">'+esc(d.error||'error')+'</span>'; return; }
  const c = d.components || {};
  let html = '';
  // 阶段
  if(c.phase){
    const ph = c.phase;
    html += '<div class="card" style="margin-bottom:14px"><div class="lbl">当前市场阶段</div>';
    const cls = ph.phase.indexOf('上行')>=0 || ph.phase.indexOf('偏多')>=0 ? 'up' : (ph.phase.indexOf('下行')>=0 || ph.phase.indexOf('偏空')>=0 ? 'down' : 'warn');
    html += `<div class="val ${cls}">${esc(ph.phase)}</div>
      <div class="hint">基于 ${esc(ph.based_on||'-')} · 宽度比 ${fmt(ph.breadth_ratio,3)} · 涨停 ${ph.n_limit_up} / 跌停 ${ph.n_limit_dn}</div>`;
    html += '</div>';
  }
  // 市场情绪
  if(c.market && c.market.data){
    const m = c.market.data;
    html += '<h3 style="font-size:14px;margin:8px 0">市场情绪 ('+esc(c.market.path.split(/[\\/]/).pop())+')</h3>';
    html += '<table class="tbl"><thead><tr><th>指标</th><th>值</th></tr></thead><tbody>';
    Object.keys(m).forEach(k=>{
      if(typeof m[k] === 'object' || k === 'ok') return;
      html += `<tr><td>${esc(k)}</td><td><code>${esc(JSON.stringify(m[k]))}</code></td></tr>`;
    });
    html += '</tbody></table>';
  }
  // 市场宽度
  if(c.breadth && c.breadth.length){
    html += '<h3 style="font-size:14px;margin:14px 0 8px">市场宽度 (最近 ' + c.breadth.length + ' 日)</h3>';
    html += '<table class="tbl"><thead><tr><th>日期</th><th>总数</th><th>涨</th><th>跌</th><th>涨停</th><th>跌停</th><th>宽度比</th><th>均价%</th><th>总成交(亿)</th></tr></thead><tbody>';
    c.breadth.forEach(b=>{
      html += `<tr>
        <td>${esc(b.date)}</td>
        <td>${b.total}</td>
        <td class="up">${b.n_up}</td>
        <td class="down">${b.n_down}</td>
        <td class="up">${b.n_limit_up}</td>
        <td class="down">${b.n_limit_dn}</td>
        <td class="${(b.breadth_ratio||0)>=0?'up':'down'}">${fmt(b.breadth_ratio,3)}</td>
        <td class="${(b.avg_chg||0)>=0?'up':'down'}">${fmt(b.avg_chg,3)}</td>
        <td>${fmt((b.total_amount||0)/1e8,2)}</td>
      </tr>`;
    });
    html += '</tbody></table>';
  }
  // 因子 IC
  if(c.factor_ic && c.factor_ic.length){
    html += '<h3 style="font-size:14px;margin:14px 0 8px">因子 IC (近 20 日)</h3>';
    html += '<table class="tbl"><thead><tr><th>因子</th><th>IC</th><th>ICIR</th><th>胜率</th><th>N</th></tr></thead><tbody>';
    c.factor_ic.forEach(f=>{
      html += `<tr><td>${esc(f.factor)}</td>
        <td class="${(f.ic_value||0)>=0?'up':'down'}">${fmt(f.ic_value,4)}</td>
        <td class="${(f.icir_20||0)>=0?'up':'down'}">${fmt(f.icir_20,3)}</td>
        <td>${fmt((f.win_rate_20||0)*100,1)}%</td>
        <td>${f.n_days}</td></tr>`;
    });
    html += '</tbody></table>';
  }
  body.innerHTML = html;
}

// ---- 异动监控 ----
async function loadAbnormal(){
  try{ const d = await fetch('/api/abnormal?limit=30').then(r=>r.json()); renderAbnormal(d); }
  catch(e){ $('#abnormalBody').innerHTML='<span class="empty">加载失败: '+e+'</span>'; }
}
function renderAbnormal(d){
  const body = $('#abnormalBody');
  if(!d.ok){ body.innerHTML='<span class="empty">'+esc(d.error||'error')+'</span>'; return; }
  let html = `<div class="muted" style="margin-bottom:8px;font-size:12px">基于 ${esc(d.latest_date)} 收盘</div>`;
  function tbl(t, title, color){
    if(!t||!t.length) return '';
    let h = `<h3 style="font-size:14px;margin:14px 0 8px;color:${color}">${title}</h3><table class="tbl"><thead><tr><th>代码</th><th>收盘</th><th>涨幅%</th><th>成交额</th></tr></thead><tbody>`;
    t.slice(0,15).forEach(r=>{
      h += `<tr><td><code>${esc(r.canon)}</code></td><td>${fmt(r.close,2)}</td><td class="${(r.change_pct||0)>=0?'up':'down'}">${(r.change_pct>=0?'+':'') + fmt(r.change_pct,2)}</td><td>${fmt((r.amount||0)/1e8,2)}亿</td></tr>`;
    });
    h += '</tbody></table>';
    return h;
  }
  html += tbl(d.limit_up, '涨停 (' + d.limit_up.length + ')', '#ff5b6a');
  html += tbl(d.limit_dn, '跌停 (' + d.limit_dn.length + ')', '#2fe6a6');
  html += tbl(d.top_change, '涨幅 Top', '#ff5b6a');
  html += tbl(d.top_drop, '跌幅 Top', '#2fe6a6');
  html += tbl(d.top_amount, '成交额 Top', '#4f8cff');
  // 放大量
  if(d.volume_surge && d.volume_surge.length){
    html += '<h3 style="font-size:14px;margin:14px 0 8px">放大量 (vs 20日均量)</h3>';
    html += '<table class="tbl"><thead><tr><th>代码</th><th>今日成交额</th><th>20日均</th><th>倍数</th></tr></thead><tbody>';
    d.volume_surge.slice(0,15).forEach(v=>{
      html += `<tr><td><code>${esc(v.canon)}</code></td><td>${fmt((v.amount||0)/1e8,2)}亿</td><td>${fmt((v.amt_avg_20d||0)/1e8,2)}亿</td><td class="up">${fmt(v.ratio,2)}x</td></tr>`;
    });
    html += '</tbody></table>';
  }
  body.innerHTML = html;
}

// ---- 数据板块 ----
let dbSyncJobId = null;       // 当前同步任务 ID
let dbSyncPollTimer = null;
async function loadDbPanel(){
  try{ const d = await fetch('/api/db_meta').then(r=>r.json()); renderDbPanel(d); }
  catch(e){ $('#dbpanelBody').innerHTML='<span class="empty">加载失败: '+e+'</span>'; }
}
function renderDbPanel(d){
  const body = $('#dbpanelBody');
  if(!d.ok){ body.innerHTML='<span class="empty">'+esc(d.error||'error')+'</span>'; return; }
  // 头部 + 同步按钮
  const size_mb = d.duckdb.size_mb || 0;
  let html = `<div class="muted" style="margin-bottom:6px;font-size:12px">
    DuckDB: ${esc(d.duckdb.path)} (${size_mb} MB) · ArcticDB: ${esc((d.arcticdb.uri||'-'))}</div>`;
  // 同步控制条
  html += `<div style="display:flex;gap:8px;flex-wrap:wrap;margin-bottom:10px;padding:10px;background:hsl(var(--elevated)/.5);border-radius:8px;border:1px solid hsl(var(--border))">
    <button class="tabBtn dbp-sync-btn" data-mode="all">增量同步 (全部表)</button>
    <select id="dbpOnlySel" class="input-dark" style="padding:5px 8px"></select>
    <button class="tabBtn dbp-sync-btn" data-mode="partial">增量同步 (选中表)</button>
    <button class="tabBtn dbp-sync-btn" data-mode="manual" id="dbpManualBtn">手动同步 (单表)</button>
    <input id="dbpDayInput" class="input-dark" type="date" value="${new Date().toISOString().slice(0,10)}" style="padding:5px"/>
    <span class="muted" style="font-size:11px">同步日期 (默认今日)</span>
    <span class="muted" id="dbpSyncStatus" style="font-size:11px;margin-left:auto"></span>
  </div>`;
  html += '<div id="dbpSyncLog" style="display:none;background:hsl(var(--base));border:1px solid hsl(var(--border));border-radius:6px;padding:8px;max-height:160px;overflow-y:auto;font-family:monospace;font-size:10px;margin-bottom:10px;white-space:pre-wrap"></div>';

  // DuckDB 表 (按行数倒序) — 表头白色 + 中文名 + 数据时间范围
  const tables = d.duckdb.tables || [];
  const groups = d.duckdb.groups || {};
  // 分组下拉
  html += '<div id="dbpOnlySelInit" style="display:none">__GROUPS__</div>';
  let groupsOpts = '<option value="">(不限)</option>';
  Object.keys(groups).sort().forEach(g=>{
    groupsOpts += `<option value="${esc(g)}">${esc(g)} (${groups[g]})</option>`;
  });
  // 渲染分组
  html += '<h3 style="font-size:14px;margin:8px 0;">DuckDB 表 (' + tables.length + ' / ' + Object.keys(groups).length + ' 类)</h3>';
  // 同步过滤
  html += `<div style="margin-bottom:6px;display:flex;gap:6px;align-items:center">
    <input id="dbpFilterInput" placeholder="过滤表名/中文名" style="flex:1;padding:5px 8px;border:1px solid #334;border-radius:6px;background:#0f1420;color:hsl(var(--fg-primary))"/>
  </div>`;
  html += '<div style="max-height:600px;overflow-y:auto"><table class="tbl"><thead><tr><th>表名 (中文)</th><th>分组</th><th>行数</th><th>列数</th><th>数据时间范围</th><th>字段</th><th>操作</th></tr></thead><tbody id="dbpTbody">';
  tables.forEach(t=>{
    const cols = (t.columns||[]).map(c=>c.name).join(', ');
    const range = t.date_range || '-';
    html += `<tr data-name="${esc(t.name)}" data-zh="${esc(t.name_zh||'')}" data-group="${esc(t.group||'')}">
      <td><a href="#" class="dbp-table" data-name="${esc(t.name)}" ><b style="color:#fff;font-size:13px;letter-spacing:0.5px">${esc(t.name)}</b><br/><span class="muted" style="font-size:10px">${esc(t.name_zh||'')}</span></a></td>
      <td><span class="badge" style="font-size:10px">${esc(t.group||'-')}</span></td>
      <td>${(t.rows||0).toLocaleString()}</td>
      <td>${t.col_count||(t.columns||[]).length}</td>
      <td><code style="font-size:10px;color:hsl(var(--accent))">${esc(range)}</code></td>
      <td><code style="font-size:10px;display:block;max-width:400px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(cols)}</code></td>
      <td><button class="tabBtn dbp-manual-sync" data-name="${esc(t.name)}" style="font-size:10px;padding:2px 8px">同步</button></td>
    </tr>`;
  });
  html += '</tbody></table></div>';

  // ArcticDB 库
  const a = d.arcticdb || {};
  if(a.libraries){
    html += '<h3 style="font-size:14px;margin:14px 0 8px;">ArcticDB 库</h3>';
    html += '<table class="tbl"><thead><tr><th>库 (中文)</th><th>symbols</th><th>说明</th></tr></thead><tbody>';
    const libZh = {"bars":"K线明细","trade_records":"交易记录","daily_summary":"每日汇总","perf_report":"绩效报告","factor_ic":"因子IC","reward_curve":"奖励曲线"};
    Object.keys(a.libraries).forEach(k=>{
      html += `<tr><td><b style="color:#fff;font-size:13px">${esc(k)}</b> <span class="muted" style="font-size:10px">${esc(libZh[k]||k)}</span></td><td>${(a.stats&&a.stats[k])||0}</td><td class="muted">LMDB-backed time-series</td></tr>`;
    });
    html += '</tbody></table>';
  }
  body.innerHTML = html;

  // 注入分组选项到 dbpOnlySel
  document.getElementById('dbpOnlySel').innerHTML = groupsOpts;
  // 过滤输入
  document.getElementById('dbpFilterInput').oninput = (e)=>{
    const q = e.target.value.toLowerCase();
    body.querySelectorAll('#dbpTbody tr').forEach(tr=>{
      const hit = !q || tr.dataset.name.toLowerCase().includes(q) ||
        (tr.dataset.zh||'').toLowerCase().includes(q) ||
        (tr.dataset.group||'').toLowerCase().includes(q);
      tr.style.display = hit ? '' : 'none';
    });
  };
  // 表名点击展开样本
  body.querySelectorAll('.dbp-table').forEach(a=>{
    a.onclick = async (e) => {
      e.preventDefault();
      const name = a.dataset.name;
      const r = await fetch('/api/db_table/' + encodeURIComponent(name) + '?limit=10').then(r=>r.json());
      if(!r.ok){ alert('读取失败: ' + r.error); return; }
      let h = `<h3 style="font-size:14px;margin:14px 0 8px;">${esc(name)} · 样本 (${r.rows.length}/${r.total})</h3>`;
      h += '<div style="overflow-x:auto"><table class="tbl" style="font-size:11px"><thead><tr>';
      r.columns.forEach(c=>{ h += `<th>${esc(c)}</th>`; });
      h += '</tr></thead><tbody>';
      r.rows.forEach(row=>{
        h += '<tr>';
        row.forEach(v=>{ h += `<td><code style="font-size:10px">${esc(v===null?'NULL':String(v).slice(0,80))}</code></td>`; });
        h += '</tr>';
      });
      h += '</tbody></table></div>';
      const div = document.createElement('div');
      div.innerHTML = h;
      body.appendChild(div);
    };
  });
  // 同步按钮
  body.querySelectorAll('.dbp-sync-btn').forEach(b=>{
    b.onclick = async () => {
      const mode = b.dataset.mode;
      const day = document.getElementById('dbpDayInput').value || null;
      let payload = {day};
      if(mode === 'partial'){
        const sel = document.getElementById('dbpOnlySel').value;
        if(!sel){ alert('请先在分组下拉选择一组'); return; }
        payload.only = (window._dbpGroups||{})[sel] || [];
      } else if(mode === 'manual'){
        // 单表手动同步: 让用户选表
        const table = prompt('请输入要手动同步的表名 (例: daily_bars)');
        if(!table) return;
        startDbSync('manual', {table, day});
        return;
      }
      startDbSync('incremental', payload);
    };
  });
  body.querySelectorAll('.dbp-manual-sync').forEach(b=>{
    b.onclick = (e) => {
      e.preventDefault(); e.stopPropagation();
      const t = b.dataset.name;
      const day = document.getElementById('dbpDayInput').value || null;
      startDbSync('manual', {table: t, day});
    };
  });
}
async function startDbSync(mode, payload){
  const url = mode === 'manual' ? '/api/db_sync/manual' : '/api/db_sync/incremental';
  const log = document.getElementById('dbpSyncLog');
  const stat = document.getElementById('dbpSyncStatus');
  log.style.display = 'block';
  log.textContent = `[${new Date().toLocaleTimeString()}] 启动 ${mode} 同步: ${JSON.stringify(payload)}\n`;
  stat.textContent = '调度中...';
  try{
    // 同步模式: POST 等到响应再显示完整结果. 同步大表可能耗时 30s+
    const r = await fetch(url, {method:'POST', headers:{'Content-Type':'application/json'},
                                  body: JSON.stringify(payload)});
    const j = await r.json();
    if(!j.ok){ log.textContent += `[ERR] ${j.error||JSON.stringify(j)}\n`; stat.textContent='失败'; return; }
    dbSyncJobId = j.job_id;
    log.textContent += `[${new Date().toLocaleTimeString()}] job_id=${dbSyncJobId}\n`;
    const res = j.result || {};
    const tbls = res.tables || {};
    Object.keys(tbls).forEach(t=>{
      const v = tbls[t] || {};
      const lvl = v.ok ? 'info' : 'warn';
      log.textContent += `[${new Date().toLocaleTimeString()}] ${t}: ok=${v.ok} rows=${v.rows}${v.error?(' err='+v.error.slice(0,80)):''}\n`;
    });
    if(res.error){ log.textContent += `[ERR] ${res.error}\n`; }
    stat.textContent = res.ok ? '完成' : (res.error ? '失败' : '部分失败');
    log.scrollTop = log.scrollHeight;
    setTimeout(()=>loadDbPanel(), 800);
  } catch(e){
    log.textContent += `[ERR] ${e}\n`; stat.textContent='失败';
  }
}


// ---- 市场看板 ----
async function loadMarketBoard(){
  try{ const d = await fetch('/api/marketboard').then(r=>r.json()); renderMarketBoard(d); }
  catch(e){
    ['#mbKpi','#mbIndices','#mbRadar','#mbBreadth','#mbDist','#mbLadder','#mbActive'].forEach(s=>{
      const el = document.querySelector(s); if(el) el.innerHTML='<span class="empty">加载失败: '+e+'</span>';
    });
  }
}
function renderMarketBoard(d){
  if(!d.ok){ $('#mbKpi').innerHTML='<span class="empty">'+esc(d.error||'error')+'</span>'; return; }
  const c = d.components || {};
  // KPI
  if(c.kpi){
    const k = c.kpi;
    const upCls = k.breadth_ratio > 0 ? 'up' : (k.breadth_ratio < 0 ? 'down' : '');
    let html = '<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:10px">';
    const cards = [
      {label: '交易日', val: esc(k.latest_date || '-'), tone: 'neutral'},
      {label: '股票家数', val: (k.universe||0).toLocaleString(), tone: 'neutral'},
      {label: '上涨', val: (k.n_up||0).toLocaleString(), tone: 'up'},
      {label: '下跌', val: (k.n_down||0).toLocaleString(), tone: 'down'},
      {label: '平盘', val: (k.n_flat||0).toLocaleString(), tone: 'neutral'},
      {label: '涨停', val: (k.n_limit_up||0).toLocaleString(), tone: 'up'},
      {label: '跌停', val: (k.n_limit_dn||0).toLocaleString(), tone: 'down'},
      {label: '宽度比', val: fmt(k.breadth_ratio, 3), tone: upCls},
      {label: '均价%', val: ((k.avg_change_pct||0)>=0?'+':'') + fmt(k.avg_change_pct, 3), tone: (k.avg_change_pct||0)>=0?'up':'down'},
      {label: '中位数%', val: ((k.median_change_pct||0)>=0?'+':'') + fmt(k.median_change_pct, 3), tone: (k.median_change_pct||0)>=0?'up':'down'},
      {label: '总成交(亿)', val: fmt((k.total_amount||0)/1e8, 1), tone: 'neutral'},
    ];
    cards.forEach(card=>{
      html += `<div class="card"><div class="lbl">${card.label}</div><div class="val ${card.tone}">${card.val}</div></div>`;
    });
    html += '</div>';
    $('#mbKpi').innerHTML = html;
  }
  // 指数
  if(c.indices){
    let html = '<table class="tbl"><thead><tr><th>名称</th><th>代码</th><th>收盘</th><th>涨跌%</th></tr></thead><tbody>';
    c.indices.forEach(idx=>{
      const cls = (idx.change_pct||0) >= 0 ? 'up' : 'down';
      const sign = (idx.change_pct||0) >= 0 ? '+' : '';
      html += `<tr><td>${esc(idx.name)}</td><td><code>${esc(idx.symbol)}</code></td><td>${fmt(idx.close, 2)}</td><td class="${cls}">${sign}${fmt(idx.change_pct, 2)}%</td></tr>`;
    });
    html += '</tbody></table>';
    $('#mbIndices').innerHTML = html;
  } else { $('#mbIndices').innerHTML = '<div class="muted">暂无指数数据 (daily_bars 可能不含指数代码)</div>'; }
  // 雷达
  if(c.radar){
    const radar = c.radar;
    const N = radar.length;
    const size = 220, cx = size/2, cy = size/2, maxR = 70;
    const pts = radar.map((r,i)=>{
      const a = -Math.PI/2 + i*2*Math.PI/N;
      const rad = maxR * Math.max(0, Math.min(100, r.value)) / 100;
      return {x: cx + Math.cos(a)*rad, y: cy + Math.sin(a)*rad,
              lx: cx + Math.cos(a)*(maxR+22), ly: cy + Math.sin(a)*(maxR+22)};
    });
    const poly = pts.map(p=>`${p.x.toFixed(1)},${p.y.toFixed(1)}`).join(' ');
    let svg = `<svg viewBox="0 0 ${size} ${size}" style="width:100%;max-width:220px;margin:0 auto;display:block">`;
    // 网格
    [1, 0.66, 0.33].forEach((lv,i)=>{
      const gp = radar.map((_,j)=>{
        const a = -Math.PI/2 + j*2*Math.PI/N;
        return `${(cx + Math.cos(a)*maxR*lv).toFixed(1)},${(cy + Math.sin(a)*maxR*lv).toFixed(1)}`;
      }).join(' ');
      svg += `<polygon points="${gp}" fill="${i%2===0?'rgba(79,140,255,0.06)':'rgba(255,255,255,0.03)'}" stroke="rgba(255,255,255,0.15)" stroke-width="0.5"/>`;
    });
    pts.forEach(p=>{
      svg += `<line x1="${cx}" y1="${cy}" x2="${p.x.toFixed(1)}" y2="${p.y.toFixed(1)}" stroke="rgba(255,255,255,0.2)" stroke-width="0.5"/>`;
    });
    svg += `<polygon points="${poly}" fill="rgba(79,140,255,0.4)" stroke="#4f8cff" stroke-width="2"/>`;
    pts.forEach(p=>{
      svg += `<circle cx="${p.x.toFixed(1)}" cy="${p.y.toFixed(1)}" r="3" fill="#4f8cff" stroke="#fff" stroke-width="1"/>`;
    });
    // 中心: sentiment score
    const ss = c.sentiment_score;
    if(ss !== null && ss !== undefined){
      svg += `<text x="${cx}" y="${cy-4}" text-anchor="middle" fill="#fff" font-size="22" font-weight="bold" font-family="monospace">${fmt(ss,1)}</text>`;
      svg += `<text x="${cx}" y="${cy+14}" text-anchor="middle" fill="#8b98b3" font-size="10">情绪分</text>`;
    }
    pts.forEach(p=>{
      svg += `<text x="${p.lx.toFixed(1)}" y="${p.ly.toFixed(1)+3}" text-anchor="middle" fill="#e6edf7" font-size="10">${esc(p.label||'')}</text>`;
    });
    svg += '</svg>';
    $('#mbRadar').innerHTML = svg + `<div style="display:flex;flex-wrap:wrap;gap:6px;margin-top:8px;justify-content:center">${radar.map(r=>`<span class="badge" style="font-size:10px">${esc(r.label)} ${fmt(r.value,0)}</span>`).join('')}</div>`;
  } else { $('#mbRadar').innerHTML = '<div class="muted">暂无市场情绪数据</div>'; }
  // 宽度
  if(c.kpi){
    const k = c.kpi;
    const tot = Math.max(1, k.universe || 0);
    const upW = (k.n_up||0)/tot*100, downW = (k.n_down||0)/tot*100, flatW = Math.max(0, 100-upW-downW);
    let html = `<div style="display:flex;height:32px;border-radius:8px;overflow:hidden;margin-bottom:8px">
      <div style="background:hsl(var(--bull));width:${upW.toFixed(2)}%;display:flex;align-items:center;justify-content:center;font-size:11px">${(k.n_up||0)}</div>
      <div style="background:hsl(var(--fg-muted));width:${flatW.toFixed(2)}%;display:flex;align-items:center;justify-content:center;font-size:11px">${(k.n_flat||0)}</div>
      <div style="background:hsl(var(--bear));width:${downW.toFixed(2)}%;display:flex;align-items:center;justify-content:center;font-size:11px">${(k.n_down||0)}</div>
    </div>`;
    html += `<div style="display:grid;grid-template-columns:repeat(5,1fr);gap:6px">`;
    const blocks = [
      {label: '涨停', val: k.n_limit_up, tone: 'up'},
      {label: '跌停', val: k.n_limit_dn, tone: 'down'},
      {label: '总成交(亿)', val: fmt((k.total_amount||0)/1e8, 1), tone: 'neutral'},
      {label: '均价%', val: ((k.avg_change_pct||0)>=0?'+':'') + fmt(k.avg_change_pct, 3), tone: (k.avg_change_pct||0)>=0?'up':'down'},
      {label: '宽度比', val: fmt(k.breadth_ratio||0, 3), tone: (k.breadth_ratio||0)>=0?'up':'down'},
    ];
    blocks.forEach(b=>{
      html += `<div class="card" style="padding:8px"><div class="lbl">${b.label}</div><div class="val ${b.tone}" style="font-size:18px">${b.val}</div></div>`;
    });
    html += '</div>';
    $('#mbBreadth').innerHTML = html;
  }
  // 分布
  if(c.distribution){
    const dist = c.distribution;
    const mx = Math.max(...dist.map(d=>d.count), 1);
    let html = '<div style="display:grid;grid-template-columns:repeat(10,1fr);gap:4px;height:140px;align-items:end">';
    dist.forEach((d,i)=>{
      const h = Math.max(2, d.count/mx*100);
      const positive = i >= 5;
      html += `<div style="display:flex;flex-direction:column;align-items:center;justify-content:end;height:100%">
        <div style="font-size:10px;color:hsl(var(--fg-muted));font-family:monospace">${d.count}</div>
        <div style="width:80%;height:${h}%;background:${positive?'linear-gradient(180deg,hsl(var(--bull)),hsl(var(--bull)/.35))':'linear-gradient(180deg,hsl(var(--bear)),hsl(var(--bear)/.35))'};border-radius:3px 3px 0 0"></div>
        <div style="font-size:9px;color:hsl(var(--fg-muted));margin-top:2px;white-space:nowrap">${esc(d.label)}</div>
      </div>`;
    });
    html += '</div>';
    $('#mbDist').innerHTML = html;
  } else { $('#mbDist').innerHTML = '<div class="muted">暂无分布数据</div>'; }
  // 连板梯队
  if(c.ladder && c.ladder.length){
    let html = '';
    c.ladder.forEach(t=>{
      const barW = Math.min(100, t.count * 12);
      html += `<div style="display:grid;grid-template-columns:50px 1fr 40px;gap:8px;align-items:center;padding:4px 0;border-bottom:1px solid hsl(var(--border))">
        <span style="font-family:monospace;font-weight:bold;color:${t.boards>=5?'hsl(var(--bull))':(t.boards>=3?'hsl(var(--warn))':'hsl(var(--fg-muted))')}">${t.boards}板</span>
        <div style="height:8px;background:hsl(var(--elevated));border-radius:4px;overflow:hidden">
          <div style="height:100%;width:${barW}%;background:hsl(var(--bull));opacity:0.7"></div>
        </div>
        <span style="font-family:monospace;font-size:12px;color:hsl(var(--fg-primary))">${t.count}</span>
      </div>`;
      if(t.stocks && t.stocks.length){
        html += `<div style="padding-left:58px;font-size:10px;color:hsl(var(--fg-muted));margin-bottom:4px">${t.stocks.map(s=>`<span class="badge" style="margin-right:4px;font-size:9px">${esc(s.symbol)} ${(s.change_pct>=0?'+':'')}${fmt(s.change_pct,1)}%</span>`).join('')}</div>`;
      }
    });
    $('#mbLadder').innerHTML = html;
  } else { $('#mbLadder').innerHTML = '<div class="muted">今日无涨停股票</div>'; }
  // 北向 + 活跃 Top
  let activeHtml = '';
  if(c.northbound){
    const nb = c.northbound;
    activeHtml += `<div style="margin-bottom:8px"><b>北向资金</b> <span class="muted" style="font-size:10px">${esc(nb.trade_date||'-')}</span>`;
    if(nb.net_buy_amount !== undefined){
      const nb_cls = (nb.net_buy_amount||0) >= 0 ? 'up' : 'down';
      const sign = (nb.net_buy_amount||0) >= 0 ? '+' : '';
      activeHtml += `<div class="card" style="margin-top:6px;padding:8px"><div class="lbl">净买入(亿)</div><div class="val ${nb_cls}" style="font-size:20px">${sign}${fmt(nb.net_buy_amount/1e8, 2)}</div></div>`;
      if(nb.sh_index_change_pct !== undefined){
        activeHtml += `<div class="card" style="margin-top:6px;padding:8px"><div class="lbl">上证</div><div class="val ${(nb.sh_index_change_pct||0)>=0?'up':'down'}" style="font-size:16px">${(nb.sh_index_change_pct>=0?'+':'')}${fmt(nb.sh_index_change_pct,2)}%</div></div>`;
      }
    }
    activeHtml += '</div>';
  } else {
    activeHtml += '<div class="muted">无北向资金数据</div>';
  }
  if(c.active_top && c.active_top.length){
    activeHtml += '<h3 style="font-size:13px;margin:10px 0 6px;">成交额 Top 10</h3>';
    activeHtml += '<table class="tbl" style="font-size:11px"><thead><tr><th>代码</th><th>收盘</th><th>涨幅%</th><th>成交(亿)</th></tr></thead><tbody>';
    c.active_top.forEach(t=>{
      const cls = (t.change_pct||0)>=0?'up':'down';
      activeHtml += `<tr><td><code>${esc(t.symbol)}</code></td><td>${fmt(t.close,2)}</td><td class="${cls}">${(t.change_pct>=0?'+':'')}${fmt(t.change_pct,2)}%</td><td>${fmt((t.amount||0)/1e8,2)}</td></tr>`;
    });
    activeHtml += '</tbody></table>';
  }
  $('#mbActive').innerHTML = activeHtml;
}

async function loadMarket(){
  try{
    const r = await fetch('/api/market'); const d = await r.json();
    renderMarket(d);
  }catch(e){ $('#sentiBar').innerHTML='<span class="empty">市场数据加载失败: '+e+'</span>'; }
}
async function loadPerf(){
  try{
    const r = await fetch('/api/perf'); const d = await r.json();
    renderPerf(d);
  }catch(e){ $('#pfMetrics').innerHTML='<span class="empty">绩效数据加载失败: '+e+'</span>'; }
}

async function loadTargetPlan(){
  try{
    const r = await fetch('/api/target_plan'); const d = await r.json();
    renderTargetPlan(d);
  }catch(e){ $('#pfTargetPlan').innerHTML='<span class="empty">加载失败: '+e+'</span>'; }
}
function renderTargetPlan(d){
  const box = $('#pfTargetPlan');
  if(!d.ok){ box.innerHTML = '<span class="empty">'+esc(d.error||'plan 不存在')+(d.hint?(' ('+d.hint+')'):'')+'</span>'; return; }
  const p = d.plan || {};
  const top = p.top_n || [];
  const w = p.weights_used || {};
  let html = `<div class="muted" style="font-size:12px;margin-bottom:8px">day=${esc(p.day)} · universe=${p.universe_size} · ${esc(p.method||'')} · 生成于 ${esc(p.generated_at||'')}</div>`;
  html += '<div style="margin-bottom:8px"><b>权重:</b> ';
  Object.keys(w).forEach(k=>{ html += `<span class="badge" style="margin-right:4px">${esc(k)}=${fmt(w[k],3)}</span>`; });
  html += '</div>';
  html += '<table class="tbl" style="font-size:12px"><thead><tr><th>#</th><th>canon</th><th>价</th><th>换手%</th><th>drl_score</th><th>权重</th><th>信号</th></tr></thead><tbody>';
  top.forEach((t,i)=>{
    html += `<tr><td>${i+1}</td><td>${esc(t.canon)}</td><td>${fmt(t.price,3)}</td><td>${fmt(t.turnover,2)}</td><td>${fmt(t.drl_score,4)}</td><td>${fmt(t.target_weight,3)}</td><td>${esc(t.source_signal||'')}</td></tr>`;
  });
  html += '</tbody></table>';
  box.innerHTML = html;
}

async function loadHealth(){
  try{
    const r = await fetch('/api/health'); const d = await r.json();
    renderHealth(d);
  }catch(e){ $('#pfHealth').innerHTML='<span class="empty">加载失败: '+e+'</span>'; }
}
function renderHealth(d){
  const box = $('#pfHealth');
  if(!d.ok && !d.level){ box.innerHTML = '<span class="empty">'+esc(d.error||'health 不存在')+'</span>'; return; }
  const lvl = d.level || (d.ok?'OK':'DEGRADED');
  const cls = lvl==='OK' ? 'up' : (lvl==='DEGRADED' ? 'warn' : 'down');
  let html = `<div style="margin-bottom:8px"><span class="badge ${cls}" style="font-size:13px;padding:6px 12px">${esc(lvl)}</span> <span class="muted">${esc(d.summary||'')} · ${esc(d.generated_at||'')}</span></div>`;
  html += '<table class="tbl" style="font-size:12px"><thead><tr><th>检查项</th><th>状态</th><th>耗时(ms)</th><th>详情</th></tr></thead><tbody>';
  (d.checks||[]).forEach(c=>{
    const c_cls = c.status==='OK' ? 'up' : (c.status==='WARN' ? 'warn' : 'down');
    let detail = '';
    if(typeof c.detail === 'object' && c.detail !== null){
      detail = JSON.stringify(c.detail, null, 0).slice(0, 240);
    } else { detail = String(c.detail||''); }
    html += `<tr><td>${esc(c.name)}</td><td><span class="badge ${c_cls}">${esc(c.status)}</span></td><td>${c.ms||0}</td><td><code style="font-size:10px">${esc(detail)}</code></td></tr>`;
  });
  html += '</tbody></table>';
  box.innerHTML = html;
}

async function loadViews(){
  try{
    const r = await fetch('/api/views'); const d = await r.json();
    renderViews(d);
  }catch(e){ $('#pfViews').innerHTML='<span class="empty">加载失败: '+e+'</span>'; }
}
function renderViews(d){
  const box = $('#pfViews');
  if(!d.ok && !d.views){ box.innerHTML = '<span class="empty">'+esc(d.error||'views 不存在')+'</span>'; return; }
  let html = `<div class="muted" style="font-size:12px;margin-bottom:8px">${esc(d.duckdb_path||'')} · 生成于 ${esc(d.generated_at||'')}</div>`;
  html += '<table class="tbl" style="font-size:12px"><thead><tr><th>视图</th><th>状态</th><th>行数</th><th>Parquet</th><th>耗时(ms)</th></tr></thead><tbody>';
  const views = d.views || {};
  Object.keys(views).forEach(name=>{
    const v = views[name];
    const c_cls = v.ok ? 'up' : 'down';
    html += `<tr><td>${esc(name)}</td><td><span class="badge ${c_cls}">${v.ok?'OK':'FAIL'}</span></td><td>${v.rows||0}</td><td><code style="font-size:10px">${esc((v.parquet||'').replace(/^.*[\\/]/,''))}</code></td><td>${v.ms||0}</td></tr>`;
  });
  html += '</tbody></table>';
  box.innerHTML = html;
}

// 市场/绩效 tab 每30秒刷新一次(不重复加载首屏)
setInterval(()=>{ if(mkLoaded) loadMarket(); if(pfLoaded) { loadPerf(); loadTargetPlan(); loadHealth(); loadViews(); } }, 30000);
