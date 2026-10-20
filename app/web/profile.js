// Profile editor: the vault, one section at a time, as a form.
// Required sections are marked; projects, roles, experience and skills are lists you add to.
// Identity documents (PAN, Aadhaar, passport...) save separately, encrypted, and only ever come back masked.
import { h } from "preact";
import { useEffect, useState } from "preact/hooks";
import htm from "htm";

const html = htm.bind(h);

// ------------------------------------------------------------ data helpers
const getIn = (o, path) => path.reduce((a, k) => (a == null ? undefined : a[k]), o);
const setIn = (o, path, v) => {
  if (!path.length) return v;
  const [k, ...rest] = path;
  const base = Array.isArray(o) ? [...o] : { ...(o || {}) };
  base[k] = setIn(o == null ? undefined : o[k], rest, v);
  return base;
};
// Drop empty strings, nulls and empty objects so optional fields stay unset (the server never guesses them).
function clean(v) {
  if (Array.isArray(v)) return v.map(clean).filter((x) => x !== undefined);
  if (v && typeof v === "object") {
    const out = {};
    for (const [k, x] of Object.entries(v)) { const c = clean(x); if (c !== undefined) out[k] = c; }
    return Object.keys(out).length ? out : undefined;
  }
  return v === "" || v === null || v === undefined ? undefined : v;
}
const slug = (s) => (s || "").toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 40) || "item";
function uniq(base, taken) { let id = base, n = 2; while (taken.has(id)) id = `${base}-${n++}`.slice(0, 48); taken.add(id); return id; }

// Ids are internal: give new entries one from their name, never rename existing ones (roles refer to them).
function assignIds(d) {
  const taken = new Set();
  const owners = [...(d.experience || []), ...(d.projects || [])];
  owners.forEach((o) => { if (o.id) taken.add(o.id); (o.bullets || []).forEach((b) => b.id && taken.add(b.id)); });
  (d.achievements || []).forEach((a) => a.id && taken.add(a.id));
  const fix = (list, prefix, name) => (list || []).map((o) => {
    const id = o.id || uniq(`${prefix}-${slug(name(o))}`.slice(0, 40), taken);
    const bullets = (o.bullets || []).map((b, i) => ({ ...b, id: b.id || uniq(`${id.slice(0, 40)}-${i + 1}`, taken) }));
    return { ...o, id, bullets };
  });
  const roleIds = new Set((d.target_roles || []).map((r) => r.id).filter(Boolean));
  const roles = (d.target_roles || []).map((r) => ({ ...r, id: r.id || uniq(slug(r.name), roleIds) }));
  const experience = fix(d.experience, "exp", (o) => `${o.company || ""} ${o.title || ""}`);
  const projects = fix(d.projects, "proj", (o) => o.name);
  // A removed project or role must not linger in the lists that point at it.
  const pids = new Set(projects.map((p) => p.id));
  const rids = new Set(roles.map((r) => r.id));
  const keepRoles = (o) => ({ ...o, roles: (o.roles || []).filter((x) => rids.has(x)) });
  return {
    ...d,
    experience: experience.map(keepRoles),
    projects: projects.map(keepRoles),
    achievements: (d.achievements || []).map((a) => ({ ...a, id: a.id || uniq("ach", taken) })),
    target_roles: roles.map((r) => ({ ...r, projects: (r.projects || []).filter((x) => pids.has(x)) })),
  };
}

const BLANK = {
  personal: { location: { country: "India" }, links: [] },
  education: [{ level: "bachelors" }], experience: [], projects: [{}], skills: [{ category: "Languages", items: [] }],
  target_roles: [{}], achievements: [], certifications: [], extra_keywords: [],
  background: { eeo_disclosure: "decline", languages: [] }, standard_answers: {},
  expectations: { locations: [], remote_types: ["onsite", "hybrid", "remote"], role_types: [], experience_level: "fresher", blacklisted_companies: [] },
};

// ------------------------------------------------------------ inputs
const idOf = (path) => "f-" + path.join("-");
const Req = () => html`<span class="req" aria-hidden="true"> *</span>`;

function Text({ f, path, label, type = "text", required, hint, placeholder, list }) {
  const id = idOf(path);
  return html`<div class="fld"><label for=${id}>${label}${required && html`<${Req}/>`}</label>
    <input id=${id} type=${type} required=${required} placeholder=${placeholder || ""} list=${list}
      value=${getIn(f.data, path) ?? ""} onInput=${(e) => f.set(path, e.target.value)} />
    ${hint && html`<span class="hint">${hint}</span>`}</div>`;
}

function Num({ f, path, label, required, hint, step = "any", min, max }) {
  const id = idOf(path);
  const v = getIn(f.data, path);
  return html`<div class="fld"><label for=${id}>${label}${required && html`<${Req}/>`}</label>
    <input id=${id} type="number" step=${step} min=${min} max=${max} required=${required} value=${v ?? ""}
      onInput=${(e) => f.set(path, e.target.value === "" ? null : Number(e.target.value))} />
    ${hint && html`<span class="hint">${hint}</span>`}</div>`;
}

function Sel({ f, path, label, options, required, hint }) {
  const id = idOf(path);
  return html`<div class="fld"><label for=${id}>${label}${required && html`<${Req}/>`}</label>
    <select id=${id} value=${getIn(f.data, path) ?? ""} onChange=${(e) => f.set(path, e.target.value || null)}>
      <option value="">—</option>${options.map(([v, t]) => html`<option value=${v}>${t}</option>`)}</select>
    ${hint && html`<span class="hint">${hint}</span>`}</div>`;
}

function YesNo({ f, path, label, hint }) {
  const id = idOf(path);
  const v = getIn(f.data, path);
  return html`<div class="fld"><label for=${id}>${label}</label>
    <select id=${id} value=${v === true ? "y" : v === false ? "n" : ""} onChange=${(e) => f.set(path, e.target.value === "" ? null : e.target.value === "y")}>
      <option value="">Not answered</option><option value="y">Yes</option><option value="n">No</option></select>
    ${hint && html`<span class="hint">${hint}</span>`}</div>`;
}

