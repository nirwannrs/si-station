// SI-Station card creator. The card being edited is one plain object (the card.json); every form
// control reads from and writes straight into it, and each change is saved to the local server,
// which answers with what the game's own checks think of the card.

const SLOTS = ["head", "body", "hands", "feet", "weapon", "offhand", "accessory"];
const FEATURE_DEFAULTS = { inventory: true, equipment: true, money: true, levels: false, skills: false, relationships: false, states: false };
const CAPABILITY_NAMES = { all: "Everything", move: "Moving between places", items: "Using or giving items", equipment: "Changing equipment", skills: "Using skills", trade: "Buying and selling", attack: "Attacking" };

const app = document.getElementById("app");
let pid = null;          // folder name of the open card
let card = null;
let problems = [];
let section = "info";
let templates = {};
let builtinStates = [];
let lastImport = "";             // what the last lorebook import did, shown once after the redraw
let saveTimer = null;
let saving = Promise.resolve();
let presetId = null;             // file name (without its ending) of the open preset; null while a card is open
let preset = null;
let presetBuiltin = false;       // the game's own preset: shown, never saved
let presetSection = "instructions";
let wording = [];                // every prompt the game writes itself: { key, reader, title, help, parts, text }
let slotNotes = {};
let previewCards = [];           // cards a prompt can be previewed with: [id, title]
let previewCard = "";
const openItems = new Set();     // which list entries are unfolded, by path
const autoIds = new WeakSet();   // entries made this session whose id still follows their name

// ---------- how the page looks
//
// Two choices, kept in this browser: light, dark or whatever the system uses; and whether text
// boxes grow to show all of their text or stay a few lines tall.

const looks = { theme: "system", boxes: "full" };
try { looks.theme = localStorage.getItem("si.theme") || looks.theme; looks.boxes = localStorage.getItem("si.boxes") || looks.boxes; } catch (error) { /* private window: the choices last for this visit */ }
looks.theme = new URLSearchParams(location.search).get("theme") || looks.theme;      // ?theme=dark opens the page that way
const systemDark = window.matchMedia("(prefers-color-scheme: dark)");

function applyLooks() {
  const root = document.documentElement;
  root.dataset.theme = looks.theme === "dark" || (looks.theme === "system" && systemDark.matches) ? "dark" : "light";
  root.dataset.boxes = looks.boxes;
  try { localStorage.setItem("si.theme", looks.theme); localStorage.setItem("si.boxes", looks.boxes); } catch (error) { /* see above */ }
  fitAll();
}
systemDark.addEventListener("change", applyLooks);

// A text box as tall as its text, so nothing has to be scrolled inside it. One that is not on
// screen yet (inside a folded entry) has no size to measure and is fitted when it is unfolded.
function fit(box) {
  box.style.height = "auto";
  if (looks.boxes === "full" && box.scrollHeight) box.style.height = box.scrollHeight + 2 + "px";
}
const fitAll = (within = document) => { for (const box of within.querySelectorAll("textarea")) fit(box); };
document.addEventListener("input", (event) => { if (event.target.tagName === "TEXTAREA") fit(event.target); });
document.addEventListener("toggle", (event) => { if (event.target.open) fitAll(event.target); }, true);
window.addEventListener("resize", () => fitAll());
new MutationObserver((changes) => {
  if (changes.some((change) => [...change.addedNodes].some((node) => node.nodeType === 1 && (node.tagName === "TEXTAREA" || node.querySelector("textarea"))))) requestAnimationFrame(() => fitAll());
}).observe(document.getElementById("app"), { childList: true, subtree: true });

// The two choices as controls, for the bar at the top of every page.
function looksControls() {
  const theme = document.createElement("select");
  for (const [value, label] of [["system", "Follow system"], ["light", "Light"], ["dark", "Dark"]]) theme.append(new Option(label, value));
  theme.value = looks.theme;
  theme.title = "Light or dark";
  theme.addEventListener("change", () => { looks.theme = theme.value; applyLooks(); });
  const boxes = document.createElement("button");
  boxes.type = "button";
  boxes.title = "Whether text boxes grow to show all of their text";
  const label = () => { boxes.textContent = looks.boxes === "full" ? "Text boxes: full" : "Text boxes: short"; };
  boxes.addEventListener("click", () => { looks.boxes = looks.boxes === "full" ? "short" : "full"; label(); applyLooks(); });
  label();
  const holder = document.createElement("div");
  holder.className = "looks";
  holder.append(theme, boxes);
  return holder;
}

// ---------- small helpers

function el(tag, attrs = {}, ...kids) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value === undefined || value === null || value === false) continue;
    if (key === "class") node.className = value;
    else if (key === "text") node.textContent = value;
    else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    else node.setAttribute(key, value === true ? "" : value);
  }
  for (const kid of kids.flat(Infinity)) {
    if (kid === null || kid === undefined || kid === false) continue;
    node.append(kid instanceof Node ? kid : document.createTextNode(kid));
  }
  return node;
}

const slug = (text) => (text || "").toLowerCase().replace(/[^a-z0-9]+/g, "_").replace(/^_+|_+$/g, "");
const cap = (text) => text.charAt(0).toUpperCase() + text.slice(1);

async function api(method, url, body, raw = false) {
  const init = { method, headers: {} };
  if (raw) init.body = body;
  else if (body !== undefined) {
    init.body = JSON.stringify(body);
    init.headers["Content-Type"] = "application/json";
  }
  const response = await fetch(url, init);
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.error || "Something went wrong.");
  return data;
}

function set(obj, key, value) {
  if (value === "" || value === undefined || value === null || Number.isNaN(value)) delete obj[key];
  else obj[key] = value;
}

const has = (feature) => {
  const on = (card.rules.features || {})[feature] ?? FEATURE_DEFAULTS[feature];
  return on && (feature !== "equipment" || has("inventory"));
};
const visual = () => ((card.display || {}).mode || "visual") === "visual";
const systemBattle = () => ((card.rules.battle || {}).mode || "free") === "system";

const named = (list, label = "name") => (list || []).map((x) => [x.id, x[label] || x.id]);
const O = {
  stats: () => named(card.rules.stats),
  items: () => named(card.items),
  skills: () => named(card.skills),
  locations: () => named(card.locations),
  characters: () => named(card.characters),
  // The built-in states, with the card's own added and any it redefines replaced.
  states: () => named([...builtinStates.filter((b) => !(card.states || []).some((s) => s.id === b.id)), ...(card.states || [])]),
};

// ---------- keeping stat references in step
//
// Stats are referred to from many places (level gains, fight rules, item and skill effects,
// starting values, quest rewards). Most of those are only shown for stats that exist, so a
// reference to a removed stat would be an error the creator cannot see or clear. Removing a stat
// therefore removes its references, and renaming one renames them.

function statHolders() {
  const rules = card.rules, maps = [], lists = [];
  if (rules.leveling) maps.push([rules.leveling, "gains"]);
  for (const who of [...(card.characters || []), card.default_persona || {}]) if (who.start) maps.push([who.start, "stats"], [who.start, "max"], [who.start, "min"]);
  for (const quest of card.quests || []) if (quest.rewards) maps.push([quest.rewards, "stats"]);
  for (const skill of card.skills || []) { maps.push([skill, "cost"]); lists.push(skill); }
  for (const item of card.items || []) lists.push(item);
  return { maps, lists };
}

function renameStat(from, to) {
  if (!from || !to || from === to) return;
  const { maps, lists } = statHolders();
  for (const [owner, key] of maps) {
    if (owner[key] && from in owner[key]) { owner[key][to] = owner[key][from]; delete owner[key][from]; }
  }
  for (const owner of lists) for (const effect of owner.effects || []) if (effect.stat === from) effect.stat = to;
  for (const key of ["health_stat", "attack_stat", "defense_stat"]) if ((card.rules.battle || {})[key] === from) card.rules.battle[key] = to;
}

// Drops every reference to a stat the card does not have. Returns true if turn-based fights had
// to be switched off because the health stat is gone.
function dropMissingStats() {
  const known = new Set(card.rules.stats.map((s) => s.id));
  const { maps, lists } = statHolders();
  for (const [owner, key] of maps) {
    for (const id of Object.keys(owner[key] || {})) if (!known.has(id)) delete owner[key][id];
    if (owner[key] && !Object.keys(owner[key]).length) delete owner[key];
  }
  for (const owner of lists) {
    if (owner.effects) owner.effects = owner.effects.filter((e) => !e.stat || known.has(e.stat));
    if (owner.effects && !owner.effects.length) delete owner.effects;
  }
  const battle = card.rules.battle || {};
  for (const key of ["attack_stat", "defense_stat"]) if (battle[key] && !known.has(battle[key])) delete battle[key];
  if (battle.health_stat && !known.has(battle.health_stat)) {
    delete battle.health_stat;
    if (battle.mode === "system") { battle.mode = "free"; return true; }
  }
  return false;
}

// ---------- saving

// Empty objects and lists left behind by cleared fields are dropped before saving, so the saved
// card stays tidy. The few the format requires are kept.
function pruned(value, path = "") {
  if (Array.isArray(value)) return value.map((v, i) => pruned(v, path + "/" + i));
  if (value && typeof value === "object") {
    const out = {};
    for (const [key, inner] of Object.entries(value)) {
      const child = pruned(inner, path + "/" + key);
      const empty = child && typeof child === "object" && Object.keys(child).length === 0;
      const required = ["/meta", "/world", "/rules", "/rules/stats", "/characters"].includes(path + "/" + key);
      if (!empty || required) out[key] = child;
    }
    return out;
  }
  return value;
}

function setStatus(text) {
  const node = document.querySelector(".status");
  if (node) node.textContent = text;
}

function showProblems() {
  const badge = document.querySelector(".badge");
  if (!badge) return;
  badge.className = "badge " + (problems.length ? "bad" : "ok");
  badge.textContent = problems.length ? problems.length + (problems.length === 1 ? " problem" : " problems") : presetId ? "Ready to use" : "Ready to play";
  badge.title = problems.join("\n");
  if (presetId) { const list = document.querySelector(".preset-problems"); if (list) list.replaceWith(presetProblems()); }
  else if (section === "export") renderSection();
}

function save() {
  clearTimeout(saveTimer);
  saveTimer = null;
  if (presetId) return savePreset();
  const project = pid, body = pruned(card);
  setStatus("Saving...");
  saving = saving.then(() => api("PUT", `/api/projects/${project}`, body)).then((reply) => {
    if (project !== pid) return;
    problems = reply.problems;
    setStatus("Saved");
    showProblems();
  }).catch((error) => setStatus("Not saved: " + error.message));
  return saving;
}

