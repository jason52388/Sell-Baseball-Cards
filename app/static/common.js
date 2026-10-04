// Shared helpers for every page: header, banner, formatting, menus, dialogs,
// toast with Undo, lightbox, card editor, listing flows, job polling.
// Page scripts: upload.js, collection.js, review.js, card.js.

const APP = { config: { ebay_mode: "preview", price_floor: null, min_store_value: 4 }, stats: null };

// --- formatting ---------------------------------------------------------------

function esc(v) {
  if (v == null) return "";
  return String(v).replace(/[&<>"']/g, (ch) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch]
  ));
}

function money(v) {
  return v == null ? "-" : "$" + Number(v).toFixed(2);
}

function cap(s) {
  return s ? s.charAt(0).toUpperCase() + s.slice(1) : "";
}

function plural(n, one, many) {
  return `${n} ${n === 1 ? one : (many || one + "s")}`;
}

// "1996 Upper Deck · #CH3 · Basketball"
function cardLine(c) {
  return [
    [c.year, c.set_brand].filter(Boolean).join(" "),
    c.card_number ? "#" + c.card_number : "",
    cap(c.sport || ""),
  ].filter(Boolean).join(" · ");
}

function cardTitle(c) {
  return c.player || "Unknown player";
}

function cropUrl(c) {
  return c.crop_path ? `/api/cards/${c.id}/crop?v=${c.upload_id || ""}` : null;
}

function backUrl(c) {
  return c.has_back ? `/api/cards/${c.id}/back-crop?v=${c.upload_id || ""}` : null;
}

function tagsHtml(c, { batch = true } = {}) {
  const out = [];
  if (batch && c.batch_tag) out.push(`<span class="tag">Tag: ${esc(c.batch_tag)}</span>`);
  if (c.subset) out.push(`<span class="tag">Subset: ${esc(c.subset)}</span>`);
  if (c.parallel) out.push(`<span class="tag par">${esc(c.parallel)}</span>`);
  if (c.rookie) out.push(`<span class="tag rc">RC</span>`);
  if (c.psa10_candidate) out.push(`<span class="tag warn">Possible PSA 10</span>`);
  if (c.anomaly_flag) out.push(`<span class="tag warn">Unusual</span>`);
  return out.join("");
}

// How many comps the price came from, read from the derivation text
// ("based on 38 CURRENT ASKING price(s) ..."). null when it does not say.
function basisCount(c) {
  const m = /based on (\d+)/i.exec(c.derivation || "");
  return m ? Number(m[1]) : null;
}

function valueHtml(c) {
  if (c.estimated_price == null) {
    return `<div class="price none">no price</div><div class="basis">no matching sales</div>`;
  }
  const n = basisCount(c);
  const b = (c.price_basis || "").toLowerCase();
  let basis = "";
  if (b === "sold") basis = `<div class="basis sold">sold${n != null ? " · " + plural(n, "sale") : ""}</div>`;
  else if (b === "active") basis = `<div class="basis ask">asking${n != null ? " · " + plural(n, "listing") : ""}</div>`;
  else if (b) basis = `<div class="basis">${esc(b)}</div>`;
  return `<div class="price">${money(c.estimated_price)}</div>${basis}`;
}

function isFloored(c) {
  return c.suggested_list_price != null && c.price_floor != null
    && Math.abs(c.suggested_list_price - c.price_floor) < 0.01;
}

function listAtHtml(c) {
  if (c.suggested_list_price == null) return `<span class="muted">-</span>`;
  return `${money(c.suggested_list_price)}${isFloored(c) ? ` <span class="basis" title="Raised to your lowest list price">min</span>` : ""}`;
}

// Long dashes from server text become a colon (or a plain hyphen).
const LONG_DASH_SPACED = /\s+[—–]\s+/g;
const LONG_DASH = /[—–]/g;
function noLongDash(s) {
  return String(s || "").replace(LONG_DASH_SPACED, ": ").replace(LONG_DASH, "-");
}

