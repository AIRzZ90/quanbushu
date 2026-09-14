const state = {
  history: [],
  timer: null,
  loading: false,
  loggedIn: false,
  dirtyFields: {},
};

const $ = (id) => document.getElementById(id);
const loginView = $("loginView");
const dashboardView = $("dashboardView");
const loginForm = $("loginForm");
const passwordInput = $("password");
const loginError = $("loginError");
const startForm = $("startForm");
const mnemonicInput = $("mnemonic");
const startMiningButton = $("startMiningButton");
const stopMiningButton = $("stopMiningButton");
const controlMessage = $("controlMessage");

function number(value, digits = 2) {
  if (value === null || value === undefined || !Number.isFinite(Number(value))) return "—";
  return Number(value).toLocaleString("zh-CN", { maximumFractionDigits: digits });
}

function hashRate(value) {
  if (value === null || value === undefined || !Number.isFinite(Number(value))) return "—";
  const n = Number(value);
  if (n >= 1e12) return `${number(n / 1e12, 3)} TH/s`;
  if (n >= 1e9) return `${number(n / 1e9, 3)} GH/s`;
  if (n >= 1e6) return `${number(n / 1e6, 3)} MH/s`;
  return `${number(n, 0)} H/s`;
}

function compact(value) {
  if (value === null || value === undefined || !Number.isFinite(Number(value))) return "—";
  const n = Number(value);
  if (Math.abs(n) >= 1e12) return `${number(n / 1e12, 3)} T`;
  if (Math.abs(n) >= 1e9) return `${number(n / 1e9, 3)} G`;
  if (Math.abs(n) >= 1e6) return `${number(n / 1e6, 3)} M`;
  if (Math.abs(n) >= 1e3) return `${number(n / 1e3, 3)} K`;
  return number(n, 0);
}

function qtc(value, signed = false) {
  if (value === null || value === undefined || !Number.isFinite(Number(value))) return "—";
  const n = Number(value);
  const prefix = signed && n > 0 ? "+" : "";
  if (n !== 0 && Math.abs(n) < 0.000001) return `${prefix}${n.toExponential(3)} QTC`;
  return `${prefix}${n.toLocaleString("en-US", { maximumFractionDigits: 8 })} QTC`;
}

function timestamp(value) {
  if (!value) return "—";
  const date = new Date(Number(value));
  return Number.isNaN(date.getTime()) ? "—" : date.toLocaleTimeString("zh-CN", { hour12: false });
}

function dateTime(value) {
  if (!value) return "—";
  const date = new Date(Number(value));
  return Number.isNaN(date.getTime()) ? "—" : date.toLocaleString("zh-CN", { hour12: false });
}

function shortAddress(value) {
  if (!value) return "账户 —";
  return `账户 ${value.slice(0, 10)}…${value.slice(-8)}`;
}

function setText(id, value) {
  const element = $(id);
  if (element) element.textContent = value;
}

function setFieldDefault(id, value) {
  const element = $(id);
  if (!element || value === null || value === undefined || state.dirtyFields[id]) return;
  if (document.activeElement === element) return;
  element.value = String(value);
}

function setBadge(id, label, tone) {
  const element = $(id);
  if (!element) return;
  element.textContent = label;
  element.className = `badge ${tone || ""}`.trim();
}

function setControlMessage(message = "", tone = "") {
  controlMessage.textContent = message;
  controlMessage.className = `control-message ${tone}`.trim();
}

function setStatus(status) {
  const tone = status.state === "online" ? "online" : status.state === "offline" ? "offline" : "warning";
  const label = status.state === "online" ? "运行中" : status.state === "syncing" ? "同步中" : status.state === "offline" ? "已停止" : "需检查";
  const dot = $("statusDot");
  dot.className = `status-dot ${tone}`;
  setText("statusText", label);
  setBadge("minerState", label, tone);
  setBadge("syncBadge", status.syncing ? "节点同步中" : "节点已同步", status.syncing ? "warning" : "online");
}

