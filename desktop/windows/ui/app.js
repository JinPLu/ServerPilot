(() => {
  "use strict";

  const view = {
    snapshot: null,
    current: null,
    observationProfiles: [],
    selectedEndpointID: null,
    filter: "all",
    sort: { key: "free", direction: "desc" },
  };
  const $ = (selector) => document.querySelector(selector);
  const $$ = (selector) => Array.from(document.querySelectorAll(selector));
  const byID = (id) => document.getElementById(id);
  const escapeHTML = (value) => String(value ?? "").replace(/[&<>'"]/g, (character) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;",
  })[character]);
  const clamp = (value, lower = 0, upper = 100) => Math.min(Math.max(Number(value) || 0, lower), upper);
  const percent = (value) => value === null || value === undefined || Number.isNaN(Number(value))
    ? "—" : `${Math.round(clamp(value))}%`;
  const plural = (count, unit) => `${Number(count) || 0} ${unit}`;

  function bridge() {
    return window.pywebview && window.pywebview.api;
  }

  async function call(method, ...args) {
    if (!bridge() || typeof bridge()[method] !== "function") {
      return { ok: false, error: { code: "desktop_bridge_unavailable", message: "Windows 桌面桥接尚未就绪。" } };
    }
    try {
      return await bridge()[method](...args);
    } catch (_) {
      return { ok: false, error: { code: "desktop_bridge_failed", message: "Windows 桌面桥接没有返回有效结果。" } };
    }
  }

  function showNotice(message, isError = false) {
    const notice = byID("notice");
    notice.textContent = message;
    notice.classList.toggle("error", isError);
    notice.hidden = false;
    window.clearTimeout(showNotice.timer);
    showNotice.timer = window.setTimeout(() => { notice.hidden = true; }, isError ? 7000 : 4600);
  }

  function failure(result) {
    const error = result && result.error;
    showNotice(error && error.message ? error.message : "操作没有完成。", true);
  }

  function stateCurrent(envelope) {
    return envelope && envelope.data && envelope.data.current ? envelope.data.current : null;
  }

  function endpointGPUs(endpointID) {
    return (view.current?.gpus || []).filter((gpu) => gpu.endpoint_id === endpointID);
  }

  function recentGPUValue(gpu, key) {
    const telemetry = gpu.telemetry || {};
    const recent = telemetry.recent_average || {};
    const value = recent[key];
    return typeof value === "number" ? value : null;
  }

  function average(values) {
    const present = values.filter((value) => typeof value === "number" && Number.isFinite(value));
    return present.length ? present.reduce((total, value) => total + value, 0) / present.length : null;
  }

  function hostMetric(endpoint, key) {
    const recent = endpoint.host_telemetry?.recent_average || {};
    const value = recent[key];
    if (typeof value !== "number") return null;
    return key === "cpu_load_fraction" ? value * 100 : value;
  }

  function hostMemoryUsedPct(endpoint) {
    const host = endpoint.host_telemetry || {};
    const limit = host.memory_limit_mib;
    const current = host.memory_current_mib;
    if (typeof limit === "number" && limit > 0 && typeof current === "number") {
      return current * 100 / limit;
    }
    return hostMetric(endpoint, "memory_used_pct");
  }

  function gpuMemoryPercent(gpu) {
    const used = gpu.telemetry?.memory_used_mib;
    const total = gpu.total_vram_mib;
    if (typeof used !== "number" || typeof total !== "number" || total <= 0) return null;
    return used * 100 / total;
  }

  function gpuMemoryLabel(gpu) {
    const used = gpu.telemetry?.memory_used_mib;
    const total = gpu.total_vram_mib;
    if (typeof used !== "number" || typeof total !== "number" || total <= 0) return "—";
    return `${Math.round(used / 1024)} / ${Math.max(1, Math.round(total / 1024))} GB`;
  }

  function isAvailable(gpu) {
    return gpu.publicly_available === true || gpu.state === "AVAILABLE";
  }

  function isKeepalive(gpu) {
    return gpu.keepalive?.actual === "ON" || gpu.keepalive?.state === "ON" || gpu.state === "KEEPALIVE";
  }

  function isErrorGPU(gpu) {
    return ["UNKNOWN_STALE", "UNKNOWN_RECOVERING", "UNHEALTHY", "CONFLICT", "ORPHANED_BUSY"].includes(gpu.state);
  }

  function gpuStatus(gpu) {
    if (isErrorGPU(gpu)) return { label: "错误", color: "#ef4d57" };
    if (isKeepalive(gpu)) return { label: "占卡", color: "#7e6ced" };
    if (isAvailable(gpu)) return { label: "空闲", color: "#39b967" };
    return { label: "繁忙", color: "#e8ad00" };
  }

  function endpointStatus(endpoint) {
    const monitor = endpoint.monitor || {};
    if (monitor.status === "ONLINE") return { label: "在线", className: "online" };
    if (endpoint.enabled === false || endpoint.lifecycle_state === "draining") return { label: "已暂停", className: "disabled" };
    return { label: "连接失败", className: "error" };
  }

  function endpointSsh(endpoint) {
    return `ssh -p ${endpoint.port || 22} ${endpoint.ssh_user || "root"}@${endpoint.host || "—"}`;
  }

  function endpointDetails(endpoint) {
    const gpus = endpointGPUs(endpoint.id);
    const leaseGPU = gpus.find((gpu) => gpu.lease && (gpu.lease.project_id || gpu.lease.task_ref));
    return {
      endpoint,
      gpus,
      free: gpus.filter(isAvailable).length,
      gpuUtil: average(gpus.map((gpu) => recentGPUValue(gpu, "gpu_utilization_pct"))),
      memory: average(gpus.map((gpu) => recentGPUValue(gpu, "memory_used_pct"))),
      cpu: hostMetric(endpoint, "cpu_load_fraction"),
      systemMemory: hostMemoryUsedPct(endpoint),
      project: leaseGPU?.lease?.project_id || "—",
      task: leaseGPU?.lease?.task_ref || (gpus.length ? "无 GPU 任务" : "CPU 节点"),
      status: endpointStatus(endpoint),
    };
  }

  function listedServerGroups() {
    return Array.isArray(view.current?.server_groups) ? view.current.server_groups : [];
  }

  function groupById(groupID) {
    if (!groupID) return null;
    return listedServerGroups().find((group) => group.id === groupID) || null;
  }

  function endpointGroupID(endpoint) {
    return typeof endpoint.server_group_id === "string" && endpoint.server_group_id
      ? endpoint.server_group_id
      : null;
  }

  function effectiveWorkspace(endpoint) {
    if (typeof endpoint.workspace_path === "string" && endpoint.workspace_path) return endpoint.workspace_path;
    if (typeof endpoint.workspace_path_override === "string" && endpoint.workspace_path_override) {
      return endpoint.workspace_path_override;
    }
    return groupById(endpointGroupID(endpoint))?.workspace_path || null;
  }

  function workspaceOverride(endpoint) {
    if (typeof endpoint.workspace_path_override === "string" && endpoint.workspace_path_override) {
      return endpoint.workspace_path_override;
    }
    const group = groupById(endpointGroupID(endpoint));
    if (group && endpoint.workspace_path && endpoint.workspace_path !== group.workspace_path) {
      return endpoint.workspace_path;
    }
    return "";
  }

  function ungroupedEndpoints() {
    return (view.current?.endpoints || []).filter((endpoint) => !endpointGroupID(endpoint));
  }

  function hasFormalGroups() {
    return listedServerGroups().length > 0 || (view.current?.endpoints || []).some(endpointGroupID);
  }

  function groupedSections(details) {
    const groups = listedServerGroups();
    if (!hasFormalGroups()) return [{ group: null, ungrouped: false, items: details }];
    const buckets = new Map(groups.map((group) => [group.id, []]));
    const extra = new Map();
    const ungrouped = [];
    details.forEach((item) => {
      const groupID = endpointGroupID(item.endpoint);
      if (!groupID) {
        ungrouped.push(item);
        return;
      }
      if (buckets.has(groupID)) {
        buckets.get(groupID).push(item);
        return;
      }
      if (!extra.has(groupID)) extra.set(groupID, []);
      extra.get(groupID).push(item);
    });
    const sections = [];
    groups.forEach((group) => {
      const items = buckets.get(group.id) || [];
      if (items.length) sections.push({ group, ungrouped: false, items });
    });
    extra.forEach((items, groupID) => {
      sections.push({ group: { id: groupID, display_name: groupID }, ungrouped: false, items });
    });
    if (ungrouped.length) sections.push({ group: null, ungrouped: true, items: ungrouped });
    return sections;
  }

  function groupCapacity(items) {
    const total = items.reduce((sum, item) => sum + item.gpus.length, 0);
    const free = items.reduce((sum, item) => sum + item.free, 0);
    return `${items.length} 台 · ${total ? `${free}/${total} 空闲` : "无 GPU"}`;
  }

  function metricTone(value) {
    if (value === null || value === undefined) return "";
    if (value >= 80) return "high";
    if (value >= 60) return "medium";
    return "";
  }

  function metric(value) {
    return `<div class="metric ${metricTone(value)}"><span>${percent(value)}</span><span class="metric-bar"><i style="--value:${clamp(value)}%"></i></span></div>`;
  }

  function filterDetails(details) {
    const query = byID("search").value.trim().toLowerCase();
    const group = groupById(endpointGroupID(details.endpoint));
    const content = [
      endpointSsh(details.endpoint),
      details.project,
      details.task,
      effectiveWorkspace(details.endpoint),
      group?.display_name,
      group?.id,
      group?.description,
      ...details.gpus.map((gpu) => gpu.name),
    ].join(" ").toLowerCase();
    if (query && !content.includes(query)) return false;
    if (view.filter === "available") return details.free > 0;
    if (view.filter === "busy") return details.gpus.some((gpu) => !isAvailable(gpu) && !isKeepalive(gpu) && !isErrorGPU(gpu));
    if (view.filter === "keepalive") return details.gpus.some(isKeepalive);
    if (view.filter === "error") return details.status.className === "error" || details.gpus.some(isErrorGPU);
    return true;
  }

  function sortDetails(items) {
    const direction = view.sort.direction === "asc" ? 1 : -1;
    const key = view.sort.key;
    const value = (item) => {
      if (key === "server") return endpointSsh(item.endpoint).toLowerCase();
      if (key === "task") return `${item.project} ${item.task}`.toLowerCase();
      if (key === "gpu") return item.gpus[0]?.name || "";
      if (key === "free") return item.free;
      return item[key] ?? -1;
    };
    return items.sort((left, right) => {
      const leftValue = value(left); const rightValue = value(right);
      if (typeof leftValue === "string") return direction * leftValue.localeCompare(rightValue, "zh-CN");
      return direction * (Number(leftValue) - Number(rightValue));
    });
  }

  function renderSummary() {
    const summary = view.current?.summary || {};
    byID("summary-servers").textContent = summary.total_servers ?? 0;
    byID("summary-gpus").textContent = summary.total_gpus ?? 0;
    byID("summary-free").textContent = summary.available_gpus ?? 0;
    const models = new Map();
    (view.current?.gpus || []).forEach((gpu) => models.set(gpu.name || "未知 GPU", (models.get(gpu.name || "未知 GPU") || 0) + 1));
    byID("summary-models").textContent = models.size
      ? Array.from(models.entries()).map(([name, count]) => `${name} × ${count}`).join(" · ")
      : "尚未发现 GPU";
  }

  function renderRows() {
    const rows = byID("server-rows");
    if (!view.current) { rows.innerHTML = `<div class="empty-state">正在读取本机资源状态。</div>`; return; }
    const details = sortDetails((view.current.endpoints || []).map(endpointDetails).filter(filterDetails));
    if (!details.length) { rows.innerHTML = `<div class="empty-state">没有符合当前筛选条件的服务器。</div>`; return; }
    rows.innerHTML = groupedSections(details).map((section) => {
      const header = section.group || section.ungrouped
        ? `<div class="group-header${section.ungrouped ? " ungrouped" : ""}" role="rowheader"><strong>${escapeHTML(section.ungrouped ? "未分组" : (section.group.display_name || section.group.id))}</strong><span>${escapeHTML(groupCapacity(section.items))}</span></div>`
        : "";
      return header + section.items.map(serverRow).join("");
    }).join("");
  }

  function serverRow(item) {
    const gpu = item.gpus[0];
    const model = gpu ? gpu.name : "无 GPU";
    const config = gpu ? `${plural(item.gpus.length, "张")} · ${Math.round((gpu.total_vram_mib || 0) / 1024)} GB/卡` : "CPU 节点";
    const selected = item.endpoint.id === view.selectedEndpointID ? "selected" : "";
    const workspace = effectiveWorkspace(item.endpoint) || "未设置工作区";
    const group = groupById(endpointGroupID(item.endpoint));
    const statusLine = group
      ? `${item.status.label} · ${group.display_name || group.id}`
      : `${item.status.label} · ${workspace}`;
    return `<div class="server-row ${selected}" data-endpoint-id="${escapeHTML(item.endpoint.id)}" role="row">
        <div class="endpoint-cell"><div class="primary-line"><i class="status-dot ${item.status.className}"></i>${escapeHTML(endpointSsh(item.endpoint))}</div><div class="secondary-line">${escapeHTML(statusLine)}</div></div>
        <div class="task-cell"><div class="primary-line">${escapeHTML(item.project)}</div><div class="secondary-line">${escapeHTML(item.task)}</div></div>
        <div class="gpu-cell"><div class="primary-line">${escapeHTML(model)}</div><div class="secondary-line">${escapeHTML(config)}</div></div>
        <div class="free-count">${item.gpus.length ? `${item.free}/${item.gpus.length}` : "—"}</div>
        ${metric(item.gpuUtil)}${metric(item.memory)}${metric(item.cpu)}${metric(item.systemMemory)}
        <button class="detail-open" type="button" data-open-detail="${escapeHTML(item.endpoint.id)}" aria-label="查看 ${escapeHTML(item.endpoint.id)} 详情">›</button>
      </div>`;
  }

  function usageRecords() {
    return (view.current?.leases || []).filter((lease) => lease.state !== "RELEASED").map((lease) => ({
      project: lease.project_id || "—",
      task: lease.task_ref || "—",
      state: lease.runtime_state || lease.state || "—",
      quantities: { gpu_count: (lease.gpu_ids || []).length },
    }));
  }

  function renderUsage() {
    const records = usageRecords();
    byID("usage-projects").textContent = new Set(records.map((item) => item.project)).size;
    byID("usage-tasks").textContent = records.length;
    byID("usage-gpus").textContent = records.reduce((total, item) => total + (Number(item.quantities.gpu_count) || 0), 0);
    const rows = byID("usage-rows");
    if (!records.length) { rows.innerHTML = `<div class="empty-state">当前没有资源分配。</div>`; return; }
    rows.innerHTML = records.map((item) => {
      const quantities = item.quantities;
      const memory = Number(quantities.memory_mib) || 0;
      const cpu = Number(quantities.cpu_cores) || 0;
      const gpu = Number(quantities.gpu_count) || 0;
      return `<article class="usage-row"><div class="usage-project"><strong>${escapeHTML(item.project)}</strong><span>当前项目</span></div><div class="usage-task"><strong>${escapeHTML(item.task)}</strong><span>${escapeHTML(item.state)}</span></div><div class="usage-quantity"><strong>${gpu}</strong><span>GPU</span></div><div class="usage-quantity"><strong>${cpu || "—"}</strong><span>CPU 核</span></div><div class="usage-quantity"><strong>${memory ? `${Math.round(memory / 1024)} GiB` : "—"}</strong><span>内存</span></div></article>`;
    }).join("");
  }

  function renderUpdatedAt() {
    const time = view.snapshot?.server_time;
    if (!time) { byID("updated-at").textContent = "尚未收到资源状态"; return; }
    const date = new Date(time);
    byID("updated-at").textContent = Number.isNaN(date.getTime()) ? "已更新" : `更新于 ${date.toLocaleString("zh-CN", { month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit" })}`;
  }

  function render() {
    renderUpdatedAt(); renderSummary(); renderRows(); renderUsage();
    $$(".table-head button").forEach((button) => button.classList.toggle("active", button.dataset.sort === view.sort.key));
  }

  async function refresh({ quiet = false } = {}) {
    const button = byID("refresh"); button.disabled = true;
    const result = await call("snapshot");
    button.disabled = false;
    if (!result.ok) { failure(result); byID("connection-dot").className = "live-dot error"; byID("connection-label").textContent = "本机服务不可用"; return; }
    view.snapshot = result.data;
    view.current = stateCurrent(result.data);
    if (!view.current) { showNotice("本机服务返回的资源快照无法读取。", true); return; }
    byID("connection-dot").className = "live-dot online";
    byID("connection-label").textContent = "本机服务已连接";
    render();
    await loadObservationProfiles();
    if (!quiet) showNotice("资源状态已刷新。");
  }

  async function loadObservationProfiles() {
    const result = await call("observation_profiles");
    const rows = result && result.ok && result.data && Array.isArray(result.data.data)
      ? result.data.data
      : [];
    view.observationProfiles = rows.length ? rows : [
      { id: "linux-nvidia", display_name: "标准 NVIDIA 采集" },
      { id: "linux-host", display_name: "主机容量采集" },
      { id: "server-script-v1", display_name: "服务器采集脚本" },
    ];
    const select = byID("observation-profile");
    if (!select) return;
    const current = select.value || "server-script-v1";
    select.innerHTML = view.observationProfiles.map((profile) => (
      `<option value="${escapeHTML(profile.id)}">${escapeHTML(profile.display_name || profile.id)}</option>`
    )).join("");
    select.value = view.observationProfiles.some((profile) => profile.id === current)
      ? current
      : (view.observationProfiles[0] && view.observationProfiles[0].id) || "server-script-v1";
    fillProfileSelect(byID("edit-observation-profile"), current);
  }

  function fillProfileSelect(select, current) {
    if (!select) return;
    const selected = select.value || current || "server-script-v1";
    select.innerHTML = view.observationProfiles.map((profile) => (
      `<option value="${escapeHTML(profile.id)}">${escapeHTML(profile.display_name || profile.id)}</option>`
    )).join("");
    select.value = view.observationProfiles.some((profile) => profile.id === selected)
      ? selected
      : (view.observationProfiles[0] && view.observationProfiles[0].id) || "server-script-v1";
  }

  function fillGroupSelect(select, { includeUngrouped = false, includeEmpty = false, selected = "" } = {}) {
    if (!select) return;
    const groups = listedServerGroups();
    const options = [];
    if (includeEmpty) options.push(`<option value="">${hasFormalGroups() ? "选择服务器组" : "自动选择"}</option>`);
    if (includeUngrouped) {
      options.push(`<option value="__ungrouped__">未分组（按单机申请）</option>`);
    }
    groups.forEach((group) => {
      options.push(`<option value="${escapeHTML(group.id)}">${escapeHTML(group.display_name || group.id)}</option>`);
    });
    if (!includeEmpty && !includeUngrouped) {
      options.unshift(`<option value="">未分组</option>`);
    }
    select.innerHTML = options.join("");
    if (selected && [...select.options].some((option) => option.value === selected)) select.value = selected;
  }

  function syncWorkspaceField(groupSelect, workspaceInput, hint, requiredWhenUngrouped) {
    const group = groupById(groupSelect.value);
    const inherit = Boolean(group);
    workspaceInput.required = requiredWhenUngrouped && !inherit;
    workspaceInput.placeholder = inherit
      ? `继承 ${group.workspace_path || "组默认工作区"}`
      : "例如：/srv/workspace";
    if (hint) {
      hint.textContent = inherit
        ? `留空则继承 ${group.workspace_path || "组默认工作区"}；填写则为这台服务器的覆盖路径。`
        : "未分组时必须填写绝对路径。";
    }
  }

  function endpointWritePayload({ id, host, port, sshUser, groupID, workspace, profile, ownerProject, isUpdate = false }) {
    const payload = {
      ssh_user: sshUser,
      observation_profile: profile,
    };
    if (id) payload.id = id;
    if (host) payload.host = host;
    if (port) payload.port = Number(port);
    if (ownerProject !== undefined) payload.owner_project_id = ownerProject || null;
    const trimmed = (workspace || "").trim();
    if (groupID) {
      payload.server_group_id = groupID;
      payload.workspace_path_override = trimmed || null;
    } else {
      if (isUpdate) payload.server_group_id = null;
      payload.workspace_path = trimmed;
    }
    return payload;
  }

  function memoryColor(value) { return value === null ? "#9aa1aa" : value >= 80 ? "#ef4d57" : value >= 60 ? "#e8ad00" : "#39b967"; }

  function statusCounts(gpus) {
    const counts = { "空闲": 0, "占卡": 0, "繁忙": 0, "错误": 0 };
    gpus.forEach((gpu) => { counts[gpuStatus(gpu).label] += 1; });
    return Object.entries(counts).map(([label, count]) => `<span class="status-chip" style="--status-color:${({ "空闲": "#39b967", "占卡": "#7e6ced", "繁忙": "#e8ad00", "错误": "#ef4d57" })[label]}"><i></i>${label} <strong>${count}</strong></span>`).join("");
  }

  function gpuCard(gpu) {
    const value = gpuMemoryPercent(gpu); const status = gpuStatus(gpu); const ring = memoryColor(value);
    return `<article class="gpu-card"><div class="memory-ring" style="--percent:${clamp(value)}%;--ring-color:${ring}"><span>${escapeHTML(gpu.gpu_index ?? "?")}</span></div><div class="gpu-card-info"><div class="gpu-card-title"><strong>GPU ${escapeHTML(gpu.gpu_index ?? "?")}</strong><span class="state-badge" style="--state-color:${status.color}">${status.label}</span></div><p>${escapeHTML(gpuMemoryLabel(gpu))}</p></div><div class="gpu-card-value" style="--ring-color:${ring}">${percent(value)}</div></article>`;
  }

  function valuesForChart(history, kind) {
    const data = history?.data || history || {};
    if (kind === "cpu") return (data.points || []).map((point) => (
      typeof point.cpu_utilization_pct === "number" ? point.cpu_utilization_pct : null
    ));
    if (kind === "systemMemory") return (data.points || []).map((point) => point.memory_used_pct ?? null);
    const field = kind === "gpuUtil" ? "gpu_utilization_pct" : "memory_used_pct";
    const series = data.gpu_series || [];
    const count = Math.max(0, ...series.map((item) => (item.points || []).length));
    return Array.from({ length: count }, (_, index) => average(series.map((item) => item.points?.[index]?.[field] ?? null)));
  }

  function sparkline(values, color) {
    const present = values.filter((value) => typeof value === "number" && Number.isFinite(value));
    if (!present.length) return `<div class="chart-empty">暂无资源历史</div>`;
    const points = values.map((value, index) => {
      if (typeof value !== "number" || !Number.isFinite(value)) return null;
      const x = values.length < 2 ? 50 : index * 100 / (values.length - 1);
      const y = 96 - clamp(value) * .86;
      return `${x.toFixed(2)},${y.toFixed(2)}`;
    }).filter(Boolean).join(" ");
    return `<svg class="sparkline" viewBox="0 0 100 100" preserveAspectRatio="none" aria-hidden="true"><path d="M0 96H100" stroke="#e9edf1" stroke-width="1" fill="none"/><path d="M0 53H100" stroke="#f1f3f5" stroke-width="1" fill="none"/><polyline points="${points}" stroke="${color}" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" fill="none"/></svg>`;
  }

  function charts(history) {
    const cards = [
      ["CPU 使用率", "CPU", "cpu", "#4f7ff0"],
      ["内存占用率", "内存", "systemMemory", "#22a18f"],
      ["GPU 利用率", "GPU", "gpuUtil", "#8e70ea"],
      ["显存占用率", "显存", "memory", "#e8ad00"],
    ];
    return cards.map(([title, label, kind, color]) => {
      const values = valuesForChart(history, kind); const latest = [...values].reverse().find((value) => typeof value === "number");
      return `<section class="chart-card"><h4>${title}</h4><p>${label}：最新 ${percent(latest)}</p>${sparkline(values, color)}</section>`;
    }).join("");
  }

  function renderDetail(endpointID, history = null, range = 3600) {
    const endpoint = (view.current?.endpoints || []).find((item) => item.id === endpointID); if (!endpoint) return;
    const gpus = endpointGPUs(endpointID); const keepalive = endpoint.keepalive?.policy === "idle_keepalive";
    const group = groupById(endpointGroupID(endpoint));
    const workspace = effectiveWorkspace(endpoint);
    const inherited = Boolean(group && !workspaceOverride(endpoint));
    const groupLine = group
      ? `<div class="workspace-path">分组：${escapeHTML(group.display_name || group.id)}${group.description ? ` · ${escapeHTML(group.description)}` : ""}</div>`
      : `<div class="workspace-path">未分组 · 按单机申请</div>`;
    const workspaceLine = `<div class="workspace-path">▣ 远端工作区：${escapeHTML(workspace || "未设置")}${group ? (inherited ? "（继承组默认）" : "（覆盖组默认）") : ""}</div>`;
    const notesLine = group && group.environment_notes
      ? `<div class="workspace-path">环境说明：${escapeHTML(group.environment_notes)}</div>`
      : "";
    byID("detail-content").innerHTML = `<div class="detail-sheet"><header class="detail-heading"><div class="detail-title"><span class="server-glyph">▤</span><div><h2>服务器详情</h2><p>${escapeHTML(endpointSsh(endpoint))}</p></div></div><div class="detail-actions"><button class="button primary" type="button" id="detail-claim">申请 GPU</button><button class="button secondary" type="button" id="detail-edit">编辑</button><button class="button secondary" type="button" id="detail-keepalive">${keepalive ? "结束占卡" : "开始占卡"}</button><button class="icon-button close" type="button" id="close-detail" aria-label="关闭">×</button></div></header>${groupLine}${workspaceLine}${notesLine}<section class="gpu-status-panel"><div class="section-heading"><h3>GPU 显存状态</h3><p>当前显存 · ${gpus.length} 张 GPU</p></div><div class="status-counts">${statusCounts(gpus)}</div><div class="gpu-cards">${gpus.length ? gpus.sort((left, right) => (left.gpu_index || 0) - (right.gpu_index || 0)).map(gpuCard).join("") : `<div class="chart-empty">此服务器没有 GPU。</div>`}</div></section><section class="history-section"><div class="history-header"><h3>历史</h3><div class="range-switch">${[[3600, "1h"], [21600, "6h"], [86400, "24h"]].map(([seconds, label]) => `<button type="button" class="${range === seconds ? "active" : ""}" data-history-range="${seconds}">${label}</button>`).join("")}</div></div><div class="chart-grid">${charts(history)}</div></section></div>`;
    byID("close-detail").addEventListener("click", () => byID("detail-dialog").close());
    byID("detail-claim").addEventListener("click", () => openClaim(endpointID));
    byID("detail-edit").addEventListener("click", () => openEdit(endpointID));
    byID("detail-keepalive").addEventListener("click", async () => {
      const enabled = !keepalive;
      if (!window.confirm(enabled ? "开始只对这台服务器的空闲 GPU 执行占卡？不会启动或停止已有任务。" : "结束这台服务器的空闲 GPU 占卡？不会停止正在运行的任务。")) return;
      const result = await call("set_keepalive", endpointID, enabled);
      if (!result.ok) { failure(result); return; }
      showNotice(enabled ? "已提交开始占卡，正在确认 GPU 状态。" : "已提交结束占卡，正在确认 GPU 状态。");
      await refresh({ quiet: true }); renderDetail(endpointID, history, range);
    });
    $$('[data-history-range]').forEach((button) => button.addEventListener("click", () => loadHistory(endpointID, Number(button.dataset.historyRange))));
  }

  async function loadHistory(endpointID, range = 3600) {
    renderDetail(endpointID, null, range);
    const result = await call("endpoint_history", endpointID, range);
    if (!result.ok) { failure(result); return; }
    renderDetail(endpointID, result.data, range);
  }

  function openDetail(endpointID) {
    view.selectedEndpointID = endpointID; renderRows();
    const dialog = byID("detail-dialog"); if (!dialog.open) dialog.showModal();
    loadHistory(endpointID);
  }

  function openClaim(endpointID = "") {
    const endpoint = (view.current?.endpoints || []).find((item) => item.id === endpointID);
    const grouped = hasFormalGroups();
    const groupWrap = byID("claim-group-wrap");
    const endpointWrap = byID("claim-endpoint-wrap");
    const groupSelect = byID("claim-group");
    const endpointSelect = byID("claim-endpoint");
    fillGroupSelect(groupSelect, { includeUngrouped: true, includeEmpty: true });
    const ungrouped = ungroupedEndpoints();
    endpointSelect.innerHTML = `<option value="">选择未分组服务器</option>${ungrouped.map((item) => `<option value="${escapeHTML(item.id)}">${escapeHTML(endpointSsh(item))}</option>`).join("")}`;
    if (grouped) {
      groupWrap.hidden = false;
      const groupID = endpoint ? (endpointGroupID(endpoint) || "__ungrouped__") : "";
      groupSelect.value = groupID;
      endpointWrap.hidden = groupSelect.value !== "__ungrouped__";
      if (endpoint && !endpointGroupID(endpoint)) endpointSelect.value = endpoint.id;
    } else {
      groupWrap.hidden = true;
      endpointWrap.hidden = false;
      endpointSelect.innerHTML = `<option value="">自动选择</option>${(view.current?.endpoints || []).map((item) => `<option value="${escapeHTML(item.id)}">${escapeHTML(endpointSsh(item))}</option>`).join("")}`;
      endpointSelect.value = endpointID;
    }
    byID("claim-help").textContent = grouped
      ? "分组申请由后端在组内 best-fit，不指定单机。未分组服务器仍按单机申请。容量不足时返回 no_capacity，不会进入排队。"
      : "可选择一台服务器，或由后端自动选择。容量不足时返回 no_capacity，不会进入排队。";
    if (!byID("claim-dialog").open) byID("claim-dialog").showModal();
  }

  function openAddServer() {
    fillGroupSelect(byID("add-server-group"));
    syncWorkspaceField(byID("add-server-group"), byID("add-workspace"), byID("add-workspace-hint"), true);
    if (!byID("add-dialog").open) byID("add-dialog").showModal();
  }

  function openEdit(endpointID) {
    const endpoint = (view.current?.endpoints || []).find((item) => item.id === endpointID);
    if (!endpoint) return;
    byID("edit-endpoint-id").value = endpoint.id;
    byID("edit-ssh").value = endpointSsh(endpoint);
    byID("edit-ssh-user").value = endpoint.ssh_user || "root";
    fillGroupSelect(byID("edit-server-group"), { selected: endpointGroupID(endpoint) || "" });
    fillProfileSelect(byID("edit-observation-profile"), endpoint.observation_profile || "server-script-v1");
    byID("edit-workspace").value = workspaceOverride(endpoint);
    syncWorkspaceField(byID("edit-server-group"), byID("edit-workspace"), byID("edit-workspace-hint"), true);
    if (!byID("edit-dialog").open) byID("edit-dialog").showModal();
  }

  function resetGroupForm() {
    byID("group-original-id").value = "";
    byID("group-id").value = "";
    byID("group-id").readOnly = false;
    byID("group-display-name").value = "";
    byID("group-workspace").value = "";
    byID("group-description").value = "";
    byID("group-environment-notes").value = "";
  }

  function renderGroupList() {
    const groups = listedServerGroups();
    const list = byID("group-list");
    if (!groups.length) {
      list.innerHTML = `<p class="field-hint">还没有服务器组。创建后即可在添加/编辑服务器时绑定，并按组申请 GPU。</p>`;
      return;
    }
    list.innerHTML = groups.map((group) => {
      const members = (view.current?.endpoints || []).filter((endpoint) => endpointGroupID(endpoint) === group.id);
      return `<article class="group-item"><div><strong>${escapeHTML(group.display_name || group.id)}</strong><small>${escapeHTML(group.workspace_path || "未设置工作区")} · ${members.length} 台</small></div><div class="group-item-actions"><button class="button secondary" type="button" data-edit-group="${escapeHTML(group.id)}">编辑</button><button class="button secondary" type="button" data-delete-group="${escapeHTML(group.id)}">删除</button></div></article>`;
    }).join("");
  }

  function fillGroupForm(group) {
    byID("group-original-id").value = group.id;
    byID("group-id").value = group.id;
    byID("group-id").readOnly = true;
    byID("group-display-name").value = group.display_name || "";
    byID("group-workspace").value = group.workspace_path || "";
    byID("group-description").value = group.description || "";
    byID("group-environment-notes").value = group.environment_notes || "";
  }

  function openGroups() {
    renderGroupList();
    resetGroupForm();
    if (!byID("groups-dialog").open) byID("groups-dialog").showModal();
  }

  async function loadSettings() {
    const [info, settings, mcp] = await Promise.all([
      call("app_info"),
      call("collector_settings"),
      call("mcp_entry"),
    ]);
    if (info.ok) {
      byID("service-url").textContent = info.data.base_url;
      byID("data-directory").textContent = info.data.data_dir;
      byID("app-version").textContent = info.data.version;
    }
    if (settings.ok) byID("collector-interval").value = String(settings.data.data?.settings?.interval_seconds || settings.data.data?.interval_seconds || 10);
    renderMcpEntry(mcp);
  }

  async function openSettings() {
    await loadSettings();
    if (!byID("settings-dialog").open) byID("settings-dialog").showModal();
  }

  function setPage(page) {
    if (page === "settings") { openSettings(); return; }
    byID("servers-page").hidden = page !== "servers";
    byID("usage-page").hidden = page !== "usage";
    $$('[data-page]').forEach((button) => {
      const active = button.dataset.page === page;
      button.classList.toggle("active", active);
      if (active) button.setAttribute("aria-current", "page");
      else button.removeAttribute("aria-current");
    });
  }

  function bindEvents() {
    $$(".form-dialog .close, .form-dialog .button.secondary[value=cancel]").forEach((button) => {
      button.addEventListener("click", () => button.closest("dialog").close());
    });
    byID("refresh").addEventListener("click", () => refresh());
    byID("search").addEventListener("input", renderRows);
    $$(".filter").forEach((button) => button.addEventListener("click", () => { view.filter = button.dataset.filter; $$(".filter").forEach((item) => item.classList.toggle("selected", item === button)); renderRows(); }));
    $$(".table-head button").forEach((button) => button.addEventListener("click", () => { const key = button.dataset.sort; view.sort = { key, direction: view.sort.key === key && view.sort.direction === "desc" ? "asc" : "desc" }; renderRows(); $$(".table-head button").forEach((item) => item.classList.toggle("active", item === button)); }));
    byID("server-rows").addEventListener("click", (event) => { const target = event.target.closest("[data-open-detail]"); if (target) openDetail(target.dataset.openDetail); });
    byID("open-claim").addEventListener("click", () => openClaim());
    byID("open-add-server").addEventListener("click", () => openAddServer());
    byID("open-groups").addEventListener("click", () => openGroups());
    $$('[data-page]').forEach((button) => button.addEventListener("click", () => setPage(button.dataset.page)));
    byID("usage-claim").addEventListener("click", () => openClaim());
    byID("add-server-group").addEventListener("change", () => {
      syncWorkspaceField(byID("add-server-group"), byID("add-workspace"), byID("add-workspace-hint"), true);
    });
    byID("edit-server-group").addEventListener("change", () => {
      syncWorkspaceField(byID("edit-server-group"), byID("edit-workspace"), byID("edit-workspace-hint"), true);
    });
    byID("claim-group").addEventListener("change", () => {
      byID("claim-endpoint-wrap").hidden = byID("claim-group").value !== "__ungrouped__";
    });

    byID("claim-form").addEventListener("submit", async (event) => {
      event.preventDefault(); const form = new FormData(event.currentTarget);
      const constraints = { gpu_count: Number(form.get("gpuCount")), placement: "pack", same_host: true };
      const groupID = form.get("serverGroup");
      const endpoint = form.get("endpoint");
      if (hasFormalGroups()) {
        if (!groupID) { showNotice("请选择服务器组，或使用未分组单机申请。", true); return; }
        if (groupID === "__ungrouped__") {
          if (!endpoint) { showNotice("未分组申请必须选择一台未分组服务器。", true); return; }
          constraints.endpoint_ids = [endpoint];
        } else {
          constraints.server_group_ids = [groupID];
        }
      } else if (endpoint) {
        constraints.endpoint_ids = [endpoint];
      }
      const memory = form.get("minimumFreeVRAM"); if (memory) constraints.min_free_vram_mib = Number(memory) * 1024;
      const result = await call("claim", { project_id: form.get("project"), task_ref: form.get("task"), purpose: form.get("purpose"), constraints });
      if (!result.ok) { failure(result); return; }
      byID("claim-dialog").close(); showNotice("已提交 GPU 申请，正在刷新资源状态。"); await refresh({ quiet: true });
    });

    byID("add-form").addEventListener("submit", async (event) => {
      event.preventDefault(); const form = new FormData(event.currentTarget);
      const profile = form.get("observationProfile") || "server-script-v1";
      const groupID = form.get("serverGroup") || "";
      const workspace = form.get("workspace");
      if (!groupID && !(workspace || "").trim()) {
        showNotice("未分组服务器必须填写绝对远端工作区路径。", true); return;
      }
      const payload = endpointWritePayload({
        id: form.get("id"),
        host: form.get("host"),
        port: form.get("port"),
        sshUser: form.get("sshUser"),
        groupID,
        workspace,
        profile,
        ownerProject: form.get("ownerProject") || null,
      });
      payload.labels = ["desktop-windows"];
      if (profile === "server-script-v1") payload.keepalive_adapter_id = "server-script-v1";
      const result = await call("create_endpoint", payload);
      if (!result.ok) { failure(result); return; }
      byID("add-dialog").close(); showNotice("已添加服务器，正在等待首次只读采集确认状态。"); await refresh({ quiet: true });
    });

    byID("edit-form").addEventListener("submit", async (event) => {
      event.preventDefault(); const form = new FormData(event.currentTarget);
      const endpointID = form.get("endpointId");
      const profile = form.get("observationProfile") || "server-script-v1";
      const groupID = form.get("serverGroup") || "";
      const workspace = form.get("workspace");
      if (!groupID && !(workspace || "").trim()) {
        showNotice("未分组服务器必须填写绝对远端工作区路径。", true); return;
      }
      const payload = endpointWritePayload({
        sshUser: form.get("sshUser"),
        groupID,
        workspace,
        profile,
        isUpdate: true,
      });
      const result = await call("update_endpoint", endpointID, payload);
      if (!result.ok) { failure(result); return; }
      byID("edit-dialog").close(); showNotice("已更新服务器设置，正在确认状态。"); await refresh({ quiet: true });
    });

    byID("reset-group-form").addEventListener("click", () => resetGroupForm());
    byID("group-list").addEventListener("click", async (event) => {
      const edit = event.target.closest("[data-edit-group]");
      if (edit) {
        const group = groupById(edit.dataset.editGroup);
        if (group) fillGroupForm(group);
        return;
      }
      const remove = event.target.closest("[data-delete-group]");
      if (!remove) return;
      if (!window.confirm("删除这个服务器组？仍有成员时后端会拒绝删除。")) return;
      const result = await call("delete_server_group", remove.dataset.deleteGroup);
      if (!result.ok) { failure(result); return; }
      showNotice("已删除服务器组。"); await refresh({ quiet: true }); renderGroupList(); resetGroupForm();
    });
    byID("group-form").addEventListener("submit", async (event) => {
      event.preventDefault();
      const form = new FormData(event.currentTarget);
      const originalID = form.get("originalId");
      const payload = {
        id: form.get("id"),
        display_name: form.get("displayName"),
        workspace_path: form.get("workspace"),
        description: form.get("description") || "",
        environment_notes: form.get("environmentNotes") || "",
      };
      const result = originalID
        ? await call("update_server_group", originalID, payload)
        : await call("create_server_group", payload);
      if (!result.ok) { failure(result); return; }
      showNotice(originalID ? "已更新服务器组。" : "已创建服务器组。");
      await refresh({ quiet: true });
      renderGroupList();
      resetGroupForm();
    });

    byID("settings-form").addEventListener("submit", async (event) => {
      event.preventDefault(); const result = await call("update_collector_interval", Number(byID("collector-interval").value));
      if (!result.ok) { failure(result); return; }
      byID("settings-dialog").close(); showNotice("数据采集间隔已更新。"); await refresh({ quiet: true });
    });
    $$(".mcp-copy").forEach((button) => {
      button.addEventListener("click", () => {
        const kind = button.dataset.copy;
        const text = kind === "path" ? byID("mcp-entry-path").textContent : byID("mcp-entry-config").textContent;
        if (!text) return;
        copyText(text);
        button.textContent = "已复制";
        window.setTimeout(() => { button.textContent = "复制"; }, 1600);
      });
    });
  }

  function mcpPayload(result) {
    if (!result || !result.ok || !result.data) return null;
    return result.data.data || result.data;
  }

  function renderMcpEntry(result) {
    const status = byID("mcp-entry-status");
    const found = byID("mcp-entry-found");
    const payload = mcpPayload(result);
    if (!payload) {
      status.hidden = false;
      status.textContent = "无法读取 MCP 入口。";
      found.hidden = true;
      return;
    }
    if (payload.available === true && typeof payload.command === "string" && payload.command && payload.mcpServers && typeof payload.mcpServers === "object") {
      const configText = JSON.stringify({ mcpServers: payload.mcpServers }, null, 2);
      byID("mcp-entry-path").textContent = payload.command;
      byID("mcp-entry-config").textContent = configText;
      status.hidden = true;
      found.hidden = false;
      return;
    }
    status.hidden = false;
    found.hidden = true;
    const hint = typeof payload.hint === "string" && payload.hint ? payload.hint : "";
    status.textContent = hint ? `未找到 MCP 入口。${hint}` : "未找到 MCP 入口。";
  }

  function copyText(value) {
    const field = document.createElement("textarea");
    field.value = value;
    field.setAttribute("readonly", "");
    field.style.position = "fixed";
    field.style.left = "-9999px";
    document.body.appendChild(field);
    field.select();
    document.execCommand("copy");
    field.remove();
  }

  async function boot() {
    bindEvents(); await refresh({ quiet: true });
    window.setInterval(() => { if (!document.hidden) refresh({ quiet: true }); }, 10_000);
  }

  window.addEventListener("pywebviewready", boot, { once: true });
})();
