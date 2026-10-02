"use strict";

const byId = (id) => document.getElementById(id);
const stageLabels = {
  query_received: "쿼리 수신",
  safe_uploaded: "일반 제출 완료",
  building: "제출물 생성",
  finishing_upload: "S3 업로드",
  waiting_official: "공식 검증 대기",
  official_published: "공식 점수 발표",
};
const ACTIVE_MINERS = new Set(["tao1", "tao2", "won1", "won2"]);
let latestSnapshot = null;
let selectedMiner = localStorage.getItem("niome-selected-miner") || "";
let lastNoticeKey = localStorage.getItem("niome-fleet-last-notice") || "";
let eventStreamLive = false;

const researchPhaseLabels = {
  waiting_for_automation: "자동화기 대기",
  fingerprint_build: "Fingerprint 생성",
  probe_score_wait: "공식 점수 감시",
  dataset_discovery: "Discovery 수집",
  historical_holdout: "Holdout 구성",
  generator_search: "생성기 가설 탐색",
  shadow_prediction: "사전 Shadow 검증",
  eligible_for_review: "운영 검토 가능",
};

const trackStatusLabels = {
  waiting: "대기",
  collecting: "증거 수집",
  active: "실행 중",
  searching: "가설 탐색",
  validating: "검증 중",
  blocked: "관문 미통과",
  inconclusive: "증거 부족",
  complete: "목표 검증",
  eligible: "검토 가능",
};

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

function firstDefined(...values) {
  return values.find((value) => value !== undefined && value !== null && value !== "");
}

function trackText(value, fallback) {
  if (typeof value === "string" || typeof value === "number") return String(value);
  return fallback;
}

function normalizeGates(rawGates, fallbackGates) {
  if (Array.isArray(rawGates)) {
    return rawGates.map((gate, index) => typeof gate === "object" ? {
      label: trackText(firstDefined(gate.label, gate.title, gate.name), `관문 ${index + 1}`),
      detail: trackText(firstDefined(gate.detail, gate.value, gate.summary), ""),
      complete: gate.complete === true || gate.passed === true || gate.status === "complete" || gate.status === "passed",
      active: gate.active === true || gate.status === "active" || gate.status === "running",
    } : { label: String(gate), detail: "", complete: false, active: false });
  }
  if (rawGates && typeof rawGates === "object") {
    return Object.entries(rawGates).map(([name, gate]) => typeof gate === "object" ? {
      label: trackText(firstDefined(gate.label, gate.title), name.replaceAll("_", " ")),
      detail: trackText(firstDefined(gate.detail, gate.value, gate.summary), ""),
      complete: gate.complete === true || gate.passed === true || gate.status === "complete" || gate.status === "passed",
      active: gate.active === true || gate.status === "active" || gate.status === "running",
    } : { label: name.replaceAll("_", " "), detail: "", complete: gate === true, active: false });
  }
  return fallbackGates;
}

function renderTrackStatus(prefix, status, label) {
  const el = byId(`${prefix}-status`);
  if (!el) return;
  const normalized = String(status || "waiting").toLowerCase();
  const style = ["complete", "eligible", "passed"].includes(normalized)
    ? "complete"
    : ["blocked", "inconclusive", "failed"].includes(normalized) ? "blocked" : normalized === "waiting" ? "waiting" : "active";
  el.className = `track-status ${style}`;
  el.textContent = label || trackStatusLabels[normalized] || researchPhaseLabels[normalized] || String(status || "대기");
}

function renderTrackGates(id, gates) {
  const container = byId(id);
  if (!container) return;
  container.replaceChildren();
  gates.forEach((gate, index) => {
    const item = document.createElement("div");
    item.className = `track-gate ${gate.complete ? "complete" : gate.active ? "active" : "waiting"}`;
    const mark = document.createElement("span");
    mark.className = "track-gate-mark";
    mark.textContent = gate.complete ? "✓" : String(index + 1);
    const label = document.createElement("strong");
    label.textContent = gate.label;
    const detail = document.createElement("small");
    detail.textContent = gate.detail || (gate.complete ? "통과" : gate.active ? "진행 중" : "대기");
    item.append(mark, label, detail);
    container.append(item);
  });
}

