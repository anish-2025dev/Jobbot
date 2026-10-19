// JobBot dashboard: Preact + htm, no build step.
import { h, render } from "preact";
import { useEffect, useState, useCallback } from "preact/hooks";
import htm from "htm";

const html = htm.bind(h);
const BUCKETS = { 3: "High", 2: "Medium", 1: "Low", 0: "Stretch" };
// Links come from job boards and mail; only http(s) may become an href (blocks javascript: URLs).
const safeUrl = (u) => { if (!u || typeof u !== "string") return null; try { const p = new URL(u, location.href); return p.protocol === "https:" || p.protocol === "http:" ? p.href : null; } catch { return null; } };

// ---------------------------------------------------------------- api
const store = {
  get() { try { return JSON.parse(localStorage.getItem("jobbot") || "null"); } catch { return null; } },
  set(v) { try { v ? localStorage.setItem("jobbot", JSON.stringify(v)) : localStorage.removeItem("jobbot"); } catch {} },
};
let session = store.get();

class ApiError extends Error {
  constructor(status, detail) { super(typeof detail === "string" ? detail : (detail?.reason || detail?.message || JSON.stringify(detail))); this.status = status; this.detail = detail; }
}

async function api(path, { method = "GET", body, headers = {}, raw = false } = {}) {
  const h = { ...headers };
  if (session?.token) h.Authorization = `Bearer ${session.token}`;
  let payload = body;
  if (body && !(body instanceof FormData)) { h["Content-Type"] = "application/json"; payload = JSON.stringify(body); }
  const r = await fetch(path, { method, headers: h, body: payload });
  if (r.status === 401 && session) { session = null; store.set(null); location.hash = "#/login"; }
  if (!r.ok) {
    let detail = r.statusText;
    try { detail = (await r.json()).detail; } catch {}
    throw new ApiError(r.status, detail);
  }
  if (raw) return r;
  return r.status === 204 ? null : r.json();
}
const u = (p) => `/users/${session.user_id}${p}`;

async function download(path, fallbackName) {
  const r = await api(path, { raw: true });
  const blob = await r.blob();
  const cd = r.headers.get("content-disposition") || "";
  const name = (cd.match(/filename="([^"]+)"/) || [])[1] || fallbackName;
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob); a.download = name; a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 2000);
}

