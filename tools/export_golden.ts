/**
 * Golden values from the app's TypeScript engine for tests/test_engine.py (parity check).
 *   npx tsx tools/export_golden.ts
 */
import { writeFileSync, mkdirSync } from 'fs';
import { exposures, lookAhead, groundSpeed, flightLevel, timeForDistance, START_NM, HAZARDS, hazardCentre } from '../../CabinShield-Frontend/src/domain/flight';
import { posAt, ROUTE_NM } from '../../CabinShield-Frontend/src/domain/geo';
import { planServices } from '../../CabinShield-Frontend/src/domain/planner';
const t0 = timeForDistance(START_NM);
const ex = exposures(START_NM, t0).map(e => ({ hazard: e.h.idx, t0: e.t0, t1: e.t1, d0: e.d0, d1: e.d1, max: e.max }));
const eta = t0 + ((ROUTE_NM - START_NM) / 492) * 3600;
const plan = planServices(0, eta, exposures(START_NM, t0), null).map(p => ({ id: p.id, start: p.start, end: p.end, mode: p.mode, shift: p.shift }));
const looks = [START_NM, 600, 900, 1200, 2300, 3900].map(d => { const t = timeForDistance(d); const g = groundSpeed(d, t); const L = lookAhead(d, t, g); return { d, t, gs: g, look: L ? { cur: L.cur, first: L.first, max: L.max, exit: L.exit, hazard: L.h.idx } : null }; });
const pts = [0, 100, 1000, 2500, 4000, ROUTE_NM].map(d => ({ d, pos: posAt(d), fl: flightLevel(d) }));
const hc = HAZARDS.map(h => ({ c0: h.c0, tRef: h.tRef, at: hazardCentre(h, h.tRef + 3600) }));
mkdirSync('./tests/fixtures', { recursive: true });
writeFileSync('./tests/fixtures/golden_ts.json', JSON.stringify({ route_nm: ROUTE_NM, t0, eta, exposures: ex, plan, looks, pts, hazards: hc }, null, 1));
console.log("golden written");
