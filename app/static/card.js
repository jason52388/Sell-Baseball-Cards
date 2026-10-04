// Card page (/card/{id}): big photos, value and where it came from, the
// eBay listing (list, change price, end), details, every sale found, and the
// identification audit.

const cardId = Number(location.pathname.split("/").pop());
const $ = (id) => document.getElementById(id);

function parseIdent(c) {
  try { return JSON.parse(c.identification_json || "{}") || {}; } catch (e) { return {}; }
}

function plainDerivation(text) {
  if (!text) return "";
  return noLongDash(text)
    .replace(/CURRENT ASKING price\(s\)/g, "asking prices")
    .replace(/recent SOLD price\(s\)/g, "recent sales")
    .replace(/price\(s\)/g, "prices")
    .replace(/comp\(s\)/g, "prices")
    .replace(/outlier\(s\)/g, "outliers");
}

function kv(rows) {
  const items = rows.filter(([, v]) => v !== null && v !== undefined && v !== "" && v !== false);
  if (!items.length) return "";
  return `<dl class="kv">${items.map(([k, v]) => `<dt>${esc(k)}</dt><dd>${v === true ? "Yes" : esc(v)}</dd>`).join("")}</dl>`;
}

function pct(v) {
  return v == null ? null : `${Math.round(v * 100)}%`;
}

function provenance(x) {
  return `<span class="tag warn">${esc(x.source || "?")}</span>${x.marketplace ? `<span class="tag">${esc(x.marketplace)}</span>` : ""}`;
}

function link(url, text = "view") {
  return url ? `<a href="${esc(url)}" target="_blank" rel="noopener">${text}</a>` : "-";
}

function compsHtml(comps) {
  const exactBySource = {};
  comps.filter((x) => x.match_type === "exact" && x.sold_price != null).forEach((x) => {
    const s = x.source || "unknown";
    if (!exactBySource[s] || (x.sold_date || "") > (exactBySource[s].sold_date || "")) exactBySource[s] = x;
  });
  const bySource = Object.values(exactBySource);
  const summary = bySource.length ? `<div class="panel"><h3>Latest price by source</h3>
    <p class="muted small" style="margin-top:-4px">The amber tag is where the data came from; the grey tag is where the sale happened. "(sold)" is a finished sale, "(active)" an asking price.</p>
    <div class="scroll-x"><table class="plain"><thead><tr><th>Source</th><th>Price</th><th>Date</th><th></th></tr></thead><tbody>
    ${bySource.map((x) => `<tr><td>${provenance(x)}</td><td><b>${money(x.sold_price)}</b></td><td>${esc(x.sold_date) || "-"}</td><td>${link(x.listing_url)}</td></tr>`).join("")}
    </tbody></table></div></div>` : "";

  const provider = (x) => (x.source || "unknown").replace(/\s*\(.*\)$/, "");
  const isSold = (x) => x.sold_price != null && !/\(active\)/.test(x.source || "");
  const groups = {};
  comps.filter(isSold).forEach((x) => { (groups[provider(x)] = groups[provider(x)] || []).push(x); });
  const sales = Object.entries(groups).map(([prov, list]) => {
    list.sort((a, b) => (b.sold_date || "").localeCompare(a.sold_date || ""));
    return `<h3 style="margin-top:14px"><span class="tag warn">${esc(prov)}</span> <span class="muted small">${plural(list.length, "sale")}</span></h3>
      <div class="scroll-x"><table class="plain"><thead><tr><th>Where</th><th>Price</th><th>Date</th><th>Grade</th><th>Title</th><th></th></tr></thead><tbody>
      ${list.map((x) => `<tr><td>${esc(x.marketplace || "-")}</td><td><b>${money(x.sold_price)}</b></td><td>${esc(x.sold_date) || "-"}</td>
        <td>${esc(x.condition_grade) || "raw"}</td><td>${esc(x.title) || "-"}</td><td>${link(x.listing_url)}</td></tr>`).join("")}
      </tbody></table></div>`;
  }).join("");
  const salesPanel = sales ? `<div class="panel"><h3>All sales found</h3>${sales}</div>` : "";

  const section = (title, type) => {
    const rows = comps.filter((x) => x.match_type === type);
    if (!rows.length) return "";
    return `<h3 style="margin-top:14px">${title} <span class="muted small">${rows.length}</span></h3>
      <div class="scroll-x"><table class="plain"><thead><tr><th>Source</th><th>Photo</th><th>Price</th><th>Date</th><th>Grade</th><th>Why it matched</th><th></th></tr></thead><tbody>
      ${rows.map((x) => `<tr><td>${provenance(x)}</td>
        <td>${x.thumbnail_url ? `<img src="${esc(x.thumbnail_url)}" alt="" data-zoom="${esc(x.thumbnail_url)}" onerror="this.remove()"/>` : "-"}</td>
        <td><b>${money(x.sold_price)}</b></td><td>${esc(x.sold_date) || "-"}</td><td>${esc(x.condition_grade) || "-"}</td>
        <td>${esc(noLongDash(x.match_reason))}</td><td>${link(x.listing_url)}</td></tr>`).join("")}
      </tbody></table></div>`;
  };
  const used = `<div class="panel"><h3>Prices used</h3>${comps.length ? "" : '<p class="muted">No prices recorded for this card.</p>'}
    ${section("Exact matches", "exact")}${section("Close matches", "near")}${section("Graded copies", "graded")}</div>`;
  return summary + salesPanel + used;
}