// Turn the server's review reason ("a; b; c") into plain sentences.
function plainReasons(reason) {
  if (!reason) return [];
  const rules = [
    [/^low identification confidence/i, "Not sure the card was read right"],
    [/^incomplete identification/i, "Some details are missing; fill them in"],
    [/^no confident price match/i, "No good price match: check the year, set or insert"],
    [/^no marketplace match/i, "No price match found for these details"],
    [/^potential PSA 10/i, "Might grade a PSA 10: check before listing"],
    [/^anomaly detected/i, "Marked as unusual (error or misprint): check the value"],
    [/^eBay sold \(Insights\) off/i, "Priced from asking prices only (no sold data)"],
    [/^only (\d+) comp/i, (m) => `Only ${m[1]} price found, so the price is rough`],
    [/^price low-confidence/i, null],
    [/^sold comps have no sale dates/i, "Sales have no dates, so the price is rough"],
    [/^SportsCardsPro rejected the API token/i, "SportsCardsPro sign-in expired, so no sold prices"],
    [/^card back/i, "A card back waiting for its front"],
    [/^pricing error/i, "Pricing failed; try Refresh prices"],
  ];
  const out = [];
  for (const raw of reason.split(";").map((s) => s.trim()).filter(Boolean)) {
    let text = noLongDash(raw);
    let matched = false;
    for (const [re, plain] of rules) {
      const m = re.exec(raw);
      if (!m) continue;
      matched = true;
      text = typeof plain === "function" ? plain(m) : plain;
      break;
    }
    if (!matched) text = cap(text);
    if (text && !out.includes(text)) out.push(text);
  }
  return out;
}

// Status chip for a library card: what state it is in and one line of why.
function statusInfo(c) {
  const floor = APP.config.min_store_value != null ? APP.config.min_store_value : 4;
  if (c.status === "deleted") return { cls: "bad", label: "Deleted", why: "Restore within 7 days" };
  if (c.listing_state === "sold") return { cls: "sold", label: "Sold", why: "Sold on eBay" };
  if (c.listing_state === "live") {
    return {
      cls: "live", label: "Live on eBay",
      why: c.ebay_listing_url ? `<a href="${esc(c.ebay_listing_url)}" target="_blank" rel="noopener">view on eBay</a>` : "",
      whyHtml: true,
    };
  }
  if (c.side === "back") return { cls: "low", label: "Back only", why: "Waiting for its front" };
  if (c.status === "preview") return { cls: "low", label: "Not added", why: "" };
  if (c.status === "needs_review") return { cls: "rev", label: "Needs review", why: plainReasons(c.review_reason)[0] || "Check the details" };
  if (c.status === "list_failed") return { cls: "bad", label: "Listing failed", why: plainReasons(c.review_reason)[0] || "Try listing again" };
  if (c.status === "below_threshold") return { cls: "low", label: `Under $${floor}`, why: `Below your $${floor} keep-and-sell line` };
  if (c.status === "priced") {
    return { cls: "ready", label: "Ready to list", why: c.listing_state === "ended" ? "Listing ended; can list again" : "" };
  }
  return { cls: "low", label: cap(String(c.status || "").replace(/_/g, " ")), why: "" };
}

function statusHtml(c) {
  const s = statusInfo(c);
  return `<span class="st ${s.cls}">${esc(s.label)}</span>${s.why ? `<div class="why">${s.whyHtml ? s.why : esc(s.why)}</div>` : ""}`;
}

// --- network --------------------------------------------------------------------

// fetch + JSON. Returns {ok, status, data}; never throws for HTTP errors.
async function api(url, opts = {}) {
  const init = { ...opts };
  if (opts.json !== undefined) {
    init.method = init.method || "POST";
    init.headers = { "Content-Type": "application/json", ...(opts.headers || {}) };
    init.body = JSON.stringify(opts.json);
    delete init.json;
  }
  try {
    const r = await fetch(url, init);
    const data = await r.json().catch(() => ({}));
    return { ok: r.ok, status: r.status, data };
  } catch (e) {
    return { ok: false, status: 0, data: { detail: "Could not reach the app. Is it running?" } };
  }
}

