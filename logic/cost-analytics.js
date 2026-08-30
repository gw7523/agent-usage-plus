// Pure, display-ready summaries for the optional API-rate cost data.
//
// Collectors already validate the cost contract before it reaches QML, but
// this module keeps chart preparation defensive and testable: no UI code has
// to sort untrusted rows, divide by zero, or guess what an empty breakdown
// means.

function nonnegative(value) {
  var number = Number(value)
  return isFinite(number) && number >= 0 ? number : 0
}

function coverageFor(cost) {
  if (!cost || typeof cost !== "object") return -1
  var priced = nonnegative(cost.pricedTokens)
  var unpriced = nonnegative(cost.unpricedTokens)
  var total = priced + unpriced
  return total > 0 ? priced / total : -1
}

function modelRows(cost) {
  var input = cost && Array.isArray(cost.byModel) ? cost.byModel : []
  var rows = []
  for (var i = 0; i < input.length; i++) {
    var entry = input[i] || {}
    var model = String(entry.model || "")
    if (model === "") continue
    rows.push({
      model: model,
      usd: nonnegative(entry.usd),
      tokens: nonnegative(entry.tokens)
    })
  }

  rows.sort(function(a, b) {
    if (b.usd !== a.usd) return b.usd - a.usd
    if (b.tokens !== a.tokens) return b.tokens - a.tokens
    return a.model < b.model ? -1 : (a.model > b.model ? 1 : 0)
  })

  var total = 0
  for (var j = 0; j < rows.length; j++) total += rows[j].usd
  for (var k = 0; k < rows.length; k++) rows[k].share = total > 0 ? rows[k].usd / total : 0
  return rows
}

function dailyRows(cost) {
  var input = cost && Array.isArray(cost.byDay) ? cost.byDay : []
  var rows = []
  for (var i = 0; i < input.length; i++) {
    var entry = input[i] || {}
    var date = String(entry.date || "")
    if (date === "") continue
    var existing = -1
    for (var j = 0; j < rows.length; j++) {
      if (rows[j].date === date) {
        existing = j
        break
      }
    }
    if (existing >= 0) rows[existing].usd += nonnegative(entry.usd)
    else rows.push({ date: date, usd: nonnegative(entry.usd) })
  }

  rows.sort(function(a, b) { return a.date < b.date ? -1 : (a.date > b.date ? 1 : 0) })
  return rows
}

function summary(cost) {
  var models = modelRows(cost)
  var days = dailyRows(cost)
  var dailyTotalUsd = 0
  var dailyPeakUsd = 0
  var dailyActiveDays = 0
  for (var i = 0; i < days.length; i++) {
    dailyTotalUsd += days[i].usd
    dailyPeakUsd = Math.max(dailyPeakUsd, days[i].usd)
    if (days[i].usd > 0) dailyActiveDays++
  }

  return {
    totalUsd: nonnegative(cost && cost.estimateUsd),
    period: String(cost && cost.period || ""),
    models: models,
    days: days,
    dailyTotalUsd: dailyTotalUsd,
    dailyPeakUsd: dailyPeakUsd,
    dailyActiveDays: dailyActiveDays,
    averageDailyUsd: dailyActiveDays > 0 ? dailyTotalUsd / dailyActiveDays : 0,
    coverage: coverageFor(cost),
    topModel: models.length > 0 ? models[0].model : ""
  }
}

function providerRows(providers) {
  var list = Array.isArray(providers) ? providers : []
  var rows = []
  var total = 0
  for (var i = 0; i < list.length; i++) {
    var provider = list[i] || {}
    var cost = provider.cost
    var estimate = Number(cost && cost.estimateUsd)
    if (!cost || typeof cost !== "object" || !isFinite(estimate) || estimate < 0) continue
    estimate = nonnegative(estimate)
    rows.push({
      providerId: String(provider.providerId || ""),
      providerName: String(provider.providerName || provider.providerId || "Provider"),
      estimateUsd: estimate,
      incomplete: cost.incomplete === true,
      period: String(cost.period || ""),
      coverage: coverageFor(cost)
    })
    total += estimate
  }

  rows.sort(function(a, b) {
    if (b.estimateUsd !== a.estimateUsd) return b.estimateUsd - a.estimateUsd
    return a.providerName < b.providerName ? -1 : (a.providerName > b.providerName ? 1 : 0)
  })
  for (var j = 0; j < rows.length; j++) rows[j].share = total > 0 ? rows[j].estimateUsd / total : 0
  return rows
}

if (typeof module !== "undefined" && module.exports) {
  module.exports = {
    coverageFor: coverageFor,
    modelRows: modelRows,
    dailyRows: dailyRows,
    summary: summary,
    providerRows: providerRows
  }
}