function normalizeGeneratorTrack(state) {
  const raw = state.tracks?.generator || {};
  const dataset = state.dataset || {};
  const probe = state.probe || {};
  const hypothesis = state.hypothesis || {};
  const forward = state.forward || {};
  const targets = state.targets || {};
  const discoveryTarget = targets.discovery || 20;
  const holdoutTarget = targets.holdout || 5;
  const forwardTarget = targets.forward_exact || forward.target || 5;
  const fallbackAction = hypothesis.accepted_model ? {
    title: "승인 가설의 사전 Shadow 검증",
    detail: "점수 공개 전에 예측값과 기록 시각을 고정하고 exact 여부를 검증합니다.",
  } : {
    title: "기각된 직접 변환군 밖의 생성기 탐색",
    detail: `${fmtNumber(hypothesis.tested_models || 0, 0)}개 기존 가설은 회귀 기준으로 보존하고 새 생성기 계열을 검증합니다.`,
  };
  const action = raw.current_action || raw.action || fallbackAction;
  const fallbackGates = [
    { label: "Discovery 라벨", detail: `${dataset.discovery_tasks || 0} / ${discoveryTarget}`, complete: (dataset.discovery_tasks || 0) >= discoveryTarget },
    { label: "독립 Holdout", detail: `${dataset.holdout_tasks || 0} / ${holdoutTarget}`, complete: (dataset.holdout_tasks || 0) >= holdoutTarget },
    { label: "가설 exact 통과", detail: hypothesis.accepted_model || "승인 모델 없음", complete: Boolean(hypothesis.accepted_model), active: !hypothesis.accepted_model },
    { label: "공개 전 예측 기록", detail: `${forward.recorded_predictions || 0}회`, complete: (forward.recorded_predictions || 0) > 0 },
    { label: "사전 exact 재현", detail: `${forward.consecutive_exact || 0} / ${forwardTarget}`, complete: (forward.consecutive_exact || 0) >= forwardTarget },
  ];
  const fallbackStatus = forward.eligible ? "complete" : hypothesis.accepted_model ? "validating" : "searching";
  return {
    status: firstDefined(raw.status, raw.phase, fallbackStatus),
    statusLabel: trackText(firstDefined(raw.status_label, raw.label), null),
    action: {
      title: trackText(firstDefined(action.title, raw.title), fallbackAction.title),
      detail: trackText(firstDefined(action.detail, action.summary, raw.detail), fallbackAction.detail),
      taskId: firstDefined(action.task_id, raw.task_id),
    },
    labels: trackText(firstDefined(raw.metrics?.labels, raw.evidence?.label_count), `${dataset.latest_epoch_seed_values || 0}`),
    labelsMeta: trackText(firstDefined(raw.metrics?.labels_detail, raw.evidence?.labels_detail), `${dataset.latest_epoch_tasks || 0} task 안정 epoch`),
    models: trackText(firstDefined(raw.metrics?.tested_models, raw.evidence?.tested_models), fmtNumber(hypothesis.tested_models || 0, 0)),
    modelsMeta: trackText(firstDefined(raw.metrics?.model_status, raw.evidence?.model_status), hypothesis.accepted_model ? `승인 ${hypothesis.accepted_model}` : "승인 후보 없음"),
    forward: trackText(firstDefined(raw.metrics?.forward_exact, raw.evidence?.forward_exact), `${forward.consecutive_exact || 0} / ${forwardTarget}`),
    forwardMeta: trackText(firstDefined(raw.metrics?.forward_detail, raw.evidence?.forward_detail), "공개 전 기록만 인정"),
    evidence: trackText(firstDefined(raw.evidence_summary, raw.evidence?.summary, raw.summary), `Probe ${probe.exact_tasks || 0}회 exact은 점수 공개 후 역산 검증이며 사전예측 성공에는 포함하지 않습니다.`),
    gates: normalizeGates(raw.gates, fallbackGates),
    safety: trackText(firstDefined(raw.safety?.label, raw.authorization?.label, raw.safety_label), state.safety?.submission_writes === false ? "SHADOW · 제출 쓰기 없음" : "안전 설정 확인 필요"),
    safetyWarning: state.safety?.submission_writes !== false && !raw.safety?.authorized,
  };
}

