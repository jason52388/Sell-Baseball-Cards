// Shared helpers ------------------------------------------------------------

function toast(msg) {
  const t = document.getElementById("toast");
  if (!t) return;
  t.textContent = msg;
  t.classList.add("show");
  setTimeout(() => t.classList.remove("show"), 3000);
}

// Expand any image tagged `.zoomable` into a full-screen lightbox overlay
// (instead of opening a new browser tab). Click anywhere / Esc to close.
function openLightbox(src) {
  let ov = document.getElementById("lightbox");
  if (!ov) {
    ov = document.createElement("div");
    ov.id = "lightbox";
    ov.innerHTML = `<img alt="expanded card"/>`;
    ov.addEventListener("click", () => ov.classList.remove("show"));
    document.body.appendChild(ov);
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape") ov.classList.remove("show");
    });
  }
  ov.querySelector("img").src = src;
  ov.classList.add("show");
}
document.addEventListener("click", (e) => {
  const img = e.target.closest("img.zoomable");
  if (!img) return;
  e.preventDefault();
  openLightbox(img.dataset.full || img.src);
});

function confBadge(c) {
  if (c == null) return '<span class="badge red">?</span>';
  const pct = Math.round(c * 100);
  const cls = c >= 0.8 ? "green" : c >= 0.6 ? "amber" : "red";
  return `<span class="badge ${cls}">${pct}%</span>`;
}

function flagBadges(card) {
  let out = "";
  if (card.psa10_candidate) out += '<span class="badge psa">🏆 PSA10?</span> ';
  if (card.anomaly_flag) out += '<span class="badge anom">⚠ anomaly</span>';
  return out;
}

function money(v) {
  return v == null ? "—" : "$" + Number(v).toFixed(2);
}

// Escape untrusted text (scraped comp titles/sources) before inserting as HTML.
function esc(v) {
  if (v == null) return "";
  return String(v).replace(/[&<>"']/g, (ch) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch]
  ));
}

// Modal asking how to list a multi-card selection. Resolves to
// "individual" | "set" | null (cancelled).
function askListMode(count) {
  return new Promise((resolve) => {
    const overlay = document.createElement("div");
    overlay.style.cssText =
      "position:fixed;inset:0;background:rgba(0,0,0,.45);display:flex;" +
      "align-items:center;justify-content:center;z-index:1000";
    overlay.innerHTML = `
      <div class="card-box" style="max-width:420px;margin:0">
        <h3>List ${count} cards</h3>
        <p class="muted">How would you like to list the ${count} selected cards?</p>
        <div style="display:flex;flex-direction:column;gap:8px">
          <button data-mode="individual">List individually — ${count} separate listings</button>
          <button data-mode="set">List as one set — a single lot listing with all ${count} cards &amp; photos</button>
          <button data-mode="cancel" class="secondary">Cancel</button>
        </div>
      </div>`;
    const done = (mode) => { overlay.remove(); resolve(mode === "cancel" ? null : mode); };
    overlay.addEventListener("click", (e) => {
      if (e.target === overlay) return done("cancel");
      const mode = e.target.getAttribute("data-mode");
      if (mode) done(mode);
    });
    document.body.appendChild(overlay);
  });
}

let APP_CONFIG = { ebay_mode: "preview", price_markup: 1.5 };

async function loadConfig() {
  try { APP_CONFIG = await (await fetch("/api/config")).json(); } catch (e) {}
  return APP_CONFIG;
}

// Create an eBay listing for one card. Returns the SellResult.
async function listOnEbay(cardId) {
  const resp = await fetch(`/api/cards/${cardId}/list`, { method: "POST" });
  return resp.json();
}

function announceListResult(r) {
  if (r.status === "published") {
    toast(`✅ Listed on eBay (id ${r.listing_id}) at ${money(r.list_price)}.`);
  } else if (r.status === "preview") {
    toast(`👁 Preview only — nothing was listed (would be ${money(r.list_price)}). Configure eBay credentials to list for real.`);
  } else {
    toast(`⚠ ${r.status}: ${r.message || "could not list"}`);
  }
}

// List each selected card as its own eBay listing.
async function sellIndividually(ids) {
  const resp = await fetch("/api/listings/sell", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ card_ids: ids }),
  });
  const data = await resp.json();
  const pub = data.results.filter((r) => r.status === "published").length;
  const prev = data.results.filter((r) => r.status === "preview").length;
  if (prev && !pub) {
    toast(`👁 Previewed ${prev} listing(s) — nothing published (preview mode).`);
  } else {
    toast(`${pub}/${data.results.length} listed.`);
  }
}

// Combine all selected cards into a single eBay lot listing.
async function sellAsSet(ids) {
  const resp = await fetch("/api/listings/sell-set", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ card_ids: ids }),
  });
  const r = await resp.json();
  const skipped = r.skipped && r.skipped.length ? ` (${r.skipped.length} skipped)` : "";
  if (r.status === "published") {
    toast(`✅ Listed ${r.card_ids.length} cards as one lot (id ${r.listing_id}) at ${money(r.list_price)}${skipped}.`);
  } else if (r.status === "preview") {
    toast(`👁 Set preview: ${r.card_ids.length} cards would list as one lot at ${money(r.list_price)} — nothing published${skipped}.`);
  } else {
    toast(`⚠ Set listing ${r.status}: ${r.message || "could not list"}`);
  }
}

// Upload page ----------------------------------------------------------------