function errText(res) {
  const d = res && res.data && res.data.detail;
  if (!d) return `something went wrong (${res ? res.status : "?"})`;
  if (typeof d === "string") return noLongDash(d);
  if (Array.isArray(d)) return d.map((x) => x.msg || String(x)).join("; ");
  return String(d);
}

async function loadConfig() {
  const r = await api("/api/config");
  if (r.ok) APP.config = r.data;
  return APP.config;
}

async function loadStats() {
  const r = await api("/api/cards/stats");
  if (r.ok) APP.stats = r.data;
  return APP.stats;
}

// Poll a background job every ~2s until it is done. onUpdate(job) on each poll.
// Returns a function that stops polling.
function pollJob(jobId, onUpdate, intervalMs = 2000) {
  let stopped = false;
  const tick = async () => {
    if (stopped) return;
    const r = await api(`/api/jobs/${jobId}`);
    if (stopped) return;
    if (r.ok) {
      onUpdate(r.data);
      if (r.data.status === "done") { stopped = true; return; }
    }
    setTimeout(tick, intervalMs);
  };
  tick();
  return () => { stopped = true; };
}

// --- page shell -----------------------------------------------------------------

function renderHeader(active) {
  const el = document.getElementById("top");
  if (!el) return;
  const nav = (key, href, label, extra = "") =>
    `<a class="nav${active === key ? " on" : ""}" href="${href}">${label}${extra}</a>`;
  el.className = "top";
  el.innerHTML = `
    <h1><a href="/repository">Sell Cards</a></h1>
    ${nav("upload", "/", "Upload")}
    ${nav("review", "/review", "Review", `<span class="count hidden" id="reviewCount"></span>`)}
    ${nav("collection", "/repository", "Collection")}
    <span class="mode" id="modePill"></span>`;
}

function renderModePill() {
  const el = document.getElementById("modePill");
  if (!el) return;
  const m = (APP.config.ebay_mode || "preview").toLowerCase();
  el.className = "mode" + (m === "live" ? "" : " " + m);
  el.textContent = m === "live" ? "LIVE eBay" : m === "sandbox" ? "eBay sandbox" : "Preview only";
  el.title = m === "live"
    ? "Listing buttons publish to real eBay."
    : m === "sandbox" ? "Listing buttons publish to eBay's test site."
      : "Listing buttons build the listing but publish nothing.";
}

function renderReviewCount() {
  const el = document.getElementById("reviewCount");
  if (!el || !APP.stats) return;
  const n = APP.stats.needs_review_count || 0;
  el.textContent = n;
  el.classList.toggle("hidden", n === 0);
}

async function renderSourceBanner() {
  const el = document.getElementById("banner");
  if (!el) return;
  const r = await api("/api/sources/health");
  const text = r.ok ? r.data.banner : null;
  if (!text) { el.innerHTML = ""; el.className = "hidden"; return; }
  const parts = noLongDash(text).split(/\s*\|\s*|\n+/).filter(Boolean);
  el.className = "banner";
  el.innerHTML = `<span aria-hidden="true">⚠️</span><span>${parts.map((p) => {
    const i = p.indexOf(":");
    return i > 0 && i < 40 ? `<b>${esc(p.slice(0, i))}:</b>${esc(p.slice(i + 1))}` : esc(p);
  }).join("<br/>")}<br/>Prices marked <b style="color:var(--amber)">asking</b> are less reliable.</span>`;
}

// Header, mode pill, review count and the price-source banner.
async function initShell(active) {
  renderHeader(active);
  await Promise.all([loadConfig(), loadStats()]);
  renderModePill();
  renderReviewCount();
  renderSourceBanner();
}

async function refreshReviewCount() {
  await loadStats();
  renderReviewCount();
}

// --- toast with optional action ---------------------------------------------------

