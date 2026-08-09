const runs = {
  contract: { id: "FR-01", title: "Evidence contract", status: "Complete", kind: "complete", objective: "Define contract types, validation boundaries, and evidence classification for the approved plan.", elapsed: "18m 02s", branch: "codex/fr-01-contract", base: "83e540c", owner: "Coordinator" },
  schema: { id: "FR-02", title: "Schema admission", status: "Complete", kind: "complete", objective: "Reject invalid source schemas before worktree creation and retain an immutable admission receipt.", elapsed: "27m 11s", branch: "codex/fr-02-schema", base: "a13f6c2", owner: "Coordinator" },
  import: { id: "FR-03", title: "Import pipeline", status: "In review", kind: "active", objective: "Normalize source manifests and preserve cryptographic provenance across the ingestion boundary.", elapsed: "32m 18s", branch: "codex/fr-03-import", base: "9b1e4d0", owner: "Coordinator" },
  api: { id: "FR-04", title: "API integration", status: "Complete", kind: "complete", objective: "Expose the verified evidence read model through bounded, typed API routes.", elapsed: "14m 44s", branch: "codex/fr-04-api", base: "9b1e4d0", owner: "Coordinator" },
  audit: { id: "FR-05", title: "Audit projection", status: "Queued", kind: "queued", objective: "Project hash-chained audit records into a safe operator-facing evidence inventory.", elapsed: "Not started", branch: "pending", base: "FR-03 candidate", owner: "Unassigned" },
  ui: { id: "FR-06", title: "Provenance UI", status: "Queued", kind: "queued", objective: "Make source lineage, evidence availability, and integrity state legible to an operator.", elapsed: "Not started", branch: "pending", base: "FR-03 candidate", owner: "Unassigned" },
  e2e: { id: "FR-07", title: "End-to-end gates", status: "Blocked", kind: "blocked", objective: "Run the approved plan tests against the final sequential candidate lineage.", elapsed: "Waiting on 2 runs", branch: "pending", base: "FR-05 + FR-06", owner: "Controller" }
};

const statusClasses = { complete: "status-running", active: "status-review", queued: "status-queued", blocked: "status-review" };

function selectRun(key, shouldScroll = false) {
  const run = runs[key];
  if (!run) return;
  document.querySelectorAll(".run-node").forEach(node => node.classList.toggle("is-selected", node.dataset.run === key));
  document.querySelectorAll("#runTableBody tr").forEach(row => row.classList.toggle("active-row", row.dataset.run === key));
  document.getElementById("inspectorId").textContent = run.id;
  document.getElementById("inspectorTitle").textContent = run.title;
  document.getElementById("inspectorObjective").textContent = run.objective;
  document.getElementById("inspectorElapsed").textContent = run.elapsed;
  document.getElementById("inspectorBranch").innerHTML = `<code>${run.branch}</code>`;
  document.getElementById("inspectorBase").innerHTML = `<code>${run.base}</code>`;
  document.getElementById("inspectorOwner").innerHTML = `<span class="tiny-avatar purple">${run.owner.slice(0,2).toUpperCase()}</span>${run.owner}`;
  const status = document.getElementById("inspectorStatus");
  status.className = `status-pill ${statusClasses[run.kind] || "status-review"}`;
  status.innerHTML = run.kind === "active" ? run.status : `<span></span>${run.status}`;
  const phaseList = document.getElementById("phaseList");
  const attention = document.getElementById("attentionCard");
  if (run.kind === "active") {
    phaseList.style.opacity = "1";
    attention.classList.remove("is-hidden");
  } else {
    phaseList.style.opacity = run.kind === "complete" ? ".82" : ".35";
    attention.classList.add("is-hidden");
  }
  if (shouldScroll) document.querySelector(".run-inspector").scrollIntoView({ behavior: "smooth", block: "center" });
}

document.querySelectorAll(".run-node").forEach(node => node.addEventListener("click", () => selectRun(node.dataset.run)));
document.querySelectorAll("#runTableBody tr").forEach(row => row.addEventListener("click", () => selectRun(row.dataset.run, true)));

document.querySelectorAll(".tabs button").forEach(button => button.addEventListener("click", () => {
  document.querySelectorAll(".tabs button").forEach(item => item.classList.toggle("is-active", item === button));
  document.querySelectorAll(".tab-panel").forEach(panel => panel.classList.add("is-hidden"));
  document.getElementById(`${button.dataset.tab}Panel`).classList.remove("is-hidden");
}));

document.querySelectorAll(".segmented button").forEach(button => button.addEventListener("click", () => {
  button.parentElement.querySelectorAll("button").forEach(item => item.classList.toggle("is-active", item === button));
}));

document.querySelectorAll(".nav-item[data-target]").forEach(button => button.addEventListener("click", () => {
  document.querySelectorAll(".nav-item[data-target]").forEach(item => item.classList.toggle("is-active", item === button));
  document.getElementById(button.dataset.target)?.scrollIntoView({ behavior: "smooth", block: "start" });
  document.body.classList.remove("nav-open");
}));

document.getElementById("menuButton").addEventListener("click", () => document.body.classList.toggle("nav-open"));

function filterRows() {
  const query = document.getElementById("runSearch").value.trim().toLowerCase();
  const status = document.getElementById("statusFilter").value;
  document.querySelectorAll("#runTableBody tr").forEach(row => {
    const matchesText = row.textContent.toLowerCase().includes(query);
    const matchesStatus = status === "all" || row.dataset.status === status;
    row.hidden = !(matchesText && matchesStatus);
  });
}

document.getElementById("runSearch").addEventListener("input", filterRows);
document.getElementById("statusFilter").addEventListener("change", filterRows);
