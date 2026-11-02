// k6 load test for rec-serve. Run against a real artifact, over HTTP:
//   k6 run -e BASE_URL=http://localhost:8080 -e APP=demo tests/load/rec.js
// Stages follow the design (1x, 3x, 10x of the expected peak); tune RATE to it.
import http from "k6/http";
import { check } from "k6";
import { Rate } from "k6/metrics";

const BASE = __ENV.BASE_URL || "http://localhost:8080";
const APP = __ENV.APP || "demo";
const RATE = Number(__ENV.RATE || 90); // requests per second at "1x"
const SHORT = __ENV.SHORT === "1"; // 5-second stages, for smoke runs
const STAGE = SHORT ? "5s" : null;
const degraded = new Rate("rec_degraded_or_overload");

export const options = {
  scenarios: {
    ramp: {
      executor: "ramping-arrival-rate",
      startRate: RATE,
      timeUnit: "1s",
      preAllocatedVUs: 200,
      maxVUs: 2000,
      stages: [
        { target: RATE, duration: STAGE || "1m" },
        { target: RATE * 3, duration: STAGE || "2m" },
        { target: RATE * 10, duration: STAGE || "2m" },
        { target: RATE, duration: STAGE || "1m" },
      ],
    },
  },
  thresholds: {
    // G2: p95 of /v1/rec under 30 ms; overload/timeout under 1% at peak.
    http_req_duration: ["p(95)<30"],
    rec_degraded_or_overload: ["rate<0.01"],
    http_req_failed: ["rate<0.001"], // fail-open: the API must never 5xx
  },
};

export default function () {
  const recent = [];
  for (let i = 0; i < 1 + (__ITER % 10); i++) {
    recent.push(`i${String((__ITER * 7 + i) % 400).padStart(5, "0")}`);
  }
  const res = http.post(
    `${BASE}/v1/rec`,
    JSON.stringify({ app: APP, device_id: `vu-${__VU}-${__ITER}`, recent, limit: 20 }),
    { headers: { "Content-Type": "application/json", "x-country": "US" } },
  );
  const body = res.json();
  degraded.add(body.reason === "overload" || body.reason === "timeout" || !!body.degraded);
  check(res, { "200": (r) => r.status === 200, "has request_id": () => !!body.request_id });
}
