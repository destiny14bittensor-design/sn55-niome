"use strict";

const byId = (id) => document.getElementById(id);
const stageLabels = {
  query_received: "쿼리 수신",
  safe_uploaded: "안전 제출 완료",
  created: "브리지 준비",
  opening: "브리지 연결",
  streaming: "스트리밍",
  waiting_for_contract_seed: "계약 시드 갱신 대기",
  waiting_for_seeds: "시드 대기",
  building: "결과 생성",
  finishing_upload: "최종 PUT",
  waiting_official: "공식 검증 대기",
  official_published: "공식 점수 발표",
  bridge_failed: "브리지 실패",
};
let latestSnapshot = null;
let selectedMiner = localStorage.getItem("niome-selected-miner") || "bitcoin-hype-fleet:bitcoin1";
let lastNoticeKey = localStorage.getItem("niome-fleet-last-notice") || "";
let eventStreamLive = false;

function text(id, value) {
  const el = byId(id);
  if (el) el.textContent = value ?? "—";
}

function fmtNumber(value, digits = 3) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return "—";
  return Number(value).toLocaleString("ko-KR", { maximumFractionDigits: digits });
}

function fmtSigned(value, digits = 4) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return "비교 불가";
  const number = Number(value);
  return `${number > 0 ? "+" : ""}${fmtNumber(number, digits)}`;
}

function fmtTime(value, withDate = false) {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "—";
  return new Intl.DateTimeFormat("ko-KR", {
    timeZone: "UTC",
    month: withDate ? "2-digit" : undefined,
    day: withDate ? "2-digit" : undefined,
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  }).format(date) + " UTC";
}

function fmtDuration(seconds) {
  if (seconds === null || seconds === undefined || Number.isNaN(Number(seconds))) return "—";
  const value = Math.max(0, Number(seconds));
  if (value < 60) return `${Math.round(value)}초`;
  if (value < 3600) return `${Math.floor(value / 60)}분 ${Math.round(value % 60)}초`;
  return `${Math.floor(value / 3600)}시간 ${Math.round((value % 3600) / 60)}분`;
}

function fmtBytes(value) {
  let bytes = Number(value || 0);
  const units = ["B", "KiB", "MiB", "GiB"];
  let index = 0;
  while (bytes >= 1024 && index < units.length - 1) { bytes /= 1024; index += 1; }
  return `${bytes.toFixed(index ? 1 : 0)} ${units[index]}`;
}

function shortTask(taskId) {
  if (!taskId) return "작업 대기";
  return taskId.length > 18 ? `${taskId.slice(0, 10)}…${taskId.slice(-6)}` : taskId;
}

function statusLabel(overall) {
  return overall === "critical" ? "긴급" : overall === "warning" ? "주의" : overall === "complete" ? "완료" : "정상";
}

function appendCell(row, value, className = "") {
  const cell = document.createElement("td");
  cell.textContent = value;
  if (className) cell.className = className;
  row.append(cell);
  return cell;
}

function classifyAlert(alert) {
  const raw = `${alert?.code || ""} ${alert?.title || ""} ${alert?.detail || ""}`.toLowerCase();
  if (raw.includes("slow down") || raw.includes("503")) return { title: "S3 요청 제한", style: "HTTP 503 · SLOW DOWN" };
  if (raw.includes("timeout") || raw.includes("timed out")) return { title: "통신 시간 초과", style: "TIMEOUT" };
  if (raw.includes("source_unreachable") || raw.includes("연결 실패")) return { title: "Fleet 연결 오류", style: "SOURCE UNREACHABLE" };
  if (raw.includes("offline")) return { title: "프로세스 오프라인", style: "PROCESS OFFLINE" };
  if (raw.includes("s3") || raw.includes("upload") || raw.includes("put")) return { title: "S3 업로드 오류", style: "UPLOAD FAILURE" };
  if (raw.includes("bridge")) return { title: "Seed Bridge 오류", style: "BRIDGE FAILURE" };
  if (raw.includes("config")) return { title: "설정 검증 경고", style: "CONFIG MISMATCH" };
  const style = String(alert?.code || "runtime_error").replaceAll("_", " ").toUpperCase();
  return { title: alert?.title || "운영 오류", style };
}

