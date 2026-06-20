const API_BASE = "https://pull-site-tool.onrender.com";

const COLD_START_HINT_DELAY = 4000;
const COLD_START_HINT_TEXT =
  "This runs on a free server that sleeps when idle — it can take up to 50 seconds to wake up. Please wait…";
const MAX_START_RETRIES = 5;
const START_RETRY_DELAY = 6000;
const MAX_POLL_FAILURES = 5;

const form = document.getElementById("pull-form");
const submitBtn = document.getElementById("submit-btn");
const progressEl = document.getElementById("progress");
const progressStageEl = document.getElementById("progress-stage");
const progressHintEl = document.getElementById("progress-hint");
const reportEl = document.getElementById("report");
const reportStatusEl = document.getElementById("report-status");
const reportListEl = document.getElementById("report-list");
const reportIssuesEl = document.getElementById("report-issues");
const previewLink = document.getElementById("preview-link");
const resetBtn = document.getElementById("reset-btn");
const errorEl = document.getElementById("error-msg");

const githubBtn = document.getElementById("github-btn");
const githubModal = document.getElementById("github-modal");
const githubModalClose = document.getElementById("github-modal-close");
const githubStepSelect = document.getElementById("github-step-select");
const githubSelectHint = document.getElementById("github-select-hint");
const githubRepoSelect = document.getElementById("github-repo-select");
const githubCancel1 = document.getElementById("github-cancel-1");
const githubContinueBtn = document.getElementById("github-continue-btn");
const githubStepConfirm = document.getElementById("github-step-confirm");
const githubConfirmProject = document.getElementById("github-confirm-project");
const githubConfirmRepo = document.getElementById("github-confirm-repo");
const githubCancel2 = document.getElementById("github-cancel-2");
const githubPushBtn = document.getElementById("github-push-btn");
const githubStepProgress = document.getElementById("github-step-progress");
const githubProgressStage = document.getElementById("github-progress-stage");
const githubStepResult = document.getElementById("github-step-result");
const githubResultStatus = document.getElementById("github-result-status");
const githubResultText = document.getElementById("github-result-text");
const githubResultLink = document.getElementById("github-result-link");
const githubCloseBtn = document.getElementById("github-close-btn");

let pollTimer = null;
let pollFailures = 0;
let pushPollTimer = null;
let lastReport = null;
let githubRepos = [];

function showError(msg) {
  errorEl.textContent = msg;
  errorEl.hidden = false;
}

function resetUI() {
  if (pollTimer) clearInterval(pollTimer);
  pollFailures = 0;
  form.reset();
  form.hidden = false;
  submitBtn.disabled = false;
  progressEl.hidden = true;
  progressHintEl.hidden = true;
  reportEl.hidden = true;
  errorEl.hidden = true;
  reportListEl.innerHTML = "";
  reportIssuesEl.innerHTML = "";
  reportIssuesEl.hidden = true;
  closeGithubModal();
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
    githubBtn.style.display = "none";
    return;
  }

  const report = job.report;

  if (report.stage === "download") {
    setStatusGlyph(false, "Download failed");
    addRow("Error", report.error);
    previewLink.style.display = "none";
    githubBtn.style.display = "none";
    return;
  }

  lastReport = { project_name: report.project_name, url: report.url };
  githubBtn.style.display = "";

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

function failStart(message) {
  progressEl.hidden = true;
  progressHintEl.hidden = true;
  form.hidden = false;
  submitBtn.disabled = false;
  showError(message);
}

async function pollStatus(jobId) {
  try {
    const res = await fetch(`${API_BASE}/api/status/${jobId}`);
    if (!res.ok) throw new Error(`Status check failed (${res.status})`);
    const job = await res.json();
    pollFailures = 0;
    progressHintEl.hidden = true;

    if (job.status === "pending" || job.status === "running") {
      progressStageEl.textContent = job.stage || "Working…";
      return;
    }

    clearInterval(pollTimer);
    renderReport(job);
  } catch (err) {
    pollFailures += 1;
    if (pollFailures >= MAX_POLL_FAILURES) {
      clearInterval(pollTimer);
      failStart(err.message);
    } else {
      progressStageEl.textContent = "Reconnecting…";
    }
  }
}

async function startPull(url, projectName) {
  const coldStartTimer = setTimeout(() => {
    progressHintEl.textContent = COLD_START_HINT_TEXT;
    progressHintEl.hidden = false;
  }, COLD_START_HINT_DELAY);

  for (let attempt = 0; attempt < MAX_START_RETRIES; attempt += 1) {
    try {
      const res = await fetch(`${API_BASE}/api/pull-site`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ url, project_name: projectName || undefined }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || `Request failed (${res.status})`);

      clearTimeout(coldStartTimer);
      progressHintEl.hidden = true;
      pollTimer = setInterval(() => pollStatus(data.job_id), 1500);
      pollStatus(data.job_id);
      return;
    } catch (err) {
      const isLastAttempt = attempt === MAX_START_RETRIES - 1;
      if (isLastAttempt) {
        clearTimeout(coldStartTimer);
        failStart(err.message);
        return;
      }
      progressHintEl.textContent = `${COLD_START_HINT_TEXT} (retry ${attempt + 1}/${MAX_START_RETRIES - 1})`;
      progressHintEl.hidden = false;
      await new Promise((resolve) => setTimeout(resolve, START_RETRY_DELAY));
    }
  }
}

