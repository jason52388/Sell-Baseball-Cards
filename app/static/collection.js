// Collection page (/repository): KPIs, filter chips, table or grid of cards,
// selection bar (list, lot, refresh prices), row menus, special views for
// duplicates, unmatched backs and deleted cards.

const STATE_KEY = "collectionState";
const VIEW_KEY = "collectionView";

const S = {
  cards: [],          // library fronts
  special: null,      // {kind, data} for duplicates / unmatched / deleted
  selected: new Set(),
  view: "list",
  firstRender: true,
  saved: {},
};

const CHIPS = [
  ["all", "All"],
  ["needs_review", "Needs review"],
  ["ready", "Ready to list"],
  ["under", "Under $4"],
  ["listed", "Listed"],
  ["sold", "Sold"],
  ["duplicates", "Duplicates"],
  ["unmatched", "Unmatched backs"],
  ["deleted", "Deleted"],
];

const CHIP_TEST = {
  all: () => true,
  needs_review: (c) => c.status === "needs_review",
  ready: (c) => c.status === "priced" && (c.listing_state === "none" || c.listing_state === "ended"),
  under: (c) => c.status === "below_threshold",
  listed: (c) => c.listing_state === "live",
  sold: (c) => c.listing_state === "sold",
};

const $ = (id) => document.getElementById(id);

function readState() {
  try { return JSON.parse(sessionStorage.getItem(STATE_KEY) || "{}"); } catch (e) { return {}; }
}

function saveState() {
  try {
    sessionStorage.setItem(STATE_KEY, JSON.stringify({
      chip: S.chip,
      sport: $("sportFilter").value,
      tag: $("tagFilter").value,
      q: $("search") ? $("search").value : "",
      sort: $("sort").value,
      min: $("minPrice").value,
      max: $("maxPrice").value,
      psa: $("psaOnly").checked,
      anom: $("anomOnly").checked,
      noPrice: $("noPriceOnly").checked,
      scrollY: window.scrollY,
    }));
  } catch (e) { /* storage may be blocked */ }
}

function loadViewPref() {
  try { return localStorage.getItem(VIEW_KEY) === "grid" ? "grid" : "list"; } catch (e) { return "list"; }
}

function saveViewPref(v) {
  try { localStorage.setItem(VIEW_KEY, v); } catch (e) { /* storage may be blocked */ }
}

const narrow = window.matchMedia("(max-width: 860px)");
function effectiveView() {
  return narrow.matches ? "grid" : S.view;
}

// --- KPIs and chips -------------------------------------------------------------

function renderKpis() {
  const s = APP.stats;
  if (!s) return;
  const kpi = (v, l, cls = "", href = "") => href
    ? `<a class="kpi ${cls}" href="${href}"><div class="v">${v}</div><div class="l">${l}</div></a>`
    : `<div class="kpi ${cls}"><div class="v">${v}</div><div class="l">${l}</div></div>`;
  $("kpis").innerHTML =
    kpi(money(s.value_from_sold), `Value from real sales (${plural(s.value_from_sold_count, "card")})`) +
    kpi(money(s.value_from_asking), `Value from asking prices (${plural(s.value_from_asking_count, "card")})`, "amber") +
    kpi(String(s.needs_review_count), s.needs_review_count ? "Need review, click to start" : "Need review", "warn click", "/review") +
    kpi(String(s.live_count), `Live on eBay (${money(s.live_value)})`) +
    kpi(String(s.sold_this_month_count), `Sold this month${s.sold_this_month_total ? ` (${money(s.sold_this_month_total)})` : ""}`);
}

function chipCount(key) {
  const s = APP.stats || {};
  if (key === "duplicates") return s.duplicates_count;
  if (key === "unmatched") return s.unmatched_backs_count;
  if (key === "deleted") return s.deleted_count;
  return S.cards.filter(CHIP_TEST[key]).length;
}

function renderChips() {
  const floor = APP.config.min_store_value != null ? APP.config.min_store_value : 4;
  const q = $("search") ? $("search").value : (S.saved.q || "");
  $("chips").innerHTML = CHIPS.map(([key, label]) => {
    const n = chipCount(key);
    const text = key === "under" ? `Under $${floor}` : label;
    return `<button type="button" class="chip${S.chip === key ? " on" : ""}" data-chip="${key}">${esc(text)}<span class="n">${n == null ? "" : n}</span></button>`;
  }).join("") + `<input class="search" type="search" id="search" placeholder="Search player, set, tag" value="${esc(q)}" aria-label="Search"/>`;
  $("chips").querySelectorAll("[data-chip]").forEach((b) => b.addEventListener("click", () => setChip(b.dataset.chip)));
  $("search").addEventListener("input", () => { saveState(); render(); });
}

