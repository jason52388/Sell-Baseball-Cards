// Upload page (/): drop photos, follow each photo's progress, then add the
// found cards to the collection (or fix, pair, re-read or discard them).

const U = {
  files: [],
  jobs: new Map(),     // job_id -> job
  stops: new Map(),    // job_id -> stop polling
  doneSeen: new Set(), // "job:index" photos already finished
  fronts: [],
  backs: [],
  pick: null,          // card id waiting for its partner (front/back pairing)
};
const $ = (id) => document.getElementById(id);

// --- choosing photos ------------------------------------------------------------

function setFiles(list) {
  U.files = Array.from(list || []);
  const n = U.files.length;
  $("picked").classList.toggle("hidden", !n);
  $("picked").textContent = n ? `${plural(n, "photo")} chosen: ${U.files.slice(0, 3).map((f) => f.name).join(", ")}${n > 3 ? "..." : ""}` : "";
  $("identifyBtn").disabled = !n;
  $("queueBtn").disabled = !n;
  $("identifyBtn").textContent = n ? `Identify cards (${n})` : "Identify cards";
}

function wireDrop() {
  const drop = $("drop"), input = $("fileInput");
  $("pick").addEventListener("click", () => input.click());
  $("pick").addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); input.click(); } });
  input.addEventListener("change", () => setFiles(input.files));
  ["dragover", "dragenter"].forEach((ev) => drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.add("drag"); }));
  ["dragleave", "drop"].forEach((ev) => drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.remove("drag"); }));
  drop.addEventListener("drop", (e) => { if (e.dataTransfer.files.length) setFiles(e.dataTransfer.files); });
  $("gridMode").addEventListener("change", () => $("gridDims").classList.toggle("hidden", !$("gridMode").checked));
  $("identifyBtn").addEventListener("click", identify);
  $("queueBtn").addEventListener("click", saveToInbox);
  $("manualBtn").addEventListener("click", addManual);
}

function formWithFiles() {
  const fd = new FormData();
  U.files.forEach((f) => fd.append("files", f));
  const tag = $("batchTag").value.trim();
  if (tag) fd.append("batch_tag", tag);
  return fd;
}

async function identify() {
  if (!U.files.length) return;
  const fd = formWithFiles();
  if ($("gridMode").checked) {
    fd.append("grid_rows", $("gridRows").value || "3");
    fd.append("grid_cols", $("gridCols").value || "3");
  }
  $("identifyBtn").disabled = true;
  $("identifyBtn").textContent = "Sending photos...";
  const r = await api("/api/upload", { method: "POST", body: fd });
  if (!r.ok) {
    toast("Upload failed: " + errText(r));
    setFiles(U.files);
    return;
  }
  setFiles([]);
  $("fileInput").value = "";
  trackJob(r.data);
}

async function saveToInbox() {
  if (!U.files.length) return;
  $("queueBtn").disabled = true;
  $("queueMsg").textContent = "Saving...";
  const r = await api("/api/queue", { method: "POST", body: formWithFiles() });
  if (!r.ok) { $("queueMsg").textContent = "Could not save: " + errText(r); $("queueBtn").disabled = false; return; }
  $("queueMsg").textContent = `Saved ${plural(r.data.queued, "photo")} to the inbox.`;
  setFiles([]);
  $("fileInput").value = "";
}

async function addManual() {
  const body = {};
  $("manualForm").querySelectorAll("[data-m]").forEach((inp) => {
    const k = inp.dataset.m;
    if (inp.type === "checkbox") body[k] = inp.checked;
    else if (inp.value.trim()) body[k] = inp.value.trim();
  });
  const msg = $("manualMsg");
  if (!body.player) { msg.textContent = "Type the player's name first."; return; }
  if (!body.year && !body.set_brand) { msg.textContent = "Add a year or a set so it can be priced."; return; }
  $("manualBtn").disabled = true;
  msg.textContent = "Looking up the price...";
  const r = await api("/api/cards/manual", { json: body });
  $("manualBtn").disabled = false;
  if (!r.ok) { msg.textContent = "Could not add it: " + errText(r); return; }
  const c = r.data;
  msg.innerHTML = `Added <a href="/card/${c.id}">${esc(cardTitle(c))}</a>: ${c.estimated_price != null ? money(c.estimated_price) : "no price found"}.`;
  $("manualForm").querySelectorAll("[data-m]").forEach((inp) => { if (inp.type === "checkbox") inp.checked = false; else inp.value = ""; });
  refreshReviewCount();
}

// --- upload jobs ------------------------------------------------------------------

