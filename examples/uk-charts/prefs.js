// @file prefs.js
//
// @brief Browser-persistent user preferences for the UK Charts Explorer.
//
// Stores a small JSON blob in localStorage under the key "ukChartsPrefs".
// No server round-trips, no cookies, no permissions required.  Storage is
// domain-scoped by the browser and survives page reloads and restarts.
//
// Schema
// ------
//   simulation   — global D3 force-simulation tuning (alphaDecay etc.)
//   nodePhysics  — per-node-type physics overrides (target_radius, link_strength, …)
//   graphBehavior — per-node-type operational flags (auto_expand)
//   nodeColour   — fill colour per node type (CSS hex strings)
//   linkColour   — stroke colour per link type (CSS hex strings)
//   chartMode    — "albums" | "singles"
//
// All keys default to the values in DEFAULTS.  Persisted prefs are merged
// *shallowly* per section on top of DEFAULTS at load time, so adding a new
// key to DEFAULTS is always safe — it appears automatically for users who
// have never set it.
//
// API
// ---
//   Prefs.get(section)             — return a merged section object
//   Prefs.get(section, key)        — return a single value
//   Prefs.set(section, key, val)   — persist a single value
//   Prefs.setSection(section, obj) — persist an entire section
//   Prefs.reset()                  — wipe all overrides, restore DEFAULTS
//
// @copyright Copyright (c) 2026 Tim Hosking
// @see https://github.com/munger
// @par Licence: MIT

// ============================================================================
// DEFAULTS
// ============================================================================

/** @brief Canonical default values for every preference key. */
const DEFAULTS = {

  /** Global D3 simulation parameters — apply to the simulation as a whole,
   *  not to individual node types.  Per-node physics lives in nodePhysics. */
  simulation: {
    alphaDecay:     0.01,   // rate at which the simulation cools; lower = longer settle
    velocityDecay:  0.4,    // friction applied each tick; higher = nodes stop faster
    centerStrength: 0.02,   // gentle pull toward canvas centre — keeps graph from drifting off-screen
    chargeDistMax:  800,    // beyond this distance nodes stop repelling each other
    angleStrength:  8,      // stiffness of the angular spring holding temporal nodes on their spoke
    radialStrength: 0.25,   // stiffness of the radial spring — lower than angle so nodes can breathe
    nudgeBatch:     20,     // how many nodes to add before rebinding the simulation
    nudgeAlpha:     0.3,    // alpha is capped at this floor on nudge so the sim never fully cools mid-expansion
    dragAlpha:      0.3,    // alpha injected when the user releases a dragged node
  },

  /** Per-node-type physics parameters.  These mirror PhysicsMixin defaults on
   *  the server; values here override what the server sends at runtime so the
   *  sidebar can adjust them without restarting. */
  nodePhysics: {
    timeline: { target_radius: 0,   link_strength: 0.8,  child_spread: 100, charge: -60, collide_pad: 3 },
    decade:   { target_radius: 220, link_strength: 0.8,  child_spread: 80,  charge: -40, collide_pad: 3 },
    year:     { target_radius: 160, link_strength: 0.8,  child_spread: 80,  charge: -30, collide_pad: 3 },
    month:    { target_radius: 120, link_strength: 0.8,  child_spread: 80,  charge: -25, collide_pad: 3 },
    week:     { target_radius: 80,  link_strength: 0.45, child_spread: 80,  charge: -20, collide_pad: 3 },
    release:  { target_radius: 70,  link_strength: 0.55, child_spread: 300, charge: -20, collide_pad: 3 },
    artist:   { target_radius: 50,  link_strength: 0.1,  child_spread: 300, charge: -20, collide_pad: 3 },
  },

  /** GraphBehavior flags — per-node-type operational switches.
   *  When auto_expand is true the server expands that node type automatically. */
  graphBehavior: {
    timeline: { auto_expand: false },
    decade:   { auto_expand: false },
    year:     { auto_expand: false },
    month:    { auto_expand: false },
    week:     { auto_expand: false },
    release:  { auto_expand: false },
    artist:   { auto_expand: false },
  },

  /** Node fill colours by node_type (CSS hex strings). */
  nodeColour: {
    timeline: "#ccad00",
    decade:   "#cc7a00",
    year:     "#e05a00",
    month:    "#c0392b",
    week:     "#3949ab",
    release:  "#1a237e",
    artist:   "#2e7d32",
  },

  /** Link stroke colours keyed by "sourceType-targetType". */
  linkColour: {
    "timeline-decade": "#ccad00",
    "decade-year":     "#cc790e",
    "year-month":      "#aa2020",
    "month-week":      "#3949ab",
    "week-release":    "#283593",
    "release-artist":  "#2e7d32",
    "artist-release":  "#2e7d32",
    "artist-artist":   "#1a237e",
  },

  /** Which chart to show — "albums" or "singles". */
  chartMode: "albums",
};

// ============================================================================
// Prefs
// ============================================================================