const flush = () => (saveTimer ? save() : saving);

// Call after any edit. redraw is for edits that change which fields exist.
function changed(redraw) {
  setStatus("Editing...");
  clearTimeout(saveTimer);
  saveTimer = setTimeout(save, 500);
  if (redraw) (presetId ? renderPreset() : renderSection());
}

// ---------- form fields
//
// A field is { t: type, k: key in the object, label, help, when: () => shown? , ... }.

function labelled(f, control) {
  return el("label", { class: "field" }, el("span", { class: "name", text: f.label }), f.help && el("span", { class: "help", text: f.help }), control);
}

function inputField(obj, f, ctx) {
  const input = f.t === "area" ? el("textarea", { rows: f.rows || 3 })
    : el("input", { type: f.t === "number" ? "number" : f.t === "color" ? "color" : "text", step: "any", placeholder: f.placeholder });
  if (f.t === "tags") input.value = (obj[f.k] || []).join(", ");
  else if (f.t === "color") input.value = obj[f.k] || "#ffd28a";
  else input.value = obj[f.k] ?? "";
  ctx.inputs[f.k] = input;
  input.addEventListener("input", () => {
    let value = input.value;
    if (f.t === "number") value = value === "" ? undefined : Number(value);
    if (f.t === "tags") value = value.split(",").map((t) => t.trim()).filter(Boolean), value = value.length ? value : undefined;
    const before = { ...obj };
    set(obj, f.k, value);
    if (f.k === "id") autoIds.delete(obj);
    if (f.feeds && autoIds.has(obj)) {
      obj[f.feeds] = slug(input.value);
      if (ctx.inputs[f.feeds]) ctx.inputs[f.feeds].value = obj[f.feeds];
    }
    if (ctx.renamed && before.id !== obj.id) ctx.renamed(before.id, obj.id);
    if (ctx.refresh) ctx.refresh();
    changed(false);
  });
  if (f.redraw) input.addEventListener("change", () => changed(true));
  return labelled(f, input);
}

function boolField(obj, f) {
  const box = el("input", { type: "checkbox" });
  box.checked = f.get ? f.get() : obj[f.k] ?? f.default ?? false;
  box.addEventListener("change", () => {
    if (f.put) f.put(box.checked); else obj[f.k] = box.checked;
    changed(f.redraw !== false);
  });
  return el("label", { class: "check" }, box, el("span", { text: f.label }), f.help && el("span", { class: "help", text: f.help }));
}

function selectControl(options, current, none, onPick) {
  const select = el("select");
  const all = (none === false ? [] : [["", none || "(none)"]]).concat(options);
  if (current && !all.some(([value]) => value === current)) all.push([current, `(missing: ${current})`]);
  for (const [value, label] of all) select.append(el("option", { value, text: label }));
  select.value = current ?? "";
  select.addEventListener("change", () => onPick(select.value));
  return select;
}

function selectField(obj, f) {
  return labelled(f, selectControl(f.options(obj), obj[f.k] ?? f.default, f.none, (value) => {
    set(obj, f.k, value);
    changed(!!f.redraw);
  }));
}

function checksField(obj, f) {
  const picked = new Set(obj[f.k] || []);
  const boxes = f.options(obj).map(([value, label]) => {
    const box = el("input", { type: "checkbox" });
    box.checked = picked.has(value);
    box.addEventListener("change", () => {
      box.checked ? picked.add(value) : picked.delete(value);
      set(obj, f.k, picked.size ? [...picked] : undefined);
      changed(false);
    });
    return el("label", {}, box, label);
  });
  return el("div", { class: "list" }, el("div", { class: "name", text: f.label }),
    boxes.length ? el("div", { class: "checks" }, boxes) : el("p", { class: "muted small", text: f.empty || "Nothing to choose from yet." }));
}

// A map of id -> number, one box per option (for example a starting value per stat).
function mapField(obj, f) {
  const options = f.options(obj);
  if (!options.length) return null;
  const cells = options.map(([id, label]) => {
    const control = f.pick
      ? selectControl(f.pick(id), (obj[f.k] || {})[id], "(nothing)", (value) => put(id, value))
      : el("input", { type: "number", step: "any", placeholder: f.placeholder ? f.placeholder(id) : "" });
    if (!f.pick) {
      control.value = (obj[f.k] || {})[id] ?? "";
      control.addEventListener("input", () => put(id, control.value === "" ? undefined : Number(control.value)));
    }
    return el("label", {}, label, control);
  });
  function put(id, value) {
    const map = obj[f.k] || (obj[f.k] = {});
    set(map, id, value);
    if (!Object.keys(map).length) delete obj[f.k];
    changed(false);
  }
  return el("div", { class: "list" }, el("div", { class: "name", text: f.label }), f.help && el("p", { class: "muted small", text: f.help }), el("div", { class: "grid" }, cells));
}

function imageUrl(path) {
  return `/api/projects/${pid}/file/${path}?v=${Date.now()}`;
}

async function upload(file, path) {
  await flush();
  await api("POST", `/api/projects/${pid}/asset?path=${encodeURIComponent(path)}`, file, true);
  return path;
}

const extension = (file) => (file.name.match(/\.(png|jpe?g|webp)$/i) || [".png"])[0].toLowerCase();

function assetField(obj, f) {
  const picker = el("input", { type: "file", accept: "image/png,image/jpeg,image/webp" });
  picker.addEventListener("change", async () => {
    if (!picker.files[0]) return;
    try {
      obj[f.k] = await upload(picker.files[0], f.path(obj, extension(picker.files[0])));
      changed(true);
    } catch (error) { alert(error.message); }
  });
  return labelled(f, el("div", { class: "asset" },
    obj[f.k] ? el("img", { src: imageUrl(obj[f.k]), alt: "" }) : el("div", { class: "empty", text: "No image" }),
    el("div", {}, picker, obj[f.k] && el("button", { class: "quiet", type: "button", text: "Remove", onclick: () => { delete obj[f.k]; changed(true); } }))));
}

// Expression name -> image, for a character's sprites.
function spritesField(obj, f) {
  const sprites = obj[f.k] || {};
  const name = el("input", { type: "text", placeholder: "expression, e.g. happy", value: Object.keys(sprites).length ? "" : "neutral" });
  const picker = el("input", { type: "file", accept: "image/png,image/jpeg,image/webp" });
  picker.addEventListener("change", async () => {
    const expression = slug(name.value);
    if (!picker.files[0]) return;
    if (!obj.id || !expression) { alert("Give the character an id and the expression a name first."); picker.value = ""; return; }
    try {
      (obj[f.k] || (obj[f.k] = {}))[expression] = await upload(picker.files[0], `assets/sprites/${obj.id}/${expression}${extension(picker.files[0])}`);
      changed(true);
    } catch (error) { alert(error.message); }
  });
  return el("div", { class: "list" }, el("div", { class: "name", text: f.label }), el("p", { class: "muted small", text: f.help }),
    el("div", { class: "sprites" }, Object.entries(sprites).map(([expression, path]) => el("div", { class: "sprite" },
      el("img", { src: imageUrl(path), alt: expression }), expression,
      el("button", { class: "quiet", type: "button", text: "Remove", onclick: () => { delete sprites[expression]; if (!Object.keys(sprites).length) delete obj[f.k]; changed(true); } })))),
    el("div", { class: "inline-item" }, el("label", {}, "Add an expression", name), el("label", {}, "Image", picker)));
}

function listField(obj, f, path) {
  const items = obj[f.k] || [];
  const here = `${path}/${f.k}`;
  const remove = (index) => {
    if (!f.inline && !confirm(`Remove ${f.title ? f.title(items[index]) || "this entry" : "this entry"}?`)) return;
    items.splice(index, 1);
    if (!items.length && !f.keep) delete obj[f.k];
    if (f.removed) f.removed();
    changed(true);
  };
  const rows = items.map((item, index) => {
    if (f.inline) {
      return el("div", { class: "inline-item" }, renderFields(item, f.fields, `${here}/${index}`),
        el("button", { class: "quiet", type: "button", text: "Remove", onclick: () => remove(index) }));
    }
    const title = el("span", { class: "title" }), sub = el("span", { class: "sub" });
    const refresh = () => { title.textContent = f.title(item) || "(unnamed)"; sub.textContent = f.sub ? f.sub(item) || "" : ""; };
    refresh();
    const details = el("details", { class: "item" },
      el("summary", {}, title, sub, el("button", { class: "quiet", type: "button", text: "Remove", onclick: (event) => { event.preventDefault(); remove(index); } })),
      el("div", { class: "body" }, renderFields(item, f.fields, `${here}/${index}`, refresh, f.renamed)));
    details.open = openItems.has(`${here}/${index}`);
    details.addEventListener("toggle", () => (details.open ? openItems.add(`${here}/${index}`) : openItems.delete(`${here}/${index}`)));
    return details;
  });
  const add = el("button", { type: "button", text: f.add || "Add", onclick: () => {
    const fresh = f.make ? f.make() : {};
    if ("id" in fresh) autoIds.add(fresh);
    (obj[f.k] || (obj[f.k] = [])).push(fresh);
    openItems.add(`${here}/${obj[f.k].length - 1}`);
    changed(true);
  } });
  return el("div", { class: "list" }, f.label && el("div", { class: "name", text: f.label }), f.help && el("p", { class: "muted small", text: f.help }), rows, add);
}

function renderFields(obj, fields, path, refresh, renamed) {
  const ctx = { inputs: {}, refresh, renamed };
  return fields.filter((f) => !f.when || f.when(obj)).map((f) => {
    switch (f.t) {
      case "bool": return boolField(obj, f);
      case "select": return selectField(obj, f);
      case "checks": return checksField(obj, f);
      case "map": return mapField(obj, f);
      case "asset": return assetField(obj, f);
      case "sprites": return spritesField(obj, f);
      case "list": return listField(obj, f, path);
      case "group": return el("fieldset", {}, el("legend", { text: f.label }), f.help && el("p", { class: "muted small", text: f.help }),
        renderFields(f.flat ? obj : obj[f.k] || (obj[f.k] = {}), f.fields, `${path}/${f.k}`));     // flat: a box around fields of the same object
      case "custom": return f.render(obj);
      default: return inputField(obj, f, ctx);
    }
  });
}

// ---------- what a card is made of

const effects = (label, help) => ({ t: "list", k: "effects", label, help, inline: true, add: "Add an effect", make: () => ({ amount: 1 }), fields: [
  { t: "select", k: "stat", label: "Stat", options: O.stats, none: "(choose)" },
  { t: "number", k: "amount", label: "Amount (negative lowers it)" },
] });

