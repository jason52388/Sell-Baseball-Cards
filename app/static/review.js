// Review page (/review): one card at a time from GET /api/review/next.
// Enter = looks right (saves edits first), R = read again, Right arrow = skip.

const R = { p: null, done: 0, busy: false };
const $ = (id) => document.getElementById(id);

const SIDE_WORDS = {
  front: "front", back: "back", both: "both sides", verifier: "second look", user: "you typed",
};

// Review rows: [field key, label, placeholder]
const ROWS = [
  ["player", "Player", ""],
  ["year", "Year", ""],
  ["set_brand", "Set", ""],
  ["card_number", "Card #", ""],
  ["subset", "Subset / insert", "e.g. League Leaders"],
  ["parallel", "Parallel", "e.g. Gold, Refractor, /99"],
  ["condition", "Condition", "e.g. Near mint"],
  ["team", "Team", ""],
  ["sport", "Sport", "baseball, basketball..."],
  ["serial_number", "Serial #", "e.g. 12/99"],
];

function confClass(c) {
  return c >= 0.85 ? "hi" : c >= 0.6 ? "mid" : "lo";
}

function confCell(f) {
  if (!f || f.value == null || f.value === "" || f.value === false) return "<span></span>";
  if (f.confidence == null) {
    return f.side ? `<span class="c"><span class="src">${esc(SIDE_WORDS[f.side] || f.side)}</span></span>` : "<span></span>";
  }
  const pct = Math.round(f.confidence * 100);
  return `<span class="c ${confClass(f.confidence)}">${pct}%${f.side ? ` <span class="src">${esc(SIDE_WORDS[f.side] || f.side)}</span>` : ""}</span>`;
}

function fieldLabel(key) {
  const row = ROWS.find((r) => r[0] === key);
  return row ? row[1].toLowerCase() : key === "rookie" ? "rookie mark" : key.replace(/_/g, " ");
}

function reasonHtml(p) {
  const c = p.card;
  const parts = [];
  if (c.confidence != null && c.confidence < 1) {
    parts.push(`The card read at ${Math.round(c.confidence * 100)}% sure.`);
  }
  const fromBack = p.fields.filter((f) => f.side === "back" && f.value);
  if (fromBack.length) {
    const list = fromBack.map((f) => `${fieldLabel(f.field)}${f.confidence != null ? ` (${Math.round(f.confidence * 100)}%)` : ""}`);
    const joined = list.length > 1 ? list.slice(0, -1).join(", ") + " and " + list[list.length - 1] : list[0];
    parts.push(`The back supplied the ${joined}.`);
  }
  if (p.user_confirmed) parts.push("You confirmed this card before; something else still needs a look.");
  const reasons = plainReasons(p.review_reason);
  return `<div class="reason"><b>Why it's here:</b> ${esc(parts.join(" "))}${
    reasons.length ? `<ul>${reasons.map((r) => `<li>${esc(r)}</li>`).join("")}</ul>` : ""}
    ${!parts.length && !reasons.length ? "Check the details below." : ""}
    <div style="margin-top:4px">If this looks right, confirm it.</div></div>`;
}

function renderEmpty() {
  $("sub").textContent = R.done ? `All done. You reviewed ${plural(R.done, "card")}.` : "One card at a time. Confirm what's right, fix what isn't.";
  $("prog").querySelector("div").style.width = R.done ? "100%" : "0";
  $("review").innerHTML = `<div class="panel empty-state">
    <h2>Nothing to review. Nice.</h2>
    <p class="muted">Every card has been checked.</p>
    <a class="btn" href="/repository">Go to your collection</a></div>`;
}

