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
 *
 * Status (Im Besitz / Zu verkaufen / Früher besessen) kommt aus dem BGG-Sync.
 * Ändern: Link "Auf BGG ändern" öffnet das Spiel auf BGG; der nächste Sync gleicht ab.
 *
 * BGG Play Card – laufende Partie mit Mitspielern, Punkten, Gewinner, Beenden / Verwerfen;
 * nach dem Beenden QR-Code / Link zum Import in BG Stats:
 *   type: custom:bgg-play-card
 *   entity: sensor.xyz_spielsitzung   # optional, wird sonst automatisch gefunden
 *   bgstats: true                     # QR-Code für BG Stats anbieten (Standard: an)
 */
const DOMAIN = "bgg_collection_sync";
const NO_LOCATION = "\u0000none";
const STATUS_LABELS = { owned: "Im Besitz", for_sale: "Zu verkaufen", prev_owned: "Früher besessen" };
const statusOf = (g) => (g.prev_owned && !g.owned ? "prev_owned" : g.for_trade ? "for_sale" : "owned");

const esc = (s) =>
  String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);

const hash = (s) => {
  let h = 2166136261;
  for (const ch of String(s)) h = Math.imul(h ^ ch.charCodeAt(0), 16777619);
  return h >>> 0;
};

const naturalSort = (a, b) => a.localeCompare(b, "de", { numeric: true, sensitivity: "base" });

function parseLocation(raw) {
  const loc = String(raw ?? "").trim();
  if (!loc) return { shelf: NO_LOCATION, fach: null };
  const m = loc.match(/(?:fach|boden|ebene)\s*(\d+)/i);
  const fach = m ? parseInt(m[1], 10) : null;
  const shelf = (m ? loc.replace(m[0], "") : loc).replace(/^[\s,;:/|-]+|[\s,;:/|-]+$/g, "").replace(/\s{2,}/g, " ");
  return { shelf: shelf || "Regal", fach: fach && fach > 0 ? fach : null };
}

// ---- Mitspieler (gemeinsam für Regal- und Partie-Karte) -------------------
const isBgg = (hass, id, key) => {
  const e = hass.entities?.[id];
  return !!e && e.platform === DOMAIN && e.translation_key === key;
};

function bggPlayers(hass) {
  return Object.values(hass.states)
    .filter((s) => isBgg(hass, s.entity_id, "player") || ("win_rate" in (s.attributes || {}) && "top_games" in s.attributes))
    .filter((s) => s.attributes.name && s.state !== "unavailable")
    .map((s) => ({ name: s.attributes.name, plays: Number(s.state) || 0, bgg: s.attributes.bgg_username }))
    .sort((a, b) => b.plays - a.plays || naturalSort(a.name, b.name));
}

function bggSession(hass, entityId) {
  if (entityId) return hass.states[entityId];
  return Object.values(hass.states).find(
    (s) => isBgg(hass, s.entity_id, "session") || (s.entity_id.startsWith("sensor.") && "bgg_user" in (s.attributes || {}))
  );
}

const playerChip = (name, on, bgg) =>
  `<button class="pchip${on ? " on" : ""}" data-player="${esc(name)}" aria-pressed="${on}"
     title="${bgg ? `BGG: ${esc(bgg)}` : "ohne BGG-Account"}">${esc(name)}${bgg ? `<i>BGG</i>` : ""}</button>`;

function pickerHtml(players, selected) {
  const known = new Set(players.map((p) => p.name.toLowerCase()));
  const extra = [...selected].filter((n) => !known.has(n.toLowerCase()));
  return `<div class="picker">
    ${players.map((p) => playerChip(p.name, selected.has(p.name), p.bgg)).join("")}
    ${extra.map((n) => playerChip(n, true, null)).join("")}
    <input class="padd" placeholder="+ Name" aria-label="Mitspieler hinzufügen" enterkeyhint="done">
  </div>`;
}