const stacks = (k, label, help) => ({ t: "list", k, label, help, inline: true, add: "Add an item", make: () => ({ qty: 1 }), fields: [
  { t: "select", k: "item", label: "Item", options: O.items, none: "(choose)" },
  { t: "number", k: "qty", label: "How many" },
] });

// Starting values for a character or for the player. Only the systems the card uses are shown.
const loadout = (isCharacter) => ({ t: "group", k: "start", label: "Starting point", help: "What they begin the game with. The game changes these as the story goes.", fields: [
  { t: "bool", k: "known", label: "The player already knows them when the game starts", default: true, redraw: false, when: () => isCharacter,
    help: "Untick for a stranger: they stay out of the player's People list until the story introduces them." },
  { t: "number", k: "relationship", label: `${cap((card.rules.relationship_name || "Affection"))} toward the player at the start (0 to 100)`, when: () => isCharacter && has("relationships") },
  { t: "list", k: "states", label: "States at the start", when: () => has("states"), inline: true, add: "Add a state", make: () => ({}), fields: [
    { t: "select", k: "state", label: "State", options: O.states, none: "(choose)" },
    { t: "text", k: "note", label: "How or why (optional)" },
  ] },
  { t: "number", k: "level", label: "Level", placeholder: "1", when: () => has("levels") },
  { t: "number", k: "money", label: card.rules.currency_name || "Gold", when: () => has("money") },
  { t: "map", k: "stats", label: "Stats", help: "Leave a box empty to use the stat's usual starting value.", options: O.stats,
    placeholder: (id) => String((card.rules.stats.find((s) => s.id === id) || {}).default ?? "") },
  { t: "map", k: "max", label: "Their own maximums", help: "The most this character can have of a stat: their full Health, their whole Mana pool. Rest brings a stat back up to it. Leave a box empty to use the stat's usual maximum.", options: O.stats,
    placeholder: (id) => String((card.rules.stats.find((s) => s.id === id) || {}).max ?? "no limit") },
  { t: "map", k: "min", label: "Their own minimums", help: "The least this character can have of a stat. Rarely needed. Leave a box empty to use the stat's usual minimum.", options: O.stats,
    placeholder: (id) => String((card.rules.stats.find((s) => s.id === id) || {}).min ?? 0) },
  { ...stacks("inventory", "Inventory"), when: () => has("inventory") },
  { t: "map", k: "equipment", label: "Wearing", when: () => has("equipment"), options: () => SLOTS.map((s) => [s, cap(s)]),
    pick: (slot) => (card.items || []).filter((i) => i.type === "equipment" && i.slot === slot).map((i) => [i.id, i.name || i.id]) },
  { t: "list", k: "skills", label: "Skills", when: () => has("skills"), inline: true, add: "Add a skill", make: () => ({}), fields: [
    { t: "select", k: "skill", label: "Skill", options: O.skills, none: "(choose)" },
    { t: "bool", k: "locked", label: "Locked at the start", redraw: false },
    { t: "number", k: "unlock_level", label: "Unlocks at level (optional)" },
  ] },
] });

