export async function request<T>(path: string, token = '', init: RequestInit = {}): Promise<T> {
  const response = await fetch(path, { ...init, signal: init.signal ? AbortSignal.any([init.signal, AbortSignal.timeout(15000)]) : AbortSignal.timeout(15000), headers: { 'Content-Type': 'application/json', ...(token ? { Authorization: `Bearer ${token}` } : {}), ...init.headers } });
  const body = await response.json();
  if (!response.ok) throw Object.assign(new Error(typeof body.detail === 'string' ? body.detail : 'Request failed. Please try again.'), { status: response.status });
  return body;
}