function trackJob(job) {
  U.jobs.set(job.job_id, job);
  renderJobs();
  noticeFinished(job);
  if (job.status !== "done" && !U.stops.has(job.job_id)) {
    U.stops.set(job.job_id, pollJob(job.job_id, (j) => {
      U.jobs.set(j.job_id, j);
      renderJobs();
      noticeFinished(j);
      if (j.status === "done") U.stops.delete(j.job_id);
    }));
  }
}

// Reload the cards below whenever a photo finishes.
let _pendingTimer = null;
function noticeFinished(job) {
  let fresh = false;
  job.photos.forEach((p) => {
    const key = `${job.job_id}:${p.index}`;
    if (p.state === "done" && !U.doneSeen.has(key)) { U.doneSeen.add(key); fresh = true; }
  });
  if (fresh) {
    clearTimeout(_pendingTimer);
    _pendingTimer = setTimeout(loadPending, 300);
  }
}

function photoRow(job, p) {
  let icon, text;
  if (p.state === "done" && p.duplicate) {
    icon = "↺";
    text = `${esc(noLongDash(p.message || "Uploaded before, skipped."))} <button class="linkbtn" data-retry="${p.index}" data-force="1">Add anyway</button>`;
  } else if (p.state === "done") {
    icon = "✅";
    text = esc(noLongDash(p.message || "Done"));
  } else if (p.state === "working") {
    icon = `<span class="spin" aria-label="working"></span>`;
    text = esc(p.step ? p.step + "..." : "Working...");
  } else if (p.state === "waiting") {
    icon = "⏳";
    text = "Waiting";
  } else {
    icon = "⚠️";
    text = `${esc(noLongDash(p.message || "Failed"))} <button class="linkbtn" data-retry="${p.index}">Retry</button> · <button class="linkbtn" data-grid="${p.index}">Split as grid</button>`;
  }
  return `<div class="job" data-job="${job.job_id}"><span class="ic">${icon}</span><span class="nm" title="${esc(p.filename)}">${esc(p.filename)}</span><span class="ms">${text}</span></div>`;
}

function renderJobs() {
  const jobs = [...U.jobs.values()].filter((j) => j.kind === "upload");
  $("jobs").innerHTML = jobs.map((j) => {
    const when = j.created_at ? new Date(j.created_at + (/Z|[+-]\d\d:?\d\d$/.test(j.created_at) ? "" : "Z")).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" }) : "";
    const head = j.status === "done"
      ? `${plural(j.total, "photo")} done${j.failed ? `, ${j.failed} failed` : ""}`
      : `Working: ${j.done} of ${plural(j.total, "photo")} done`;
    return `<div class="jobs" data-jobbox="${j.job_id}">
      <div class="jhead"><b>${head}</b><span class="muted small">${when ? "Started " + esc(when) : ""}${j.batch_tag ? ` · batch "${esc(j.batch_tag)}"` : ""}</span>
        ${j.status === "done" ? `<button class="linkbtn" data-dismiss="${j.job_id}" style="margin-left:auto">Hide</button>` : ""}</div>
      ${j.photos.map((p) => photoRow(j, p)).join("")}
    </div>`;
  }).join("");
  $("jobs").querySelectorAll("[data-retry]").forEach((b) => b.addEventListener("click", () => {
    const jobId = b.closest("[data-job]").dataset.job;
    retry(jobId, Number(b.dataset.retry), b.dataset.force ? "?force=true" : "");
  }));
  $("jobs").querySelectorAll("[data-grid]").forEach((b) => b.addEventListener("click", () => {
    splitAsGrid(b.closest("[data-job]").dataset.job, Number(b.dataset.grid));
  }));
  $("jobs").querySelectorAll("[data-dismiss]").forEach((b) => b.addEventListener("click", async () => {
    const id = b.dataset.dismiss;
    await api(`/api/jobs/${id}/dismiss`, { method: "POST" });
    U.jobs.delete(id);
    renderJobs();
  }));
}

async function retry(jobId, index, query) {
  const r = await api(`/api/jobs/${jobId}/retry/${index}${query}`, { method: "POST" });
  if (!r.ok) { toast("Could not retry: " + errText(r)); return; }
  U.doneSeen.delete(`${jobId}:${index}`);
  U.stops.delete(jobId);
  trackJob(r.data);
}

