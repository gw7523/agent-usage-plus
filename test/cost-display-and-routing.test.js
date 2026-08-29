"use strict"

const test = require("node:test")
const assert = require("node:assert/strict")
const fs = require("node:fs")
const os = require("node:os")
const path = require("node:path")
const { execFileSync } = require("node:child_process")

const panel = path.join(__dirname, "..", "Panel.qml")
const localUpdater = path.join(__dirname, "../../../../../../personal/.local/bin/omarchy-agent-usage-update")

function executable(file, body) {
  fs.writeFileSync(file, `#!/usr/bin/env bash\nset -euo pipefail\n${body}\n`, { mode: 0o755 })
}

test("local updater uses the cost-aware collector when one is available", t => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "agent-usage-cost-routing-"))
  t.after(() => fs.rmSync(root, { recursive: true, force: true }))
  const omarchyBin = path.join(root, "omarchy", "bin")
  const collectorBin = path.join(root, "collectors", "bin")
  fs.mkdirSync(omarchyBin, { recursive: true })
  fs.mkdirSync(collectorBin, { recursive: true })

  executable(path.join(omarchyBin, "omarchy-agent-usage-claude"),
    'printf \'%s\\n\' \'{"id":"claude","ready":true}\'')
  executable(path.join(collectorBin, "omarchy-agent-usage-claude-cost"),
    'printf \'%s\\n\' \'{"id":"claude","ready":true,"cost":{"estimateUsd":12.34,"byModel":[]}}\'')

  execFileSync("bash", [localUpdater, "--force", "claude"], {
    env: {
      ...process.env,
      HOME: root,
      XDG_STATE_HOME: path.join(root, "state"),
      OMARCHY_PATH: path.join(root, "omarchy"),
      AGENT_USAGE_PLUS_COLLECTOR_BIN: collectorBin,
    },
  })

  const record = JSON.parse(fs.readFileSync(path.join(root, "state", "omarchy", "agents", "usage", "claude.json")))
  assert.equal(record.cost.estimateUsd, 12.34)
})

test("panel restores a compact estimated API cost card", () => {
  const source = fs.readFileSync(panel, "utf8")
  assert.match(source, /id: costSection/)
  assert.match(source, /visible: !root\.settingsOpen && !!root\.cost/)
  assert.match(source, /text: "Estimated API cost"/)
  assert.match(source, /root\.cost\.estimateUsd/)
})