function renderActivity(activity) {
  const list = $("activityList");
  const entries = activity?.recent || [];
  if (!entries.length) {
    list.innerHTML = '<p class="empty-state">暂无新的日志活动</p>';
    return;
  }
  list.innerHTML = entries.map((entry) => (
    `<div class="activity-item"><span>${escapeHtml(entry.label || "活动")}</span><time>${escapeHtml(entry.time || "—")}</time></div>`
  )).join("");
}

function escapeHtml(value) {
  return String(value).replace(/[&<>"']/g, (char) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#039;",
  }[char]));
}

function render(data) {
  const miner = data.miner || {};
  const node = data.node || {};
  const gpu = data.gpu || {};
  const output = data.output || {};
  const status = data.status || {};
  const control = data.control || {};

  setStatus(status);
  setText("lastUpdate", timestamp(data.server_time));
  setText("hashRate", hashRate(miner.hash_rate_hs));
  setText("hashRateSub", `GPU ${hashRate(miner.gpu_hash_rate_hs)}`);
  setText("averageHashRate", hashRate(miner.average_hash_rate_hs));
  setText("sessionHashes", `监控累计 ${compact(miner.session_hashes)} hashes`);
  setText("estimatedDaily", qtc(miner.estimated_daily_qtc));
  setText("expectedBlocks", `预期出块 ${number(miner.expected_blocks_day, 4)} / 天`);
  setText("observedOutput", qtc(output.observed_balance_delta_qtc, true));
  setText("monitorStarted", `监控开始 ${dateTime(output.monitor_started_at)}`);
  setText("hashesPerWatt", miner.hashes_per_watt ? `${compact(miner.hashes_per_watt)} H/s/W` : "—");
  setText("energyDay", miner.energy_kwh_day ? `日耗电约 ${number(miner.energy_kwh_day, 2)} kWh` : "功耗 —");
  setText("balance", qtc(output.balance_qtc));
  setText("availableBalance", `可用 ${qtc(output.available_qtc)}`);

  setText("gpuName", gpu.name || "—");
  setText("gpuUtilization", gpu.utilization_percent == null ? "—" : `${number(gpu.utilization_percent, 0)}%`);
  setText("gpuThermals", `${gpu.temperature_c == null ? "—" : `${number(gpu.temperature_c, 0)} °C`} / ${gpu.power_w == null ? "—" : `${number(gpu.power_w, 1)} W`}`);
  setText("gpuMemory", gpu.memory_used_mb == null ? "—" : `${number(gpu.memory_used_mb, 0)} / ${number(gpu.memory_total_mb, 0)} MB`);
  setText("activeJobs", `${number(miner.active_jobs, 0)} 个 · ${number(miner.workers, 0)} worker`);
  setText("totalHashes", compact(miner.hashes_total));
  const controlLabel = control.miner_running
    ? `运行中 · ${number(control.cpu_workers, 0)}C / ${number(control.gpu_devices, 0)}G`
    : control.node_running
      ? "节点运行中"
      : "已停止";
  setBadge("controlState", controlLabel, control.miner_running ? "online" : control.node_running ? "warning" : "offline");
  setFieldDefault("nodeName", control.node_name);
  setFieldDefault("cpuWorkers", control.cpu_workers);
  setFieldDefault("gpuDevices", control.gpu_devices);

  setText("chainHeight", number(node.height, 0));
  setText("peers", number(node.peers, 0));
  setText("blockTime", node.block_time_seconds == null ? "—" : `${number(node.block_time_seconds, 2)} 秒`);
  setText("difficulty", compact(node.difficulty));
  setText("networkHashRate", hashRate(node.difficulty && node.block_time_seconds ? node.difficulty / node.block_time_seconds : null));
  setText("networkShare", miner.network_share_percent == null ? "—" : `${number(miner.network_share_percent, 5)}%`);
  setText("blockReward", qtc(output.reward_qtc));
  setText("rewardSource", output.reward_source || "—");
  setText("walletAddress", shortAddress(output.wallet_address));

  const banner = $("errorBanner");
  const errors = status.errors || [];
  if (errors.length) {
    banner.hidden = false;
    banner.textContent = errors.join(" · ");
  } else {
    banner.hidden = true;
    banner.textContent = "";
  }

  renderActivity(data.activity);
  state.history.push({
    time: Date.now(),
    hash: Number(miner.hash_rate_hs) || 0,
    temperature: Number(gpu.temperature_c) || 0,
    power: Number(gpu.power_w) || 0,
  });
  if (state.history.length > 60) state.history.shift();
  drawChart();
}