const SECTIONS = [
  { id: "info", title: "Card info", intro: "What players see before they play: on the choose-a-card list, the main menu and the Card info screen.", fields: () => [
    { t: "group", k: "meta", label: "About this card", fields: [
      { t: "text", k: "title", label: "Title" },
      { t: "text", k: "id", label: "Card id", help: "Lowercase letters, digits and underscores. Names the packed file and keeps this card's saves apart from other cards'." },
      { t: "text", k: "author", label: "Author" },
      { t: "text", k: "version", label: "Version" },
      { t: "area", k: "description", label: "Short description", rows: 2 },
      { t: "tags", k: "tags", label: "Tags", help: "Separated by commas." },
      { t: "area", k: "creator_note", label: "Creator's note", rows: 5, help: "Anything you want to tell players. The AI never sees this." },
      { t: "asset", k: "cover", label: "Cover picture", help: "Shown behind the main menu.", path: (o, ext) => `assets/cover${ext}` },
    ] },
  ] },

  { id: "setup", title: "Game setup", intro: "Which systems this card uses. Anything switched off is not tracked, not shown to the player and never mentioned to the AI.", fields: () => [
    { t: "custom", render: presetButtons },
    { t: "group", k: "display", label: "How it looks", fields: [
      { t: "select", k: "mode", label: "Presentation", none: false, default: "visual", redraw: true,
        options: () => [["visual", "Visual: backgrounds, character sprites and a text box"], ["text", "Text only: a scrolling story, no character sprites"]] },
      { t: "asset", k: "background", label: "Background picture for the whole card (optional)", path: (d, ext) => `assets/bg/card${ext}`,
        help: visual() ? "Shown wherever a place has no picture of its own. Give a place its own under Locations."
          : "Shown behind the story, darkened so the text stays easy to read. Leave empty for plain black. A place can have its own picture under Locations, which is used while the player is there." },
    ] },
    { t: "group", k: "rules", label: "Systems", fields: [
      ...[["inventory", "Inventory", "Items the player and characters carry."], ["equipment", "Equipment", "Wearing and wielding items. Needs inventory."],
        ["money", "Money", "A currency, and shops if you add any."], ["levels", "Levels and experience", "The AI awards experience; the game does the levelling."],
        ["skills", "Skills", "Abilities with a cost and an effect, different for each character."],
        ["relationships", "Relationships", "How each character feels about the player, from 0 to 100."],
        ["states", "States", "Conditions like asleep, restrained or away, which can stop a character acting."]].map(([key, label, help]) =>
        ({ t: "bool", label, help, get: () => has(key), put: (on) => { (card.rules.features || (card.rules.features = {}))[key] = on; } })),
    ] },
    { t: "group", k: "rules", label: "Stats", help: "Numbers every character has, such as Health or Mana. A card with no stats tracks none.", fields: [
      { t: "list", k: "stats", keep: true, add: "Add a stat", make: () => ({ id: "", name: "", min: 0, default: 10, max: 10 }), renamed: renameStat,
        removed: () => { if (dropMissingStats()) alert("Turn-based fights need a health stat, so fights are now told by the story. You can change this under Fights once a health stat exists."); },
        title: (s) => s.name, sub: (s) => `starts at ${s.default ?? "?"}${s.max !== undefined ? " of " + s.max : ""}`, fields: [
          { t: "text", k: "name", label: "Name", feeds: "id" },
          { t: "text", k: "id", label: "Id" },
          { t: "number", k: "default", label: "Usual starting value" },
          { t: "number", k: "max", label: "Maximum", help: "Leave empty for a stat with no ceiling, such as Attack." },
          { t: "number", k: "min", label: "Minimum" },
        ] },
    ] },
    { t: "group", k: "rules", label: "Details", fields: [
      { t: "text", k: "currency_name", label: "What money is called", placeholder: "Gold", when: () => has("money") },
      { t: "bool", k: "allow_generated_locations", label: "The story may create new places", help: "Somewhere the card does not define, such as a pocket dimension or wherever a villain throws everyone. Off means the map is fixed to your locations.", default: true, redraw: false, when: () => (card.locations || []).length },
      { t: "bool", k: "allow_generated_items", label: "The AI may invent new items", help: "They have no effects. With equipment on, the AI can make one wearable. Off means only the items you define exist.", default: true, redraw: false, when: () => has("inventory") },
      { t: "text", k: "relationship_name", label: "What the relationship value is called", placeholder: "Affection", redraw: true, when: () => has("relationships") },
      { t: "group", k: "leveling", label: "Levelling", when: () => has("levels"), fields: [
        { t: "number", k: "xp_per_level", label: "Experience for the first level", placeholder: "100", help: "Each level after takes this much more: level 2 needs twice as much, level 3 three times." },
        { t: "map", k: "gains", label: "Gained on every level up", options: O.stats, help: "A stat with a maximum has its maximum raised too." },
      ] },
      { t: "group", k: "battle", label: "Fights", fields: [
        { t: "select", k: "mode", label: "How fights are played", none: false, default: "free", redraw: true,
          help: "Turn-based fights need a stat to use as health. With no stats, fights are told by the story.",
          options: () => [["free", "Told by the story: the AI narrates, the game keeps score"], ...(card.rules.stats.length ? [["system", "Turn-based: the game runs each round on a battle screen"]] : [])] },
        { t: "select", k: "health_stat", label: "The stat that is health", help: "A fighter at zero is out.", options: O.stats, none: "(choose)", when: systemBattle },
        { t: "number", k: "basic_damage", label: "Damage of a plain attack", placeholder: "2", when: systemBattle },
        { t: "select", k: "attack_stat", label: "Stat added to a plain attack (optional)", options: O.stats, when: systemBattle },
        { t: "select", k: "defense_stat", label: "Stat taken off damage received (optional)", options: O.stats, when: systemBattle },
        { t: "group", k: "on_defeat", label: "When the player is beaten", when: systemBattle, fields: [
          { t: "select", k: "type", label: "What happens", none: false, default: "survive", redraw: true,
            options: () => [["survive", "They survive and come round later"], ["game_over", "The story ends"]] },
          { t: "number", k: "health", label: "Health they come round with", placeholder: "1", when: (o) => (o.type || "survive") === "survive" },
          { t: "select", k: "location", label: "Where they come round", none: "(where they fell)", options: O.locations, when: (o) => (o.type || "survive") === "survive" },
        ] },
      ] },
    ] },
  ] },

  { id: "world", title: "World", intro: "The setting and how the story begins. The AI reads all of this on every turn.", fields: () => [
    { t: "group", k: "world", label: "World", fields: [
      { t: "area", k: "description", label: "Setting and tone", rows: 5 },
      { t: "area", k: "scenario", label: "Situation at the start", rows: 3, help: "Use {{user}} wherever the player's name belongs." },
      { t: "area", k: "opening", label: "Opening", rows: 7, help: "The first thing the player reads. Blank lines separate paragraphs." },
      { t: "area", k: "narrator_instructions", label: "Instructions for the narrator", rows: 4, help: "Guidance only the AI sees: pacing, secrets to keep, what to avoid." },
      { t: "text", k: "header_format", label: "Time and place line: its form (optional)", help: "Every reply is headed by one line giving the time, date, place and weather, which the AI keeps going as the story moves. Write the form it should take, with placeholders the AI fills in. Leave empty for the usual one: 🕰️ HH:MM AM/PM | 🗓️ Day # - DayOfWeek, Month DD, YYYY Era | 📍 Location - Specific area | WeatherEmoji Weather, Temp °F",
        placeholder: "🕰️ HH:MM | 🗓️ Day #, Month DD, Year 1 of the New Calendar | 📍 Location - Specific area | WeatherEmoji Weather, Temp °C" },
      { t: "text", k: "header_start", label: "Time and place line: where the story starts (optional)", help: "The line as it stands at the opening, filled in. Leave empty to let the AI choose a fitting start.",
        placeholder: "🕰️ 08:00 | 🗓️ Day 1, March 3, Year 1 of the New Calendar | 📍 Capital - East gate | ☀️ Clear, 18 °C" },
      { t: "select", k: "start_location", label: "Where the player starts", options: O.locations, when: () => (card.locations || []).length },
    ] },
  ] },

  { id: "characters", title: "Characters", count: () => (card.characters || []).length, intro: "Everyone the player can meet. In turn-based fights, enemies are characters too.", fields: () => [
    { t: "list", k: "characters", keep: true, add: "Add a character", make: () => ({ id: "", name: "", description: "" }), title: (c) => c.name, sub: (c) => c.id, fields: [
      { t: "text", k: "name", label: "Name", feeds: "id" },
      { t: "text", k: "id", label: "Id" },
      { t: "color", k: "color", label: "Name colour" },
      { t: "area", k: "description", label: "Who they are", rows: 3, help: "The player can read this: it is shown under the character's name in the game's People list, and it is given to the AI. Keep it to what the player may know." },
      { t: "group", k: "__ai", label: "For the AI only", flat: true, help: "Given to the AI so it can play the character. None of this is shown anywhere in the game.", fields: [
        { t: "area", k: "personality", label: "Personality", rows: 2 },
        { t: "area", k: "appearance", label: "Appearance", rows: 2, help: "What they look like. Later this also drives sprite generation." },
        { t: "area", k: "dialogue_examples", label: "How they talk", rows: 2, help: "A line or two in their voice." },
        { t: "area", k: "ai_notes", label: "Extra notes", rows: 3, help: "Anything else the AI needs that the player should not read: secrets, motives, their part in the plot, what they would never do." },
      ] },
      { t: "select", k: "location", label: "Where they are at the start", none: "(off the map until the story brings them in)", options: O.locations, when: () => (card.locations || []).length },
      { t: "number", k: "xp_reward", label: "Experience for beating them in a fight", when: () => systemBattle() && has("levels") },
      { t: "sprites", k: "sprites", label: "Sprites", help: "One image per expression. \"neutral\" is used when no other fits. Without sprites the character shows as a name card.", when: visual },
      loadout(true),
    ] },
  ] },

  { id: "persona", title: "Player", intro: "The player describes themselves in the game, on its Persona screen. This is who they are when they have not: the card's default, and what they start with.", fields: () => [
    { t: "custom", render: clearPlayerButtons },
    { t: "group", k: "default_persona", label: "Default player character", fields: [
      { t: "text", k: "name", label: "Name", placeholder: "Traveler" },
      { t: "area", k: "description", label: "Who they are", rows: 2 },
      { t: "area", k: "appearance", label: "Appearance", rows: 2 },
      loadout(false),
    ] },
  ] },

  { id: "items", title: "Items", when: () => has("inventory"), count: () => (card.items || []).length, intro: "Things that can be carried, used, worn, bought and sold.", fields: () => [
    { t: "list", k: "items", add: "Add an item", make: () => ({ id: "", name: "", type: "misc" }), title: (i) => i.name, sub: (i) => i.type + (i.slot ? ", " + i.slot : ""), fields: [
      { t: "text", k: "name", label: "Name", feeds: "id" },
      { t: "text", k: "id", label: "Id" },
      { t: "area", k: "description", label: "Description", rows: 2 },
      { t: "select", k: "type", label: "Kind", none: false, redraw: true, options: () => [["consumable", "Consumable: used up when used"], ["equipment", "Equipment: worn or wielded"], ["key", "Key item: cannot be sold"], ["misc", "Other"]] },
      { t: "select", k: "slot", label: "Where it is worn", none: "(choose)", options: () => SLOTS.map((s) => [s, cap(s)]), when: (i) => i.type === "equipment" },
      { t: "number", k: "value", label: "Price", when: () => has("money") },
      { ...effects("Effects", "A consumable applies these once when used. Equipment applies them while worn."), when: (i) => i.type === "consumable" || i.type === "equipment" },
    ] },
  ] },

  { id: "skills", title: "Skills", when: () => has("skills"), count: () => (card.skills || []).length, intro: "Abilities. Give them to characters and the player in their starting point, where each can also be locked.", fields: () => [
    { t: "list", k: "skills", add: "Add a skill", make: () => ({ id: "", name: "", target: "other" }), title: (s) => s.name, sub: (s) => (s.target === "self" ? "on oneself" : "on someone else"), fields: [
      { t: "text", k: "name", label: "Name", feeds: "id" },
      { t: "text", k: "id", label: "Id" },
      { t: "area", k: "description", label: "Description", rows: 2 },
      { t: "select", k: "target", label: "Used on", none: false, default: "other", options: () => [["other", "Someone else"], ["self", "Oneself"]] },
      { t: "map", k: "cost", label: "Cost to use", options: O.stats, help: "For example 4 in Mana." },
      effects("What it does to the target", "A negative amount is damage."),
    ] },
  ] },

  { id: "states", title: "States", when: () => has("states"), count: () => (card.states || []).length, intro: "Conditions a character can be in. The AI sets and clears them as the story goes; the game stops the player doing what their state blocks.", fields: () => [
    { t: "custom", render: () => el("div", { class: "notice" }, el("p", {}, "Every card already has these: ",
      builtinStates.map((b, n) => [n ? ", " : "", el("b", {}, b.name)]), ". Add your own below. A state with the same id as a built-in one replaces it."),
      el("p", { class: "small muted", text: "The AI may also invent states you did not list, such as \"tipsy\". Those are remembered and shown but stop nothing." })) },
    { t: "list", k: "states", add: "Add a state", make: () => ({ id: "", name: "", blocks: [] }), title: (s) => s.name,
      sub: (s) => ((s.blocks || []).includes("all") ? "stops everything" : (s.blocks || []).length ? "stops " + s.blocks.join(", ") : "stops nothing") + (s.away ? ", out of the scene" : ""), fields: [
        { t: "text", k: "name", label: "Name", feeds: "id" },
        { t: "text", k: "id", label: "Id" },
        { t: "area", k: "description", label: "What it means", rows: 2, help: "The AI reads this to know when the state applies." },
        { t: "checks", k: "blocks", label: "What it stops", options: () => Object.entries(CAPABILITY_NAMES) },
        { t: "bool", k: "away", label: "Takes the character out of the scene", help: "Not shown, and nobody can hand them things or target them.", redraw: false },
      ] },
  ] },

  { id: "locations", title: "Locations", count: () => (card.locations || []).length, intro: "Places the player can be. Connected places are one step apart on the map.", fields: () => [
    { t: "list", k: "locations", add: "Add a location", make: () => ({ id: "", name: "" }), title: (l) => l.name, sub: (l) => l.id, fields: [
      { t: "text", k: "name", label: "Name", feeds: "id" },
      { t: "text", k: "id", label: "Id" },
      { t: "area", k: "description", label: "Description", rows: 2 },
      { t: "asset", k: "background", label: "Background picture (optional)", path: (l, ext) => `assets/bg/${l.id || "location"}${ext}`,
        help: visual() ? "Shown while the player is here. Without one, the card's own background picture is used, if it has one."
          : "Shown behind the story, darkened, while the player is here. Without one, the card's own background picture is used, if it has one." },
      { t: "bool", k: "hidden", label: "Hidden at the start", redraw: false,
        help: "Not on the player's map until the story reveals it: they are told of it, find the way, or are taken there. Where the player starts is never hidden." },
      { t: "checks", k: "connections", label: "Leads to", options: (l) => O.locations().filter(([id]) => id !== l.id), empty: "Add another location to connect this one to." },
    ] },
  ] },

  { id: "shops", title: "Shops", when: () => has("money") && has("inventory"), count: () => (card.shops || []).length, intro: "Where items are bought and sold. Shops buy things back at half price.", fields: () => [
    { t: "list", k: "shops", add: "Add a shop", make: () => ({ id: "", name: "", stock: [] }), title: (s) => s.name, sub: (s) => `${(s.stock || []).length} for sale`, fields: [
      { t: "text", k: "name", label: "Name", feeds: "id" },
      { t: "text", k: "id", label: "Id" },
      { t: "select", k: "location", label: "Where it is", none: "(everywhere)", options: O.locations },
      { t: "select", k: "keeper", label: "Shopkeeper", options: O.characters },
      { t: "list", k: "stock", label: "For sale", keep: true, inline: true, add: "Add an item", make: () => ({}), fields: [
        { t: "select", k: "item", label: "Item", options: O.items, none: "(choose)" },
        { t: "number", k: "price", label: "Price (empty: the item's own)" },
        { t: "number", k: "qty", label: "In stock (empty: unlimited)" },
      ] },
    ] },
  ] },

  { id: "quests", title: "Quests", count: () => (card.quests || []).length, intro: "Goals with one or more objectives. The game marks an objective done by itself when you give it a condition it can check; otherwise the AI decides. The game tracks progress and pays the reward.", fields: () => [
    { t: "list", k: "quests", add: "Add a quest", make: () => ({ id: "", title: "", stages: [{ id: "start", description: "" }] }), title: (q) => q.title, sub: (q) => `${(q.stages || []).length} objectives`, fields: [
      { t: "text", k: "title", label: "Title", feeds: "id" },
      { t: "text", k: "id", label: "Id" },
      { t: "area", k: "description", label: "Description", rows: 2 },
      { t: "bool", k: "auto_start", label: "Active from the start of the game", redraw: true },
      { t: "group", k: "__when", label: "When it can begin", when: (q) => !q.auto_start, flat: true,
        help: "Until a quest begins, the AI is only told about it when it can actually begin, so it does not start hinting at later parts of the story. Leave all three empty for a quest that can begin anywhere, at any time.", fields: [
        { t: "select", k: "after", label: "Only after this quest is finished", options: (q) => named((card.quests || []).filter((other) => other !== q && other.id), "title") },
        { t: "select", k: "giver", label: "Handed out by", options: O.characters },
        { t: "select", k: "start_location", label: "Or begins at", options: O.locations, when: () => (card.locations || []).length },
      ] },
      { t: "area", k: "fail_when", label: "What makes it fail (optional)", rows: 2, help: "Only the AI sees this. For example: the player leaves the exam grounds before the exam is over." },
      { t: "list", k: "stages", label: "Objectives, in order", keep: true, add: "Add an objective", make: () => ({ id: "", description: "" }), title: (s) => s.description, sub: (s) => s.id, fields: [
        { t: "text", k: "description", label: "What the player sees", feeds: "id" },
        { t: "text", k: "id", label: "Id" },
        { t: "area", k: "done_when", label: "It is finished only when", rows: 2, help: "What must have happened for this objective to count as done. Be exact: \"all four tests are over\", not \"the tests\". Leave empty and the game requires everything in the description to be over. Only the AI sees this." },
        { t: "list", k: "done_if", label: "Or: the game marks it done by itself when", inline: true, add: "Add a condition", make: () => ({ type: "has_item" }),
          help: "Conditions the game checks without asking the AI, like an ordinary game. When all of them are true the objective is done, even if the player got there early. Use these whenever an objective comes down to holding something or being somewhere.", fields: [
          { t: "select", k: "type", label: "Condition", none: false, default: "has_item", redraw: true,
            options: () => [["has_item", "Someone has an item"], ["at", "Someone is at a place"], ...(has("levels") ? [["level", "The player's level is at least"]] : []), ...(has("relationships") ? [["relationship", (card.rules.relationship_name || "Relationship") + " with a character is at least"]] : [])] },
          { t: "select", k: "who", label: "Who", none: "The player", options: O.characters, when: (c) => (c.type || "has_item") === "has_item" || c.type === "at" },
          { t: "select", k: "who", label: "Character", none: "(choose)", options: O.characters, when: (c) => c.type === "relationship" },
          { t: "select", k: "item", label: "Item", none: "(choose)", options: O.items, when: (c) => (c.type || "has_item") === "has_item" },
          { t: "select", k: "location", label: "Place", none: "(choose)", options: O.locations, when: (c) => c.type === "at" },
          { t: "number", k: "at_least", label: "At least", when: (c) => c.type === "level" || c.type === "relationship" },
        ] },
        { t: "area", k: "guidance", label: "How to play this part", rows: 3, help: "Direction for the narrator: who appears, what should happen, what to keep secret. It does not decide when the objective is over. Only the AI sees this." },
      ] },
      { t: "group", k: "rewards", label: "Reward for finishing", fields: [
        { t: "number", k: "money", label: card.rules.currency_name || "Gold", when: () => has("money") },
        { ...stacks("items", "Items"), when: () => has("inventory") },
        { t: "map", k: "stats", label: "Stat changes", options: O.stats },
      ] },
    ] },
  ] },

  { id: "lorebook", title: "Lorebook", count: () => (card.lorebook || []).length, intro: "Background facts the AI is only told when they come up, so they do not crowd every turn.", fields: () => [
    { t: "custom", render: lorebookImport },
    { t: "list", k: "lorebook", add: "Add an entry", make: () => ({ id: "lore_" + ((card.lorebook || []).length + 1), content: "" }), title: (e) => (e.keys || []).join(", ") || (e.always_on ? "always on" : ""), sub: (e) => (e.content || "").slice(0, 60), fields: [
      { t: "tags", k: "keys", label: "Trigger words", help: "Separated by commas. The entry is used when one of them appears in recent turns." },
      { t: "area", k: "content", label: "What the AI is told", rows: 3 },
      { t: "bool", k: "always_on", label: "Always include it", redraw: false },
      { t: "number", k: "priority", label: "Priority", help: "Higher is kept longer when space runs short." },
      { t: "text", k: "id", label: "Id" },
    ] },
  ] },

  { id: "export", title: "Export", intro: "Check the card and pack it into a .sicard file for players.", fields: () => [{ t: "custom", render: exportPanel }] },
];