function initUpload() {
  const dz = document.getElementById("dropzone");
  const input = document.getElementById("fileInput");
  const btn = document.getElementById("uploadBtn");
  const queueBtn = document.getElementById("queueBtn");
  const countEl = document.getElementById("fileCount");
  let files = [];

  const setFiles = (list) => {
    files = Array.from(list);
    countEl.textContent = files.length ? `${files.length} file(s) selected` : "";
    btn.disabled = files.length === 0;
    if (queueBtn) queueBtn.disabled = files.length === 0;
  };

  dz.addEventListener("click", () => input.click());
  input.addEventListener("change", () => setFiles(input.files));
  ["dragover", "dragenter"].forEach((e) =>
    dz.addEventListener(e, (ev) => { ev.preventDefault(); dz.classList.add("drag"); })
  );
  ["dragleave", "drop"].forEach((e) =>
    dz.addEventListener(e, (ev) => { ev.preventDefault(); dz.classList.remove("drag"); })
  );
  dz.addEventListener("drop", (ev) => setFiles(ev.dataTransfer.files));

  // Grid-split toggle: reveal the rows×cols inputs only when enabled.
  const gridMode = document.getElementById("gridMode");
  const gridDims = document.getElementById("gridDims");
  if (gridMode && gridDims) {
    gridMode.addEventListener("change", () => {
      gridDims.style.display = gridMode.checked ? "inline" : "none";
    });
  }

  wireManualAdd();

  btn.addEventListener("click", async () => {
    if (!files.length) return;
    btn.disabled = true;
    const status = document.getElementById("status");
    status.textContent = `Analyzing ${files.length} photo(s)… this can take a moment.`;
    const fd = new FormData();
    files.forEach((f) => fd.append("files", f));
    const tag = (document.getElementById("batchTag")?.value || "").trim();
    if (tag) fd.append("batch_tag", tag);
    // When grid mode is on, ask the server to split each photo evenly.
    if (gridMode && gridMode.checked) {
      fd.append("grid_rows", document.getElementById("gridRows").value || "3");
      fd.append("grid_cols", document.getElementById("gridCols").value || "3");
    }
    try {
      const resp = await fetch("/api/upload", { method: "POST", body: fd });
      const data = await resp.json();
      renderUploadResults(data.results);
      status.textContent = "Done. Review valuable cards in the Repository.";
    } catch (e) {
      status.textContent = "Upload failed: " + e;
    } finally {
      btn.disabled = false;
    }
  });

  if (queueBtn) {
    queueBtn.addEventListener("click", async () => {
      if (!files.length) return;
      queueBtn.disabled = true;
      const status = document.getElementById("status");
      status.textContent = `Queueing ${files.length} photo(s)…`;
      const fd = new FormData();
      files.forEach((f) => fd.append("files", f));
      const tag = (document.getElementById("batchTag")?.value || "").trim();
      if (tag) fd.append("batch_tag", tag);
      try {
        const resp = await fetch("/api/queue", { method: "POST", body: fd });
        const data = await resp.json();
        status.innerHTML = `✅ Queued ${data.queued} photo(s) to the inbox. `
          + `Identify them on your Claude subscription:<br/>`
          + `<code>tools/ingest_folder.sh</code> &nbsp;(or ask Claude Code to ingest the inbox).`;
        setFiles([]);
        input.value = "";
      } catch (e) {
        status.textContent = "Queue failed: " + e;
      } finally {
        queueBtn.disabled = files.length === 0;
      }
    });
  }

  // Restore any previews still awaiting review so a refresh doesn't lose them.
  loadPending();
}