function shortMiner(value) {
  const parts = String(value || "Fleet").split(":");
  return parts[parts.length - 1];
}

function renderTopAlerts(snapshot) {
  const fleet = snapshot.fleet || {};
  const panel = byId("alert-summary-panel");
  const list = byId("alert-summary-list");
  const criticals = (fleet.alerts || []).filter((item) => item.severity === "critical");
  const grouped = new Map();
  for (const alert of criticals) {
    const category = classifyAlert(alert);
    const key = `${category.title}:${category.style}`;
    const group = grouped.get(key) || { ...category, miners: new Set() };
    group.miners.add(shortMiner(alert.miner));
    grouped.set(key, group);
  }
  panel.classList.toggle("critical", criticals.length > 0);
  text("alert-summary-count", criticals.length ? `${criticals.length}건 긴급` : "정상");
  list.replaceChildren();
  if (!criticals.length) {
    const item = document.createElement("div"); item.className = "alert-summary-item clear";
    const signal = document.createElement("span"); signal.className = "alert-summary-signal";
    const body = document.createElement("div");
    const title = document.createElement("strong"); title.textContent = "긴급 오류 없음";
    const meta = document.createElement("small"); meta.textContent = `${fleet.sources_online || 0}개 Fleet · ${fleet.online || 0}/${fleet.total || 0} 마이너 online`;
    body.append(title, meta); item.append(signal, body); list.append(item);
    return;
  }
  for (const group of grouped.values()) {
    const item = document.createElement("div"); item.className = "alert-summary-item critical";
    const signal = document.createElement("span"); signal.className = "alert-summary-signal";
    const body = document.createElement("div");
    const title = document.createElement("strong"); title.textContent = group.title;
    const meta = document.createElement("small"); meta.textContent = group.style;
    body.append(title, meta);
    const miners = document.createElement("span"); miners.className = "alert-summary-miners"; miners.textContent = [...group.miners].join(", ");
    item.append(signal, body, miners); list.append(item);
  }
}

function renderFleetHeader(snapshot) {
  const fleet = snapshot.fleet || {};
  const chain = snapshot.chain || {};
  const badge = byId("overall-badge");
  badge.className = `badge ${fleet.overall || "healthy"}`;
  badge.textContent = fleet.online === fleet.total ? `${fleet.online}/${fleet.total} 온라인` : `${fleet.online || 0}/${fleet.total || 0} 확인 필요`;
  text("coverage-label", `현재 과제 수신 ${fleet.coverage || 0}/${fleet.total || 0}`);
  text("active-task", fleet.active_task_id || "작업을 기다리고 있습니다");
  text("online-count", `${fleet.online || 0} / ${fleet.total || 0}`);
  text("source-count", `${fleet.sources_online || 0} / ${fleet.sources_total || 0}`);
  text("baseline-score", fmtNumber(fleet.task_top_score, 6));
  text("current-block", chain.block === null || chain.block === undefined ? "—" : Number(chain.block).toLocaleString("ko-KR"));
  text("round-phase", chain.round_phase === null || chain.round_phase === undefined ? "—" : `${chain.round_phase} / 720`);
  text("comparison-task", shortTask(fleet.active_task_id));
  text("footer-generated", `생성 ${fmtTime(snapshot.generated_at, true)} · ${chain.source || "—"}`);
  renderTopAlerts(snapshot);
}