function render() {
  const p = R.p;
  if (!p || !p.card) { renderEmpty(); return; }
  const c = p.card;
  const total = R.done + p.remaining;
  $("sub").textContent = `One card at a time. Confirm what's right, fix what isn't. ${R.done + 1} of ${total}.`;
  $("prog").querySelector("div").style.width = `${total ? (R.done / total) * 100 : 0}%`;
  const byField = Object.fromEntries(p.fields.map((f) => [f.field, f]));
  const front = p.front_crop_url ? `${p.front_crop_url}?v=${c.upload_id || ""}` : null;
  const back = p.back_crop_url ? `${p.back_crop_url}?v=${c.upload_id || ""}` : null;
  const zoom = [front, back].filter(Boolean).join("|");
  const meta = [
    c.batch_tag ? `Batch "${esc(c.batch_tag)}"` : "",
    c.reference_image_url ? `<a href="${esc(c.reference_image_url)}" data-zoom="${esc(c.reference_image_url)}">see market photo</a>` : "",
    `<a href="/card/${c.id}">open card page</a>`,
  ].filter(Boolean).join(" · ");

  const row = ([key, label, ph]) => {
    const val = key === "condition" ? c.condition : (byField[key] ? byField[key].value : c[key]);
    return `<div class="f"><label for="f_${key}">${label}</label>
      <input id="f_${key}" data-f="${key}" data-orig="${esc(val || "")}" value="${esc(val || "")}" placeholder="${esc(ph)}" autocomplete="off"/>
      ${confCell(byField[key])}</div>`;
  };

  $("review").innerHTML = `<div class="rv">
    <div class="panel">
      <div class="pics">
        <figure><div class="big">${front ? `<img src="${front}" alt="front" data-zoom="${zoom}"/>` : "no front photo"}</div><figcaption>Front (click to zoom)</figcaption></figure>
        <figure><div class="big">${back ? `<img src="${back}" alt="back" data-zoom="${zoom}"/>` : "no back photo"}</div><figcaption>Back</figcaption></figure>
      </div>
      <div style="margin-top:10px;font-size:12px;color:var(--mute)">${meta}</div>
      <div style="margin-top:10px;display:flex;gap:16px;flex-wrap:wrap">
        <div><div class="muted small">Value</div>${valueHtml(c)}</div>
        <div><div class="muted small">Would list at</div><div style="font-weight:600;margin-top:2px">${listAtHtml(c)}</div></div>
      </div>
    </div>
    <div class="panel">
      ${reasonHtml(p)}
      <form id="form" autocomplete="off" onsubmit="return false">
        ${ROWS.slice(0, 7).map(row).join("")}
        <div class="f"><label for="f_team">Team · RC</label>
          <input id="f_team" data-f="team" data-orig="${esc(c.team || "")}" value="${esc(c.team || "")}"/>
          <label class="check" style="justify-content:flex-end"><input type="checkbox" data-f="rookie" data-orig="${c.rookie ? "1" : ""}" ${c.rookie ? "checked" : ""}/> RC</label></div>
        ${ROWS.slice(8).map(row).join("")}
      </form>
      <div class="acts">
        <button class="btn" type="button" id="okBtn">Looks right<span class="kbd">Enter</span></button>
        <button class="btn ghost" type="button" id="againBtn">Re-analyze front + back<span class="kbd">R</span></button>
        <button class="btn ghost" type="button" id="skipBtn">Skip<span class="kbd">→</span></button>
        <button class="btn ghost" type="button" id="moreBtn" style="margin-left:auto">⋯ Not a card / This is a back</button>
      </div>
      <div class="muted small" id="msg" style="margin-top:8px"></div>
    </div>
  </div>`;

  $("form").querySelectorAll("[data-f]").forEach((inp) => {
    const mark = () => inp.classList.toggle("changed", changedValue(inp) !== undefined);
    inp.addEventListener("input", mark);
    inp.addEventListener("change", mark);
  });
  $("okBtn").addEventListener("click", looksRight);
  $("againBtn").addEventListener("click", readAgain);
  $("skipBtn").addEventListener("click", skip);
  $("moreBtn").addEventListener("click", (e) => {
    e.stopPropagation();
    openMenu(e.currentTarget, [
      { label: "Not a card (delete it)", danger: true, onClick: notACard },
      { label: "This is a back", onClick: isABack },
    ]);
  });
}

// The new value when an input differs from what it started with, else undefined.
function changedValue(inp) {
  if (inp.type === "checkbox") {
    return inp.checked !== Boolean(inp.dataset.orig) ? inp.checked : undefined;
  }
  const v = inp.value.trim();
  return v !== (inp.dataset.orig || "") ? v : undefined;
}

function setBusy(on, text = "") {
  R.busy = on;
  document.querySelectorAll(".acts button").forEach((b) => { b.disabled = on; });
  if ($("msg")) $("msg").textContent = text;
}