async function request(path, options = {}) {
  const response = await fetch(path, {
    credentials: "same-origin",
    cache: "no-store",
    ...options,
  });
  let body = null;
  try { body = await response.json(); } catch { body = null; }
  return { response, body };
}

function showLogin(message = "") {
  state.loggedIn = false;
  if (state.timer) window.clearInterval(state.timer);
  state.timer = null;
  dashboardView.hidden = true;
  loginView.hidden = false;
  loginError.textContent = message;
  mnemonicInput.value = "";
  setControlMessage("");
  passwordInput.focus();
}

function showDashboard() {
  state.loggedIn = true;
  loginView.hidden = true;
  dashboardView.hidden = false;
}

async function loadStatus() {
  if (!state.loggedIn || state.loading) return;
  state.loading = true;
  try {
    const { response, body } = await request("/api/status");
    if (response.status === 401) {
      showLogin("登录状态已过期。");
      return;
    }
    if (!response.ok || !body?.ok) {
      const message = body?.error || `监控接口返回 ${response.status}`;
      $("errorBanner").hidden = false;
      $("errorBanner").textContent = message;
      return;
    }
    render(body);
  } catch {
    $("errorBanner").hidden = false;
    $("errorBanner").textContent = "暂时无法连接监控服务。";
  } finally {
    state.loading = false;
  }
}

async function login(event) {
  event.preventDefault();
  loginError.textContent = "";
  const password = passwordInput.value;
  passwordInput.value = "";
  const submit = loginForm.querySelector("button");
  submit.disabled = true;
  try {
    const { response, body } = await request("/api/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ password }),
    });
    if (!response.ok || !body?.ok) {
      loginError.textContent = body?.error || "登录失败。";
      return;
    }
    showDashboard();
    await loadStatus();
    state.timer = window.setInterval(loadStatus, 5000);
  } catch {
    loginError.textContent = "无法连接监控服务。";
  } finally {
    submit.disabled = false;
  }
}

async function logout() {
  await request("/api/logout", { method: "POST" });
  state.history = [];
  showLogin();
}

async function startMining(event) {
  event.preventDefault();
  setControlMessage("");
  const phrase = mnemonicInput.value.trim();
  const words = phrase ? phrase.split(/\s+/) : [];
  if (words.length !== 24) {
    setControlMessage(`助记词必须是 24 个单词，当前为 ${words.length} 个。`, "error");
    mnemonicInput.focus();
    return;
  }
  if (window.location.protocol !== "https:") {
    setControlMessage("启动控制只接受 HTTPS 连接。", "error");
    return;
  }
  const payload = {
    mnemonic: phrase,
    node_name: $("nodeName").value.trim(),
    wallet_index: Number($("walletIndex").value),
    cpu_workers: Number($("cpuWorkers").value),
    gpu_devices: Number($("gpuDevices").value),
  };
  startMiningButton.disabled = true;
  stopMiningButton.disabled = true;
  setControlMessage("正在派生奖励地址并启动节点，首次启动可能需要等待同步接口…");
  try {
    const { response, body } = await request("/api/start", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    if (!response.ok || !body?.ok) {
      setControlMessage(body?.error || `启动接口返回 ${response.status}`, "error");
      return;
    }
    const address = body.address ? `奖励地址 ${body.address}。` : "";
    setControlMessage(`${address}节点和矿工已启动；首次启动请等待节点同步完成，期间无需重复点击。`, "success");
    await loadStatus();
  } catch {
    setControlMessage("无法连接启动控制服务。", "error");
  } finally {
    mnemonicInput.value = "";
    startMiningButton.disabled = false;
    stopMiningButton.disabled = false;
  }
}

async function stopMining() {
  setControlMessage("正在停止节点和矿工…");
  startMiningButton.disabled = true;
  stopMiningButton.disabled = true;
  try {
    const { response, body } = await request("/api/stop", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: "{}",
    });
    if (!response.ok || !body?.ok) {
      setControlMessage(body?.error || `停止接口返回 ${response.status}`, "error");
      return;
    }
    setControlMessage("节点和矿工已停止。", "success");
    await loadStatus();
  } catch {
    setControlMessage("无法连接停止控制服务。", "error");
  } finally {
    startMiningButton.disabled = false;
    stopMiningButton.disabled = false;
  }
}

