// Vercel Cron (see vercel.json "crons"): asks the bot's server to start a run for the owner's account,
// the same as "Run now" (POST /cycles). The server skips it when a run is already waiting or going.
// Environment variables on the Vercel project: CRON_SECRET (Vercel sends it), JOBBOT_BACKEND_URL (the admin
// site of the server, e.g. https://admin.1-2-3-4.sslip.io) and JOBBOT_API_KEY (an API key of the owner).
export async function GET(request) {
  if (request.headers.get("authorization") !== `Bearer ${process.env.CRON_SECRET}`) {
    return Response.json({ error: "not allowed" }, { status: 401 });
  }
  const backend = (process.env.JOBBOT_BACKEND_URL || "").replace(/\/+$/, "");
  if (!backend || !process.env.JOBBOT_API_KEY) {
    return Response.json({ error: "JOBBOT_BACKEND_URL or JOBBOT_API_KEY is not set" }, { status: 500 });
  }
  try {
    const res = await fetch(`${backend}/cycles`, {
      method: "POST",
      headers: { "X-API-Key": process.env.JOBBOT_API_KEY, "Content-Type": "application/json" },
      body: "{}",
      signal: AbortSignal.timeout(30000),
    });
    const answer = await res.json().catch(() => ({}));
    console.log("run requested", res.status, JSON.stringify(answer));
    return Response.json({ status: res.status, answer }, { status: res.ok ? 200 : 502 });
  } catch (e) {
    console.error("server not reached", String(e));
    return Response.json({ error: "the bot's server did not answer", detail: String(e) }, { status: 502 });
  }
}