async function setChip(key) {
  S.chip = key;
  S.selected.clear();
  saveState();
  renderChips();
  await loadSpecial();
  render();
}

// --- data -------------------------------------------------------------------------

async function loadCards() {
  const r = await api("/api/cards");
  S.cards = r.ok ? r.data : [];
}

async function loadSpecial() {
  S.special = null;
  if (S.chip === "duplicates") {
    const r = await api("/api/cards/duplicates");
    S.special = { kind: "duplicates", data: r.ok ? r.data.groups || [] : [] };
  } else if (S.chip === "unmatched") {
    const r = await api("/api/cards?status=unmatched_backs");
    S.special = { kind: "unmatched", data: r.ok ? r.data : [] };
  } else if (S.chip === "deleted") {
    const r = await api("/api/cards?status=deleted");
    S.special = { kind: "deleted", data: r.ok ? r.data : [] };
  }
}

async function reloadAll() {
  await Promise.all([loadCards(), loadStats(), loadSpecial()]);
  // Drop selected ids that are gone.
  const ids = new Set(S.cards.map((c) => c.id));
  [...S.selected].forEach((id) => { if (!ids.has(id)) S.selected.delete(id); });
  renderKpis();
  renderReviewCount();
  renderChips();
  fillDropdowns();
  render();
}

function fillDropdowns() {
  const fill = (sel, values, allLabel, labelFn, want) => {
    const cur = sel.value || want || "";
    sel.innerHTML = `<option value="">${allLabel}</option>` +
      values.map((v) => `<option value="${esc(v)}">${esc(labelFn(v))}</option>`).join("");
    sel.value = values.includes(cur) ? cur : "";
  };
  fill($("sportFilter"), [...new Set(S.cards.map((c) => c.sport).filter(Boolean))].sort(), "All sports", cap, S.saved.sport);
  fill($("tagFilter"), [...new Set(S.cards.map((c) => c.batch_tag).filter(Boolean))].sort(), "All tags", (t) => t, S.saved.tag);
}

// The cards in view after the chip, dropdowns, search and More filters.
function filtered(list) {
  const sport = $("sportFilter").value;
  const tag = $("tagFilter").value;
  const q = ($("search") ? $("search").value : "").trim().toLowerCase();
  const min = parseFloat($("minPrice").value);
  const max = parseFloat($("maxPrice").value);
  let out = list;
  if (CHIP_TEST[S.chip]) out = out.filter(CHIP_TEST[S.chip]);
  if (sport) out = out.filter((c) => c.sport === sport);
  if (tag) out = out.filter((c) => c.batch_tag === tag);
  if (q) {
    out = out.filter((c) => [c.player, c.set_brand, c.year, c.card_number, c.batch_tag, c.team, c.subset, c.parallel]
      .filter(Boolean).join(" ").toLowerCase().includes(q));
  }
  if (!isNaN(min)) out = out.filter((c) => c.estimated_price != null && c.estimated_price >= min);
  if (!isNaN(max)) out = out.filter((c) => c.estimated_price != null && c.estimated_price <= max);
  if ($("psaOnly").checked) out = out.filter((c) => c.psa10_candidate);
  if ($("anomOnly").checked) out = out.filter((c) => c.anomaly_flag);
  if ($("noPriceOnly").checked) out = out.filter((c) => c.estimated_price == null);
  return sortCards(out);
}

function sortCards(list) {
  const v = (c) => (c.estimated_price == null ? -1 : c.estimated_price);
  const by = {
    value_desc: (a, b) => v(b) - v(a),
    value_asc: (a, b) => (a.estimated_price == null) - (b.estimated_price == null) || v(a) - v(b),
    list_desc: (a, b) => (b.suggested_list_price || 0) - (a.suggested_list_price || 0),
    newest: (a, b) => b.id - a.id,
    oldest: (a, b) => a.id - b.id,
    player: (a, b) => (a.player || "~").localeCompare(b.player || "~"),
  }[$("sort").value] || ((a, b) => v(b) - v(a));
  return [...list].sort(by);
}