function presetButtons() {
  const apply = (name, label) => el("button", { type: "button", text: label, onclick: () => {
    if (!confirm(`Apply the ${label} preset? It replaces this card's systems, stats and fight rules.`)) return;
    const preset = JSON.parse(JSON.stringify(templates[name]));
    for (const key of ["features", "leveling", "battle", "relationship_name"]) delete card.rules[key];
    Object.assign(card.rules, preset);
    changed(true);
  } });
  return el("div", { class: "notice" }, el("p", {}, "Start from a preset, then adjust anything below. ",
    el("b", {}, "Casual"), " tracks only relationships, for slice-of-life stories. ", el("b", {}, "Classic RPG"), " adds health, mana, stamina, levels, skills, money and turn-based fights."),
    el("div", { class: "row" }, apply("casual", "Casual"), apply("rpg", "Classic RPG")));
}

// Empties the default player character in one go, after asking. Who they are and what they start
// with are cleared separately, since a card usually wants to keep one of the two.
function clearPlayerButtons() {
  const persona = card.default_persona || {};
  const about = ["name", "description", "appearance"].filter((key) => persona[key]);
  const kit = persona.start && Object.keys(persona.start).length > 0;
  const clear = (question, act) => () => {
    if (!confirm(question)) return;
    act(card.default_persona || (card.default_persona = {}));
    changed(true);
  };
  return el("div", { class: "row clear-row" },
    el("button", { type: "button", class: "danger", text: "Clear the player's info", disabled: !about.length,
      onclick: clear("Clear the default player's name, description and appearance?\n\nThis cannot be undone. What they start with is kept.", (p) => { for (const key of ["name", "description", "appearance"]) delete p[key]; }) }),
    el("button", { type: "button", class: "danger", text: "Clear what they start with", disabled: !kit,
      onclick: clear("Clear everything the player starts with (stats, money, items, equipment, skills)?\n\nThis cannot be undone. Their name, description and appearance are kept.", (p) => { delete p.start; }) }),
    el("span", { class: "muted small", text: about.length || kit ? "Each asks before it clears anything." : "There is nothing to clear." }));
}

// Adds a SillyTavern lorebook's entries to the open card. The server only converts the file; the
// entries are added here so that nothing typed but not yet saved is lost.
function lorebookImport() {
  const note = el("p", { class: "small muted" });
  const picker = el("input", { type: "file", accept: ".json,.png,application/json,image/png" });
  picker.addEventListener("change", async () => {
    if (!picker.files[0]) return;
    try {
      const { entries } = await api("POST", "/api/convert/lorebook", picker.files[0], true);
      const taken = new Set((card.lorebook || []).map((e) => e.id));
      const only = (card.characters || []).length === 1 ? card.characters[0].name : null;
      for (const { title, ...entry } of entries) {
        const base = slug(title) || "lore";
        let id = base === "lore" ? "lore_1" : base;
        for (let n = 2; taken.has(id); n++) id = `${base}_${n}`;
        taken.add(id);
        if (only) entry.content = entry.content.replaceAll("{{char}}", only);
        (card.lorebook || (card.lorebook = [])).push({ id, ...entry });
      }
      lastImport = `Added ${entries.length} ${entries.length === 1 ? "entry" : "entries"} from ${picker.files[0].name}.`;
      changed(true);
    } catch (error) { note.className = "error"; note.textContent = error.message; picker.value = ""; }
  });
  note.textContent = lastImport;
  lastImport = "";
  return el("div", { class: "panel" }, el("h3", { text: "Import from SillyTavern" }),
    el("p", { class: "muted small", text: "A World Info file (JSON), or a character card (PNG or JSON) that carries a lorebook. Its entries are added to the ones below; switched-off and empty entries are skipped." }),
    picker, note);
}

function exportPanel() {
  const result = el("div");
  const pack = el("button", { class: "primary", type: "button", text: "Create .sicard file", disabled: problems.length > 0, onclick: async () => {
    result.replaceChildren(el("p", { class: "muted", text: "Packing..." }));
    try {
      await flush();
      const done = await api("POST", `/api/projects/${pid}/pack`);
      result.replaceChildren(el("div", { class: "notice" }, el("p", {}, "Created ", el("b", {}, done.file), ` (${Math.ceil(done.size / 1024)} KB) in `, el("code", {}, done.folder), "."),
        el("a", { class: "button", href: `/api/exports/${done.file}`, text: "Download it" })));
    } catch (error) { result.replaceChildren(el("p", { class: "error", text: error.message })); }
  } });
  return el("div", {},
    problems.length
      ? el("div", { class: "problems" }, el("b", {}, `${problems.length} thing${problems.length === 1 ? "" : "s"} to fix before this card can be played:`), el("ul", {}, problems.map((p) => el("li", { text: p }))))
      : el("div", { class: "notice", text: "This card passes every check the game makes." }),
    el("div", { class: "panel" }, el("h3", { text: "For players" }),
      el("p", { class: "muted", text: "A .sicard is the whole card, pictures included, sealed in one file. Players insert it with Choose, then Import a card file." }), pack, result),
    el("div", { class: "panel" }, el("h3", { text: "While you are making it" }),
      el("p", { class: "muted" }, "No need to pack. This card is saved as the folder ", el("code", {}, pid), " in the game's cards folder, so it already appears under Choose in SI-Station. Eject and insert it again to pick up changes.")));
}

// Every part of a preset page says which of two kinds it is, so there is no guessing what can be changed.
const tag = (editable) => el("span", { class: "tag " + (editable ? "yours" : "builtin"), text: editable ? "Editable" : "Built in" });
const legend = () => el("p", { class: "legend" }, tag(true), " is yours to write and is saved in this preset. ", tag(false), " is filled in by the game each time it is sent, and cannot be changed here.");

// ---------- presets
//
// A preset is one file: the instructions the story model is given, the game's own prompts it
// rewords, and starting values for sampling. It is edited the same way a card is: one object,
// written to by the controls and saved after every change.

function savePreset() {
  const id = presetId, body = JSON.parse(JSON.stringify(preset));
  if (presetBuiltin) return saving;
  for (const key of ["sampling", "context", "suggestions", "prompts"]) if (body[key] && !Object.keys(body[key]).length) delete body[key];
  setStatus("Saving...");
  saving = saving.then(() => api("PUT", `/api/presets/${id}`, body)).then((reply) => {
    if (id !== presetId) return;
    problems = reply.problems;
    setStatus("Saved");
    showProblems();
  }).catch((error) => setStatus("Not saved: " + error.message));
  return saving;
}

function presetProblems() {
  return el("div", { class: "preset-problems" }, problems.length > 0 && el("div", { class: "problems" },
    el("b", {}, `${problems.length} thing${problems.length === 1 ? "" : "s"} to fix before the game can use this preset:`), el("ul", {}, problems.map((p) => el("li", { text: p })))));
}

async function copyPreset(source, name) {
  await flush();
  const made = await api("POST", "/api/presets", { name, copy_of: source });
  location.hash = `#/preset/${made.id}/instructions`;
}

