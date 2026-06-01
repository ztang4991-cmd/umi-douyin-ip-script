const state = {
  token: localStorage.getItem("mamba_token") || "",
  user: null,
  view: "dashboard",
  users: [],
  leads: [],
  opportunities: [],
  venues: [],
  bookings: [],
  orders: [],
};

const $ = (sel) => document.querySelector(sel);
const fmtMoney = (n) => `¥${Number(n || 0).toLocaleString("zh-CN", { maximumFractionDigits: 0 })}`;
const today = () => new Date().toISOString().slice(0, 10);

function toast(message) {
  const el = $("#toast");
  el.textContent = message;
  el.style.display = "block";
  setTimeout(() => (el.style.display = "none"), 2600);
}

async function api(path, options = {}) {
  const headers = options.headers || {};
  if (!(options.body instanceof FormData)) headers["Content-Type"] = "application/json";
  if (state.token) headers.Authorization = `Bearer ${state.token}`;
  const res = await fetch(path, { ...options, headers });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || "请求失败");
  return data;
}

function formData(form) {
  return Object.fromEntries(new FormData(form).entries());
}

function setView(view) {
  state.view = view;
  document.querySelectorAll(".view").forEach((el) => el.classList.add("hidden"));
  document.querySelector(`#${view}`).classList.remove("hidden");
  document.querySelectorAll("nav button").forEach((btn) => btn.classList.toggle("active", btn.dataset.view === view));
  $("#viewTitle").textContent = { dashboard: "老板看板", leads: "客资 CRM", opportunities: "商机", schedule: "档期", orders: "订单合同" }[view];
  loadView();
}

async function boot() {
  if (!state.token) return showLogin();
  try {
    const me = await api("/api/me");
    state.user = me.user;
    showApp();
    await loadView();
  } catch {
    localStorage.removeItem("mamba_token");
    state.token = "";
    showLogin();
  }
}

function showLogin() {
  $("#loginView").classList.remove("hidden");
  $("#appView").classList.add("hidden");
}

function showApp() {
  $("#loginView").classList.add("hidden");
  $("#appView").classList.remove("hidden");
  $("#tenantName").textContent = state.user.tenant_name;
  $("#userInfo").textContent = `${state.user.name} · ${state.user.role}`;
}

async function loadCommon() {
  const [users, venues] = await Promise.all([api("/api/users"), api("/api/venues")]);
  state.users = users.users;
  state.venues = venues.venues;
}

async function loadView() {
  if (!state.user) return;
  if (state.view === "dashboard") return renderDashboard();
  if (state.view === "leads") return renderLeads();
  if (state.view === "opportunities") return renderOpportunities();
  if (state.view === "schedule") return renderSchedule();
  if (state.view === "orders") return renderOrders();
}

function userOptions(selected = "") {
  return state.users.map((u) => `<option value="${u.id}" ${String(selected) === String(u.id) ? "selected" : ""}>${u.name}（${u.role}）</option>`).join("");
}

function venueOptions(selected = "") {
  return state.venues.map((v) => `<option value="${v.id}" ${String(selected) === String(v.id) ? "selected" : ""}>${v.name}</option>`).join("");
}

function assistantHtml(a) {
  return `<div class="panel assistant-box">
    <h2>销售跟进助手</h2>
    <p class="muted">辅助建议，不自动报价、不承诺档期/优惠、不判定返佣归属。</p>
    <p><b>意向等级：</b>${a.intention_level}　<b>心理阶段：</b>${a.psychology_stage}</p>
    <p><b>关注点：</b>${a.concerns}</p>
    <p><b>异议类型：</b>${a.objections}</p>
    <p><b>触达节奏：</b>${a.cadence}</p>
    <h2>固定话术模板</h2>
    <ul>${a.templates.map((t) => `<li>${t}</li>`).join("")}</ul>
  </div>`;
}

