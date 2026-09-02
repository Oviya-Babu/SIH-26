import { NextResponse, type NextRequest } from "next/server";

const apiOrigin = new URL(
  process.env.NEXT_PUBLIC_API_ORIGIN ?? "http://localhost:8000",
).origin;
const oidcOrigin = new URL(
  process.env.NEXT_PUBLIC_OIDC_ISSUER ?? "http://localhost:8080/realms/medikiosk",
).origin;

export function proxy(request: NextRequest) {
  const nonce = crypto.randomUUID().replace(/-/g, "");
  const requestHeaders = new Headers(request.headers);
  requestHeaders.set("x-nonce", nonce);

  const policy = [
    "default-src 'self'",
    "base-uri 'self'",
    "form-action 'self'",
    "frame-ancestors 'none'",
    "object-src 'none'",
    "font-src 'self'",
    `connect-src 'self' ${apiOrigin} ${oidcOrigin}`,
    "img-src 'self' data:",
    // style-src omits the inline-scripts exception.  React inline style props
    // are element attributes, not <style> tags, so they are allowed by the
    // browser regardless of the directive.  Including the inline exception
    // would negate nonce-based script protection against style-based injection.
    "style-src 'self'",
    `script-src 'self' 'nonce-${nonce}' 'strict-dynamic'`,
  ].join("; ");
  requestHeaders.set("Content-Security-Policy", policy);
  const response = NextResponse.next({ request: { headers: requestHeaders } });
  response.headers.set("Content-Security-Policy", policy);
  return response;
}

export const config = { matcher: ["/((?!_next/static|_next/image|favicon.ico).*)"] };