function renderSources(sources) {
  const grid = byId("source-grid");
  grid.replaceChildren();
  for (const source of sources || []) {
    const card = document.createElement("article");
    card.className = `source-card panel ${source.transport || "unreachable"}`;
    const head = document.createElement("div"); head.className = "source-card-head";
    const identity = document.createElement("div");
    const title = document.createElement("strong"); title.textContent = source.label || source.id;
    const kind = document.createElement("small"); kind.textContent = `${source.kind === "local" ? "LOCAL" : "REMOTE"} · ${source.id}`;
    identity.append(title, kind);
    const status = document.createElement("span"); status.className = `source-status ${source.transport || "unreachable"}`;
    status.textContent = source.transport === "healthy" ? "CONNECTED" : source.transport === "stale" ? "STALE" : "UNREACHABLE";
    head.append(identity, status);

    const stats = document.createElement("div"); stats.className = "source-stats";
    const values = [
      ["마이너", `${source.online || 0} / ${source.total || 0}`],
      ["블록", source.chain?.block === null || source.chain?.block === undefined ? "—" : Number(source.chain.block).toLocaleString("ko-KR")],
      ["지연", source.latency_ms === null || source.latency_ms === undefined ? "—" : `${fmtNumber(source.latency_ms, 0)} ms`],
      ["스냅샷", source.snapshot_age_seconds === null || source.snapshot_age_seconds === undefined ? "—" : `${fmtDuration(source.snapshot_age_seconds)} 전`],
    ];
    for (const [label, value] of values) {
      const item = document.createElement("div");
      const labelEl = document.createElement("span"); labelEl.textContent = label;
      const valueEl = document.createElement("b"); valueEl.textContent = value;
      item.append(labelEl, valueEl); stats.append(item);
    }
    const task = document.createElement("code"); task.className = "source-task"; task.textContent = shortTask(source.active_task_id);
    task.title = source.active_task_id || "";
    card.append(head, stats, task);
    grid.append(card);
  }
}

function renderRanking(ranking) {
  const body = byId("ranking-body");
  body.replaceChildren();
  text("ranking-task", ranking?.task_id ? `기준 ${shortTask(ranking.task_id)}` : "채점 task 대기");
  text("ranking-previous-task", ranking?.previous_task_id ? `이전 ${shortTask(ranking.previous_task_id)}` : "이전 task —");
  const rows = ranking?.rows || [];
  if (!rows.length) {
    const empty = document.createElement("p");
    empty.className = "ranking-empty";
    empty.textContent = "공식 순위가 발표된 task를 기다리고 있습니다.";
    body.append(empty);
    return;
  }
  for (const item of rows) {
    const row = document.createElement("button");
    row.type = "button";
    row.className = `ranking-row ranking-entry${item.miner_id === selectedMiner ? " selected" : ""}`;
    row.setAttribute("role", "row");
    row.addEventListener("click", () => {
      selectedMiner = item.miner_id;
      localStorage.setItem("niome-selected-miner", selectedMiner);
      render(latestSnapshot);
      byId("detail-label").scrollIntoView({ behavior: "smooth", block: "center" });
    });

    const miner = document.createElement("span"); miner.className = "ranking-miner"; miner.setAttribute("role", "cell");
    const name = document.createElement("strong"); name.textContent = item.label || item.miner_id;
    const source = document.createElement("small"); source.textContent = item.source_label || item.source_id || "—";
    miner.append(name, source);

    const identity = document.createElement("span"); identity.className = "ranking-uid"; identity.setAttribute("role", "cell");
    identity.textContent = `UID ${item.uid ?? "—"}`;

    const rank = document.createElement("span"); rank.className = `ranking-rank rank-${item.rank || "none"}`; rank.setAttribute("role", "cell");
    rank.textContent = item.rank === null || item.rank === undefined ? "미채점" : `#${item.rank}`;

    const score = document.createElement("span"); score.className = "ranking-score"; score.setAttribute("role", "cell");
    score.textContent = fmtNumber(item.score, 6);

    const previous = document.createElement("span"); previous.className = "ranking-old-rank"; previous.setAttribute("role", "cell");
    const previousRank = document.createElement("strong");
    previousRank.textContent = item.previous_rank === null || item.previous_rank === undefined ? "—" : `#${item.previous_rank}`;
    previous.append(previousRank);
    if (item.movement !== null && item.movement !== undefined) {
      const movement = document.createElement("small");
      movement.className = item.movement > 0 ? "rank-up" : item.movement < 0 ? "rank-down" : "rank-flat";
      movement.textContent = item.movement > 0 ? `↑ ${item.movement}` : item.movement < 0 ? `↓ ${Math.abs(item.movement)}` : "— 유지";
      previous.append(movement);
    }
    row.append(miner, identity, rank, score, previous);
    body.append(row);
  }
}

