import { auth } from './firebase';

export class ApiError extends Error {
  constructor(
    public status: number,
    public code: string,
    message: string,
    public details?: unknown,
  ) {
    super(message);
  }
}

async function authHeader(): Promise<Record<string, string>> {
  const user = auth.currentUser;
  if (!user) throw new ApiError(401, 'unauthenticated', 'Sign in to continue');
  return { Authorization: `Bearer ${await user.getIdToken()}` };
}

async function parse(res: Response): Promise<unknown> {
  if (res.status === 204) return null;
  const type = res.headers.get('content-type') || '';
  const body = type.includes('application/json') ? await res.json() : await res.text();
  if (!res.ok) {
    const err = (body as { error?: { code: string; message: string; details?: unknown } })?.error;
    throw new ApiError(res.status, err?.code || 'http_error', err?.message || `Request failed (${res.status})`, err?.details);
  }
  return body;
}

export async function api<T>(path: string, init: RequestInit & { json?: unknown } = {}): Promise<T> {
  const headers: Record<string, string> = { ...(await authHeader()) };
  let body = init.body;
  if (init.json !== undefined) {
    headers['Content-Type'] = 'application/json';
    body = JSON.stringify(init.json);
  }
  let res = await fetch(`/api${path}`, { ...init, body, headers: { ...headers, ...(init.headers as Record<string, string>) } });
  if (res.status === 401 && auth.currentUser) {
    // Token may have just expired: refresh once.
    const token = await auth.currentUser.getIdToken(true);
    res = await fetch(`/api${path}`, { ...init, body, headers: { ...headers, Authorization: `Bearer ${token}` } });
  }
  return (await parse(res)) as T;
}

export const get = <T>(path: string) => api<T>(path);
export const post = <T>(path: string, json?: unknown) => api<T>(path, { method: 'POST', json: json ?? {} });
export const put = <T>(path: string, json: unknown) => api<T>(path, { method: 'PUT', json });
export const patch = <T>(path: string, json: unknown) => api<T>(path, { method: 'PATCH', json });
export const del = <T>(path: string) => api<T>(path, { method: 'DELETE' });

export interface StreamEvent {
  type: 'delta' | 'reset' | 'final' | 'error';
  text?: string;
  message?: string;
  answer?: string;
  citations?: import('./types').Citation[];
  scope?: string;
  grounded?: boolean;
  qaId?: string;
}

/** POST with a server-sent events response (used by /ask). */
export async function stream(path: string, json: unknown, onEvent: (e: StreamEvent) => void, signal?: AbortSignal) {
  const headers = { ...(await authHeader()), 'Content-Type': 'application/json' };
  const res = await fetch(`/api${path}`, { method: 'POST', headers, body: JSON.stringify(json), signal });
  if (!res.ok || !res.body) {
    await parse(res);
    return;
  }
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buf = '';
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buf += decoder.decode(value, { stream: true });
    let idx;
    while ((idx = buf.indexOf('\n\n')) >= 0) {
      const chunk = buf.slice(0, idx);
      buf = buf.slice(idx + 2);
      for (const line of chunk.split('\n')) {
        if (line.startsWith('data: ')) onEvent(JSON.parse(line.slice(6)) as StreamEvent);
      }
    }
  }
}

export async function upload<T>(path: string, form: FormData): Promise<T> {
  const res = await fetch(`/api${path}`, { method: 'POST', headers: await authHeader(), body: form });
  return (await parse(res)) as T;
}

export async function download(path: string, filename: string) {
  const res = await fetch(`/api${path}`, { headers: await authHeader() });
  if (!res.ok) await parse(res);
  const blob = await res.blob();
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = filename;
  a.click();
  URL.revokeObjectURL(a.href);
}