function updateMoreLabel() {
  const n = [$("minPrice").value, $("maxPrice").value].filter(Boolean).length
    + [$("psaOnly"), $("anomOnly"), $("noPriceOnly")].filter((x) => x.checked).length;
  $("moreBtn").textContent = n ? `More filters (${n})` : "More filters";
}

// --- rendering ----------------------------------------------------------------------

function render() {
  updateMoreLabel();
  document.querySelectorAll("[data-view]").forEach((b) => b.classList.toggle("on", b.dataset.view === S.view));
  const view = $("view");
  if (S.special && S.special.kind === "unmatched") renderUnmatched(view, S.special.data);
  else if (S.special && S.special.kind === "deleted") renderDeleted(view, S.special.data);
  else if (S.special && S.special.kind === "duplicates") renderDuplicates(view, S.special.data);
  else renderCards(view, filtered(S.cards));
  renderSelbar();
  if (S.firstRender) {
    S.firstRender = false;
    if (S.saved.scrollY) window.scrollTo(0, S.saved.scrollY);
  }
}

function emptyText() {
  if (S.chip === "needs_review") return "Nothing needs review. Nice.";
  if (S.chip === "listed") return "Nothing is live on eBay right now.";
  if (S.chip === "sold") return "No sales yet. Use Check for sales after something sells.";
  if (!S.cards.length) return `No cards yet. <a href="/">Upload some photos</a> to get started.`;
  return "No cards match these filters.";
}

function rowHtml(c) {
  return `<tr data-id="${c.id}">
    <td><input type="checkbox" class="sel" data-id="${c.id}" ${S.selected.has(c.id) ? "checked" : ""} aria-label="Select"/></td>
    <td>${photosHtml(c)}</td>
    <td class="who"><b>${esc(cardTitle(c))}</b><div class="s">${esc(cardLine(c)) || "&nbsp;"}</div>${tagsHtml(c)}</td>
    <td>${esc(cap(c.condition || "")) || '<span class="muted">-</span>'}</td>
    <td>${valueHtml(c)}</td>
    <td>${listAtHtml(c)}</td>
    <td>${statusHtml(c)}</td>
    <td><button class="more" type="button" data-menu="${c.id}" aria-label="More actions">⋯</button></td>
  </tr>`;
}

function tileHtml(c) {
  const f = cropUrl(c), b = backUrl(c);
  const zoom = [f, b].filter(Boolean).join("|");
  return `<div class="tile click" data-id="${c.id}">
    <input type="checkbox" class="sel tick" data-id="${c.id}" ${S.selected.has(c.id) ? "checked" : ""} aria-label="Select"/>
    <div class="pic">${f ? `<img src="${f}" alt="front" loading="lazy" data-zoom="${zoom}"/>` : "no photo"}</div>
    <div class="t">${esc(cardTitle(c))}</div>
    <div class="s">${esc(cardLine(c))}</div>
    <div>${tagsHtml(c)}</div>
    <div style="display:flex;justify-content:space-between;align-items:flex-end;margin-top:6px;gap:6px">
      <div>${valueHtml(c)}</div>
      <button class="more" type="button" data-menu="${c.id}" aria-label="More actions">⋯</button>
    </div>
    <div style="margin-top:6px">${statusHtml(c)}</div>
  </div>`;
}

const TABLE_HEAD = `<thead><tr>
  <th style="width:28px"><input type="checkbox" id="selAll" aria-label="Select all"/></th>
  <th style="width:100px">Photos</th><th>Card</th><th>Condition</th><th>Value</th><th>List at</th><th>Status</th><th style="width:40px"></th>
</tr></thead>`;

function renderCards(view, list, groupsHtml) {
  S.visible = list;
  if (effectiveView() === "grid") {
    view.innerHTML = list.length
      ? `<div class="tiles">${list.map(tileHtml).join("")}</div>`
      : `<div class="panel empty-state muted">${emptyText()}</div>`;
  } else {
    view.innerHTML = `<table class="cards">${TABLE_HEAD}<tbody>${
      list.length ? list.map(rowHtml).join("") : `<tr class="empty"><td colspan="8">${emptyText()}</td></tr>`
    }</tbody></table>`;
  }
  wireCards(view);
}