function stageDots(current) {
  if (!current) return ["", "", "", "", ""];
  const safe = current.safe || {};
  const bridge = current.bridge || {};
  return [
    current.received_at ? "complete" : "",
    safe.uploaded ? "complete" : safe.state === "failed" ? "failed" : "active",
    (bridge.round_seeds || []).length ? "complete" : bridge.failure ? "failed" : bridge.started_at ? "active" : "",
    bridge.state === "complete" ? "complete" : bridge.state === "failed" ? "failed" : ["building", "finishing_upload"].includes(bridge.state) ? "active" : "",
    current.official?.published ? "complete" : bridge.state === "complete" ? "active" : "",
  ];
}

function renderMinerCards(miners) {
  const grid = byId("miner-grid");
  grid.replaceChildren();
  for (const miner of miners) {
    const current = miner.current || {};
    const card = document.createElement("button");
    card.type = "button";
    card.className = `miner-card panel ${miner.overall || "healthy"}${miner.id === selectedMiner ? " selected" : ""}`;
    card.setAttribute("aria-pressed", miner.id === selectedMiner ? "true" : "false");
    card.addEventListener("click", () => {
      selectedMiner = miner.id;
      localStorage.setItem("niome-selected-miner", selectedMiner);
      render(latestSnapshot);
      byId("detail-label").scrollIntoView({ behavior: "smooth", block: "center" });
    });

    const head = document.createElement("div"); head.className = "miner-card-head";
    const identity = document.createElement("div");
    const title = document.createElement("strong"); title.textContent = miner.label;
    const uid = document.createElement("span"); uid.textContent = `${miner.source_label || miner.source_id || "Fleet"} · UID ${miner.uid} · :${miner.axon_port}`;
    identity.append(title, uid);
    const state = document.createElement("span"); state.className = `card-status ${miner.overall || "healthy"}`; state.textContent = miner.online ? statusLabel(miner.overall) : "OFFLINE";
    head.append(identity, state);

    const task = document.createElement("code"); task.className = "card-task"; task.textContent = shortTask(current.task_id);
    task.title = current.task_id || "";
    const stage = document.createElement("div"); stage.className = "card-stage";
    const stageText = document.createElement("span"); stageText.textContent = stageLabels[current.stage] || current.stage || "과제 대기";
    const received = document.createElement("small"); received.textContent = fmtTime(current.received_at);
    stage.append(stageText, received);

    const progress = document.createElement("div"); progress.className = "card-progress";
    for (const dotState of stageDots(miner.current)) {
      const dot = document.createElement("span");
      if (dotState) dot.className = dotState;
      progress.append(dot);
    }

    const metrics = document.createElement("div"); metrics.className = "card-metrics";
    const values = [
      ["Local", fmtNumber(current.local?.score, 4)],
      ["Official", current.official?.published ? fmtNumber(current.official.score, 4) : "대기"],
      ["Δ top", miner.federation_comparison?.comparable ? fmtSigned(miner.federation_comparison.delta_to_top, 4) : "—"],
    ];
    for (const [label, value] of values) {
      const item = document.createElement("div");
      const labelEl = document.createElement("span"); labelEl.textContent = label;
      const valueEl = document.createElement("b"); valueEl.textContent = value;
      item.append(labelEl, valueEl); metrics.append(item);
    }
    const resource = document.createElement("div"); resource.className = "card-resource";
    resource.textContent = `CPU ${fmtNumber(miner.resources?.cpu_percent, 1)}% · RAM ${fmtBytes(miner.resources?.memory_bytes)}`;
    card.append(head, task, stage, progress, metrics, resource);
    grid.append(card);
  }
}

