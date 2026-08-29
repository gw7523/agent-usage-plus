"use strict"

const test = require("node:test")
const assert = require("node:assert/strict")
const fs = require("node:fs")
const path = require("node:path")

const panel = path.join(__dirname, "..", "Panel.qml")

test("panel restores a compact estimated API cost card", () => {
  const source = fs.readFileSync(panel, "utf8")
  assert.match(source, /id: costSection/)
  assert.match(source, /visible: !root\.settingsOpen && !!root\.cost/)
  assert.match(source, /text: "Estimated API cost"/)
  assert.match(source, /root\.cost\.estimateUsd/)
})