async function renderDashboard() {
  const d = await api("/api/dashboard");
  $("#dashboard").innerHTML = `
    <div class="grid cols-4">
      <button class="card metric" data-drill="leads"><span>线索数</span><strong>${d.lead_count}</strong><span>点击进入明细</span></button>
      <button class="card metric" data-drill="orders"><span>订单数</span><strong>${d.order_count}</strong><span>点击进入明细</span></button>
      <button class="card metric" data-drill="orders"><span>成交额</span><strong>${fmtMoney(d.order_amount)}</strong><span>含意向/已定/完成</span></button>
      <button class="card metric" data-drill="third_party"><span>预计返佣</span><strong>${fmtMoney(d.estimated_commission)}</strong><span>成本率 ${d.commission_cost_rate.toFixed(1)}%</span></button>
    </div>
    <div class="grid cols-2" style="margin-top:14px">
      <div class="panel">
        <h2>渠道返佣成本卡</h2>
        <p>第三方成交额：${fmtMoney(d.third_party_amount)}　成交占比：${d.third_party_share.toFixed(1)}%</p>
        <table><thead><tr><th>渠道</th><th>订单</th><th>成交额</th><th>预计返佣</th></tr></thead><tbody>
          ${d.channels.map((c) => `<tr><td>${c.channel}</td><td>${c.orders}</td><td>${fmtMoney(c.amount)}</td><td>${fmtMoney(c.commission)}</td></tr>`).join("")}
        </tbody></table>
      </div>
      <div class="panel">
        <h2>销售看板</h2>
        <table><thead><tr><th>销售</th><th>线索</th><th>订单</th><th>金额</th></tr></thead><tbody>
          ${d.sales.map((s) => `<tr><td>${s.name}</td><td>${s.leads}</td><td>${s.orders}</td><td>${fmtMoney(s.amount)}</td></tr>`).join("")}
        </tbody></table>
      </div>
    </div>
    <div class="panel" style="margin-top:14px">
      <h2>超期未跟进提醒</h2>
      <table><thead><tr><th>客户</th><th>手机</th><th>应跟进日期</th></tr></thead><tbody>
        ${d.overdue_followups.map((l) => `<tr><td>${l.name}</td><td>${l.phone || ""}</td><td><span class="tag danger">${l.next_follow_up_at}</span></td></tr>`).join("") || `<tr><td colspan="3">暂无超期</td></tr>`}
      </tbody></table>
    </div>`;
  $("#dashboard").querySelectorAll("[data-drill]").forEach((btn) => btn.onclick = () => setView(btn.dataset.drill === "leads" ? "leads" : "orders"));
}

async function renderLeads() {
  await loadCommon();
  const data = await api("/api/leads");
  state.leads = data.leads;
  $("#leads").innerHTML = `
    <div class="toolbar"><div class="filters"><span class="muted">手机/微信/姓名+婚期基础查重已内置</span></div><button id="newLeadBtn">新增线索</button></div>
    <table><thead><tr><th>客户</th><th>婚期</th><th>归属</th><th>状态</th><th>第三方</th><th>下次跟进</th><th>操作</th></tr></thead><tbody>
      ${state.leads.map((l) => `<tr>
        <td><b>${l.name}</b><br><span class="muted">${l.phone || ""} ${l.wechat || ""}</span></td>
        <td>${l.wedding_date || ""}</td><td>${l.owner_name || "公海"}</td>
        <td><span class="tag ${l.pool_status === "public" ? "warn" : "ok"}">${l.status}/${l.pool_status}</span></td>
        <td>${l.third_party_name || ""}<br><span class="muted">${l.referrer_name || ""}</span></td>
        <td>${l.next_follow_up_at ? `<span class="tag ${l.next_follow_up_at < today() ? "danger" : ""}">${l.next_follow_up_at}</span>` : ""}</td>
        <td><button data-open-lead="${l.id}">详情</button></td>
      </tr>`).join("")}
    </tbody></table>`;
  $("#newLeadBtn").onclick = () => openLeadForm();
  $("#leads").querySelectorAll("[data-open-lead]").forEach((btn) => btn.onclick = () => openLeadDetail(btn.dataset.openLead));
}