// The instructions, in the order they are sent. Text ones are the author's; the others are parts
// the game fills in, which can only be moved or switched off.
function blocksPanel() {
  const blocks = preset.blocks;
  const move = (index, step) => { [blocks[index], blocks[index + step]] = [blocks[index + step], blocks[index]]; changed(true); };
  const add = (afterStory) => {
    const taken = new Set(blocks.map((b) => b.id));
    let n = 1;
    while (taken.has(`custom_${n}`)) n++;
    const block = { id: `custom_${n}`, name: "My instruction", kind: "text", content: "", enabled: true };
    const story = blocks.findIndex((b) => b.slot === "history");
    blocks.splice(afterStory || story < 0 ? blocks.length : story, 0, block);
    openItems.add("preset/" + block.id);
    changed(true);
  };
  const rows = blocks.map((block, index) => {
    const own = block.kind === "text", fixed = block.slot === "action_protocol";
    const tick = el("input", { type: "checkbox", disabled: fixed, title: "Use this" });
    tick.checked = fixed || block.enabled !== false;
    tick.addEventListener("click", (event) => event.stopPropagation());
    tick.addEventListener("change", () => { block.enabled = tick.checked; changed(true); });
    const title = el("span", { class: "title", text: block.name || block.id });
    const sub = el("span", { class: "sub", text: own ? (block.content || "(empty)").slice(0, 90) : slotNotes[block.slot] || "" });
    const button = (text, act, disabled) => el("button", { class: "quiet", type: "button", text, disabled, onclick: (event) => { event.preventDefault(); act(); } });
    const summary = el("summary", {}, tick, title, tag(own), sub, button("Up", () => move(index, -1), index === 0), button("Down", () => move(index, 1), index === blocks.length - 1),
      own && button("Remove", () => { if (confirm(`Remove "${block.name || block.id}"?`)) { blocks.splice(index, 1); changed(true); } }));
    let body;
    if (own) {
      const name = el("input", { type: "text", value: block.name || "" });
      name.addEventListener("input", () => { block.name = name.value; title.textContent = name.value || block.id; changed(false); });
      const help = el("input", { type: "text", value: block.help || "" });
      help.addEventListener("input", () => { set(block, "help", help.value); changed(false); });
      const text = el("textarea", { rows: 7 });
      text.value = block.content || "";
      text.addEventListener("input", () => { block.content = text.value; sub.textContent = (text.value || "(empty)").slice(0, 90); changed(false); });
      body = [labelled({ label: "Name" }, name), labelled({ label: "One-line note for players (optional)", help: "Shown under the name on the game's Preset screen." }, help),
        labelled({ label: "What the story model is told", help: "A plain instruction. Use {{user}} for the player's name and {{currency}} for the card's money." }, text)];
    } else {
      body = [el("p", { class: "muted", text: (slotNotes[block.slot] || "") + " The game writes this part from the card and the save. Here you can only move it or switch it off." })];
    }
    const details = el("details", { class: "item " + (own ? "yours" : "builtin") + (tick.checked ? "" : " off") }, summary, el("div", { class: "body" }, body));
    details.open = openItems.has("preset/" + block.id);
    details.addEventListener("toggle", () => (details.open ? openItems.add("preset/" + block.id) : openItems.delete("preset/" + block.id)));
    return [details, block.slot === "history" && el("p", { class: "muted small divider", text: "Above the story: sent once and cached, so it costs little. Below it, the game's changing parts are sent just before what the player typed, and your own instructions just after it, as the very last thing the model reads. That costs a few tokens every turn and is what models heed most." })];
  });
  return el("div", { class: "list" },
    el("div", { class: "row preview" }, el("button", { class: "primary", type: "button", text: "Preview one turn as it is sent", onclick: turnPreview }),
      el("span", { class: "muted small", text: "The whole request for one turn, as JSON, exactly as the story model receives it." })),
    legend(), rows,
    el("div", { class: "row" }, el("button", { type: "button", text: "Add an instruction", onclick: () => add(false) }),
      el("button", { type: "button", text: "Add a reminder after the story", onclick: () => add(true) })));
}

// The game's own prompts. Each shows the wording in use; typing in it makes it this preset's own,
// and putting the original back removes it from the preset again.
function wordingPanel(reader) {
  return el("div", { class: "list" }, legend(), wording.filter((entry) => entry.reader === reader).map((entry) => {
    const mine = () => (preset.prompts || {})[entry.key];
    const title = el("span", { class: "title", text: entry.title }), sub = el("span", { class: "sub" }), warn = el("p", { class: "error small" });
    const text = el("textarea", { rows: Math.min(24, Math.max(8, Math.ceil(entry.text.length / 110))), spellcheck: "false" });
    const reset = el("button", { class: "quiet", type: "button", text: "Put the original back", onclick: () => { delete preset.prompts[entry.key]; text.value = entry.text; refresh(); changed(false); } });
    const refresh = () => {
      const own = mine() !== undefined;
      sub.textContent = own ? "reworded in this preset" : "the game's own wording";
      sub.className = "sub" + (own ? " changed" : "");
      reset.hidden = !own;
      const lost = Object.keys(entry.parts).filter((part) => !text.value.includes(`{{${part}}}`));
      warn.textContent = lost.length ? `Your text no longer has ${lost.map((part) => `{{${part}}}`).join(", ")}. The game will add ${lost.length === 1 ? "it" : "them"} at the end; put ${lost.length === 1 ? "it" : "them"} back where ${lost.length === 1 ? "it belongs" : "they belong"} if that reads badly.` : "";
    };
    text.value = mine() ?? entry.text;
    text.addEventListener("input", () => {
      const prompts = preset.prompts || (preset.prompts = {});
      if (!text.value.trim() || text.value === entry.text) delete prompts[entry.key]; else prompts[entry.key] = text.value;
      refresh();
      changed(false);
    });
    refresh();
    const parts = Object.entries(entry.parts);
    const details = el("details", { class: "item" }, el("summary", {}, title, sub),
      el("div", { class: "body" }, el("p", { class: "muted", text: entry.help }),
        el("div", { class: "sent" },
          el("div", { class: "zone yours" },
            el("div", { class: "step" }, tag(true), el("b", {}, reader === "helper" ? "1. The instruction" : "This wording"), el("span", { class: "muted small", text: entry.where })),
            text, warn, reset,
            parts.length > 0 && (() => {
              const made = parts.map(([name, what]) => [explained(entry, `{{${name}}}`, entry.part_sources[name], (sent) => sent.parts[name] || null), what]);
              return [el("p", { class: "small" }, tag(false), " inside your text: ", made.map(([piece, what], i) => [i > 0 && "; ", piece.chip, ` is ${what}`]),
                ". Write around ", parts.length === 1 ? "it" : "them", " and leave ", parts.length === 1 ? "it" : "them", " where the list should appear; the game puts the list there. Click one to see what it holds."),
                made.map(([piece]) => piece.box)];
            })(),
            entry.text.includes("JSON") && el("p", { class: "small", text: "Keep the part that says how to reply (the JSON shape). The game reads the reply by that shape, so a different shape means the reply is ignored." })),
          entry.sends.length > 0 && el("div", { class: "zone builtin" },
            el("div", { class: "step" }, tag(false), el("b", {}, "2. Sent under it, in this order"), el("span", { class: "muted small", text: "The game writes these each time. They are not part of the preset." })),
            el("ol", { class: "sections" }, entry.sends.map((section) => {
              const piece = explained(entry, section.heading, section, (sent) => sectionText(entry, section.heading, sent.messages[0].content));
              return el("li", {}, piece.chip, " ", section.what, piece.box);
            })),
            el("p", { class: "muted small", text: "Click a section to see where it comes from and what it holds." })),
          previewBox(entry))));
    details.open = openItems.has("wording/" + entry.key);
    details.addEventListener("toggle", () => (details.open ? openItems.add("wording/" + entry.key) : openItems.delete("wording/" + entry.key)));
    return details;
  }));
}

// A built-in piece of a prompt, as something to click: a [section] sent under the instruction, or
// a {{part}} inside it. Opening it says where the piece comes from, where (if anywhere) it can be
// changed, and shows what it holds right now for the card chosen for previews.
function explained(entry, label, told, pick) {
  const box = el("div", { class: "explain", hidden: true });
  const chip = el("button", { class: "chip", type: "button", text: label, title: "Where does this come from?", onclick: async () => {
    box.hidden = !box.hidden;
    chip.classList.toggle("open", !box.hidden);
    if (box.hidden) return;
    const [kind, target] = (told.link || ":").split(":");
    const cardTitle = (previewCards.find(([id]) => id === previewCard) || ["", "a card"])[1];
    const pages = Object.fromEntries([...SECTIONS, ...PRESET_SECTIONS].map((s) => [s.id, s.title]));
    const [page, item] = (target || "").split("/");
    const where = kind === "card" ? (previewCard ? el("a", { href: `#/card/${previewCard}/${page}`, text: `Change it in the card editor, on the ${pages[page]} page (opens ${cardTitle})` }) : `Change it in the card editor, on the ${pages[page]} page.`)
      : kind === "preset" ? el("a", { href: `#/preset/${presetId}/${target}`, text: item ? `It follows from another prompt: ${wording.find((e) => e.key === item).title}` : `Change it in this preset, on the ${pages[page]} page` })
      : "This is the game's own. It cannot be changed from a preset or a card.";
    const shown = el("pre", { class: "sent-text builtin", text: "Building..." });
    box.replaceChildren(el("p", {}, el("b", {}, "Where it comes from. "), told.source), el("p", { class: "small" }, where),
      el("div", { class: "sent-label", text: previewCard ? `What it holds right now, for ${cardTitle}` : "" }), previewCard ? shown : "");
    if (!previewCard) return;
    try {
      const text = pick(await api("POST", "/api/preview", { preset, card: previewCard, key: entry.key }));
      shown.textContent = text ?? "(Not sent for this card: it has nothing of this kind.)";
    } catch (error) { shown.textContent = error.message; }
  } });
  if (openItems.delete(`explain/${entry.key}/${label}`)) setTimeout(() => chip.click(), 0);     // a link asked for it
  return { chip, box };
}

// The text of one headed section of a message: from its heading to the next heading the job sends.
function sectionText(entry, heading, message) {
  const at = (h) => (message.startsWith(h + "\n") ? 0 : message.indexOf("\n" + h + "\n") + 1 || -1);
  const start = at(heading);
  if (start < 0) return null;
  const later = entry.sends.map((s) => at(s.heading)).filter((index) => index > start);
  return message.slice(start, later.length ? Math.min(...later) : undefined).trim();
}