let _toastTimer = null;
function toast(msg, { action, onAction, ms } = {}) {
  let t = document.getElementById("toast");
  if (!t) {
    t = document.createElement("div");
    t.id = "toast";
    document.body.appendChild(t);
  }
  t.className = "toast";
  t.setAttribute("role", "status");
  t.innerHTML = `<span>${esc(msg)}</span>${action ? `<button type="button">${esc(action)}</button>` : ""}`;
  if (action) {
    t.querySelector("button").onclick = () => {
      t.classList.remove("show");
      onAction && onAction();
    };
  }
  void t.offsetWidth;
  t.classList.add("show");
  clearTimeout(_toastTimer);
  _toastTimer = setTimeout(() => t.classList.remove("show"), ms || (action ? 8000 : 3500));
}

// --- lightbox ---------------------------------------------------------------------

function openLightbox(srcs) {
  let ov = document.getElementById("lightbox");
  if (!ov) {
    ov = document.createElement("div");
    ov.id = "lightbox";
    ov.className = "lightbox";
    ov.addEventListener("click", () => ov.classList.remove("show"));
    document.body.appendChild(ov);
  }
  ov.innerHTML = srcs.filter(Boolean).map((s) => `<img src="${esc(s)}" alt="card photo"/>`).join("");
  ov.classList.add("show");
}

function closeLightbox() {
  const ov = document.getElementById("lightbox");
  if (ov && ov.classList.contains("show")) { ov.classList.remove("show"); return true; }
  return false;
}

// Any element with data-zoom="url|url" opens those photos.
document.addEventListener("click", (e) => {
  const z = e.target.closest("[data-zoom]");
  if (!z) return;
  e.preventDefault();
  e.stopPropagation();
  openLightbox(z.dataset.zoom.split("|"));
}, true);

document.addEventListener("keydown", (e) => {
  if (e.key !== "Escape") return;
  if (closeLightbox()) return;
  if (_menu) { closeMenu(); return; }
  const ovs = document.querySelectorAll(".overlay");
  const top = ovs[ovs.length - 1];
  if (top && top._close) top._close();
});

function photosHtml(c, cls = "ph") {
  const f = cropUrl(c), b = backUrl(c);
  const zoom = [f, b].filter(Boolean).join("|");
  if (!f && !b) return `<div class="${cls}"><span class="none">no photo</span></div>`;
  return `<div class="${cls}">${f ? `<img src="${f}" alt="front" loading="lazy" data-zoom="${zoom}"/>` : ""}${b ? `<img class="b" src="${b}" alt="back" loading="lazy" data-zoom="${zoom}" onerror="this.remove()"/>` : ""}</div>`;
}

// --- menus ------------------------------------------------------------------------

let _menu = null;
function closeMenu() {
  if (_menu) { _menu.remove(); _menu = null; }
}

// items: [{label, onClick, danger}] or "sep"; falsy items are skipped.
function openMenu(anchor, items) {
  closeMenu();
  const m = document.createElement("div");
  m.className = "menu";
  m.setAttribute("role", "menu");
  items.filter(Boolean).forEach((it) => {
    if (it === "sep") { m.appendChild(document.createElement("hr")); return; }
    const b = document.createElement("button");
    b.type = "button";
    b.textContent = it.label;
    if (it.danger) b.className = "danger";
    b.addEventListener("click", (e) => { e.stopPropagation(); closeMenu(); it.onClick(); });
    m.appendChild(b);
  });
  document.body.appendChild(m);
  const r = anchor.getBoundingClientRect();
  const w = m.offsetWidth, h = m.offsetHeight;
  let left = Math.min(r.right - w, window.innerWidth - w - 8);
  left = Math.max(8, left);
  let top = r.bottom + 4;
  if (top + h > window.innerHeight - 8) top = Math.max(8, r.top - h - 4);
  m.style.left = left + "px";
  m.style.top = top + "px";
  _menu = m;
}

document.addEventListener("click", (e) => {
  if (_menu && !e.target.closest(".menu")) closeMenu();
});
window.addEventListener("scroll", closeMenu, { passive: true });

// --- dialogs ----------------------------------------------------------------------