function normalizeEarlyScoreTrack(state) {
  const raw = state.tracks?.early_score || {};
  const probe = state.probe || {};
  const evidence = raw.evidence || {};
  const metrics = raw.metrics || {};
  const hits = Number(firstDefined(metrics.actionable_individual_scores, metrics.early_individual_hits, evidence.early_individual_hits, raw.early_individual_hits, 0));
  const batchCount = Number(firstDefined(metrics.batch_observations, evidence.batch_observations, raw.batch_observations, probe.captured_tasks, 0));
  const leadSeconds = firstDefined(metrics.lead_time_seconds, evidence.lead_time_seconds, raw.lead_time_seconds);
  const action = raw.current_action || raw.action || {};
  const fallbackGates = [
    { label: "일괄 공개 기준선", detail: `${batchCount} task 시간축 확보`, complete: batchCount > 0 },
    { label: "개별 점수 신호 포착", detail: `${hits}회`, complete: hits > 0, active: hits === 0 },
    { label: "batch 이전성 입증", detail: "서버 시각으로 선후 검증", complete: raw.precedes_batch === true || metrics.precedes_batch === true },
    { label: "실행 가능 시간창", detail: "결과 생성·PUT 여유 검증", complete: raw.actionable_window_verified === true || metrics.actionable_window_verified === true },
    { label: "독립 라운드 재현", detail: trackText(firstDefined(metrics.reproduced_rounds, raw.reproduced_rounds), "0회"), complete: raw.reproducible === true || metrics.reproducible === true },
  ];
  return {
    status: firstDefined(raw.status, raw.phase, hits > 0 ? "validating" : "searching"),
    statusLabel: trackText(firstDefined(raw.status_label, raw.label), null),
    action: {
      title: trackText(firstDefined(action.title, raw.title), "허가된 조기 점수 관측면 탐색"),
      detail: trackText(firstDefined(action.detail, action.summary, raw.detail), "개별 결과가 batch 게시보다 앞서는지 동일 시계 기준으로 계측합니다."),
      taskId: firstDefined(action.task_id, raw.task_id),
    },
    hits: trackText(firstDefined(metrics.actionable_individual_scores, metrics.early_individual_hits, evidence.early_individual_hits), `${hits}`),
    hitsMeta: trackText(firstDefined(metrics.hits_detail, evidence.hits_detail), "batch보다 앞선 결과만 인정"),
    lead: leadSeconds === undefined ? "—" : fmtDuration(leadSeconds),
    leadMeta: trackText(firstDefined(metrics.lead_detail, evidence.lead_detail), leadSeconds === undefined ? "입증된 시간창 없음" : "batch 게시 대비"),
    batch: trackText(firstDefined(metrics.batch_observations, evidence.batch_observations), `${batchCount}`),
    batchMeta: trackText(firstDefined(metrics.batch_detail, evidence.batch_detail), "관측됐지만 조기 신호 아님"),
    evidence: trackText(firstDefined(raw.evidence_summary, evidence.summary, raw.summary), hits > 0 ? "조기 후보 신호가 포착됐으며 batch 이전성과 사용 가능한 시간창을 추가 검증합니다." : "현재 확보한 점수는 batch 공개 후 신호입니다. 실행 가능한 조기 개별 점수 증거는 아직 없습니다."),
    gates: normalizeGates(raw.gates, fallbackGates),
    safety: trackText(firstDefined(raw.safety?.label, raw.authorization?.label, raw.safety_label), "READ ONLY · 허가된 경로만 관측"),
    safetyWarning: raw.safety?.authorized === false || raw.authorization?.authorized === false,
  };
}

