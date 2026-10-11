/*
 * BGG Shelf Card – zeigt die Sammlung als virtuelle Billy-Regale.
 *
 * Standort-Format (frei, wird so gelesen):  "<Regalname>, Fach <n>"
 *   "Regal 2, Fach 3"   -> Regal "Regal 2", drittes Fach von oben
 *   "Wohnzimmer Fach 1" -> Regal "Wohnzimmer", oberstes Fach
 *   "Keller"            -> Regal "Keller", Bereich "ohne Fach"
 *   leer                -> Stapel "Ohne Standort"
 *
 * Konfiguration:
 *   type: custom:bgg-shelf-card
 *   title: Spieleregal            # optional
 *   compartments: 6               # Fächer pro Billy (Standard 6)
 *   stack_size: 5                 # Schachteln pro Stapel
 *   shelves: [Regal 1, Regal 2]   # optional: Reihenfolge / leere Regale anzeigen
 *   show_expansions: false        # Erweiterungen mit eigenem Standort mit einsortieren
 */
const DOMAIN = "bgg_collection_sync";
const NO_LOCATION = "\u0000none";

const esc = (s) =>
  String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);

const hash = (s) => {
  let h = 2166136261;
  for (const ch of String(s)) h = Math.imul(h ^ ch.charCodeAt(0), 16777619);
  return h >>> 0;
};

const naturalSort = (a, b) => a.localeCompare(b, "de", { numeric: true, sensitivity: "base" });

export function parseLocation(raw) {
  const loc = String(raw ?? "").trim();
  if (!loc) return { shelf: NO_LOCATION, fach: null };
  const m = loc.match(/(?:fach|boden|ebene)\s*(\d+)/i);
  const fach = m ? parseInt(m[1], 10) : null;
  const shelf = (m ? loc.replace(m[0], "") : loc).replace(/^[\s,;:/|-]+|[\s,;:/|-]+$/g, "").replace(/\s{2,}/g, " ");
  return { shelf: shelf || "Regal", fach: fach && fach > 0 ? fach : null };
}

