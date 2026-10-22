// Load test for POST /refund-agent/ask: more and more simulated users, each asking again as soon as it gets an answer.
//
// Run the API with the stand-in model (no model is called, nothing is spent), then k6 from Docker:
//   LLM_PROVIDER=fake TRACING=off ASK_RATE_LIMIT=1000000 uv run uvicorn refund_harness.app:app --port 8000
//   docker run --rm -i --add-host=host.docker.internal:host-gateway grafana/k6:2.3.0 run - < load/ask.js
//   (add -e PEAK=1500 after `run` to ramp to 1,500 users instead of 500)
// and watch http://localhost:3000 while it runs. BASE_URL (-e BASE_URL=...) points it elsewhere.
//
// The stand-in model waits FAKE_LLM_DELAY_S (1 s) per call and a run makes 2 calls, so ~2 s is the floor for one ask.
// Anything above that is OUR harness: HTTP, Redis (rate limit), Postgres (the order lookup), the agent loop.
import http from 'k6/http';
import { check } from 'k6';

const BASE_URL = __ENV.BASE_URL || 'http://host.docker.internal:8000';
const MERCHANTS = ['acme', 'globex'];
const PEAK = Number(__ENV.PEAK || 500);   // most simultaneous users at the end of the ramp (-e PEAK=1500 to push harder)

export const options = {
  scenarios: {
    ramp: {
      executor: 'ramping-vus',   // VU = virtual user: one loop of request, wait for answer, repeat
      startVUs: 0,
      stages: [
        { duration: '30s', target: Math.round(PEAK * 0.05) },
        { duration: '30s', target: Math.round(PEAK * 0.2) },
        { duration: '30s', target: Math.round(PEAK * 0.5) },
        { duration: '30s', target: PEAK },
        { duration: '15s', target: 0 },
      ],
    },
  },
  // Pass/fail lines for the run as a whole: k6 exits non-zero if one is crossed (usable in CI).
  thresholds: {
    http_req_failed: ['rate<0.01'],      // under 1% errors
    http_req_duration: ['p(95)<3000'],   // 95% of asks within 3 s (the floor is ~2 s)
  },
};

export default function () {
  const merchant = MERCHANTS[__VU % MERCHANTS.length];
  const response = http.post(
    `${BASE_URL}/refund-agent/ask`,
    JSON.stringify({ question: 'Can I get a refund for A100?' }),
    { headers: { 'Content-Type': 'application/json', 'X-Merchant-Id': merchant }, timeout: '60s' },
  );
  check(response, { 'answered (200)': (r) => r.status === 200 });
}