function wireManualAdd() {
  const btn = document.getElementById("addManualBtn");
  if (!btn) return;
  const val = (id) => document.getElementById(id).value.trim();
  btn.addEventListener("click", async () => {
    const player = val("m_player");
    const out = document.getElementById("manualResult");
    if (!player) { out.textContent = "Player is required."; return; }
    btn.disabled = true;
    out.textContent = "Pricing…";
    const body = {
      player,
      year: val("m_year") || null,
      set_brand: val("m_set") || null,
      card_number: val("m_number") || null,
      parallel: val("m_parallel") || null,
      condition: val("m_condition") || null,
      psa10_candidate: document.getElementById("m_psa10").checked,
      anomaly_flag: document.getElementById("m_anomaly").checked,
    };
    try {
      const resp = await fetch("/api/cards/manual", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      if (!resp.ok) {
        const err = await resp.json();
        out.textContent = "Error: " + (err.detail || resp.status);
        return;
      }
      const c = await resp.json();
      const est = c.estimated_price != null ? money(c.estimated_price) : "no price found";
      out.innerHTML = `Added <a href="/card/${c.id}">${player}</a> — ${est} `
        + `(${c.price_basis || c.status}). <a href="/repository">View library</a>.`;
    } catch (e) {
      out.textContent = "Failed: " + e;
    } finally {
      btn.disabled = false;
    }
  });
}

function renderUploadResults(results) {
  const container = document.getElementById("results");
  container.innerHTML = "";

  const allIds = [];
  results.forEach((r) => {
    const box = document.createElement("div");
    box.className = "card-box";
    if (r.error) {
      box.innerHTML = `<strong>${r.filename}</strong> — <span class="badge red">error</span> ${r.error}`;
      container.appendChild(box);
      return;
    }
    box.innerHTML = `<strong>${r.filename}</strong> — ${r.card_count} card(s) detected.
      <p class="muted">Review each card below, then add the ones you want to keep.</p>`;
    const grid = document.createElement("div");
    grid.className = "preview-grid";
    r.cards.forEach((c) => {
      allIds.push(c.id);
      grid.appendChild(renderPreviewCard(c));
    });
    box.appendChild(grid);
    container.appendChild(box);
  });

  // "Add all" convenience button across every previewed card.
  if (allIds.length > 1) {
    const bar = document.createElement("div");
    bar.style.margin = "8px 0 4px";
    bar.innerHTML = `<button id="addAllBtn">Add all ${allIds.length} to repository</button>`;
    container.prepend(bar);
    document.getElementById("addAllBtn").addEventListener("click", async (e) => {
      e.target.disabled = true;
      const remaining = Array.from(document.querySelectorAll(".preview-card[data-id]"))
        .map((el) => Number(el.dataset.id));
      await promoteCards(remaining);
    });
  }
}

// One previewed (not-yet-added) card, with verification photo + actions.
// opts.selectable adds the "select to match" checkbox (used in the matching section).
function renderPreviewCard(c, opts = {}) {
  const el = document.createElement("div");
  el.className = "preview-card";
  el.dataset.id = c.id;
  const lowConf = (c.confidence ?? 0) < 0.7;
  // Your uploaded photo — click to expand it in a lightbox.
  const cropV = c.upload_id || "";
  const cropUrl = `/api/cards/${c.id}/crop?v=${cropV}`;
  const yourPhoto = c.crop_path
    ? `<figure class="pv-fig"><img class="thumb zoomable" src="${cropUrl}" data-full="${cropUrl}"/>
         <figcaption class="muted">your photo (click to enlarge)</figcaption></figure>`
    : "";
  // Source comparison photo from the marketplace (only eBay sources carry images;
  // PriceCharting prices have no photo). Click to expand.
  const refPhoto = c.reference_image_url
    ? `<figure class="pv-fig"><img class="thumb zoomable" src="${c.reference_image_url}" data-full="${c.reference_image_url}" alt="market photo" onerror="this.closest('figure').replaceWith(document.createTextNode(''))"/>
         <figcaption class="muted">source match (click to enlarge)</figcaption></figure>`
    : `<figure class="pv-fig"><span class="muted">no source photo${c.price_sources ? ` from ${c.price_sources}` : ""}<br/>(add a free eBay keyset for comparison photos)</span></figure>`;
  const lowHint = lowConf
    ? `<p class="muted">⚠ Low confidence — compare the photos, <button class="linklike reanalyzeBtn">re-analyze with a stronger model</button>, or <a href="#addManualBtn" onclick="document.getElementById('m_player').focus()">enter it manually below</a>.</p>`
    : "";
  const matchPick = opts.selectable
    ? `<label class="match-pick muted"><input type="checkbox" class="matchSel" data-id="${c.id}"/> select to pair</label>`
    : "";
  el.innerHTML = `
    ${matchPick}
    <div class="pv-photos">${yourPhoto}${refPhoto}</div>
    <div class="pv-id">
      <strong>${c.player || "—"}</strong> ${confBadge(c.confidence)} ${flagBadges(c)}
      ${c.has_back
        ? `<span class="badge green" title="A back scan is attached">⇄ back matched</span>`
        : `<span class="badge amber" title="No back scan attached to this front">⚠ no back matched</span>`}<br/>
      <span class="muted">${[c.year, c.set_brand, c.card_number ? "#" + c.card_number : "", c.parallel].filter(Boolean).join(" ") || "—"}</span><br/>
      ${c.batch_tag ? `<span class="badge">🏷 ${c.batch_tag}</span><br/>` : ""}
      ${c.estimated_price != null
        ? `<span class="price">Est. ${money(c.estimated_price)}${c.price_basis ? ` (${c.price_basis})` : ""}</span>${c.price_sources ? ` <span class="muted">via ${c.price_sources}</span>` : ""}`
        : `<span class="muted">No price${c.review_reason ? ` — ${c.review_reason}` : " found"}</span>`}
      ${c.sold_max_estimate != null ? `<br/><span class="muted">Max sold (raw): ${money(c.sold_max_estimate)}</span>` : ""}
    </div>
    ${lowHint}
    <div class="pv-actions">
      <button class="addBtn">Add to repository</button>
      ${c.has_back ? `<button class="unmatchBtn linklike">Unmatch back</button>` : ""}
      <button class="reanalyzeBtn">Re-analyze (stronger AI)</button>
      <a class="link" href="/card/${c.id}" target="_blank" rel="noopener">View details</a>
      <button class="discardBtn linklike">Discard</button>
    </div>
    <div class="pv-msg muted"></div>`;

  const msg = el.querySelector(".pv-msg");
  el.querySelector(".addBtn").addEventListener("click", () => promoteCards([c.id]));
  const unmatchBtn = el.querySelector(".unmatchBtn");
  if (unmatchBtn) unmatchBtn.addEventListener("click", async () => {
    msg.textContent = "Detaching back…";
    try {
      const r = await fetch(`/api/cards/${c.id}/detach-back`, { method: "POST" });
      if (r.ok) { toast("Unmatched — back returned to the matching section."); loadPending(); }
      else { const d = await r.json().catch(() => ({})); msg.textContent = "Unmatch failed: " + (d.detail || r.status); }
    } catch (e) { msg.textContent = "Unmatch failed: " + e; }
  });
  el.querySelectorAll(".reanalyzeBtn").forEach((b) =>
    b.addEventListener("click", async () => {
      msg.textContent = "Re-analyzing with a stronger model…";
      try {
        const resp = await fetch(`/api/cards/${c.id}/reanalyze`, { method: "POST" });
        const data = await resp.json();
        if (!resp.ok) { msg.textContent = "Error: " + (data.detail || resp.status); return; }
        el.replaceWith(renderPreviewCard(data));
        toast("Re-analyzed.");
      } catch (e) { msg.textContent = "Failed: " + e; }
    })
  );
  el.querySelector(".discardBtn").addEventListener("click", async () => {
    const resp = await fetch(`/api/cards/${c.id}`, { method: "DELETE" });
    if (resp.ok || resp.status === 204) { el.remove(); toast("Discarded."); }
    else { msg.textContent = "Could not discard."; }
  });
  return el;
}

// Promote previewed cards into the repository, then reflect it in the UI.
async function promoteCards(ids) {
  if (!ids.length) return;
  try {
    const resp = await fetch("/api/cards/promote", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ card_ids: ids }),
    });
    const cards = await resp.json();
    if (!resp.ok) { toast("Add failed."); return; }
    cards.forEach((c) => {
      const el = document.querySelector(`.preview-card[data-id="${c.id}"]`);
      if (el) {
        el.classList.add("added");
        el.querySelector(".pv-actions").innerHTML =
          `<span class="badge green">✓ in repository</span> ${statusBadge(c)} ` +
          `<a class="link" href="/repository">View library</a>`;
        const m = el.querySelector(".pv-msg"); if (m) m.textContent = "";
      }
    });
    toast(`${cards.length} added to repository.`);
  } catch (e) { toast("Add failed: " + e); }
}

// Reload any previews still awaiting review (server is the source of truth) so a
// page refresh restores your pending cards instead of showing a blank page.
async function loadPending() {
  let cards = [], backs = [];
  try {
    [cards, backs] = await Promise.all([
      fetch("/api/cards?status=preview").then((r) => r.json()),
      fetch("/api/cards?status=unmatched_backs").then((r) => r.json()),
    ]);
  } catch (e) { return; }
  renderPendingPreviews(cards, backs);
}

// The (up to two) card ids the user has picked to pair together.
const matchPicks = [];