async function load(afterId) {
  const r = await api("/api/review/next" + (afterId != null ? `?after_id=${afterId}` : ""));
  if (!r.ok) { $("review").innerHTML = `<div class="panel">Could not load the review queue: ${esc(errText(r))}</div>`; return; }
  R.p = r.data;
  render();
  refreshReviewCount();
  window.scrollTo(0, 0);
}

async function looksRight() {
  if (R.busy || !R.p || !R.p.card) return;
  const c = R.p.card;
  const body = {};
  $("form").querySelectorAll("[data-f]").forEach((inp) => {
    const v = changedValue(inp);
    if (v !== undefined) body[inp.dataset.f] = v;
  });
  setBusy(true, Object.keys(body).length ? "Saving your changes and looking up the price..." : "Confirming...");
  if (Object.keys(body).length) {
    const r = await api(`/api/cards/${c.id}`, { method: "PATCH", json: body });
    if (!r.ok) { setBusy(false, "Could not save: " + errText(r)); return; }
  }
  const r = await api(`/api/cards/${c.id}/confirm`, { method: "POST" });
  if (!r.ok) { setBusy(false, "Could not confirm: " + errText(r)); return; }
  R.done += 1;
  const still = r.data.card && r.data.card.status === "needs_review";
  toast(still ? `${cardTitle(c)} saved. It still needs a look later: ${plainReasons(r.data.card.review_reason)[0] || "see the card page"}.` : `${cardTitle(c)} confirmed.`);
  setBusy(false);
  await load(c.id);
}

async function skip() {
  if (R.busy || !R.p || !R.p.card) return;
  await load(R.p.card.id);
}

async function readAgain() {
  if (R.busy || !R.p || !R.p.card) return;
  const c = R.p.card;
  setBusy(true, "Reading the front and back again. This can take a minute...");
  const r = await api(`/api/cards/${c.id}/reanalyze`, { method: "POST" });
  setBusy(false);
  if (!r.ok) { $("msg").textContent = "Re-analyze failed: " + errText(r); return; }
  toast("Read again.");
  await load(c.id - 1);  // the same card when it is still in review
}

async function notACard() {
  const c = R.p.card;
  const r = await api(`/api/cards/${c.id}`, { method: "DELETE" });
  if (!r.ok) { toast("Could not delete: " + errText(r)); return; }
  R.done += 1;
  toast(`Deleted ${cardTitle(c).replace(/\.$/, "")}.`, {
    action: "Undo",
    onAction: async () => {
      const u = await api(`/api/cards/${c.id}/restore`, { method: "POST" });
      if (!u.ok) { toast("Could not restore: " + errText(u)); return; }
      R.done = Math.max(0, R.done - 1);
      toast("Restored.");
      load(c.id - 1);
    },
  });
  load(c.id);
}

async function isABack() {
  const c = R.p.card;
  const ok = await confirmDialog("Is this the back of a card?",
    "It leaves your collection and joins its front if one matches. If none matches, it waits under Unmatched backs.",
    "Yes, it's a back");
  if (!ok) return;
  const r = await api(`/api/cards/${c.id}/mark-back?confirm=true`, { method: "POST" });
  if (!r.ok) { toast("Could not change it: " + errText(r)); return; }
  R.done += 1;
  toast(r.data.merged_into ? "Joined to its front." : "Moved to Unmatched backs.");
  load(c.id);
}

document.addEventListener("keydown", (e) => {
  if (e.metaKey || e.ctrlKey || e.altKey) return;
  if (document.querySelector(".overlay, .lightbox.show, .menu")) return;
  const t = e.target instanceof Element ? e.target : document.body;
  const typing = t.closest("input, textarea, select, [contenteditable]");
  if (e.key === "Enter") {
    if (t.closest("button, a")) return;  // let a focused button do its own thing
    e.preventDefault();
    looksRight();
    return;
  }
  if (typing) return;
  if (e.key === "r" || e.key === "R") { e.preventDefault(); readAgain(); }
  else if (e.key === "ArrowRight") { e.preventDefault(); skip(); }
});

(async () => {
  await initShell("review");
  await load(null);
})();