function auditHtml(c) {
  const ident = parseIdent(c);
  const reads = Object.entries(ident.field_reads || {});
  const v = ident.verification;
  const verdict = v ? (v.agree === true ? "agreed" : v.agree === false ? "disagreed" : "could not confirm") : null;
  return `<div class="panel"><h3>How it was identified</h3>
    ${kv([
      ["Overall read", pct(c.confidence)],
      ["Confirmed by you", ident.user_confirmed ? "Yes" : null],
      ["Second look", verdict ? cap(verdict) + (v.notes ? `: ${noLongDash(v.notes)}` : "") : null],
      ["Text read", ident.raw_text ? noLongDash(ident.raw_text) : null],
      ["Grading notes", c.grading_notes ? noLongDash(c.grading_notes) : null],
      ["Unusual because", c.anomaly_notes ? noLongDash(c.anomaly_notes) : null],
    ])}
    ${reads.length ? `<div class="scroll-x" style="margin-top:10px"><table class="plain"><thead><tr><th>Field</th><th>Read as</th><th>Sure</th></tr></thead><tbody>
      ${reads.map(([k, r]) => `<tr><td>${esc(k.replace(/_/g, " "))}</td><td>${esc(r && r.value) || "-"}</td><td>${r && r.confidence != null ? pct(r.confidence) : "-"}</td></tr>`).join("")}
      </tbody></table></div>` : ""}
  </div>`;
}

async function listingPanel(c) {
  if (c.side === "back" || c.status === "preview" || c.status === "deleted") {
    return `<div class="panel"><h3>eBay</h3><p class="muted" style="margin:0">${
      c.status === "preview" ? "Add this card to your collection before listing it." : c.status === "deleted" ? "Restore this card to list it." : "Backs are not listed on their own."}</p></div>`;
  }
  const r = await api(`/api/listings/${c.id}`);
  const L = r.ok ? r.data : { listing_state: c.listing_state };
  if (L.listing_state === "live") {
    return `<div class="panel"><h3>eBay</h3>
      <p style="margin:0 0 10px"><span class="st live">Live on eBay</span> at <b>${money(L.list_price)}</b> ${L.listing_url ? `· <a href="${esc(L.listing_url)}" target="_blank" rel="noopener">view listing</a>` : ""}</p>
      <div class="acts" style="margin-top:0"><button class="btn" id="priceBtn" type="button">Change price</button>
        <button class="btn danger" id="endBtn" type="button">End listing</button></div></div>`;
  }
  if (L.listing_state === "sold") {
    return `<div class="panel"><h3>eBay</h3><p style="margin:0"><span class="st sold">Sold</span> for <b>${money(L.sold_price)}</b>${
      L.sold_at ? ` on ${esc(new Date(L.sold_at).toLocaleDateString())}` : ""}${L.listing_url ? ` · <a href="${esc(L.listing_url)}" target="_blank" rel="noopener">view</a>` : ""}</p></div>`;
  }
  return `<div class="panel"><h3>eBay</h3>
    <p class="muted" style="margin:0 0 10px">${L.listing_state === "ended" ? "The last listing ended. You can list it again." : "Not listed yet."}</p>
    ${c.estimated_price != null
      ? `<button class="btn" id="listBtn" type="button">List on eBay${APP.config.ebay_mode === "preview" ? " (preview)" : ""}</button>`
      : `<p class="muted small" style="margin:0">It needs a price before it can be listed.</p>`}
  </div>`;
}

