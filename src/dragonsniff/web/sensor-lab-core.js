"use strict";
/* Offline-only measurement processing. No device, network, or storage APIs. */
(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.SensorLabCore = api;
})(typeof globalThis === "object" ? globalThis : this, function () {
  const HEADERS = ["timestamp_us", "channel", "source_index", "raw", "millivolts", "temperature_c", "status", "rref_kohm"];
  const MAX_BYTES = 131072;
  const MAX_ROWS = 256;
  const STATUSES = new Set(["ok", "open", "short", "uninit"]);
  const CHANNELS = new Set(["ptc", "chamber"]);

  function csvRows(text) {
    if (text.charCodeAt(0) === 0xfeff) text = text.slice(1);
    const rows = []; let row = []; let field = ""; let quoted = false; let closed = false;
    for (let i = 0; i < text.length; i++) {
      const ch = text[i];
      if (quoted) {
        if (ch === '"') {
          if (text[i + 1] === '"') { field += '"'; i++; }
          else { quoted = false; closed = true; }
        } else { field += ch; }
      } else if (ch === "," || ch === "\n" || ch === "\r") {
        row.push(field); field = ""; closed = false;
        if (ch !== ",") {
          if (ch === "\r" && text[i + 1] === "\n") i++;
          rows.push(row); row = [];
          if (rows.length > MAX_ROWS + 2) throw Error("Too many CSV records (maximum 256)");
        }
      } else if (ch === '"' && field === "" && !closed) {
        quoted = true;
      } else {
        if (ch === '"' || closed) throw Error("Malformed CSV quoting");
        field += ch;
      }
    }
    if (quoted) throw Error("Unclosed CSV quotation");
    if (field !== "" || row.length) { row.push(field); rows.push(row); }
    while (rows.length && rows[rows.length - 1].length === 1 && rows[rows.length - 1][0] === "") rows.pop();
    return rows;
  }
  function numeric(value, label, row, opts = {}) {
    if (value.trim() === "" && opts.blank) return null;
    if (!(opts.integer ? /^-?\d+$/ : /^-?\d+(?:\.\d+)?$/).test(value)) throw Error(`Row ${row}: invalid ${label}`);
    const n = Number(value);
    if (!Number.isFinite(n) || (opts.integer && !Number.isSafeInteger(n)) ||
        (opts.min !== undefined && n < opts.min)) throw Error(`Row ${row}: invalid ${label}`);
    return n;
  }
  function parse(text) {
    if (typeof text !== "string") throw Error("Expected UTF-8 CSV text");
    if (new TextEncoder().encode(text).length > MAX_BYTES) throw Error("CSV exceeds 128 KiB limit");
    const rows = csvRows(text);
    if (!rows.length) throw Error("Empty CSV: header required");
    const header = rows.shift();
    if (header.length !== HEADERS.length || header.some((x, i) => x !== HEADERS[i]))
      throw Error("Unsupported CSV header: expected exact ADC capture columns in order");
    if (rows.length > MAX_ROWS) throw Error("Too many CSV records (maximum 256)");
    const samples = [];
    rows.forEach((fields, i) => {
      const line = i + 2;
      if (fields.length !== HEADERS.length) throw Error(`Row ${line}: expected 8 fields`);
      const [timestamp, channel, source, raw, mv, temp, status, rref] = fields;
      const timestamp_us = numeric(timestamp, "timestamp_us", line, { integer: true, min: 0 });
      if (!CHANNELS.has(channel)) throw Error(`Row ${line}: unsupported channel`);
      const source_index = numeric(source, "source_index", line, { integer: true, min: 0 });
      if (source_index > 1) throw Error(`Row ${line}: invalid source_index`);
      const rawValue = numeric(raw, "raw", line, { integer: true, min: -1 });
      if (rawValue > 4095) throw Error(`Row ${line}: raw exceeds 12-bit ADC range`);
      const millivolts = numeric(mv, "millivolts", line, { integer: true, min: -1 });
      const temperature_c = numeric(temp, "temperature_c", line, { blank: true });
      if (!STATUSES.has(status)) throw Error(`Row ${line}: unsupported status`);
      const rref_kohm = numeric(rref, "rref_kohm", line, { min: 0 });
      if (rref_kohm === 0) throw Error(`Row ${line}: invalid rref_kohm`);
      if (status !== "ok" && temperature_c !== null) throw Error(`Row ${line}: non-OK temperature must be blank`);
      if (status === "ok" && (rawValue < 0 || millivolts < 0))
        throw Error(`Row ${line}: OK measurement is incomplete`);
      if (samples.length && timestamp_us < samples[samples.length - 1].timestamp_us)
        throw Error(`Row ${line}: timestamps are not monotonic`);
      samples.push(Object.freeze({ timestamp_us, channel, source_index, raw: rawValue,
        millivolts, temperature_c, status, rref_kohm, row: line }));
    });
    return Object.freeze(samples);
  }
  function selected(samples, channel, start = -Infinity, end = Infinity) {
    return samples.filter(s => s.channel === channel && s.timestamp_us >= start && s.timestamp_us <= end);
  }
  function available(sample, metric) {
    if (sample[metric] === null || sample[metric] === undefined) return false;
    return metric === "temperature_c"
      ? sample.status === "ok" && Number.isFinite(sample.temperature_c)
      : Number.isFinite(sample[metric]) && sample[metric] >= 0;
  }
  function stats(samples, channel, metric = "temperature_c", start = -Infinity, end = Infinity) {
    if (!["raw", "millivolts", "temperature_c"].includes(metric)) throw Error("Unsupported metric");
    const all = selected(samples, channel, start, end);
    const valid = all.filter(s => available(s, metric));
    const values = valid.map(s => s[metric]); const times = all.map(s => s.timestamp_us);
    const gaps = []; const intervals = [];
    for (let i = 1; i < times.length; i++) intervals.push(times[i] - times[i - 1]);
    const sorted = intervals.filter(value => value > 0).sort((a, b) => a - b);
    const median = sorted.length ? (sorted[(sorted.length - 1) >> 1] + sorted[sorted.length >> 1]) / 2 : null;
    if (median !== null && median > 0) intervals.forEach((v, i) => {
      if (v > median * 1.5) gaps.push({ after_row: all[i].row, interval_us: v });
    });
    const mean = values.length ? values.reduce((a, b) => a + b, 0) / values.length : null;
    const variance = values.length > 1 ? values.reduce((a, b) => a + (b - mean) ** 2, 0) / values.length : null;
    let largestChange = null;
    for (let i = 1; i < all.length; i++) {
      const a = all[i - 1], b = all[i];
      if (available(a, metric) && available(b, metric)) {
        const d = Math.abs(b[metric] - a[metric]);
        largestChange = largestChange === null ? d : Math.max(largestChange, d);
      }
    }
    return { channel, metric, count: all.length, valid_count: values.length,
      invalid_count: all.length - valid.length,
      duration_us: all.length > 1 ? times[times.length - 1] - times[0] : null,
      min: values.length ? Math.min(...values) : null, max: values.length ? Math.max(...values) : null,
      mean, stddev: variance === null ? null : Math.sqrt(variance),
      peak_to_peak: values.length ? Math.max(...values) - Math.min(...values) : null,
      median_interval_us: median, largest_adjacent_change: largestChange,
      irregular_intervals: gaps, tied_intervals: intervals.filter(v => v === 0).length };
  }
  function compare(a, b) {
    if (!a || !b) return null;
    if (a.channel !== b.channel) return { cross_channel: true, elapsed_us: b.timestamp_us - a.timestamp_us, raw_difference: null, millivolts_difference: null, temperature_difference_c: null, apparent_rate_c_per_s: null };
    const dt = b.timestamp_us - a.timestamp_us;
    const delta = key => !available(a, key) || !available(b, key)
      ? null : b[key] - a[key];
    const d = { elapsed_us: dt, raw_difference: delta("raw"),
      millivolts_difference: delta("millivolts"), temperature_difference_c: delta("temperature_c") };
    d.apparent_rate_c_per_s = dt === 0 || d.temperature_difference_c === null ? null :
      d.temperature_difference_c / (dt / 1e6);
    return d;
  }
  function graphPoints(samples, channel, metric, start, end) {
    return selected(samples, channel, start, end).map(s => ({
      row: s.row, timestamp_us: s.timestamp_us, value: available(s, metric) ? s[metric] : null,
      valid: available(s, metric), status: s.status
    }));
  }
  return { HEADERS, MAX_BYTES, MAX_ROWS, parse, selected, available, stats, compare, graphPoints };
});