function renderPendingPreviews(cards, backs = []) {
  const container = document.getElementById("results");
  if (!container) return;
  container.innerHTML = "";
  matchPicks.length = 0;
  if (!cards.length && !backs.length) return;

  // Unmatched = fronts with no back attached + orphan back scans. Matched =
  // fronts that already have a back.
  const unmatchedFronts = cards.filter((c) => !c.has_back);
  const matchedFronts = cards.filter((c) => c.has_back);

  // --- Global action bar: add all fronts / discard everything ---
  const total = cards.length + backs.length;
  const topBar = document.createElement("div");
  topBar.style.margin = "0 0 10px";
  topBar.innerHTML =
    (cards.length > 1 ? `<button id="addAllPendingBtn">Add all ${cards.length}</button> ` : "") +
    `<button id="discardAllPendingBtn" class="danger">Discard all ${total}</button>`;
  container.appendChild(topBar);
  const addAll = document.getElementById("addAllPendingBtn");
  if (addAll) addAll.addEventListener("click", (e) => {
    e.target.disabled = true;
    promoteCards(cards.map((c) => c.id));
  });
  document.getElementById("discardAllPendingBtn")
    .addEventListener("click", () => discardAllPending(cards, backs));

  // --- Top section: anything that still needs a front/back match ---
  if (unmatchedFronts.length || backs.length) {
    container.appendChild(renderMatchingSection(unmatchedFronts, backs));
  }

  // --- Main section: matched (front+back) cards ready to add ---
  if (matchedFronts.length) {
    const box = document.createElement("div");
    box.className = "card-box";
    box.innerHTML = `<strong>${matchedFronts.length} matched card(s) ready to add</strong>
      <p class="muted">Front and back paired. Add the ones you want, or discard.</p>`;
    const grid = document.createElement("div");
    grid.className = "preview-grid";
    matchedFronts.forEach((c) => grid.appendChild(renderPreviewCard(c)));
    box.appendChild(grid);
    container.appendChild(box);
  }

  wireMatchSelection();
}

// Discard every pending card AND every unmatched back currently awaiting review.
async function discardAllPending(cards = [], backs = []) {
  const ids = [...cards.map((c) => c.id), ...backs.map((b) => b.id)];
  if (!ids.length) return;
  if (!confirm(`Discard all ${ids.length} card(s) waiting for review? This cannot be undone.`)) return;
  await Promise.all(ids.map((id) =>
    fetch(`/api/cards/${id}`, { method: "DELETE" }).catch(() => {})
  ));
  toast(`Discarded ${ids.length} card(s).`);
  loadPending();
}

// Top "needs matching" section: unmatched fronts (no back yet) + orphan backs,
// each selectable. Pick one front and one back, then Match.
function renderMatchingSection(unmatchedFronts, backs) {
  const n = unmatchedFronts.length + backs.length;
  const box = document.createElement("div");
  box.className = "card-box match-box";
  box.innerHTML = `<strong>${n} card(s) need front/back matching</strong>
    <p class="muted">These don't have both sides paired yet. To pair: click any <em>two</em> cards
    here (the front and back of the same card — either order), then click <em>Pair</em>. A card
    that's truly single-sided can just be added as-is.</p>
    <div class="match-bar"><button id="matchBtn" disabled>Pair selected cards</button>
      <span id="matchHint" class="muted"></span></div>`;
  const grid = document.createElement("div");
  grid.className = "preview-grid";
  // Unmatched fronts first (full preview cards, selectable), then orphan backs.
  unmatchedFronts.forEach((c) => grid.appendChild(renderPreviewCard(c, { selectable: true })));
  backs.forEach((b) => {
    const ident = [b.year, b.set_brand, b.player, b.card_number ? "#" + b.card_number : ""]
      .filter(Boolean).join(" ") || "could not read identity";
    const v = b.upload_id || "";
    const url = `/api/cards/${b.id}/crop?v=${v}`;
    const tile = document.createElement("div");
    tile.className = "preview-card";
    tile.dataset.backId = b.id;
    tile.innerHTML = `
      <label class="match-pick muted"><input type="checkbox" class="matchSel" data-id="${b.id}"/> select to pair</label>
      <figure class="pv-fig"><img class="thumb zoomable" src="${url}" data-full="${url}"/>
        <figcaption class="muted">back scan (click to enlarge)</figcaption></figure>
      <div class="pv-id"><span class="badge amber">back</span> <span class="muted">read as:</span> <strong>${ident}</strong></div>
      <div class="pv-actions"><button class="discardBackBtn linklike" data-id="${b.id}">Discard</button></div>`;
    grid.appendChild(tile);
  });
  box.appendChild(grid);
  // Discard a back outright.
  grid.querySelectorAll(".discardBackBtn").forEach((btn) =>
    btn.addEventListener("click", async () => {
      if (!confirm("Discard this back scan?")) return;
      const r = await fetch(`/api/cards/${btn.dataset.id}`, { method: "DELETE" });
      if (r.ok || r.status === 204) { toast("Back discarded."); loadPending(); }
    })
  );
  return box;
}

// Single-front + single-back selection, then POST attach-back.
function wireMatchSelection() {
  const boxes = Array.from(document.querySelectorAll(".matchSel"));
  const btn = document.getElementById("matchBtn");
  const hint = document.getElementById("matchHint");
  if (!btn) return;
  matchPicks.length = 0;

  const cbById = (id) => boxes.find((x) => Number(x.dataset.id) === id);
  const refresh = () => {
    btn.disabled = matchPicks.length !== 2;
    hint.textContent = matchPicks.length === 2
      ? `pair #${matchPicks[0]} ⇄ #${matchPicks[1]}`
      : matchPicks.length === 1
        ? "now pick the other side"
        : "select any two cards (the two sides of one card)";
  };

  boxes.forEach((cb) =>
    cb.addEventListener("change", () => {
      const id = Number(cb.dataset.id);
      if (cb.checked) {
        if (!matchPicks.includes(id)) matchPicks.push(id);
        // Cap at two — picking a third drops the oldest selection.
        while (matchPicks.length > 2) {
          const dropped = cbById(matchPicks.shift());
          if (dropped) dropped.checked = false;
        }
      } else {
        const i = matchPicks.indexOf(id);
        if (i >= 0) matchPicks.splice(i, 1);
      }
      refresh();
    })
  );

  // Make the WHOLE card clickable to toggle its selection (not just the small
  // checkbox). Clicks on buttons/links/images keep their own behavior (so you
  // can still Add, zoom a photo, etc.).
  document.querySelectorAll(".match-box .preview-card").forEach((tile) => {
    const cb = tile.querySelector(".matchSel");
    if (!cb) return;
    tile.classList.add("matchable");
    tile.addEventListener("click", (e) => {
      if (e.target.closest("button, a, input, label, img")) return;
      cb.checked = !cb.checked;
      cb.dispatchEvent(new Event("change"));
    });
  });

  btn.addEventListener("click", async () => {
    if (matchPicks.length !== 2) return;
    btn.disabled = true;
    try {
      const r = await fetch(`/api/cards/${matchPicks[0]}/pair/${matchPicks[1]}`, { method: "POST" });
      if (r.ok) { toast("Paired — front and back joined."); loadPending(); }
      else if (r.status === 404) {
        // Stale page (a card was discarded/re-ingested elsewhere). Reload.
        toast("That card list was out of date — refreshed it, please pick again.");
        loadPending();
      }
      else { const d = await r.json().catch(() => ({})); toast("Pair failed: " + (d.detail || r.status)); btn.disabled = false; }
    } catch (e) { toast("Pair failed: " + e); btn.disabled = false; }
  });
  refresh();
}