async function render(c) {
  document.title = cardTitle(c);
  const f = cropUrl(c), b = backUrl(c);
  const zoom = [f, b].filter(Boolean).join("|");
  const n = basisCount(c);
  const basis = c.price_basis === "sold" ? `<span class="basis sold">from ${n != null ? plural(n, "real sale") : "real sales"}</span>`
    : c.price_basis === "active" ? `<span class="basis ask">from ${n != null ? plural(n, "asking price") : "asking prices"} (less reliable)</span>`
      : c.price_basis ? `<span class="basis">${esc(c.price_basis)}</span>` : "";
  const reasons = plainReasons(c.review_reason);
  const listing = await listingPanel(c);

  $("detail").innerHTML = `
    <p style="margin:0 0 10px"><a href="/repository">← Collection</a></p>
    ${c.status === "deleted" ? `<div class="banner"><span>This card is deleted. It can be restored for 7 days.</span>
      <button class="btn" id="restoreBtn" type="button" style="margin-left:auto">Restore</button></div>` : ""}
    <div class="bar" style="align-items:flex-start;margin-bottom:14px">
      <div class="who" style="min-width:0">
        <h2 style="font-size:22px">${c.side === "back" ? "Back: " : ""}${esc(cardTitle(c))}</h2>
        <div class="s">${esc(cardLine(c))}</div>
        <div>${tagsHtml(c)}</div>
      </div>
      <div class="grow" style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">
        ${statusHtml(c).replace('<div class="why">', '<span class="why" style="margin:0 6px 0 0">').replace(/<\/div>$/, "</span>")}
        ${c.status !== "deleted" ? `<button class="btn ghost" id="editBtn" type="button">Edit</button>
        <button class="more" id="menuBtn" type="button" aria-label="More actions">⋯</button>` : ""}
      </div>
    </div>

    <div class="detail">
      <div>
        <div class="panel">
          <div class="pics">
            <figure><div class="big">${f ? `<img src="${f}" alt="front" data-zoom="${zoom}"/>` : "no front photo"}</div><figcaption>Front</figcaption></figure>
            <figure><div class="big">${b ? `<img src="${b}" alt="back" data-zoom="${zoom}" onerror="this.replaceWith('no back photo')"/>` : "no back photo"}</div><figcaption>Back</figcaption></figure>
          </div>
          ${c.reference_image_url ? `<div style="margin-top:12px;display:flex;gap:10px;align-items:center">
            <img src="${esc(c.reference_image_url)}" alt="market photo" data-zoom="${esc(c.reference_image_url)}" style="width:70px;border-radius:6px;cursor:zoom-in" onerror="this.parentNode.remove()"/>
            <span class="muted small">Market photo of the same card from a listing, to compare.</span></div>` : ""}
        </div>
      </div>
      <div>
        <div class="panel">
          <div class="muted small">Value</div>
          <div class="value-big">${c.estimated_price != null ? money(c.estimated_price) : '<span class="muted" style="font-size:20px">no price</span>'}</div>
          <div>${basis}</div>
          <div style="margin-top:10px">${kv([
            ["Would list at", c.suggested_list_price != null ? money(c.suggested_list_price) + (isFloored(c) ? ` (raised to your ${money(c.price_floor)} minimum)` : "") : null],
            ["Last sold", c.sold_estimate != null ? money(c.sold_estimate) : null],
            ["Highest sold", c.sold_max_estimate != null ? money(c.sold_max_estimate) : null],
            ["Asking now", c.active_estimate != null ? money(c.active_estimate) : null],
            ["If graded PSA 10", c.graded_value_estimate != null ? money(c.graded_value_estimate) : null],
            ["Price sources", c.price_sources || null],
          ])}</div>
          ${c.derivation ? `<p class="muted small" style="margin:10px 0 0">${esc(cap(plainDerivation(c.derivation)))}${c.excluded_count ? `. ${plural(c.excluded_count, "sale")} that did not match were left out.` : ""}</p>` : ""}
          ${reasons.length ? `<div class="reason" style="margin:12px 0 0"><b>Needs a look:</b><ul>${reasons.map((r) => `<li>${esc(r)}</li>`).join("")}</ul></div>` : ""}
          ${c.status !== "deleted" && c.side !== "back" ? `<div style="margin-top:14px">
            <div class="muted small" style="margin-bottom:4px">Wrong price? Paste the card's SportsCardsPro link.</div>
            <div class="inline-form"><input type="url" id="scpUrl" placeholder="https://www.sportscardspro.com/game/..." aria-label="SportsCardsPro link"/>
              <button class="btn ghost" id="scpBtn" type="button">Use link</button></div>
            <div class="muted small" id="scpMsg" style="margin-top:4px"></div></div>` : ""}
        </div>
        ${listing}
        <div class="panel"><h3>Details</h3>${kv([
          ["Condition", cap(c.condition || "")],
          ["Team", c.team],
          ["Sport", cap(c.sport || "")],
          ["Subset / insert", c.subset],
          ["Parallel", c.parallel],
          ["Serial #", c.serial_number],
          ["Rookie card", c.rookie ? "Yes" : null],
          ["Batch tag", c.batch_tag],
          ["Photo", c.photo_quality && c.photo_quality !== "good" ? `${c.photo_quality} (consider a new photo)` : null],
          ["Grade guess", c.grade_estimate],
          ["PSA 10 chance", c.gem_mint_score ? pct(c.gem_mint_score) : null],
        ]) || '<p class="muted" style="margin:0">No extra details.</p>'}</div>
      </div>
    </div>
    ${compsHtml(c.comps || [])}
    ${auditHtml(c)}`;

  wire(c);
}