// ---------------------------------------------------------------- helpers
const fmtMoney = (v, cur) => {
  if (v == null) return "";
  if (!cur || cur === "INR") return `₹${(v / 100000).toFixed(v % 100000 ? 1 : 0)} LPA`;
  return `${cur} ${Math.round(v / 1000)}k`;
};
const ago = (iso) => {
  if (!iso) return "";
  const h = (Date.now() - new Date(iso)) / 36e5;
  return h < 1 ? "just now" : h < 24 ? `${Math.round(h)}h ago` : `${Math.round(h / 24)}d ago`;
};
const hoursLeft = (iso) => Math.max(0, (new Date(iso) - Date.now()) / 36e5);
const when = (iso) => iso ? new Date(iso).toLocaleString([], { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" }) : "";

function useLoad(fn, deps = []) {
  const [state, set] = useState({ loading: true, data: null, error: null });
  const reload = useCallback(() => {
    set((s) => ({ ...s, loading: true }));
    fn().then((data) => set({ loading: false, data, error: null }), (error) => set({ loading: false, data: null, error }));
  }, deps);
  useEffect(reload, [reload]);
  return [state, reload];
}

let toastTimer;
function toast(msg) {
  let el = document.querySelector(".toast");
  if (!el) { el = document.createElement("div"); el.className = "toast"; el.setAttribute("role", "status"); document.body.appendChild(el); }
  el.textContent = msg; el.hidden = false;
  clearTimeout(toastTimer); toastTimer = setTimeout(() => { el.hidden = true; }, 3500);
}
const act = (fn, ok) => async (...a) => { try { const r = await fn(...a); if (ok) toast(typeof ok === "function" ? ok(r) : ok); return r; } catch (e) { toast(e.message); } };

const Loading = () => html`<p class="lede">Loading…</p>`;
const Failed = ({ error }) => html`<p class="notice error">${error.message}</p>`;
const Empty = ({ children }) => html`<div class="empty">${children}</div>`;

// ---------------------------------------------------------------- auth
function Auth({ onIn }) {
  const [mode, setMode] = useState("login");
  const [err, setErr] = useState("");
  const submit = async (e) => {
    e.preventDefault();
    const f = new FormData(e.target);
    const body = { email: f.get("email"), password: f.get("password") };
    if (mode === "signup") body.display_name = f.get("name") || null;
    try {
      const r = await api(`/auth/${mode}`, { method: "POST", body });
      session = { user_id: r.user_id, token: r.token }; store.set(session); onIn();
    } catch (e2) { setErr(e2.detail?.password ? `Password needs: ${e2.detail.password.join(", ")}` : e2.message); }
  };
  return html`<div class="auth">
    <h1>JobBot</h1>
    <p class="lede">Finds matching jobs, tailors a truthful resume for each, and applies for you within safe limits.</p>
    ${err && html`<p class="notice error">${err}</p>`}
    <form onSubmit=${submit}>
      ${mode === "signup" && html`<label for="nm">Your name</label><input id="nm" name="name" type="text" autocomplete="name" />`}
      <label for="em">Email</label><input id="em" name="email" type="email" required autocomplete="email" />
      <label for="pw">Password</label><input id="pw" name="password" type="password" required minlength=${mode === "signup" ? 10 : 1}
        autocomplete=${mode === "signup" ? "new-password" : "current-password"} />
      <p><button class="btn primary" type="submit">${mode === "login" ? "Sign in" : "Create account"}</button></p>
    </form>
    <p><button class="btn" onClick=${() => { setErr(""); setMode(mode === "login" ? "signup" : "login"); }}>
      ${mode === "login" ? "New here? Create an account" : "Have an account? Sign in"}</button></p>
  </div>`;
}

// ---------------------------------------------------------------- today
function Today({ go }) {
  const [sum] = useLoad(() => api(u("/summary")), []);
  const [pos, reloadPos] = useLoad(() => api(u("/responses")), []);
  const [wl] = useLoad(() => api(u("/waiting")), []);
  const [ref] = useLoad(() => api(u("/referrals")), []);
  const [chk] = useLoad(() => api(u("/onboarding")), []);
  const drafts = (ref.data || []).filter((r) => r.status === "awaiting_approval");
  const todo = (chk.data || []).filter((c) => !c.done && c.step !== "email_finders");
  return html`
    <h1>Today</h1>
    <p class="lede">Replies that need you, jobs the bot couldn't finish, and referral drafts waiting for approval.</p>
    ${sum.data && html`<div class="stats">
      <div><strong>${sum.data.positives_open}</strong>replies to handle</div>
      <div><strong>${sum.data.waiting}</strong>waiting for you</div>
      <div><strong>${sum.data.applied_7d}</strong>applied this week</div>
      <div><strong>${sum.data.ranked_jobs}</strong>ranked jobs</div>
    </div>`}
    ${todo.length > 0 && html`<div class="panel"><strong>Finish setting up</strong>
      <ul class="check">${(chk.data || []).map((c) => html`<li class=${c.done ? "done" : ""}>${label(c.step)}: <span class="meta">${c.detail}</span></li>`)}</ul>
      <button class="btn" onClick=${() => go(todo[0].step === "vault" ? "vault" : "settings")}>Continue setup</button></div>`}
    <h2>Replies</h2>
    ${pos.loading ? html`<${Loading}/>` : (pos.data || []).length === 0 ? html`<${Empty}>No replies waiting. Interviews, tests and offers show up here the moment they arrive.<//>`
      : html`<ul class="list">${pos.data.map((r) => html`<${Reply} r=${r} reload=${reloadPos}/>`)}</ul>`}
    <h2>Waiting for you</h2>
    ${wl.loading ? html`<${Loading}/>` : (wl.data || []).length === 0 ? html`<${Empty}>Nothing waiting. Jobs land here when they need a CAPTCHA, an answer, or a manual apply.<//>`
      : html`<ul class="list">${wl.data.slice(0, 5).map((w) => html`<${WaitingRow} w=${w} compact/>`)}</ul>
             ${wl.data.length > 5 && html`<p><button class="btn" onClick=${() => go("waiting")}>See all ${wl.data.length}</button></p>`}`}
    ${drafts.length > 0 && html`<h2>Referral drafts</h2><p class="lede">${drafts.length} ${drafts.length === 1 ? "job has" : "jobs have"} emails to approve.</p>
      <button class="btn primary" onClick=${() => go("referrals")}>Review drafts</button>`}
  `;
}
const label = (s) => ({ vault: "Vault", mail: "Connect mail", telegram: "Link Telegram", email_finders: "Email finders", auto_apply: "Auto-apply" }[s] || s);

function Reply({ r, reload }) {
  const kinds = { offer: "Offer", interview: "Interview", test: "Test", recruiter: "Recruiter", info_request: "Info requested",
                  referral_given: "Referral", outreach_question: "Referral question", unclear: "Possible reply" };
  const done = act(() => api(u(`/responses/${r.id}/done`), { method: "POST" }).then(reload), "Marked done. Kept at least 7 days.");
  return html`<li class="row" data-bucket=${r.kind === "offer" ? 3 : 2}>
    <div><div class="title">${kinds[r.kind] || r.kind}${r.company_name ? `: ${r.company_name}` : ""}</div>
      <div class="meta">${r.title || ""}</div>${r.snippet && html`<div class="meta">"${r.snippet.slice(0, 160)}"</div>`}</div>
    <div class="side">${r.deadline ? html`<strong>Due ${when(r.deadline)}</strong>` : ago(r.received_at)}</div>
    <div class="actions">${safeUrl(r.gmail_url) && html`<a class="btn" href=${safeUrl(r.gmail_url)} target="_blank" rel="noopener">Open in Gmail</a>`}
      <button class="btn" onClick=${done}>Mark done</button></div>
  </li>`;
}

// ---------------------------------------------------------------- ranked jobs
function Jobs() {
  const [bucket, setBucket] = useState("all");
  const [q, setQ] = useState("");
  const [state, reload] = useLoad(() => api(u(`/jobs?bucket=${bucket}${q ? `&q=${encodeURIComponent(q)}` : ""}`)), [bucket, q]);
  const counts = state.data?.counts || {};
  return html`
    <h1>Ranked jobs</h1>
    <p class="lede">Best chance of success first, then salary, then freshness. Stretch jobs are shown with the skills you'd need; they're never applied to automatically.</p>
    <div class="toolbar">
      <div class="seg" role="group" aria-label="Bucket">${["all", "high", "medium", "low", "stretch"].map((b) => html`
        <button aria-pressed=${bucket === b} onClick=${() => setBucket(b)}>${b[0].toUpperCase() + b.slice(1)}${b !== "all" && counts[b] ? ` ${counts[b]}` : ""}</button>`)}</div>
      <input type="search" placeholder="Filter by title or company" aria-label="Filter" value=${q}
        onInput=${(e) => setQ(e.target.value)} style="max-width:18rem" />
    </div>
    ${state.loading ? html`<${Loading}/>` : state.error ? html`<${Failed} error=${state.error}/>` :
      state.data.items.length === 0 ? html`<${Empty}>No ranked jobs yet. The bot polls company boards every two hours; new matches appear here.<//>` :
      html`<ul class="list">${state.data.items.map((j) => html`<${JobRow} j=${j} reload=${reload}/>`)}</ul>`}
  `;
}

function JobRow({ j, reload }) {
  const [open, setOpen] = useState(false);
  const applied = act(() => api(u(`/jobs/${j.id}/applied`), { method: "POST" }).then(reload), "Recorded as applied.");
  const queue = act(() => api(u(`/jobs/${j.id}/queue`), { method: "POST" }).then(reload), "Queued for auto-apply.");
  const resume = act((fmt) => download(u(`/jobs/${j.id}/resume.${fmt}`), `resume.${fmt}`));
  return html`<li class="row" data-bucket=${j.bucket}>
    <div>
      <div class="title">${j.title}</div>
      <div class="meta">${j.company_name}${j.location ? `, ${j.location}` : ""}${j.remote_type !== "unknown" ? `, ${j.remote_type}` : ""}</div>
      ${j.bucket === 0 && j.missing_skills?.length > 0 && html`<div class="chips">${j.missing_skills.map((s) => html`<span class="chip missing">${s}</span>`)}
        <span class="meta">about ${j.prep_days} days of prep</span></div>`}
    </div>
    <div class="side num">
      <div class="bucket-name">${BUCKETS[j.bucket]}</div>
      <div><span class="score">${Math.round(j.score * 100)}</span> match</div>
      <div>${fmtMoney(j.salary_max, j.salary_currency)}</div>
      <div class="meta">${ago(j.posted_at)}</div>
    </div>
    <div class="actions">
      ${safeUrl(j.url) && html`<a class="btn" href=${safeUrl(j.url)} target="_blank" rel="noopener">Open posting</a>`}
      <button class="btn" onClick=${() => resume("pdf")}>Resume PDF</button>
      <button class="btn" onClick=${() => resume("docx")}>DOCX</button>
      ${j.app_status && j.app_status !== "waiting" ? html`<span class="meta">Status: ${j.app_status}</span>` : html`
        ${j.bucket >= 1 && html`<button class="btn" onClick=${queue}>Queue for auto-apply</button>`}
        <button class="btn primary" onClick=${applied}>I applied</button>`}
      <button class="btn" aria-expanded=${open} onClick=${() => setOpen(!open)}>${open ? "Less" : "Details"}</button>
    </div>
    ${open && html`<div class="detail"><p>${j.summary}</p><div class="chips">${(j.skills || []).map((s) => html`<span class="chip">${s}</span>`)}</div></div>`}
  </li>`;
}

// ---------------------------------------------------------------- waiting list
function Waiting() {
  const [state, reload] = useLoad(() => api(u("/waiting")), []);
  return html`
    <h1>Waiting for you</h1>
    <p class="lede">Up to 30 of your best jobs the bot couldn't finish. Each stays 24 hours; pin one to keep it another day.</p>
    ${state.loading ? html`<${Loading}/>` : state.error ? html`<${Failed} error=${state.error}/>` :
      state.data.length === 0 ? html`<${Empty}>Nothing waiting right now.<//>` :
      html`<ul class="list">${state.data.map((w) => html`<${WaitingRow} w=${w} reload=${reload}/>`)}</ul>`}
  `;
}

function WaitingRow({ w, reload, compact }) {
  const [open, setOpen] = useState(false);
  const p = w.ready_pack || {};
  const left = hoursLeft(w.expires_at);
  const pct = Math.min(100, (left / 24) * 100);
  const unresolved = p.unresolved_questions || [];
  const pin = act(() => api(u(`/waiting/${w.id}/pin`), { method: "POST" }).then(reload), "Pinned for another 24 hours.");
  const applied = act(() => api(u(`/waiting/${w.id}/applied`), { method: "POST" }).then(reload), "Recorded as applied.");
  const dismiss = act(() => api(u(`/waiting/${w.id}`), { method: "DELETE" }).then(reload), "Dismissed.");
  const resume = act((fmt) => download(u(`/applications/${w.id}/resume.${fmt}`), `resume.${fmt}`));
  const answer = act(async (e) => {
    e.preventDefault();
    const f = new FormData(e.target);
    const answers = Object.fromEntries(unresolved.map((q, i) => [q, f.get(`a${i}`)]).filter(([, a]) => a));
    await api(u(`/waiting/${w.id}/answers`), { method: "POST", body: { answers } });
    reload && reload();
  }, "Saved. The bot will retry this application and reuse your answers.");
  return html`<li class="row" data-bucket=${w.bucket}>
    <div><div class="title">${w.title}</div>
      <div class="meta">${w.company_name}${w.failure_reason ? `. ${w.failure_reason}` : ""}</div></div>
    <div class="side num"><div class="bucket-name">${BUCKETS[w.bucket] || ""}</div>
      ${w.ready_at ? html`<div>Ready at ${when(w.ready_at)}</div>` : html`<div>${Math.round(left)}h left</div>`}
      ${w.salary_max && html`<div>${fmtMoney(w.salary_max)}</div>`}</div>
    <div class=${`drain ${left < 4 ? "urgent" : ""}`} role="img" aria-label=${`${Math.round(left)} of 24 hours left`}><span style=${`width:${pct}%`}></span></div>
    ${!compact && html`<div class="actions">
      ${safeUrl(w.apply_url) && html`<a class="btn primary" href=${safeUrl(w.apply_url)} target="_blank" rel="noopener">Open application</a>`}
      <button class="btn" onClick=${() => resume("pdf")}>Resume PDF</button>
      <button class="btn" onClick=${() => resume("docx")}>DOCX</button>
      <button class="btn" onClick=${applied} disabled=${!!w.ready_at}>I applied</button>
      <button class="btn" onClick=${pin}>Pin 24h</button>
      <button class="btn" aria-expanded=${open} onClick=${() => setOpen(!open)}>${open ? "Hide Ready Pack" : "Ready Pack"}</button>
      <button class="btn danger" onClick=${dismiss}>Dismiss</button>
    </div>`}
    ${open && html`<div class="detail">
      ${unresolved.length > 0 && html`<form onSubmit=${answer} class="panel"><strong>The form asked something your vault doesn't answer</strong>
        ${unresolved.map((q, i) => html`<label for=${`a${w.id}-${i}`}>${q}</label><textarea id=${`a${w.id}-${i}`} name=${`a${i}`} required></textarea>`)}
        <p><button class="btn primary" type="submit">Save answers and retry</button></p></form>`}
      <dl class="pack">${Object.entries(p.copy || {}).map(([k, v]) => html`<dt>${k}</dt><dd>${v}</dd>
        <dd><button class="btn" onClick=${() => navigator.clipboard.writeText(String(v)).then(() => toast(`Copied ${k}`))}>Copy</button></dd>`)}</dl>
      ${p.cover_letter && html`<p><strong>Cover letter</strong> <button class="btn" onClick=${() => navigator.clipboard.writeText(p.cover_letter).then(() => toast("Copied cover letter"))}>Copy</button></p>
        <div class="letter">${p.cover_letter}</div>`}
      ${p.gaps?.length > 0 && html`<p><strong>Skills this job asks for that your vault doesn't list</strong></p>
        <ul>${p.gaps.map((g) => html`<li>${g.skill}${g.related_evidence.length ? `: closest real work. ${g.related_evidence.map((e) => `${e.from} (${e.related})`).join("; ")}` : ""}</li>`)}</ul>`}
    </div>`}
  </li>`;
}

// ---------------------------------------------------------------- referrals
function Referrals() {
  const [state, reload] = useLoad(() => api(u("/referrals")), []);
  return html`
    <h1>Referral outreach</h1>
    <p class="lede">For your best jobs the bot emails up to three people at the company from your Gmail, waits 24 hours, then applies with or without the referral.</p>
    ${state.loading ? html`<${Loading}/>` : state.error ? html`<${Failed} error=${state.error}/>` :
      state.data.length === 0 ? html`<${Empty}>No referral asks yet. Turn on outreach in Settings and add an email-finder key.<//>` :
      html`<ul class="list">${state.data.map((r) => html`<${ReferralRow} r=${r} reload=${reload}/>`)}</ul>`}
  `;
}

const RQ = { finding: "Finding people", awaiting_approval: "Drafts to approve", scheduled: "Scheduled", sent: "Sent, waiting",
             referred: "Referred", applying: "Applying", applied: "Applied", closed: "Job closed", skipped: "Skipped" };

function ReferralRow({ r, reload }) {
  const job = r.job || {};
  return html`<li class="row" data-bucket="3">
    <div><div class="title">${job.title} at ${job.company}</div>
      <div class="meta">${RQ[r.status] || r.status}${r.apply_at ? `, applies ${when(r.apply_at)}` : ""}${job.skip_reason ? `. ${job.skip_reason}` : ""}</div></div>
    <div class="side">${r.referral ? html`<strong>Referred by ${r.referral.by}</strong>` : ""}</div>
    <div class="detail">${(r.contacts || []).map((c, i) => html`<${Draft} r=${r} c=${c} i=${i} reload=${reload}/>`)}</div>
  </li>`;
}

function Draft({ r, c, i, reload }) {
  const [edit, setEdit] = useState(false);
  const [body, setBody] = useState(c.body);
  const decide = act((approve) => api(u(`/referrals/${r.id}/contacts/${i}`), { method: "POST",
    body: { approve, ...(edit ? { body } : {}) } }).then(reload), (x) => x.result);
  const pending = r.status === "awaiting_approval" && c.status === "drafted";
  const words = body.trim().split(/\s+/).length;
  return html`<div class="panel">
    <div><strong>${c.name}</strong> <span class="meta">${c.title}, ${c.role}, ${c.status}${c.sent_at ? ` ${when(c.sent_at)}` : ""}</span></div>
    <div class="meta">Subject: ${c.subject}</div>
    ${edit ? html`<label for=${`d${r.id}-${i}`}>Email (${words} words, must stay under 120)</label>
      <textarea id=${`d${r.id}-${i}`} value=${body} onInput=${(e) => setBody(e.target.value)} rows="10"></textarea>`
      : html`<div class="letter">${c.body}</div>`}
    ${pending && html`<div class="toolbar" style="margin-top:.6rem">
      <button class="btn primary" onClick=${() => decide(true)}>${edit ? "Save and approve" : "Approve"}</button>
      <button class="btn" onClick=${() => setEdit(!edit)}>${edit ? "Cancel edit" : "Edit"}</button>
      <button class="btn" onClick=${() => decide(false)}>Don't send</button></div>`}
  </div>`;
}

// ---------------------------------------------------------------- applications
function Applications() {
  const [status, setStatus] = useState("");
  const [state] = useLoad(() => api(u(`/applications${status ? `?status=${status}` : ""}`)), [status]);
  const statuses = ["", "applied", "positive", "queued", "waiting", "applied_pending_referral", "closed", "failed"];
  return html`
    <h1>Applications</h1>
    <div class="toolbar"><label for="st" style="margin:0">Status</label>
      <select id="st" style="max-width:16rem" value=${status} onChange=${(e) => setStatus(e.target.value)}>
        ${statuses.map((s) => html`<option value=${s}>${s ? s.replaceAll("_", " ") : "All"}</option>`)}</select></div>
    ${state.loading ? html`<${Loading}/>` : state.error ? html`<${Failed} error=${state.error}/>` :
      state.data.length === 0 ? html`<${Empty}>No applications in this view.<//>` :
      html`<ul class="list">${state.data.map((a) => html`<li class="row" data-bucket=${a.bucket}>
        <div><div class="title">${a.title}</div><div class="meta">${a.company_name}${a.via_referral ? `, referred by ${a.referrer}` : ""}
          ${a.failure_reason ? `. ${a.failure_reason}` : ""}</div></div>
        <div class="side"><div>${(a.status || "").replaceAll("_", " ")}</div><div class="meta">${a.applied_at ? when(a.applied_at) : a.not_before ? `after ${when(a.not_before)}` : ""}</div></div>
      </li>`)}</ul>`}
  `;
}

// ---------------------------------------------------------------- vault
function VaultEditor() {
  const [text, setText] = useState("");
  const [rev, setRev] = useState(null);
  const [msg, setMsg] = useState(null);
  const [warnings, setWarnings] = useState([]);
  const load = async () => {
    try {
      const r = await api(u("/vault"));
      setText(JSON.stringify(r.data, null, 2)); setRev(r.revision); setWarnings(r.warnings);
    } catch (e) {
      if (e.status === 404) { const ex = await fetch("example-vault.json").then((x) => x.ok ? x.text() : "{}").catch(() => "{}"); setText(ex); setRev(null); }
      else setMsg({ error: true, text: e.message });
    }
  };
  useEffect(() => { load(); }, []);
  const parse = () => { try { return JSON.parse(text); } catch (e) { setMsg({ error: true, text: `Not valid JSON: ${e.message}` }); return null; } };
  const validate = async () => {
    const data = parse(); if (!data) return;
    const r = await api(u("/vault/validate"), { method: "POST", body: data });
    setWarnings(r.warnings);
    setMsg(r.valid ? { text: "Valid. Nothing saved yet." } : { error: true, text: r.errors.map((e) => `${e.loc.join(".")}: ${e.msg}`).join("\n") });
  };
  const save = async () => {
    const data = parse(); if (!data) return;
    try {
      const r = await api(u("/vault"), { method: "PUT", body: data, headers: rev ? { "If-Match": `"${rev}"` } : {} });
      setRev(r.revision); setWarnings(r.warnings); setMsg({ text: `Saved (revision ${r.revision}).` });
    } catch (e) {
      const errs = Array.isArray(e.detail) ? e.detail.map((x) => `${x.loc.join(".")}: ${x.msg}`).join("\n") : e.message;
      setMsg({ error: true, text: e.status === 409 ? "Someone changed the vault in another tab. Reload to get the latest." : errs });
    }
  };
  const importResume = async (e) => {
    const f = e.target.files[0]; if (!f) return;
    const fd = new FormData(); fd.append("file", f);
    try {
      const r = await api(u("/onboarding/import"), { method: "POST", body: fd });
      setText(JSON.stringify(r.draft, null, 2)); setWarnings(r.warnings);
      setMsg(r.errors.length ? { error: true, text: `Draft from your resume. Fill these before saving:\n${r.errors.map((x) => `${x.loc.join(".")}: ${x.msg}`).join("\n")}` }
                             : { text: "Draft from your resume. Review every line, then save." });
    } catch (err) { setMsg({ error: true, text: err.message }); }
  };
  return html`
    <h1>Vault</h1>
    <p class="lede">The only source for your resumes. Every skill, number and claim on any resume or email must come from here, so add real metrics to your bullets.</p>
    <div class="toolbar">
      <button class="btn primary" onClick=${save}>Save vault</button>
      <button class="btn" onClick=${validate}>Check</button>
      <label class="btn" style="margin:0">Import from resume <input type="file" accept=".pdf,.docx,.txt" onChange=${importResume} hidden /></label>
      ${rev && html`<span class="meta">Revision ${rev}</span>`}
    </div>
    ${msg && html`<p class=${`notice ${msg.error ? "error" : ""}`} style="white-space:pre-wrap">${msg.text}</p>`}
    ${warnings.length > 0 && html`<details open><summary>${warnings.length} suggestions</summary><ul>${warnings.map((w) => html`<li><span class="meta">${w.path}</span> ${w.message}</li>`)}</ul></details>`}
    <label for="vault-json">Vault JSON</label>
    <textarea id="vault-json" class="code" spellcheck="false" value=${text} onInput=${(e) => setText(e.target.value)}></textarea>
  `;
}

// ---------------------------------------------------------------- settings
function Settings() {
  const [st, reload] = useLoad(() => api(u("/settings")), []);
  const [creds, reloadCreds] = useLoad(() => api(u("/credentials")), []);
  const [code, setCode] = useState(null);
  const connected = new Set((creds.data || []).map((c) => c.host));
  const patch = act((body) => api(u("/settings"), { method: "PATCH", body }).then(reload), "Settings saved.");
  const saveCred = (kind, fields) => act(async (e) => {
    e.preventDefault();
    const f = new FormData(e.target);
    const secret = Object.fromEntries(fields.map((k) => [k, f.get(k)]));
    await api(u(`/credentials/${kind}`), { method: "PUT", body: { username: f.get("username") || "", secret,
      meta: f.get("monthly_limit") ? { monthly_limit: Number(f.get("monthly_limit")) } : {} } });
    e.target.reset(); reloadCreds();
  }, "Saved and encrypted.");
  const gmail = act(async () => { const r = await api(u("/gmail/connect")); location.href = r.url; });
  const link = act(async () => { const r = await api(u("/telegram/link"), { method: "POST" }); setCode(r.code); });
  const s = st.data || {};
  const Toggle = ({ k, title, hint }) => html`<div class="switch"><input type="checkbox" id=${k} checked=${!!s[k]}
      onChange=${(e) => patch({ [k]: e.target.checked })} /><label for=${k} style="margin:0">${title}<span class="hint">${hint}</span></label></div>`;
  return html`
    <h1>Settings</h1>
    ${st.loading ? html`<${Loading}/>` : html`
    <div class="panel"><strong>Automation</strong>
      <${Toggle} k="auto_apply" title="Apply automatically" hint="Off: you get a ranked list and apply yourself. On: the bot applies to Low-bucket jobs and above, within 3 per company per 30 days and 3 hours apart."/>
      <${Toggle} k="outreach" title="Referral outreach" hint="Emails 2–3 people for your top jobs from your own Gmail. Drafts need your approval for the first week."/>
      ${s.outreach && html`<div class="switch"><input type="checkbox" id="om" checked=${s.outreach_mode === "auto"}
        onChange=${(e) => patch({ outreach_mode: e.target.checked ? "auto" : "review" })} /><label for="om" style="margin:0">Send approved-style drafts without asking
        <span class="hint">Only takes effect after the first week of review mode.</span></label></div>`}
      <${Toggle} k="paused" title="Pause everything" hint="Stops auto-apply and outreach. Ranking continues."/>
      <div class="field-row">
        <div><label for="ms">Minimum match to auto-apply (default 0.60)</label><input id="ms" type="number" step="0.01" min="0.5" max="0.95"
          value=${s.min_score ?? 0.6} onChange=${(e) => patch({ min_score: Number(e.target.value) })} /></div>
        <div><label for="tz">Time zone</label><input id="tz" type="text" value=${s.timezone || ""} onChange=${(e) => patch({ timezone: e.target.value })} /></div>
      </div>
      ${s.weights && html`<p class="meta">Scoring weights tuned from your outcomes: ${Object.entries(s.weights).map(([k, v]) => `${k} ${Math.round(v * 100)}%`).join(", ")}</p>`}
    </div>`}
    <div class="panel"><strong>Mail</strong>
      <p class="meta">${connected.has("gmail") ? "Gmail connected." : connected.has("imap") ? "IMAP connected." : "Not connected. The bot needs mail to confirm applications, catch replies and send referral emails."}</p>
      <details><summary>Connect Gmail with your own Google Cloud OAuth client</summary>
        <p class="meta">Create an OAuth client in Google Cloud (Web application, redirect URI ${location.origin}/oauth/google/callback), publish the app "In production", then paste its ID and secret.</p>
        <form onSubmit=${saveCred("gmail_client", ["client_id", "client_secret"])}><div class="field-row">
          <div><label for="gci">Client ID</label><input id="gci" name="client_id" type="text" required /></div>
          <div><label for="gcs">Client secret</label><input id="gcs" name="client_secret" type="password" required /></div></div>
          <p><button class="btn" type="submit">Save client</button> ${connected.has("gmail_client") && html`<button class="btn primary" type="button" onClick=${gmail}>Connect Gmail</button>`}</p></form>
      </details>
      <details><summary>Or use IMAP with an app password</summary>
        <form onSubmit=${saveCred("imap", ["password"])}><div class="field-row">
          <div><label for="iu">Mailbox address</label><input id="iu" name="username" type="email" required /></div>
          <div><label for="ip">App password</label><input id="ip" name="password" type="password" required /></div></div>
          <p><button class="btn" type="submit">Save</button></p></form></details>
    </div>
    <div class="panel"><strong>Telegram alerts</strong>
      <p class="meta">Instant alerts for interviews and offers, and one-tap approvals.</p>
      ${code ? html`<p>Send <strong class="num">/start ${code}</strong> to your JobBot Telegram bot.</p>` : html`<button class="btn" onClick=${link}>Get link code</button>`}
    </div>
    <div class="panel"><strong>Email finders</strong> <span class="meta">(for referral outreach, your own free-tier keys)</span>
      ${[["hunter", "Hunter", ["api_key"]], ["apollo", "Apollo", ["api_key"]], ["snov", "Snov.io", ["client_id", "client_secret"]]].map(([k, name, fields]) => html`
        <details><summary>${name} ${connected.has(k) ? "(connected)" : ""}</summary>
          <form onSubmit=${saveCred(k, fields)}><div class="field-row">${fields.map((f) => html`<div><label for=${k + f}>${f.replace("_", " ")}</label><input id=${k + f} name=${f} type="password" required /></div>`)}
            <div><label for=${k + "lim"}>Monthly credit limit (optional)</label><input id=${k + "lim"} name="monthly_limit" type="number" min="0" /></div></div>
            <p><button class="btn" type="submit">Save ${name} key</button></p></form></details>`)}
    </div>
    <${MailLog}/>
  `;
}

function MailLog() {
  const [state, reload] = useLoad(() => api(u("/mail-actions?limit=30")), []);
  const undo = act((id) => api(u(`/mail-actions/${id}/undo`), { method: "POST" }).then(reload), "Restored from Trash.");
  if (!state.data || state.data.length === 0) return null;
  const names = { trash: "Trashed", star_label: "Starred", restore: "Restored" };
  return html`<div class="panel"><strong>Recent mail actions</strong> <span class="meta">(undo within 30 days)</span>
    <ul class="list" style="margin-top:.5rem">${state.data.map((a) => html`<li class="row"><div>${names[a.action]} <span class="meta">${a.gmail_id}, ${when(a.acted_at)}</span></div>
      <div class="side">${a.action === "trash" && !a.undone_at ? html`<button class="btn" onClick=${() => undo(a.id)}>Undo</button>` : a.undone_at ? "Undone" : ""}</div></li>`)}</ul></div>`;
}

// ---------------------------------------------------------------- shell
const VIEWS = { today: ["Today", Today], jobs: ["Ranked jobs", Jobs], waiting: ["Waiting", Waiting], referrals: ["Referrals", Referrals],
                applications: ["Applications", Applications], vault: ["Vault", VaultEditor], settings: ["Settings", Settings] };

function App() {
  const read = () => (location.hash.replace(/^#\//, "") || "today").split("/")[0];
  const [view, setView] = useState(read());
  const [, force] = useState(0);
  useEffect(() => { const f = () => setView(read()); addEventListener("hashchange", f); return () => removeEventListener("hashchange", f); }, []);
  const go = (v) => { location.hash = `#/${v}`; };
  if (!session) return html`<${Auth} onIn=${() => { go("today"); force((n) => n + 1); }}/>`;
  const [title, View] = VIEWS[view] || VIEWS.today;
  return html`<div class="shell">
    <nav class="rail" aria-label="Sections">
      <div class="brand">JobBot<small>your job search, ranked</small></div>
      ${Object.entries(VIEWS).map(([k, [t]]) => html`<button aria-current=${k === view ? "page" : undefined} onClick=${() => go(k)}>${t}</button>`)}
      <button class="signout" onClick=${() => { session = null; store.set(null); force((n) => n + 1); }}>Sign out</button>
    </nav>
    <main id="main" aria-label=${title}><${View} go=${go}/></main>
  </div>`;
}

const root = document.getElementById("root");
root.textContent = "";
render(html`<${App}/>`, root);