function renderComparison(snapshot) {
  const body = byId("comparison-body"); body.replaceChildren();
  const activeTask = snapshot.fleet?.active_task_id;
  for (const miner of snapshot.miners || []) {
    const current = miner.current || {};
    const sameTask = Boolean(activeTask && current.task_id === activeTask);
    const safe = current.safe || {};
    const bridge = current.bridge || {};
    const row = document.createElement("tr");
    row.className = `${miner.id === selectedMiner ? "selected-row" : ""}${sameTask ? "" : " stale-row"}`;
    row.addEventListener("click", () => { selectedMiner = miner.id; localStorage.setItem("niome-selected-miner", selectedMiner); render(snapshot); });
    appendCell(row, miner.source_label || miner.source_id || "—", "source-name-cell");
    appendCell(row, miner.label, "miner-name-cell");
    appendCell(row, String(miner.uid));
    appendCell(row, sameTask ? (stageLabels[current.stage] || current.stage || "대기") : "다른 과제");
    appendCell(row, safe.uploaded ? "완료" : current.task_id ? "진행" : "—", safe.uploaded ? "positive" : "");
    appendCell(row, bridge.state || "—");
    appendCell(row, bridge.put?.http_status ? `HTTP ${bridge.put.http_status}` : bridge.state === "complete" ? "완료" : "—");
    appendCell(row, sameTask ? fmtNumber(current.local?.score, 5) : "—");
    appendCell(row, sameTask && current.official?.published ? `${fmtNumber(current.official.score, 5)} / #${current.official.rank}` : "—");
    const comparison = miner.federation_comparison || {};
    const deltaCell = appendCell(row, comparison.comparable ? fmtSigned(comparison.delta_to_top, 5) : "—");
    if (comparison.comparable) deltaCell.className = Number(comparison.delta_to_top) >= 0 ? "positive" : "negative";
    appendCell(row, `${fmtNumber(miner.resources?.cpu_percent, 1)}%`);
    appendCell(row, fmtBytes(miner.resources?.memory_bytes));
    body.append(row);
  }
}

function appendStep(container, label, meta, state) {
  const item = document.createElement("div"); item.className = `step ${state || ""}`;
  const dot = document.createElement("span"); dot.className = "step-dot";
  const body = document.createElement("div");
  const title = document.createElement("strong"); title.textContent = label;
  const detail = document.createElement("small"); detail.textContent = meta || "—";
  body.append(title, detail); item.append(dot, body); container.append(item);
}

function renderLanes(current) {
  const safeContainer = byId("safe-steps"); safeContainer.replaceChildren();
  const bridgeContainer = byId("bridge-steps"); bridgeContainer.replaceChildren();
  const safe = current?.safe || {};
  const bridge = current?.bridge || {};
  appendStep(safeContainer, "쿼리 수신", fmtTime(current?.received_at), current?.received_at ? "complete" : "");
  appendStep(safeContainer, "기본 결과 생성", safe.submission_rows ? `${safe.submission_rows}개 실험` : "대기", safe.submission_rows ? "complete" : current ? "active" : "");
  appendStep(safeContainer, "S3 안전 제출", safe.uploaded ? `${fmtTime(safe.uploaded_at)} · ${fmtDuration(safe.upload_elapsed_seconds)}` : "대기", safe.uploaded ? "complete" : safe.state === "failed" ? "failed" : current ? "active" : "");

  const seedsReady = Array.isArray(bridge.round_seeds) && bridge.round_seeds.length > 0;
  const failed = bridge.state === "failed" || Boolean(bridge.failure);
  appendStep(bridgeContainer, "스트림 연결", bridge.started_at ? fmtTime(bridge.started_at) : "대기", bridge.started_at ? "complete" : current ? "active" : "");
  appendStep(bridgeContainer, "시드 확보", seedsReady ? `[${bridge.round_seeds.join(", ")}]` : bridge.seed_target_block ? `${bridge.blocks_to_seeds ?? "—"} 블록 남음` : "대기", seedsReady ? "complete" : failed ? "failed" : bridge.started_at ? "active" : "");
  appendStep(bridgeContainer, "결과 생성", bridge.build_elapsed_seconds ? fmtDuration(bridge.build_elapsed_seconds) : "대기", bridge.state === "complete" || bridge.state === "finishing_upload" ? "complete" : bridge.state === "building" ? "active" : failed ? "failed" : "");
  appendStep(bridgeContainer, "최종 PUT", bridge.put?.http_status ? `HTTP ${bridge.put.http_status} · ${bridge.put.label || ""}` : "대기", bridge.state === "complete" ? "complete" : bridge.state === "finishing_upload" ? "active" : failed ? "failed" : "");
  appendStep(bridgeContainer, "공식 점수", current?.official?.published ? `${fmtNumber(current.official.score, 5)} · #${current.official.rank}` : "검증 대기", current?.official?.published ? "complete" : bridge.state === "complete" ? "active" : "");
}