function Check({ f, path, label, hint, dflt = false }) {
  const id = idOf(path);
  const v = getIn(f.data, path);
  return html`<div class="switch"><input type="checkbox" id=${id} checked=${v ?? dflt} onChange=${(e) => f.set(path, e.target.checked)} />
    <label for=${id} style="margin:0">${label}${hint && html`<span class="hint">${hint}</span>`}</label></div>`;
}

// Comma-separated list, parsed when the field loses focus so typing a comma never fights the cursor.
function Tags({ f, path, label, required, hint, sep = ",", rows }) {
  const id = idOf(path);
  const list = getIn(f.data, path) || [];
  const joiner = sep === "\n" ? "\n" : ", ";
  const [text, setText] = useState(list.join(joiner));
  useEffect(() => { setText(list.join(joiner)); }, [JSON.stringify(list)]);
  const commit = () => f.set(path, text.split(sep === "\n" ? /\n/ : /[,\n]/).map((s) => s.trim()).filter(Boolean));
  const props = { id, value: text, onInput: (e) => setText(e.target.value), onBlur: commit };
  return html`<div class="fld"><label for=${id}>${label}${required && html`<${Req}/>`} <span class="meta">(${list.length})</span></label>
    ${rows ? html`<textarea rows=${rows} ...${props}></textarea>` : html`<input type="text" ...${props} />`}
    ${hint && html`<span class="hint">${hint}</span>`}</div>`;
}

// Bullets: one per line. Existing bullets keep their ids by position.
function Bullets({ f, path, label = "Bullets (one per line)", required = true, hint }) {
  const id = idOf(path);
  const list = getIn(f.data, path) || [];
  const [text, setText] = useState(list.map((b) => b.text).join("\n"));
  useEffect(() => { setText(list.map((b) => b.text).join("\n")); }, [JSON.stringify(list.map((b) => b.text))]);
  const commit = () => f.set(path, text.split("\n").map((s) => s.trim()).filter(Boolean)
    .map((t, i) => ({ ...(list[i]?.id ? { id: list[i].id } : {}), text: t })));
  return html`<div class="fld"><label for=${id}>${label}${required && html`<${Req}/>`} <span class="meta">(${list.length}/8)</span></label>
    <textarea id=${id} rows="5" value=${text} onInput=${(e) => setText(e.target.value)} onBlur=${commit}></textarea>
    <span class="hint">${hint || "Start with a verb and keep real numbers: \"Cut API latency from 900 ms to 350 ms with Redis caching\". 4–6 bullets give the bot room to choose."}</span></div>`;
}

function Area({ f, path, label, hint, rows = 3, required }) {
  const id = idOf(path);
  return html`<div class="fld"><label for=${id}>${label}${required && html`<${Req}/>`}</label>
    <textarea id=${id} rows=${rows} value=${getIn(f.data, path) ?? ""} onInput=${(e) => f.set(path, e.target.value)}></textarea>
    ${hint && html`<span class="hint">${hint}</span>`}</div>`;
}

function Picks({ f, path, label, options, hint }) {
  const sel = getIn(f.data, path) || [];
  const toggle = (v) => f.set(path, sel.includes(v) ? sel.filter((x) => x !== v) : [...sel, v]);
  if (!options.length) return null;
  return html`<fieldset class="picks"><legend>${label}</legend>
    ${options.map(([v, t]) => html`<label class="pick"><input type="checkbox" checked=${sel.includes(v)} onChange=${() => toggle(v)} /> ${t}</label>`)}
    ${hint && html`<span class="hint">${hint}</span>`}</fieldset>`;
}

function List({ f, path, noun, max, min = 0, make = () => ({}), title, children }) {
  const items = getIn(f.data, path) || [];
  const move = (i, d) => { const a = [...items]; [a[i], a[i + d]] = [a[i + d], a[i]]; f.set(path, a); };
  return html`<div class="list-ed">
    ${items.map((it, i) => html`<section class="card" key=${it.id || i} aria-label=${`${noun} ${i + 1}`}>
      <header><strong>${title(it) || `New ${noun}`}</strong>
        <span class="card-actions">
          <button class="btn" type="button" disabled=${i === 0} onClick=${() => move(i, -1)} aria-label=${`Move ${noun} ${i + 1} up`}>↑</button>
          <button class="btn" type="button" disabled=${i === items.length - 1} onClick=${() => move(i, 1)} aria-label=${`Move ${noun} ${i + 1} down`}>↓</button>
          <button class="btn danger" type="button" disabled=${items.length <= min} onClick=${() => f.set(path, items.filter((_, j) => j !== i))}>Remove</button>
        </span></header>
      ${children([...path, i], it, i)}
    </section>`)}
    <p>${items.length < max ? html`<button class="btn" type="button" onClick=${() => f.set(path, [...items, make()])}>Add ${noun}</button>` : null}
      <span class="meta"> ${items.length} of ${max} max${min ? `, at least ${min}` : ""}</span></p>
  </div>`;
}

const MONTH_HINT = "YYYY-MM";
const COUNTRY_HINT = "e.g. India";

