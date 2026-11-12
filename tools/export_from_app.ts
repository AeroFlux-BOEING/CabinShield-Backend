/**
 * Re-export seed data + CST weights from the crew app so both sides stay identical.
 * Run from the backend root, with CabinShield-Frontend checked out next to it:
 *   npx tsx tools/export_from_app.ts
 */
import { writeFileSync, mkdirSync } from 'fs';
import { POINTS, STAY, AFTER, HISTORY, PLACES, AIRPORTS, CREW, REST_A, REST_B, SERVICES } from '../../CabinShield-Frontend/src/domain/content';
import { SCHEDULE } from '../../CabinShield-Frontend/src/domain/schedule';
import { HAZARDS, STRENGTH } from '../../CabinShield-Frontend/src/domain/flight';
import { CST_MODEL } from '../../CabinShield-Frontend/src/ai/cstWeights';
import { PLANS } from '../../CabinShield-Frontend/src/ai/cstModel';
const out = './app/data/seed';
mkdirSync(out, { recursive: true });
const w = (f: string, o: unknown) => writeFileSync(`${out}/${f}`, JSON.stringify(o, null, 2) + '\n');
w('schedule.json', Object.entries(SCHEDULE).map(([no, f]) => ({ flight_no: no, origin: f.from, destination: f.to, destination_name: f.toName, dep_local: f.dep, block_min: f.dur, aircraft: f.aircraft, aircraft_note: f.aircraftNote, days: f.days, supported: f.supported, route_id: `${f.from}-${f.to}` })));
w('protocol.json', { points: POINTS.map(p => ({ n: p.n, lead_s: p.lead, name: p.name, tag: p.tag, todo: p.todo, short: p.short, say_label: p.sayLabel, say: p.say })), stay: { todo: STAY.todo, say_label: STAY.sayLabel, say: STAY.say }, after: { todo: AFTER.todo, say_label: AFTER.sayLabel, say: AFTER.say } });
w('history_ICN-SFO.json', HISTORY.map((h, i) => ({ id: `H${i + 1}`, d0_nm: +h.d0.toFixed(1), d1_nm: +h.d1.toFixed(1), name: h.name, cases: h.cases, fl_band: h.band, pattern: h.pattern, severity: h.sev, season: h.season })));
w('places.json', { places: PLACES.map(([n, lat, lon, city]) => ({ name: n, lat, lon, city })), airports: AIRPORTS.map(([code, n, lat, lon]) => ({ code, name: n, lat, lon })) });
w('aircraft.json', { 'B787-9': { crew: CREW.map(([pos, role]) => ({ pos, role })), rest_groups: { A: REST_A, B: REST_B }, crew_zone: [0, 0, 0, 1, 1, 1, 2, 2, 2, 2], cart_zone: [0, 1, 2, 2], lavatories: 6, galleys: 3 } });
w('services.json', SERVICES.map(s => ({ id: s.id, name: s.name, pref_s: s.pref, dur_s: s.dur, simplified_s: s.simp ?? null, optional: !!s.optional })));
w('hazards_ICN-SFO.json', { strength: STRENGTH, hazards: HAZARDS.map(h => ({ id: `HZ${h.idx + 1}`, name: h.name, fl: h.fl, f: h.f, off_nm: h.off, r_nm: h.r, drift_kt: h.kt, shape: h.shape })) });
mkdirSync('./app/data/models', { recursive: true });
writeFileSync('./app/data/models/cst_mlp_v1.json', JSON.stringify({ version: 'cst-mlp-v1', arch: [38, 32, 16, 3], activation: 'relu', scale: CST_MODEL.scale, margin: CST_MODEL.margin, plans: PLANS.map(p => ({ id: p.id, name: p.name, desc: p.desc })), W: CST_MODEL.W, b: CST_MODEL.b, test: CST_MODEL.test }) + '\n');
console.log('ok');
