export function needsExecutionReview(status: string, nodeStates: Record<string, string>): boolean {
  // A lost dispatch response can leave READY nodes inside an UNKNOWN run.
  // Those nodes are not evidence that the external request never executed.
  return status === "UNKNOWN" || Object.values(nodeStates).some((value) => value === "UNKNOWN" || value === "RUNNING");
}