// ------------------------------------------------------------ sections
function Basics({ f }) {
  return html`
    <p class="lede">Shown at the top of every resume and used on every form.</p>
    <${Text} f=${f} path=${["personal", "full_name"]} label="Full name (as on your documents)" required />
    <div class="field-row">
      <${Text} f=${f} path=${["personal", "first_name"]} label="First name" hint="Left empty: taken from your full name." />
      <${Text} f=${f} path=${["personal", "middle_name"]} label="Middle name" />
      <${Text} f=${f} path=${["personal", "last_name"]} label="Last name / surname" />
      <${Text} f=${f} path=${["personal", "preferred_name"]} label="Preferred name" />
    </div>
    <div class="field-row">
      <${Text} f=${f} path=${["personal", "email"]} type="email" label="Email" required />
      <${Text} f=${f} path=${["personal", "phone"]} type="tel" label="Phone" required placeholder="+91 98765 43210" />
      <${Text} f=${f} path=${["personal", "location", "city"]} label="City you live in" required />
      <${Text} f=${f} path=${["personal", "location", "country"]} label="Country" required placeholder=${COUNTRY_HINT} />
    </div>
    <h3>Links</h3>
    <p class="meta">LinkedIn, GitHub, portfolio, LeetCode… They appear on the resume as clickable links (max 12).</p>
    <${List} f=${f} path=${["personal", "links"]} noun="link" max=${12} title=${(l) => l.label}>
      ${(p) => html`<div class="field-row"><${Text} f=${f} path=${[...p, "label"]} label="Label" required placeholder="GitHub" />
        <${Text} f=${f} path=${[...p, "url"]} type="url" label="URL" required placeholder="https://github.com/you" /></div>`}
    <//>`;
}

function Address({ f, path }) {
  return html`<div class="field-row">
    <${Text} f=${f} path=${[...path, "line1"]} label="Address line 1" required />
    <${Text} f=${f} path=${[...path, "line2"]} label="Address line 2" />
    <${Text} f=${f} path=${[...path, "city"]} label="City" required />
    <${Text} f=${f} path=${[...path, "district"]} label="District" />
    <${Text} f=${f} path=${[...path, "state"]} label="State" required />
    <${Text} f=${f} path=${[...path, "pincode"]} label="PIN code" required />
    <${Text} f=${f} path=${[...path, "country"]} label="Country" required placeholder=${COUNTRY_HINT} /></div>`;
}

function PersonalDetails({ f }) {
  const same = getIn(f.data, ["personal", "permanent_address"]) == null;
  return html`
    <p class="lede">Never printed on your resume. Only used to answer forms that ask.</p>
    <div class="field-row">
      <${Text} f=${f} path=${["personal", "date_of_birth"]} type="date" label="Date of birth" />
      <${Sel} f=${f} path=${["personal", "gender"]} label="Gender" options=${[["male", "Male"], ["female", "Female"], ["non_binary", "Non-binary"], ["other", "Other"]]}
        hint="Only given to forms if you choose to share it (Background)." />
      <${Text} f=${f} path=${["personal", "nationality"]} label="Nationality" placeholder="Indian" />
      <${Sel} f=${f} path=${["personal", "marital_status"]} label="Marital status" options=${[["single", "Single"], ["married", "Married"], ["divorced", "Divorced"], ["widowed", "Widowed"], ["other", "Other"]]} />
      <${Text} f=${f} path=${["personal", "pronouns"]} label="Pronouns" />
    </div>
    <div class="field-row">
      <${Text} f=${f} path=${["personal", "alternate_email"]} type="email" label="Alternate email" />
      <${Text} f=${f} path=${["personal", "alternate_phone"]} type="tel" label="Alternate phone" />
      <${Text} f=${f} path=${["personal", "whatsapp_phone"]} type="tel" label="WhatsApp number" hint="Empty: your phone number." />
    </div>
    <h3>Current address</h3>
    <${Address} f=${f} path=${["personal", "current_address"]} />
    <div class="switch"><input type="checkbox" id="same-addr" checked=${same}
      onChange=${(e) => f.set(["personal", "permanent_address"], e.target.checked ? null : { ...(getIn(f.data, ["personal", "current_address"]) || {}) })} />
      <label for="same-addr" style="margin:0">Permanent address is the same</label></div>
    ${!same && html`<h3>Permanent address</h3><${Address} f=${f} path=${["personal", "permanent_address"]} />`}`;
}

function BackgroundSec({ f }) {
  return html`
    <p class="lede">Some Indian campus and PSU-style forms ask these. All optional.</p>
    <div class="field-row">
      <${Text} f=${f} path=${["background", "father_name"]} label="Father's name" />
      <${Text} f=${f} path=${["background", "mother_name"]} label="Mother's name" />
    </div>
    <${Sel} f=${f} path=${["background", "eeo_disclosure"]} label="Self-identification questions (gender, category, disability, veteran)"
      options=${[["decline", "Decline to answer (default)"], ["share", "Answer with my details below"]]} />
    <div class="field-row">
      <${Sel} f=${f} path=${["background", "category"]} label="Category" options=${[["general", "General"], ["obc", "OBC"], ["sc", "SC"], ["st", "ST"], ["ews", "EWS"], ["other", "Other"]]} />
      <${YesNo} f=${f} path=${["background", "person_with_disability"]} label="Person with disability" />
      <${Text} f=${f} path=${["background", "disability_details"]} label="Disability details" />
      <${YesNo} f=${f} path=${["background", "veteran"]} label="Veteran (US forms)" />
    </div>
    <h3>Languages</h3>
    <${List} f=${f} path=${["background", "languages"]} noun="language" max=${10} title=${(l) => l.name} make=${() => ({ proficiency: "professional" })}>
      ${(p) => html`<div class="field-row"><${Text} f=${f} path=${[...p, "name"]} label="Language" required />
        <${Sel} f=${f} path=${[...p, "proficiency"]} label="Proficiency" options=${[["native", "Native"], ["fluent", "Fluent"], ["professional", "Professional"], ["intermediate", "Intermediate"], ["basic", "Basic"]]} /></div>`}
    <//>
    <${Tags} f=${f} path=${["background", "hobbies"]} label="Hobbies" />`;
}

