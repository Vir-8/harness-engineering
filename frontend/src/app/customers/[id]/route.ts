import type { NextRequest } from "next/server";

import { ApiError, api } from "@/lib/api";

// A read for the customer popover. A Route Handler, not a Server Action: actions are for changes, and the client runs
// them one at a time. The harness API and its key stay on the server, as with every other call.
export async function GET(_req: NextRequest, ctx: RouteContext<"/customers/[id]">) {
  const { id } = await ctx.params;
  if (id.length < 1 || id.length > 64) return Response.json({ error: "Unknown customer." }, { status: 404 });
  try {
    return Response.json(await api.customer(id));
  } catch (error) {
    const status = error instanceof ApiError ? error.status : 502;
    const message = error instanceof ApiError ? error.message : "The harness could not be reached.";
    return Response.json({ error: message }, { status });
  }
}
