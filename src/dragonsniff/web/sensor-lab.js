"use strict";
(() => {
  const core = window.SensorLabCore;
  const el = id => document.getElementById(id);
  const ns = "http://www.w3.org/2000/svg";
  let originals = Object.freeze([]), source = null, selected = null, a = null, b = null;
  let extent = null, view = null, interval = null, drag = null;
  const fmt = (v, digits = 3) => v === null || v === undefined || !Number.isFinite(v) ? "unavailable" : Number(v.toFixed(digits)).toLocaleString("en-US");
  function svg(tag, attrs = {}, text) {
    const node = document.createElementNS(ns, tag);
    Object.entries(attrs).forEach(([k, v]) => node.setAttribute(k, String(v)));
    if (text !== undefined) node.textContent = text;
    return node;
  }
  function message(text, error = false) { el("sensorMessage").textContent = text; el("sensorMessage").style.color = error ? "var(--bad)" : ""; }
  function fieldList(data) {
    const dl = document.createElement("dl");
    Object.entries(data).forEach(([key, value]) => {
      const dt = document.createElement("dt"); dt.textContent = key;
      const dd = document.createElement("dd"); dd.textContent = String(value);
      dl.append(dt, dd);
    });
    return dl;
  }
  function showDetail() {
    const node = el("sensorDetail"); node.replaceChildren();
    if (!selected) { node.textContent = "Select a retained sample."; return; }
    node.append(fieldList({
      "Provenance": "Imported device measurement",
      "CSV row": selected.row, "Time (μs)": selected.timestamp_us,
      "Channel": selected.channel, "ADC input": selected.source_index,
      "Raw (counts)": selected.raw, "Calibrated (mV)": selected.millivolts,
      "Reported (°C)": selected.temperature_c ?? "blank",
      "Status": selected.status, "Rref (kΩ)": selected.rref_kohm
    }));
    const c = core.compare(a, b), compare = el("sensorCompare"); compare.replaceChildren();
    if (!c) { compare.textContent = "Choose A and B from imported records."; return; }
    compare.append(fieldList({
      "A / B rows": `${a.row} / ${b.row}`,
      "Δ time (s)": fmt(c.elapsed_us / 1e6, 6),
      "Δ ADC (counts)": fmt(c.raw_difference),
      "Δ voltage (mV)": fmt(c.millivolts_difference),
      "Δ reported (°C)": fmt(c.temperature_difference_c),
      "Apparent reported rate (°C/s)": fmt(c.apparent_rate_c_per_s, 5)
    }));
  }
  function updateStats() {
    const bounds = interval || view;
    const data = core.stats(originals, el("statsChannel").value, el("quantity").value, bounds?.[0], bounds?.[1]);
    el("sensorInterval").textContent = !originals.length ? "No imported evidence" :
      interval ? `Operator-selected [${interval[0]}, ${interval[1]}] μs` : "Entire visible interval (derived statistics)";
    const values = {
      "Retained samples": data.count, "Valid for metric": data.valid_count, "Invalid / missing": data.invalid_count,
      "Observed duration (s)": data.duration_us === null ? null : data.duration_us / 1e6,
      "Minimum": data.min, "Maximum": data.max, "Mean": data.mean, "Standard deviation": data.stddev,
      "Peak-to-peak": data.peak_to_peak, "Median cadence (s)": data.median_interval_us === null ? null : data.median_interval_us / 1e6,
      "Largest adjacent Δ": data.largest_adjacent_change, "Irregular gaps": data.irregular_intervals.length,
      "Timestamp ties": data.tied_intervals
    };
    const holder = el("sensorStats"); holder.replaceChildren();
    Object.entries(values).forEach(([name, value]) => {
      const item = document.createElement("div"), label = document.createElement("span"), strong = document.createElement("strong");
      label.textContent = name; strong.textContent = typeof value === "number" ? fmt(value) : "insufficient evidence";
      item.append(label, strong); holder.append(item);
    });
  }
  function plot() {
    const chart = el("sensorPlot"); chart.replaceChildren();
    if (!view || !originals.length) { chart.append(svg("text", {x:24, y:36}, "Import a CSV to plot actual measurements.")); return; }
    const left = 72, right = 876, top = 30, bottom = 283;
    const channels = ["ptc", "chamber"].filter(c => el(c === "ptc" ? "showPtc" : "showChamber").checked);
    const metric = el("quantity").value;
    const visible = originals.filter(s => channels.includes(s.channel) && s.timestamp_us >= view[0] && s.timestamp_us <= view[1]);
    const measured = visible.filter(s => s.status === "ok" && s[metric] !== null && s[metric] >= (metric === "temperature_c" ? -Infinity : 0));
    let lo = Math.min(...measured.map(s => s[metric])), hi = Math.max(...measured.map(s => s[metric]));
    if (!Number.isFinite(lo)) { lo = 0; hi = 1; }
    const pad = (hi - lo || 1) * 0.1; lo -= pad; hi += pad;
    const x = t => left + (t - view[0]) / Math.max(1, view[1] - view[0]) * (right - left);
    const y = v => bottom - (v - lo) / (hi - lo) * (bottom - top);
    for (let n = 0; n <= 4; n++) {
      const yy = top + (bottom - top) * n / 4;
      chart.append(svg("line", {x1:left, x2:right, y1:yy, y2:yy, class:"axis"}));
      chart.append(svg("text", {x:6, y:yy + 4}, fmt(hi - (hi - lo) * n / 4, 2)));
    }
    chart.append(svg("text", {x:left, y:315}, `${view[0]} μs (device monotonic)`));
    chart.append(svg("text", {x:right, y:315, "text-anchor":"end"}, `${view[1]} μs`));
    for (const channel of channels) {
      const channelPoints = visible.filter(s => s.channel === channel).sort((a,b) => a.timestamp_us - b.timestamp_us || a.row - b.row);
      let segment = [];
      function flush() {
        if (segment.length > 1) chart.append(svg("polyline", {points:segment.join(" "), class:`trace ${channel}`}));
        segment = [];
      }
      channelPoints.forEach(s => {
        const ok = s.status === "ok" && s[metric] !== null && s[metric] >= (metric === "temperature_c" ? -Infinity : 0);
        if (!ok) { flush(); return; }
        segment.push(`${x(s.timestamp_us)},${y(s[metric])}`);
      });
      flush();
      channelPoints.forEach(s => {
        const ok = s.status === "ok" && s[metric] !== null && s[metric] >= (metric === "temperature_c" ? -Infinity : 0);
        const xx = x(s.timestamp_us), yy = ok ? y(s[metric]) : bottom - 3;
        const p = ok ? svg("circle", {cx:xx,cy:yy,r:selected === s ? 6 : 3,class:`sample ${channel}`,tabindex:0,role:"button",
          "aria-label":`Row ${s.row}, ${channel}, ${metric}: ${s[metric]}`}) :
          svg("path", {d:`M${xx-4} ${yy-4}l8 8m-8 0l8 -8`,class:"invalid",role:"button",tabindex:0,
            "aria-label":`Row ${s.row}, invalid ${s.status}`});
        p.addEventListener("click", event => { event.stopPropagation(); inspect(s); });
        p.addEventListener("keydown", event => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); inspect(s); } });
        p.append(svg("title", {}, `Row ${s.row}: ${s.channel}, t=${s.timestamp_us} μs, ${metric}=${s[metric] ?? "blank"}, status=${s.status}`));
        chart.append(p);
      });
    }
    if (interval) {
      chart.append(svg("rect", {x:x(Math.max(view[0],interval[0])), y:top,
        width:Math.max(0,x(Math.min(view[1],interval[1]))-x(Math.max(view[0],interval[0]))),
        height:bottom-top, fill:"var(--primary)", opacity:.12, "pointer-events":"none"}));
    }
    if (selected && selected.timestamp_us >= view[0] && selected.timestamp_us <= view[1])
      chart.append(svg("line",{x1:x(selected.timestamp_us),x2:x(selected.timestamp_us),y1:top,y2:bottom,class:"marker","pointer-events":"none"}));
    updateStats();
  }
  function inspect(s) { selected = s; showDetail(); list(); plot(); }
  function list() {
    const tbody = el("sensorRows"); tbody.replaceChildren();
    const metric = el("quantity").value;
    originals.filter(s => s.timestamp_us >= (view?.[0] ?? -Infinity) && s.timestamp_us <= (view?.[1] ?? Infinity))
      .forEach(s => {
        const tr = document.createElement("tr");
        [s.timestamp_us, s.channel, s[metric] ?? "blank", s.status].forEach(value => {
          const td = document.createElement("td"); td.textContent = String(value); tr.append(td);
        });
        const td = document.createElement("td"), button = document.createElement("button");
        button.textContent = "Select"; button.type = "button"; button.setAttribute("aria-current", String(s === selected));
        button.addEventListener("click", () => inspect(s)); td.append(button); tr.append(td); tbody.append(tr);
      });
  }
  function redraw() { list(); plot(); }
  function adjust(scale, direction = 0) {
    if (!view) return;
    const width = Math.max(1, view[1] - view[0]), full = Math.max(1, extent[1] - extent[0]);
    const nextWidth = Math.min(full, Math.max(1, Math.round(width * scale)));
    let center = (view[0] + view[1]) / 2 + direction * width * 0.22;
    center = Math.min(extent[1] - nextWidth / 2, Math.max(extent[0] + nextWidth / 2, center));
    view = [Math.round(center - nextWidth / 2), Math.round(center + nextWidth / 2)];
    interval = null; redraw();
  }
  function localX(event) { const r = el("sensorPlot").getBoundingClientRect(); return (event.clientX - r.left) / r.width * 900; }
  function timeX(event) { return Math.round(view[0] + (Math.max(72,Math.min(876,localX(event))) - 72) / 804 * (view[1]-view[0])); }
  el("sensorPlot").addEventListener("pointerdown", event => { if (!view || event.target.closest(".sample")) return; drag = timeX(event); });
  el("sensorPlot").addEventListener("pointerup", event => {
    if (drag === null) return;
    const end = timeX(event);
    if (Math.abs(end - drag) > 0) interval = [Math.min(drag,end),Math.max(drag,end)];
    drag = null; plot();
  });
  el("sensorPlot").addEventListener("wheel", event => { if (!view) return; event.preventDefault(); adjust(event.deltaY < 0 ? .75 : 1.33); }, {passive:false});
  el("sensorFile").addEventListener("change", async event => {
    originals = Object.freeze([]); source = selected = a = b = extent = view = interval = null;
    redraw(); showDetail(); el("exportAnalysis").disabled = true;
    const file = event.target.files?.[0]; if (!file) { message("No file selected."); return; }
    if (file.size > core.MAX_BYTES) { message("CSV exceeds the 128 KiB limit.", true); return; }
    try {
      const bytes = await file.arrayBuffer();
      const text = new TextDecoder("utf-8", {fatal:true}).decode(bytes);
      const parsed = core.parse(text);
      originals = parsed; source = {name:file.name, size_bytes:file.size};
      if (parsed.length) {
        extent = [parsed[0].timestamp_us,parsed[parsed.length-1].timestamp_us];
        view = [...extent]; selected = parsed[0]; el("exportAnalysis").disabled = false;
      }
      message(`Imported ${parsed.length} retained measurements; ${parsed.length ? "ready to inspect" : "header-only empty capture"}.`);
      el("sensorProvenance").textContent = `Imported evidence: ${file.name} (${file.size} bytes). Original CSV remains outside DragonSniff storage; this is a session-only local copy.`;
      showDetail(); redraw();
    } catch (error) { message(`Import rejected: ${error.message}`, true); }
  });
  ["quantity","showPtc","showChamber","statsChannel"].forEach(id =>
    el(id).addEventListener("change", redraw));
  el("resetView").addEventListener("click", () => { if (extent) { view = [...extent]; interval = null; redraw(); } });
  el("zoomIn").addEventListener("click", () => adjust(.5));
  el("zoomOut").addEventListener("click", () => adjust(2));
  el("panLeft").addEventListener("click", () => adjust(1,-1));
  el("panRight").addEventListener("click", () => adjust(1,1));
  el("clearInterval").addEventListener("click", () => { interval = null; plot(); });
  el("markA").addEventListener("click", () => { a = selected; showDetail(); });
  el("markB").addEventListener("click", () => { b = selected; showDetail(); });
  el("exportAnalysis").addEventListener("click", () => {
    if (!source) return;
    const bounds = interval || view;
    const report = {type:"Sensor Lab derived analysis", provenance:"Imported device measurements, local browser computation",
      source, source_schema:core.HEADERS, selected_rows:{a:a?.row ?? null,b:b?.row ?? null},
      operator_interval_us:interval, plotted_view_us:view, metric:el("quantity").value,
      stats:["ptc","chamber"].map(channel => core.stats(originals,channel,el("quantity").value,bounds[0],bounds[1])),
      comparison:core.compare(a,b), note:"Reported temperature and rate are not physical safety verification. No original evidence modified."};
    const url = URL.createObjectURL(new Blob([JSON.stringify(report,null,2)+"\n"],{type:"application/json"}));
    const link = document.createElement("a"); link.href = url; link.download = "dragonsniff-sensor-lab-analysis.json"; link.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  });
  redraw(); showDetail();
})();