// Opens a modal. Returns {el, close}. onClose runs however it is closed.
function openModal(html, { wide = false, onClose } = {}) {
  const ov = document.createElement("div");
  ov.className = "overlay";
  ov.innerHTML = `<div class="modal${wide ? " wide" : ""}" role="dialog" aria-modal="true">${html}</div>`;
  let closed = false;
  const close = () => { if (closed) return; closed = true; ov.remove(); onClose && onClose(); };
  ov._close = close;
  ov.addEventListener("mousedown", (e) => { if (e.target === ov) close(); });
  ov.querySelectorAll("[data-close]").forEach((b) => b.addEventListener("click", close));
  document.body.appendChild(ov);
  return { el: ov.querySelector(".modal"), close };
}

function confirmDialog(title, body, okLabel = "OK", { danger = false } = {}) {
  return new Promise((resolve) => {
    let answered = false;
    const { el, close } = openModal(`
      <div class="head"><h3>${esc(title)}</h3></div>
      <p class="muted" style="margin:0">${esc(body)}</p>
      <div class="foot">
        <button class="btn ${danger ? "danger" : ""}" data-ok>${esc(okLabel)}</button>
        <button class="btn ghost" data-close>Cancel</button>
      </div>`, { onClose: () => { if (!answered) resolve(false); } });
    el.querySelector("[data-ok]").addEventListener("click", () => { answered = true; close(); resolve(true); });
    el.querySelector("[data-ok]").focus();
  });
}

// --- card actions -----------------------------------------------------------------

// Delete (to the trash) with an Undo toast. Asks first for a sold card.
async function deleteCard(c, onDone) {
  let r = await api(`/api/cards/${c.id}`, { method: "DELETE" });
  if (r.status === 409) {
    const ok = await confirmDialog("Delete a sold card?",
      "This card sold on eBay, and its record is the sale's history. Delete it anyway?",
      "Delete anyway", { danger: true });
    if (!ok) return false;
    r = await api(`/api/cards/${c.id}?confirm=true`, { method: "DELETE" });
  }
  if (!r.ok) { toast("Could not delete: " + errText(r)); return false; }
  const extra = r.data.message && r.data.message !== "already deleted" ? ` ${noLongDash(r.data.message)}` : "";
  toast(`Deleted ${cardTitle(c).replace(/\.$/, "")}.${extra}`, {
    action: "Undo",
    onAction: async () => {
      const u = await api(`/api/cards/${c.id}/restore`, { method: "POST" });
      toast(u.ok ? "Restored." : "Could not restore: " + errText(u));
      onDone && onDone();
    },
  });
  onDone && onDone();
  return true;
}

async function restoreCard(c, onDone) {
  const r = await api(`/api/cards/${c.id}/restore`, { method: "POST" });
  toast(r.ok ? `Restored ${cardTitle(c)}.` : "Could not restore: " + errText(r));
  if (r.ok && onDone) onDone();
}

async function reanalyzeCard(c, onDone) {
  toast("Reading the card again. This can take a minute...", { ms: 120000 });
  const r = await api(`/api/cards/${c.id}/reanalyze`, { method: "POST" });
  if (!r.ok) { toast("Re-analyze failed: " + errText(r)); return null; }
  toast("Read again and re-priced.");
  onDone && onDone(r.data);
  return r.data;
}

async function unmatchBack(c, onDone) {
  const ok = await confirmDialog("Unmatch the back?",
    "The back photo is split off and goes to Unmatched backs, where you can attach it to the right card.",
    "Unmatch");
  if (!ok) return;
  const r = await api(`/api/cards/${c.id}/detach-back`, { method: "POST" });
  toast(r.ok ? "Back unmatched." : "Could not unmatch: " + errText(r));
  if (r.ok && onDone) onDone();
}

async function endListing(c, onDone) {
  const ok = await confirmDialog("End this eBay listing?",
    `${cardTitle(c)} comes off eBay. You can list it again later.`, "End listing", { danger: true });
  if (!ok) return;
  const r = await api(`/api/listings/${c.id}/end`, { method: "POST" });
  toast(r.ok ? (noLongDash(r.data.message) || "Listing ended.") : "Could not end it: " + errText(r));
  if (r.ok && onDone) onDone();
}