function wireCards(view) {
  const byId = new Map([...S.cards, ...(S.visible || [])].map((c) => [c.id, c]));
  view.querySelectorAll("[data-id]").forEach((row) => {
    if (!row.matches("tr, .tile")) return;
    row.addEventListener("click", (e) => {
      if (e.target.closest("button, a, input, label, [data-zoom]")) return;
      window.location.href = `/card/${row.dataset.id}`;
    });
  });
  view.querySelectorAll("input.sel").forEach((cb) => cb.addEventListener("change", () => {
    const id = Number(cb.dataset.id);
    if (cb.checked) S.selected.add(id); else S.selected.delete(id);
    renderSelbar();
  }));
  view.querySelectorAll("[data-menu]").forEach((b) => b.addEventListener("click", (e) => {
    e.stopPropagation();
    const c = byId.get(Number(b.dataset.menu));
    if (c) openMenu(b, rowMenu(c));
  }));
  const all = $("selAll");
  if (all) {
    all.addEventListener("change", () => {
      (S.visible || []).forEach((c) => { if (all.checked) S.selected.add(c.id); else S.selected.delete(c.id); });
      view.querySelectorAll("input.sel").forEach((cb) => { cb.checked = all.checked; });
      renderSelbar();
    });
  }
}

function rowMenu(c) {
  const live = c.listing_state === "live";
  return [
    { label: "Open", onClick: () => { window.location.href = `/card/${c.id}`; } },
    { label: "Edit", onClick: () => openEditor(c.id, reloadAll) },
    !live && c.listing_state !== "sold" && { label: "Re-analyze", onClick: () => reanalyzeCard(c, reloadAll) },
    c.has_back && { label: "Unmatch back", onClick: () => unmatchBack(c, reloadAll) },
    live && "sep",
    live && { label: "Change price", onClick: () => changeListingPrice(c, reloadAll) },
    live && { label: "End listing", onClick: () => endListing(c, reloadAll) },
    "sep",
    { label: "Delete", danger: true, onClick: () => deleteCard(c, reloadAll) },
  ];
}

function renderSelbar() {
  const n = S.selected.size;
  $("selbar").classList.toggle("hidden", n === 0);
  $("selCount").textContent = `${n} selected`;
  const all = $("selAll");
  if (all && S.visible) {
    const vis = S.visible.filter((c) => S.selected.has(c.id)).length;
    all.checked = vis > 0 && vis === S.visible.length;
    all.indeterminate = vis > 0 && vis < S.visible.length;
  }
}

function selectedCards() {
  const byId = new Map(S.cards.map((c) => [c.id, c]));
  return [...S.selected].map((id) => byId.get(id)).filter(Boolean);
}

// Duplicates: a heading per group, then its cards.
function renderDuplicates(view, groups) {
  if (!groups.length) {
    view.innerHTML = `<div class="panel empty-state muted">No duplicates. Every card looks unique.</div>`;
    return;
  }
  const cards = groups.flatMap((g) => g.cards);
  S.visible = cards;
  const head = (g) => `${g.tier === "certain" ? "Same card" : "Maybe the same card"}: ${esc(g.label)} · ${g.cards.length} copies${g.reason ? " · " + esc(noLongDash(g.reason)) : ""}`;
  const intro = `<p class="muted">${plural(groups.length, "group")}, ${plural(cards.length, "card")}. Deleting one copy keeps the rest.</p>`;
  if (effectiveView() === "grid") {
    view.innerHTML = intro + groups.map((g) => `<h3 style="margin:14px 0 8px"><span class="st ${g.tier === "certain" ? "bad" : "rev"}">${g.tier === "certain" ? "Same" : "Maybe"}</span> ${head(g)}</h3>
      <div class="tiles">${g.cards.map(tileHtml).join("")}</div>`).join("");
  } else {
    view.innerHTML = intro + `<table class="cards">${TABLE_HEAD}<tbody>${groups.map((g) =>
      `<tr class="group"><td colspan="8"><span class="st ${g.tier === "certain" ? "bad" : "rev"}">${g.tier === "certain" ? "Same" : "Maybe"}</span> <b>${head(g)}</b></td></tr>${g.cards.map(rowHtml).join("")}`
    ).join("")}</tbody></table>`;
  }
  wireCards(view);
}