function drawChart() {
  const canvas = $("hashChart");
  const rect = canvas.getBoundingClientRect();
  if (!rect.width || !rect.height) return;
  const ratio = window.devicePixelRatio || 1;
  const width = Math.round(rect.width * ratio);
  const height = Math.round(rect.height * ratio);
  if (canvas.width !== width || canvas.height !== height) {
    canvas.width = width;
    canvas.height = height;
  }
  const ctx = canvas.getContext("2d");
  ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
  ctx.clearRect(0, 0, rect.width, rect.height);
  const padding = { left: 44, right: 12, top: 14, bottom: 26 };
  const plotWidth = rect.width - padding.left - padding.right;
  const plotHeight = rect.height - padding.top - padding.bottom;
  const history = state.history;
  if (!history.length) return;
  const maxHash = Math.max(1, ...history.map((point) => point.hash));
  const maxTemp = Math.max(1, ...history.map((point) => point.temperature));
  const maxPower = Math.max(1, ...history.map((point) => point.power));
  ctx.strokeStyle = "#23303a";
  ctx.lineWidth = 1;
  ctx.font = "10px Inter, sans-serif";
  ctx.fillStyle = "#71818d";
  for (let index = 0; index <= 4; index += 1) {
    const y = padding.top + plotHeight * index / 4;
    ctx.beginPath();
    ctx.moveTo(padding.left, y);
    ctx.lineTo(rect.width - padding.right, y);
    ctx.stroke();
    const label = hashRate(maxHash * (1 - index / 4)).replace(" /s", "");
    ctx.fillText(label, 0, y + 3);
  }
  ctx.fillText("现在", rect.width - 30, rect.height - 6);
  ctx.fillText("算力", padding.left, rect.height - 6);

  const draw = (key, max, color) => {
    if (history.length < 2) return;
    ctx.strokeStyle = color;
    ctx.lineWidth = 2;
    ctx.beginPath();
    history.forEach((point, index) => {
      const x = padding.left + plotWidth * index / Math.max(1, history.length - 1);
      const y = padding.top + plotHeight * (1 - point[key] / max);
      if (index === 0) ctx.moveTo(x, y);
      else ctx.lineTo(x, y);
    });
    ctx.stroke();
  };
  draw("hash", maxHash, "#f3a45f");
  draw("temperature", maxTemp, "#83b8ff");
  draw("power", maxPower, "#79d6a4");
}

loginForm.addEventListener("submit", login);
startForm.addEventListener("submit", startMining);
stopMiningButton.addEventListener("click", stopMining);
["nodeName", "walletIndex", "cpuWorkers", "gpuDevices"].forEach((id) => {
  $(id).addEventListener("input", () => { state.dirtyFields[id] = true; });
});
$("refreshButton").addEventListener("click", loadStatus);
$("logoutButton").addEventListener("click", logout);
window.addEventListener("resize", drawChart);
window.addEventListener("pagehide", () => { mnemonicInput.value = ""; });

request("/api/status").then(({ response }) => {
  if (response.status === 401) showLogin();
  else {
    showDashboard();
    loadStatus();
    state.timer = window.setInterval(loadStatus, 5000);
  }
}).catch(() => showLogin());
