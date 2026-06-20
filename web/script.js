const API_BASE = "https://pull-site-tool.onrender.com";

const form = document.getElementById("pull-form");
const submitBtn = document.getElementById("submit-btn");
const progressEl = document.getElementById("progress");
const progressStageEl = document.getElementById("progress-stage");
const reportEl = document.getElementById("report");
const reportStatusEl = document.getElementById("report-status");
const reportListEl = document.getElementById("report-list");
const reportIssuesEl = document.getElementById("report-issues");
const previewLink = document.getElementById("preview-link");
const resetBtn = document.getElementById("reset-btn");
const errorEl = document.getElementById("error-msg");

let pollTimer = null;

function showError(msg) {
  errorEl.textContent = msg;
  errorEl.hidden = false;
}

function resetUI() {
  if (pollTimer) clearInterval(pollTimer);
  form.reset();
  form.hidden = false;
  submitBtn.disabled = false;
  progressEl.hidden = true;
  reportEl.hidden = true;
  errorEl.hidden = true;
  reportListEl.innerHTML = "";
  reportIssuesEl.innerHTML = "";
  reportIssuesEl.hidden = true;
}

function addRow(label, value) {
  const li = document.createElement("li");
  const l = document.createElement("span");
  l.className = "label";
  l.textContent = label;
  const v = document.createElement("span");
  v.className = "value";
  v.textContent = value;
  li.append(l, v);
  reportListEl.appendChild(li);
}

function setStatusGlyph(ok, text) {
  reportStatusEl.innerHTML = `<span class="glyph">${ok ? "&#10003;" : "&times;"}</span><span class="text">${text}</span>`;
}

function renderReport(job) {
  progressEl.hidden = true;
  reportEl.hidden = false;

  if (job.status === "failed") {
    setStatusGlyph(false, "Job failed");
    addRow("Error", job.error || "Unknown error");
    previewLink.style.display = "none";
    return;
  }

  const report = job.report;

  if (report.stage === "download") {
    setStatusGlyph(false, "Download failed");
    addRow("Error", report.error);
    previewLink.style.display = "none";
    return;
  }

  setStatusGlyph(report.success, report.success ? "Completed successfully" : "Completed with issues");

  addRow("Filenames sanitized", String(Object.keys(report.renames).length));
  addRow("Links rewritten", String(report.links_rewritten));
  addRow(
    "SPA links fixed",
    `${report.spa_links_fixed} (${report.spa_links_unresolved.length} unresolved)`
  );
  addRow(
    "Framer artifacts removed",
    `badge ${report.framer_artifacts_removed.badge}, tracking ${report.framer_artifacts_removed.tracking_script}`
  );
  addRow("Broken links", String(report.broken_links.length));

  const smokeValues = Object.values(report.smoke_test);
  const smokeOk = smokeValues.filter((v) => v === 200).length;
  addRow("Smoke test", `${smokeOk}/${smokeValues.length} OK`);

  const issues = [
    ...report.broken_links.map((b) => `${b.file}: ${b.value}`),
    ...report.spa_links_unresolved.map((s) => `${s.file}: ${s.value} (${s.reason})`),
  ];
  if (issues.length) {
    reportIssuesEl.hidden = false;
    const h = document.createElement("h3");
    h.textContent = "Unresolved Issues";
    reportIssuesEl.appendChild(h);
    issues.forEach((text) => {
      const row = document.createElement("div");
      row.className = "issue";
      row.innerHTML = `<span class="x">&times;</span><span>${text}</span>`;
      reportIssuesEl.appendChild(row);
    });
  }

  previewLink.style.display = "";
  previewLink.href = `${API_BASE}/preview/${report.project_name}/`;
}

async function pollStatus(jobId) {
  try {
    const res = await fetch(`${API_BASE}/api/status/${jobId}`);
    if (!res.ok) throw new Error(`Status check failed (${res.status})`);
    const job = await res.json();

    if (job.status === "pending" || job.status === "running") {
      progressStageEl.textContent = job.stage || "Working…";
      return;
    }

    clearInterval(pollTimer);
    renderReport(job);
  } catch (err) {
    clearInterval(pollTimer);
    progressEl.hidden = true;
    form.hidden = false;
    submitBtn.disabled = false;
    showError(err.message);
  }
}

form.addEventListener("submit", async (e) => {
  e.preventDefault();
  errorEl.hidden = true;
  reportEl.hidden = true;

  const url = document.getElementById("url").value.trim();
  const projectName = document.getElementById("project_name").value.trim();

  submitBtn.disabled = true;
  form.hidden = true;
  progressEl.hidden = false;
  progressStageEl.textContent = "Starting…";

  try {
    const res = await fetch(`${API_BASE}/api/pull-site`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url, project_name: projectName || undefined }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || `Request failed (${res.status})`);

    pollTimer = setInterval(() => pollStatus(data.job_id), 1500);
    pollStatus(data.job_id);
  } catch (err) {
    progressEl.hidden = true;
    form.hidden = false;
    submitBtn.disabled = false;
    showError(err.message);
  }
});

resetBtn.addEventListener("click", resetUI);