async function changeListingPrice(c, onDone) {
  const info = await api(`/api/listings/${c.id}`);
  const current = info.ok ? info.data.list_price : null;
  const suggested = info.ok ? info.data.suggested_list_price : c.suggested_list_price;
  const floor = info.ok ? info.data.price_floor : c.price_floor;
  const { el, close } = openModal(`
    <div class="head"><h3>Change the eBay price</h3><button class="more" data-close aria-label="Close">✕</button></div>
    <p class="muted" style="margin-top:0">${esc(cardTitle(c))}. Now ${money(current)}. Suggested ${money(suggested)}. Lowest allowed ${money(floor)}.</p>
    <label class="muted small">New price ($)<br/>
      <input type="number" step="0.01" min="0.01" id="newPrice" value="${suggested != null ? Number(suggested).toFixed(2) : ""}" style="width:140px;font-weight:700"/></label>
    <div class="foot"><button class="btn" data-ok>Change price</button><button class="btn ghost" data-close>Cancel</button>
      <span class="muted small" data-msg></span></div>`);
  el.querySelector("[data-ok]").addEventListener("click", async (e) => {
    const v = parseFloat(el.querySelector("#newPrice").value);
    e.target.disabled = true;
    const r = await api(`/api/listings/${c.id}/price`, { json: isNaN(v) ? {} : { price: v } });
    if (!r.ok) { el.querySelector("[data-msg]").textContent = errText(r); e.target.disabled = false; return; }
    close();
    toast(r.data.message || "Price changed.");
    onDone && onDone();
  });
}

// --- card editor --------------------------------------------------------------------

const EDIT_FIELDS = [
  ["player", "Player"], ["year", "Year"], ["set_brand", "Set"], ["card_number", "Card #"],
  ["subset", "Subset / insert"], ["parallel", "Parallel"], ["team", "Team"], ["sport", "Sport"],
  ["condition", "Condition"], ["serial_number", "Serial #"],
];

function editFieldsHtml(c) {
  return `<div class="fields">
    ${EDIT_FIELDS.map(([k, label]) => `<label>${label}<input data-f="${k}" value="${esc(c[k] || "")}"/></label>`).join("")}
    <div class="full" style="display:flex;gap:16px;flex-wrap:wrap">
      <label class="check"><input type="checkbox" data-f="rookie" ${c.rookie ? "checked" : ""}/> Rookie card (RC)</label>
      <label class="check"><input type="checkbox" data-f="psa10_candidate" ${c.psa10_candidate ? "checked" : ""}/> Possible PSA 10</label>
      <label class="check"><input type="checkbox" data-f="anomaly_flag" ${c.anomaly_flag ? "checked" : ""}/> Unusual (error, misprint)</label>
    </div>
    <label>Replace front photo<input type="file" accept="image/*" data-photo="front"/></label>
    <label>Replace back photo<input type="file" accept="image/*" data-photo="back"/></label>
  </div>`;
}

// PATCH the changed fields in `root`, then upload any new photos.
async function saveEditFields(c, root) {
  const body = {};
  root.querySelectorAll("[data-f]").forEach((inp) => {
    const k = inp.dataset.f;
    if (inp.type === "checkbox") {
      if (Boolean(c[k]) !== inp.checked) body[k] = inp.checked;
    } else if ((c[k] || "") !== inp.value.trim()) {
      body[k] = inp.value.trim();
    }
  });
  let card = c;
  if (Object.keys(body).length) {
    const r = await api(`/api/cards/${c.id}`, { method: "PATCH", json: body });
    if (!r.ok) throw new Error(errText(r));
    card = r.data;
  }
  for (const inp of root.querySelectorAll("[data-photo]")) {
    const file = inp.files && inp.files[0];
    if (!file) continue;
    const fd = new FormData();
    fd.append("file", file);
    const r = await api(`/api/cards/${c.id}/replace-photo?side=${inp.dataset.photo}`, { method: "POST", body: fd });
    if (!r.ok) throw new Error(errText(r));
    card = r.data;
  }
  return card;
}

