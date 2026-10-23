# Operator console

The web console for the harness: the agent, the approval queue, the ledger, eval results and the architecture. Next.js
16 with React server components; every call to the harness API is made from the Next.js server, so the browser never
sees the API's address or key.

```
cp .env.example .env.local   # HARNESS_API_URL, and HARNESS_API_KEY if the API has one
npm install
npm run dev                  # http://localhost:3001 (Grafana has :3000)
```

The API must be running (see the main README). The merchant shown is chosen in the header; there is no sign-in, so
decisions made here are recorded under one reviewer name (`src/lib/api.ts`).