function Identity({ api, u, toast }) {
  const [masked, setMasked] = useState({});
  const [form, setForm] = useState({});
  const [err, setErr] = useState("");
  const load = () => api(u("/identity")).then((r) => setMasked(r.data)).catch((e) => setErr(e.message));
  useEffect(() => { load(); }, []);
  const set = (k, v) => setForm((prev) => ({ ...prev, [k]: v }));
  const save = async (e) => {
    e.preventDefault(); setErr("");
    const patch = {};
    for (const k of ["pan", "aadhaar", "driving_licence", "voter_id", "uan"]) if (form[k]) patch[k] = form[k].trim();
    const pp = clean({ number: form.passport_number, issue_date: form.passport_issue_date, expiry_date: form.passport_expiry_date,
      place_of_issue: form.passport_place, issuing_country: form.passport_country });
    if (pp) patch.passport = { ...(masked.passport || {}), ...pp, number: pp.number || undefined };
    if (patch.passport && !patch.passport.number && !masked.passport) { setErr("Passport number is required."); return; }
    if (patch.passport && !patch.passport.number) delete patch.passport.number;
    try {
      const r = await api(u("/identity"), { method: "PATCH", body: patch });
      setMasked(r.data); setForm({}); toast("Saved and encrypted.");
    } catch (e2) {
      const nice = { pan: "PAN must look like ABCDE1234F (5 letters, 4 digits, 1 letter).", aadhaar: "Aadhaar must be 12 digits and can't start with 0 or 1.",
        uan: "UAN must be 12 digits.", voter_id: "Voter ID must be 8–12 letters or digits.", driving_licence: "Driving licence must be 8–20 letters, digits or dashes.",
        "passport.number": "Passport number must be 6–9 letters or digits.", "passport": "Passport expiry must be after its issue date." };
      setErr(Array.isArray(e2.detail) ? e2.detail.map((x) => nice[x.loc.join(".")] || `${x.loc.join(" › ")}: ${x.msg}`).join("\n") : e2.message);
    }
  };
  const remove = async (k) => { const r = await api(u("/identity"), { method: "PATCH", body: { [k]: null } }); setMasked(r.data); toast("Removed."); };
  const field = (k, label, ph, hint) => html`<div class="fld"><label for=${"id-" + k}>${label}</label>
    <input id=${"id-" + k} type="text" autocomplete="off" spellcheck="false" placeholder=${masked[k] ? `On file: ${masked[k]}` : ph}
      value=${form[k] || ""} onInput=${(e) => set(k, e.target.value)} />
    ${masked[k] && html`<button class="btn danger linkish" type="button" onClick=${() => remove(k)}>Remove ${label}</button>`}
    ${hint && html`<span class="hint">${hint}</span>`}</div>`;
  const mp = masked.passport || {};
  return html`<form onSubmit=${save}>
    <p class="lede">Encrypted on the server, shown back only as the last 4 characters, never put on a resume, never sent to an AI model.
      The bot types them only into forms that ask. Leave a field empty to keep what is on file.</p>
    ${err && html`<p class="notice error" style="white-space:pre-wrap">${err}</p>`}
    <div class="field-row">
      ${field("pan", "PAN", "ABCDE1234F")}
      ${field("aadhaar", "Aadhaar", "1234 5678 9012", "Only fill this if you are fine with forms receiving it.")}
      ${field("driving_licence", "Driving licence", "")}
      ${field("voter_id", "Voter ID", "")}
      ${field("uan", "UAN (EPF)", "12 digits", "Asked by employers if you have worked before.")}
    </div>
    <h3>Passport</h3>
    <div class="field-row">
      <div class="fld"><label for="pp-n">Passport number</label><input id="pp-n" type="text" autocomplete="off" placeholder=${mp.number ? `On file: ${mp.number}` : "Z1234567"}
        value=${form.passport_number || ""} onInput=${(e) => set("passport_number", e.target.value)} /></div>
      <div class="fld"><label for="pp-i">Issue date</label><input id="pp-i" type="date" value=${form.passport_issue_date || mp.issue_date || ""} onInput=${(e) => set("passport_issue_date", e.target.value)} /></div>
      <div class="fld"><label for="pp-e">Expiry date</label><input id="pp-e" type="date" value=${form.passport_expiry_date || mp.expiry_date || ""} onInput=${(e) => set("passport_expiry_date", e.target.value)} /></div>
      <div class="fld"><label for="pp-p">Place of issue</label><input id="pp-p" type="text" value=${form.passport_place ?? mp.place_of_issue ?? ""} onInput=${(e) => set("passport_place", e.target.value)} /></div>
      <div class="fld"><label for="pp-c">Issuing country</label><input id="pp-c" type="text" value=${form.passport_country ?? mp.issuing_country ?? ""} onInput=${(e) => set("passport_country", e.target.value)} /></div>
    </div>
    ${masked.passport && html`<p><button class="btn danger" type="button" onClick=${() => remove("passport")}>Remove passport</button></p>`}
    <p><button class="btn primary" type="submit">Save documents</button></p>
  </form>`;
}

const LEVELS = [["bachelors", "Bachelor's (B.Tech, B.E., B.Sc, BCA…)"], ["masters", "Master's (M.Tech, MCA, MBA…)"], ["diploma", "Diploma"],
  ["phd", "PhD"], ["class_xii", "Class XII"], ["class_x", "Class X"], ["certificate", "Certificate"], ["other", "Other"]];