// Unmatched backs: attach each to a front, or delete it.
function renderUnmatched(view, backs) {
  S.visible = [];
  if (!backs.length) {
    view.innerHTML = `<div class="panel empty-state muted">No unmatched backs. Every back found its front.</div>`;
    return;
  }
  const fronts = [...S.cards].sort((a, b) => (a.player || "").localeCompare(b.player || ""));
  const opts = fronts.map((f) => `<option value="${f.id}">${esc([f.player, f.year, f.set_brand, f.card_number ? "#" + f.card_number : ""].filter(Boolean).join(" ") || "Unnamed card")}</option>`).join("");
  view.innerHTML = `<p class="muted">Backs that did not pair with a front. Pick the card each one belongs to.</p>
    <div class="tiles">${backs.map((b) => {
      const url = `/api/cards/${b.id}/crop?v=${b.upload_id || ""}`;
      return `<div class="tile dashed" data-back="${b.id}">
        <div class="pic"><img src="${url}" alt="back" data-zoom="${url}"/></div>
        <div class="t">Back: ${esc(b.player || "could not read")}</div>
        <div class="s">${esc(cardLine(b))}</div>
        <select data-attach="${b.id}" style="margin-top:8px;width:100%"><option value="">Attach to...</option>${opts}</select>
        <div class="acts"><button class="btn" type="button" data-do-attach="${b.id}">Attach</button>
          <button class="btn danger" type="button" data-del-back="${b.id}">Delete</button></div>
      </div>`;
    }).join("")}</div>`;
  view.querySelectorAll("[data-do-attach]").forEach((btn) => btn.addEventListener("click", async () => {
    const backId = btn.dataset.doAttach;
    const frontId = view.querySelector(`[data-attach="${backId}"]`).value;
    if (!frontId) { toast("Pick the card this back belongs to first."); return; }
    btn.disabled = true;
    let r = await api(`/api/cards/${frontId}/attach-back/${backId}`, { method: "POST" });
    if (r.status === 409 && /confirm=true/.test(errText(r))) {
      const ok = await confirmDialog("Attach this back?", errText(r).replace(/\s*Repeat with confirm=true\.?/, ""), "Attach anyway");
      if (ok) r = await api(`/api/cards/${frontId}/attach-back/${backId}?confirm=true`, { method: "POST" });
    }
    if (r.ok) { toast("Back attached."); reloadAll(); }
    else { btn.disabled = false; if (r.status) toast("Could not attach: " + errText(r)); }
  }));
  view.querySelectorAll("[data-del-back]").forEach((btn) => btn.addEventListener("click", () => {
    const b = backs.find((x) => String(x.id) === btn.dataset.delBack);
    deleteCard({ ...b, player: b.player ? `the back of ${b.player}` : "the back" }, reloadAll);
  }));
}

// Deleted: restore within 7 days.
function renderDeleted(view, cards) {
  S.visible = [];
  if (!cards.length) {
    view.innerHTML = `<div class="panel empty-state muted">Nothing deleted. Deleted cards stay here for 7 days.</div>`;
    return;
  }
  view.innerHTML = `<p class="muted">Deleted cards can be restored for 7 days, then they are removed for good.</p>
    <div class="tiles">${cards.map((c) => {
      const f = cropUrl(c);
      return `<div class="tile">
        <div class="pic">${f ? `<img src="${f}" alt="front" data-zoom="${f}"/>` : "no photo"}</div>
        <div class="t">${c.side === "back" ? "Back: " : ""}${esc(cardTitle(c))}</div>
        <div class="s">${esc(cardLine(c))}</div>
        <div class="msg">${c.deleted_at ? "Deleted " + esc(new Date(c.deleted_at + (/Z|[+-]\d\d:?\d\d$/.test(c.deleted_at) ? "" : "Z")).toLocaleDateString()) : ""}</div>
        <div class="acts"><button class="btn" type="button" data-restore="${c.id}">Restore</button></div>
      </div>`;
    }).join("")}</div>`;
  view.querySelectorAll("[data-restore]").forEach((btn) => btn.addEventListener("click", () => {
    const c = cards.find((x) => String(x.id) === btn.dataset.restore);
    btn.disabled = true;
    restoreCard(c, reloadAll);
  }));
}

// --- price refresh job ----------------------------------------------------------------

let stopJob = null;
function watchRepriceJob(job) {
  if (stopJob) stopJob();
  const line = $("jobLine");
  const show = (j) => {
    line.classList.remove("hidden");
    if (j.status === "done") {
      line.innerHTML = `<span>✅</span><span>Prices refreshed for ${plural(j.total - j.failed, "card")}${j.failed ? `, ${j.failed} failed` : ""}.</span>
        <button class="linkbtn" type="button" id="jobHide" style="margin-left:auto">Hide</button>`;
      $("jobHide").onclick = () => { line.classList.add("hidden"); api(`/api/jobs/${j.job_id}/dismiss`, { method: "POST" }); };
      reloadAll();
      return;
    }
    line.innerHTML = `<span class="spin"></span><span>Refreshing prices: ${j.done} of ${j.total} done.</span>`;
  };
  show(job);
  if (job.status !== "done") stopJob = pollJob(job.job_id, show);
}