function statusBadge(c) {
  const map = {
    priced: ["green", "priced"],
    needs_review: ["amber", "needs review"],
    below_threshold: ["red", "< $4"],
    listed: ["green", "listed"],
    list_failed: ["red", "list failed"],
  };
  const [cls, label] = map[c.status] || ["amber", c.status];
  // Listed-on-eBay is a separate dimension from the price status, so show both.
  const listed = c.is_listed
    ? `<span class="badge green" title="Has a published eBay listing">✓ listed on eBay</span>`
    : `<span class="badge" style="background:#e7ebee;color:#56636b" title="Not yet listed on eBay">not listed</span>`;
  const reason = c.review_reason ? ` <span class="muted">(${c.review_reason})</span>` : "";
  return `<span class="badge ${cls}">${label}</span> ${listed}${reason}`;
}

// Repository page ------------------------------------------------------------

const REPO_STATE_KEY = "repoState";

// Persist the filter selections and scroll position so returning to the
// repository (e.g. after viewing a card) leaves you where you left off.
function saveRepoState() {
  try {
    const g = (id) => document.getElementById(id);
    sessionStorage.setItem(REPO_STATE_KEY, JSON.stringify({
      filter: g("filter")?.value || "",
      sport: g("sportFilter")?.value || "",
      psaOnly: g("psaOnly")?.checked || false,
      anomOnly: g("anomOnly")?.checked || false,
      tag: g("tagFilter")?.value || "",
      player: g("playerSearch")?.value || "",
      minPrice: g("minPrice")?.value || "",
      maxPrice: g("maxPrice")?.value || "",
      scrollY: window.scrollY,
    }));
  } catch (e) {}
}

function readRepoState() {
  try { return JSON.parse(sessionStorage.getItem(REPO_STATE_KEY) || "{}"); }
  catch (e) { return {}; }
}

// Rebuild the Tag dropdown from the distinct batch tags present in `cards`,
// keeping the current selection (or a saved one) if it still exists.
function populateTagOptions(sel, cards, savedTag) {
  if (!sel) return;
  const want = sel.value || savedTag || "";
  const tags = [...new Set(cards.map((c) => c.batch_tag).filter(Boolean))].sort();
  sel.innerHTML = '<option value="">All</option>'
    + tags.map((t) => `<option value="${t}">${t}</option>`).join("");
  sel.value = tags.includes(want) ? want : "";
}

// Rebuild the Sport dropdown from the distinct sports present in `cards`.
function populateSportOptions(sel, cards, savedSport) {
  if (!sel) return;
  const want = sel.value || savedSport || "";
  const sports = [...new Set(cards.map((c) => c.sport).filter(Boolean))].sort();
  const label = (s) => s.charAt(0).toUpperCase() + s.slice(1);
  sel.innerHTML = '<option value="">All</option>'
    + sports.map((s) => `<option value="${s}">${label(s)}</option>`).join("");
  sel.value = sports.includes(want) ? want : "";
}

async function initRepository() {
  const filter = document.getElementById("filter");
  const psaOnly = document.getElementById("psaOnly");
  const anomOnly = document.getElementById("anomOnly");
  const tagFilter = document.getElementById("tagFilter");
  const sportFilter = document.getElementById("sportFilter");
  const playerSearch = document.getElementById("playerSearch");
  const minPrice = document.getElementById("minPrice");
  const maxPrice = document.getElementById("maxPrice");
  const sellBtn = document.getElementById("sellBtn");

  // Restore previously chosen filters before the first load.
  const saved = readRepoState();
  if (saved.filter != null) filter.value = saved.filter;
  if (saved.psaOnly != null) psaOnly.checked = saved.psaOnly;
  if (saved.anomOnly != null) anomOnly.checked = saved.anomOnly;
  if (saved.player != null) playerSearch.value = saved.player;
  if (saved.minPrice != null) minPrice.value = saved.minPrice;
  if (saved.maxPrice != null) maxPrice.value = saved.maxPrice;
  const savedTag = saved.tag || "";
  const savedSport = saved.sport || "";

  await loadConfig();
  const banner = document.getElementById("modeBanner");
  if (banner) {
    banner.innerHTML = APP_CONFIG.ebay_mode === "preview"
      ? '<span class="badge amber">PREVIEW MODE</span> Listing buttons build the real eBay listing but publish nothing. Set EBAY_MODE=sandbox/live with credentials to list for real.'
      : `<span class="badge green">${(APP_CONFIG.ebay_mode || "").toUpperCase()} MODE</span> Listing buttons publish to eBay.`;
  }

  const load = async (restoreScroll = false) => {
    const status = filter.value;
    // Dedicated view: back scans that didn't auto-pair to a front.
    if (status === "unmatched_backs") {
      const [backs, fronts] = await Promise.all([
        fetch("/api/cards?status=unmatched_backs").then((r) => r.json()),
        fetch("/api/cards").then((r) => r.json()),
      ]);
      renderUnmatchedBacks(backs, fronts, sellBtn);
      if (restoreScroll && saved.scrollY) window.scrollTo(0, saved.scrollY);
      return;
    }
    // "listed" is a separate dimension (is_listed), not a card status — fetch
    // all and filter client-side; otherwise filter by the real status.
    const url = "/api/cards" + (status && status !== "listed" ? `?status=${status}` : "");
    const resp = await fetch(url);
    let cards = await resp.json();
    if (status === "listed") cards = cards.filter((c) => c.is_listed);
    if (psaOnly.checked) cards = cards.filter((c) => c.psa10_candidate);
    if (anomOnly.checked) cards = cards.filter((c) => c.anomaly_flag);
    // Refresh the tag + sport dropdowns from the cards in view, keeping selection.
    populateTagOptions(tagFilter, cards, savedTag);
    populateSportOptions(sportFilter, cards, savedSport);
    if (tagFilter.value) cards = cards.filter((c) => (c.batch_tag || "") === tagFilter.value);
    if (sportFilter.value) cards = cards.filter((c) => (c.sport || "") === sportFilter.value);
    const q = (playerSearch.value || "").trim().toLowerCase();
    if (q) cards = cards.filter((c) => (c.player || "").toLowerCase().includes(q));
    const min = parseFloat(minPrice.value);
    const max = parseFloat(maxPrice.value);
    if (!isNaN(min)) cards = cards.filter((c) => c.estimated_price != null && c.estimated_price >= min);
    if (!isNaN(max)) cards = cards.filter((c) => c.estimated_price != null && c.estimated_price <= max);
    renderRepo(cards, sellBtn);
    if (restoreScroll && saved.scrollY) window.scrollTo(0, saved.scrollY);
  };

  // Dropdowns/checkboxes fire on change; text/number inputs filter live as typed.
  [filter, psaOnly, anomOnly, tagFilter, sportFilter].forEach((el) =>
    el.addEventListener("change", () => { saveRepoState(); load(); })
  );
  [playerSearch, minPrice, maxPrice].forEach((el) =>
    el.addEventListener("input", () => { saveRepoState(); load(); })
  );

  // Save scroll position as the user scrolls and right before leaving the page.
  window.addEventListener("scroll", () => saveRepoState(), { passive: true });
  window.addEventListener("pagehide", () => saveRepoState());

  sellBtn.addEventListener("click", async () => {
    const ids = Array.from(document.querySelectorAll(".sel:checked")).map((c) =>
      Number(c.value)
    );
    if (!ids.length) return;

    // For 2+ cards, ask whether to combine them into one lot listing. A single
    // card always lists individually (no point prompting).
    let mode = "individual";
    if (ids.length > 1) {
      mode = await askListMode(ids.length);
      if (!mode) return;  // cancelled
    }

    sellBtn.disabled = true;
    try {
      if (mode === "set") {
        await sellAsSet(ids);
      } else {
        await sellIndividually(ids);
      }
    } finally {
      sellBtn.disabled = false;
      load();
    }
  });

  const refreshBtn = document.getElementById("refreshPricesBtn");
  if (refreshBtn) refreshBtn.addEventListener("click", async () => {
    if (!confirm("Re-fetch fresh prices for every card? This ignores the cache and may take a while.")) return;
    const label = refreshBtn.textContent;
    refreshBtn.disabled = true;
    refreshBtn.textContent = "Refreshing…";
    try {
      const r = await fetch("/api/cards/reprice", { method: "POST" });
      const d = await r.json().catch(() => ({}));
      if (r.ok) toast(`Refreshed ${d.repriced ?? 0} card price(s).`);
      else toast("Refresh failed: " + (d.detail || r.status));
    } catch (e) { toast("Refresh failed: " + e); }
    finally { refreshBtn.disabled = false; refreshBtn.textContent = label; load(); }
  });

  load(true);
}