function splitAsGrid(jobId, index) {
  const { el, close } = openModal(`
    <div class="head"><h3>Split this photo as a grid</h3><button class="more" data-close aria-label="Close">✕</button></div>
    <p class="muted" style="margin-top:0">Cuts the photo into even rows and columns and reads each piece as a card.</p>
    <div class="inline-form">
      <input type="number" id="gr" min="1" max="10" value="3" style="width:70px" aria-label="Rows"/> rows by
      <input type="number" id="gc" min="1" max="10" value="3" style="width:70px" aria-label="Columns"/> columns
    </div>
    <div class="foot"><button class="btn" data-ok>Split and read</button><button class="btn ghost" data-close>Cancel</button></div>`);
  el.querySelector("[data-ok]").addEventListener("click", () => {
    const rows = parseInt(el.querySelector("#gr").value, 10) || 3;
    const cols = parseInt(el.querySelector("#gc").value, 10) || 3;
    close();
    retry(jobId, index, `?grid_rows=${rows}&grid_cols=${cols}`);
  });
}

async function resumeJobs() {
  const r = await api("/api/jobs/active");
  if (!r.ok) return;
  (r.data.jobs || []).filter((j) => j.kind === "upload").forEach((j) => {
    // Photos finished before this page opened do not trigger a reload.
    j.photos.forEach((p) => { if (p.state === "done") U.doneSeen.add(`${j.job_id}:${p.index}`); });
    trackJob(j);
  });
}

// --- cards waiting to be added ------------------------------------------------------

async function loadPending() {
  const [f, b] = await Promise.all([api("/api/cards?status=preview"), api("/api/cards?status=unmatched_backs")]);
  U.fronts = f.ok ? f.data : [];
  U.backs = b.ok ? b.data : [];
  if (U.pick && ![...U.fronts, ...U.backs].some((c) => c.id === U.pick)) U.pick = null;
  renderPending();
}

function frontTile(c) {
  const f = cropUrl(c), b = backUrl(c);
  const zoom = [f, b].filter(Boolean).join("|");
  const low = (c.confidence == null ? 0 : c.confidence) < 0.7;
  const price = c.estimated_price != null
    ? `<span class="price" style="font-size:15px">${money(c.estimated_price)}</span> <span class="basis ${c.price_basis === "sold" ? "sold" : c.price_basis === "active" ? "ask" : ""}">${c.price_basis === "active" ? "asking" : esc(c.price_basis || "")}</span>`
    : `<span class="st rev">no price yet</span>`;
  const why = low ? "Not sure it was read right; check it" : plainReasons(c.review_reason)[0] || "";
  return `<div class="tile${U.pick === c.id ? " picked" : ""}" data-card="${c.id}">
    <div class="pic">${f ? `<img src="${f}" alt="front" data-zoom="${zoom}"/>` : "no photo"}</div>
    <div class="t">${esc(cardTitle(c))}</div>
    <div class="s">${esc(cardLine(c))}</div>
    <div>${tagsHtml(c, { batch: false })}${c.has_back ? "" : `<span class="tag warn">no back yet</span>`}</div>
    <div style="margin-top:6px">${price}</div>
    ${why ? `<div class="msg">${esc(why)}</div>` : ""}
    <div class="acts">
      <button class="btn" type="button" data-add="${c.id}">Add</button>
      <button class="btn ghost" type="button" data-edit="${c.id}">Edit</button>
      <button class="more" type="button" data-menu="${c.id}" aria-label="More actions">⋯</button>
    </div>
  </div>`;
}

function backTile(b) {
  const url = `/api/cards/${b.id}/crop?v=${b.upload_id || ""}`;
  return `<div class="tile dashed${U.pick === b.id ? " picked" : ""}" data-card="${b.id}" data-isback="1">
    <div class="pic"><img src="${url}" alt="back" data-zoom="${url}"/></div>
    <div class="t">Back: ${esc(b.player || "could not read")}</div>
    <div class="s">${esc(cardLine(b))}</div>
    <div class="acts">
      <button class="btn ghost" type="button" data-pickfront="${b.id}">Pick its front</button>
      <button class="more" type="button" data-backmenu="${b.id}" aria-label="More actions">⋯</button>
    </div>
  </div>`;
}

function renderPending() {
  const nF = U.fronts.length, nB = U.backs.length;
  $("pendingHead").classList.toggle("hidden", !nF && !nB);
  $("readyCount").textContent = `Ready to add (${nF})`;
  $("backsNote").textContent = nB ? `· ${plural(nB, "back")} still waiting for ${nB === 1 ? "its front" : "their fronts"}` : "";
  $("addAllBtn").textContent = nF ? `Add all ${nF}` : "Add all";
  $("addAllBtn").disabled = !nF;
  $("discardAllBtn").textContent = `Discard all`;
  // Unpaired fronts first (they are the ones that may need a back), then backs.
  const fronts = [...U.fronts].sort((a, b) => (a.has_back - b.has_back) || a.id - b.id);
  $("pending").innerHTML = fronts.map(frontTile).join("") + U.backs.map(backTile).join("");
  renderPickBar();
  wirePending();
}