async function startReprice(ids) {
  const r = await api("/api/cards/reprice", ids ? { json: { card_ids: ids } } : { method: "POST" });
  if (!r.ok) { toast("Could not refresh prices: " + errText(r)); return; }
  watchRepriceJob(r.data);
}

// --- wiring ---------------------------------------------------------------------------

function wireToolbar() {
  ["sportFilter", "tagFilter", "sort"].forEach((id) => $(id).addEventListener("change", () => { saveState(); render(); }));
  ["minPrice", "maxPrice"].forEach((id) => $(id).addEventListener("input", () => { saveState(); render(); }));
  ["psaOnly", "anomOnly", "noPriceOnly"].forEach((id) => $(id).addEventListener("change", () => { saveState(); render(); }));
  $("clearMore").addEventListener("click", () => {
    $("minPrice").value = ""; $("maxPrice").value = "";
    ["psaOnly", "anomOnly", "noPriceOnly"].forEach((id) => { $(id).checked = false; });
    saveState(); render();
  });
  $("moreBtn").addEventListener("click", (e) => { e.stopPropagation(); $("morePanel").classList.toggle("hidden"); });
  document.addEventListener("click", (e) => {
    if (!e.target.closest(".dropdown")) $("morePanel").classList.add("hidden");
  });
  document.querySelectorAll("[data-view]").forEach((b) => b.addEventListener("click", () => {
    S.view = b.dataset.view;
    saveViewPref(S.view);
    render();
  }));
  narrow.addEventListener("change", render);

  $("selClear").addEventListener("click", () => { S.selected.clear(); render(); });
  $("selList").addEventListener("click", () => listOnEbayFlow(selectedCards(), "single", () => { S.selected.clear(); reloadAll(); }));
  $("selLot").addEventListener("click", () => {
    const cards = selectedCards();
    if (cards.length < 2) { toast("Pick at least 2 cards for a lot."); return; }
    listOnEbayFlow(cards, "lot", () => { S.selected.clear(); reloadAll(); });
  });
  $("selReprice").addEventListener("click", () => startReprice([...S.selected]));
  $("repriceAllBtn").addEventListener("click", async () => {
    const ok = await confirmDialog("Refresh every price?",
      "Looks up fresh prices for every card, skipping cards on eBay. It runs in the background and can take a while.",
      "Refresh all");
    if (ok) startReprice(null);
  });
  $("syncBtn").addEventListener("click", async (e) => {
    e.target.disabled = true;
    e.target.textContent = "Checking...";
    const r = await api("/api/listings/sync-sold", { method: "POST" });
    e.target.disabled = false;
    e.target.textContent = "Check for sales";
    if (!r.ok) { toast("Could not check eBay: " + errText(r)); return; }
    const sold = (r.data.sold || []).length;
    const errs = (r.data.errors || []).length;
    toast(sold ? `${plural(sold, "card")} sold. Marked as sold.` : `No new sales${errs ? ` (${plural(errs, "problem")} talking to eBay)` : ""}.`);
    if (sold) reloadAll();
  });

  window.addEventListener("scroll", () => saveState(), { passive: true });
  window.addEventListener("pagehide", saveState);
}

async function initCollection() {
  S.saved = readState();
  S.chip = CHIPS.some(([k]) => k === S.saved.chip) ? S.saved.chip : "all";
  S.view = loadViewPref();
  if (S.saved.sort) $("sort").value = S.saved.sort;
  if (S.saved.min) $("minPrice").value = S.saved.min;
  if (S.saved.max) $("maxPrice").value = S.saved.max;
  $("psaOnly").checked = !!S.saved.psa;
  $("anomOnly").checked = !!S.saved.anom;
  $("noPriceOnly").checked = !!S.saved.noPrice;
  wireToolbar();
  await initShell("collection");
  await reloadAll();
  // Resume a price refresh that is still running.
  const active = await api("/api/jobs/active");
  const job = active.ok && (active.data.jobs || []).find((j) => j.kind === "reprice" && j.status !== "done");
  if (job) watchRepriceJob(job);
}

initCollection();
