import { connection } from "next/server";

import StaffWorkspace from "./staff-workspace";

// Nonce-based CSP can only be applied while serving a request. This prevents a
// build-time static render from producing scripts without the request nonce.
export default async function Page() {
  await connection();
  return <StaffWorkspace />;
}