function renderMetrics(miner) {
  const current = miner.current || {};
  const local = current.local || {};
  const official = current.official || {};
  const breakdown = (official.published ? official.breakdown : local.breakdown) || {};
  text("local-score", fmtNumber(local.score, 6));
  const localSource = local.score_semantics === "unknown-seed-holdout-estimate"
    ? "미지 seed 홀드아웃 추정"
    : local.score_semantics === "provisional-seed-estimate"
      ? "잠정 seed 추정"
      : local.source === "optimized_bridge"
        ? "공식 seed 재현"
        : local.source === "safe_submission"
          ? "안전 제출"
          : "채점 대기";
  text("local-source", localSource);
  text("official-score", official.published ? fmtNumber(official.score, 6) : "—");
  text("official-rank", official.published ? `${official.participants}명 중 #${official.rank}` : "공식 검증 전");
  const comparison = miner.federation_comparison || {};
  text("score-delta", comparison.comparable ? fmtSigned(comparison.delta_to_top, 6) : "—");
  text("delta-source", comparison.comparable ? `${comparison.score_source === "official" ? "공식" : "로컬"} 점수 · ${comparison.participants}개 결과 중 #${comparison.rank}` : "동일 과제 비교 점수 대기");
  const consistency = breakdown.consistency_factor ?? breakdown.consistency_score;
  text("consistency-score", consistency === null || consistency === undefined ? "—" : `${fmtNumber(Number(consistency) <= 1 ? Number(consistency) * 100 : consistency, 3)}%`);
  text("weighted-score", fmtNumber(breakdown.total_weighted_score, 5));
  text("fidelity-score", fmtNumber(breakdown.distribution_fidelity_factor ?? breakdown.distribution_fidelity_score, 5));
  text("valid-experiments", breakdown.n_valid_experiments ?? local.valid_experiments ?? "—");
  const sha = current.bridge?.submission_sha256 || current.safe?.submission_sha256;
  text("submission-sha", sha ? `${sha.slice(0, 10)}…${sha.slice(-8)}` : "—");
}