/** Chips direkt im DOM umschalten (kein Neuzeichnen -> Eingaben bleiben erhalten). */
function bindPicker(root, selected, onChange) {
  const bind = (b) =>
    b.addEventListener("click", () => {
      const n = b.dataset.player;
      selected.has(n) ? selected.delete(n) : selected.add(n);
      b.classList.toggle("on", selected.has(n));
      b.setAttribute("aria-pressed", selected.has(n));
      onChange?.();
    });
  root.querySelectorAll("[data-player]").forEach(bind);
  const input = root.querySelector(".padd");
  input?.addEventListener("keydown", (e) => {
    if (e.key !== "Enter") return;
    const n = input.value.trim();
    input.value = "";
    if (!n || [...selected].some((x) => x.toLowerCase() === n.toLowerCase())) return;
    const existing = [...root.querySelectorAll("[data-player]")].find(
      (b) => b.dataset.player.toLowerCase() === n.toLowerCase()
    );
    if (existing) return existing.click();
    selected.add(n);
    input.insertAdjacentHTML("beforebegin", playerChip(n, true, null));
    bind(input.previousElementSibling);
    onChange?.();
  });
}

const PICKER_STYLE = `
.picker { display: flex; flex-wrap: wrap; gap: 6px; align-items: center; }
.pchip { all: unset; box-sizing: border-box; cursor: pointer; padding: 5px 12px; border-radius: 16px; font-size: .9em;
  border: 1px solid var(--divider-color, #ccc); color: var(--primary-text-color); display: inline-flex; gap: 6px; align-items: center; }
.pchip i { font-style: normal; font-size: .7em; opacity: .7; }
.pchip.on { background: var(--primary-color, #03a9f4); border-color: var(--primary-color, #03a9f4); color: var(--text-primary-color, #fff); }
.pchip:focus-visible { outline: 2px solid var(--primary-color, #03a9f4); outline-offset: 2px; }
.padd { flex: 0 1 110px; min-width: 80px; padding: 5px 10px; border-radius: 16px; font: inherit; font-size: .9em;
  border: 1px dashed var(--divider-color, #ccc); background: none; color: var(--primary-text-color); }
`;

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
    this._picked = new Set();
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
        service_data: { type: this._config.show_expansions ? "all" : "games", include_prev_owned: true },
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
      if (!shelves.has(name)) shelves.set(name, { name, faecher: new Map(), loose: [] });
      return shelves.get(name);
    };
    (this._config.shelves || []).forEach((n) => ensure(String(n)));
    const unplaced = [];
    const forSale = [];
    const prevOwned = [];
    for (const g of this._games) {
      const status = statusOf(g);
      if (status === "prev_owned") {
        prevOwned.push(g); // nicht mehr im Regal
        continue;
      }
      if (status === "for_sale") forSale.push(g); // bleibt im Regal, zusätzlich im Verkaufsbereich
      const { shelf, fach } = parseLocation(g.location);
      if (shelf === NO_LOCATION) {
        unplaced.push(g);
        continue;
      }
      const s = ensure(shelf);
      if (fach == null) s.loose.push(g);
      else {
        if (!s.faecher.has(fach)) s.faecher.set(fach, []);
        s.faecher.get(fach).push(g);
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
      s.faecher.forEach((arr) => arr.sort(byName));
      s.loose.sort(byName);
    });
    unplaced.sort(byName);
    forSale.sort(byName);
    prevOwned.sort(byName);
    return { shelves: list, unplaced, forSale, prevOwned };
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
    const status = statusOf(g);
    const cls = status === "for_sale" ? " sale" : status === "prev_owned" ? " gone" : "";
    const tag = status === "for_sale" ? `<i class="tag" aria-label="zu verkaufen">€</i>` : "";
    return `<button class="box${dim}${cls}" data-id="${g.bgg_id}" title="${esc(g.name)}"
      style="--hue:${hue};height:${thick}px;width:${width}%">${thumb}<span>${esc(g.name)}</span>${tag}</button>`;
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
    const maxUsed = Math.max(0, ...s.faecher.keys());
    const count = Math.max(this._config.compartments, maxUsed);
    let rows = "";
    for (let f = 1; f <= count; f++) {
      const games = s.faecher.get(f) || [];
      rows += `<div class="fach"><span class="num">${f}</span>${this._stacks(games)}</div>`;
    }
    const total = [...s.faecher.values()].reduce((n, a) => n + a.length, 0) + s.loose.length;
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
    const current = statusOf(g);
    const bggUrl = `https://boardgamegeek.com/boardgame/${encodeURIComponent(g.bgg_id)}`;
    return `<div class="overlay" data-close>
      <div class="dialog" role="dialog" aria-label="${esc(g.name)}">
        <button class="x" data-close aria-label="Schließen">×</button>
        <div class="head">
          ${g.thumbnail ? `<img src="${esc(g.thumbnail)}" alt="">` : ""}
          <div><h2>${esc(g.name)}</h2><p>${facts.join(" · ")}</p></div>
        </div>
        ${exp ? `<h3>Erweiterungen</h3><ul>${exp}</ul>` : ""}
        <h3>Status bei BGG</h3>
        <div class="status">
          <span class="chip ${current}">${STATUS_LABELS[current]}</span>
          <a class="bgg-link" href="${bggUrl}" target="_blank" rel="noopener noreferrer">Auf BGG ändern ↗</a>
        </div>
        <p class="hint">Änderungen auf BGG erscheinen hier nach dem nächsten Sync.</p>
        <h3>Standort</h3>
        <div class="loc">
          <input list="bgg-shelves" id="shelf" placeholder="Regal" value="${shelf === NO_LOCATION ? "" : esc(shelf)}">
          <datalist id="bgg-shelves">${names.map((n) => `<option value="${esc(n)}">`).join("")}</datalist>
          <label>Fach <input id="fach" type="number" min="1" max="20" value="${fach ?? ""}"></label>
        </div>
        ${current === "prev_owned" ? "" : `<h3>Mitspieler</h3>${pickerHtml(bggPlayers(this._hass), this._picked)}`}
        <h3>Zuordnungen</h3>
        <div class="meta">
          <label>NFC-Tag
            <span class="with-btn">
              <input id="nfc" value="${esc(g.nfc_tag)}" placeholder="Tag-ID">
              <button id="scan" title="Zuletzt gescannten Tag (letzte 10 Min.) übernehmen">Gescannten übernehmen</button>
            </span>
          </label>
          <label>Spotify-Playlist
            <input id="spotify" value="${esc(g.spotify_uri)}" placeholder="Link oder spotify:playlist:…">
          </label>
          <label>Lichtprofil
            <select id="light">${this._lightOptions(g.light_scene)}</select>
          </label>
        </div>
        <div class="actions">
          <button id="save">Speichern</button>
          ${current === "prev_owned" ? "" : `<button id="play" class="primary">Partie starten</button>`}
        </div>
        <p class="msg" id="msg">${esc(this._msg || "")}</p>
      </div>
    </div>`;
  }

  _lightOptions(current) {
    const list = Object.values(this._hass?.states || {})
      .filter((st) => st.entity_id.startsWith("scene.") || st.entity_id.startsWith("script."))
      .map((st) => [st.entity_id, st.attributes.friendly_name || st.entity_id])
      .sort((a, b) => naturalSort(a[1], b[1]));
    if (current && !list.some(([id]) => id === current)) list.unshift([current, `${current} (nicht gefunden)`]);
    const opt = (id, label) => `<option value="${esc(id)}"${id === (current || "") ? " selected" : ""}>${esc(label)}</option>`;
    return [opt("", "— keins —"), ...list.map(([id, label]) => opt(id, label))].join("");
  }

  async _save() {
    const root = this.shadowRoot;
    const shelf = root.getElementById("shelf").value.trim();
    const fach = root.getElementById("fach").value.trim();
    const location = shelf ? (fach ? `${shelf}, Fach ${fach}` : shelf) : fach ? `Regal, Fach ${fach}` : "";
    await this._call(
      "set_game_meta",
      {
        bgg_id: this._selected,
        location,
        nfc_tag: root.getElementById("nfc").value.trim(),
        spotify_uri: root.getElementById("spotify").value.trim(),
        light_scene: root.getElementById("light").value,
      },
      "Gespeichert"
    );
  }

  async _scan() {
    try {
      const res = await this._hass.callWS({
        type: "call_service",
        domain: DOMAIN,
        service: "assign_scanned_tag",
        service_data: { bgg_id: this._selected },
        return_response: true,
      });
      const input = this.shadowRoot.getElementById("nfc");
      if (input) input.value = res.response?.nfc_tag || "";
      this._msg = "Tag zugeordnet";
    } catch (err) {
      this._msg = err?.message || String(err);
    }
    const msg = this.shadowRoot.getElementById("msg");
    if (msg) msg.textContent = this._msg;
  }

  async _call(service, data, okMsg) {
    try {
      await this._hass.callService(DOMAIN, service, data);
      this._msg = okMsg;
    } catch (err) {
      this._msg = err?.message || String(err);
    }
    const msg = this.shadowRoot.getElementById("msg");
    if (msg) msg.textContent = this._msg;
  }

  _render() {
    if (!this._config) return;
    const { shelves, unplaced, forSale, prevOwned } = this._games.length
      ? this._layout()
      : { shelves: [], unplaced: [], forSale: [], prevOwned: [] };
    const area = (title, list, cls) =>
      list.length
        ? `<section class="pile ${cls}"><header>${title} <small>${list.length}</small></header>
             <div class="fach floor">${this._stacks(list)}</div></section>`
        : "";
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
           ${area("Ohne Standort", unplaced, "")}
           ${area("Zu verkaufen", forSale, "sale-area")}
           ${area("Früher besessen", prevOwned, "gone-area")}`;

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
        this._msg = "";
        this._picked = new Set();
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
    root.getElementById("scan")?.addEventListener("click", () => this._scan());
    if (this._selected != null) bindPicker(root.querySelector(".dialog"), this._picked);
    root.getElementById("play")?.addEventListener("click", () => {
      const data = { bgg_id: this._selected };
      if (this._picked.size) data.player_names = [...this._picked];
      this._call("start_play", data, "Partie gestartet");
    });
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
.meta { display: flex; flex-direction: column; gap: 10px; }
.meta label { display: flex; flex-direction: column; gap: 4px; font-size: .85em; color: var(--secondary-text-color); }
.meta input, .meta select { padding: 8px; border-radius: 6px; font: inherit; font-size: 1rem; min-width: 0;
  border: 1px solid var(--divider-color, #ccc); background: var(--card-background-color, #fff); color: var(--primary-text-color); }
.with-btn { display: flex; gap: 8px; }
.with-btn input { flex: 1 1 auto; }
.with-btn button { all: unset; cursor: pointer; padding: 6px 10px; border-radius: 6px; font-size: .9em; white-space: nowrap;
  border: 1px solid var(--primary-color, #03a9f4); color: var(--primary-color, #03a9f4); }
.actions { display: flex; gap: 8px; margin-top: 16px; flex-wrap: wrap; }
.actions button { all: unset; cursor: pointer; padding: 8px 14px; border-radius: 8px;
  border: 1px solid var(--primary-color, #03a9f4); color: var(--primary-color, #03a9f4); }
.actions .primary { background: var(--primary-color, #03a9f4); color: var(--text-primary-color, #fff); }
.box { position: relative; }
.box .tag { font-style: normal; font-size: 10px; font-weight: 700; flex: none; margin-left: auto;
  background: #ffd54f; color: #4a3b00; border-radius: 3px; padding: 1px 4px; text-shadow: none; }
.box.gone { filter: grayscale(.85); opacity: .75; }
.sale-area header::before { content: "€ "; color: #b8860b; }
.gone-area .floor { opacity: .9; }
.status { display: flex; flex-wrap: wrap; gap: 10px; align-items: center; }
.chip { padding: 4px 10px; border-radius: 12px; font-size: .85em;
  background: var(--secondary-background-color, #eee); color: var(--primary-text-color); }
.chip.for_sale { background: #ffd54f; color: #4a3b00; }
.chip.prev_owned { opacity: .75; }
.bgg-link { color: var(--primary-color, #03a9f4); text-decoration: none; font-weight: 500; }
.bgg-link:hover, .bgg-link:focus-visible { text-decoration: underline; }
.hint { font-size: .8em; color: var(--secondary-text-color); margin: 6px 0 0; }
.msg { min-height: 1em; font-size: .9em; color: var(--secondary-text-color); margin: 10px 0 0; }
${PICKER_STYLE}`;

// ---- Partie-Karte -----------------------------------------------------------
class BggPlayCard extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._stamp = null;
    this._playKey = null;
    this._picked = new Set();
    this._winner = "";
    this._note = "";
    this._scores = {};
    this._msg = "";
    this._busy = false;
    this._result = null;
  }

  static getStubConfig() {
    return {};
  }

  setConfig(config) {
    this._config = { bgstats: true, ...config };
    this._stamp = null;
    if (this._hass) this.hass = this._hass;
  }

  getCardSize() {
    return 4;
  }

  getGridOptions() {
    return { columns: "full", min_columns: 6 };
  }

  connectedCallback() {
    this._timer = setInterval(() => this._tick(), 30000);
  }

  disconnectedCallback() {
    clearInterval(this._timer);
  }

  set hass(hass) {
    this._hass = hass;
    if (!this._config) return;
    const s = bggSession(hass, this._config.entity);
    const players = bggPlayers(hass);
    const stamp = `${s?.last_updated}|${players.map((p) => p.name + p.plays).join(",")}`;
    if (stamp === this._stamp) return;
    this._stamp = stamp;
    // Neue Partie -> Auswahl aus dem Start übernehmen
    const key = s?.state === "running" ? s.attributes.started_at : null;
    if (key !== this._playKey) {
      this._playKey = key;
      this._picked = new Set(s?.attributes?.player_names || []);
      this._winner = "";
      this._note = "";
      this._scores = {};
      if (key) this._msg = "";
    }
    this._session = s;
    this._players = players;
    this._render();
  }

  _minutes() {
    const start = Date.parse(this._session?.attributes?.started_at);
    return Number.isNaN(start) ? null : Math.max(0, Math.round((Date.now() - start) / 60000));
  }

  _tick() {
    const el = this.shadowRoot.getElementById("mins");
    const m = this._minutes();
    if (el && m != null) el.textContent = `${m} Min.`;
  }

  _winnerOptions() {
    const me = this._session?.attributes?.bgg_user;
    const names = [...this._picked];
    if (this._winner && this._winner !== me && !names.includes(this._winner)) this._winner = "";
    const opt = (v, label) => `<option value="${esc(v)}"${v === this._winner ? " selected" : ""}>${esc(label)}</option>`;
    return [opt("", "Automatisch (meiste Punkte)"), me ? opt(me, `Ich (${me})`) : "", ...names.map((n) => opt(n, n))].join("");
  }

  _scoreRows() {
    const me = this._session?.attributes?.bgg_user;
    const rows = [...(me ? [[me, `Ich (${me})`]] : []), ...[...this._picked].map((n) => [n, n])];
    return rows
      .map(
        ([key, label]) => `<label class="score"><span>${esc(label)}</span>
          <input type="number" inputmode="decimal" step="any" data-score="${esc(key)}" value="${esc(this._scores[key] ?? "")}" placeholder="–"></label>`
      )
      .join("");
  }

  _bindScores(root) {
    root.querySelectorAll("[data-score]").forEach((el) =>
      el.addEventListener("input", (e) => (this._scores[el.dataset.score] = e.target.value))
    );
  }

  _resultHtml() {
    const r = this._result;
    if (!r) return "";
    return `<div class="result">
      <h3>In BG Stats übernehmen</h3>
      <p class="hint">QR-Code mit dem Handy scannen oder Link öffnen – BG Stats zeigt dann den Import-Dialog.</p>
      <div class="qr">${r.qr_svg}</div>
      <div class="actions">
        <a class="btn primary" href="${esc(r.url)}" target="_blank" rel="noopener noreferrer">In BG Stats öffnen</a>
        <button id="done">Fertig</button>
      </div>
    </div>`;
  }

  _render() {
    const s = this._session;
    let body;
    if (!s) {
      body = `<p class="empty">Sitzungs-Sensor der BGG-Integration nicht gefunden.</p>`;
    } else if (s.state !== "running") {
      body = this._result
        ? ""
        : `<p class="empty">Keine laufende Partie. Starte eine im Regal.</p>
           ${this._config.bgstats ? `<div class="actions"><button id="last">Letzte Partie an BG Stats</button></div>` : ""}
           <p class="msg" id="msg">${esc(this._msg)}</p>`;
    } else {
      const a = s.attributes;
      const start = new Date(a.started_at);
      const time = Number.isNaN(start.getTime())
        ? ""
        : start.toLocaleTimeString("de-DE", { hour: "2-digit", minute: "2-digit" });
      body = `
        <div class="head">
          <div>
            <h2>🎲 ${esc(a.name)}</h2>
            <p>seit ${time} Uhr · <span id="mins">${this._minutes() ?? "?"} Min.</span>${a.location ? ` · ${esc(a.location)}` : ""}</p>
          </div>
        </div>
        <h3>Mitspieler</h3>
        ${pickerHtml(this._players, this._picked)}
        <h3>Punkte</h3>
        <div class="scores" id="scores">${this._scoreRows()}</div>
        <div class="row">
          <label>Gewinner <select id="winner">${this._winnerOptions()}</select></label>
          <label class="grow">Notiz <input id="note" value="${esc(this._note)}" placeholder="optional"></label>
        </div>
        <div class="actions">
          <button id="end" class="primary"${this._busy ? " disabled" : ""}>Beenden</button>
          <button id="cancel"${this._busy ? " disabled" : ""}>Verwerfen</button>
        </div>
        <p class="msg" id="msg">${esc(this._msg)}</p>`;
    }
    this.shadowRoot.innerHTML = `<style>${PLAY_STYLE}</style><ha-card>${this._resultHtml()}${body}</ha-card>`;
    const root = this.shadowRoot;
    root.getElementById("done")?.addEventListener("click", () => {
      this._result = null;
      this._render();
    });
    root.getElementById("last")?.addEventListener("click", () => this._bgstats(null));
    if (!s || s.state !== "running") return;
    const winner = root.getElementById("winner");
    const scores = root.getElementById("scores");
    this._bindScores(root);
    bindPicker(root.querySelector(".picker"), this._picked, () => {
      winner.innerHTML = this._winnerOptions();
      scores.innerHTML = this._scoreRows();
      this._bindScores(scores);
    });
    winner.addEventListener("change", (e) => (this._winner = e.target.value));
    root.getElementById("note").addEventListener("input", (e) => (this._note = e.target.value));
    root.getElementById("end").addEventListener("click", () => this._end());
    root.getElementById("cancel").addEventListener("click", () => {
      if (confirm("Partie wirklich verwerfen? Sie wird nicht gespeichert.")) this._run("cancel_play", {});
    });
  }

  _end() {
    const data = { player_names: [...this._picked] };
    if (this._winner) data.winner = this._winner;
    if (this._note.trim()) data.note = this._note.trim();
    const me = this._session?.attributes?.bgg_user;
    const keys = new Set([...(me ? [me] : []), ...this._picked]);
    const scores = {};
    for (const [k, v] of Object.entries(this._scores)) {
      const n = parseFloat(String(v).replace(",", "."));
      if (keys.has(k) && Number.isFinite(n)) scores[k] = n;
    }
    if (Object.keys(scores).length) data.scores = scores;
    this._run("end_play", data);
  }

  async _bgstats(playId) {
    try {
      const res = await this._hass.callWS({
        type: "call_service",
        domain: DOMAIN,
        service: "bgstats_link",
        service_data: playId ? { play_id: playId } : {},
        return_response: true,
      });
      this._result = res.response;
    } catch (err) {
      this._msg = err?.message || String(err);
    }
    this._render();
  }

  async _run(service, data) {
    this._busy = true;
    this._msg = "";
    try {
      const res = await this._hass.callWS({
        type: "call_service",
        domain: DOMAIN,
        service,
        service_data: data,
        return_response: service === "end_play",
      });
      const r = res?.response;
      this._msg = r?.bgg_error
        ? `Gespeichert, aber nicht auf BGG geloggt: ${r.bgg_error}`
        : service === "end_play"
          ? `Gespeichert${r?.winner ? ` – gewonnen: ${r.winner}` : ""}${r?.bgg_play_id != null ? " · auf BGG geloggt" : ""}.`
          : "Verworfen.";
      if (service === "end_play" && r?.play_id && this._config.bgstats) await this._bgstats(r.play_id);
    } catch (err) {
      this._msg = err?.message || String(err);
    } finally {
      this._busy = false;
      this._render();
    }
  }
}

