export async function backend(action, body, role = "operator") {
  const base = process.env.FLY_DATA_URL;
  const token = role === "proposal"
    ? process.env.EXECUTION_PROPOSAL_TOKEN : process.env.EXECUTION_OPERATOR_TOKEN;
  if (!base || !token || new URL(base).protocol !== "https:") {
    throw new Error("Persistent execution backend is not configured");
  }
  const response = await fetch(`${base.replace(/\/$/, "")}/execution/${action}`, {
    method: body === undefined ? "GET" : "POST",
    headers: {
      Authorization: `Bearer ${token}`,
      ...(body === undefined ? {} : { "Content-Type": "application/json" }),
    },
    ...(body === undefined ? {} : { body: JSON.stringify(body) }),
    signal: AbortSignal.timeout(action === "confirm" ? 25000 : 15000),
    redirect: "error",
  });
  const result = await response.json();
  if (!response.ok) {
    const error = new Error(result.error || result.reason || "Execution request refused");
    error.status = response.status;
    throw error;
  }
  return result;
}