function openLeadForm() {
  $("#modalBody").innerHTML = `
    <h2>新增线索</h2>
    <form id="leadForm" class="form-grid">
      <label>客户姓名<input name="name" required /></label>
      <label>手机<input name="phone" /></label>
      <label>微信<input name="wechat" /></label>
      <label>婚期<input name="wedding_date" type="date" /></label>
      <label>桌数<input name="guest_count" type="number" min="0" /></label>
      <label>来源<input name="source" placeholder="抖音/企微/转介绍" /></label>
      <label>归属销售<select name="owner_user_id">${userOptions(state.user.id)}</select></label>
      <label>公海状态<select name="pool_status"><option value="private">私有</option><option value="public">公海</option></select></label>
      <label>意向等级<select name="intention_level"><option>A</option><option selected>B</option><option>C</option></select></label>
      <label>心理阶段<input name="psychology_stage" value="观望比较" /></label>
      <label class="full">关注点<input name="concerns" placeholder="预算、厅型、档期、服务" /></label>
      <label class="full">异议类型<input name="objections" placeholder="价格顾虑/家人未定/竞品比较" /></label>
      <label>第三方名称<input name="third_party_name" /></label>
      <label>推荐人<input name="referrer_name" /></label>
      <label>报备时间<input name="reported_at" type="datetime-local" /></label>
      <label>返佣规则<input name="commission_rule" placeholder="按成交额 3% / 固定 3000" /></label>
      <label>下次跟进<input name="next_follow_up_at" type="date" /></label>
      <div class="full actions"><button type="button" id="dupBtn">先查重</button><button type="submit">保存线索</button></div>
    </form>
    <div id="dupResult" class="panel hidden"></div>`;
  $("#modal").showModal();
  $("#dupBtn").onclick = async () => {
    const data = formData($("#leadForm"));
    const q = new URLSearchParams(data).toString();
    const dup = await api(`/api/duplicates?${q}`);
    $("#dupResult").classList.remove("hidden");
    $("#dupResult").innerHTML = `<h2>查重结果</h2>${dup.duplicates.length ? dup.duplicates.map((d) => `<p>#${d.id} ${d.name} ${d.phone || ""} ${d.wechat || ""} ${d.wedding_date || ""}</p>`).join("") : "<p>未发现重复线索</p>"}`;
  };
  $("#leadForm").onsubmit = async (e) => {
    e.preventDefault();
    await api("/api/leads", { method: "POST", body: JSON.stringify(formData(e.target)) });
    $("#modal").close();
    toast("线索已创建");
    renderLeads();
  };
}

async function openLeadDetail(id) {
  const { lead } = await api(`/api/leads/${id}`);
  $("#modalBody").innerHTML = `
    <div class="detail-layout">
      <div class="grid">
        <div class="panel">
          <h2>${lead.name}</h2>
          <p>${lead.phone || ""} ${lead.wechat || ""}　婚期：${lead.wedding_date || "未定"}</p>
          <p>第三方：${lead.third_party_name || "无"}　推荐人：${lead.referrer_name || ""}　返佣规则：${lead.commission_rule || ""}</p>
          <div class="actions">
            <button id="convertBtn">转商机</button>
            <button id="publicBtn" class="ghost">${lead.pool_status === "public" ? "收回私有" : "放入公海"}</button>
          </div>
        </div>
        <div class="panel">
          <h2>记录跟进</h2>
          <form id="followForm" class="stack">
            <select name="contact_method"><option>微信</option><option>电话</option><option>到店</option><option>企业微信</option></select>
            <textarea name="content" placeholder="真实跟进内容" required></textarea>
            <input name="next_follow_up_at" type="date" />
            <button>保存跟进</button>
          </form>
        </div>
        <div class="panel">
          <h2>跟进记录</h2>
          <div class="timeline">${lead.followups.map((f) => `<div class="timeline-item"><b>${f.user_name}</b> · ${f.contact_method} · ${f.created_at}<p>${f.content}</p></div>`).join("") || "<p>暂无记录</p>"}</div>
        </div>
      </div>
      ${assistantHtml(lead.assistant)}
    </div>`;
  $("#modal").showModal();
  $("#followForm").onsubmit = async (e) => {
    e.preventDefault();
    await api(`/api/leads/${id}/followups`, { method: "POST", body: JSON.stringify(formData(e.target)) });
    toast("跟进已记录");
    openLeadDetail(id);
  };
  $("#convertBtn").onclick = async () => {
    await api(`/api/leads/${id}/convert`, { method: "POST", body: JSON.stringify({ estimated_amount: 0 }) });
    toast("已转为商机");
    $("#modal").close();
    setView("opportunities");
  };
  $("#publicBtn").onclick = async () => {
    await api(`/api/leads/${id}`, { method: "PUT", body: JSON.stringify({ pool_status: lead.pool_status === "public" ? "private" : "public" }) });
    openLeadDetail(id);
  };
}