function renderStreams(current) {
  const streams = current?.bridge?.streams || [];
  const grid = byId("stream-grid"); grid.replaceChildren();
  const live = streams.filter((item) => ["opening", "streaming", "complete"].includes(item.state));
  text("stream-summary", streams.length ? `${live.length} / ${streams.length} 생존` : "대기");
  if (!streams.length) {
    const empty = document.createElement("p"); empty.className = "empty"; empty.textContent = "스트림 정보를 기다리고 있습니다."; grid.append(empty); return;
  }
  for (const stream of streams) {
    const card = document.createElement("div"); card.className = "stream-card";
    const top = document.createElement("div"); top.className = "stream-top";
    const name = document.createElement("strong"); name.textContent = String(stream.label || "unknown").toUpperCase();
    const state = document.createElement("span"); state.className = `stream-state ${["opening", "streaming", "complete"].includes(stream.state) ? "live" : "failed"}`; state.textContent = stream.state || "unknown";
    top.append(name, state);
    const bar = document.createElement("div"); bar.className = "stream-bar";
    const fill = document.createElement("span"); fill.style.width = `${Math.max(1, Math.min(100, Number(stream.progress || 0) * 100))}%`; bar.append(fill);
    const meta = document.createElement("div"); meta.className = "stream-meta";
    for (const [label, value] of [["전송", `${fmtBytes(stream.bytes_sent)} / ${fmtBytes(stream.total_bytes)}`], ["마지막 성공", stream.last_send_age_seconds === null ? "—" : `${fmtDuration(stream.last_send_age_seconds)} 전`], ["전송 횟수", fmtNumber(stream.send_count, 0)]]) {
      const row = document.createElement("div"); const left = document.createElement("span"); left.textContent = label; const right = document.createElement("b"); right.textContent = value; row.append(left, right); meta.append(row);
    }
    card.append(top, bar, meta); grid.append(card);
  }
}

function renderProcesses(miner) {
  const list = byId("process-list"); list.replaceChildren();
  for (const role of ["miner", "bridge"]) {
    const process = miner.processes?.[role];
    const row = document.createElement("div"); row.className = "process-row";
    const left = document.createElement("div");
    const title = document.createElement("strong"); title.textContent = process?.name || role;
    const meta = document.createElement("small"); meta.textContent = process ? `PID ${process.pid} · CPU ${fmtNumber(process.cpu_percent, 1)}% · ${fmtBytes(process.memory_bytes)}` : "프로세스 정보 없음";
    left.append(title, meta);
    const state = document.createElement("span"); state.className = `process-status ${process?.status === "online" ? "" : "offline"}`; state.textContent = process?.status || "없음";
    row.append(left, state); list.append(row);
  }
  text("resource-cpu", `${fmtNumber(miner.resources?.cpu_percent, 1)}%`);
  text("resource-ram", fmtBytes(miner.resources?.memory_bytes));
  text("resource-restarts", `${fmtNumber(miner.resources?.restarts, 0)}회`);
}

function renderAlerts(miner) {
  const alerts = [...(miner.alerts || [])];
  const list = byId("alert-list"); list.replaceChildren();
  text("alert-count", String(alerts.length));
  if (!alerts.length) alerts.push({ severity: "ok", title: "현재 감지된 위험 없음", detail: "프로세스·설정·진행 상태가 정상 범위입니다." });
  for (const alert of alerts) {
    const category = alert.severity === "ok" ? { title: alert.title, style: alert.detail } : classifyAlert(alert);
    const item = document.createElement("div"); item.className = `alert-item ${alert.severity}`;
    const signal = document.createElement("span"); signal.className = "signal";
    const body = document.createElement("div"); const title = document.createElement("strong"); title.textContent = category.title; const detail = document.createElement("p"); detail.textContent = category.style; body.append(title, detail); item.append(signal, body); list.append(item);
  }
}

function renderHistory(history) {
  const body = byId("history-body"); body.replaceChildren();
  for (const item of history || []) {
    const row = document.createElement("tr");
    const values = [fmtTime(item.received_at, true), item.task_id, item.safe_uploaded ? "완료" : "—", item.bridge_state || "—", item.seeds?.length ? `[${item.seeds.join(", ")}]` : "—", fmtNumber(item.local_score, 5), item.official_rank ? `${fmtNumber(item.official_score, 5)} · #${item.official_rank}` : "—"];
    values.forEach((value, index) => appendCell(row, value, index === 1 ? "task-cell" : index === 2 && value === "완료" ? "positive" : ""));
    body.append(row);
  }
  if (!history?.length) {
    const row = document.createElement("tr"); const cell = document.createElement("td"); cell.colSpan = 7; cell.className = "empty-cell"; cell.textContent = "아직 수신한 작업이 없습니다."; row.append(cell); body.append(row);
  }
}