form.addEventListener("submit", (e) => {
  e.preventDefault();
  errorEl.hidden = true;
  reportEl.hidden = true;
  pollFailures = 0;

  const url = document.getElementById("url").value.trim();
  const projectName = document.getElementById("project_name").value.trim();

  submitBtn.disabled = true;
  form.hidden = true;
  progressEl.hidden = false;
  progressHintEl.hidden = true;
  progressStageEl.textContent = "Starting…";

  startPull(url, projectName);
});

resetBtn.addEventListener("click", resetUI);

function showGithubStep(stepEl) {
  [githubStepSelect, githubStepConfirm, githubStepProgress, githubStepResult].forEach((el) => {
    el.hidden = el !== stepEl;
  });
}

function closeGithubModal() {
  if (pushPollTimer) clearInterval(pushPollTimer);
  githubModal.hidden = true;
  githubRepoSelect.hidden = true;
  githubRepoSelect.innerHTML = "";
  githubContinueBtn.disabled = true;
  githubSelectHint.textContent = "Loading your GitHub repositories…";
  githubResultLink.hidden = true;
  githubRepos = [];
}

async function openGithubModal() {
  if (!lastReport) return;
  githubModal.hidden = false;
  showGithubStep(githubStepSelect);

  try {
    const res = await fetch(`${API_BASE}/api/github/repos`);
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || `Request failed (${res.status})`);

    githubRepos = data.repos || [];
    if (githubRepos.length === 0) {
      githubSelectHint.textContent = "No repositories found on this GitHub account.";
      return;
    }

    githubSelectHint.textContent = "Choose the repository to push this project to:";
    githubRepoSelect.innerHTML = githubRepos
      .map((r, i) => `<option value="${i}">${r.full_name}${r.private ? " (private)" : " (public)"}</option>`)
      .join("");
    githubRepoSelect.hidden = false;
    githubContinueBtn.disabled = false;
  } catch (err) {
    githubSelectHint.textContent = `Could not load repositories: ${err.message}`;
  }
}

githubBtn.addEventListener("click", openGithubModal);
githubModalClose.addEventListener("click", closeGithubModal);
githubCancel1.addEventListener("click", closeGithubModal);
githubCancel2.addEventListener("click", closeGithubModal);
githubCloseBtn.addEventListener("click", closeGithubModal);

githubContinueBtn.addEventListener("click", () => {
  const repo = githubRepos[Number(githubRepoSelect.value)];
  if (!repo) return;

  githubConfirmProject.textContent = lastReport.project_name;
  githubConfirmRepo.textContent = repo.full_name;
  showGithubStep(githubStepConfirm);
});

async function pollPushStatus(jobId, repoFullName) {
  try {
    const res = await fetch(`${API_BASE}/api/push-status/${jobId}`);
    if (!res.ok) throw new Error(`Status check failed (${res.status})`);
    const job = await res.json();

    if (job.status === "pending" || job.status === "running") {
      githubProgressStage.textContent = job.stage || "Working…";
      return;
    }

    clearInterval(pushPollTimer);
    showGithubStep(githubStepResult);

    if (job.status === "done") {
      githubResultStatus.innerHTML = `<span class="glyph">&#10003;</span><span class="text">Pushed successfully</span>`;
      githubResultText.textContent = `${lastReport.project_name} was force-pushed to ${repoFullName}.`;
      githubResultLink.href = job.repo_url;
      githubResultLink.hidden = false;
    } else {
      githubResultStatus.innerHTML = `<span class="glyph">&times;</span><span class="text">Push failed</span>`;
      githubResultText.textContent = job.error || "Unknown error";
    }
  } catch (err) {
    clearInterval(pushPollTimer);
    showGithubStep(githubStepResult);
    githubResultStatus.innerHTML = `<span class="glyph">&times;</span><span class="text">Push failed</span>`;
    githubResultText.textContent = err.message;
  }
}

githubPushBtn.addEventListener("click", async () => {
  const repo = githubRepos[Number(githubRepoSelect.value)];
  if (!repo || !lastReport) return;

  showGithubStep(githubStepProgress);
  githubProgressStage.textContent = "Starting…";

  try {
    const res = await fetch(`${API_BASE}/api/push-to-github`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        project_name: lastReport.project_name,
        url: lastReport.url,
        repo: repo.full_name,
        default_branch: repo.default_branch,
      }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || `Request failed (${res.status})`);

    pushPollTimer = setInterval(() => pollPushStatus(data.job_id, repo.full_name), 1500);
    pollPushStatus(data.job_id, repo.full_name);
  } catch (err) {
    showGithubStep(githubStepResult);
    githubResultStatus.innerHTML = `<span class="glyph">&times;</span><span class="text">Push failed</span>`;
    githubResultText.textContent = err.message;
  }
});