function EducationSec({ f }) {
  return html`
    <p class="lede">Add your degree and, since many Indian forms ask, Class XII and Class X. Untick "show on resume" for school entries to keep the page short.</p>
    <${List} f=${f} path=${["education"]} noun="education entry" min=${1} max=${8} make=${() => ({ level: "class_xii", show_on_resume: false })}
      title=${(e) => [e.degree, e.institution].filter(Boolean).join(", ")}>
      ${(p, e) => {
        const school = e.level === "class_x" || e.level === "class_xii";
        return html`<div class="field-row">
          <${Sel} f=${f} path=${[...p, "level"]} label="Level" options=${LEVELS} />
          <${Text} f=${f} path=${[...p, "institution"]} label=${school ? "School" : "College / university"} required />
          <${Text} f=${f} path=${[...p, "degree"]} label=${school ? "Exam (e.g. Class XII)" : "Degree (e.g. B.Tech)"} required />
          <${Text} f=${f} path=${[...p, "field_of_study"]} label=${school ? "Stream (e.g. Science PCM)" : "Branch / major"} />
          ${school && html`<${Text} f=${f} path=${[...p, "board"]} label="Board" placeholder="CBSE" />`}
          <${Text} f=${f} path=${[...p, "location"]} label="Location" />
          <${Text} f=${f} path=${[...p, "start"]} type="month" label="Start" placeholder=${MONTH_HINT} />
          <${Text} f=${f} path=${[...p, "end"]} type="month" label="End" placeholder=${MONTH_HINT} />
          <${Num} f=${f} path=${[...p, "graduation_year"]} label="Year of passing" required step="1" min="1990" max="2100" />
          <${Num} f=${f} path=${[...p, "score"]} label="Score" hint="8.4 for a CGPA, 91.2 for a percentage" />
          <${Sel} f=${f} path=${[...p, "score_scale"]} label="Score type" options=${[["cgpa_10", "CGPA out of 10"], ["cgpa_4", "GPA out of 4"], ["percentage", "Percentage"]]} />
          ${!school && html`<${Num} f=${f} path=${[...p, "active_backlogs"]} label="Active backlogs" step="1" min="0" />
            <${Num} f=${f} path=${[...p, "backlog_history"]} label="Backlogs ever (cleared or not)" step="1" min="0" />`}
        </div>
        ${!school && html`<${Tags} f=${f} path=${[...p, "coursework"]} label="Relevant coursework" hint="Comma-separated, max 20." />`}
        <${Check} f=${f} path=${[...p, "show_on_resume"]} label="Show on resume" dflt=${true} />`;
      }}
    <//>`;
}

function roleOptions(f) { return (getIn(f.data, ["target_roles"]) || []).filter((r) => r.id).map((r) => [r.id, r.name || r.id]); }

function ExperienceSec({ f }) {
  return html`
    <p class="lede">Internships and jobs, newest first. Each resume shows up to 4, picking the most relevant bullets.</p>
    <${List} f=${f} path=${["experience"]} noun="experience" max=${15} make=${() => ({ employment_type: "internship", bullets: [] })}
      title=${(e) => [e.title, e.company].filter(Boolean).join(" at ")}>
      ${(p) => html`<div class="field-row">
          <${Text} f=${f} path=${[...p, "title"]} label="Job title" required />
          <${Text} f=${f} path=${[...p, "company"]} label="Company" required />
          <${Text} f=${f} path=${[...p, "location"]} label="Location" />
          <${Sel} f=${f} path=${[...p, "employment_type"]} label="Type" required options=${[["internship", "Internship"], ["full_time", "Full-time"], ["part_time", "Part-time"], ["contract", "Contract"], ["freelance", "Freelance"]]} />
          <${Text} f=${f} path=${[...p, "start"]} type="month" label="Start" required placeholder=${MONTH_HINT} />
          <${Text} f=${f} path=${[...p, "end"]} type="month" label="End" placeholder=${MONTH_HINT} hint="Empty if you still work here." />
        </div>
        <${Bullets} f=${f} path=${[...p, "bullets"]} />
        <${Tags} f=${f} path=${[...p, "tech"]} label="Tech used" hint="Comma-separated." />
        <${Picks} f=${f} path=${[...p, "roles"]} label="Best for these target roles" options=${roleOptions(f)} />`}
    <//>`;
}

function ProjectsSec({ f }) {
  return html`
    <p class="lede">Up to 10. Each resume shows the 3–4 that fit the job best; the bullets you write here are the only claims it can make.</p>
    <${List} f=${f} path=${["projects"]} noun="project" min=${1} max=${10} make=${() => ({ bullets: [], tech: [] })} title=${(x) => x.name}>
      ${(p) => html`<div class="field-row">
          <${Text} f=${f} path=${[...p, "name"]} label="Project name" required />
          <${Text} f=${f} path=${[...p, "repo"]} type="url" label="Code (GitHub) URL" />
          <${Text} f=${f} path=${[...p, "url"]} type="url" label="Live / demo URL" />
          <${Text} f=${f} path=${[...p, "start"]} type="month" label="Start" placeholder=${MONTH_HINT} />
          <${Text} f=${f} path=${[...p, "end"]} type="month" label="End" placeholder=${MONTH_HINT} />
        </div>
        <${Bullets} f=${f} path=${[...p, "bullets"]} />
        <${Tags} f=${f} path=${[...p, "tech"]} label="Tech stack" required hint="Comma-separated. Shown next to the project name (first 6)." />
        <${Tags} f=${f} path=${[...p, "tags"]} label="Area tags" hint="e.g. backend, ml, data, security" />
        <${Picks} f=${f} path=${[...p, "roles"]} label="Best for these target roles" options=${roleOptions(f)} />`}
    <//>`;
}

function SkillsSec({ f }) {
  const groups = getIn(f.data, ["skills"]) || [];
  const total = new Set(groups.flatMap((g) => (g.items || []).map((s) => s.toLowerCase()))).size;
  return html`
    <p class="lede">Your whole skill pool, up to 200, grouped your way. Each resume lists only the ~25 that matter for that job.
      <strong class="num"> ${total}/200</strong></p>
    <${List} f=${f} path=${["skills"]} noun="skill group" min=${1} max=${15} make=${() => ({ items: [] })} title=${(g) => g.category}>
      ${(p) => html`<${Text} f=${f} path=${[...p, "category"]} label="Group name" required placeholder="Languages, Frameworks, Data, Cloud…" />
        <${Tags} f=${f} path=${[...p, "items"]} label="Skills" required rows=${3} hint="Comma-separated." />`}
    <//>`;
}