function renderSelected(miner) {
  text("detail-label", miner.label);
  text("detail-source", miner.source_label || miner.source_id || "—");
  text("detail-profile", miner.profile);
  renderLanes(miner.current);
  renderMetrics(miner);
  renderStreams(miner.current);
  renderProcesses(miner);
  renderAlerts(miner);
  renderHistory(miner.history);
}

function maybeNotify(snapshot) {
  if (!("Notification" in window) || Notification.permission !== "granted") return;
  const critical = (snapshot.fleet?.alerts || []).find((item) => item.severity === "critical");
  const published = (snapshot.miners || []).find((item) => item.current?.official?.published);
  const key = critical ? `${critical.miner}:${critical.code}` : published ? `${published.id}:${published.current.task_id}:official:${published.current.official.rank}` : "";
  if (!key || key === lastNoticeKey) return;
  const title = critical ? `NIOME 경보 · ${critical.miner}` : `NIOME 공식 점수 · ${published.label}`;
  const body = critical ? `${classifyAlert(critical).title} · ${shortMiner(critical.miner)}` : `${fmtNumber(published.current.official.score, 6)}점 · #${published.current.official.rank}`;
  new Notification(title, { body, tag: key });
  lastNoticeKey = key; localStorage.setItem("niome-fleet-last-notice", key);
}

function render(snapshot) {
  if (!snapshot) return;
  latestSnapshot = snapshot;
  const miners = snapshot.miners || [];
  if (!miners.some((item) => item.id === selectedMiner)) selectedMiner = miners[0]?.id || "bitcoin-hype-fleet:bitcoin1";
  renderFleetHeader(snapshot);
  renderRanking(snapshot.ranking || {});
  renderSources(snapshot.sources || []);
  text("miner-count-heading", `${miners.length}개 통합 마이너 상태`);
  renderMinerCards(miners);
  renderComparison(snapshot);
  const selected = miners.find((item) => item.id === selectedMiner);
  if (selected) renderSelected(selected);
  maybeNotify(snapshot);
}

function setConnection(state, label) {
  const pill = byId("connection-pill"); pill.className = `connection-pill ${state}`; text("connection-text", label);
}

function updateFreshness() {
  if (!latestSnapshot?.generated_at) return;
  const age = Math.max(0, (Date.now() - new Date(latestSnapshot.generated_at).getTime()) / 1000);
  text("freshness", age < 4 ? "방금" : `${Math.round(age)}초 전`);
  if (age > 25) setConnection("offline", "데이터 지연");
}

async function fetchState() {
  try {
    const response = await fetch("/api/v1/federation/state", { cache: "no-store" });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    render(await response.json()); setConnection("live", "실시간 연결");
  } catch (_) { setConnection("offline", "연결 재시도"); }
}

function connectEvents() {
  const source = new EventSource("/api/v1/federation/events");
  source.onopen = () => { eventStreamLive = true; setConnection("live", "실시간 연결"); };
  source.addEventListener("state", (event) => {
    try { render(JSON.parse(event.data)); setConnection("live", "실시간 연결"); }
    catch (_) { setConnection("offline", "데이터 오류"); }
  });
  source.onerror = () => { eventStreamLive = false; setConnection("offline", "재연결 중"); };
}

byId("notify-button").addEventListener("click", async () => {
  if (!("Notification" in window)) { text("notify-button", "알림 미지원"); return; }
  const permission = await Notification.requestPermission();
  text("notify-button", permission === "granted" ? "브라우저 알림 켜짐" : "알림 허용 필요");
});

if ("Notification" in window && Notification.permission === "granted") text("notify-button", "브라우저 알림 켜짐");
fetchState();
connectEvents();
setInterval(updateFreshness, 1000);
setInterval(() => { if (!eventStreamLive) fetchState(); }, 15000);