async function openEditor(cardOrId, onSaved) {
  let c = cardOrId;
  if (typeof cardOrId === "number") {
    const r = await api(`/api/cards/${cardOrId}`);
    if (!r.ok) { toast("Could not load that card."); return; }
    c = r.data;
  }
  const f = cropUrl(c), b = backUrl(c);
  const zoom = [f, b].filter(Boolean).join("|");
  const onEbay = c.listing_state === "live" || c.listing_state === "sold";
  const { el, close } = openModal(`
    <div class="head"><h3>Edit ${esc(cardTitle(c))}</h3><button class="more" data-close aria-label="Close">✕</button></div>
    <div style="display:flex;gap:10px;margin-bottom:12px">
      ${f ? `<img src="${f}" data-zoom="${zoom}" style="width:90px;border-radius:6px;cursor:zoom-in" alt="front"/>` : ""}
      ${b ? `<img src="${b}" data-zoom="${zoom}" style="width:90px;border-radius:6px;cursor:zoom-in" alt="back" onerror="this.remove()"/>` : ""}
    </div>
    ${editFieldsHtml(c)}
    <p class="muted small">${onEbay ? "This card is on eBay, so saving keeps its listed price." : "Saving looks up the price again."}</p>
    <div class="foot"><button class="btn" data-save>Save</button><button class="btn ghost" data-close>Cancel</button>
      <span class="muted small" data-msg></span></div>`, { wide: true });
  el.querySelector("[data-save]").addEventListener("click", async (e) => {
    const msg = el.querySelector("[data-msg]");
    e.target.disabled = true;
    msg.textContent = "Saving...";
    try {
      const card = await saveEditFields(c, el);
      close();
      toast("Saved.");
      onSaved && onSaved(card);
    } catch (err) {
      msg.textContent = "Could not save: " + err.message;
      e.target.disabled = false;
    }
  });
}

// --- listing on eBay ----------------------------------------------------------------

function modeWords() {
  const m = APP.config.ebay_mode;
  return m === "live" ? "on eBay" : m === "sandbox" ? "on the eBay test site" : "as a preview (nothing is published)";
}