/**
 * @brief Thin wrapper around localStorage for UK Charts user preferences.
 *
 * The persisted object is always a *delta* — only keys that differ from
 * DEFAULTS are written.  This keeps the stored blob small and means new
 * DEFAULTS keys appear automatically without a migration step.
 *
 * Internal layout of localStorage["ukChartsPrefs"]:
 *   { physics: {...overrides}, nodeColour: {...overrides}, ... }
 *
 * Sections absent from the stored blob are served entirely from DEFAULTS.
 * Scalar top-level keys (e.g. chartMode) are stored as-is.
 */
const Prefs = (() => {

  const STORAGE_KEY = "ukChartsPrefs";

  // -------------------------------------------------------------------------
  // Internal helpers
  // -------------------------------------------------------------------------

  /**
   * @brief Read the raw delta object from localStorage, or {} on any error.
   * @return Plain object containing only keys the user has overridden.
   */
  function _load() {
    try {
      // getItem returns null when the key is absent; fall back to "{}" so
      // JSON.parse always receives a valid string rather than null.
      return JSON.parse(localStorage.getItem(STORAGE_KEY) || "{}");
    } catch (_) {
      // JSON.parse throws SyntaxError if the stored value is corrupted
      // (e.g. truncated by a browser crash).  Return an empty delta so the
      // caller continues with DEFAULTS rather than surfacing an error.
      return {};
    }
  }

  /**
   * @brief Persist *delta* to localStorage.
   * @param delta  Plain object of overrides to store.
   */
  function _save(delta) {
    try {
      localStorage.setItem(STORAGE_KEY, JSON.stringify(delta));
    } catch (_) {
      // setItem throws when the browser's storage quota is exceeded, or in
      // private-browsing mode where writes are blocked.  Preferences are
      // advisory; swallowing the error is preferable to crashing the UI.
    }
  }

  // -------------------------------------------------------------------------
  // Public API
  // -------------------------------------------------------------------------

  /**
   * @brief Return a merged preference value.
   *
   * Called with one argument returns the full section (DEFAULTS merged with
   * any stored overrides).  Called with two arguments returns a single key
   * from that section, or the scalar top-level value when *section* is a
   * scalar (e.g. "chartMode").
   *
   * @param section  Top-level section name (e.g. "physics", "nodeColour").
   * @param key      Optional key within the section.
   * @return Merged section object, or a single value.
   */
  function get(section, key) {
    const stored = _load();
    const def    = DEFAULTS[section];

    // Scalar sections (e.g. chartMode) are not objects, so object-merge logic
    // does not apply.  The null check guards against a DEFAULTS entry that is
    // explicitly set to null — typeof null === "object" in JS.
    if (typeof def !== "object" || def === null) {
      // Use the `in` operator rather than truthiness so that a stored value
      // of false or 0 is not ignored in favour of the default.
      const val = section in stored ? stored[section] : def;
      // A key argument on a scalar section makes no sense — return undefined
      // so the caller can detect the mistake rather than getting a silent null.
      return key === undefined ? val : undefined;
    }

    // Spread DEFAULTS first so every key is present in the result, then
    // overwrite with only the keys the user has stored.  Object.assign is
    // intentionally shallow — each section is a flat key/value bag.
    const merged = Object.assign({}, def, stored[section] || {});
    // || {} guards against a stored entry that is null or a non-object value
    // left by a write from an older version of this module.
    return key === undefined ? merged : merged[key];
  }

  /**
   * @brief Persist a single preference key within *section*.
   *
   * Only the supplied key is written; all other keys in the section remain
   * at their current stored (or default) values.
   *
   * For scalar sections call as set("chartMode", "singles") — there is no
   * sub-key, so *key* is treated as the value and *value* is ignored.
   *
   * @param section  Top-level section name.
   * @param key      Key within the section, or the new value for scalars.
   * @param value    New value to store (unused for scalar sections).
   * @return undefined
   */
  function set(section, key, value) {
    // Read-modify-write: load the full delta so we do not lose other sections
    // that have already been stored.  localStorage has no partial-update API.
    const delta = _load();

    if (typeof DEFAULTS[section] === "object" && DEFAULTS[section] !== null) {
      // Initialise the section bucket if this is the first override for it.
      if (!delta[section]) delta[section] = {};
      delta[section][key] = value;
    } else {
      // Scalar section — the caller passes the new value as the second
      // argument because there is no sub-key to name.
      delta[section] = key;
    }
    _save(delta);
  }

  /**
   * @brief Replace an entire section with *obj* (merged on top of DEFAULTS).
   *
   * Only keys present in *obj* are persisted; keys absent from *obj* fall
   * back to DEFAULTS at next read.  Useful when saving a whole settings panel
   * at once rather than individual fields.
   *
   * @param section  Top-level section name.
   * @param obj      Partial or complete section object.
   * @return undefined
   */
  function setSection(section, obj) {
    // Load first so other sections in the delta are preserved.
    const delta = _load();
    delta[section] = obj;
    _save(delta);
  }

  /**
   * @brief Remove all stored overrides, reverting every key to DEFAULTS.
   * @return undefined
   */
  function reset() {
    try {
      // removeItem is a no-op if the key is absent, so no existence check
      // is needed.  The try/catch guards against private-browsing mode, where
      // even reads on localStorage can throw in some browsers.
      localStorage.removeItem(STORAGE_KEY);
    } catch (_) {}
  }

  return { DEFAULTS, get, set, setSection, reset };

})();