function wire(c) {
  const reload = () => load();
  const on = (id, fn) => { const el = $(id); if (el) el.addEventListener("click", fn); };
  on("editBtn", () => openEditor(c, reload));
  on("restoreBtn", () => restoreCard(c, reload));
  on("listBtn", () => listOnEbayFlow([c], "single", reload));
  on("priceBtn", () => changeListingPrice(c, reload));
  on("endBtn", () => endListing(c, reload));
  on("menuBtn", (e) => {
    e.stopPropagation();
    const onEbay = c.listing_state === "live" || c.listing_state === "sold";
    openMenu(e.currentTarget, [
      !onEbay && { label: "Re-analyze front + back", onClick: () => reanalyzeCard(c, reload) },
      c.has_back && { label: "Unmatch back", onClick: () => unmatchBack(c, reload) },
      !onEbay && c.side !== "back" && !c.has_back && { label: "This is a back", onClick: () => markBack(c) },
      "sep",
      { label: "Delete", danger: true, onClick: () => deleteCard(c, reload) },
    ]);
  });
  on("scpBtn", async () => {
    const url = $("scpUrl").value.trim();
    const msg = $("scpMsg");
    if (!url) { msg.textContent = "Paste a link first."; return; }
    $("scpBtn").disabled = true;
    msg.textContent = "Reading that page...";
    const r = await api(`/api/cards/${c.id}/price-from-url`, { json: { url } });
    if (!r.ok) { msg.textContent = errText(r); $("scpBtn").disabled = false; return; }
    toast("Price updated from the link.");
    load();
  });
}

async function markBack(c) {
  const ok = await confirmDialog("Is this the back of a card?",
    "It leaves your collection and joins its front if one matches. If none matches, it waits under Unmatched backs.",
    "Yes, it's a back");
  if (!ok) return;
  const inLibrary = c.status !== "preview";
  const r = await api(`/api/cards/${c.id}/mark-back${inLibrary ? "?confirm=true" : ""}`, { method: "POST" });
  if (!r.ok) { toast("Could not change it: " + errText(r)); return; }
  if (r.data.merged_into) {
    toast("Joined to its front. Opening that card...");
    window.location.href = `/card/${r.data.merged_into}`;
  } else {
    toast("Moved to Unmatched backs.");
    window.location.href = "/repository";
  }
}

async function load() {
  const r = await api(`/api/cards/${cardId}`);
  if (!r.ok) {
    $("detail").innerHTML = `<div class="panel empty-state"><h2>Card not found</h2>
      <p class="muted">It may have been removed. <a href="/repository">Back to your collection</a>.</p></div>`;
    return;
  }
  await render(r.data);
  refreshReviewCount();
}

(async () => {
  await initShell("collection");
  await load();
})();
