import { createExecutionContext, env } from "cloudflare:test";
import { describe, expect, it } from "vitest";
import relay from "../src/index";

const ORIGIN = "https://relay.example.test";
const entra = {
  GITHUB_CLIENT_ID:"", GITHUB_CLIENT_SECRET:"",
  ENTRA_TENANT_ID:"11111111-2222-3333-4444-555555555555",
  ENTRA_AUTHORITY:"https://astra-demo.ciamlogin.com/11111111-2222-3333-4444-555555555555",
  ENTRA_CLIENT_ID:"aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
  ENTRA_CLIENT_SECRET:"synthetic-client-secret",
  ENTRA_REQUIRED_ROLE:"Astra.User"
};

describe("provider-independent authentication readiness", () => {
  it("reports configured Entra without GitHub credentials and does not expose provider configuration", async () => {
    const response=await relay.fetch(new Request(ORIGIN+"/health"),{...env,...entra},createExecutionContext());
    const body=await response.text();
    expect(JSON.parse(body)).toMatchObject({version:"0.17.0",authentication_configured:true,deployment_mode:"local"});
    expect(response.headers.get("Cache-Control")).toBe("no-store");
    for(const value of Object.values(entra).filter(Boolean)) expect(body).not.toContain(value);
    expect(body).not.toContain(env.SESSION_SECRET);
    const {ENTRA_REQUIRED_ROLE:omitted,...unrestricted}=entra;
    const selfService=await relay.fetch(new Request(ORIGIN+"/health"),{...env,...unrestricted},createExecutionContext());
    expect(await selfService.json()).toMatchObject({authentication_configured:true});
  });

  it("fails closed for incomplete, invalid or sessionless Entra configuration", async () => {
    for(const changes of [{ENTRA_CLIENT_SECRET:""},{ENTRA_CLIENT_ID:"wrong-audience"},
      {ENTRA_TENANT_ID:"common"},{ENTRA_AUTHORITY:"https://evil.example/"+entra.ENTRA_TENANT_ID},
      {SESSION_SECRET:"short"},{SESSION_SECRET:""}]) {
      const response=await relay.fetch(new Request(ORIGIN+"/health"),{...env,...entra,...changes},createExecutionContext());
      expect(await response.json()).toMatchObject({authentication_configured:false});
    }
  });

  it("keeps valid GitHub readiness when optional Entra configuration is incomplete", async () => {
    const response=await relay.fetch(new Request(ORIGIN+"/health"),{...env,ENTRA_TENANT_ID:entra.ENTRA_TENANT_ID},createExecutionContext());
    expect(await response.json()).toMatchObject({authentication_configured:true});
    const mcp=await relay.fetch(new Request(ORIGIN+"/mcp",{method:"POST",body:"{}"}),{...env,...entra},createExecutionContext());
    expect(mcp.status).toBe(401);
    expect(mcp.headers.get("WWW-Authenticate")).toContain("oauth-protected-resource");
  });
});