function renderUnmatchedBacks(backs, fronts, sellBtn) {
  if (sellBtn) { sellBtn.disabled = true; sellBtn.textContent = "Sell selected"; }
  const tbody = document.getElementById("rows");
  tbody.innerHTML = "";
  if (!backs.length) {
    tbody.innerHTML = `<tr><td colspan="19" class="muted" style="padding:20px">
      No unmatched backs — every back scan paired to a front. 🎉</td></tr>`;
    return;
  }
  const frontOpts = fronts.map((f) =>
    `<option value="${f.id}">#${f.id} · ${[f.year, f.set_brand, f.player].filter(Boolean).join(" ") || "card " + f.id}</option>`
  ).join("");
  backs.forEach((b) => {
    const ident = [b.year, b.set_brand, b.player, b.card_number ? "#" + b.card_number : ""]
      .filter(Boolean).join(" ") || "could not read identity";
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td colspan="3"><a href="/api/cards/${b.id}/crop?v=${b.upload_id||""}" target="_blank" rel="noopener">
        <img class="thumb" src="/api/cards/${b.id}/crop?v=${b.upload_id||""}" style="width:90px"/></a></td>
      <td colspan="6">back scan — read as: <strong>${ident}</strong></td>
      <td colspan="7">
        Attach to front:
        <select class="attachSel" data-back="${b.id}"><option value="">choose…</option>${frontOpts}</select>
        <button class="attachBtn" data-back="${b.id}">Attach</button>
      </td>
      <td colspan="3" class="actions"><button class="delOne linklike" data-id="${b.id}">Delete</button></td>`;
    tbody.appendChild(tr);
  });
  tbody.querySelectorAll(".attachBtn").forEach((btn) =>
    btn.addEventListener("click", async () => {
      const backId = btn.dataset.back;
      const sel = tbody.querySelector(`.attachSel[data-back="${backId}"]`);
      const frontId = sel && sel.value;
      if (!frontId) { toast("Pick a front to attach to first."); return; }
      btn.disabled = true;
      const r = await fetch(`/api/cards/${frontId}/attach-back/${backId}`, { method: "POST" });
      if (r.ok) { toast("Back attached to front."); document.getElementById("filter").dispatchEvent(new Event("change")); }
      else { btn.disabled = false; toast("Attach failed."); }
    })
  );
  tbody.querySelectorAll(".delOne").forEach((b) =>
    b.addEventListener("click", async () => {
      if (!confirm("Delete this back scan?")) return;
      await fetch(`/api/cards/${b.dataset.id}`, { method: "DELETE" });
      document.getElementById("filter").dispatchEvent(new Event("change"));
    })
  );
}

function renderRepo(cards, sellBtn) {
  const tbody = document.getElementById("rows");
  tbody.innerHTML = "";
  cards.forEach((c) => {
    // Listable = the user can pick it to sell: it has a real price and isn't
    // already listed. (Below-threshold / needs-review are selectable on purpose
    // so the user can choose to sell them; the backend still never auto-lists.)
    const sellable = c.estimated_price != null && !c.is_listed;
    const tr = document.createElement("tr");
    if (c.status === "below_threshold") tr.className = "dim";
    const listPrice = c.estimated_price ? c.estimated_price * APP_CONFIG.price_markup : null;
    const refImg = c.reference_image_url
      ? `<img class="thumb" src="${c.reference_image_url}" alt="market photo" onerror="this.replaceWith(document.createTextNode('—'))"/>`
      : "—";
    tr.innerHTML = `
      <td>${sellable ? `<input type="checkbox" class="sel" value="${c.id}"/>` : ""}</td>
      <td><a href="/card/${c.id}">${c.crop_path ? `<img class="thumb" src="/api/cards/${c.id}/crop?v=${c.upload_id||""}"/>` : "view"}</a>${c.has_back ? `<br/><span class="muted" title="Back image matched">⇄ has back</span>` : ""}</td>
      <td>${refImg}</td>
      <td>${c.batch_tag || "—"}</td>
      <td>${c.sport ? c.sport.charAt(0).toUpperCase() + c.sport.slice(1) : "—"}</td>
      <td>${c.player || "—"}</td>
      <td>${[c.year, c.set_brand].filter(Boolean).join(" ") || "—"}</td>
      <td>${c.card_number || "—"}</td>
      <td>${c.parallel || "—"}</td>
      <td>${c.condition || "—"}</td>
      <td>${confBadge(c.confidence)}</td>
      <td>${flagBadges(c)}</td>
      <td class="price">${money(c.sold_estimate)}</td>
      <td class="price">${money(c.sold_max_estimate)}</td>
      <td class="price">${money(c.active_estimate)}</td>
      <td class="price">${money(c.estimated_price)}${c.price_basis ? ` <span class="muted">(${c.price_basis})</span>` : ""}</td>
      <td>${money(c.graded_value_estimate)}</td>
      <td class="price">${money(listPrice)}</td>
      <td>${statusBadge(c)}</td>
      <td class="actions">
        <button class="delOne linklike" data-id="${c.id}">Delete</button>
      </td>`;
    tbody.appendChild(tr);
  });
  const selAll = document.getElementById("selAll");
  const update = () => {
    const boxes = [...tbody.querySelectorAll(".sel")];
    const checked = boxes.filter((b) => b.checked);
    sellBtn.disabled = checked.length === 0;
    sellBtn.textContent = checked.length ? `Sell selected (${checked.length})` : "Sell selected";
    if (selAll) {
      selAll.disabled = boxes.length === 0;
      selAll.checked = boxes.length > 0 && checked.length === boxes.length;
      selAll.indeterminate = checked.length > 0 && checked.length < boxes.length;
    }
  };
  // Header checkbox toggles every selectable row. onclick (not addEventListener)
  // so re-renders don't stack duplicate handlers.
  if (selAll) {
    selAll.onclick = () => {
      tbody.querySelectorAll(".sel").forEach((b) => { b.checked = selAll.checked; });
      update();
    };
  }
  tbody.querySelectorAll(".sel").forEach((c) => c.addEventListener("change", update));
  update();
  tbody.querySelectorAll(".delOne").forEach((b) =>
    b.addEventListener("click", async () => {
      if (!confirm("Delete this card from your repository? This can't be undone.")) return;
      b.disabled = true;
      const resp = await fetch(`/api/cards/${b.dataset.id}`, { method: "DELETE" });
      if (resp.ok || resp.status === 204) {
        b.closest("tr").remove();
        toast("Card deleted.");
        update();
      } else {
        b.disabled = false;
        const err = await resp.json().catch(() => ({}));
        toast("Could not delete: " + (err.detail || resp.status));
      }
    })
  );
  update();
}

// Card detail page -----------------------------------------------------------

async function initCardDetail() {
  await loadConfig();
  const id = Number(location.pathname.split("/").pop());
  const c = await (await fetch(`/api/cards/${id}`)).json();
  renderDetail(c);
}

function renderDetail(c) {
  const el = document.getElementById("detail");
  let ident = {};
  try { ident = JSON.parse(c.identification_json || "{}"); } catch (e) {}
  const fieldRows = Object.entries(ident.field_reads || {}).map(([k, v]) =>
    `<tr><td>${k}</td><td>${v.value ?? "—"}</td><td>${confBadge(v.confidence)}</td></tr>`
  ).join("");
  const verification = ident.verification
    ? `<p class="muted">Verification: ${ident.verification.agree ? "✅ agrees" : "⚠ disagrees"} ${
        ident.verification.notes ? "— " + ident.verification.notes : ""}</p>`
    : "";

  const comps = (c.comps || []);
  // Two pills per row: the PROVIDER the data came through (130point /
  // SportsCardsPro / eBay) and the ORIGINAL marketplace the sale happened on.
  const provenance = (x) => `<span class="badge amber">${esc(x.source || "?")}</span>${
    x.marketplace ? ` <span class="badge">${esc(x.marketplace)}</span>` : ""}`;
  const compRows = (type) => comps.filter((x) => x.match_type === type).map((x) => `
      <tr>
        <td>${provenance(x)}</td>
        <td>${x.thumbnail_url ? `<img class="thumb" src="${esc(x.thumbnail_url)}" onerror="this.replaceWith(document.createTextNode('—'))"/>` : "—"}</td>
        <td class="price">${x.sold_price != null ? money(x.sold_price) : "—"}</td>
        <td>${esc(x.sold_date) || "—"}</td>
        <td>${esc(x.condition_grade) || "—"}</td>
        <td>${esc(x.match_reason)}</td>
        <td>${x.listing_url ? `<a class="link" href="${esc(x.listing_url)}" target="_blank" rel="noopener">view sale</a>` : "—"}</td>
      </tr>`).join("");

  const compSection = (title, type) => {
    const rows = compRows(type);
    if (!rows) return "";
    return `<h3>${title}</h3><table><thead><tr><th>Provider / Marketplace</th><th>Photo</th>
      <th>Sold</th><th>Date</th><th>Cond/Grade</th><th>Why matched</th><th>Link</th>
      </tr></thead><tbody>${rows}</tbody></table>`;
  };

  // --- Full sales list: every completed sale, grouped by provider, newest first.
  const provider = (x) => (x.source || "unknown").replace(/\s*\(.*\)$/, "");  // strip "(sold)" etc.
  const isSold = (x) => x.sold_price != null && !/\(active\)/.test(x.source || "");
  const salesByProvider = {};
  comps.filter(isSold).forEach((x) => { (salesByProvider[provider(x)] ||= []).push(x); });
  const salesGroups = Object.entries(salesByProvider).map(([prov, list]) => {
    list.sort((a, b) => (b.sold_date || "").localeCompare(a.sold_date || ""));
    const rows = list.map((x) => `
      <tr>
        <td><span class="badge">${esc(x.marketplace || "—")}</span></td>
        <td class="price">${money(x.sold_price)}</td>
        <td>${esc(x.sold_date) || "—"}</td>
        <td>${esc(x.condition_grade) || "raw"}</td>
        <td>${esc(x.title) || "—"}</td>
        <td>${x.listing_url ? `<a class="link" href="${esc(x.listing_url)}" target="_blank" rel="noopener">view</a>` : "—"}</td>
      </tr>`).join("");
    return `<h3><span class="badge amber">${esc(prov)}</span> <span class="muted">(${list.length} sale${list.length === 1 ? "" : "s"})</span></h3>
      <table><thead><tr><th>Marketplace</th><th>Price</th><th>Date</th><th>Grade</th><th>Title</th><th>Link</th></tr></thead>
      <tbody>${rows}</tbody></table>`;
  }).join("");

  // Last sold price per marketplace (most recent / representative exact comp).
  const bySource = {};
  comps.filter((x) => x.match_type === "exact" && x.sold_price != null)
    .forEach((x) => {
      const s = x.source || "unknown";
      if (!bySource[s] || (x.sold_date || "") > (bySource[s].sold_date || "")) bySource[s] = x;
    });
  const sourceSummary = Object.entries(bySource).map(([s, x]) =>
    `<tr><td>${provenance(x)}</td><td class="price">${money(x.sold_price)}</td>
     <td>${esc(x.sold_date) || "—"}</td>
     <td>${x.listing_url ? `<a class="link" href="${esc(x.listing_url)}" target="_blank" rel="noopener">view</a>` : "—"}</td></tr>`
  ).join("");

  const refBlock = c.reference_image_url
    ? `<figure style="margin:0"><img class="zoomable" src="${c.reference_image_url}" data-full="${c.reference_image_url}" style="max-width:200px;border-radius:8px;cursor:zoom-in"
         onerror="this.closest('figure').replaceWith(document.createTextNode(''))"/>
       <figcaption class="muted">reference photo from marketplace listing</figcaption></figure>`
    : "";

  el.innerHTML = `
    <div class="card-box">
      <h2>${[c.year, c.set_brand, c.player].filter(Boolean).join(" ") || "Card #" + c.id}</h2>
      <div style="display:flex;gap:16px;flex-wrap:wrap;align-items:flex-start">
        <figure style="margin:0">${c.crop_path ? `<img class="zoomable" src="/api/cards/${c.id}/crop?v=${c.upload_id||""}" data-full="/api/cards/${c.id}/crop?v=${c.upload_id||""}" style="max-width:200px;border-radius:8px;cursor:zoom-in"/>` : ""}
          <figcaption class="muted">front</figcaption></figure>
        ${c.has_back ? `<figure style="margin:0"><img class="zoomable" src="/api/cards/${c.id}/back-crop?v=${c.upload_id||""}" data-full="/api/cards/${c.id}/back-crop?v=${c.upload_id||""}" style="max-width:200px;border-radius:8px;cursor:zoom-in"
          onerror="this.closest('figure').replaceWith(document.createTextNode(''))"/>
          <figcaption class="muted">back</figcaption></figure>` : ""}
        ${refBlock}
      </div>
      <p>${flagBadges(c)} ${statusBadge(c)}</p>
      <p class="price">Last sold: ${money(c.sold_estimate)} · Current asking: ${money(c.active_estimate)}</p>
      <p class="price">Estimate: ${money(c.estimated_price)}${c.price_basis ? ` (${c.price_basis})` : ""} · List ×${APP_CONFIG.price_markup}: ${money(c.estimated_price ? c.estimated_price * APP_CONFIG.price_markup : null)}
         ${c.graded_value_estimate ? "· Graded (PSA10) est: " + money(c.graded_value_estimate) : ""}</p>
      <p class="muted">${c.derivation || "No price derivation."} ${c.excluded_count ? `(${c.excluded_count} non-matching sales excluded)` : ""}</p>
      ${c.status === "priced" ? `<button id="listBtn">List on eBay${APP_CONFIG.ebay_mode === "preview" ? " (preview)" : ""}</button>` : ""}
      <button id="markBackBtn" class="secondary" title="If the AI mislabeled this card back as a front, reclassify it">This is a card back</button>
    </div>
    ${sourceSummary ? `<div class="card-box"><h3>Recent prices by source</h3>
      <p class="muted">Each row shows the <strong>provider</strong> (amber) and the original <strong>marketplace</strong> the sale came from. "(sold)" are completed sales; "(active)" are current asking prices.</p>
      <table><thead><tr><th>Provider / Marketplace</th><th>Price</th><th>Date</th><th>Link</th></tr></thead>
      <tbody>${sourceSummary}</tbody></table></div>` : ""}
    ${salesGroups ? `<div class="card-box"><h3>All sales</h3>
      <p class="muted">Every completed sale we found, grouped by provider. The amber pill is who we got the data through (e.g. SportsCardsPro, 130point); the plain pill is the original marketplace it sold on (e.g. eBay, PWCC).</p>
      ${salesGroups}</div>` : ""}
    <div class="card-box">
      <h3>Identification audit</h3>
      ${c.grading_notes ? `<p class="muted"><strong>Grading:</strong> ${c.grading_notes}</p>` : ""}
      ${c.anomaly_notes ? `<p class="muted"><strong>Anomaly:</strong> ${c.anomaly_notes}</p>` : ""}
      ${ident.raw_text ? `<p class="muted"><strong>Text read:</strong> ${ident.raw_text}</p>` : ""}
      ${verification}
      ${fieldRows ? `<table><thead><tr><th>Field</th><th>Read</th><th>Conf</th></tr></thead><tbody>${fieldRows}</tbody></table>` : ""}
    </div>
    <div class="card-box">
      <h3>Sold comps used</h3>
      ${comps.length ? "" : '<p class="muted">No comps recorded.</p>'}
      ${compSection("Exact matches", "exact")}
      ${compSection("Near matches", "near")}
      ${compSection("Graded comps", "graded")}
    </div>`;

  const listBtn = document.getElementById("listBtn");
  if (listBtn) {
    listBtn.addEventListener("click", async () => {
      listBtn.disabled = true;
      listBtn.textContent = "Listing…";
      const r = await listOnEbay(c.id);
      announceListResult(r);
      initCardDetail();  // refresh
    });
  }

  const markBackBtn = document.getElementById("markBackBtn");
  if (markBackBtn) {
    markBackBtn.addEventListener("click", async () => {
      if (!confirm("Reclassify this as a card BACK? It leaves your collection and pairs to its matching front (or moves to 'Unmatched backs').")) return;
      markBackBtn.disabled = true;
      const r = await fetch(`/api/cards/${c.id}/mark-back`, { method: "POST" });
      if (!r.ok) { markBackBtn.disabled = false; toast("Couldn't reclassify."); return; }
      const data = await r.json();
      if (data.merged_into) {
        toast("Matched to its front. Opening that card…");
        window.location.href = `/card/${data.merged_into}`;
      } else {
        toast("Marked as back — find it under My Collection → Show → Unmatched backs.");
        window.location.href = "/repository";
      }
    });
  }
}