async function renderOpportunities() {
  await loadCommon();
  const data = await api("/api/opportunities");
  state.opportunities = data.opportunities;
  $("#opportunities").innerHTML = `
    <table><thead><tr><th>客户</th><th>阶段</th><th>销售</th><th>预计金额</th><th>第三方</th><th>操作</th></tr></thead><tbody>
      ${state.opportunities.map((o) => `<tr>
        <td>${o.lead_name}<br><span class="muted">${o.phone || ""}</span></td><td>${o.stage}</td><td>${o.owner_name || ""}</td>
        <td>${fmtMoney(o.estimated_amount)}</td><td>${o.third_party_name || ""}</td>
        <td class="actions"><button data-order-opp="${o.id}">转订单</button><button data-assist-opp="${o.lead_id}">助手</button></td>
      </tr>`).join("")}
    </tbody></table>`;
  $("#opportunities").querySelectorAll("[data-order-opp]").forEach((btn) => btn.onclick = () => openOrderFromOpportunity(btn.dataset.orderOpp));
  $("#opportunities").querySelectorAll("[data-assist-opp]").forEach((btn) => btn.onclick = () => openLeadDetail(btn.dataset.assistOpp));
}

function openOrderFromOpportunity(oppId) {
  $("#modalBody").innerHTML = `
    <h2>商机转订单</h2>
    <form id="orderForm" class="form-grid">
      <input type="hidden" name="opportunity_id" value="${oppId}" />
      <label>状态<select name="status"><option>意向</option><option>已定</option><option>完成</option><option>退订</option><option>作废</option></select></label>
      <label>订单金额<input name="total_amount" type="number" min="0" step="0.01" /></label>
      <label>桌数<input name="table_count" type="number" min="0" /></label>
      <label>返佣比例 %<input name="commission_rate" type="number" min="0" step="0.01" /></label>
      <label class="full">返佣方式<input name="commission_method" placeholder="按比例/固定金额/待人工确认" /></label>
      <div class="full"><button>生成订单</button></div>
    </form>`;
  $("#modal").showModal();
  $("#orderForm").onsubmit = async (e) => {
    e.preventDefault();
    await api("/api/orders", { method: "POST", body: JSON.stringify(formData(e.target)) });
    $("#modal").close();
    toast("订单已生成");
    setView("orders");
  };
}

async function renderSchedule() {
  await loadCommon();
  const start = today();
  const data = await api(`/api/bookings?start=${start}`);
  state.bookings = data.bookings;
  const days = Array.from({ length: 14 }, (_, i) => {
    const d = new Date();
    d.setDate(d.getDate() + i);
    return d.toISOString().slice(0, 10);
  });
  $("#schedule").innerHTML = `
    <div class="toolbar"><span class="muted">同一资源同一日期同一时间段不可重复占用</span><button id="newBookingBtn">锁档期</button></div>
    <div class="calendar">${days.map((day) => `<div class="day"><h3>${day}</h3>${state.bookings.filter((b) => b.booking_date === day).map((b) => `<span class="booking">${b.venue_name}<br>${b.time_slot} · ${bookingTypeText(b.booking_type)}<br>${b.note || ""}</span>`).join("")}</div>`).join("")}</div>`;
  $("#newBookingBtn").onclick = openBookingForm;
}

function bookingTypeText(t) {
  return { meal: "正餐", tasting: "试菜", temporary_lock: "临时锁台" }[t] || t;
}

function openBookingForm() {
  $("#modalBody").innerHTML = `
    <h2>锁定档期</h2>
    <form id="bookingForm" class="form-grid">
      <label>资源<select name="venue_id">${venueOptions()}</select></label>
      <label>占用类型<select name="booking_type"><option value="meal">正餐</option><option value="tasting">试菜</option><option value="temporary_lock">临时锁台</option></select></label>
      <label>日期<input name="booking_date" type="date" value="${today()}" required /></label>
      <label>时间段<select name="time_slot"><option>午宴</option><option>晚宴</option><option>全天</option></select></label>
      <label class="full">备注<input name="note" placeholder="客户/订单/内部确认说明" /></label>
      <div class="full"><button>确认锁定</button></div>
    </form>`;
  $("#modal").showModal();
  $("#bookingForm").onsubmit = async (e) => {
    e.preventDefault();
    await api("/api/bookings", { method: "POST", body: JSON.stringify(formData(e.target)) });
    $("#modal").close();
    toast("档期已锁定");
    renderSchedule();
  };
}

