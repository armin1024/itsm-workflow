export type WorkflowNode = {
  id: string;
  type: string;
  schemaVersion?: number;
  handlerVersion?: string;
  title: string;
  config: Record<string, unknown>;
  inputs: Array<{
    name: string;
    type: string;
    description?: string;
    required?: boolean;
    source: {
      kind: string;
      key?: string;
      nodeId?: string;
      jsonPointer?: string;
      value?: unknown;
    };
  }>;
  approvalPolicy: string;
  timeoutSeconds: number;
  uiPosition?: { x: number; y: number };
};

export type NodeCatalogItem = {
  type: string;
  schemaVersion: number;
  handlerVersion: string;
  name: string;
  category: string;
  description: string;
  riskLevel: string;
  approvalPolicy: string;
  idempotencyClass: string;
  resumeSemantics: string;
  supportedModes: string[];
  configSchema: Record<string, unknown>;
  inputSchema: Record<string, unknown>;
  outputSchema: Record<string, unknown>;
  uiSchema: Record<string, unknown>;
  allowSingleNodeDebug: boolean;
  studioEnabled?: boolean;
  studioUpdatedAt?: string;
};

export type WorkflowEdge = {
  id: string;
  source: string;
  target: string;
  kind?: "NORMAL" | "CONDITION" | "REFINEMENT";
  label?: string;
  default?: boolean;
  condition?: Record<string, unknown>;
  maxIterations?: number;
  feedbackInputName?: string;
};