class BggShelfCard extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._games = [];
    this._query = "";
    this._selected = null;
    this._error = null;
    this._loading = false;
    this._stamp = null;
  }

  static getStubConfig() {
    return { title: "Spieleregal" };
  }

  static getConfigElement() {
    return undefined;
  }

  setConfig(config) {
    this._config = { compartments: 6, stack_size: 5, show_expansions: false, ...config };
    this._render();
  }

  getCardSize() {
    return 12;
  }

  getGridOptions() {
    return { columns: "full", min_columns: 6 };
  }

  set hass(hass) {
    this._hass = hass;
    // Neu laden, sobald sich der Sammlungs-Sensor ändert (Sync, neuer Standort …)
    const sensor = Object.values(hass.states).find(
      (s) => s.entity_id.startsWith("sensor.") && Array.isArray(s.attributes?.games) && "languages" in s.attributes
    );
    const stamp = sensor ? sensor.last_updated : "none";
    if (stamp !== this._stamp) {
      this._stamp = stamp;
      this._load();
    }
  }

  async _load() {
    if (!this._hass || this._loading) return;
    this._loading = true;
    try {
      const res = await this._hass.callWS({
        type: "call_service",
        domain: DOMAIN,
        service: "get_games",
        service_data: { type: this._config.show_expansions ? "all" : "games" },
        return_response: true,
      });
      let games = res.response.games;
      // Erweiterungen nur, wenn sie einen eigenen Standort haben
      games = games.filter((g) => g.type === "game" || g.location);
      this._games = games;
      this._error = null;
    } catch (err) {
      this._error = err?.message || String(err);
    } finally {
      this._loading = false;
      this._render();
    }
  }

  _layout() {
    const shelves = new Map();
    const ensure = (name) => {
      if (!shelves.has(name)) shelves.set(name, { name, fächer: new Map(), loose: [] });
      return shelves.get(name);
    };
    (this._config.shelves || []).forEach((n) => ensure(String(n)));
    const unplaced = [];
    for (const g of this._games) {
      const { shelf, fach } = parseLocation(g.location);
      if (shelf === NO_LOCATION) {
        unplaced.push(g);
        continue;
      }
      const s = ensure(shelf);
      if (fach == null) s.loose.push(g);
      else {
        if (!s.fächer.has(fach)) s.fächer.set(fach, []);
        s.fächer.get(fach).push(g);
      }
    }
    const configured = (this._config.shelves || []).map(String);
    const list = [...shelves.values()].sort((a, b) => {
      const ia = configured.indexOf(a.name);
      const ib = configured.indexOf(b.name);
      if (ia !== -1 || ib !== -1) return (ia === -1 ? 1e9 : ia) - (ib === -1 ? 1e9 : ib);
      return naturalSort(a.name, b.name);
    });
    const byName = (a, b) => naturalSort(a.name, b.name);
    list.forEach((s) => {
      s.fächer.forEach((arr) => arr.sort(byName));
      s.loose.sort(byName);
    });
    unplaced.sort(byName);
    return { shelves: list, unplaced };
  }

  _matches(g) {
    const q = this._query.trim().toLowerCase();
    return !q || g.name.toLowerCase().includes(q);
  }

  _box(g) {
    const h = hash(g.name);
    const hue = h % 360;
    const thick = 22 + (h % 5) * 3;
    const width = 82 + ((h >> 4) % 4) * 5;
    const dim = this._query && !this._matches(g) ? " dim" : this._query ? " hit" : "";
    const thumb = g.thumbnail ? `<img src="${esc(g.thumbnail)}" alt="" loading="lazy">` : "";
    return `<button class="box${dim}" data-id="${g.bgg_id}" title="${esc(g.name)}"
      style="--hue:${hue};height:${thick}px;width:${width}%">${thumb}<span>${esc(g.name)}</span></button>`;
  }

  _stacks(games) {
    const size = Math.max(1, this._config.stack_size);
    const out = [];
    for (let i = 0; i < games.length; i += size) {
      out.push(`<div class="stack">${games.slice(i, i + size).map((g) => this._box(g)).join("")}</div>`);
    }
    return out.join("");
  }

  _billy(s) {
    const maxUsed = Math.max(0, ...s.fächer.keys());
    const count = Math.max(this._config.compartments, maxUsed);
    let rows = "";
    for (let f = 1; f <= count; f++) {
      const games = s.fächer.get(f) || [];
      rows += `<div class="fach"><span class="num">${f}</span>${this._stacks(games)}</div>`;
    }
    const total = [...s.fächer.values()].reduce((n, a) => n + a.length, 0) + s.loose.length;
    const loose = s.loose.length
      ? `<div class="loose"><div class="label">ohne Fach</div><div class="fach">${this._stacks(s.loose)}</div></div>`
      : "";
    return `<section class="billy-wrap">
      <header>${esc(s.name)} <small>${total}</small></header>
      <div class="billy">${rows}</div>
      <div class="plinth"></div>
      ${loose}
    </section>`;
  }

  _dialog() {
    const g = this._games.find((x) => x.bgg_id === this._selected);
    if (!g) return "";
    const { shelf, fach } = parseLocation(g.location);
    const names = this._layout().shelves.map((s) => s.name);
    const players =
      g.min_players && g.max_players
        ? g.min_players === g.max_players ? `${g.min_players}` : `${g.min_players}–${g.max_players}`
        : null;
    const facts = [
      g.year && `${g.year}`,
      players && `${players} Spieler`,
      g.playing_time && `${g.playing_time} min`,
      g.language && esc(g.language),
      `${g.num_plays || 0}× gespielt`,
      g.user_rating && `★ ${g.user_rating}`,
    ].filter(Boolean);
    const exp = (g.expansions || []).map((e) => `<li>${esc(e.name)}</li>`).join("");
    return `<div class="overlay" data-close>
      <div class="dialog" role="dialog" aria-label="${esc(g.name)}">
        <button class="x" data-close aria-label="Schließen">×</button>
        <div class="head">
          ${g.thumbnail ? `<img src="${esc(g.thumbnail)}" alt="">` : ""}
          <div><h2>${esc(g.name)}</h2><p>${facts.join(" · ")}</p></div>
        </div>
        ${exp ? `<h3>Erweiterungen</h3><ul>${exp}</ul>` : ""}
        <h3>Standort</h3>
        <div class="loc">
          <input list="bgg-shelves" id="shelf" placeholder="Regal" value="${shelf === NO_LOCATION ? "" : esc(shelf)}">
          <datalist id="bgg-shelves">${names.map((n) => `<option value="${esc(n)}">`).join("")}</datalist>
          <label>Fach <input id="fach" type="number" min="1" max="20" value="${fach ?? ""}"></label>
        </div>
        <div class="actions">
          <button id="save">Standort speichern</button>
          <button id="play" class="primary">Partie starten</button>
        </div>
        <p class="msg" id="msg"></p>
      </div>
    </div>`;
  }

  async _save() {
    const root = this.shadowRoot;
    const shelf = root.getElementById("shelf").value.trim();
    const fach = root.getElementById("fach").value.trim();
    const location = shelf ? (fach ? `${shelf}, Fach ${fach}` : shelf) : fach ? `Regal, Fach ${fach}` : "";
    await this._call("set_game_meta", { bgg_id: this._selected, location }, "Gespeichert");
  }

  async _call(service, data, okMsg) {
    const msg = this.shadowRoot.getElementById("msg");
    try {
      await this._hass.callService(DOMAIN, service, data);
      if (msg) msg.textContent = okMsg;
    } catch (err) {
      if (msg) msg.textContent = err?.message || String(err);
    }
  }

  _render() {
    if (!this._config) return;
    const { shelves, unplaced } = this._games.length ? this._layout() : { shelves: [], unplaced: [] };
    const hits = this._query ? this._games.filter((g) => this._matches(g)) : [];
    const hitInfo = this._query
      ? hits.length
        ? hits.slice(0, 3).map((g) => `${esc(g.name)}: <b>${esc(g.location || "ohne Standort")}</b>`).join("<br>") +
          (hits.length > 3 ? `<br>… und ${hits.length - 3} weitere` : "")
        : "Nichts gefunden"
      : "";
    const body = this._error
      ? `<p class="error">Sammlung konnte nicht geladen werden: ${esc(this._error)}</p>`
      : !this._games.length
        ? `<p class="empty">${this._loading ? "Lade Sammlung …" : "Keine Spiele gefunden."}</p>`
        : `<div class="room">${shelves.map((s) => this._billy(s)).join("")}</div>
           ${unplaced.length ? `<section class="pile"><header>Ohne Standort <small>${unplaced.length}</small></header>
             <div class="fach floor">${this._stacks(unplaced)}</div></section>` : ""}`;

    const focused = this.shadowRoot.activeElement?.id === "search";
    this.shadowRoot.innerHTML = `<style>${STYLE}</style>
      <ha-card>
        <div class="top">
          ${this._config.title ? `<h1>${esc(this._config.title)}</h1>` : ""}
          <input id="search" type="search" placeholder="Spiel suchen …" value="${esc(this._query)}">
        </div>
        ${hitInfo ? `<div class="hits">${hitInfo}</div>` : ""}
        ${body}
        ${this._selected != null ? this._dialog() : ""}
      </ha-card>`;

    const root = this.shadowRoot;
    const search = root.getElementById("search");
    search.addEventListener("input", (e) => {
      this._query = e.target.value;
      this._render();
    });
    if (focused) {
      search.focus();
      search.setSelectionRange(search.value.length, search.value.length);
    }
    root.querySelectorAll(".box").forEach((b) =>
      b.addEventListener("click", () => {
        this._selected = Number(b.dataset.id);
        this._render();
      })
    );
    root.querySelectorAll("[data-close]").forEach((el) =>
      el.addEventListener("click", (e) => {
        if (e.target !== el) return;
        this._selected = null;
        this._render();
      })
    );
    root.getElementById("save")?.addEventListener("click", () => this._save());
    root.getElementById("play")?.addEventListener("click", () =>
      this._call("start_play", { bgg_id: this._selected }, "Partie gestartet")
    );
  }
}

