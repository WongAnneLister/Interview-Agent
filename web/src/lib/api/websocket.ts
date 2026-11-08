export function textWebSocketUrl(baseUrl: string, id: string): string {
  return `${baseUrl.replace(/^http/, "ws").replace(/\/$/, "")}/ws/interviews/${encodeURIComponent(id)}/text`;
}