function renderResearch(state) {
  if (!state) return;
  const automation = state.automation || {};
  const targets = state.targets || {};
  const probe = state.probe || {};
  const generator = normalizeGeneratorTrack(state);
  const earlyScore = normalizeEarlyScoreTrack(state);

  renderTrackStatus("generator", generator.status, generator.statusLabel);
  text("generator-action-title", generator.action.title);
  text("generator-action-detail", generator.action.detail);
  text("generator-action-task", generator.action.taskId ? `task ${shortTask(generator.action.taskId)}` : "task 자동 선택");
  text("generator-labels", generator.labels);
  text("generator-labels-meta", generator.labelsMeta);
  text("generator-models", generator.models);
  text("generator-models-meta", generator.modelsMeta);
  text("generator-forward", generator.forward);
  text("generator-forward-meta", generator.forwardMeta);
  text("generator-evidence", generator.evidence);
  text("generator-safety", generator.safety);
  byId("generator-safety")?.classList.toggle("warning", generator.safetyWarning);
  renderTrackGates("generator-gates", generator.gates);

  renderTrackStatus("early-score", earlyScore.status, earlyScore.statusLabel);
  text("early-score-action-title", earlyScore.action.title);
  text("early-score-action-detail", earlyScore.action.detail);
  text("early-score-action-task", earlyScore.action.taskId ? `task ${shortTask(earlyScore.action.taskId)}` : "task 자동 선택");
  text("early-score-hits", earlyScore.hits);
  text("early-score-hits-meta", earlyScore.hitsMeta);
  text("early-score-lead", earlyScore.lead);
  text("early-score-lead-meta", earlyScore.leadMeta);
  text("early-score-batch", earlyScore.batch);
  text("early-score-batch-meta", earlyScore.batchMeta);
  text("early-score-evidence", earlyScore.evidence);
  text("early-score-safety", earlyScore.safety);
  byId("early-score-safety")?.classList.toggle("warning", earlyScore.safetyWarning);
  renderTrackGates("early-score-gates", earlyScore.gates);

  text("research-probe", `${probe.exact_tasks || 0} / ${targets.probe_verification || 5} exact`);
  text("research-probe-meta", probe.latest?.recovery_latency_seconds === null || probe.latest?.recovery_latency_seconds === undefined ? "점수 공개 후 seed 역산 검증" : `최근 공개 후 ${fmtDuration(probe.latest.recovery_latency_seconds)} · 사전예측 아님`);
  text("research-health", automation.supervisor_healthy ? "자동화기 ONLINE" : "자동화기 CHECK");
  text("research-updated", state.generated_at ? `갱신 ${fmtTime(state.generated_at)}` : "갱신 대기");

  const rounds = byId("research-rounds");
  rounds.replaceChildren();
  const recent = (state.recent_rounds || []).slice(0, 6);
  if (!recent.length) {
    const empty = document.createElement("p"); empty.className = "research-round-empty"; empty.textContent = "공개 seed 라벨을 기다리고 있습니다."; rounds.append(empty);
  }
  for (const round of recent) {
    const row = document.createElement("div"); row.className = "research-round";
    const task = document.createElement("code"); task.textContent = shortTask(round.task_id);
    const partition = document.createElement("span"); partition.className = `research-partition ${round.partition || "observed"}`; partition.textContent = String(round.partition || "observed").toUpperCase();
    const seed = document.createElement("strong"); seed.textContent = round.official_seeds?.length ? `[${round.official_seeds.join(", ")}]` : "seed 대기";
    const check = document.createElement("small");
    check.textContent = round.prediction ? (round.prediction.exact === true ? "PREDICT ✓" : round.prediction.exact === false ? "PREDICT ✕" : "PREDICT 대기") : round.probe?.exact_unordered ? "PROBE ✓" : "PUBLIC";
    row.append(task, partition, seed, check); rounds.append(row);
  }
}