const PLAY_STYLE = `
ha-card { padding: 16px; }
h2 { font-size: 1.25em; font-weight: 500; margin: 0 0 2px; }
.head p { margin: 0; color: var(--secondary-text-color); font-size: .9em; }
h3 { font-size: .85em; text-transform: uppercase; letter-spacing: .04em; color: var(--secondary-text-color); margin: 14px 0 6px; }
.row { display: flex; gap: 12px; flex-wrap: wrap; margin-top: 12px; align-items: flex-end; }
.row label { display: flex; flex-direction: column; gap: 4px; font-size: .85em; color: var(--secondary-text-color); min-width: 0; }
.row .grow { flex: 1 1 180px; }
select, #note { padding: 8px; border-radius: 6px; font: inherit; font-size: 1rem; border: 1px solid var(--divider-color, #ccc);
  background: var(--card-background-color, #fff); color: var(--primary-text-color); min-width: 0; }
.actions { display: flex; gap: 8px; margin-top: 16px; flex-wrap: wrap; }
.actions button { all: unset; cursor: pointer; padding: 8px 16px; border-radius: 8px;
  border: 1px solid var(--primary-color, #03a9f4); color: var(--primary-color, #03a9f4); }
.actions .primary { background: var(--primary-color, #03a9f4); color: var(--text-primary-color, #fff); }
.actions button[disabled] { opacity: .5; cursor: default; }
.actions button:focus-visible { outline: 2px solid var(--primary-color, #03a9f4); outline-offset: 2px; }
.empty { color: var(--secondary-text-color); margin: 0; }
.msg { min-height: 1em; font-size: .9em; color: var(--secondary-text-color); margin: 10px 0 0; }
.scores { display: grid; grid-template-columns: repeat(auto-fill, minmax(130px, 1fr)); gap: 8px; }
.score { display: flex; flex-direction: column; gap: 4px; font-size: .85em; color: var(--secondary-text-color); min-width: 0; }
.score span { white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.score input { padding: 8px; border-radius: 6px; font: inherit; font-size: 1rem; border: 1px solid var(--divider-color, #ccc);
  background: var(--card-background-color, #fff); color: var(--primary-text-color); min-width: 0; width: 100%; box-sizing: border-box; }
.result { margin-bottom: 12px; }
.result h3 { margin-top: 0; }
.hint { font-size: .85em; color: var(--secondary-text-color); margin: 0 0 10px; }
.qr { background: #fff; padding: 8px; border-radius: 8px; width: min(260px, 100%); box-sizing: border-box; }
.qr svg { display: block; width: 100%; height: auto; }
.actions .btn { all: unset; cursor: pointer; padding: 8px 16px; border-radius: 8px; border: 1px solid var(--primary-color, #03a9f4); }
.actions .btn.primary { background: var(--primary-color, #03a9f4); color: var(--text-primary-color, #fff); }
${PICKER_STYLE}`;

if (!customElements.get("bgg-shelf-card")) {
  customElements.define("bgg-shelf-card", BggShelfCard);
  window.customCards = window.customCards || [];
  window.customCards.push({
    type: "bgg-shelf-card",
    name: "BGG Spieleregal",
    description: "Zeigt die BGG-Sammlung als Billy-Regale anhand der Standorte.",
  });
}
if (!customElements.get("bgg-play-card")) {
  customElements.define("bgg-play-card", BggPlayCard);
  window.customCards = window.customCards || [];
  window.customCards.push({
    type: "bgg-play-card",
    name: "BGG Partie",
    description: "Laufende Partie mit Mitspielern, Gewinner, Beenden und Verwerfen.",
  });
}
