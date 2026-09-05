// Pure API-rate cost estimator for transcript collectors. This is purposely
// separate from the QML display parser: collectors can require it in Node,
// while QML can import it if it ever needs to explain a price version.

var Catalogue = typeof require !== "undefined" ? require("./api-price-catalogue.js") : PRICE_CATALOGUE

function nonnegative(value) {
  var n = Number(value)
  return isFinite(n) && n > 0 ? n : 0
}

function bucketTokens(bucket) {
  var b = bucket || {}
  return nonnegative(b.inputTokens) + nonnegative(b.outputTokens)
    + nonnegative(b.cacheReadInputTokens) + nonnegative(b.cacheCreationInputTokens)
}

function bucketUsd(bucket, price) {
  var b = bucket || {}
  return (nonnegative(b.inputTokens) * price.input
    + nonnegative(b.outputTokens) * price.output
    + nonnegative(b.cacheReadInputTokens) * price.cacheRead
    + nonnegative(b.cacheCreationInputTokens) * price.cacheWrite) / 1000000
}

function priceFor(provider, model) {
  var p = Catalogue.providers[String(provider || "")] || null
  return p && p.models[String(model || "")] ? p.models[String(model || "")] : null
}

function activeDayCount(value) {
  var n = Number(value)
  return isFinite(n) && n > 0 ? Math.min(100000, Math.floor(n)) : 0
}

// Returns `{ cost, unknownModels, pricingVersion }`. `cost` is null only when
// every used model has no exact catalogue entry; a mixed result is an
// explicitly partial subtotal whose callers must surface `unknownModels`.
// `dailyModelUsage` is optional: `{ "YYYY-MM-DD": { model: TokenBucket }}`.
function calculateCost(input) {
  var options = input || {}
  var provider = String(options.provider || "")
  var usage = options.modelUsage && typeof options.modelUsage === "object" ? options.modelUsage : {}
  var byModel = []
  var unknown = []
  var total = 0
  var pricedTokens = 0
  var unpricedTokens = 0

  for (var model in usage) {
    var bucket = usage[model]
    var tokens = bucketTokens(bucket)
    if (tokens === 0) continue
    var price = priceFor(provider, model)
    if (!price) { unknown.push(model); unpricedTokens += tokens; continue }
    var usd = bucketUsd(bucket, price)
    total += usd
    pricedTokens += tokens
    byModel.push({ model: model, usd: usd, tokens: Math.round(tokens) })
  }

  var daily = options.dailyModelUsage && typeof options.dailyModelUsage === "object" ? options.dailyModelUsage : {}
  var byDay = []
  for (var date in daily) {
    var dayUsd = 0
    var dayUsage = daily[date] || {}
    for (var dayModel in dayUsage) {
      var dayTokens = bucketTokens(dayUsage[dayModel])
      if (dayTokens === 0) continue
      var dayPrice = priceFor(provider, dayModel)
      if (!dayPrice) {
        if (unknown.indexOf(dayModel) < 0) unknown.push(dayModel)
        continue
      }
      dayUsd += bucketUsd(dayUsage[dayModel], dayPrice)
    }
    if (dayUsd > 0) byDay.push({ date: String(date), usd: dayUsd })
  }

  byModel.sort(function(a, b) { return b.usd - a.usd })
  byDay.sort(function(a, b) { return a.date < b.date ? -1 : a.date > b.date ? 1 : 0 })
  return {
    pricingVersion: Catalogue.version,
    unknownModels: unknown.sort(),
    // Never turn an entirely unknown transcript into a fabricated $0. But
    // one unpriced internal model must not hide all of Codex's known usage.
    // The result explicitly marks that subtotal as partial.
    cost: pricedTokens === 0 && unknown.length > 0 ? null : {
      estimateUsd: total,
      period: String(options.period || ""),
      pricingVersion: Catalogue.version,
      byModel: byModel,
      byDay: byDay,
      incomplete: unknown.length > 0,
      unknownModels: unknown.sort(),
      pricedTokens: Math.round(pricedTokens),
      unpricedTokens: Math.round(unpricedTokens),
      // The base transcript collector knows how many distinct days its
      // aggregate covers even when it cannot retain a per-day/model matrix.
      // Keep that bounded fact so the UI can still calculate a truthful
      // average instead of displaying a broken zero.
      activeDays: activeDayCount(options.activeDays)
    }
  }
}

if (typeof module !== "undefined" && module.exports)
  module.exports = { priceFor: priceFor, bucketTokens: bucketTokens, bucketUsd: bucketUsd, calculateCost: calculateCost, PRICE_CATALOGUE: Catalogue }
