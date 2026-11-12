"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const core = require("../src/dragonsniff/web/sensor-lab-core.js");
const H = core.HEADERS.join(",") + "\n";
const row = (t, channel="ptc", status="ok", raw="1000", mv="900", temp="30.5", source="1") =>
  [t,channel,source,raw,mv,temp,status,"82"].join(",") + "\n";
test("valid CSV, provenance and channel identities survive parsing", () => {
  const s=core.parse(H+row(100,"chamber","ok","2048","1200","24.5","0")+row(100)+row(600000));
  assert.equal(s.length,3); assert.equal(s[0].timestamp_us,100);
  assert.equal(s[0].channel,"chamber"); assert.equal(s[0].source_index,0);
  assert.equal(s[1].source_index,1); assert.equal(s[1].row,3);
  assert.ok(Object.isFrozen(s)); assert.ok(Object.isFrozen(s[0]));
});
test("malformed and extra headers rejected", () => {
  assert.throws(()=>core.parse("timestamp_us,channel\n"),/header/);
  assert.throws(()=>core.parse(H.replace("raw","value")) ,/header/);
  assert.throws(()=>core.parse(H.replace("\n",",other\n")) ,/header/);
});
test("empty files rejected; header-only valid",()=>{
  assert.throws(()=>core.parse(""),/Empty/); assert.deepEqual(core.parse(H),[]);
});
test("invalid states and blank temperature retained",()=>{
  const s=core.parse(H+row(0,"ptc","open","-1","-1","")+row(5,"chamber","short","-1","-1","","0")+
    row(10,"ptc","uninit","-1","-1",""));
  assert.equal(s[0].temperature_c,null);
  assert.equal(s[0].raw,-1);
  assert.equal(core.stats(s,"ptc").invalid_count,2);
  assert.equal(core.graphPoints(s,"ptc","raw",0,20)[0].value,null);
});
test("malformed rows and inconsistent status rejected",()=>{
  for (const text of [
    H+"1,ptc\n", H+row(1,"ptc","ok","-1"), H+row(1,"ptc","open","-1","-1","20"),
    H+row(1,"ptc","ok","4096"), H+row(1,"ptc","bad"),
    H+row(1,"surprise"),H+row(1,"ptc","ok","-2"),
    H+row(1,"ptc","ok","1000","-1"),
    H+row(1,"ptc","ok","1000","900","not-number"),
    H+row(1,"ptc","ok","1000","900","30","3"),
    H+'1,"ptc"extra,1,1000,900,30,ok,82\n',
    H+'1,"ptc,1,1000,900,30,ok,82\n'
  ]) assert.throws(()=>core.parse(text));
});
test("max records and max bytes enforced",()=>{
  assert.equal(core.parse(H+row(1).repeat(256)).length,256);
  assert.throws(()=>core.parse(H+row(1).repeat(257)),/Too many/);
  assert.throws(()=>core.parse(H+" ".repeat(core.MAX_BYTES)),/128 KiB/);
});
test("ties accepted, descending timestamps rejected",()=>{
  assert.equal(core.parse(H+row(2)+row(2)).length,2);
  assert.throws(()=>core.parse(H+row(2)+row(1)),/monotonic/);
});
test("ring rollover remains observed-only",()=>{
  const s=core.parse(H+row(1000)+row(1500)+row(2000));
  assert.equal(core.stats(s,"ptc").count,3);
  assert.equal(core.stats(s,"ptc").duration_us,1000);
});
test("population statistics, interval and adjacent delta",()=>{
  const s=core.parse(H+row(0,"ptc","ok","10","10","10")+row(500000,"ptc","ok","20","20","20")+
    row(1000000,"ptc","ok","30","30","30")+row(3000000,"ptc","open","-1","-1",""));
  const n=core.stats(s,"ptc","raw");
  assert.equal(n.count,4); assert.equal(n.valid_count,3); assert.equal(n.invalid_count,1);
  assert.equal(n.min,10); assert.equal(n.max,30); assert.equal(n.mean,20);
  assert.equal(n.peak_to_peak,20);assert.equal(n.largest_adjacent_change,10);
  assert.ok(Math.abs(n.stddev-Math.sqrt(200/3))<1e-9);
  assert.equal(n.duration_us,3000000);assert.equal(n.median_interval_us,500000);
  assert.equal(n.irregular_intervals.length,1);
  assert.equal(core.stats(s,"ptc","raw",500000,1000000).count,2);
});
test("insufficient stats and missing measurements",()=>{
  assert.equal(core.stats([],"ptc").mean,null);
  const n=core.stats(core.parse(H+row(0)),"ptc");
  assert.equal(n.stddev,null);assert.equal(n.duration_us,null);
});
test("A/B comparison is signed and does not imply physical rate",()=>{
  const s=core.parse(H+row(0,"ptc","ok","1","10","20")+row(2000000,"ptc","ok","4","22","28"));
  const d=core.compare(s[0],s[1]); assert.equal(d.elapsed_us,2000000);
  assert.equal(d.raw_difference,3);assert.equal(d.millivolts_difference,12);
  assert.equal(d.temperature_difference_c,8);assert.equal(d.apparent_rate_c_per_s,4);
  assert.equal(core.compare(s[0],s[0]).apparent_rate_c_per_s,null);
});
test("graph preparation never interpolates; no original mutation",()=>{
  const s=core.parse(H+row(0)+row(500,"ptc","open","-1","-1","")+row(1000));
  const points=core.graphPoints(s,"ptc","temperature_c",0,1000);
  assert.deepEqual(points.map(p=>p.timestamp_us),[0,500,1000]);
  assert.deepEqual(points.map(p=>p.value),[30.5,null,30.5]);
  points[0].value=999; assert.equal(s[0].temperature_c,30.5);
});
test("UTF-8 BOM and CRLF accepted without rewriting evidence",()=>{
  const s=core.parse("\ufeff"+H.replace("\n","\r\n")+row(0).replace("\n","\r\n"));
  assert.equal(s.length,1);
});

