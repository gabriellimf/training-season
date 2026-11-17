import { TEXT } from "./constants.js";

export class ApiFailure extends Error {
  constructor(kind, message) {
    super(message);
    this.kind = kind;
  }
}

export function requireApiFailure(error) {
  if (!(error instanceof ApiFailure)) throw error;
}

// The server guard answers 403, 404, 405, 411, 413 and 415 with status "invalid".
function failureOf(statusCode, payload) {
  const message = payload && typeof payload.message === "string" ? payload.message : null;
  if (statusCode === 409) {
    return new ApiFailure(payload?.status === "busy" ? "busy" : "conflict", message ?? TEXT.serverError);
  }
  if (statusCode === 422 || payload?.status === "invalid") return new ApiFailure("invalid", message ?? TEXT.invalid);
  return new ApiFailure("server", message ?? TEXT.serverError);
}

// The server refuses a write without a JSON body, so a route with no input gets {}.
async function apiCall(method, path, body) {
  const headers = { "Content-Type": "application/json", Accept: "application/json" };
  const init = { method, credentials: "same-origin", headers };
  if (method !== "GET") init.body = JSON.stringify(body ?? {});
  let response;
  try {
    response = await fetch(path, init);
  } catch {
    console.warn("api", "network", 0);
    throw new ApiFailure("network", TEXT.noResponse);
  }
  const payload = await response.json().catch(() => null);
  if (response.ok) return payload;
  const failure = failureOf(response.status, payload);
  console.warn("api", failure.kind, response.status);
  throw failure;
}

export const apiGet = (path) => apiCall("GET", path);
export const apiPost = (path, body) => apiCall("POST", path, body);
