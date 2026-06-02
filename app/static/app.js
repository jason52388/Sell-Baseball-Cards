// Shared helpers ------------------------------------------------------------

function toast(msg) {
  const t = document.getElementById("toast");
  if (!t) return;
  t.textContent = msg;
  t.classList.add("show");
  setTimeout(() => t.classList.remove("show"), 3000);
}

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

// Upload page ----------------------------------------------------------------

function initUpload() {
  const dz = document.getElementById("dropzone");
  const input = document.getElementById("fileInput");
  const btn = document.getElementById("uploadBtn");
  const countEl = document.getElementById("fileCount");
  let files = [];

  const setFiles = (list) => {
    files = Array.from(list);
    countEl.textContent = files.length ? `${files.length} file(s) selected` : "";
    btn.disabled = files.length === 0;
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

  wireManualAdd();

  btn.addEventListener("click", async () => {
    if (!files.length) return;
    btn.disabled = true;
    const status = document.getElementById("status");
    status.textContent = `Analyzing ${files.length} photo(s)… this can take a moment.`;
    const fd = new FormData();
    files.forEach((f) => fd.append("files", f));
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
function renderPreviewCard(c) {
  const el = document.createElement("div");
  el.className = "preview-card";
  el.dataset.id = c.id;
  const lowConf = (c.confidence ?? 0) < 0.7;
  const yourPhoto = c.crop_path
    ? `<figure class="pv-fig"><img class="thumb" src="/api/cards/${c.id}/crop"/><figcaption class="muted">your photo</figcaption></figure>`
    : "";
  const refPhoto = c.reference_image_url
    ? `<figure class="pv-fig"><img class="thumb" src="${c.reference_image_url}" alt="market photo" onerror="this.closest('figure').replaceWith(document.createTextNode(''))"/><figcaption class="muted">marketplace match</figcaption></figure>`
    : `<figure class="pv-fig"><span class="muted">no marketplace photo</span></figure>`;
  const lowHint = lowConf
    ? `<p class="muted">⚠ Low confidence — compare the photos, <button class="linklike reanalyzeBtn">re-analyze with Gemini Pro</button>, or <a href="#addManualBtn" onclick="document.getElementById('m_player').focus()">enter it manually below</a>.</p>`
    : "";
  el.innerHTML = `
    <div class="pv-photos">${yourPhoto}${refPhoto}</div>
    <div class="pv-id">
      <strong>${c.player || "—"}</strong> ${confBadge(c.confidence)} ${flagBadges(c)}<br/>
      <span class="muted">${[c.year, c.set_brand, c.card_number ? "#" + c.card_number : "", c.parallel].filter(Boolean).join(" ") || "—"}</span><br/>
      <span class="price">Est. ${money(c.estimated_price)}${c.price_basis ? ` (${c.price_basis})` : ""}</span>
    </div>
    ${lowHint}
    <div class="pv-actions">
      <button class="addBtn">Add to repository</button>
      <button class="reanalyzeBtn">Re-analyze (Gemini Pro)</button>
      <a class="link" href="/card/${c.id}" target="_blank" rel="noopener">View details</a>
      <button class="discardBtn linklike">Discard</button>
    </div>
    <div class="pv-msg muted"></div>`;

  const msg = el.querySelector(".pv-msg");
  el.querySelector(".addBtn").addEventListener("click", () => promoteCards([c.id]));
  el.querySelectorAll(".reanalyzeBtn").forEach((b) =>
    b.addEventListener("click", async () => {
      msg.textContent = "Re-analyzing with Gemini Pro…";
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

function statusBadge(c) {
  const map = {
    priced: ["green", "priced"],
    needs_review: ["amber", "needs review"],
    below_threshold: ["red", "< $4"],
    listed: ["green", "listed"],
    list_failed: ["red", "list failed"],
  };
  const [cls, label] = map[c.status] || ["amber", c.status];
  const reason = c.review_reason ? ` <span class="muted">(${c.review_reason})</span>` : "";
  return `<span class="badge ${cls}">${label}</span>${reason}`;
}

// Repository page ------------------------------------------------------------

const REPO_STATE_KEY = "repoState";

// Persist the filter selections and scroll position so returning to the
// repository (e.g. after viewing a card) leaves you where you left off.
function saveRepoState(filter, psaOnly, anomOnly) {
  try {
    sessionStorage.setItem(REPO_STATE_KEY, JSON.stringify({
      filter: filter.value,
      psaOnly: psaOnly.checked,
      anomOnly: anomOnly.checked,
      scrollY: window.scrollY,
    }));
  } catch (e) {}
}

function readRepoState() {
  try { return JSON.parse(sessionStorage.getItem(REPO_STATE_KEY) || "{}"); }
  catch (e) { return {}; }
}

async function initRepository() {
  const filter = document.getElementById("filter");
  const psaOnly = document.getElementById("psaOnly");
  const anomOnly = document.getElementById("anomOnly");
  const sellBtn = document.getElementById("sellBtn");

  // Restore previously chosen filters before the first load.
  const saved = readRepoState();
  if (saved.filter != null) filter.value = saved.filter;
  if (saved.psaOnly != null) psaOnly.checked = saved.psaOnly;
  if (saved.anomOnly != null) anomOnly.checked = saved.anomOnly;

  await loadConfig();
  const banner = document.getElementById("modeBanner");
  if (banner) {
    banner.innerHTML = APP_CONFIG.ebay_mode === "preview"
      ? '<span class="badge amber">PREVIEW MODE</span> Listing buttons build the real eBay listing but publish nothing. Set EBAY_MODE=sandbox/live with credentials to list for real.'
      : `<span class="badge green">${(APP_CONFIG.ebay_mode || "").toUpperCase()} MODE</span> Listing buttons publish to eBay.`;
  }

  const load = async (restoreScroll = false) => {
    const status = filter.value;
    const url = "/api/cards" + (status ? `?status=${status}` : "");
    const resp = await fetch(url);
    let cards = await resp.json();
    if (psaOnly.checked) cards = cards.filter((c) => c.psa10_candidate);
    if (anomOnly.checked) cards = cards.filter((c) => c.anomaly_flag);
    renderRepo(cards, sellBtn);
    if (restoreScroll && saved.scrollY) window.scrollTo(0, saved.scrollY);
  };

  [filter, psaOnly, anomOnly].forEach((el) =>
    el.addEventListener("change", () => {
      saveRepoState(filter, psaOnly, anomOnly);
      load();
    })
  );

  // Save scroll position as the user scrolls and right before leaving the page.
  window.addEventListener("scroll", () => saveRepoState(filter, psaOnly, anomOnly), { passive: true });
  window.addEventListener("pagehide", () => saveRepoState(filter, psaOnly, anomOnly));

  sellBtn.addEventListener("click", async () => {
    const ids = Array.from(document.querySelectorAll(".sel:checked")).map((c) =>
      Number(c.value)
    );
    if (!ids.length) return;
    sellBtn.disabled = true;
    const resp = await fetch("/api/listings/sell", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ card_ids: ids }),
    });
    const data = await resp.json();
    const ok = data.results.filter((r) => r.status === "published").length;
    toast(`${ok}/${data.results.length} listed.`);
    load();
  });

  load(true);
}

function renderRepo(cards, sellBtn) {
  const tbody = document.getElementById("rows");
  tbody.innerHTML = "";
  cards.forEach((c) => {
    const sellable = c.status === "priced";
    const tr = document.createElement("tr");
    if (c.status === "below_threshold") tr.className = "dim";
    const listPrice = c.estimated_price ? c.estimated_price * APP_CONFIG.price_markup : null;
    const refImg = c.reference_image_url
      ? `<img class="thumb" src="${c.reference_image_url}" alt="market photo" onerror="this.replaceWith(document.createTextNode('—'))"/>`
      : "—";
    tr.innerHTML = `
      <td>${sellable ? `<input type="checkbox" class="sel" value="${c.id}"/>` : ""}</td>
      <td><a href="/card/${c.id}">${c.crop_path ? `<img class="thumb" src="/api/cards/${c.id}/crop"/>` : "view"}</a></td>
      <td>${refImg}</td>
      <td>${c.player || "—"}</td>
      <td>${[c.year, c.set_brand].filter(Boolean).join(" ") || "—"}</td>
      <td>${c.card_number || "—"}</td>
      <td>${c.parallel || "—"}</td>
      <td>${c.condition || "—"}</td>
      <td>${confBadge(c.confidence)}</td>
      <td>${flagBadges(c)}</td>
      <td class="price">${money(c.sold_estimate)}</td>
      <td class="price">${money(c.active_estimate)}</td>
      <td class="price">${money(c.estimated_price)}${c.price_basis ? ` <span class="muted">(${c.price_basis})</span>` : ""}</td>
      <td>${money(c.graded_value_estimate)}</td>
      <td class="price">${money(listPrice)}</td>
      <td>${statusBadge(c)}</td>
      <td>${sellable ? `<button class="listOne" data-id="${c.id}">List on eBay</button>` : ""}</td>`;
    tbody.appendChild(tr);
  });
  const update = () => {
    sellBtn.disabled = document.querySelectorAll(".sel:checked").length === 0;
  };
  tbody.querySelectorAll(".sel").forEach((c) => c.addEventListener("change", update));
  tbody.querySelectorAll(".listOne").forEach((b) =>
    b.addEventListener("click", async () => {
      b.disabled = true;
      b.textContent = "Listing…";
      const r = await listOnEbay(Number(b.dataset.id));
      announceListResult(r);
      // Reload so a published card moves out of the priced view.
      document.getElementById("filter").dispatchEvent(new Event("change"));
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
  const compRows = (type) => comps.filter((x) => x.match_type === type).map((x) => `
      <tr>
        <td><span class="badge amber">${x.source || "?"}</span></td>
        <td>${x.thumbnail_url ? `<img class="thumb" src="${x.thumbnail_url}" onerror="this.replaceWith(document.createTextNode('—'))"/>` : "—"}</td>
        <td class="price">${x.sold_price != null ? money(x.sold_price) : "—"}</td>
        <td>${x.sold_date || "—"}</td>
        <td>${x.condition_grade || "—"}</td>
        <td>${x.match_reason || ""}</td>
        <td>${x.listing_url ? `<a class="link" href="${x.listing_url}" target="_blank" rel="noopener">view sale</a>` : "—"}</td>
      </tr>`).join("");

  const compSection = (title, type) => {
    const rows = compRows(type);
    if (!rows) return "";
    return `<h3>${title}</h3><table><thead><tr><th>Source</th><th>Photo</th>
      <th>Sold</th><th>Date</th><th>Cond/Grade</th><th>Why matched</th><th>Link</th>
      </tr></thead><tbody>${rows}</tbody></table>`;
  };

  // Last sold price per marketplace (most recent / representative exact comp).
  const bySource = {};
  comps.filter((x) => x.match_type === "exact" && x.sold_price != null)
    .forEach((x) => {
      const s = x.source || "unknown";
      if (!bySource[s] || (x.sold_date || "") > (bySource[s].sold_date || "")) bySource[s] = x;
    });
  const sourceSummary = Object.entries(bySource).map(([s, x]) =>
    `<tr><td><span class="badge amber">${s}</span></td><td class="price">${money(x.sold_price)}</td>
     <td>${x.sold_date || "—"}</td>
     <td>${x.listing_url ? `<a class="link" href="${x.listing_url}" target="_blank" rel="noopener">view</a>` : "—"}</td></tr>`
  ).join("");

  const refBlock = c.reference_image_url
    ? `<figure style="margin:0"><img src="${c.reference_image_url}" style="max-width:200px;border-radius:8px"
         onerror="this.closest('figure').replaceWith(document.createTextNode(''))"/>
       <figcaption class="muted">reference photo from marketplace listing</figcaption></figure>`
    : "";

  el.innerHTML = `
    <div class="card-box">
      <h2>${[c.year, c.set_brand, c.player].filter(Boolean).join(" ") || "Card #" + c.id}</h2>
      <div style="display:flex;gap:16px;flex-wrap:wrap;align-items:flex-start">
        <figure style="margin:0">${c.crop_path ? `<img src="/api/cards/${c.id}/crop" style="max-width:200px;border-radius:8px"/>` : ""}
          <figcaption class="muted">your photo</figcaption></figure>
        ${refBlock}
      </div>
      <p>${flagBadges(c)} ${statusBadge(c)}</p>
      <p class="price">Last sold: ${money(c.sold_estimate)} · Current asking: ${money(c.active_estimate)}</p>
      <p class="price">Estimate: ${money(c.estimated_price)}${c.price_basis ? ` (${c.price_basis})` : ""} · List ×${APP_CONFIG.price_markup}: ${money(c.estimated_price ? c.estimated_price * APP_CONFIG.price_markup : null)}
         ${c.graded_value_estimate ? "· Graded (PSA10) est: " + money(c.graded_value_estimate) : ""}</p>
      <p class="muted">${c.derivation || "No price derivation."} ${c.excluded_count ? `(${c.excluded_count} non-matching sales excluded)` : ""}</p>
      ${c.status === "priced" ? `<button id="listBtn">List on eBay${APP_CONFIG.ebay_mode === "preview" ? " (preview)" : ""}</button>` : ""}
    </div>
    ${sourceSummary ? `<div class="card-box"><h3>Recent prices by source</h3>
      <p class="muted">Sources tagged "(sold)" are completed sales; "(active)" are current asking prices.</p>
      <table><thead><tr><th>Source</th><th>Price</th><th>Date</th><th>Link</th></tr></thead>
      <tbody>${sourceSummary}</tbody></table></div>` : ""}
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
}