test("classified fault excursions remain available in ADC and voltage domains", () => {
  const s = core.parse(H + row(0,"ptc","ok","10","10","20") +
    row(500000,"ptc","open","4095","3134","") +
    row(1000000,"ptc","ok","90","90","22"));
  assert.deepEqual(core.graphPoints(s,"ptc","raw",0,1000000).map(p => p.value), [10,4095,90]);
  assert.equal(core.stats(s,"ptc","raw").max,4095);
  assert.equal(core.stats(s,"ptc","raw").peak_to_peak,4085);
  assert.equal(core.stats(s,"ptc","millivolts").max,3134);
  assert.equal(core.stats(s,"ptc","temperature_c").valid_count,2);
  assert.equal(core.stats(s,"ptc","temperature_c").invalid_count,1);
  assert.equal(core.graphPoints(s,"ptc","temperature_c",0,1000000)[1].value,null);
});
test("non-OK invalid ADC values are excluded but original state survives", () => {
  const s=core.parse(H+row(0,"ptc","open","-1","-1","")+
    row(500000,"ptc","short","4095","3134",""));
  assert.equal(core.stats(s,"ptc","raw").valid_count,1);
  assert.equal(core.stats(s,"ptc","raw").invalid_count,1);
  assert.equal(s[0].status,"open");
  assert.equal(s[1].status,"short");
});
test("cross-channel bookmarks do not produce physically meaningless rates", () => {
  const s=core.parse(H+row(0,"ptc","ok","10","10","20")+
    row(35,"chamber","ok","12","15","21","0"));
  const result=core.compare(s[0],s[1]);
  assert.equal(result.cross_channel,true);
  assert.equal(result.elapsed_us,35);
  assert.equal(result.temperature_difference_c,null);
  assert.equal(result.apparent_rate_c_per_s,null);
});
test("same-channel sub-millisecond comparisons retain microsecond differences",()=>{
  const s=core.parse(H+row(1000000)+row(1000035));
  assert.equal(core.compare(s[0],s[1]).elapsed_us,35);
});
test("timestamp ties do not lower the positive cadence median",()=>{
  const s=core.parse(H+row(0)+row(0)+row(500000)+row(1000000));
  assert.equal(core.stats(s,"ptc").median_interval_us,500000);
  assert.equal(core.stats(s,"ptc").tied_intervals,1);
});
test("parser accepts firmware OK rows lacking a converted temperature",()=>{
  const s=core.parse(H+row(100,"ptc","ok","1000","900",""));
  assert.equal(s[0].temperature_c,null);
  assert.equal(core.stats(s,"ptc","raw").valid_count,1);
  assert.equal(core.stats(s,"ptc","temperature_c").valid_count,0);
});
test("canonical integer forms required; blank trailing lines ignored",()=>{
  for(const bad of [row("1.0"),row(" 1"),row("1","ptc","ok","1000.0")])
    assert.throws(()=>core.parse(H+bad),/invalid/);
  assert.equal(core.parse(H+row(0)+"\n\r\n").length,1);
});
test("only retained ring records contribute, no invented predecessors",()=>{
  const times=Array.from({length:256},(_,i)=>5000000+i*500000);
  const s=core.parse(H+times.map(t=>row(t)).join(""));
  assert.equal(s[0].timestamp_us,5000000);
  assert.equal(s[s.length-1].timestamp_us,132500000);
  assert.equal(core.stats(s,"ptc").count,256);
  assert.equal(core.graphPoints(s,"ptc","raw",0,999999999).length,256);
});