async function fetchResearch() {
  try {
    const response = await fetch("/api/v1/seed-research/state", { cache: "no-store" });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    renderResearch(await response.json());
  } catch (_) {
    renderTrackStatus("generator", "blocked", "상태 연결 재시도");
    renderTrackStatus("early-score", "blocked", "상태 연결 재시도");
    text("research-health", "자동화기 OFFLINE");
  }
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
  const criticals = (fleet.alerts || []).filter((item) => {
    const miner = shortMiner(item.miner);
    const signal = `${item.code || ""} ${item.title || ""}`.toLowerCase();
    return item.severity === "critical" && ACTIVE_MINERS.has(miner) && !signal.includes("bridge");
  });
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
    const active = (snapshot.miners || []).filter((miner) => ACTIVE_MINERS.has(miner.label));
    const meta = document.createElement("small"); meta.textContent = `${active.filter((miner) => miner.online).length}/${active.length} 활성 마이너 online`;
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

function renderFleetHeader(snapshot, miners) {
  const fleet = snapshot.fleet || {};
  const chain = snapshot.chain || {};
  const badge = byId("overall-badge");
  badge.className = `badge ${fleet.overall || "healthy"}`;
  const online = miners.filter((miner) => miner.online).length;
  const coverage = miners.filter((miner) => miner.current?.task_id === fleet.active_task_id && fleet.active_task_id).length;
  const submitted = miners.filter((miner) => miner.current?.safe?.uploaded && miner.current?.task_id === fleet.active_task_id).length;
  badge.textContent = online === miners.length ? `${online}/${miners.length} 온라인` : `${online}/${miners.length} 확인 필요`;
  text("coverage-label", `현재 과제 수신 ${coverage}/${miners.length}`);
  text("active-task", fleet.active_task_id || "작업을 기다리고 있습니다");
  text("online-count", `${online} / ${miners.length}`);
  text("submitted-count", `${submitted} / ${miners.length}`);
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

function renderRanking(ranking, activeIds) {
  const body = byId("ranking-body");
  body.replaceChildren();
  text("ranking-task", ranking?.task_id ? `기준 ${shortTask(ranking.task_id)}` : "채점 task 대기");
  text("ranking-previous-task", ranking?.previous_task_id ? `이전 ${shortTask(ranking.previous_task_id)}` : "이전 task —");
  const rows = (ranking?.rows || []).filter((item) => activeIds.has(item.miner_id) || ACTIVE_MINERS.has(item.label));
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
  return [
    current.received_at ? "complete" : "",
    safe.submission_rows ? "complete" : current.received_at ? "active" : "",
    safe.uploaded ? "complete" : safe.state === "failed" ? "failed" : current.received_at ? "active" : "",
    current.local?.score !== null && current.local?.score !== undefined ? "complete" : safe.uploaded ? "active" : "",
    current.official?.published ? "complete" : safe.uploaded ? "active" : "",
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
    const row = document.createElement("tr");
    row.className = `${miner.id === selectedMiner ? "selected-row" : ""}${sameTask ? "" : " stale-row"}`;
    row.addEventListener("click", () => { selectedMiner = miner.id; localStorage.setItem("niome-selected-miner", selectedMiner); render(snapshot); });
    appendCell(row, miner.label, "miner-name-cell");
    appendCell(row, String(miner.uid));
    appendCell(row, sameTask ? (stageLabels[current.stage] || current.stage || "대기") : "다른 과제");
    appendCell(row, safe.uploaded ? "완료" : current.task_id ? "진행" : "—", safe.uploaded ? "positive" : "");
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
  const container = byId("submission-steps"); container.replaceChildren();
  const safe = current?.safe || {};
  const localReady = current?.local?.score !== null && current?.local?.score !== undefined;
  appendStep(container, "쿼리 수신", fmtTime(current?.received_at), current?.received_at ? "complete" : "");
  appendStep(container, "제출물 생성", safe.submission_rows ? `${safe.submission_rows}개 실험` : "대기", safe.submission_rows ? "complete" : current?.received_at ? "active" : "");
  appendStep(container, "S3 업로드", safe.uploaded ? `${fmtTime(safe.uploaded_at)} · ${fmtDuration(safe.upload_elapsed_seconds)}` : "대기", safe.uploaded ? "complete" : safe.state === "failed" ? "failed" : current?.received_at ? "active" : "");
  appendStep(container, "로컬 검증", localReady ? `${fmtNumber(current.local.score, 5)}점` : "대기", localReady ? "complete" : safe.uploaded ? "active" : "");
  appendStep(container, "공식 점수", current?.official?.published ? `${fmtNumber(current.official.score, 5)} · #${current.official.rank}` : "검증 대기", current?.official?.published ? "complete" : safe.uploaded ? "active" : "");
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
      : local.source === "safe_submission"
          ? "안전 제출"
          : "채점 대기";
  text("local-source", localSource);
  const calibration = current.calibration || {};
  text("calibrated-score", calibration.available ? fmtNumber(calibration.estimate, 6) : "—");
  text(
    "calibrated-range",
    calibration.available
      ? `P10–P90 ${fmtNumber(calibration.lower, 3)}–${fmtNumber(calibration.upper, 3)} · n=${calibration.records} · ${calibration.confidence === "high" ? "높음" : calibration.confidence === "moderate" ? "보통" : "낮음"}`
      : calibration.reason === "insufficient-training-records"
        ? `학습자료 ${calibration.records || 0}/${calibration.minimum_records || 8}`
        : "과거 자료 학습 대기",
  );
  text("official-score", official.published ? fmtNumber(official.score, 6) : "—");
  text(
    "official-rank",
    official.published
      ? official.reached_target
        ? `${official.participants}명 중 #${official.rank} · 목표 달성`
        : `${official.participants}명 중 #${official.rank} · ${official.target_rank || 30}위 컷까지 ${fmtSigned(official.gap_to_target, 3)}`
      : "공식 검증 전",
  );
  const comparison = miner.federation_comparison || {};
  text("score-delta", comparison.comparable ? fmtSigned(comparison.delta_to_top, 6) : "—");
  text("delta-source", comparison.comparable ? `${comparison.score_source === "official" ? "공식" : "로컬"} 점수 · ${comparison.participants}개 결과 중 #${comparison.rank}` : "동일 과제 비교 점수 대기");
  const consistency = breakdown.consistency_factor ?? breakdown.consistency_score;
  text("consistency-score", consistency === null || consistency === undefined ? "—" : `${fmtNumber(Number(consistency) <= 1 ? Number(consistency) * 100 : consistency, 3)}%`);
  text("weighted-score", fmtNumber(breakdown.total_weighted_score, 5));
  text("fidelity-score", fmtNumber(breakdown.distribution_fidelity_factor ?? breakdown.distribution_fidelity_score, 5));
  text("valid-experiments", breakdown.n_valid_experiments ?? local.valid_experiments ?? "—");
  const sha = current.safe?.submission_sha256;
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
  for (const role of ["miner"]) {
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
  const alerts = (miner.alerts || []).filter((alert) => !`${alert.code || ""} ${alert.title || ""}`.toLowerCase().includes("bridge"));
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
    const values = [fmtTime(item.received_at, true), item.task_id, item.safe_uploaded ? "완료" : "—", fmtNumber(item.local_score, 5), item.official_rank ? `${fmtNumber(item.official_score, 5)} · #${item.official_rank}` : "—"];
    values.forEach((value, index) => appendCell(row, value, index === 1 ? "task-cell" : index === 2 && value === "완료" ? "positive" : ""));
    body.append(row);
  }
  if (!history?.length) {
    const row = document.createElement("tr"); const cell = document.createElement("td"); cell.colSpan = 5; cell.className = "empty-cell"; cell.textContent = "아직 수신한 작업이 없습니다."; row.append(cell); body.append(row);
  }
}

function renderSelected(miner) {
  text("detail-label", miner.label);
  text("detail-source", miner.source_label || miner.source_id || "—");
  text("detail-profile", miner.profile);
  renderLanes(miner.current);
  renderMetrics(miner);
  renderProcesses(miner);
  renderAlerts(miner);
  renderHistory(miner.history);
}

function maybeNotify(snapshot) {
  if (!("Notification" in window) || Notification.permission !== "granted") return;
  const critical = (snapshot.fleet?.alerts || []).find((item) => {
    const signal = `${item.code || ""} ${item.title || ""}`.toLowerCase();
    return item.severity === "critical" && ACTIVE_MINERS.has(shortMiner(item.miner)) && !signal.includes("bridge");
  });
  const published = (snapshot.miners || []).find((item) => ACTIVE_MINERS.has(item.label) && item.current?.official?.published);
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
  const miners = (snapshot.miners || []).filter((item) => ACTIVE_MINERS.has(item.label));
  if (!miners.some((item) => item.id === selectedMiner)) selectedMiner = miners[0]?.id || "";
  renderFleetHeader(snapshot, miners);
  renderRanking(snapshot.ranking || {}, new Set(miners.map((item) => item.id)));
  text("miner-count-heading", `${miners.length}개 활성 마이너 상태`);
  renderMinerCards(miners);
  renderComparison({ ...snapshot, miners });
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