async function renderOrders() {
  const data = await api("/api/orders");
  state.orders = data.orders;
  $("#orders").innerHTML = `
    <table><thead><tr><th>客户</th><th>状态</th><th>金额/已收</th><th>第三方返佣</th><th>合同</th><th>操作</th></tr></thead><tbody>
      ${state.orders.map((o) => `<tr>
        <td>${o.customer_name}<br><span class="muted">${o.phone || ""}</span></td>
        <td><span class="tag">${o.status}</span></td><td>${fmtMoney(o.total_amount)}<br><span class="muted">已收 ${fmtMoney(o.paid_amount)}</span></td>
        <td>${o.third_party_name || "无"}<br><span class="muted">${o.commission_method || ""} ${o.commission_rate || 0}% · ${fmtMoney(o.estimated_commission)} · ${o.commission_status}</span></td>
        <td>${o.contract_pdf_path ? `<a href="/${o.contract_pdf_path}" target="_blank">查看 PDF</a>` : "未上传"}</td>
        <td class="actions"><button data-pay="${o.id}">收款</button><button data-pdf="${o.id}">上传合同</button><button data-assist-order="${o.lead_id}">助手</button></td>
      </tr>`).join("")}
    </tbody></table>`;
  $("#orders").querySelectorAll("[data-pay]").forEach((btn) => btn.onclick = () => openPaymentForm(btn.dataset.pay));
  $("#orders").querySelectorAll("[data-pdf]").forEach((btn) => btn.onclick = () => openContractUpload(btn.dataset.pdf));
  $("#orders").querySelectorAll("[data-assist-order]").forEach((btn) => btn.onclick = () => openLeadDetail(btn.dataset.assistOrder));
}

function openPaymentForm(orderId) {
  $("#modalBody").innerHTML = `
    <h2>记录收款</h2>
    <form id="paymentForm" class="form-grid">
      <label>金额<input name="amount" type="number" min="0" step="0.01" required /></label>
      <label>收款日期<input name="paid_at" type="date" value="${today()}" /></label>
      <label>方式<input name="method" value="转账" /></label>
      <label>备注<input name="note" /></label>
      <div class="full"><button>保存收款</button></div>
    </form>`;
  $("#modal").showModal();
  $("#paymentForm").onsubmit = async (e) => {
    e.preventDefault();
    await api(`/api/orders/${orderId}/payments`, { method: "POST", body: JSON.stringify(formData(e.target)) });
    $("#modal").close();
    toast("收款已记录");
    renderOrders();
  };
}

function openContractUpload(orderId) {
  $("#modalBody").innerHTML = `
    <h2>上传合同 PDF</h2>
    <form id="contractForm" class="stack">
      <input name="file" type="file" accept="application/pdf" required />
      <button>上传</button>
    </form>`;
  $("#modal").showModal();
  $("#contractForm").onsubmit = async (e) => {
    e.preventDefault();
    const body = new FormData(e.target);
    await api(`/api/orders/${orderId}/contract`, { method: "POST", body, headers: state.token ? { Authorization: `Bearer ${state.token}` } : {} });
    $("#modal").close();
    toast("合同已上传");
    renderOrders();
  };
}

$("#loginForm").onsubmit = async (e) => {
  e.preventDefault();
  try {
    const data = await api("/api/login", { method: "POST", body: JSON.stringify(formData(e.target)) });
    state.token = data.token;
    state.user = data.user;
    localStorage.setItem("mamba_token", state.token);
    showApp();
    setView("dashboard");
  } catch (err) {
    toast(err.message);
  }
};

$("#bootstrapForm").onsubmit = async (e) => {
  e.preventDefault();
  try {
    await api("/api/bootstrap", { method: "POST", body: JSON.stringify(formData(e.target)) });
    toast("门店已创建，请使用新账号登录");
    e.target.reset();
  } catch (err) {
    toast(err.message);
  }
};

$("#logoutBtn").onclick = () => {
  localStorage.removeItem("mamba_token");
  location.reload();
};

$("#refreshBtn").onclick = () => loadView().catch((err) => toast(err.message));
document.querySelectorAll("nav button").forEach((btn) => btn.onclick = () => setView(btn.dataset.view));

window.addEventListener("error", (e) => toast(e.message));
boot();
