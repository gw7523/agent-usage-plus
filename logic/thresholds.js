// Percentage-to-severity classification for meters and the bar icon.
//
// Moved out of Panel.qml's inline `>= 0.9` / `<= 0.1` comparisons (issue
// #1). Issue #6 replaces the original boolean alarming/not-alarming check
// with a three-level severity model driven by two user-configurable
// thresholds (percentage points, 0-100), so the panel can show a distinct
// "warn" state before things turn "critical".

var DEFAULT_WARN_PCT = 75
var DEFAULT_CRITICAL_PCT = 90

var SEVERITY_RANK = {
  ok: 0,
  warn: 1,
  critical: 2
}

// Classifies a percentage-point value (0-100 scale, matching the
// warnThresholdPct/criticalThresholdPct manifest settings) into one of
// "ok" | "warn" | "critical".
//
// `thresholds.warn` and `thresholds.critical` fall back to the defaults
// above when missing or non-numeric. A misconfigured `warn >= critical` is
// guarded rather than left to produce contradictory output: `critical`
// stays the effective floor for "critical", and `warn` is clamped down so
// it can never report a state at or above `critical` — in that case the
// "warn" band simply collapses to empty and values classify as "ok" or
// "critical" only.
function severityFor(pct, thresholds) {
  var opts = thresholds || {}
  var warn = typeof opts.warn === "number" && isFinite(opts.warn) ? opts.warn : DEFAULT_WARN_PCT
  var critical = typeof opts.critical === "number" && isFinite(opts.critical) ? opts.critical : DEFAULT_CRITICAL_PCT

  if (warn >= critical) warn = critical

  if (typeof pct !== "number" || !isFinite(pct)) return "ok"
  if (pct >= critical) return "critical"
  if (pct >= warn) return "warn"
  return "ok"
}

function normaliseSeverity(value) {
  return value === "warn" || value === "critical" ? value : "ok"
}

// Returns the state after a refresh and the one notification, if any, that
// refresh crossed. An unknown previous state establishes a baseline without
// notifying: a widget starting while usage is already high did not observe a
// crossing. A jump from a known ok state straight to critical emits only
// critical, because the observer did not see a warn crossing. The returned
// state must be stored even when no notification is emitted so a later drop
// below a threshold rearms the next upward crossing without repeating on
// every refresh while the value remains in the same band.
function notificationTransition(previousSeverity, currentSeverity) {
  var hasPrevious = previousSeverity === "ok"
    || previousSeverity === "warn"
    || previousSeverity === "critical"
  var previous = normaliseSeverity(previousSeverity)
  var current = normaliseSeverity(currentSeverity)
  return {
    severity: current,
    notification: hasPrevious && SEVERITY_RANK[current] > SEVERITY_RANK[previous] ? current : ""
  }
}

// The stricter of two severities — for collapsing several meters (every
// limit window plus a credit gauge) into one chip-level state.
function worstSeverity(a, b) {
  var left = normaliseSeverity(a)
  var right = normaliseSeverity(b)
  return SEVERITY_RANK[right] > SEVERITY_RANK[left] ? right : left
}

if (typeof module !== "undefined" && module.exports) {
  module.exports = {
    DEFAULT_WARN_PCT: DEFAULT_WARN_PCT,
    DEFAULT_CRITICAL_PCT: DEFAULT_CRITICAL_PCT,
    severityFor: severityFor,
    worstSeverity: worstSeverity,
    notificationTransition: notificationTransition
  }
}
