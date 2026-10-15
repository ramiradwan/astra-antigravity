import { createExecutionContext, env, SELF } from "cloudflare:test";
import { describe, expect, it } from "vitest";
import relay from "../src/index";

const ORIGIN = "https://relay.example.test";
const PATH = "/.well-known/openai-apps-challenge";
const SYNTHETIC = "a".repeat(42) + "_";
function request(challenge: string, method = "GET", path = PATH, headers: Record<string,string> = {}) {
  return relay.fetch(new Request(ORIGIN + path, { method, headers }), { ...env, OPENAI_APPS_CHALLENGE: challenge }, createExecutionContext());
}

describe("OpenAI plugin domain challenge", () => {
  it("is disabled with plain-text 404 when unset or empty", async () => {
    const response = await SELF.fetch(ORIGIN + PATH);
    expect(response.status).toBe(404);
    expect(response.headers.get("Content-Type")).toContain("text/plain");
    expect(response.headers.get("Location")).toBeNull();
    expect(response.headers.get("WWW-Authenticate")).toBeNull();
    const actual = { ...env };
    Object.defineProperty(actual, "OPENAI_APPS_CHALLENGE", { value: undefined });
    const missing = await relay.fetch(new Request(ORIGIN + PATH), actual, createExecutionContext());
    expect(missing.status).toBe(404);
  });

  it("returns exactly the configured text without cookies, OAuth, JSON, or a newline", async () => {
    const response = await relay.fetch(new Request(ORIGIN + PATH, { headers: { Authorization: "Bearer invalid" } }), {
      ...env, OPENAI_APPS_CHALLENGE: SYNTHETIC, GITHUB_CLIENT_ID: "", GITHUB_CLIENT_SECRET: "", SESSION_SECRET: ""
    }, createExecutionContext());
    expect(response.status).toBe(200);
    expect(await response.text()).toBe(SYNTHETIC);
    expect(response.headers.get("Content-Type")).toBe("text/plain; charset=utf-8");
    expect(response.headers.get("Cache-Control")).toBe("no-store");
    expect(response.headers.get("X-Content-Type-Options")).toBe("nosniff");
    for (const header of ["WWW-Authenticate", "Set-Cookie", "Location"]) expect(response.headers.get(header)).toBeNull();
  });

  it("fails closed without echoing malformed configuration", async () => {
    for (const invalid of [" "+SYNTHETIC, SYNTHETIC+"\n", SYNTHETIC+"\r\n", SYNTHETIC.slice(1), SYNTHETIC+"b", '"'+SYNTHETIC+'"', SYNTHETIC+","+SYNTHETIC, "<".repeat(43)]) {
      const response = await request(invalid);
      expect(response.status).toBe(503);
      expect(response.headers.get("Content-Type")).toContain("text/plain");
      expect(await response.text()).not.toContain(invalid);
    }
  });

  it("allows only GET on the configured exact path", async () => {
    for (const method of ["HEAD", "POST", "PUT", "OPTIONS", "DELETE"]) {
      const response = await request(SYNTHETIC, method);
      expect(response.status).toBe(405);
      expect(response.headers.get("Allow")).toBe("GET");
      expect(await response.text()).not.toContain(SYNTHETIC);
    }
    for (const path of [PATH+"/", PATH+"/child", PATH+"-other"]) {
      const response = await request(SYNTHETIC, "GET", path);
      expect(response.status).toBe(404);
      expect(await response.text()).not.toContain(SYNTHETIC);
    }
  });

  it("preserves origin checks and leaves MCP authentication protected", async () => {
    const denied = await request(SYNTHETIC, "GET", PATH, { Origin: "https://untrusted.example" });
    expect(denied.status).toBe(403);
    expect(await denied.text()).not.toContain(SYNTHETIC);
    expect((await request(SYNTHETIC, "GET", PATH, { Origin: "https://chatgpt.com" })).status).toBe(200);
    const wrongHost = await relay.fetch(new Request("https://wrong.example"+PATH), { ...env, OPENAI_APPS_CHALLENGE: SYNTHETIC }, createExecutionContext());
    expect(wrongHost.status).toBe(400);
    const mcp = await request(SYNTHETIC, "POST", "/mcp");
    expect(mcp.status).toBe(401);
    expect(mcp.headers.get("WWW-Authenticate")).toBeTruthy();
    expect(await mcp.text()).not.toContain(SYNTHETIC);
    const health = await request(SYNTHETIC, "GET", "/health");
    expect(await health.text()).not.toContain(SYNTHETIC);
  });
});