// One whole turn, as the request the game sends to the story model, in a window over the page.
// It is built by the game's own code from this preset as it stands, saved or not.
function turnPreview() {
  if (!previewCards.length) return alert("Make a card with no problems first; the preview is built from a real card.");
  let provider = "openrouter", bookkeeper = true, check = false, header = true, json = "", unfolded = false;
  // Strict JSON writes every line break inside a text as \n, which makes long instructions one unbroken block. Unfolding them is for reading only.
  const draw = () => { body.textContent = unfolded ? json.replace(/\\n/g, "\n") : json; };
  const body = el("pre", { class: "json" }), note = el("p", { class: "muted small" }), size = el("span", { class: "muted small" });
  const close = () => { shade.remove(); document.removeEventListener("keydown", onKey); };
  const onKey = (event) => { if (event.key === "Escape") close(); };
  const load = async () => {
    body.textContent = "Building...";
    try {
      const sent = await api("POST", "/api/preview", { preset, card: previewCard, turn: true, provider, bookkeeper, check, header });
      json = JSON.stringify(sent.request, null, 2);
      draw();
      note.textContent = `POST ${sent.url}`;
      const text = JSON.stringify(sent.request);
      size.textContent = `${sent.request.messages.length} messages, about ${Math.round(text.length / 3.5).toLocaleString()} tokens`;
    } catch (error) { json = ""; body.textContent = error.message; note.textContent = ""; size.textContent = ""; }
  };
  const unfold = el("input", { type: "checkbox" });
  unfold.addEventListener("change", () => { unfolded = unfold.checked; draw(); });
  const tick = el("input", { type: "checkbox" });
  tick.checked = true;
  tick.addEventListener("change", () => { bookkeeper = tick.checked; load(); });
  const checking = el("input", { type: "checkbox" });
  checking.addEventListener("change", () => { check = checking.checked; load(); });
  const clock = el("input", { type: "checkbox" });
  clock.checked = true;
  clock.addEventListener("change", () => { header = clock.checked; load(); });
  const copy = el("button", { type: "button", text: "Copy", onclick: async () => { await navigator.clipboard.writeText(json); copy.textContent = "Copied"; setTimeout(() => (copy.textContent = "Copy"), 1200); } });
  const shade = el("div", { class: "shade", onclick: (event) => { if (event.target === shade) close(); } },
    el("div", { class: "window" },
      el("div", { class: "window-top" }, el("h3", { text: "One turn, as it is sent to the story model" }), el("button", { class: "quiet", type: "button", text: "Close", onclick: close })),
      el("div", { class: "row" },
        el("label", { class: "small" }, "Card", selectControl(previewCards, previewCard, false, (value) => { previewCard = value; load(); })),
        el("label", { class: "small" }, "Sent to", selectControl([["openrouter", "OpenRouter"], ["nanogpt", "Nano-GPT"], ["anthropic", "Anthropic"], ["custom", "Custom (OpenAI-style)"], ["local", "Local"]], provider, false, (value) => { provider = value; load(); })),
        el("label", { class: "check" }, tick, el("span", { text: "Player has the bookkeeper on" })),
        el("label", { class: "check" }, checking, el("span", { text: "Player has Check before writing on" })),
        el("label", { class: "check" }, clock, el("span", { text: "Player has the time and place line on" })),
        el("label", { class: "check" }, unfold, el("span", { text: "Show line breaks (easier to read; Copy still gives exact JSON)" })),
        copy, size),
      note,
      el("p", { class: "muted small", text: "This is the body of the request, made by the same code the game runs. The model name, the story so far and the player's lines are stand-ins; in a real game the player's own Parameters replace the sampling numbers. The last message is the one that changes every turn; everything before it is what providers cache." }),
      body));
  document.body.append(shade);
  document.addEventListener("keydown", onKey);
  load();
}

// Shows one prompt exactly as the game would send it, built by the game's own code from a real
// card and this preset as it stands on the page, saved or not.
function previewBox(entry) {
  if (!previewCards.length) return el("p", { class: "muted small", text: "Make a card with no problems to see this prompt exactly as it is sent." });
  const out = el("div");
  const pick = selectControl(previewCards, previewCard, false, (value) => { previewCard = value; });
  const show = el("button", { type: "button", text: "Show exactly what is sent", onclick: async () => {
    out.replaceChildren(el("p", { class: "muted small", text: "Building..." }));
    try {
      const sent = await api("POST", "/api/preview", { preset, card: previewCard, key: entry.key });
      const part = (label, content, editable) => [el("div", { class: "sent-label" }, tag(editable), " ", label), el("pre", { class: "sent-text " + (editable ? "yours" : "builtin"), text: content })];
      out.replaceChildren(el("div", {}, el("p", { class: "muted small", text: "Lines in brackets that stand in for the story are placeholders; everything else is what the game sends." }),
        entry.reader === "helper"
          ? part("Instruction (system): your wording, with the game's lists put in", sent.system, true)
          : part("The story model's instructions (system): this wording is one part of it, among the preset's instructions and the card", sent.system, true),
        sent.messages.map((message, index) => part(`Message ${index + 1} of ${sent.messages.length}, from the ${message.role === "user" ? "player's side (user)" : "story model (assistant)"}`, message.content, false))));
    } catch (error) { out.replaceChildren(el("p", { class: "error small", text: error.message })); }
  } });
  if (openItems.delete("preview/" + entry.key)) setTimeout(() => show.click(), 0);     // a link asked for it: #/preset/ID/helpers/KEY/preview
  return el("div", { class: "preview" }, el("div", { class: "row" }, show, el("label", { class: "muted small" }, "using the card ", pick)), out);
}

const PRESET_SECTIONS = [
  { id: "instructions", title: "Instructions", count: () => preset.blocks.filter((b) => b.kind === "text").length,
    intro: "What the story model is told before it writes, top to bottom. Tick an instruction to use it, open it to change its wording, and use Up and Down to reorder. The greyed entries are parts the game fills in from the card and the save.",
    render: () => [blocksPanel()] },
  { id: "story", title: "The game's rules, as told to the story model", count: () => wording.filter((e) => e.reader === "story" && (preset.prompts || {})[e.key] !== undefined).length || "",
    intro: "The game writes this part itself: it sits where the instruction list says \"Game mechanics\". Reword it when a model keeps misreading the rules. Which of these are sent depends on the card and on whether the player has the bookkeeper on.",
    render: () => [wordingPanel("story")] },
  { id: "helpers", title: "Helper jobs", count: () => wording.filter((e) => e.reader === "helper" && (preset.prompts || {})[e.key] !== undefined).length || "",
    intro: "Each small job the helper model does is one call: an instruction you can reword, and under it the material the game sends for that job. Both are shown here in the order they go out. Every new helper job the game gains appears here by itself.",
    render: () => [wordingPanel("helper")] },
  { id: "settings", title: "About and starting values", intro: "The preset's name, and the values a player starts from. Players can change the numbers for themselves in the game, under Parameters.",
    render: () => renderFields(preset, [
      { t: "text", k: "name", label: "Name", help: "Shown on the game's Preset screen." },
      { t: "area", k: "description", label: "Description", rows: 2 },
      { t: "group", k: "sampling", label: "How the story model writes", help: "Leave a box empty to let the provider decide.", fields: [
        { t: "number", k: "temperature", label: "Temperature (0 to 2; higher is more varied)" },
        { t: "number", k: "max_tokens", label: "Response length, in tokens" },
        { t: "number", k: "top_p", label: "Top P" },
        { t: "number", k: "top_k", label: "Top K" },
        { t: "number", k: "frequency_penalty", label: "Frequency penalty" },
        { t: "number", k: "presence_penalty", label: "Presence penalty" },
      ] },
      { t: "group", k: "context", label: "Memory", fields: [
        { t: "number", k: "max_context_tokens", label: "Context size, in tokens", help: "Only used when the game cannot tell what the chosen model can take." },
        { t: "number", k: "summarize_after_turns", label: "Fold turns older than this into the summary (0 turns it off)" },
      ] },
      { t: "group", k: "suggestions", label: "Suggested replies", fields: [
        { t: "bool", k: "enabled", label: "Offer suggested replies", default: true, redraw: false },
        { t: "number", k: "count", label: "How many (1 to 6)" },
      ] },
    ], "preset") },
];

function renderPreset() {
  const main = document.querySelector(".main");
  if (!main) return;
  const scroll = main.scrollTop;
  const spec = PRESET_SECTIONS.find((s) => s.id === presetSection) || PRESET_SECTIONS[0];
  const body = el("div", { class: presetBuiltin ? "readonly" : "" }, spec.render());
  // The game's own preset is for reading: nothing in it can be changed, but looking at how a prompt is sent is still allowed.
  if (presetBuiltin) for (const control of body.querySelectorAll("input, textarea, select, button")) if (!control.closest(".preview") && !control.classList.contains("chip")) control.disabled = true;
  main.replaceChildren(el("div", { class: "section" }, el("h2", { text: spec.title }), el("p", { class: "intro", text: spec.intro }),
    presetBuiltin && el("div", { class: "notice" }, el("p", { text: "This is the game's own preset. It can be read here but not changed, so there is always a known-good one to go back to." }),
      el("button", { class: "primary", type: "button", text: "Make a copy to edit", onclick: () => copyPreset("default", "My preset").catch((error) => alert(error.message)) })),
    presetProblems(), body));
  main.scrollTop = scroll;
  document.querySelector(".sidebar").replaceChildren(...PRESET_SECTIONS.map((s) =>
    el("a", { class: s.id === presetSection ? "active" : "", href: `#/preset/${presetId}/${s.id}` }, s.title, s.count && el("span", { class: "count", text: s.count() }))));
  document.querySelector(".topbar h1").textContent = preset.name || "Untitled preset";
}