// Ask for prices, list, then show every card's result (with the reason when
// one failed). mode: "single" (each card its own listing) or "lot".
function listOnEbayFlow(cards, mode, onDone) {
  if (!cards.length) return;
  const lot = mode === "lot";
  const sumList = cards.reduce((s, c) => s + (c.suggested_list_price || 0), 0);
  const rows = cards.map((c) => {
    const img = cropUrl(c) ? `<img src="${cropUrl(c)}" alt=""/>` : "";
    return `<div class="row-item">${img}<div class="grow"><b>${esc(cardTitle(c))}</b><div class="muted small">${esc(cardLine(c))}</div></div>
      <div style="text-align:right">${lot ? `<span class="muted">${money(c.suggested_list_price)}</span>` : `<label class="muted small">List at $<br/><input type="number" step="0.01" min="0.01" data-price="${c.id}"
        value="${c.suggested_list_price != null ? Number(c.suggested_list_price).toFixed(2) : ""}" style="width:90px;font-weight:700"/></label>`}</div></div>`;
  }).join("");
  const { el, close } = openModal(`
    <div class="head"><h3>${lot ? `List ${cards.length} cards as one lot` : `List ${plural(cards.length, "card")}`}</h3>
      <button class="more" data-close aria-label="Close">✕</button></div>
    <p class="muted" style="margin-top:0">This lists ${modeWords()}.${lot ? " One listing with every card and its photos." : " Each card gets its own listing."}</p>
    <div>${rows}</div>
    ${lot ? `<div style="margin-top:12px"><label class="muted small">Lot price ($)<br/><input type="number" step="0.01" min="0.01" id="lotPrice" placeholder="${sumList.toFixed(2)}" style="width:120px;font-weight:700"/></label>
      <div class="muted small" style="margin-top:4px">Leave blank to use the cards' prices added up (lowest price applied once).</div></div>` : ""}
    <div class="foot"><button class="btn" data-go>${lot ? "List the lot" : "List now"}</button><button class="btn ghost" data-close>Cancel</button>
      <span class="muted small" data-msg></span></div>`, { wide: true });
  el.querySelector("[data-go]").addEventListener("click", async (e) => {
    e.target.disabled = true;
    el.querySelector("[data-msg]").textContent = "Listing...";
    const ids = cards.map((c) => c.id);
    let r;
    if (lot) {
      const v = parseFloat(el.querySelector("#lotPrice").value);
      r = await api("/api/listings/sell-set", { json: { card_ids: ids, ...(isNaN(v) ? {} : { prices: { set: v } }) } });
    } else {
      const prices = {};
      el.querySelectorAll("[data-price]").forEach((inp) => {
        const v = parseFloat(inp.value);
        const c = cards.find((x) => String(x.id) === inp.dataset.price);
        if (!isNaN(v) && (!c || c.suggested_list_price == null || Math.abs(v - c.suggested_list_price) > 0.001)) {
          prices[inp.dataset.price] = v;
        }
      });
      r = await api("/api/listings/sell", { json: { card_ids: ids, ...(Object.keys(prices).length ? { prices } : {}) } });
    }
    close();
    if (!r.ok) { toast("Listing failed: " + errText(r)); onDone && onDone(); return; }
    showListResults(cards, r.data, lot);
    onDone && onDone();
  });
}

// Plain words for the server's per-card listing message.
function plainListError(msg) {
  const m = noLongDash(msg || "");
  if (/already listed on eBay/i.test(m)) return "Already live on eBay. Change its price or end it first.";
  if (/already sold/i.test(m)) return "Already sold on eBay.";
  if (/no estimated price/i.test(m)) return "No price yet, so it cannot be listed.";
  if (/not in library/i.test(m)) return "Not in your collection yet.";
  if (/not found/i.test(m)) return "Card not found.";
  return m;
}

function showListResults(cards, data, lot) {
  const name = (id) => {
    const c = cards.find((x) => x.id === id);
    return c ? cardTitle(c) : "A card";
  };
  const preview = (s) => s === "preview";
  let body;
  if (lot) {
    const ok = data.status === "published" || preview(data.status);
    const skipped = (data.skipped || []).map((s) => {
      const m = /^card (\d+): (.*)$/.exec(s);
      return m ? `${name(Number(m[1]))}: ${plainListError(m[2])}` : plainListError(s);
    });
    body = `<p class="${ok ? "result-ok" : "result-bad"}" style="margin-top:0"><b>${
      data.status === "published" ? `Listed ${plural((data.card_ids || []).length, "card")} as one lot at ${money(data.list_price)}.`
        : preview(data.status) ? `Preview only: the lot would list at ${money(data.list_price)}. Nothing was published.`
          : "The lot was not listed."}</b></p>
      ${!ok && data.message && !skipped.length ? `<p>${esc(plainListError(data.message))}</p>` : ""}
      ${skipped.length ? `<p class="muted">Left out:</p><ul>${skipped.map((s) => `<li>${esc(s)}</li>`).join("")}</ul>` : ""}`;
  } else {
    const results = data.results || [];
    const good = results.filter((x) => x.ok);
    const allPreview = good.length && results.every((x) => preview(x.status));
    body = `<p style="margin-top:0"><b>${good.length} of ${results.length} ${allPreview ? "previewed (nothing published)" : "listed"}.</b></p>
      <table class="plain"><tbody>${results.map((x) => `<tr>
        <td>${esc(name(x.card_id))}</td>
        <td class="${x.ok ? "result-ok" : "result-bad"}">${x.ok
          ? (preview(x.status) ? `Preview at ${money(x.list_price)}` : `Listed at ${money(x.list_price)}${x.listing_url ? ` · <a href="${esc(x.listing_url)}" target="_blank" rel="noopener">view</a>` : ""}`)
          : esc(plainListError(x.error || x.message || x.status))}</td></tr>`).join("")}</tbody></table>`;
  }
  openModal(`<div class="head"><h3>Listing results</h3><button class="more" data-close aria-label="Close">✕</button></div>
    ${body}<div class="foot"><button class="btn" data-close>Done</button></div>`, { wide: true });
}