function RolesSec({ f }) {
  const projects = (getIn(f.data, ["projects"]) || []).filter((x) => x.id).map((x) => [x.id, x.name || x.id]);
  return html`
    <p class="lede">The kinds of jobs you want, e.g. "Software Engineer" and "Data Analyst". For each job, the bot picks the role its title matches,
      then writes the resume with that role's summary, its skills first and its projects preferred. Max 10.</p>
    <${List} f=${f} path=${["target_roles"]} noun="target role" min=${1} max=${10} make=${() => ({ title_keywords: [], skills: [], projects: [] })} title=${(r) => r.name}>
      ${(p) => html`<${Text} f=${f} path=${[...p, "name"]} label="Role name" required placeholder="Software Development Engineer" />
        <${Tags} f=${f} path=${[...p, "title_keywords"]} label="Job titles it covers" hint="Comma-separated: SDE, Backend Developer, Graduate Engineer Trainee…" />
        <${Area} f=${f} path=${[...p, "summary"]} label="Resume summary for this role" hint="2 lines, 20–600 characters, only true claims. Empty: the general summary is used." />
        <${Tags} f=${f} path=${[...p, "skills"]} label="Skills to put first" rows=${2} hint="Comma-separated, from your skill pool, most important first." />
        <${Picks} f=${f} path=${[...p, "projects"]} label="Preferred projects (ticked order = priority)" options=${projects}
          hint=${projects.length ? "" : "Save after adding projects to pick them here."} />`}
    <//>`;
}

function ExtrasSec({ f }) {
  return html`
    <${Area} f=${f} path=${["summary"]} label="General resume summary (optional)" hint="Used when a job matches no role summary. 20–600 characters." />
    <h3>Certifications</h3>
    <${List} f=${f} path=${["certifications"]} noun="certification" max=${20} title=${(c) => c.name}>
      ${(p) => html`<div class="field-row"><${Text} f=${f} path=${[...p, "name"]} label="Name" required />
        <${Text} f=${f} path=${[...p, "issuer"]} label="Issuer" /><${Text} f=${f} path=${[...p, "date"]} type="month" label="Date" placeholder=${MONTH_HINT} />
        <${Text} f=${f} path=${[...p, "url"]} type="url" label="Credential URL" /></div>`}
    <//>
    <${Bullets} f=${f} path=${["achievements"]} label="Achievements (one per line, max 15)" required=${false} hint="Contest ranks, hackathon wins, scholarships, with real numbers." />
    <${Tags} f=${f} path=${["extra_keywords"]} label="Extra keywords you can back up" hint="Concepts like REST APIs, System Design, CI/CD. Used only when a job asks for them." />`;
}

function AnswersSec({ f }) {
  const p = ["standard_answers"];
  const custom = Object.entries(getIn(f.data, [...p, "custom"]) || {});
  const [rows, setRows] = useState(custom);
  useEffect(() => setRows(custom), [JSON.stringify(custom)]);
  const commit = (next) => { setRows(next); f.set([...p, "custom"], Object.fromEntries(next.filter(([q]) => q.trim()))); };
  return html`
    <p class="lede">Answers forms ask again and again. Unanswered ones send that form to you (Assisted mode) instead of the bot guessing.</p>
    <div class="field-row">
      <${Num} f=${f} path=${[...p, "expected_ctc_lpa"]} label="Expected CTC (LPA)" />
      <${YesNo} f=${f} path=${[...p, "expected_ctc_negotiable"]} label="Expected CTC negotiable" />
      <${Num} f=${f} path=${[...p, "current_ctc_lpa"]} label="Current CTC (LPA, 0 if none)" />
      <${Num} f=${f} path=${[...p, "notice_period_days"]} label="Notice period (days, 0 = immediate)" step="1" min="0" />
      <${Text} f=${f} path=${[...p, "earliest_start"]} type="month" label="Earliest start" placeholder=${MONTH_HINT} />
      <${Text} f=${f} path=${[...p, "work_authorization"]} label="Work authorization" placeholder="Indian citizen" />
      <${YesNo} f=${f} path=${[...p, "requires_sponsorship"]} label="Need visa sponsorship" />
      <${YesNo} f=${f} path=${[...p, "willing_to_relocate"]} label="Willing to relocate" />
      <${Sel} f=${f} path=${[...p, "preferred_work_mode"]} label="Preferred work mode" options=${[["any", "Any"], ["onsite", "On-site"], ["hybrid", "Hybrid"], ["remote", "Remote"]]} />
      <${YesNo} f=${f} path=${[...p, "currently_studying"]} label="Currently studying" />
      <${YesNo} f=${f} path=${[...p, "currently_employed"]} label="Currently employed" />
      <${YesNo} f=${f} path=${[...p, "willing_to_sign_bond"]} label="Willing to sign a service bond" />
      <${Num} f=${f} path=${[...p, "max_bond_months"]} label="Longest bond you'd accept (months)" step="1" min="0" />
      <${YesNo} f=${f} path=${[...p, "willing_night_shifts"]} label="Night / rotational shifts OK" />
      <${YesNo} f=${f} path=${[...p, "willing_to_travel"]} label="Willing to travel" />
      <${YesNo} f=${f} path=${[...p, "has_laptop"]} label="Own a laptop" />
      <${YesNo} f=${f} path=${[...p, "has_reliable_internet"]} label="Reliable internet at home" />
      <${Num} f=${f} path=${[...p, "education_gap_years"]} label="Gap years in education" step="1" min="0" />
      <${Text} f=${f} path=${[...p, "gap_reason"]} label="Reason for the gap" />
      <${YesNo} f=${f} path=${[...p, "criminal_record"]} label="Any criminal record" />
      <${YesNo} f=${f} path=${[...p, "background_check_consent"]} label="Consent to background checks" />
      <${Text} f=${f} path=${[...p, "heard_about_us"]} label="Default for 'How did you hear about us?'" placeholder="Company website" />
    </div>
    <${Tags} f=${f} path=${[...p, "relocation_cities"]} label="Cities you'd relocate to" />
    <h3>Your own answers</h3>
    <p class="meta">For questions some companies ask ("Why do you want to join?" works best answered per company, so leave those to Assisted mode).</p>
    ${rows.map(([q, a], i) => html`<div class="card"><div class="field-row">
      <div class="fld"><label for=${"cq" + i}>Question</label><input id=${"cq" + i} type="text" value=${q}
        onInput=${(e) => setRows(rows.map((r, j) => (j === i ? [e.target.value, r[1]] : r)))} onBlur=${() => commit(rows)} /></div>
      <div class="fld"><label for=${"ca" + i}>Answer</label><input id=${"ca" + i} type="text" value=${a}
        onInput=${(e) => setRows(rows.map((r, j) => (j === i ? [r[0], e.target.value] : r)))} onBlur=${() => commit(rows)} /></div></div>
      <button class="btn danger" type="button" onClick=${() => commit(rows.filter((_, j) => j !== i))}>Remove</button></div>`)}
    <p><button class="btn" type="button" onClick=${() => setRows([...rows, ["", ""]])}>Add question</button></p>`;
}

