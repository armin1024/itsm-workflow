declare global {
  interface Window {
    __ITSM_WORKFLOW_CONFIG__?: { basePath?: string }
  }
}

export const basePath = (window.__ITSM_WORKFLOW_CONFIG__?.basePath || '').replace(/\/$/, '')

export function withBase(path: string): string {
  return `${basePath}${path.startsWith('/') ? path : `/${path}`}`
}
