import { NextResponse } from "next/server";

export const dynamic = "force-dynamic";

/** Container healthcheck target. Deliberately checks nothing but this process:
 *  the API's availability is reported by the API's own probe. */
export function GET() {
  return NextResponse.json({ status: "alive", service: "frontend" });
}