const STYLE = `
:host { --wood: #f3eee4; --wood-edge: #d9cfbd; --wood-dark: #c4b79f; --back: #e8e0d0; }
ha-card { padding: 16px; overflow: hidden; }
.top { display: flex; gap: 12px; align-items: center; flex-wrap: wrap; margin-bottom: 12px; }
h1 { font-size: 1.3em; font-weight: 500; margin: 0; flex: 1 1 auto; }
#search { flex: 0 1 260px; min-width: 0; padding: 8px 12px; border-radius: 8px; font: inherit;
  border: 1px solid var(--divider-color, #ccc); background: var(--card-background-color, #fff);
  color: var(--primary-text-color); }
.hits { font-size: .9em; margin: -4px 0 12px; color: var(--secondary-text-color); }
.hits b { color: var(--primary-text-color); }
.room { display: flex; gap: 20px; overflow-x: auto; padding-bottom: 8px; align-items: flex-start; }
.billy-wrap { flex: 0 0 260px; }
header { font-weight: 500; margin-bottom: 6px; color: var(--primary-text-color); }
header small { color: var(--secondary-text-color); font-weight: 400; margin-left: 4px; }
.billy { background: var(--wood); border: 10px solid var(--wood-edge); border-top-width: 12px;
  border-bottom: none; box-shadow: inset 0 0 0 1px var(--wood-dark); }
.fach { position: relative; min-height: 120px; background: linear-gradient(var(--back), var(--wood));
  border-bottom: 8px solid var(--wood-edge); box-shadow: 0 1px 0 var(--wood-dark);
  display: flex; flex-wrap: wrap; align-items: flex-end; align-content: flex-end; gap: 4px; padding: 6px 6px 0; }
.num { position: absolute; top: 3px; left: 5px; font-size: 10px; color: var(--wood-dark); }
.plinth { height: 14px; background: var(--wood-edge); margin: 0 2px; border-radius: 0 0 2px 2px;
  box-shadow: inset 0 3px 3px rgba(0,0,0,.08); }
.stack { flex: 1 1 calc(50% - 4px); min-width: 0; display: flex; flex-direction: column-reverse; align-items: center; }
.box { all: unset; box-sizing: border-box; cursor: pointer; display: flex; align-items: center; gap: 4px;
  padding: 0 6px; border-radius: 2px; overflow: hidden;
  background: hsl(var(--hue) 45% 46%); color: #fff; font-size: 11px; line-height: 1.1;
  box-shadow: inset 0 -2px 0 rgba(0,0,0,.25), inset 0 1px 0 rgba(255,255,255,.2);
  transition: transform .15s, opacity .15s, filter .15s; }
.box img { height: 80%; aspect-ratio: 1; object-fit: cover; border-radius: 1px; flex: none; }
.box span { white-space: nowrap; overflow: hidden; text-overflow: ellipsis; text-shadow: 0 1px 1px rgba(0,0,0,.4); }
.box:hover, .box:focus-visible { transform: translateX(4px); filter: brightness(1.12); outline: none; }
.box.dim { opacity: .18; }
.box.hit { outline: 2px solid var(--accent-color, #ff9800); outline-offset: 1px; z-index: 1; }
.loose { margin-top: 10px; }
.label { font-size: .8em; color: var(--secondary-text-color); margin-bottom: 2px; }
.loose .fach, .floor { border: 1px dashed var(--divider-color, #ccc); background: none; min-height: 60px; }
.pile { margin-top: 16px; }
.floor .stack { flex: 0 1 200px; }
.empty, .error { color: var(--secondary-text-color); }
.error { color: var(--error-color, #c00); }
.overlay { position: fixed; inset: 0; background: rgba(0,0,0,.45); display: flex; align-items: center;
  justify-content: center; z-index: 10; padding: 16px; }
.dialog { position: relative; width: min(440px, 100%); max-height: 90vh; overflow: auto; border-radius: 12px; padding: 20px;
  background: var(--card-background-color, #fff); color: var(--primary-text-color); box-shadow: 0 8px 32px rgba(0,0,0,.3); }
.x { all: unset; position: absolute; top: 8px; right: 12px; font-size: 24px; cursor: pointer; color: var(--secondary-text-color); }
.head { display: flex; gap: 14px; align-items: flex-start; }
.head img { width: 84px; height: 84px; object-fit: contain; border-radius: 6px; flex: none; }
h2 { font-size: 1.2em; margin: 0 24px 4px 0; }
.head p { margin: 0; color: var(--secondary-text-color); font-size: .9em; }
h3 { font-size: .85em; text-transform: uppercase; letter-spacing: .04em; color: var(--secondary-text-color); margin: 16px 0 6px; }
ul { margin: 0; padding-left: 18px; font-size: .9em; }
.loc { display: flex; gap: 8px; flex-wrap: wrap; align-items: center; }
.loc input { padding: 8px; border-radius: 6px; font: inherit; border: 1px solid var(--divider-color, #ccc);
  background: var(--card-background-color, #fff); color: var(--primary-text-color); }
#shelf { flex: 1 1 160px; min-width: 0; }
#fach { width: 64px; margin-left: 4px; }
.actions { display: flex; gap: 8px; margin-top: 16px; flex-wrap: wrap; }
.actions button { all: unset; cursor: pointer; padding: 8px 14px; border-radius: 8px;
  border: 1px solid var(--primary-color, #03a9f4); color: var(--primary-color, #03a9f4); }
.actions .primary { background: var(--primary-color, #03a9f4); color: var(--text-primary-color, #fff); }
.msg { min-height: 1em; font-size: .9em; color: var(--secondary-text-color); margin: 10px 0 0; }
`;

if (!customElements.get("bgg-shelf-card")) {
  customElements.define("bgg-shelf-card", BggShelfCard);
  window.customCards = window.customCards || [];
  window.customCards.push({
    type: "bgg-shelf-card",
    name: "BGG Spieleregal",
    description: "Zeigt die BGG-Sammlung als Billy-Regale anhand der Standorte.",
  });
}