function PreferencesSec({ f }) {
  const p = ["expectations"];
  return html`
    <p class="lede">Which jobs to look for. Jobs outside these never reach you.</p>
    <${Tags} f=${f} path=${[...p, "locations"]} label="Locations" required hint="Comma-separated cities, plus Remote if you want remote jobs." />
    <${Picks} f=${f} path=${[...p, "remote_types"]} label="Work arrangements (at least one)" options=${[["onsite", "On-site"], ["hybrid", "Hybrid"], ["remote", "Remote"]]} />
    <div class="field-row">
      <${Sel} f=${f} path=${[...p, "experience_level"]} label="Experience level" required options=${[["fresher", "Fresher"], ["junior", "Junior (1–2 years)"], ["mid", "Mid (3+ years)"]]} />
      <${Num} f=${f} path=${[...p, "min_ctc_lpa"]} label="Minimum CTC (LPA)" hint="Jobs with a known lower salary are skipped." />
    </div>
    <${Tags} f=${f} path=${[...p, "role_types"]} label="Extra role names to search for" hint="Your target roles are searched already; add more search terms here if you like." />
    <${Tags} f=${f} path=${[...p, "blacklisted_companies"]} label="Never apply to" rows=${2} />`;
}

function RawJson({ f, onImport }) {
  const [text, setText] = useState(JSON.stringify(f.data, null, 2));
  const [msg, setMsg] = useState("");
  useEffect(() => setText(JSON.stringify(f.data, null, 2)), [f.data]);
  const apply = () => { try { f.replace(JSON.parse(text)); setMsg("Applied. Save to keep it."); } catch (e) { setMsg(`Not valid JSON: ${e.message}`); } };
  return html`
    <p class="lede">Import a draft from an existing resume (review every line afterwards), or edit the raw JSON.</p>
    <div class="toolbar">
      <label class="btn" style="margin:0">Import from resume (PDF/DOCX) <input type="file" accept=".pdf,.docx,.txt" onChange=${onImport} hidden /></label>
      <button class="btn" type="button" onClick=${apply}>Apply JSON</button>
      <button class="btn" type="button" onClick=${() => fetch("example-vault.json").then((r) => r.json()).then((d) => f.replace(d))}>Load the example</button>
    </div>
    ${msg && html`<p class="notice">${msg}</p>`}
    <label for="vault-json">Profile JSON</label>
    <textarea id="vault-json" class="code" spellcheck="false" value=${text} onInput=${(e) => setText(e.target.value)}></textarea>`;
}

// ------------------------------------------------------------ editor shell
const STEPS = [
  ["basics", "Basics", true, Basics], ["roles", "Target roles", true, RolesSec], ["education", "Education", true, EducationSec],
  ["experience", "Experience", false, ExperienceSec], ["projects", "Projects", true, ProjectsSec], ["skills", "Skills", true, SkillsSec],
  ["extras", "Summary & achievements", false, ExtrasSec], ["preferences", "Job preferences", true, PreferencesSec],
  ["answers", "Application answers", false, AnswersSec], ["personal", "Personal details", false, PersonalDetails],
  ["background", "Family & background", false, BackgroundSec], ["identity", "ID documents", false, null], ["json", "Import / JSON", false, null],
];
const STEP_OF = { personal: (l) => (["full_name", "first_name", "middle_name", "last_name", "preferred_name", "email", "phone", "location", "links"].includes(l[1]) ? "basics" : "personal"),
  background: () => "background", target_roles: () => "roles", education: () => "education", experience: () => "experience", projects: () => "projects",
  skills: () => "skills", summary: () => "extras", certifications: () => "extras", achievements: () => "extras", extra_keywords: () => "extras",
  expectations: () => "preferences", standard_answers: () => "answers", identity: () => "identity" };
const stepOf = (loc) => (STEP_OF[loc[0]] || (() => "json"))(loc);

function done(d, key) {
  const has = (p) => { const v = getIn(d, p); return Array.isArray(v) ? v.length > 0 : v != null && v !== ""; };
  switch (key) {
    case "basics": return ["full_name", "email", "phone"].every((k) => has(["personal", k])) && has(["personal", "location", "city"]);
    case "roles": return (d.target_roles || []).some((r) => r.name);
    case "education": return (d.education || []).some((e) => e.institution && e.degree && e.graduation_year);
    case "experience": return (d.experience || []).length > 0;
    case "projects": return (d.projects || []).some((p) => p.name && (p.bullets || []).length);
    case "skills": return (d.skills || []).some((g) => (g.items || []).length);
    case "preferences": return has(["expectations", "locations"]) && has(["expectations", "remote_types"]);
    case "personal": return has(["personal", "date_of_birth"]) && has(["personal", "current_address"]);
    case "answers": return has(["standard_answers", "expected_ctc_lpa"]) && has(["standard_answers", "notice_period_days"]);
    case "extras": return has(["summary"]) || (d.achievements || []).length > 0;
    case "background": return has(["background", "father_name"]) || (getIn(d, ["background", "languages"]) || []).length > 0;
    default: return false;
  }
}

