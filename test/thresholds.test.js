"use strict"

const test = require("node:test")
const assert = require("node:assert/strict")

const Thresholds = require("../logic/thresholds.js")

test("severityFor: default thresholds (75/90) boundaries", () => {
  assert.equal(Thresholds.severityFor(74), "ok")
  assert.equal(Thresholds.severityFor(75), "warn")
  assert.equal(Thresholds.severityFor(76), "warn")
  assert.equal(Thresholds.severityFor(89), "warn")
  assert.equal(Thresholds.severityFor(90), "critical")
  assert.equal(Thresholds.severityFor(91), "critical")
})

test("severityFor: custom thresholds (warn=60, critical=80) boundaries", () => {
  const t = { warn: 60, critical: 80 }
  assert.equal(Thresholds.severityFor(59, t), "ok")
  assert.equal(Thresholds.severityFor(60, t), "warn")
  assert.equal(Thresholds.severityFor(61, t), "warn")
  assert.equal(Thresholds.severityFor(79, t), "warn")
  assert.equal(Thresholds.severityFor(80, t), "critical")
  assert.equal(Thresholds.severityFor(81, t), "critical")
})

test("severityFor: misconfigured warn >= critical collapses the warn band instead of contradicting", () => {
  const t = { warn: 90, critical: 80 }
  assert.equal(Thresholds.severityFor(70, t), "ok")
  assert.equal(Thresholds.severityFor(79, t), "ok")
  assert.equal(Thresholds.severityFor(80, t), "critical")
  assert.equal(Thresholds.severityFor(95, t), "critical")
})

test("severityFor: non-numeric or missing percent is never alarming", () => {
  assert.equal(Thresholds.severityFor(undefined), "ok")
  assert.equal(Thresholds.severityFor(null), "ok")
  assert.equal(Thresholds.severityFor(NaN), "ok")
  assert.equal(Thresholds.severityFor("95"), "ok")
  assert.equal(Thresholds.severityFor(-1), "ok")
})

test("severityFor: falls back to defaults when thresholds are missing or non-numeric", () => {
  assert.equal(Thresholds.severityFor(80, {}), "warn")
  assert.equal(Thresholds.severityFor(80, { warn: "x", critical: null }), "warn")
  assert.equal(Thresholds.severityFor(95), "critical")
})

test("notificationTransition: emits once when warn is crossed, then stays quiet", () => {
  assert.deepEqual(Thresholds.notificationTransition("ok", "warn"), {
    severity: "warn",
    notification: "warn"
  })
  assert.deepEqual(Thresholds.notificationTransition("warn", "warn"), {
    severity: "warn",
    notification: ""
  })
})

test("notificationTransition: emits once more at critical, not on every higher percent", () => {
  assert.deepEqual(Thresholds.notificationTransition("warn", "critical"), {
    severity: "critical",
    notification: "critical"
  })
  assert.deepEqual(Thresholds.notificationTransition("critical", "critical"), {
    severity: "critical",
    notification: ""
  })
})

test("notificationTransition: an unknown first sample establishes a quiet baseline", () => {
  assert.deepEqual(Thresholds.notificationTransition(undefined, "critical"), {
    severity: "critical",
    notification: ""
  })
  assert.deepEqual(Thresholds.notificationTransition("ok", "critical"), {
    severity: "critical",
    notification: "critical"
  })
})

test("notificationTransition: descending below a threshold rearms the next crossing", () => {
  assert.deepEqual(Thresholds.notificationTransition("critical", "warn"), {
    severity: "warn",
    notification: ""
  })
  assert.deepEqual(Thresholds.notificationTransition("warn", "ok"), {
    severity: "ok",
    notification: ""
  })
  assert.deepEqual(Thresholds.notificationTransition("ok", "warn"), {
    severity: "warn",
    notification: "warn"
  })
})

test("worstSeverity: collapses several meter severities to the strictest", () => {
  const T = require("../logic/thresholds.js")
  assert.equal(T.worstSeverity("ok", "ok"), "ok")
  assert.equal(T.worstSeverity("ok", "warn"), "warn")
  assert.equal(T.worstSeverity("warn", "ok"), "warn")
  assert.equal(T.worstSeverity("warn", "critical"), "critical")
  assert.equal(T.worstSeverity("critical", "warn"), "critical")
  // Unknown values normalise to ok instead of poisoning the fold.
  assert.equal(T.worstSeverity(undefined, "warn"), "warn")
  assert.equal(T.worstSeverity("nonsense", null), "ok")
})
