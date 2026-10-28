import type { LocalRun } from "./browserStore";

export function applyRunEvent(run: LocalRun, event: Record<string, unknown>): LocalRun {
  const snapshot = event.snapshot as Record<string, unknown> | undefined;
  const { snapshot: _snapshot, result: _result, interaction: _interaction, ...safeEvent } = event;
  const next = { ...run, lastEventId: Number(event.eventId), updatedAt: new Date().toISOString(), events: [...run.events, safeEvent].slice(-300) };
  if (snapshot) {
    const fields = ["status", "nodeStates", "nodeOutputs", "selectedRoutes", "currentNodeId", "interaction", "diagnostic", "nodeResults"] as const;
    for (const field of fields) if (field in snapshot) Object.assign(next, { [field]: snapshot[field] });
    if (snapshot.nodeStatuses) next.nodeStates = snapshot.nodeStatuses as Record<string, string>;
  } else if (event.type === "NODE_STARTED") {
    next.status = "RUNNING"; next.currentNodeId = String(event.nodeId);
    next.nodeStates = { ...run.nodeStates, [String(event.nodeId)]: "RUNNING" };
    next.nodeResults = { ...run.nodeResults, [String(event.nodeId)]: { attemptId: event.attemptId, status: "RUNNING", startedAt: event.startedAt } };
  } else if (event.type === "NODE_COMPLETED") {
    const nodeId = String(event.nodeId);
    next.nodeStates = { ...run.nodeStates, [nodeId]: String(event.status) };
    next.nodeResults = { ...run.nodeResults, [nodeId]: { ...run.nodeResults?.[nodeId], attemptId: event.attemptId, status: event.status, diagnostic: event.diagnostic } };
    if (event.result != null) next.nodeOutputs = { ...run.nodeOutputs, [nodeId]: event.result };
    if (event.interaction) next.interaction = event.interaction as Record<string, unknown>;
    if (event.diagnostic) next.diagnostic = event.diagnostic;
    if (event.selectedEdgeId) next.selectedRoutes = { ...run.selectedRoutes, [nodeId]: String(event.selectedEdgeId) };
    for (const id of event.skippedNodeIds as string[] || []) next.nodeStates[id] = "SKIPPED";
  } else if (event.type === "RUN_RELEASED") {
    next.status = String(event.status); next.currentNodeId = String(event.currentNodeId || run.currentNodeId);
  }
  return next;
}

export const pollDelay = (hidden: boolean) => hidden ? 5000 : 2000;
export async function mapWithConcurrency<T>(items: T[], limit: number, apply: (item: T) => Promise<void>): Promise<void> {
  let next = 0;
  await Promise.all(Array.from({ length: Math.min(limit, items.length) }, async () => {
    while (next < items.length) { const index = next++; await apply(items[index]); }
  }));
}