export function ProfileEditor({ api, u, toast }) {
  const [data, setData] = useState(null);
  const [rev, setRev] = useState(null);
  const [step, setStep] = useState(() => (location.hash.split("/")[2] || "basics"));
  const [errors, setErrors] = useState([]);
  const [warnings, setWarnings] = useState([]);
  const [gaps, setGaps] = useState([]);
  const [dirty, setDirty] = useState(false);
  const [saving, setSaving] = useState(false);

  const load = async () => {
    try {
      const r = await api(u("/vault"));
      setData(r.data); setRev(r.revision); setWarnings(r.warnings || []); setGaps(r.gaps || []); setErrors(r.errors || []); setDirty(false);
    } catch (e) {
      if (e.status === 404) { setData(structuredClone(BLANK)); setRev(null); setDirty(false); }
      else toast(e.message);
    }
  };
  useEffect(() => { load(); }, []);
  useEffect(() => {
    const warn = (e) => { if (dirty) { e.preventDefault(); e.returnValue = ""; } };
    addEventListener("beforeunload", warn); return () => removeEventListener("beforeunload", warn);
  }, [dirty]);
  const go = (k) => { setStep(k); history.replaceState(null, "", `#/vault/${k}`); window.scrollTo(0, 0); };

  if (!data) return html`<h1>Profile</h1><p class="lede">Loading…</p>`;
  const f = { data, set: (path, v) => { setData((d) => setIn(d, path, v)); setDirty(true); }, replace: (d) => { setData(d); setDirty(true); } };

  const save = async () => {
    const body = clean(assignIds(data)) || {};
    setSaving(true);
    try {
      const r = await api(u("/vault"), { method: "PUT", body, headers: rev ? { "If-Match": `"${rev}"` } : {} });
      setData(r.data); setRev(r.revision); setWarnings(r.warnings || []); setGaps(r.gaps || []); setErrors([]); setDirty(false);
      toast(`Saved (revision ${r.revision}).`);
    } catch (e) {
      if (e.status === 409) toast("Your profile changed in another tab. Reload to get the latest.");
      else if (Array.isArray(e.detail)) { setErrors(e.detail); toast("Not saved: fix the fields listed at the top."); window.scrollTo(0, 0); }
      else toast(e.message);
    } finally { setSaving(false); }
  };
  const importResume = async (e) => {
    const file = e.target.files[0]; if (!file) return;
    const fd = new FormData(); fd.append("file", file);
    try {
      const r = await api(u("/onboarding/import"), { method: "POST", body: fd });
      f.replace({ ...structuredClone(BLANK), ...r.draft }); setErrors(r.errors || []);
      toast("Draft from your resume. Review every section, then save."); go("basics");
    } catch (err) { toast(err.message); }
  };

  const errBy = errors.reduce((m, e) => { const k = stepOf(e.loc || []); (m[k] = m[k] || []).push(e); return m; }, {});
  const gapBy = gaps.reduce((m, g) => { const k = stepOf(g.path.split(".")); (m[k] = m[k] || []).push(g); return m; }, {});
  const [, title, , View] = STEPS.find((s) => s[0] === step) || STEPS[0];

  return html`
    <h1>Profile</h1>
    <p class="lede">Everything the bot knows about you. Resumes are built from this per job, and every claim on them must come from here.
      Sections marked <span class="req">*</span> are required.</p>
    <div class="profile">
      <nav class="steps" aria-label="Profile sections">
        ${STEPS.map(([k, t, req]) => html`<button type="button" aria-current=${k === step ? "step" : undefined} onClick=${() => go(k)}>
          <span class=${`dot ${errBy[k] ? "bad" : done(data, k) ? "ok" : ""}`} aria-hidden="true"></span>
          <span>${t}${req && html`<span class="req"> *</span>`}</span>
          ${errBy[k] ? html`<span class="count bad">${errBy[k].length}</span>` : gapBy[k] ? html`<span class="count">${gapBy[k].length}</span>` : null}</button>`)}
      </nav>
      <div class="step-body">
        <div class="toolbar sticky">
          <button class="btn primary" type="button" onClick=${save} disabled=${saving || step === "identity"}>${saving ? "Saving…" : "Save profile"}</button>
          <span class="meta">${dirty ? "Unsaved changes" : rev ? `Saved, revision ${rev}` : "Not saved yet"}</span>
        </div>
        ${errors.length > 0 && html`<div class="notice error"><strong>Fix these before saving:</strong><ul>${errors.map((e) => html`<li>
          <button class="linkish" type="button" onClick=${() => go(stepOf(e.loc || []))}>${(e.loc || []).join(" › ") || "profile"}</button>: ${e.msg}</li>`)}</ul></div>`}
        <h2>${title}</h2>
        ${gapBy[step] && html`<div class="notice"><strong>Forms ask for these and they're empty:</strong> ${gapBy[step].map((g) => `${g.label} (${g.asked})`).join(", ")}</div>`}
        ${step === "identity" ? html`<${Identity} api=${api} u=${u} toast=${toast} />`
          : step === "json" ? html`<${RawJson} f=${f} onImport=${importResume} />` : html`<${View} f=${f} />`}
        ${warnings.length > 0 && step !== "identity" && html`<details><summary>${warnings.length} suggestions for better resumes</summary>
          <ul>${warnings.map((w) => html`<li><span class="meta">${w.path}</span> ${w.message}</li>`)}</ul></details>`}
        <p class="step-nav">${(() => { const i = STEPS.findIndex((s) => s[0] === step); return html`
          ${i > 0 && html`<button class="btn" type="button" onClick=${() => go(STEPS[i - 1][0])}>← ${STEPS[i - 1][1]}</button>`}
          ${i < STEPS.length - 1 && html`<button class="btn" type="button" onClick=${() => go(STEPS[i + 1][0])}>${STEPS[i + 1][1]} →</button>`}`; })()}</p>
      </div>
    </div>`;
}
