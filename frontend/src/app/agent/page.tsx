import type { Metadata } from "next";
import { Suspense } from "react";

import { AgentConsole } from "@/components/agent-console";
import { PageTitle } from "@/components/ui";
import { currentTenant } from "@/lib/api";
import { tenantExamples } from "@/lib/tenants";

export const metadata: Metadata = { title: "Agent" };

export default function AgentPage() {
  return (
    <>
      <PageTitle lede="Send a message as a customer. Each model call and tool call is listed with its time, tokens and cost.">Agent</PageTitle>
      <Suspense>
        <Console />
      </Suspense>
    </>
  );
}

async function Console() {
  const tenant = await currentTenant();
  // keyed by merchant: switching merchant starts a fresh conversation with that merchant's suggestions
  return <AgentConsole key={tenant} examples={tenantExamples(tenant)} />;
}
