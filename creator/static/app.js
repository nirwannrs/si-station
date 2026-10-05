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
const openItems = new Set();     // which list entries are unfolded, by path
const autoIds = new WeakSet();   // entries made this session whose id still follows their name

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
  for (const who of [...(card.characters || []), card.default_persona || {}]) if (who.start) maps.push([who.start, "stats"]);
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
  badge.textContent = problems.length ? problems.length + (problems.length === 1 ? " problem" : " problems") : "Ready to play";
  if (section === "export") renderSection();
}

function save() {
  clearTimeout(saveTimer);
  saveTimer = null;
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
  if (redraw) renderSection();
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
        renderFields(obj[f.k] || (obj[f.k] = {}), f.fields, `${path}/${f.k}`));
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
        options: () => [["visual", "Visual: backgrounds, character sprites and a text box"], ["text", "Text only: a scrolling story, no pictures"]] },
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
      { t: "bool", k: "allow_generated_items", label: "The AI may invent new items", help: "They are plain items with no effects. Off means only the items you define exist.", default: true, redraw: false, when: () => has("inventory") },
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
      { t: "select", k: "start_location", label: "Where the player starts", options: O.locations, when: () => (card.locations || []).length },
    ] },
  ] },

  { id: "characters", title: "Characters", count: () => (card.characters || []).length, intro: "Everyone the player can meet. In turn-based fights, enemies are characters too.", fields: () => [
    { t: "list", k: "characters", keep: true, add: "Add a character", make: () => ({ id: "", name: "", description: "" }), title: (c) => c.name, sub: (c) => c.id, fields: [
      { t: "text", k: "name", label: "Name", feeds: "id" },
      { t: "text", k: "id", label: "Id" },
      { t: "color", k: "color", label: "Name colour" },
      { t: "area", k: "description", label: "Who they are", rows: 3, help: "Background and their part in the story." },
      { t: "area", k: "personality", label: "Personality", rows: 2 },
      { t: "area", k: "appearance", label: "Appearance", rows: 2, help: "What they look like. Later this also drives sprite generation." },
      { t: "area", k: "dialogue_examples", label: "How they talk", rows: 2, help: "A line or two in their voice." },
      { t: "select", k: "location", label: "Where they are at the start", none: "(off the map until the story brings them in)", options: O.locations, when: () => (card.locations || []).length },
      { t: "number", k: "xp_reward", label: "Experience for beating them in a fight", when: () => systemBattle() && has("levels") },
      { t: "sprites", k: "sprites", label: "Sprites", help: "One image per expression. \"neutral\" is used when no other fits. Without sprites the character shows as a name card.", when: visual },
      loadout(true),
    ] },
  ] },

  { id: "persona", title: "Player", intro: "The player describes themselves in the game. These are the defaults, and what they start with.", fields: () => [
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
      { t: "asset", k: "background", label: "Background picture", when: visual, path: (l, ext) => `assets/bg/${l.id || "location"}${ext}` },
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

  { id: "quests", title: "Quests", count: () => (card.quests || []).length, intro: "Goals with one or more objectives. The AI decides when an objective is met; the game tracks it and pays the reward.", fields: () => [
    { t: "list", k: "quests", add: "Add a quest", make: () => ({ id: "", title: "", stages: [{ id: "start", description: "" }] }), title: (q) => q.title, sub: (q) => `${(q.stages || []).length} objectives`, fields: [
      { t: "text", k: "title", label: "Title", feeds: "id" },
      { t: "text", k: "id", label: "Id" },
      { t: "area", k: "description", label: "Description", rows: 2 },
      { t: "bool", k: "auto_start", label: "Active from the start of the game", redraw: false },
      { t: "area", k: "fail_when", label: "What makes it fail (optional)", rows: 2, help: "Only the AI sees this. For example: the player leaves the exam grounds before the exam is over." },
      { t: "list", k: "stages", label: "Objectives, in order", keep: true, add: "Add an objective", make: () => ({ id: "", description: "" }), title: (s) => s.description, sub: (s) => s.id, fields: [
        { t: "text", k: "description", label: "What the player sees", feeds: "id" },
        { t: "text", k: "id", label: "Id" },
        { t: "area", k: "hint", label: "When it counts as done", rows: 2, help: "Only the AI sees this." },
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

async function openEditor(project, wanted) {
  if (project !== pid) {
    await flush();
    const reply = await api("GET", `/api/projects/${project}`);
    pid = project; card = reply.card; problems = reply.problems;
    card.meta = card.meta || {}; card.world = card.world || {}; card.rules = card.rules || {}; card.rules.stats = card.rules.stats || []; card.characters = card.characters || [];
    openItems.clear();
    // Repairs a card saved before stat references were kept in step.
    const repaired = JSON.stringify(card);
    dropMissingStats();
    if (JSON.stringify(card) !== repaired) setTimeout(() => changed(false), 0);
    app.replaceChildren(el("div", { class: "editor" },
      el("div", { class: "topbar" }, el("a", { class: "button", href: "#/", text: "All cards" }), el("h1"), el("span", { class: "status", text: "Saved" }),
        el("button", { class: "badge", onclick: () => { location.hash = `#/card/${pid}/export`; } }),
        el("a", { class: "button", href: `#/card/${project}/export`, text: "Export" })),
      el("nav", { class: "sidebar" }), el("main", { class: "main" })));
    showProblems();
  }
  section = SECTIONS.some((s) => s.id === wanted) ? wanted : "info";
  document.querySelector(".main").scrollTop = 0;
  renderSection();
}

async function openHome() {
  await flush();
  pid = null; card = null;
  const [{ projects, folder }] = await Promise.all([api("GET", "/api/projects")]);
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
    el("header", {}, el("h1", { text: "Card Creator" }), el("span", { class: "brand", text: "SI-Station" })),
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
    error, el("p", { class: "muted small" }, "Cards are kept in ", el("code", {}, folder))));
}

async function route() {
  const parts = location.hash.replace(/^#\/?/, "").split("/");
  try {
    if (parts[0] === "card" && parts[1]) await openEditor(parts[1], parts[2]);
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