function renderPickBar() {
  const bar = $("pickBar");
  if (!U.pick) { bar.classList.add("hidden"); return; }
  const c = [...U.fronts, ...U.backs].find((x) => x.id === U.pick);
  const isBack = c && c.side === "back";
  bar.classList.remove("hidden");
  bar.innerHTML = `<b>${isBack ? "Click the front that goes with this back." : "Click the other side of this card."}</b>
    <span>${esc(c ? (isBack ? "Back: " : "") + cardTitle(c) : "")}</span>
    <button class="clear" type="button" id="pickCancel">Cancel</button>`;
  $("pickCancel").onclick = () => { U.pick = null; renderPending(); };
}

function findCard(id) {
  return [...U.fronts, ...U.backs].find((c) => c.id === Number(id));
}

function wirePending() {
  const root = $("pending");
  root.querySelectorAll("[data-add]").forEach((b) => b.addEventListener("click", () => addCards([Number(b.dataset.add)], b)));
  root.querySelectorAll("[data-edit]").forEach((b) => b.addEventListener("click", () => openEditor(findCard(b.dataset.edit), loadPending)));
  root.querySelectorAll("[data-menu]").forEach((b) => b.addEventListener("click", (e) => {
    e.stopPropagation();
    const c = findCard(b.dataset.menu);
    openMenu(b, [
      { label: "Open details", onClick: () => { window.location.href = `/card/${c.id}`; } },
      { label: "Re-analyze", onClick: () => reanalyzeCard(c, loadPending) },
      { label: "Pair with its other side", onClick: () => { U.pick = c.id; renderPending(); } },
      c.has_back && { label: "Unmatch back", onClick: () => unmatchBack(c, loadPending) },
      { label: "Fix price with a SportsCardsPro link", onClick: () => priceFromLink(c) },
      "sep",
      { label: "Discard", danger: true, onClick: () => deleteCard(c, loadPending) },
    ]);
  }));
  root.querySelectorAll("[data-pickfront]").forEach((b) => b.addEventListener("click", () => {
    U.pick = Number(b.dataset.pickfront);
    renderPending();
    toast("Now click the front that goes with this back.");
  }));
  root.querySelectorAll("[data-backmenu]").forEach((b) => b.addEventListener("click", (e) => {
    e.stopPropagation();
    const c = findCard(b.dataset.backmenu);
    openMenu(b, [{ label: "Discard this back", danger: true, onClick: () => deleteCard({ ...c, player: c.player ? `the back of ${c.player}` : "the back" }, loadPending) }]);
  }));
  // Pairing: with a card picked, clicking another tile pairs the two.
  root.querySelectorAll("[data-card]").forEach((tile) => tile.addEventListener("click", (e) => {
    if (!U.pick || e.target.closest("button, a, input, [data-zoom]")) return;
    const other = Number(tile.dataset.card);
    if (other === U.pick) return;
    pair(U.pick, other);
  }));
}

async function pair(a, b) {
  const r = await api(`/api/cards/${a}/pair/${b}`, { method: "POST" });
  U.pick = null;
  if (r.ok) toast("Front and back joined.");
  else toast("Could not pair them: " + errText(r));
  loadPending();
}

function priceFromLink(c) {
  const { el, close } = openModal(`
    <div class="head"><h3>Fix the price with a link</h3><button class="more" data-close aria-label="Close">✕</button></div>
    <p class="muted" style="margin-top:0">Wrong price for ${esc(cardTitle(c))}? Paste the card's page from sportscardspro.com and the price comes from there.</p>
    <div class="inline-form"><input type="url" id="scp" placeholder="https://www.sportscardspro.com/game/..."/></div>
    <div class="foot"><button class="btn" data-ok>Use this link</button><button class="btn ghost" data-close>Cancel</button><span class="muted small" data-msg></span></div>`);
  el.querySelector("#scp").focus();
  el.querySelector("[data-ok]").addEventListener("click", async (e) => {
    const url = el.querySelector("#scp").value.trim();
    if (!url) { el.querySelector("[data-msg]").textContent = "Paste a link first."; return; }
    e.target.disabled = true;
    el.querySelector("[data-msg]").textContent = "Reading that page...";
    const r = await api(`/api/cards/${c.id}/price-from-url`, { json: { url } });
    if (!r.ok) { el.querySelector("[data-msg]").textContent = errText(r); e.target.disabled = false; return; }
    close();
    toast("Price updated from the link.");
    loadPending();
  });
}