async function openPreset(id, wanted, entry, preview) {
  if (id !== presetId) {
    await flush();
    const [reply, shared, shelf] = await Promise.all([api("GET", `/api/presets/${id}`), api("GET", "/api/presets"), api("GET", "/api/projects")]);
    previewCards = shelf.projects.filter((p) => !p.problems).map((p) => [p.id, p.title]);
    if (!previewCards.some(([cardId]) => cardId === previewCard)) previewCard = (previewCards[0] || [""])[0];
    pid = null; card = null;
    presetId = id; preset = reply.preset; problems = reply.problems; presetBuiltin = reply.builtin;
    wording = shared.wording; slotNotes = shared.slots;
    preset.blocks = preset.blocks || [];
    openItems.clear();
    app.replaceChildren(el("div", { class: "editor" },
      el("div", { class: "topbar" }, el("a", { class: "button", href: "#/", text: "All cards and presets" }), el("h1"), el("span", { class: "status", text: presetBuiltin ? "Read only" : "Saved" }),
        el("span", { class: "badge" }),
        el("a", { class: "button", href: `/api/presets/${id}/download`, title: "Saves this preset as a file to share. In the game: Menu, Settings, Preset, Import a preset file.",
          onclick: async (event) => { event.preventDefault(); await flush(); location.href = `/api/presets/${id}/download`; } }, "Export"),
        !presetBuiltin && el("button", { class: "button", type: "button", text: "Duplicate", onclick: () => copyPreset(presetId, (preset.name || "Preset") + " copy").catch((error) => alert(error.message)) }), looksControls()),
      el("nav", { class: "sidebar" }), el("main", { class: "main" })));
    showProblems();
  }
  presetSection = PRESET_SECTIONS.some((s) => s.id === wanted) ? wanted : "instructions";
  // A link can name one entry to show unfolded: #/preset/ID/helpers/judge_quests
  if (entry) openItems.add((presetSection === "instructions" ? "preset/" : "wording/") + entry);
  if (entry && preview === "preview") openItems.add("preview/" + entry);
  else if (entry && preview) openItems.add(`explain/${entry}/${decodeURIComponent(preview)}`);       // #/preset/ID/helpers/summarize/[New scenes]
  document.querySelector(".main").scrollTop = 0;
  renderPreset();
  if (wanted === "instructions" && entry === "turn") turnPreview();          // a link asked for it: #/preset/ID/instructions/turn
}

// ---------- screens

function renderSection() {
  const main = document.querySelector(".main");
  if (!main) return;
  const scroll = main.scrollTop;
  const spec = SECTIONS.find((s) => s.id === section) || SECTIONS[0];
  main.replaceChildren(el("div", { class: "section" }, el("h2", { text: spec.title }), el("p", { class: "intro", text: spec.intro }), renderFields(card, spec.fields(), spec.id)));
  main.scrollTop = scroll;
  renderSidebar();
}

function renderSidebar() {
  const side = document.querySelector(".sidebar");
  side.replaceChildren(...SECTIONS.filter((s) => !s.when || s.when()).flatMap((s) => [
    s.id === "export" && el("hr"),
    el("a", { class: s.id === section ? "active" : "", href: `#/card/${pid}/${s.id}` }, s.title, s.count && el("span", { class: "count", text: s.count() })),
  ]).filter(Boolean));
  document.querySelector(".topbar h1").textContent = card.meta.title || "Untitled card";
}

async function openEditor(project, wanted, entry) {
  if (project !== pid) {
    await flush();
    presetId = null; preset = null;
    const reply = await api("GET", `/api/projects/${project}`);
    pid = project; card = reply.card; problems = reply.problems;
    card.meta = card.meta || {}; card.world = card.world || {}; card.rules = card.rules || {}; card.rules.stats = card.rules.stats || []; card.characters = card.characters || [];
    openItems.clear();
    // "hint" used to hold both direction for the narrator and the finishing condition. It is now
    // called guidance, with the condition in its own field.
    let renamed = false;
    for (const quest of card.quests || []) for (const stage of quest.stages || []) {
      if (stage.hint !== undefined) { if (!stage.guidance) stage.guidance = stage.hint; delete stage.hint; renamed = true; }
    }
    if (renamed) setTimeout(() => changed(false), 0);
    // Repairs a card saved before stat references were kept in step.
    const repaired = JSON.stringify(card);
    dropMissingStats();
    if (JSON.stringify(card) !== repaired) setTimeout(() => changed(false), 0);
    app.replaceChildren(el("div", { class: "editor" },
      el("div", { class: "topbar" }, el("a", { class: "button", href: "#/", text: "All cards and presets" }), el("h1"), el("span", { class: "status", text: "Saved" }),
        el("button", { class: "badge", onclick: () => { location.hash = `#/card/${pid}/export`; } }),
        el("a", { class: "button", href: `#/card/${project}/export`, text: "Export" }), looksControls()),
      el("nav", { class: "sidebar" }), el("main", { class: "main" })));
    showProblems();
  }
  section = SECTIONS.some((s) => s.id === wanted) ? wanted : "info";
  // A link can name one entry of the page's list to show unfolded, by position: #/card/ID/characters/1
  if (entry !== undefined && /^\d+$/.test(entry)) openItems.add(`${section}/${section}/${entry}`);
  document.querySelector(".main").scrollTop = 0;
  renderSection();
}

async function openHome() {
  await flush();
  pid = null; card = null; presetId = null; preset = null;
  const [{ projects, folder }, shelf] = await Promise.all([api("GET", "/api/projects"), api("GET", "/api/presets")]);
  const presetName = el("input", { type: "text", placeholder: "Name of your preset" });
  const presetFrom = el("select", {}, shelf.presets.map((p) => el("option", { value: p.id, text: "Copy of " + p.name })));
  const presetFile = el("input", { type: "file", accept: ".json,application/json" });
  presetFile.addEventListener("change", async () => {
    if (!presetFile.files[0]) return;
    try {
      const made = await api("POST", `/api/presets/import?name=${encodeURIComponent(presetFile.files[0].name)}`, presetFile.files[0], true);
      location.hash = `#/preset/${made.id}/instructions`;
    } catch (problem) { error.textContent = problem.message; presetFile.value = ""; }
  });
  const makePreset = async () => {
    try { await copyPreset(presetFrom.value || undefined, presetName.value); } catch (problem) { error.textContent = problem.message; }
  };
  const title = el("input", { type: "text", placeholder: "Name of your card" });
  const template = el("select", {}, el("option", { value: "casual", text: "Casual / Slice of Life" }), el("option", { value: "rpg", text: "Classic RPG" }), el("option", { value: "blank", text: "Blank" }));
  const error = el("p", { class: "error" });
  const create = async () => {
    try {
      const made = await api("POST", "/api/projects", { title: title.value, template: template.value });
      location.hash = `#/card/${made.id}/info`;
    } catch (problem) { error.textContent = problem.message; }
  };
  const picker = el("input", { type: "file", accept: ".png,.json,image/png,application/json" });
  picker.addEventListener("change", async () => {
    if (!picker.files[0]) return;
    try {
      const made = await api("POST", "/api/import/sillytavern", picker.files[0], true);
      location.hash = `#/card/${made.id}/info`;
    } catch (problem) { error.textContent = problem.message; picker.value = ""; }
  });
  app.replaceChildren(el("div", { class: "home" },
    el("header", {}, el("h1", { text: "Card Creator" }), el("span", { class: "brand", text: "SI-Station" }), looksControls()),
    el("div", { class: "cards" }, projects.map((p) => el("div", { class: "card-tile", onclick: () => { location.hash = `#/card/${p.id}/info`; } },
      el("h3", { text: p.title }), p.author && el("span", { class: "muted small", text: "by " + p.author }), el("span", { class: "small", text: p.description }),
      el("div", { class: "row" }, el("span", { class: "small " + (p.problems ? "error" : "muted"), text: p.problems ? `${p.problems} to fix` : "Ready to play" }),
        el("button", { class: "quiet", text: "Move to trash", onclick: async (event) => {
          event.stopPropagation();
          if (!confirm(`Move "${p.title}" to the trash? It goes to the .trash folder inside the cards folder, not deleted.`)) return;
          await api("DELETE", `/api/projects/${p.id}`);
          openHome();
        } }))))),
    el("div", { class: "panel" }, el("h3", { text: "Make a new card" }), el("p", { class: "muted small", text: "The starting point only sets which systems are on. You can change every one of them later." }),
      el("div", { class: "row" }, el("label", {}, "Title", title), el("label", {}, "Starting point", template), el("button", { class: "primary", text: "Create", onclick: create }))),
    el("div", { class: "panel" }, el("h3", { text: "Import a SillyTavern character" }),
      el("p", { class: "muted small", text: "A character card as a PNG or JSON file. It becomes a casual card with that one character and its lorebook; you add places, items and the rest here." }), picker),
    el("header", { class: "second" }, el("h1", { text: "Presets" })),
    el("p", { class: "muted" }, "A preset is what the models are told, for any card: the instructions to the story model, the wording of the game's own rules, and the prompt of each helper job. Players pick one on the game's Preset screen."),
    el("div", { class: "cards" }, shelf.presets.map((p) => el("div", { class: "card-tile", onclick: () => { location.hash = `#/preset/${p.id}/instructions`; } },
      el("h3", { text: p.name }), p.builtin && el("span", { class: "muted small", text: "comes with the game" }), el("span", { class: "small", text: p.description }),
      el("div", { class: "row" }, el("span", { class: "small " + (p.problems ? "error" : "muted"), text: p.problems ? `${p.problems} to fix` : "Ready to use" }),
        !p.builtin && el("button", { class: "quiet", text: "Move to trash", onclick: async (event) => {
          event.stopPropagation();
          if (!confirm(`Move "${p.name}" to the trash? It goes to the .trash folder inside the presets folder, not deleted.`)) return;
          await api("DELETE", `/api/presets/${p.id}`);
          openHome();
        } }))))),
    el("div", { class: "panel" }, el("h3", { text: "Make a new preset" }), el("p", { class: "muted small", text: "A new preset starts as a copy of another, so it works from the first moment." }),
      el("div", { class: "row" }, el("label", {}, "Name", presetName), el("label", {}, "Starting point", presetFrom), el("button", { class: "primary", text: "Create", onclick: makePreset }))),
    el("div", { class: "panel" }, el("h3", { text: "Import a preset file" }),
      el("p", { class: "muted small", text: "A .preset.json file somebody shared, or one you exported from here. It is added beside your presets; nothing is replaced." }), presetFile),
    error, el("p", { class: "muted small" }, "Cards are kept in ", el("code", {}, folder), ", presets in ", el("code", {}, shelf.folder))));
}

async function route() {
  const parts = location.hash.replace(/^#\/?/, "").split("/");
  try {
    if (parts[0] === "card" && parts[1]) await openEditor(parts[1], parts[2], parts[3]);
    else if (parts[0] === "preset" && parts[1]) await openPreset(parts[1], parts[2], parts[3], parts[4]);
    else await openHome();
  } catch (error) {
    app.replaceChildren(el("div", { class: "home" }, el("p", { class: "error", text: error.message }), el("a", { class: "button", href: "#/", text: "All cards" })));
  }
}

window.addEventListener("hashchange", route);
window.addEventListener("beforeunload", () => { if (saveTimer) save(); });
const shared = await api("GET", "/api/templates");
templates = shared.rules;
builtinStates = shared.states;
route();
