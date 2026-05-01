// cc-log-viewer frontend v3 — turn-based grouped timeline.
// Vanilla JS. No build step.

(() => {
"use strict";

// ===== Constants =====
const ROW_H_PRIMARY = 84;       // user / assistant_text card
const ROW_H_SPAN = 32;           // collapsed span "+ N tools, M thinking, ..."
const ROW_H_INNER = 28;          // expanded span's inner row
const ROW_H_COMPACT = 30;        // compaction divider
const ROW_BUFFER_PX = 240;
const STUB_PAGE = 200;
const PROJECT_POLL_MS = 800;
const SESSION_POLL_MS = 700;
const STUB_RETRY_MS = 900;
const MAX_STUB_RETRIES = 30;
const ENTRY_BATCH_NEIGHBORS = 3;
const MAX_FETCHED_ENTRIES = 200;

// "Primary" classes — get full cards in the timeline.
// Everything else is bucketed into spans between primaries.
const PRIMARY_CLASSES = new Set(["u", "a"]);

const FILTER_LABELS = {
  u: "user", a: "assistant", T: "thinking", t: "tools",
  r: "results", c: "compact", C: "/cmd", m: "meta", o: "other",
};
const FILTER_ORDER = ["u", "a", "t", "r", "T", "C", "c", "m", "o"];
const FILTER_DEFAULT = {
  u: true, a: true, T: true, t: true, r: true,
  c: true, C: true, m: false, o: true,
};

const PALETTE_HUES = [
  8, 23, 38, 53, 68, 83, 98, 113, 128, 143, 158, 173,
  188, 203, 218, 233, 248, 263, 278, 293, 308, 323, 338, 353,
];

// Display strings for inner-row classes (everything that's NOT primary).
const CLASS_TAGS = {
  t: { icon: "▶", label: "tool" },
  r: { icon: "←", label: "result" },
  T: { icon: "θ", label: "thinking" },
  C: { icon: "/", label: "cmd" },
  m: { icon: "·", label: "meta" },
  o: { icon: "?", label: "other" },
};

// ===== State =====
const S = {
  config: null,
  projects: [],
  selectedProjectId: null,
  projectDates: null,
  selectedDate: null,
  selectedSession: null,
  pollProjectTimer: null,
  pollSessionTimer: null,

  sessionMeta: null,         // {total, by_date, day_map, filter_classes, ...}
  stubs: new Map(),          // entry_idx -> stub
  fetchedRanges: [],         // covered byte/idx ranges
  inFlightRanges: new Map(), // offset -> { retries: int, timer: id }
  groups: [],                // computed turn groups
  positions: null,           // Float64Array of cumulative top per group
  totalHeight: 0,

  filters: { ...FILTER_DEFAULT },
  expandedSpans: new Set(),  // group indices that are expanded
  // Date-scope filter: when set, the timeline shows ONLY entries whose
  // timestamp falls on this YYYY-MM-DD. Default scope = the date the user
  // picked in the drawer. User clears via the × in the topbar chip.
  dateScopeFilter: null,

  selectedEntryIdx: null,
  fetchedEntries: new Map(),
  detailLoadId: 0,
  entryRetryTimer: null,

  drawerPickerOpen: false,
  helpOpen: false,
};

// ===== DOM =====
const $ = (id) => document.getElementById(id);
const D = {};
function initDom() {
  D.btnSidebar = $("btn-sidebar");
  D.brandName = document.querySelector(".brand-name");
  D.sessionInfo = $("session-info");
  D.filterBar = $("filter-bar");
  D.btnRefresh = $("btn-refresh");
  D.tzLabel = $("tz-label");
  D.btnHelp = $("btn-help");
  D.emptyState = $("empty-state");
  D.btnEmptyPick = $("btn-empty-pick");
  D.paneTimeline = $("pane-timeline");
  D.statusBar = $("timeline-status-bar");
  D.timelineScroll = $("timeline-scroll");
  D.timelineSpacer = $("timeline-spacer");
  D.timelineRows = $("timeline-rows");
  D.paneDetail = $("pane-detail");
  D.detailTitle = $("detail-title");
  D.detailContent = $("detail-content");
  D.btnDetailPrev = $("btn-detail-prev");
  D.btnDetailNext = $("btn-detail-next");
  D.btnDetailClose = $("btn-detail-close");
  D.drawerBackdrop = $("drawer-backdrop");
  D.drawerPicker = $("drawer-picker");
  D.btnDrawerClose = $("btn-drawer-close");
  D.projectList = $("project-list");
  D.dateList = $("date-list");
  D.helpPopover = $("help-popover");
  D.sshCmd = $("ssh-cmd");
  D.localUrl = $("local-url");
  D.btnCopySsh = $("btn-copy-ssh");
  D.toast = $("toast");
}

// ===== Helpers =====
function escHtml(s) {
  if (s == null) return "";
  return String(s).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}
function showToast(msg, ms = 1800) {
  if (!D.toast) return;
  D.toast.textContent = msg;
  D.toast.classList.add("show");
  clearTimeout(D.toast._t);
  D.toast._t = setTimeout(() => D.toast.classList.remove("show"), ms);
}
async function fetchJSON(url) {
  try {
    const r = await fetch(url, { headers: { "Accept": "application/json" } });
    let data = null;
    try { data = await r.json(); } catch (_) {}
    return { status: r.status, data };
  } catch (e) {
    return { status: 0, data: null, error: e };
  }
}

let _tzFmt = null, _dateFmt = null;
function getTz() { return (S.config && S.config.tz) || undefined; }
function tzFmt() {
  if (_tzFmt) return _tzFmt;
  const tz = getTz();
  try {
    _tzFmt = new Intl.DateTimeFormat(undefined, {
      timeZone: tz === "local" ? undefined : tz,
      hour12: false, hour: "2-digit", minute: "2-digit", second: "2-digit",
    });
  } catch (_) {
    _tzFmt = new Intl.DateTimeFormat(undefined, {
      hour12: false, hour: "2-digit", minute: "2-digit", second: "2-digit",
    });
  }
  return _tzFmt;
}
function localClock(ts) {
  if (!ts) return "";
  const d = new Date(ts);
  if (isNaN(d.getTime())) return "";
  return tzFmt().format(d);
}
function localDate(ts) {
  if (!ts) return "";
  const d = new Date(ts);
  if (isNaN(d.getTime())) return "";
  if (!_dateFmt) {
    const tz = getTz();
    try {
      _dateFmt = new Intl.DateTimeFormat("en-CA", {
        timeZone: tz === "local" ? undefined : tz,
        year: "numeric", month: "2-digit", day: "2-digit",
      });
    } catch (_) {
      _dateFmt = new Intl.DateTimeFormat("en-CA", {
        year: "numeric", month: "2-digit", day: "2-digit",
      });
    }
  }
  return _dateFmt.format(d);
}
function hueFor(colorIdx) {
  if (colorIdx == null || colorIdx < 0) return 200;
  return PALETTE_HUES[colorIdx % PALETTE_HUES.length];
}

// ===== Bootstrap =====
async function boot() {
  initDom();
  if (window.marked) window.marked.setOptions({ breaks: false, gfm: true });

  const cfg = await fetchJSON("/api/config");
  if (cfg.status !== 200) {
    document.body.innerHTML = `<div style="padding:40px;color:#e2e6ee;text-align:center">Failed to load config (HTTP ${cfg.status}).</div>`;
    return;
  }
  S.config = cfg.data;
  D.tzLabel.textContent = `tz: ${S.config.tz}`;
  D.sshCmd.textContent = S.config.ssh_hint || "";
  D.localUrl.textContent = `http://127.0.0.1:${S.config.port}/`;

  bindEvents();
  buildFilterBar();
  await loadProjects();

  const st = readUrlState();
  if (st.p) {
    const p = S.projects.find((x) => x.id === st.p);
    if (p) {
      await selectProject(p);
      if (st.s) await selectSessionByPath(st.s, st.d, parseInt(st.e || "", 10));
    } else {
      openDrawer();
    }
  } else {
    openDrawer();
  }
}

// ===== URL state =====
function setUrlState() {
  const parts = [];
  if (S.selectedProjectId) parts.push(`p=${encodeURIComponent(S.selectedProjectId)}`);
  if (S.selectedDate) parts.push(`d=${S.selectedDate}`);
  if (S.selectedSession) parts.push(`s=${encodeURIComponent(S.selectedSession.sessionPath)}`);
  if (S.selectedEntryIdx != null) parts.push(`e=${S.selectedEntryIdx}`);
  history.replaceState(null, "", "#" + parts.join("&"));
}
function readUrlState() {
  const out = {};
  const h = location.hash.replace(/^#/, "");
  for (const part of h.split("&")) {
    const [k, v] = part.split("=");
    if (k) out[k] = decodeURIComponent(v || "");
  }
  return out;
}

// ===== Drawer / detail / popover =====
function openDrawer() {
  S.drawerPickerOpen = true;
  D.drawerPicker.classList.remove("closed");
  D.drawerBackdrop.classList.add("show");
}
function closeDrawer() {
  S.drawerPickerOpen = false;
  D.drawerPicker.classList.add("closed");
  D.drawerBackdrop.classList.remove("show");
}
function openHelp() { S.helpOpen = true; D.helpPopover.classList.remove("closed"); }
function closeHelp() { S.helpOpen = false; D.helpPopover.classList.add("closed"); }
function openDetail() { D.paneDetail.classList.remove("closed"); }
function closeDetail() {
  D.paneDetail.classList.add("closed");
  S.selectedEntryIdx = null;
  if (S.entryRetryTimer) { clearTimeout(S.entryRetryTimer); S.entryRetryTimer = null; }
  scheduleRender();
  setUrlState();
}

function bindEvents() {
  D.btnSidebar.addEventListener("click", () => S.drawerPickerOpen ? closeDrawer() : openDrawer());
  D.btnDrawerClose.addEventListener("click", closeDrawer);
  D.drawerBackdrop.addEventListener("click", closeDrawer);
  D.btnEmptyPick.addEventListener("click", openDrawer);
  D.btnHelp.addEventListener("click", () => S.helpOpen ? closeHelp() : openHelp());
  D.btnRefresh.addEventListener("click", refreshAll);
  D.btnDetailClose.addEventListener("click", closeDetail);
  D.btnDetailPrev.addEventListener("click", () => stepDetail(-1));
  D.btnDetailNext.addEventListener("click", () => stepDetail(+1));
  D.btnCopySsh.addEventListener("click", () => {
    if (!S.config.ssh_hint) return;
    navigator.clipboard.writeText(S.config.ssh_hint).then(
      () => showToast("SSH command copied"),
      () => showToast("Copy failed"),
    );
  });

  document.addEventListener("click", (ev) => {
    if (!S.helpOpen) return;
    if (D.helpPopover.contains(ev.target) || D.btnHelp.contains(ev.target)) return;
    closeHelp();
  });

  document.addEventListener("keydown", (ev) => {
    if (ev.target.tagName === "INPUT" || ev.target.tagName === "TEXTAREA") return;
    if (ev.key === "Escape") {
      if (S.helpOpen) closeHelp();
      else if (S.drawerPickerOpen) closeDrawer();
      else if (S.selectedEntryIdx != null) closeDetail();
      return;
    }
    if (ev.key === "p" && (ev.ctrlKey || ev.metaKey)) {
      ev.preventDefault();
      openDrawer();
      return;
    }
    if (S.sessionMeta) {
      if (ev.key === "j") { stepDetail(+1); ev.preventDefault(); }
      else if (ev.key === "k") { stepDetail(-1); ev.preventDefault(); }
    }
  });

  setupTimelineScroll();
  window.addEventListener("resize", scheduleRender);
}

function refreshAll() {
  D.btnRefresh.classList.add("busy");
  const projId = S.selectedProjectId;
  const sp = S.selectedSession ? S.selectedSession.sessionPath : null;
  const eIdx = S.selectedEntryIdx;
  const sd = S.selectedDate;

  resetSession({ keepSelection: true });
  S.projectDates = null;
  (async () => {
    try {
      await loadProjects();
      if (projId) {
        const p = S.projects.find((x) => x.id === projId);
        if (p) await selectProject(p);
        if (sp) await selectSessionByPath(sp, sd, eIdx);
      }
    } finally {
      setTimeout(() => D.btnRefresh.classList.remove("busy"), 300);
    }
  })();
}

// ===== Projects =====
async function loadProjects() {
  const r = await fetchJSON("/api/projects");
  if (r.status !== 200) {
    D.projectList.innerHTML = `<div class="indexing-banner">Failed to load projects (${r.status})</div>`;
    return;
  }
  S.projects = r.data.projects;
  renderProjects();
}
function renderProjects() {
  const html = S.projects.map((p) => {
    const cls = (p.session_count > 0)
      ? "project-row" + (p.id === S.selectedProjectId ? " active" : "")
      : "project-row disabled";
    return `<div class="${cls}" data-id="${escHtml(p.id)}">
      <div class="project-name">${escHtml(p.display_name)}</div>
      <div class="project-meta">${p.session_count} session${p.session_count === 1 ? "" : "s"}</div>
    </div>`;
  }).join("");
  D.projectList.innerHTML = html;
  D.projectList.querySelectorAll(".project-row").forEach((el) => {
    el.addEventListener("click", () => {
      if (el.classList.contains("disabled")) return;
      const id = el.dataset.id;
      const p = S.projects.find((x) => x.id === id);
      if (p) selectProject(p);
    });
  });
}

async function selectProject(p) {
  if (S.selectedProjectId !== p.id) {
    S.selectedProjectId = p.id;
    S.projectDates = null;
    S.selectedDate = null;
    S.selectedSession = null;
    resetSession();
  }
  if (S.pollProjectTimer) { clearTimeout(S.pollProjectTimer); S.pollProjectTimer = null; }
  renderProjects();
  D.dateList.innerHTML = `<div class="indexing-banner">Indexing…</div>`;
  setUrlState();
  await pollProjectDates();
}

async function pollProjectDates() {
  if (!S.selectedProjectId) return;
  const url = `/api/projects/${encodeURIComponent(S.selectedProjectId)}/dates`;
  const r = await fetchJSON(url);
  if (r.status === 0 || r.data == null) {
    D.dateList.innerHTML = `<div class="indexing-banner">Server unreachable</div>`;
    return;
  }
  S.projectDates = r.data;
  renderDateList();
  if (r.data.in_progress > 0) {
    S.pollProjectTimer = setTimeout(pollProjectDates, PROJECT_POLL_MS);
  }
}

function renderDateList() {
  if (!S.projectDates) return;
  const dates = S.projectDates.dates;
  if (!dates || dates.length === 0) {
    if (S.projectDates.in_progress > 0) {
      D.dateList.innerHTML = `<div class="indexing-banner">Indexing ${S.projectDates.in_progress} session(s)…</div>`;
    } else {
      D.dateList.innerHTML = `<div class="empty-hint">No conversations.</div>`;
    }
    return;
  }
  const parts = [];
  if (S.projectDates.in_progress > 0) {
    parts.push(`<div class="indexing-banner"><span class="indexing-dot"></span>Indexing ${S.projectDates.in_progress} more session(s)…</div>`);
  }
  for (const dEntry of dates) {
    const d = dEntry.date;
    const expanded = (S.selectedDate === d) || (dates.length <= 4);
    parts.push(`<div class="date-row" data-date="${d}">`);
    parts.push(`<div class="date-header">
      <span>${d}</span>
      <span class="date-count">${dEntry.session_count} · ${dEntry.total_entries.toLocaleString()} msg</span>
    </div>`);
    if (expanded) {
      for (const s of dEntry.sessions) {
        const hue = hueFor(s.color_idx);
        const active = S.selectedSession && S.selectedSession.sessionPath === s.session_path && S.selectedDate === d ? " active" : "";
        parts.push(`<div class="session-row${active}" data-date="${d}" data-session="${escHtml(s.session_path)}" data-firstidx="${s.first_idx_on_date}">
          <span class="session-dot" style="background: hsl(${hue}, 70%, 60%)"></span>
          <span class="session-label" title="${escHtml(s.label)}">${escHtml(s.label || s.session_path.slice(0, 8))}</span>
          <span class="day-chip">D${s.day_n}/${s.day_total}</span>
        </div>`);
      }
    }
    parts.push(`</div>`);
  }
  D.dateList.innerHTML = parts.join("");
  D.dateList.querySelectorAll(".date-header").forEach((el) => {
    el.addEventListener("click", () => {
      const d = el.parentElement.dataset.date;
      S.selectedDate = (S.selectedDate === d) ? null : d;
      renderDateList();
    });
  });
  D.dateList.querySelectorAll(".session-row").forEach((el) => {
    el.addEventListener("click", () => {
      onSelectSession(el.dataset.date, el.dataset.session, parseInt(el.dataset.firstidx, 10));
    });
  });
}

// ===== Session =====
function resetSession(opts = {}) {
  if (S.pollSessionTimer) { clearTimeout(S.pollSessionTimer); S.pollSessionTimer = null; }
  if (S.entryRetryTimer) { clearTimeout(S.entryRetryTimer); S.entryRetryTimer = null; }
  S.sessionMeta = null;
  S.stubs.clear();
  S.fetchedRanges = [];
  for (const [, info] of S.inFlightRanges) {
    if (info.timer) clearTimeout(info.timer);
  }
  S.inFlightRanges.clear();
  S.groups = [];
  S.positions = null;
  S.totalHeight = 0;
  S.expandedSpans.clear();
  S.dateScopeFilter = null;
  if (!opts.keepSelection) S.selectedEntryIdx = null;
  S.fetchedEntries.clear();
  D.timelineSpacer.style.height = "0px";
  D.timelineRows.innerHTML = "";
  D.statusBar.innerHTML = "";
  D.detailContent.innerHTML = `<div class="detail-empty">Pick a message in the timeline.</div>`;
  D.detailTitle.textContent = "Detail";
  D.paneDetail.classList.add("closed");
  updateSessionInfo();
  showEmptyState();
}

function showEmptyState() { D.emptyState.style.display = ""; D.paneTimeline.hidden = true; }
function hideEmptyState() { D.emptyState.style.display = "none"; D.paneTimeline.hidden = false; }

function updateSessionInfo() {
  if (!S.selectedSession) { D.sessionInfo.innerHTML = ""; return; }
  const ses = S.selectedSession;
  const hue = hueFor(ses.color_idx);
  const dayChip = ses.day_n != null ? `<span class="sess-meta">Day ${ses.day_n}/${ses.day_total}</span>` : "";
  // Date-scope chip: clickable × clears the scope so user can fall back to
  // a full-session view. Default scope = the date the user picked.
  let scopeChip = "";
  if (S.dateScopeFilter) {
    let scopedCount = "";
    if (S.sessionMeta && S.sessionMeta.by_date) {
      const arr = S.sessionMeta.by_date[S.dateScopeFilter];
      if (Array.isArray(arr)) scopedCount = ` · ${arr.length.toLocaleString()}`;
    }
    scopeChip = `<span class="date-scope-chip" title="Showing only ${escHtml(S.dateScopeFilter)} — click × to show all dates">📅 ${escHtml(S.dateScopeFilter)}${scopedCount}<button class="date-scope-clear" aria-label="Show all dates">×</button></span>`;
  }
  const totalText = S.sessionMeta && S.sessionMeta.total
    ? (S.dateScopeFilter
       ? `<span class="sess-meta">${S.sessionMeta.total.toLocaleString()} total</span>`
       : `<span class="sess-meta">${S.sessionMeta.total.toLocaleString()} entries</span>`)
    : "";
  D.sessionInfo.innerHTML = `
    <span class="sess-dot" style="background: hsl(${hue}, 70%, 60%)"></span>
    <span class="sess-label" title="${escHtml(ses.label || ses.sessionPath)}">${escHtml(ses.label || ses.sessionPath.slice(0, 8))}</span>
    ${dayChip}${totalText}${scopeChip}
  `;
  const clearBtn = D.sessionInfo.querySelector(".date-scope-clear");
  if (clearBtn) {
    clearBtn.addEventListener("click", (ev) => {
      ev.stopPropagation();
      clearDateScope();
    });
  }
}

function clearDateScope() {
  if (!S.dateScopeFilter) return;
  S.dateScopeFilter = null;
  computeGroupsAndLayout();
  renderStatusBar();
  updateSessionInfo();
  scheduleRender();
}

function setDateScope(date) {
  S.dateScopeFilter = date || null;
  // Layout recompute happens via the caller (loadSession or filter toggle).
}

async function onSelectSession(date, sessionPath, firstIdxOnDate) {
  let dayN = null, dayTotal = null, colorIdx = 0, lastIdxOnDate = null;
  if (S.projectDates) {
    for (const de of S.projectDates.dates) {
      for (const s of de.sessions) {
        if (s.session_path === sessionPath && de.date === date) {
          dayN = s.day_n; dayTotal = s.day_total; colorIdx = s.color_idx;
          lastIdxOnDate = s.last_idx_on_date;
          break;
        }
      }
    }
  }
  const sm = S.projectDates && S.projectDates.session_meta && S.projectDates.session_meta[sessionPath];
  S.selectedDate = date;
  S.selectedSession = {
    projectId: S.selectedProjectId, sessionPath,
    label: (sm && sm.label) || sessionPath,
    color_idx: colorIdx, day_n: dayN, day_total: dayTotal,
  };
  resetSession({ keepSelection: false });
  S.selectedSession.color_idx = colorIdx;
  S.selectedSession.day_n = dayN;
  S.selectedSession.day_total = dayTotal;
  // Default scope: the picked date. User can clear via × in the topbar.
  // 90% of the time a multi-day session has tens-of-thousands of entries
  // and the user only cares about today's activity.
  S.dateScopeFilter = date || null;
  hideEmptyState();
  updateSessionInfo();
  closeDrawer();
  renderDateList();
  // Default scroll target: the latest entry on the chosen date (so opening
  // a session lands you at the most recent activity, not the first message
  // of the day). Falls back to firstIdxOnDate, then 0.
  const scrollTarget = (lastIdxOnDate != null) ? lastIdxOnDate : (firstIdxOnDate || 0);
  await loadSession(scrollTarget);
  setUrlState();
}

async function selectSessionByPath(sp, date, entryIdx) {
  if (!S.projectDates) await pollProjectDates();
  let chosenDate = date, firstIdx = 0;
  if (S.projectDates) {
    for (const dEntry of S.projectDates.dates) {
      if (date && dEntry.date !== date) continue;
      for (const s of dEntry.sessions) {
        if (s.session_path === sp) {
          chosenDate = chosenDate || dEntry.date;
          firstIdx = s.first_idx_on_date;
          break;
        }
      }
      if (chosenDate) break;
    }
    if (!chosenDate) {
      for (const dEntry of S.projectDates.dates) {
        for (const s of dEntry.sessions) {
          if (s.session_path === sp) { chosenDate = dEntry.date; firstIdx = s.first_idx_on_date; break; }
        }
        if (chosenDate) break;
      }
    }
  }
  if (chosenDate) {
    await onSelectSession(chosenDate, sp, firstIdx);
    if (entryIdx != null && !isNaN(entryIdx) && entryIdx >= 0) {
      await openEntry(entryIdx);
    }
  }
}

async function loadSession(scrollToIdx) {
  const proj = S.selectedProjectId;
  const sp = S.selectedSession.sessionPath;
  D.timelineRows.innerHTML = `<div class="indexing-banner" style="margin:14px">Loading session…</div>`;
  D.statusBar.innerHTML = "";
  const url = `/api/sessions/${encodeURIComponent(proj)}/${encodeURIComponent(sp)}?offset=0&limit=${STUB_PAGE}`;
  const r = await fetchJSON(url);
  if (r.status === 202 && r.data && r.data.indexing) {
    D.timelineRows.innerHTML = `<div class="indexing-banner" style="margin:14px">Indexing… ${formatProgress(r.data.progress)}</div>`;
    S.pollSessionTimer = setTimeout(() => loadSession(scrollToIdx), SESSION_POLL_MS);
    return;
  }
  if (r.status !== 200 || !r.data) {
    D.timelineRows.innerHTML = `<div class="indexing-banner" style="margin:14px">Failed to load session.</div>`;
    return;
  }
  S.sessionMeta = r.data;
  for (const stub of r.data.stubs || []) S.stubs.set(stub.idx, stub);
  insertRange(0, r.data.end);

  computeGroupsAndLayout();
  renderStatusBar();
  updateSessionInfo();
  scrollToEntryIdx(scrollToIdx, false);
  scheduleRender();
}

function formatProgress(p) {
  if (!p) return "…";
  return `${Math.round((p.fraction || 0) * 100)}% (${(p.lines_done || 0).toLocaleString()} lines)`;
}

function renderStatusBar() {
  if (!S.sessionMeta) return;
  const total = S.sessionMeta.total || 0;
  const compact = (S.sessionMeta.compact_indices || []).length;
  const subagents = (S.sessionMeta.subagents || []).length;
  const blobs = (S.sessionMeta.tool_result_blobs || []).length;
  const fork = S.sessionMeta.fork;
  const groups = S.groups.length;

  const items = [];
  items.push(`<span class="stat-item"><span class="stat-num">${total.toLocaleString()}</span> entries</span>`);
  items.push(`<span class="stat-item"><span class="stat-num">${groups.toLocaleString()}</span> turns</span>`);
  if (compact) items.push(`<span class="stat-item"><span class="stat-num">${compact}</span> compactions</span>`);
  if (subagents) items.push(`<span class="stat-item"><span class="stat-num">${subagents}</span> subagents</span>`);
  if (blobs) items.push(`<span class="stat-item"><span class="stat-num">${blobs}</span> blobs</span>`);
  if (fork) items.push(`<span class="stat-item">↳ forked from ${escHtml(String(fork).slice(0, 8))}…</span>`);
  D.statusBar.innerHTML = items.join("");
}

// ===== Filter bar =====
function buildFilterBar() {
  const parts = [];
  for (const code of FILTER_ORDER) {
    const lbl = FILTER_LABELS[code] || code;
    const on = S.filters[code];
    parts.push(`<span class="filter-chip${on ? " active" : ""}" data-code="${code}" title="Toggle ${lbl}">${lbl}</span>`);
  }
  D.filterBar.innerHTML = parts.join("");
  D.filterBar.querySelectorAll(".filter-chip").forEach((el) => {
    el.addEventListener("click", () => {
      const code = el.dataset.code;
      S.filters[code] = !S.filters[code];
      el.classList.toggle("active", S.filters[code]);
      if (S.sessionMeta) {
        const prevSelected = S.selectedEntryIdx;
        computeGroupsAndLayout();
        renderStatusBar();
        if (prevSelected != null) scrollToEntryIdx(prevSelected, false);
        scheduleRender();
      }
    });
  });
}

// ===== Group computation =====
function computeGroupsAndLayout() {
  if (!S.sessionMeta) {
    S.groups = []; S.positions = null; S.totalHeight = 0;
    D.timelineSpacer.style.height = "0px";
    return;
  }
  const fc = S.sessionMeta.filter_classes || "";
  const compactSet = new Set(S.sessionMeta.compact_indices || []);
  const filters = S.filters;
  const groups = [];
  let span = null;

  // Date-scope: when set, only consider entries whose date matches.
  // by_date[date] is an array of entry indices for that date.
  let scopeIndices = null;
  if (S.dateScopeFilter && S.sessionMeta.by_date) {
    const arr = S.sessionMeta.by_date[S.dateScopeFilter];
    if (Array.isArray(arr)) scopeIndices = new Set(arr);
  }
  // If scope is set, iterate only the in-scope contiguous range. Spans
  // never bridge across-date entries because we hard-skip out-of-scope
  // indices, which naturally flushes any pending span at the boundary.

  function flushSpan() {
    if (!span) return;
    // Only emit span if it has at least one entry whose class is enabled.
    let hasVisible = false;
    for (const code in span.counts) {
      if (filters[code]) { hasVisible = true; break; }
    }
    if (hasVisible) groups.push(span);
    span = null;
  }

  for (let i = 0, n = fc.length; i < n; i++) {
    if (scopeIndices && !scopeIndices.has(i)) {
      // Crossing a date boundary — break any in-progress span so we don't
      // create a span that spans across hidden out-of-scope entries.
      flushSpan();
      continue;
    }
    const cls = fc[i];
    if (compactSet.has(i)) {
      flushSpan();
      if (filters.c) groups.push({ kind: "compaction", entryIdx: i });
      continue;
    }
    if (PRIMARY_CLASSES.has(cls)) {
      flushSpan();
      if (filters[cls]) groups.push({ kind: "primary", entryIdx: i, cls });
    } else {
      if (!span) span = { kind: "span", start: i, end: i + 1, counts: {} };
      else span.end = i + 1;
      span.counts[cls] = (span.counts[cls] || 0) + 1;
    }
  }
  flushSpan();

  // Re-resolve expanded spans by their (start, end) since group indices can shift.
  // We key expanded state by `${start}:${end}` instead of group index.
  const newExpandedSet = new Set();
  for (let g = 0; g < groups.length; g++) {
    if (groups[g].kind === "span") {
      const key = `${groups[g].start}:${groups[g].end}`;
      if (S.expandedSpans.has(key)) {
        groups[g].expanded = true;
        newExpandedSet.add(key);
      }
    }
  }
  S.expandedSpans = newExpandedSet;
  S.groups = groups;

  // Layout positions
  const positions = new Float64Array(groups.length);
  let pos = 0;
  for (let g = 0; g < groups.length; g++) {
    positions[g] = pos;
    pos += groupHeight(groups[g]);
  }
  S.positions = positions;
  S.totalHeight = pos;
  D.timelineSpacer.style.height = pos + "px";
}

function groupHeight(g) {
  if (g.kind === "primary") return ROW_H_PRIMARY;
  if (g.kind === "compaction") return ROW_H_COMPACT;
  if (g.kind === "span") {
    if (g.expanded) {
      // header + visible inner rows
      const fc = S.sessionMeta.filter_classes || "";
      const filters = S.filters;
      let count = 0;
      for (let i = g.start; i < g.end; i++) {
        if (filters[fc[i]]) count++;
      }
      return ROW_H_SPAN + count * ROW_H_INNER + 6;
    }
    return ROW_H_SPAN;
  }
  return 0;
}

function findGroupByOffset(scrollTop) {
  const positions = S.positions;
  if (!positions || positions.length === 0) return 0;
  let lo = 0, hi = positions.length;
  while (lo < hi) {
    const mid = (lo + hi) >>> 1;
    if (positions[mid] <= scrollTop) lo = mid + 1;
    else hi = mid;
  }
  return Math.max(0, lo - 1);
}

function entryIdxToGroup(entryIdx) {
  // Find the group containing this entry idx.
  const groups = S.groups;
  if (!groups || groups.length === 0) return -1;
  // Binary search: groups are sorted by entry index
  let lo = 0, hi = groups.length;
  while (lo < hi) {
    const mid = (lo + hi) >>> 1;
    const g = groups[mid];
    const gStart = g.kind === "span" ? g.start : g.entryIdx;
    if (gStart < entryIdx) lo = mid + 1;
    else hi = mid;
  }
  // lo is the first group with start >= entryIdx; check the previous one for span containment
  if (lo > 0) {
    const prev = groups[lo - 1];
    if (prev.kind === "span" && entryIdx >= prev.start && entryIdx < prev.end) return lo - 1;
  }
  if (lo < groups.length) {
    const g = groups[lo];
    if (g.kind === "span" && entryIdx >= g.start && entryIdx < g.end) return lo;
    if ((g.kind === "primary" || g.kind === "compaction") && g.entryIdx === entryIdx) return lo;
  }
  return Math.max(0, lo - 1);
}

function scrollToEntryIdx(entryIdx, animate) {
  const g = entryIdxToGroup(entryIdx);
  if (g < 0 || !S.positions) return;
  const top = S.positions[g] - 100;
  if (animate) D.timelineScroll.scrollTo({ top: Math.max(0, top), behavior: "smooth" });
  else D.timelineScroll.scrollTop = Math.max(0, top);
}

// ===== Virtual scroll render =====
function setupTimelineScroll() {
  let raf = null;
  D.timelineScroll.addEventListener("scroll", () => {
    if (raf) return;
    raf = requestAnimationFrame(() => { raf = null; renderTimeline(); });
  });
}
function scheduleRender() { requestAnimationFrame(renderTimeline); }

function renderTimeline() {
  if (!S.sessionMeta || !S.positions) {
    D.timelineRows.innerHTML = "";
    return;
  }
  if (S.groups.length === 0) {
    D.timelineRows.innerHTML = `<div class="indexing-banner" style="margin:14px">No entries match the current filters. Toggle chips above to show more.</div>`;
    D.timelineSpacer.style.height = "0px";
    return;
  }

  const positions = S.positions;
  const groups = S.groups;
  const scrollTop = D.timelineScroll.scrollTop;
  const viewport = D.timelineScroll.clientHeight;
  const firstGroup = Math.max(0, findGroupByOffset(scrollTop - ROW_BUFFER_PX));
  const targetBottom = scrollTop + viewport + ROW_BUFFER_PX;
  let lastGroup = firstGroup;
  while (lastGroup < groups.length && positions[lastGroup] < targetBottom) lastGroup++;

  // Collect missing entry indices from visible groups
  const needed = [];
  for (let r = firstGroup; r < lastGroup; r++) {
    const g = groups[r];
    if (g.kind === "primary") {
      if (!S.stubs.has(g.entryIdx)) needed.push(g.entryIdx);
    } else if (g.kind === "span" && g.expanded) {
      for (let i = g.start; i < g.end; i++) {
        if (S.filters[S.sessionMeta.filter_classes[i]] && !S.stubs.has(i)) needed.push(i);
      }
    }
  }
  if (needed.length > 0) ensureStubsAround(needed);

  const out = [];
  for (let r = firstGroup; r < lastGroup; r++) {
    out.push(renderGroup(groups[r], positions[r], r));
  }
  D.timelineRows.innerHTML = out.join("");
  attachRowHandlers();
}

function attachRowHandlers() {
  D.timelineRows.querySelectorAll("[data-entry-idx]").forEach((el) => {
    el.addEventListener("click", (ev) => {
      ev.stopPropagation();
      const idx = parseInt(el.dataset.entryIdx, 10);
      if (!isNaN(idx)) openEntry(idx);
    });
  });
  D.timelineRows.querySelectorAll(".span-row").forEach((el) => {
    el.addEventListener("click", () => {
      const start = parseInt(el.dataset.spanStart, 10);
      const end = parseInt(el.dataset.spanEnd, 10);
      toggleSpan(start, end);
    });
  });
}

function toggleSpan(start, end) {
  const key = `${start}:${end}`;
  // Anchor: preserve the span's screen-relative position across the layout
  // recompute. Without this, expanding a tall span would push everything below
  // it down, but the user's scrollTop stays the same, so the scrollbar thumb
  // (totalHeight grew) appears to "jump" upward to a wrong position.
  let anchor = null;
  const oldIdx = S.groups.findIndex((g) => g.kind === "span" && g.start === start && g.end === end);
  if (oldIdx >= 0 && S.positions) {
    anchor = {
      offsetFromScrollTop: S.positions[oldIdx] - D.timelineScroll.scrollTop,
    };
  }
  if (S.expandedSpans.has(key)) S.expandedSpans.delete(key);
  else S.expandedSpans.add(key);
  computeGroupsAndLayout();
  if (anchor != null) {
    const newIdx = S.groups.findIndex((g) => g.kind === "span" && g.start === start && g.end === end);
    if (newIdx >= 0) {
      const newTop = S.positions[newIdx];
      D.timelineScroll.scrollTop = Math.max(0, newTop - anchor.offsetFromScrollTop);
    }
  }
  scheduleRender();
}

// ===== Render functions =====
function renderGroup(g, top, gIdx) {
  if (g.kind === "compaction") return renderCompaction(g, top);
  if (g.kind === "primary") return renderPrimary(g, top);
  return renderSpan(g, top);
}

function renderCompaction(g, top) {
  return `<div class="row compact-row" style="top:${top}px; height:${ROW_H_COMPACT}px;">
    <span class="compact-line"></span>
    <span class="compact-text">compaction · #${g.entryIdx}</span>
    <span class="compact-line"></span>
  </div>`;
}

function renderPrimary(g, top) {
  const stub = S.stubs.get(g.entryIdx);
  const selected = (S.selectedEntryIdx === g.entryIdx) ? " selected" : "";
  const cls = g.cls;
  const role = (cls === "u") ? "USER" : "ASSISTANT";
  const roleIcon = (cls === "u") ? "U" : "A";
  const time = stub ? localClock(stub.ts) : "";
  const preview = stub ? (stub.preview || "") : "loading…";
  return `<div class="row tall cls-${cls}${selected}" data-entry-idx="${g.entryIdx}" style="top:${top}px; height:${ROW_H_PRIMARY - 4}px;">
    <span class="row-icon">${roleIcon}</span>
    <div class="row-body">
      <div class="row-head">
        <span class="row-role">${role}</span>
        <span class="row-time">${escHtml(time)}</span>
      </div>
      <div class="row-preview">${escHtml(preview)}</div>
    </div>
  </div>`;
}

function renderSpan(g, top) {
  const counts = g.counts;
  const filters = S.filters;
  const segs = [];
  for (const code of ["t", "r", "T", "C", "m", "o"]) {
    if (counts[code] && filters[code]) {
      const tag = CLASS_TAGS[code] || { icon: "·", label: code };
      const plural = counts[code] === 1 ? "" : "s";
      segs.push(`<span class="span-seg cls-${code}">${tag.icon} ${counts[code]} ${tag.label}${plural}</span>`);
    }
  }
  const summary = segs.join(" · ") || `${g.end - g.start} steps`;
  const fc = S.sessionMeta.filter_classes;
  const startStub = S.stubs.get(g.start);
  const endStub = S.stubs.get(g.end - 1);
  const tStart = startStub ? localClock(startStub.ts) : "";
  const tEnd = endStub ? localClock(endStub.ts) : "";
  const tRange = (tStart && tEnd && tStart !== tEnd) ? `${tStart}–${tEnd}` : tStart;
  const expanded = !!g.expanded;
  const arrow = expanded ? "▼" : "▶";
  const h = groupHeight(g);

  let html = `<div class="span-row${expanded ? " expanded" : ""}" data-span-start="${g.start}" data-span-end="${g.end}" style="top:${top}px; height:${ROW_H_SPAN}px;">
    <span class="span-arrow">${arrow}</span>
    <span class="span-summary">${summary}</span>
    <span class="span-time">${escHtml(tRange)}</span>
  </div>`;

  if (expanded) {
    let innerTop = top + ROW_H_SPAN;
    for (let i = g.start; i < g.end; i++) {
      if (!filters[fc[i]]) continue;
      html += renderInnerRow(i, fc[i], innerTop);
      innerTop += ROW_H_INNER;
    }
  }
  return html;
}

function renderInnerRow(eIdx, cls, top) {
  const stub = S.stubs.get(eIdx);
  const selected = (S.selectedEntryIdx === eIdx) ? " selected" : "";
  const tag = CLASS_TAGS[cls] || { icon: "·", label: cls };
  const time = stub ? localClock(stub.ts) : "";
  const tagText = stub ? (stub.tool_name || stub.role || stub.type || tag.label) : "…";
  const preview = stub ? (stub.preview || "") : "loading…";
  return `<div class="row inner cls-${cls}${selected}" data-entry-idx="${eIdx}" style="top:${top}px; height:${ROW_H_INNER - 2}px;">
    <span class="row-icon">${tag.icon}</span>
    <span class="row-time">${escHtml(time)}</span>
    <span class="row-tag">${escHtml(tagText.slice(0, 9))}</span>
    <span class="row-preview">${escHtml(preview)}</span>
  </div>`;
}

// ===== Stub fetching with 202 backoff =====
function isRangeCovered(start, end) {
  for (const r of S.fetchedRanges) {
    if (r.start <= start && end <= r.end) return true;
  }
  return false;
}
function insertRange(start, end) {
  const ranges = [...S.fetchedRanges, { start, end }].sort((a, b) => a.start - b.start);
  const merged = [];
  for (const r of ranges) {
    if (merged.length && merged[merged.length - 1].end >= r.start) {
      merged[merged.length - 1].end = Math.max(merged[merged.length - 1].end, r.end);
    } else merged.push({ ...r });
  }
  S.fetchedRanges = merged;
}

function ensureStubsAround(neededIndices) {
  if (!S.sessionMeta) return;
  const total = S.sessionMeta.total;
  let minN = total, maxN = 0;
  for (const i of neededIndices) {
    if (i < minN) minN = i;
    if (i > maxN) maxN = i;
  }
  minN = Math.max(0, minN - 50);
  maxN = Math.min(total, maxN + 50);
  const chunkSize = STUB_PAGE;
  let off = Math.floor(minN / chunkSize) * chunkSize;
  while (off < maxN) {
    const end = Math.min(off + chunkSize, total);
    if (!isRangeCovered(off, end) && !S.inFlightRanges.has(off)) {
      fetchStubPage(off, chunkSize);
    }
    off += chunkSize;
  }
}

async function fetchStubPage(offset, limit) {
  if (!S.sessionMeta || !S.selectedSession) return;
  const existing = S.inFlightRanges.get(offset);
  if (existing && existing.fetching) return;
  const info = existing || { retries: 0, fetching: false, timer: null };
  info.fetching = true;
  S.inFlightRanges.set(offset, info);

  const proj = S.selectedProjectId;
  const sp = S.selectedSession.sessionPath;
  const url = `/api/sessions/${encodeURIComponent(proj)}/${encodeURIComponent(sp)}?offset=${offset}&limit=${limit}`;
  try {
    const r = await fetchJSON(url);
    info.fetching = false;
    if (r.status === 200 && r.data && r.data.stubs) {
      for (const stub of r.data.stubs) S.stubs.set(stub.idx, stub);
      insertRange(r.data.offset || offset, r.data.end || (offset + limit));
      S.inFlightRanges.delete(offset);
      scheduleRender();
      return;
    }
    if (r.status === 202) {
      // Indexing in progress; back off and retry.
      info.retries = (info.retries || 0) + 1;
      if (info.retries > MAX_STUB_RETRIES) {
        S.inFlightRanges.delete(offset);
        return;
      }
      const wait = Math.min(STUB_RETRY_MS * Math.min(info.retries, 4), 4000);
      info.timer = setTimeout(() => {
        info.timer = null;
        fetchStubPage(offset, limit);
      }, wait);
      S.inFlightRanges.set(offset, info);
      return;
    }
    // Other failure
    S.inFlightRanges.delete(offset);
  } catch (e) {
    info.fetching = false;
    S.inFlightRanges.delete(offset);
  }
}

// ===== Detail =====
async function openEntry(eIdx) {
  S.selectedEntryIdx = eIdx;
  setUrlState();
  openDetail();
  scheduleRender();
  D.detailContent.innerHTML = `<div class="detail-empty">Loading entry ${eIdx}…</div>`;
  D.detailTitle.textContent = `Entry #${eIdx}`;
  S.detailLoadId += 1;
  if (S.entryRetryTimer) { clearTimeout(S.entryRetryTimer); S.entryRetryTimer = null; }
  await tryLoadEntry(eIdx, S.detailLoadId, 0);
}

async function tryLoadEntry(eIdx, loadId, retries) {
  if (loadId !== S.detailLoadId) return;
  let entry = S.fetchedEntries.get(eIdx);
  if (!entry) {
    const proj = S.selectedProjectId;
    const sp = S.selectedSession.sessionPath;
    const neighbors = [];
    for (let off = -ENTRY_BATCH_NEIGHBORS; off <= ENTRY_BATCH_NEIGHBORS; off++) {
      const cand = eIdx + off;
      if (cand >= 0 && (S.sessionMeta == null || cand < S.sessionMeta.total) && !S.fetchedEntries.has(cand)) {
        neighbors.push(cand);
      }
    }
    const url = `/api/sessions/${encodeURIComponent(proj)}/${encodeURIComponent(sp)}/entries?indices=${neighbors.join(",")}`;
    const r = await fetchJSON(url);
    if (loadId !== S.detailLoadId) return;
    if (r.status === 202) {
      // indexing in progress, retry
      if (retries < MAX_STUB_RETRIES) {
        const wait = Math.min(STUB_RETRY_MS * Math.min(retries + 1, 4), 4000);
        D.detailContent.innerHTML = `<div class="detail-empty">Indexing… retrying in ${Math.round(wait/1000)}s</div>`;
        S.entryRetryTimer = setTimeout(() => tryLoadEntry(eIdx, loadId, retries + 1), wait);
        return;
      }
      D.detailContent.innerHTML = `<div class="detail-empty">Failed: indexing did not finish.</div>`;
      return;
    }
    if (r.status !== 200 || !r.data || !r.data.entries) {
      D.detailContent.innerHTML = `<div class="detail-empty">Failed to load entry (HTTP ${r.status}).</div>`;
      return;
    }
    for (const e of r.data.entries) S.fetchedEntries.set(e.idx, e.entry);
    while (S.fetchedEntries.size > MAX_FETCHED_ENTRIES) {
      const k = S.fetchedEntries.keys().next().value;
      S.fetchedEntries.delete(k);
    }
    entry = S.fetchedEntries.get(eIdx);
  }
  if (loadId !== S.detailLoadId) return;
  if (!entry) {
    D.detailContent.innerHTML = `<div class="detail-empty">Failed to load entry.</div>`;
    return;
  }
  renderDetail(eIdx, entry);
}

function stepDetail(dir) {
  if (S.selectedEntryIdx == null) return;
  // Move within the visible flat sequence: primaries + (visible inner rows of expanded spans)
  const fc = S.sessionMeta.filter_classes || "";
  const compactSet = new Set(S.sessionMeta.compact_indices || []);
  const filters = S.filters;
  const seq = [];
  for (let i = 0, n = fc.length; i < n; i++) {
    const cls = fc[i];
    if (compactSet.has(i)) continue;  // skip compactions for j/k stepping
    if (PRIMARY_CLASSES.has(cls) && filters[cls]) {
      seq.push(i);
    } else if (!PRIMARY_CLASSES.has(cls) && filters[cls]) {
      // include only if the parent span is expanded
      // For simplicity, j/k only steps through primaries.
    }
  }
  const cur = seq.indexOf(S.selectedEntryIdx);
  if (cur < 0) {
    // selected is inner row of an expanded span; step within the span first, then onward to primaries
    // For simplicity, jump to the next primary
    let next = seq.find((i) => i > S.selectedEntryIdx);
    if (dir < 0) next = seq.slice().reverse().find((i) => i < S.selectedEntryIdx);
    if (next != null) { scrollToEntryIdx(next, true); openEntry(next); }
    return;
  }
  const ni = cur + dir;
  if (ni < 0 || ni >= seq.length) return;
  const newIdx = seq[ni];
  scrollToEntryIdx(newIdx, true);
  openEntry(newIdx);
}

function renderDetail(eIdx, entry) {
  const stub = S.stubs.get(eIdx) || {};
  const meta = entry || {};
  const msg = meta.message || {};
  const role = msg.role || meta.type || "?";
  const ts = meta.timestamp || stub.ts || "";
  const tsLabel = ts ? `${localDate(ts)} ${localClock(ts)} (${S.config.tz})` : "";

  const parts = [];
  parts.push(`<div class="detail-meta">`);
  parts.push(pair("idx", String(eIdx)));
  parts.push(pair("type", meta.type || ""));
  if (role) parts.push(pair("role", role));
  if (tsLabel) parts.push(pair("ts", tsLabel));
  if (meta.uuid) parts.push(pair("uuid", String(meta.uuid).slice(0, 8) + "…"));
  if (meta.parentUuid) parts.push(pair("parent", String(meta.parentUuid).slice(0, 8) + "…"));
  if (meta.gitBranch) parts.push(pair("git", meta.gitBranch));
  if (meta.cwd) parts.push(pair("cwd", meta.cwd));
  if (meta.version) parts.push(pair("v", meta.version));
  if (meta.isCompactSummary) parts.push(pair("flag", "compaction"));
  if (meta.isSidechain) parts.push(pair("flag", "subagent"));
  if (meta.forkedFrom) parts.push(pair("forked", String(meta.forkedFrom).slice(0, 8) + "…"));
  parts.push(`</div>`);

  if (meta.forkedFrom) {
    parts.push(`<div class="fork-banner">↳ continued from ${escHtml(String(meta.forkedFrom).slice(0, 8))}…</div>`);
  }
  if (meta.isCompactSummary) {
    parts.push(`<div class="compact-banner">⚠ Auto-compaction summary; everything before this point was compressed.</div>`);
  }

  const content = msg.content;
  if (typeof content === "string") {
    parts.push(renderTextBlock("text", content));
  } else if (Array.isArray(content)) {
    for (const blk of content) {
      if (!blk || typeof blk !== "object") continue;
      switch (blk.type) {
        case "text": parts.push(renderTextBlock("text", blk.text || "")); break;
        case "thinking": parts.push(renderThinkingBlock(blk.thinking || "")); break;
        case "tool_use": parts.push(renderToolUseBlock(blk)); break;
        case "tool_result": parts.push(renderToolResultBlock(blk)); break;
        default: parts.push(renderUnknownBlock(blk));
      }
    }
  } else if (content == null && meta.snapshot) {
    parts.push(renderUnknownBlock({ type: meta.type, snapshot: meta.snapshot }));
  } else {
    parts.push(renderUnknownBlock(meta));
  }

  if (S.sessionMeta && S.sessionMeta.subagents && S.sessionMeta.subagents.length && eIdx === 0) {
    parts.push(`<div class="block"><div class="block-header"><span>Subagents</span><span class="block-flag">${S.sessionMeta.subagents.length}</span></div><div class="block-content">`);
    for (const sa of S.sessionMeta.subagents) {
      parts.push(`<span class="subagent-link" data-subagent-path="${escHtml(sa.path)}">${escHtml(sa.name)} (${formatBytes(sa.size)})</span>`);
    }
    parts.push(`</div></div>`);
  }

  D.detailContent.innerHTML = parts.join("");
  D.detailContent.querySelectorAll(".block-header").forEach((el) => {
    el.addEventListener("click", () => el.parentElement.classList.toggle("collapsed"));
  });
  D.detailContent.querySelectorAll(".subagent-link").forEach((el) => {
    el.addEventListener("click", () => {
      const sp = el.dataset.subagentPath;
      onSelectSession(S.selectedDate, sp, 0);
    });
  });
  if (window.hljs) {
    requestIdleCallback(() => {
      D.detailContent.querySelectorAll("pre code").forEach((el) => {
        // Only highlight blocks that marked tagged with an explicit
        // language. Untagged code blocks are usually prose / log output
        // (markdown indents 4-space content into <pre>), and hljs's
        // auto-detection turns them into unreadable color sludge.
        if (!/\blanguage-\w+/.test(el.className || "")) return;
        try { window.hljs.highlightElement(el); } catch (_) {}
      });
    }, { timeout: 800 });
  }
}

function pair(k, v) {
  return `<div class="pair"><span class="k">${escHtml(k)}:</span><span class="v">${escHtml(v)}</span></div>`;
}
function formatBytes(n) {
  if (n < 1024) return n + " B";
  if (n < 1024 * 1024) return (n / 1024).toFixed(1) + " KB";
  return (n / 1024 / 1024).toFixed(1) + " MB";
}

const LONG_TEXT_CHARS = 5000;
const TEXT_PREVIEW_CHARS = 2000;
function maybeTruncate(text) {
  if (!text || text.length <= LONG_TEXT_CHARS) return [text, null];
  return [text.slice(0, TEXT_PREVIEW_CHARS), text];
}

function renderTextBlock(kind, text) {
  if (!text) return "";
  const [show, full] = maybeTruncate(text);
  let body;
  if (window.marked) {
    try { body = window.marked.parse(show, { mangle: false, headerIds: false }); }
    catch (_) { body = `<pre>${escHtml(show)}</pre>`; }
  } else {
    body = `<pre>${escHtml(show)}</pre>`;
  }
  let extra = "";
  if (full) extra = `<button class="show-more">Show full (${full.length.toLocaleString()} chars)</button>`;
  return `<div class="block">
    <div class="block-header"><span>${kind}</span><span class="block-flag">${text.length.toLocaleString()} chars</span></div>
    <div class="block-content long block-text" data-full="${escHtml(full || '')}">${body}</div>
    ${extra}
  </div>`;
}
function renderThinkingBlock(text) {
  if (!text) return "";
  const [show, full] = maybeTruncate(text);
  let extra = "";
  if (full) extra = `<button class="show-more">Show full (${full.length.toLocaleString()} chars)</button>`;
  return `<div class="block block-thinking collapsed">
    <div class="block-header"><span>thinking</span><span class="block-flag">click to expand · ${text.length.toLocaleString()} chars</span></div>
    <div class="block-content long" data-full="${escHtml(full || '')}">${escHtml(show)}</div>
    ${extra}
  </div>`;
}
function renderToolUseBlock(blk) {
  const name = blk.name || "tool";
  const input = blk.input;
  let body = "";
  if (input && typeof input === "object") {
    if (name === "Bash" && typeof input.command === "string") {
      body = `<pre>${escHtml(input.command)}</pre>`;
      if (input.description) body += `<div class="block-flag" style="margin-top:6px">${escHtml(input.description)}</div>`;
    } else {
      try { body = `<pre>${escHtml(JSON.stringify(input, null, 2))}</pre>`; }
      catch (_) { body = `<pre>${escHtml(String(input))}</pre>`; }
    }
  } else {
    body = `<pre>${escHtml(String(input == null ? "" : input))}</pre>`;
  }
  return `<div class="block block-tool collapsed">
    <div class="block-header"><span>tool: ${escHtml(name)}</span><span class="block-flag">id ${escHtml(String(blk.id || "").slice(0, 8))}…</span></div>
    <div class="block-content">${body}</div>
  </div>`;
}
function renderToolResultBlock(blk) {
  const id = blk.tool_use_id || "";
  const raw = blk.content;
  let text;
  if (typeof raw === "string") text = raw;
  else if (Array.isArray(raw)) {
    text = raw.map((b) => {
      if (!b || typeof b !== "object") return String(b);
      if (b.type === "text") return b.text || "";
      if (b.type === "image") return "[image]";
      return JSON.stringify(b);
    }).join("\n---\n");
  } else if (raw == null) text = "";
  else text = JSON.stringify(raw, null, 2);
  const [show, full] = maybeTruncate(text || "");
  let extra = "";
  if (full) extra = `<button class="show-more">Show full (${full.length.toLocaleString()} chars)</button>`;
  let blobButtons = "";
  const blobRefs = (text || "").match(/tool-results\/[a-zA-Z0-9._-]+/g) || [];
  for (const ref of new Set(blobRefs)) {
    const name = ref.split("/").pop();
    blobButtons += `<button class="show-more" data-blob="${escHtml(name)}">Load full output: ${escHtml(name)}</button>`;
  }
  const errFlag = blk.is_error ? ` · ⚠ error` : "";
  return `<div class="block block-tool-result collapsed">
    <div class="block-header"><span>tool result</span><span class="block-flag">id ${escHtml(String(id).slice(0, 8))}…${errFlag}</span></div>
    <div class="block-content"><pre data-full="${escHtml(full || '')}">${escHtml(show || "(empty)")}</pre></div>
    ${extra}${blobButtons}
  </div>`;
}
function renderUnknownBlock(blk) {
  let s;
  try { s = JSON.stringify(blk, null, 2); } catch (_) { s = String(blk); }
  return `<div class="block collapsed">
    <div class="block-header"><span>raw</span><span class="block-flag">click to expand</span></div>
    <div class="block-content"><pre>${escHtml(s)}</pre></div>
  </div>`;
}

// show-more / blob loader
document.addEventListener("click", async (ev) => {
  const target = ev.target;
  if (!(target instanceof HTMLElement)) return;
  if (!target.classList.contains("show-more")) return;
  const blob = target.dataset.blob;
  if (blob) {
    if (!S.selectedSession) return;
    const proj = S.selectedProjectId;
    const sp = S.selectedSession.sessionPath;
    const url = `/api/sessions/${encodeURIComponent(proj)}/${encodeURIComponent(sp)}/blob/tool-results/${encodeURIComponent(blob)}`;
    target.textContent = `Loading ${blob}…`;
    try {
      const r = await fetch(url);
      if (!r.ok) { target.textContent = `Failed (${r.status})`; return; }
      const txt = await r.text();
      const block = target.closest(".block");
      const pre = block && block.querySelector(".block-content pre");
      if (pre) { pre.textContent = txt; pre.dataset.full = ""; target.remove(); }
    } catch (e) {
      target.textContent = `Error: ${e.message || e}`;
    }
    return;
  }
  const block = target.closest(".block");
  if (!block) return;
  const slot = block.querySelector(".block-content");
  const full = slot && slot.dataset.full;
  if (full) {
    if (slot.classList.contains("block-text") && window.marked) {
      try { slot.innerHTML = window.marked.parse(full, { mangle: false, headerIds: false }); }
      catch (_) { slot.textContent = full; }
      requestIdleCallback(() => {
        slot.querySelectorAll("pre code").forEach((el) => {
          if (!/\blanguage-\w+/.test(el.className || "")) return;
          try { window.hljs && window.hljs.highlightElement(el); } catch (_) {}
        });
      }, { timeout: 800 });
    } else {
      const pre = slot.querySelector("pre");
      if (pre) pre.textContent = full;
      else slot.textContent = full;
    }
    slot.dataset.full = "";
  }
  target.remove();
});

boot().catch((e) => {
  console.error(e);
  document.body.innerHTML = `<div style="padding:40px;color:#e2e6ee;text-align:center">Boot error: ${escHtml(e.message || e)}</div>`;
});

})();