// Before Add: ask which cards look like ones you already own (or like each
// other). Resolves to the ids to add, or null when the user cancels.
async function confirmDuplicates(ids) {
  const r = await api("/api/cards/promote/check", { json: { card_ids: ids } });
  const matches = r.ok ? r.data.matches || [] : [];
  if (!matches.length) return ids;
  const line = (m) => {
    const where = m.others.map((o) => `${esc(o.title)} ${o.in_collection
      ? `(<a href="/card/${o.id}" target="_blank">in your collection</a>)` : "(also being added)"}`).join(", ");
    const how = m.tier === "certain" ? "Same card as" : `Might be the same card as (${esc(m.reason)})`;
    return `<li><b>${esc(m.title)}</b><br><span class="muted small">${how} ${where}</span></li>`;
  };
  const flagged = new Set(matches.map((m) => m.id));
  const rest = ids.filter((id) => !flagged.has(id));
  return new Promise((resolve) => {
    const { el, close } = openModal(`
      <div class="head"><h3>${matches.length === 1 ? "This card may be a duplicate" : `${matches.length} cards may be duplicates`}</h3><button class="more" data-close aria-label="Close">✕</button></div>
      <p class="muted" style="margin-top:0">If you own more than one copy, add them anyway.</p>
      <ul style="margin:0 0 8px;padding-left:18px;display:grid;gap:8px">${matches.map(line).join("")}</ul>
      <div class="foot">
        <button class="btn" data-all>Add anyway</button>
        ${rest.length ? `<button class="btn ghost" data-rest>Add the other ${plural(rest.length, "card")} only</button>` : ""}
        <button class="btn ghost" data-close>Cancel</button></div>`,
      { wide: true, onClose: () => finish(null) });
    let done = false;
    const finish = (v) => { if (done) return; done = true; close(); resolve(v); };
    el.querySelector("[data-all]").addEventListener("click", () => finish(ids));
    el.querySelector("[data-rest]")?.addEventListener("click", () => finish(rest));
  });
}

// Add cards to the collection. Copying photos can take a while, so the button says so.
async function addCards(ids, btn) {
  if (!ids.length) return;
  ids = await confirmDuplicates(ids);
  if (!ids || !ids.length) return;
  const label = btn ? btn.textContent : "";
  if (btn) { btn.disabled = true; btn.textContent = ids.length > 1 ? `Adding ${ids.length}... copying photos to your collection` : "Adding..."; }
  const r = await api("/api/cards/promote", { json: { card_ids: ids } });
  if (btn) { btn.disabled = false; btn.textContent = label; }
  if (!r.ok) { toast("Could not add: " + errText(r)); return; }
  const added = r.data.added || [], skipped = r.data.skipped || [];
  if (ids.length > 1 || skipped.length) {
    $("addResult").innerHTML = `<div class="progress-line"><span>✅</span><span>${plural(added.length, "card")} added to your collection${
      skipped.length ? `; ${skipped.length} not added (${esc(skipped.map((s) => s.reason).join("; "))})` : ""}.</span>
      <a href="/repository" style="margin-left:auto">View collection</a></div>`;
  } else {
    toast(`${cardTitle(added[0] || {})} added to your collection.`);
  }
  await loadPending();
  refreshReviewCount();
}

function wirePendingBar() {
  $("addAllBtn").addEventListener("click", (e) => addCards(U.fronts.map((c) => c.id), e.currentTarget));
  $("discardAllBtn").addEventListener("click", async () => {
    const all = [...U.fronts, ...U.backs];
    if (!all.length) return;
    const ok = await confirmDialog(`Discard all ${all.length}?`,
      "Every card and back waiting here is deleted. You can undo right after, or restore them from Collection, Deleted, for 7 days.",
      "Discard all", { danger: true });
    if (!ok) return;
    await Promise.all(all.map((c) => api(`/api/cards/${c.id}`, { method: "DELETE" })));
    toast(`Discarded ${plural(all.length, "card")}.`, {
      action: "Undo",
      onAction: async () => {
        await Promise.all(all.map((c) => api(`/api/cards/${c.id}/restore`, { method: "POST" })));
        toast("Restored.");
        loadPending();
      },
    });
    loadPending();
  });
}

(async () => {
  wireDrop();
  wirePendingBar();
  await initShell("upload");
  await Promise.all([resumeJobs(), loadPending()]);
})();
