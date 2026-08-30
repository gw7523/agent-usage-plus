"use strict"

const test = require("node:test")
const assert = require("node:assert/strict")
const fs = require("node:fs")
const os = require("node:os")
const path = require("node:path")
const { execFileSync } = require("node:child_process")

const panel = path.join(__dirname, "..", "Panel.qml")
const claudeCostCollector = path.join(__dirname, "..", "collectors", "bin", "omarchy-agent-usage-claude-cost")
const costCalculator = path.join(__dirname, "..", "scripts", "calculate-api-cost")

function readCostCard() {
  const source = fs.readFileSync(panel, "utf8")
  const start = source.indexOf("id: costSection")
  const end = source.indexOf("// ---------- Usage ----------", start)
  assert.ok(start >= 0 && end > start, "cost card block should be present")
  return source.slice(start, end)
}

function executable(file, body) {
  fs.writeFileSync(file, `#!/usr/bin/env bash\nset -euo pipefail\n${body}\n`, { mode: 0o755 })
}

test("panel restores a compact estimated API cost card", () => {
  const source = fs.readFileSync(panel, "utf8")
  assert.match(source, /id: costSection/)
  assert.match(source, /visible: !root\.settingsOpen && !!root\.cost/)
  assert.match(source, /text: "Estimated API cost"/)
  assert.match(source, /root\.cost\.estimateUsd/)
})

test("panel guards optional cost values before evaluating a hidden card", () => {
  const source = readCostCard()
  assert.doesNotMatch(source, /text:\s*root\.cost\.incomplete\b/)
  assert.doesNotMatch(source, /color:\s*root\.cost\.incomplete\b/)
  assert.doesNotMatch(source, /text:\s*root\.formatUsd\(root\.cost\.estimateUsd\)/)
  assert.match(source, /root\.cost\s*&&\s*root\.cost\.incomplete/)
  assert.match(source, /root\.cost\s*\?\s*root\.formatUsd\(root\.cost\.estimateUsd\)\s*:\s*""/)
})

test("panel displays the collector cost period next to the estimate", () => {
  assert.match(readCostCard(), /"Estimated API cost"\s*\+\s*\(root\.cost\s*&&\s*root\.cost\.period/)
})

test("Claude cost wrapper routes a base record through the bundled estimator", t => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "agent-usage-cost-routing-"))
  t.after(() => fs.rmSync(root, { recursive: true, force: true }))
  const base = path.join(root, "claude-base")
  executable(base, `printf '%s\\n' '{"id":"claude","ready":true,"modelUsage":{"claude-sonnet-5":{"inputTokens":1000000}}}'`)

  const output = execFileSync(claudeCostCollector, [], {
    env: {
      ...process.env,
      AGENT_USAGE_PLUS_CLAUDE_BASE_COLLECTOR: base,
      AGENT_USAGE_PLUS_COST_HELPER: costCalculator,
    },
    encoding: "utf8",
  })
  const record = JSON.parse(output)
  assert.equal(record.id, "claude")
  assert.equal(record.cost.estimateUsd, 2)
  assert.equal(record.cost.period, "Local transcript history")
})
