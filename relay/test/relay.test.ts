import { env, SELF, runInDurableObject, evictDurableObject, createExecutionContext } from "cloudflare:test";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { authPage, authorizationServer, managementSession } from "../src/auth";
import { digest, SCOPES } from "../src/contracts";
import { callbackUrl, page, seal } from "../src/security";
import type { OwnerRelay } from "../src/broker";
import relay from "../src/index";

declare module "cloudflare:test" { interface ProvidedEnv extends Env {} }
const ORIGIN = "https://relay.example.test";
let testPrefix = "";
beforeEach(() => { testPrefix = crypto.randomUUID(); });
afterEach(() => { vi.restoreAllMocks(); });
const workspace = { id: "project", label: "Synthetic project", capabilities: ["local_files", "shell"] as const,
  isolation: ["worktree", "shared"] as const, task_types: ["implementation", "analysis", "research", "document"] as const };

async function owner(user = "owner-a") {
  const key = await digest(testPrefix + user);
  const stub = env.OWNERS.getByName(key);
  const input = { label: "Synthetic worker", credential_hash: await digest("a".repeat(64)),
    workspaces: [{ ...workspace, capabilities: [...workspace.capabilities], isolation: [...workspace.isolation], task_types: [...workspace.task_types] }] };
  const worker = await stub.registerWorker(input, key, "pair-" + user);
  return { key, stub, worker_id: worker.worker_id, secret: "a".repeat(64) };
}

async function accessToken(user = "owner-a", scopes = [...SCOPES]) {
  user = testPrefix + user;
  const server = authorizationServer(env), helpers = server.getOAuthApi(env);
  const client = await helpers.createClient({ clientName: "Synthetic client", redirectUris: ["http://localhost:12345/callback"],
    tokenEndpointAuthMethod: "none", grantTypes: ["authorization_code", "refresh_token"], responseTypes: ["code"] });
  const verifier = "s".repeat(64);
  const hash = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(verifier));
  const challenge = btoa(String.fromCharCode(...new Uint8Array(hash))).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  const grant = await helpers.completeAuthorization({ request: { responseType: "code", clientId: client.clientId,
    redirectUri: client.redirectUris[0], scope: scopes, state: "synthetic-state", resource: ORIGIN + "/mcp", issuer: ORIGIN,
    codeChallenge: challenge, codeChallengeMethod: "S256" }, userId: user, scope: scopes, metadata: {},
    props: { userId: user, ownerKey: await digest(user) } });
  const response = await SELF.fetch(ORIGIN + "/oauth/token", { method: "POST", body: new URLSearchParams({ grant_type: "authorization_code",
    client_id: client.clientId, code: new URL(grant.redirectTo).searchParams.get("code")!, code_verifier: verifier,
    redirect_uri: client.redirectUris[0], resource: ORIGIN + "/mcp" }) });
  expect(response.status).toBe(200);
  const result = await response.json<{access_token: string}>();
  return result.access_token;
}
async function rpc(bearer: string, method: string, params: Record<string, unknown> = {}, modern = false) {
  const response = await SELF.fetch(ORIGIN + "/mcp", { method: "POST", headers: { Authorization: "Bearer " + bearer,
    "Content-Type": "application/json", Accept: "application/json, text/event-stream", ...(modern ? {"MCP-Protocol-Version":"2026-07-28","Mcp-Method":method,
      ...(["tools/call","resources/read"].includes(method) ? {"Mcp-Name":String(params.name ?? params.uri)} : {})} : {}) },
    body: JSON.stringify({jsonrpc:"2.0",id:1,method,params:{...params,...(modern ? {_meta:{"io.modelcontextprotocol/protocolVersion":"2026-07-28",
      "io.modelcontextprotocol/clientCapabilities":{},"io.modelcontextprotocol/clientInfo":{name:"test",version:"1"}}} : {})}}) });
  const body = await response.text();
  if (body.startsWith("event:") || body.includes("\ndata:")) {
    const data = body.split("\n").find(line => line.startsWith("data: "))!;
    return { response, json: JSON.parse(data.slice(6)) };
  }
  return { response, json: JSON.parse(body) };
}
async function expectFault(stub: DurableObjectStub<OwnerRelay>, action: (instance: OwnerRelay) => Promise<unknown>, message: string) {
  // Catch inside the object so the Vitest RPC wrapper doesn't register an
  // expected remote rejection as a separate unhandled waitUntil failure.
  const failure = await runInDurableObject(stub, async (instance: OwnerRelay) => {
    try { await action(instance); return null; }
    catch (error) { return error instanceof Error ? error.message : "unknown error"; }
  });
  expect(failure).toContain(message);
}

describe("first-use setup", () => {
  const firstTask = {request_id:"first-use-task",task:"Create answer.py with answer() returning 42",task_type:"implementation",
    isolation:"auto",reasoning:"light",required_capabilities:[]};

  it("saves the first task without a worker and resumes the same receipt after browser workspace approval", async () => {
    const key = await digest(testPrefix + "fresh-account"), stub = env.OWNERS.getByName(key);
    const receipt = await stub.tool("prepare_delegation", firstTask);
    expect(receipt).toMatchObject({status:"awaiting_setup",inference_started:false,request_id:firstTask.request_id});
    expect((await stub.workspaces()).workspaces).toEqual([]);
    expect((await stub.events(String(receipt.delegation_id),0)).events).toEqual([]);
    expect((await stub.tool("prepare_delegation",firstTask)).delegation_id).toBe(receipt.delegation_id);
    const paired = await SELF.fetch(ORIGIN+"/worker/pair",{method:"POST",headers:{"Content-Type":"application/json"},
      body:JSON.stringify({label:"My computer",credential_hash:await digest("a".repeat(64)),workspaces:[workspace],setup_request_id:firstTask.request_id})});
    const pairing = await paired.json<{code:string;claim:string}>();
    const cookie = await seal({userId:"fresh-account",ownerKey:key,csrf:"first-use-consent",expires:Date.now()+60_000},env.SESSION_SECRET);
    const headers={Cookie:"__Host-astra-session="+cookie,Origin:ORIGIN};
    const preview=await SELF.fetch(ORIGIN+"/pair?code="+pairing.code,{headers});
    expect(await preview.text()).toContain("Create answer.py");
    const approved=await SELF.fetch(ORIGIN+"/pair?code="+pairing.code,{method:"POST",headers,body:new URLSearchParams({decision:"approve",csrf:"first-use-consent"})});
    expect(approved.status).toBe(200);
    expect(await approved.text()).toContain("Setup continues automatically");
    const claim=await SELF.fetch(ORIGIN+"/worker/pair/"+pairing.code,{headers:{Authorization:"Bearer "+pairing.claim}});
    const identity=await claim.json<{status:string;worker_id:string}>();
    expect(identity.status).toBe("approved");
    const resumed=await stub.tool("inspect_delegation",{request_id:firstTask.request_id});
    expect(resumed).toMatchObject({delegation_id:receipt.delegation_id,status:"queued",workspace_id:"project"});
    const commands=await stub.pullCommands(identity.worker_id,"a".repeat(64));
    expect(commands.commands).toHaveLength(1);
    const command=(commands.commands as Record<string,unknown>[])[0];
    expect(command).toMatchObject({delegation_id:receipt.delegation_id,allow_start:true,payload:{task:firstTask.task,isolation:"worktree"}});
    const retry=await SELF.fetch(ORIGIN+"/pair?code="+pairing.code,{method:"POST",headers,body:new URLSearchParams({decision:"approve",csrf:"first-use-consent"})});
    expect(retry.status).toBe(200);
    expect((await stub.tool("prepare_delegation",firstTask)).delegation_id).toBe(receipt.delegation_id);
    const replay=(await stub.pullCommands(identity.worker_id,"a".repeat(64))).commands as Record<string,unknown>[];
    expect(replay).toHaveLength(1);expect(replay[0].allow_start).toBe(false);
  });

  it("uses the original worker for an explicit existing alias even while offline", async () => {
    const a=await owner();
    const receipt=await a.stub.tool("prepare_delegation",{...firstTask,workspace_id:"project"});
    expect(receipt.status).toBe("queued");
    expect(receipt.setup_url).toContain("/connect?request_id=");
    const commands=(await a.stub.pullCommands(a.worker_id,a.secret)).commands as Record<string,unknown>[];
    expect(commands).toHaveLength(1);expect(commands[0].delegation_id).toBe(receipt.delegation_id);
    await expectFault(a.stub,instance=>instance.bindPrepared(firstTask.request_id,a.worker_id,"another-workspace"),"already bound");
  });

  it("allows cancellation before pairing and never starts that saved task", async () => {
    const a=await owner();
    const receipt=await a.stub.tool("prepare_delegation",firstTask);
    expect(receipt.status).toBe("awaiting_setup");
    const waiting=a.stub.tool("receive_delegation_events",{delegation_id:receipt.delegation_id,after_sequence:0});
    await new Promise(resolve=>setTimeout(resolve,30));
    await a.stub.tool("cancel_delegation",{delegation_id:receipt.delegation_id});
    expect(await waiting).toMatchObject({closed:true,events:[{kind:"cancelled",terminal:true}]});
    expect((await a.stub.events(String(receipt.delegation_id),0)).events).toHaveLength(1);
    await expectFault(a.stub,instance=>instance.bindPrepared(firstTask.request_id,a.worker_id),"no longer awaiting approval");
  });

  it("rejects unavailable capabilities before saving an unusable setup request", async () => {
    const a=await owner();
    await expectFault(a.stub,instance=>instance.tool("prepare_delegation",{...firstTask,required_capabilities:["web"]}),"not inherited");
    await expectFault(a.stub,instance=>instance.setupDetails(firstTask.request_id),"does not belong");
  });

  it("rejects changed intent and another account's setup receipt", async () => {
    const a=await owner("setup-a"),b=await owner("setup-b");
    await a.stub.tool("prepare_delegation",firstTask);
    await expectFault(a.stub,instance=>instance.tool("prepare_delegation",{...firstTask,task:"Different task"}),"different work");
    await expectFault(b.stub,instance=>instance.setupDetails(firstTask.request_id),"does not belong");
  });

  it("reports browser decline to the worker without granting access", async () => {
    const request=await SELF.fetch(ORIGIN+"/worker/pair",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({label:"Declined computer",credential_hash:await digest("z".repeat(64)),workspaces:[workspace]})});
    const pairing=await request.json<{code:string;claim:string}>();
    const session=await seal({userId:"declining-user",ownerKey:await digest(testPrefix+"declining-user"),csrf:"decline",expires:Date.now()+60_000},env.SESSION_SECRET);
    const denied=await SELF.fetch(ORIGIN+"/pair?code="+pairing.code,{method:"POST",headers:{Cookie:"__Host-astra-session="+session,Origin:ORIGIN},body:new URLSearchParams({decision:"deny",csrf:"decline"})});
    expect(denied.status).toBe(200);
    const status=await SELF.fetch(ORIGIN+"/worker/pair/"+pairing.code,{headers:{Authorization:"Bearer "+pairing.claim}});
    expect(await status.json()).toEqual({status:"declined"});
  });
});

describe("durable owner and worker boundaries", () => {
  it("isolates workspace discovery and saved tasks across accounts", async () => {
    const a = await owner("a"), b = env.OWNERS.getByName(await digest(testPrefix + "b"));
    expect((await b.workspaces()).workspaces).toEqual([]);
    const receipt = await a.stub.tool("delegate_via_luna", { workspace_id: "project", request_id: "once", task: "Create answer.py" });
    await expectFault(b, item => item.tool("inspect_delegation", { delegation_id: receipt.delegation_id }), "Owned delegation not found");
    await expectFault(b, item => item.tool("delegate_via_luna", { workspace_id: "project", request_id: "once", task: "Create answer.py" }), "Owned workspace not found");
  });
  it("returns the same receipt for an idempotent request and rejects changed input", async () => {
    const a = await owner();
    const input = { workspace_id:"project",request_id:"once",task:"Create answer.py" };
    const first = await a.stub.tool("delegate_via_luna", input);
    const again = await a.stub.tool("delegate_via_luna", input);
    expect(again.delegation_id).toBe(first.delegation_id);
    await expectFault(a.stub, item => item.tool("delegate_via_luna", {...input,task:"Different work"}), "request_id was already used");
  });
  it("cancels an offline queued launch without issuing a provider command", async () => {
    const a = await owner(), receipt = await a.stub.tool("delegate_via_luna", {workspace_id:"project",request_id:"once",task:"Create answer.py"});
    const stopped = await a.stub.tool("cancel_delegation", { delegation_id:receipt.delegation_id });
    expect(stopped.status).toBe("cancelled");
    const result = await a.stub.tool("read_delegation_result", { delegation_id:receipt.delegation_id });
    expect(result.handoff).toMatchObject({inference_started:false});
    const queued = await runInDurableObject(a.stub, async (_instance: OwnerRelay, state) => state.storage.sql.exec("SELECT id FROM commands WHERE state='queued'").toArray());
    expect(queued).toEqual([]);
  });
  it("reconnects by replaying to the same authenticated worker and denies another worker", async () => {
    const a = await owner();
    const receipt = await a.stub.tool("delegate_via_luna", {workspace_id:"project",request_id:"once",task:"Create answer.py"});
    const first = await a.stub.pullCommands(a.worker_id, a.secret);
    const replay = await a.stub.pullCommands(a.worker_id, a.secret);
    expect(first.commands).toMatchObject([{allow_start:true}]);
    expect(replay.commands).toMatchObject([{command_id:(first.commands as {command_id:string}[])[0].command_id,allow_start:false}]);
    await expectFault(a.stub, item => item.pullCommands(a.worker_id, "b".repeat(64)), "Worker authentication failed");
    const other = await owner("other");
    await expectFault(other.stub, item => item.workerHttp(other.worker_id,other.secret,{kind:"receipt",delegation_id:receipt.delegation_id,local_id:"wrong"}), "Owned delegation not found");
  });
  it("persists terminal output before ACK and deduplicates replay", async () => {
    const a = await owner(), receipt = await a.stub.tool("delegate_via_luna", {workspace_id:"project",request_id:"once",task:"Create answer.py"});
    const event = {sequence:1,kind:"completed",terminal:true,summary:"Done",task_type:"implementation",verification_required:true};
    const packet = {kind:"event",delegation_id:receipt.delegation_id,event,result:{status:"completed",summary:"answer returns 42"},message_id:"msg-1"};
    const ack = await a.stub.workerHttp(a.worker_id,a.secret,packet);
    expect(ack.message_id).toBe("msg-1");
    await a.stub.workerHttp(a.worker_id,a.secret,packet);
    expect((await a.stub.events(String(receipt.delegation_id),0)).events).toHaveLength(1);
    const result = await a.stub.tool("read_delegation_result",{delegation_id:receipt.delegation_id});
    expect(result.handoff).toMatchObject({status:"completed"});
    await a.stub.tool("ack_delegation_events",{delegation_id:receipt.delegation_id,sequence:1});
    expect((await a.stub.events(String(receipt.delegation_id),0)).events).toHaveLength(1);
  });
  it("enforces event/result bounds and revoked-worker access", async () => {
    const a=await owner(),receipt=await a.stub.tool("delegate_via_luna",{workspace_id:"project",request_id:"once",task:"Create answer.py"});
    await expectFault(a.stub, item => item.workerHttp(a.worker_id,a.secret,{kind:"event",delegation_id:receipt.delegation_id,
      event:{sequence:1,kind:"decision",terminal:false,summary:"x".repeat(601),task_type:"implementation",verification_required:false}}), "Decision summary exceeds");
    await a.stub.revokeWorker(a.worker_id);
    await expectFault(a.stub, item => item.tool("inspect_delegation",{delegation_id:receipt.delegation_id}), "expired or was revoked");
    await expectFault(a.stub, item => item.pullCommands(a.worker_id,a.secret), "expired or was revoked");
  });
  it("keeps a provider terminal event after a transport recovery notice", async () => {
    const a=await owner(),receipt=await a.stub.tool("delegate_via_luna",{workspace_id:"project",request_id:"once",task:"Create answer.py"});
    await a.stub.pullCommands(a.worker_id,a.secret);
    await a.stub.workerHttp(a.worker_id,a.secret,{kind:"recovery_required",delegation_id:receipt.delegation_id,message_id:"recovery"});
    const packet={kind:"event",delegation_id:receipt.delegation_id,message_id:"terminal",event:{sequence:1,kind:"completed",terminal:true,
      summary:"Saved task completed",task_type:"implementation",model:"sonnet-5.5",verification_required:true},result:{status:"completed"}};
    await a.stub.workerHttp(a.worker_id,a.secret,packet);
    await a.stub.workerHttp(a.worker_id,a.secret,packet);
    expect((await a.stub.events(String(receipt.delegation_id),0)).events).toMatchObject([{sequence:1,kind:"recovery_required"},{sequence:2,kind:"completed"}]);
  });
  it("expires unfinished payloads while retaining a launch tombstone", async () => {
    const a=await owner(),input={workspace_id:"project",request_id:"once",task:"Private task prompt"},receipt=await a.stub.tool("delegate_via_luna",input);
    await runInDurableObject(a.stub,async(instance: OwnerRelay,state)=>{
      state.storage.sql.exec("UPDATE jobs SET expires=? WHERE id=?",Date.now()-1,String(receipt.delegation_id));
      await instance.alarm();
    });
    const stored=await runInDurableObject(a.stub,async(_instance: OwnerRelay,state)=>({jobs:state.storage.sql.exec("SELECT * FROM jobs").toArray(),
      commands:state.storage.sql.exec("SELECT * FROM commands").toArray(),retired:state.storage.sql.exec("SELECT * FROM retired_requests").toArray()}));
    expect(stored.jobs).toEqual([]);
    expect(stored.commands).toEqual([]);
    expect(stored.retired).toHaveLength(1);
    await expectFault(a.stub,item=>item.tool("delegate_via_luna",input),"Saved task expired");
  });
  it("survives object eviction without replacing a launch identity", async () => {
    const a=await owner(),input={workspace_id:"project",request_id:"once",task:"Create answer.py"},receipt=await a.stub.tool("delegate_via_luna",input);
    await a.stub.pullCommands(a.worker_id,a.secret);
    await evictDurableObject(a.stub);
    expect((await a.stub.tool("delegate_via_luna",input)).delegation_id).toBe(receipt.delegation_id);
    expect((await a.stub.pullCommands(a.worker_id,a.secret)).commands).toMatchObject([{allow_start:false}]);
  });
  it("keeps the parent event budget without blocking a provider terminal",async()=>{
    const a=await owner(),receipt=await a.stub.tool("delegate_via_luna",{workspace_id:"project",request_id:"once",task:"Create answer.py"});
    await a.stub.workerHttp(a.worker_id,a.secret,{kind:"recovery_required",delegation_id:receipt.delegation_id});
    for(let sequence=1;sequence<=8;sequence++) await a.stub.workerHttp(a.worker_id,a.secret,{kind:"event",delegation_id:receipt.delegation_id,
      event:{sequence,kind:"decision",terminal:false,summary:"Bounded decision",task_type:"implementation",verification_required:false}});
    await a.stub.workerHttp(a.worker_id,a.secret,{kind:"event",delegation_id:receipt.delegation_id,event:{sequence:9,kind:"completed",terminal:true,
      summary:"Done",task_type:"implementation",verification_required:true},result:{status:"completed"}});
    const events=(await a.stub.events(String(receipt.delegation_id),0)).events as {sequence:number;terminal:boolean}[];
    expect(events).toHaveLength(9);
    expect(events[8]).toMatchObject({sequence:9,terminal:true});
  });
  it("bounds the entire escaped parent envelope including Unicode and IDs",async()=>{
    const a=await owner(),receipt=await a.stub.tool("delegate_via_luna",{workspace_id:"project",request_id:"once",task:"Create answer.py"});
    for(const [sequence,terminal,summary] of [[1,false,"🧪".repeat(250)],[2,true,"🧪".repeat(750)]] as const) {
      const packet={kind:"event",delegation_id:receipt.delegation_id,event:{sequence,terminal,kind:terminal?"completed":"decision",summary,
        task_type:"implementation",verification_required:terminal},...(terminal?{result:{status:"completed"}}:{})};
      await a.stub.workerHttp(a.worker_id,a.secret,packet);
      await a.stub.workerHttp(a.worker_id,a.secret,packet);
    }
    const events=(await a.stub.events(String(receipt.delegation_id),0)).events as {terminal:boolean;summary:string}[];
    for(const event of events) {
      const wire=JSON.stringify(event).replace(/[\u007f-\uffff]/g,c=>"\\u"+c.charCodeAt(0).toString(16).padStart(4,"0"));
      expect(wire.length).toBeLessThanOrEqual(event.terminal?1600:600);
      expect(event.summary).not.toMatch(/[\ud800-\udbff]$/);
    }
  });
  it("delivers commands and durable ACKs over a hibernating socket",async()=>{
    const a=await owner();
    const connected=await SELF.fetch(ORIGIN+"/worker/connect",{headers:{Upgrade:"websocket",Authorization:`Bearer ${a.key}.${a.worker_id}.${a.secret}`}});
    expect(connected.status).toBe(101);
    const socket=connected.webSocket!;socket.accept();
    const received: Record<string,unknown>[]=[];
    const command=new Promise<Record<string,unknown>>(resolve=>socket.addEventListener("message",message=>{
      const packet=JSON.parse(String(message.data));received.push(packet);if(packet.kind==="command")resolve(packet);
    }));
    try {
      const receipt=await a.stub.tool("delegate_via_luna",{workspace_id:"project",request_id:"once",task:"Create answer.py"});
      const packet=await command;
      expect(packet).toMatchObject({delegation_id:receipt.delegation_id,allow_start:true});
      await evictDurableObject(a.stub);
      const ack=new Promise<Record<string,unknown>>(resolve=>socket.addEventListener("message",message=>{
        const value=JSON.parse(String(message.data));if(value.kind==="ack")resolve(value);
      }));
      socket.send(JSON.stringify({kind:"receipt",delegation_id:receipt.delegation_id,local_id:"local-task",message_id:"receipt"}));
      expect(await ack).toMatchObject({message_id:"receipt"});
      expect((await a.stub.tool("inspect_delegation",{delegation_id:receipt.delegation_id})).status).toBe("running");
    } finally {socket.close(1000);}
  });
});

describe("real HTTP MCP and OAuth", () => {
  it("exposes the acknowledged cleanup outcome without another inference call",async()=>{
    const a=await owner(),receipt=await a.stub.tool("delegate_via_luna",{workspace_id:"project",request_id:"once",task:"Create answer.py"});
    await a.stub.workerHttp(a.worker_id,a.secret,{kind:"receipt",delegation_id:receipt.delegation_id,local_id:"local-task"});
    await a.stub.workerHttp(a.worker_id,a.secret,{kind:"event",delegation_id:receipt.delegation_id,event:{sequence:1,kind:"completed",terminal:true,
      summary:"Done",task_type:"implementation",verification_required:true},result:{status:"completed"}});
    const cleanup=await a.stub.tool("cleanup_task_worktree",{delegation_id:receipt.delegation_id});
    await a.stub.workerHttp(a.worker_id,a.secret,{kind:"command_result",delegation_id:receipt.delegation_id,command_id:cleanup.cleanup_command_id,result:{cleaned:true}});
    expect((await a.stub.tool("inspect_delegation",{delegation_id:receipt.delegation_id})).cleanup).toMatchObject({status:"done",result:{cleaned:true}});
  });
  it("publishes resource discovery and challenges unauthenticated calls", async () => {
    const metadata=await SELF.fetch(ORIGIN+"/.well-known/oauth-protected-resource/mcp");
    expect(metadata.status).toBe(200);
    expect(await metadata.json()).toMatchObject({resource:ORIGIN+"/mcp"});
    const denied=await SELF.fetch(ORIGIN+"/mcp",{method:"POST",body:"{}"});
    expect(denied.status).toBe(401);
    expect(denied.headers.get("WWW-Authenticate")).toContain("oauth-protected-resource");
  });
  it("reports sign-in configuration without exposing secrets or caching readiness", async () => {
    for (const [changes,configured] of [
      [{},true], [{GITHUB_CLIENT_ID:""},false], [{GITHUB_CLIENT_SECRET:""},false],
      [{SESSION_SECRET:"short"},false]
    ] as const) {
      const actual={...env,...changes};
      const response=await relay.fetch(new Request(ORIGIN+"/health"),actual,createExecutionContext());
      expect(response.status).toBe(200);
      expect(response.headers.get("Cache-Control")).toBe("no-store");
      const body=await response.text();
      expect(JSON.parse(body)).toMatchObject({authentication_configured:configured,deployment_mode:"local"});
      for(const name of ["GITHUB_CLIENT_ID","GITHUB_CLIENT_SECRET","SESSION_SECRET"] as const) {
        if(actual[name]) expect(body).not.toContain(actual[name]);
      }
    }
  });
  it("initializes and discovers the supported tools using an actual issued token", async () => {
    const bearer=await accessToken();
    const initial=await rpc(bearer,"initialize",{protocolVersion:"2025-11-25",capabilities:{},clientInfo:{name:"test",version:"1"}});
    expect(initial.response.status).toBe(200);
    expect(initial.json.result.serverInfo.name).toBe("Astra Antigravity Relay");
    expect(initial.json.result.serverInfo.version).toBe("0.17.0");
    const listed=await rpc(bearer,"tools/list");
    expect(listed.json.result.tools.map((t:{name:string})=>t.name)).toContain("delegate_task");
    expect(listed.json.result.tools).toHaveLength(23);
    expect(listed.json.result.tools.find((tool:{name:string})=>tool.name==="prepare_delegation")).toMatchObject({
      annotations:{readOnlyHint:false,idempotentHint:true},securitySchemes:[{type:"oauth2",scopes:["tasks:write"]}]});
    expect(listed.json.result.tools.every((t:{annotations:unknown})=>Boolean(t.annotations))).toBe(true);
    expect(listed.json.result.tools.every((t:{securitySchemes:unknown})=>Boolean(t.securitySchemes))).toBe(true);
  });
  it.each([false,true])("lists neutral task names while preserving task authority (modern=%s)",async modern=>{
    const listed=await rpc(await accessToken(),"tools/list",{},modern);
    expect(listed.response.status).toBe(200);
    const tools=listed.json.result.tools as Array<Record<string,any>>;
    for(const name of ["delegate_task","continue_task"])expect(tools.find(tool=>tool.name===name)).toMatchObject({
      annotations:{readOnlyHint:false,destructiveHint:true,openWorldHint:true,idempotentHint:true},
      securitySchemes:[{type:"oauth2",scopes:["tasks:write"]}],_meta:{securitySchemes:[{type:"oauth2",scopes:["tasks:write"]}]}
    });
    expect(tools.some(tool=>["delegate_via_luna","continue_via_luna"].includes(tool.name))).toBe(false);
  });
  it.each([false,true])("shares launch and correction receipts across current and cached tool names (modern=%s)",async modern=>{
    const a=await owner(),bearer=await accessToken();
    const call=async(name:string,args:Record<string,unknown>)=>{
      const response=await rpc(bearer,"tools/call",{name,arguments:args},modern);
      expect(response.response.status).toBe(200);expect(response.json.result.isError).not.toBe(true);
      return JSON.parse(response.json.result.content[0].text);
    };
    const input={workspace_id:"project",request_id:"same-launch",task:"Create the approved document",task_type:"document",model:"sonnet-5.5"};
    const first=await call("delegate_task",input),cached=await call("delegate_via_luna",input);
    expect(cached).toEqual(first);
    const launched=await a.stub.pullCommands(a.worker_id,a.secret);
    expect(launched.commands).toMatchObject([{delegation_id:first.delegation_id,operation:"delegate",allow_start:true,payload:input}]);
    expect(launched.commands).toHaveLength(1);
    const changed=await rpc(bearer,"tools/call",{name:"delegate_via_luna",arguments:{...input,task:"Different work"}},modern);
    expect(changed.json.result.isError).toBe(true);expect(changed.json.result.content[0].text).toContain("request_id was already used");
    await a.stub.workerHttp(a.worker_id,a.secret,{kind:"receipt",delegation_id:first.delegation_id,local_id:"saved-document"});
    await a.stub.workerHttp(a.worker_id,a.secret,{kind:"event",delegation_id:first.delegation_id,event:{sequence:1,kind:"completed",terminal:true,summary:"Done",task_type:"document",model:"sonnet-5.5",verification_required:true},result:{status:"completed"}});
    const findings={delegation_id:first.delegation_id,request_id:"same-correction",findings:"Fix the heading in the saved document"};
    const corrected=await call("continue_task",findings),cachedCorrection=await call("continue_via_luna",findings);
    expect(cachedCorrection).toEqual(corrected);expect(corrected.delegation_id).not.toBe(first.delegation_id);
    const corrections=await a.stub.pullCommands(a.worker_id,a.secret);
    expect(corrections.commands).toMatchObject([{delegation_id:corrected.delegation_id,operation:"continue",allow_start:true,payload:{...findings,source_id:first.delegation_id}}]);
    expect(corrections.commands).toHaveLength(1);
    const pinned=await runInDurableObject(a.stub,async(_instance,state)=>state.storage.sql.exec("SELECT worker_id,workspace_id,task_type,model_pin FROM jobs WHERE id=?",corrected.delegation_id).toArray());
    expect(pinned).toEqual([{worker_id:a.worker_id,workspace_id:"project",task_type:"document",model_pin:"sonnet-5.5"}]);
  });
  it.each([false,true])("declares cancellation as destructive in the served tool catalog (modern=%s)",async modern=>{
    const bearer=await accessToken(),listed=await rpc(bearer,"tools/list",{},modern);
    expect(listed.response.status).toBe(200);
    const tools=listed.json.result.tools as Array<{name:string;annotations:Record<string,unknown>}>;
    expect(tools.find(tool=>tool.name==="cancel_delegation")).toMatchObject({
      annotations:{readOnlyHint:false,destructiveHint:true},
      securitySchemes:[{type:"oauth2",scopes:["tasks:write"]}]
    });
    expect(tools.find(tool=>tool.name==="inspect_delegation")).toMatchObject({
      annotations:{readOnlyHint:true,destructiveHint:false}
    });
  });
  it("permits verified OAuth form redirects without broadening management forms", async () => {
    const redirectUri="http://127.0.0.1:12345/callback";
    const client=await authorizationServer(env).getOAuthApi(env).createClient({clientName:"Browser consent fixture",
      redirectUris:[redirectUri],tokenEndpointAuthMethod:"none",grantTypes:["authorization_code"],responseTypes:["code"]});
    const query=new URLSearchParams({client_id:client.clientId,redirect_uri:redirectUri,response_type:"code",
      scope:SCOPES.join(" "),resource:ORIGIN+"/mcp",state:"synthetic-state",code_challenge:"a".repeat(43),code_challenge_method:"S256"});
    const response=await SELF.fetch(ORIGIN+"/authorize?"+query);
    expect(response.status).toBe(200);
    const policy=response.headers.get("Content-Security-Policy")!;
    expect(policy).toContain("form-action 'self' https://github.com http://127.0.0.1:12345;");
    expect(policy).not.toContain("*");
    expect(policy).toContain("default-src 'none'");
    expect(page("Management","<form method=post></form>").headers.get("Content-Security-Policy"))
      .toContain("form-action 'self';");
    for(const destination of ["https://user:password@unapproved.example", "https://unapproved.example;", "http://unapproved.example", "http://127.unapproved.example"]) {
      expect(()=>page("Invalid","",new Headers(),[destination])).toThrow();
    }
  });
  it("completes the normal GitHub management callback with bounded upstream replies", async () => {
    const helpers=authorizationServer(env).getOAuthApi(env);
    const next="/pair?code="+"a".repeat(24);
    const login=await authPage(new Request(ORIGIN+"/auth/login?next="+encodeURIComponent(next)),env,helpers);
    expect(login!.status).toBe(302);
    const provider=new URL(login!.headers.get("Location")!);
    const state=provider.searchParams.get("state")!;
    const binding=login!.headers.get("Set-Cookie")!.split(";")[0];
    const outbound=vi.spyOn(globalThis,"fetch").mockImplementation(async(input,init)=>{
      // Keep native Workers request validation even though the remote replies
      // are synthetic; a bare fetch spy would hide unsupported RequestInit.
      new Request(String(input),init);
      expect(init!.redirect).toBe("manual");
      if(String(input)==="https://github.com/login/oauth/access_token") {
        expect(init!.method).toBe("POST");
        expect(new Headers(init!.headers).get("User-Agent")).toBe("Astra-Antigravity-Relay");
        const body=new URLSearchParams(String(init!.body));
        expect(body.get("code")).toBe("synthetic-code");
        expect(body.get("code_verifier")).toHaveLength(64);
        return Response.json({access_token:"synthetic-ephemeral-upstream-token",scope:"",token_type:"bearer"});
      }
      if(String(input)==="https://api.github.com/user") {
        expect(new Headers(init!.headers).get("Authorization")).toBe("Bearer synthetic-ephemeral-upstream-token");
        return Response.json({id:12345,login:"synthetic-user"});
      }
      throw new Error("Unexpected upstream request in credential-free callback fixture");
    });
    const callback=await authPage(new Request(ORIGIN+"/auth/github/callback?"+new URLSearchParams({state,code:"synthetic-code"}),
      {headers:{Cookie:binding}}),env,helpers);
    expect(callback!.status).toBe(302);
    expect(callback!.headers.get("Location")).toBe(ORIGIN+next);
    const sessionCookie=callback!.headers.get("Set-Cookie")!.split(";")[0];
    const session=await managementSession(new Request(ORIGIN+"/account",{headers:{Cookie:sessionCookie}}),env);
    expect(session).toMatchObject({userId:"github-12345",ownerKey:await digest("github:12345")});
    expect(JSON.stringify(session)).not.toContain("synthetic-ephemeral-upstream-token");
    expect(outbound).toHaveBeenCalledTimes(2);
  });
  it("normalizes legacy management subjects without rebinding paired workers",async()=>{
    const previous={userId:"github:12345",ownerKey:await digest("github:12345"),csrf:"synthetic-csrf",expires:Date.now()+60_000};
    const cookie=await seal(previous,env.SESSION_SECRET);
    const request=new Request(ORIGIN+"/account",{headers:{Cookie:"__Host-astra-session="+cookie}});
    expect(await managementSession(request,env)).toMatchObject({...previous,userId:"github-12345"});
    const inconsistent=await seal({...previous,ownerKey:await digest("github:54321")},env.SESSION_SECRET);
    expect(await managementSession(new Request(ORIGIN+"/account",{headers:{Cookie:"__Host-astra-session="+inconsistent}}),env)).toBeNull();
  });
  it("completes browser consent, GitHub callback and the client's PKCE token exchange", async () => {
    const server=authorizationServer(env),helpers=server.getOAuthApi(env),redirectUri="http://127.0.0.1:12345/callback";
    const client=await helpers.createClient({clientName:"Full callback fixture",redirectUris:[redirectUri],
      tokenEndpointAuthMethod:"none",grantTypes:["authorization_code","refresh_token"],responseTypes:["code"]});
    const verifier="v".repeat(64),hash=await crypto.subtle.digest("SHA-256",new TextEncoder().encode(verifier));
    const challenge=btoa(String.fromCharCode(...new Uint8Array(hash))).replace(/\+/g,"-").replace(/\//g,"_").replace(/=+$/,"");
    const query=new URLSearchParams({client_id:client.clientId,redirect_uri:redirectUri,response_type:"code",
      scope:SCOPES.join(" "),resource:ORIGIN+"/mcp",state:"synthetic-client-state",code_challenge:challenge,code_challenge_method:"S256"});
    const consent=await authPage(new Request(ORIGIN+"/authorize?"+query),env,helpers);
    const handle=/name="handle" value="([^"]+)"/.exec(await consent!.text())![1];
    const cookies=(headers:Headers)=>headers.getSetCookie().map(value=>value.split(";")[0]).join("; ");
    const approval=await authPage(new Request(ORIGIN+"/authorize",{method:"POST",headers:{Origin:ORIGIN,Cookie:cookies(consent!.headers)},
      body:new URLSearchParams([["handle",handle],["decision","approve"],...SCOPES.map(scope=>["scope",scope])])}),env,helpers);
    expect(approval!.status).toBe(302);
    const upstream=new URL(approval!.headers.get("Location")!);
    vi.spyOn(globalThis,"fetch").mockImplementation(async(input,init)=>{
      new Request(String(input),init);
      if(String(input)==="https://github.com/login/oauth/access_token") return Response.json({access_token:"synthetic-upstream-token"});
      if(String(input)==="https://api.github.com/user") return Response.json({id:12345});
      throw new Error("Unexpected synthetic callback request");
    });
    const callback=await authPage(new Request(ORIGIN+"/auth/github/callback?"+new URLSearchParams({state:upstream.searchParams.get("state")!,code:"synthetic-github-code"}),
      {headers:{Cookie:cookies(approval!.headers)}}),env,helpers);
    expect(callback!.status).toBe(302);
    const clientCallback=new URL(callback!.headers.get("Location")!);
    const setupSession=await managementSession(new Request(ORIGIN+"/connect",{headers:{Cookie:cookies(callback!.headers)}}),env);
    expect(setupSession).toMatchObject({userId:"github-12345",ownerKey:await digest("github:12345")});
    expect(clientCallback.searchParams.get("state")).toBe("synthetic-client-state");
    expect(clientCallback.searchParams.get("iss")).toBe(ORIGIN);
    const exchange=await SELF.fetch(ORIGIN+"/oauth/token",{method:"POST",body:new URLSearchParams({grant_type:"authorization_code",client_id:client.clientId,
      code:clientCallback.searchParams.get("code")!,code_verifier:verifier,redirect_uri:redirectUri,resource:ORIGIN+"/mcp"})});
    expect(exchange.status).toBe(200);
    const issued=await exchange.json<{access_token:string}>();
    const authorization=await server.validateToken<{userId:string;ownerKey:string}>(ORIGIN+"/mcp",issued.access_token,env);
    expect(authorization!.props).toMatchObject({userId:"github-12345",ownerKey:await digest("github:12345")});
  });
  it("rejects upstream redirects and conceals transport exception contents", async () => {
    for(const kind of ["redirect","network"] as const) {
      const login=await relay.fetch(new Request(ORIGIN+"/auth/login?next=%2Faccount"),env,createExecutionContext());
      const state=new URL(login.headers.get("Location")!).searchParams.get("state")!;
      const binding=login.headers.get("Set-Cookie")!.split(";")[0];
      const outbound=vi.spyOn(globalThis,"fetch").mockImplementation(async(input,init)=>{
        new Request(String(input),init);
        expect(init!.redirect).toBe("manual");
        if(kind==="redirect") return new Response(null,{status:302,headers:{Location:"https://unapproved.example/credentials"}});
        throw new TypeError("Synthetic secret and callback code that must never reach a response");
      });
      try {
        const response=await relay.fetch(new Request(ORIGIN+"/auth/github/callback?"+new URLSearchParams({state,code:"synthetic-code"}),
          {headers:{Cookie:binding}}),env,createExecutionContext());
        expect(response.status).toBe(502);
        const text=await response.text();
        expect(JSON.parse(text).error).toBe(kind==="redirect" ? "github_redirect" : "github_token_unavailable");
        expect(text).not.toContain("Synthetic secret");
        expect(text).not.toContain("unapproved.example");
        expect(outbound).toHaveBeenCalledTimes(1);
      } finally {outbound.mockRestore();}
    }
  });
  it.each([false,true])("denies task writes through a read-only grant under both current and cached names (modern=%s)", async modern => {
    await owner();
    const bearer=await accessToken("owner-a",["workspaces:read","tasks:read"]);
    for(const name of ["delegate_task","delegate_via_luna","continue_task","continue_via_luna"]){
      const args=name.startsWith("delegate")?{workspace_id:"project",request_id:"once",task:"Edit"}:{delegation_id:"saved-task",request_id:"once",findings:"Fix"};
      const attempted=await rpc(bearer,"tools/call",{name,arguments:args},modern);
      expect(attempted.json.result.isError).toBe(true);
      expect(attempted.json.result._meta["mcp/www_authenticate"]).toEqual([expect.stringContaining('scope="tasks:write"')]);
    }
  });
  it("requires explicit browser consent for pairing and protects CSRF", async () => {
    const request=await SELF.fetch(ORIGIN+"/worker/pair",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({label:"My worker",credential_hash:await digest("x".repeat(64)),
      workspaces:[{...workspace,capabilities:[...workspace.capabilities],isolation:[...workspace.isolation],task_types:[...workspace.task_types]}]})});
    expect(request.status).toBe(201);
    const pairing=await request.json<{code:string;claim:string}>();
    const status=await SELF.fetch(ORIGIN+"/worker/pair/"+pairing.code,{headers:{Authorization:"Bearer "+pairing.claim}});
    expect(await status.json()).toMatchObject({status:"pending"});
    const noConsent=await SELF.fetch(ORIGIN+"/pair?code="+pairing.code,{method:"POST",redirect:"manual",body:new URLSearchParams({decision:"approve"})});
    expect(noConsent.status).toBe(302);
    const session=await seal({userId:"owner-a",ownerKey:await digest("owner-a"),csrf:"correct",expires:Date.now()+60_000},env.SESSION_SECRET);
    const headers={Cookie:"__Host-astra-session="+session,Origin:ORIGIN};
    const shown=await SELF.fetch(ORIGIN+"/pair?code="+pairing.code,{headers});
    expect(shown.status).toBe(200);
    expect(shown.headers.get("Referrer-Policy")).toBe("same-origin");
    expect(shown.headers.get("Content-Security-Policy")).toContain("form-action 'self';");
    for(const origin of ["null","https://unapproved.example"]) {
      const crossOrigin=await SELF.fetch(ORIGIN+"/pair?code="+pairing.code,{method:"POST",headers:{...headers,Origin:origin},
        body:new URLSearchParams({decision:"approve",csrf:"correct"})});
      expect(crossOrigin.status).toBe(403);
      expect(await crossOrigin.json()).toMatchObject({error:"origin_denied"});
    }
    const forged=await SELF.fetch(ORIGIN+"/pair?code="+pairing.code,{method:"POST",headers:{Cookie:"__Host-astra-session="+session,Origin:ORIGIN},
      body:new URLSearchParams({decision:"approve",csrf:"wrong"})});
    expect(forged.status).toBe(403);
    const approved=await SELF.fetch(ORIGIN+"/pair?code="+pairing.code,{method:"POST",headers,
      body:new URLSearchParams({decision:"approve",csrf:"correct"})});
    expect(approved.status).toBe(200);
    const complete=await SELF.fetch(ORIGIN+"/worker/pair/"+pairing.code,{headers:{Authorization:"Bearer "+pairing.claim}});
    expect(await complete.json()).toMatchObject({status:"approved",owner_key:await digest("owner-a")});
  });
  it("rejects oversized streamed bodies and callback destinations outside approved hosts", async () => {
    const excessive=await SELF.fetch(ORIGIN+"/worker/pair",{method:"POST",body:"x".repeat(32_769)});
    expect(excessive.status).toBe(413);
    for(const url of ["http://chatgpt.com/callback","https://127.0.0.1/","https://chatgpt.com@evil.test/","https://chatgpt.com:444/","https://chatgpt.com.evil.test/"])
      expect(()=>callbackUrl(url,env)).toThrow();
  });
  it("returns a useful worker authentication error across RPC", async () => {
    const a=await owner();
    const rejected=await SELF.fetch(ORIGIN+"/worker/message",{method:"POST",headers:{Authorization:`Bearer ${a.key}.${a.worker_id}.${"b".repeat(64)}`,
      "Content-Type":"application/json"},body:JSON.stringify({kind:"readiness",readiness:{}})});
    expect(rejected.status).toBe(401);
    expect(await rejected.json()).toMatchObject({error:"invalid_worker_token"});
  });
  it("requires owner consent for record erasure and removes export secrets",async()=>{
    const a=await owner();
    await a.stub.tool("delegate_via_luna",{workspace_id:"project",request_id:"once",task:"Synthetic task"});
    const session=await seal({userId:testPrefix+"owner-a",ownerKey:a.key,csrf:"correct",expires:Date.now()+60_000},env.SESSION_SECRET);
    const headers={Cookie:"__Host-astra-session="+session,Origin:ORIGIN};
    const exported=await SELF.fetch(ORIGIN+"/account/export",{headers});
    expect(exported.status).toBe(200);expect(await exported.text()).not.toContain("credential_hash");
    expect(exported.headers.get("Content-Type")).toContain("application/json");
    expect(exported.headers.get("Content-Disposition")).toBeNull();
    const refused=await SELF.fetch(ORIGIN+"/account/erase",{method:"POST",headers,body:new URLSearchParams({csrf:"correct",confirm:"wrong"})});
    expect(refused.status).toBe(400);
    const erased=await SELF.fetch(ORIGIN+"/account/erase",{method:"POST",headers,body:new URLSearchParams({csrf:"correct",confirm:"ERASE"})});
    expect(erased.status).toBe(200);
    expect((await a.stub.workspaces()).workspaces).toEqual([]);
    expect((await a.stub.exportData()).tasks).toEqual([]);
  });
  it("discovers MCP 2.0 and the compact event catalog", async () => {
    const bearer=await accessToken();
    const discovered=await rpc(bearer,"server/discover",{},true);
    expect(discovered.response.status,JSON.stringify(discovered.json)).toBe(200);
    expect(discovered.json.result.capabilities.events).toBeDefined();
    const listed=await rpc(bearer,"events/list",{},true);
    expect(listed.json.result.events).toMatchObject([{name:"astra.delegation.updated",delivery:["webhook"]}]);
  });
});

describe("signed completion subscriptions",()=>{
  it("verifies a callback and replays terminal-before-subscription with a valid HMAC",async()=>{
    const a=await owner(),bearer=await accessToken(),receipt=await a.stub.tool("delegate_via_luna",{workspace_id:"project",request_id:"once",task:"Create answer.py"});
    await a.stub.workerHttp(a.worker_id,a.secret,{kind:"event",delegation_id:receipt.delegation_id,event:{sequence:1,kind:"completed",terminal:true,
      summary:"Done",task_type:"implementation",verification_required:true},result:{status:"completed"}});
    const secret="whsec_"+btoa("s".repeat(32)),received: {body:string;headers:Headers}[]=[];
    vi.spyOn(globalThis,"fetch").mockImplementation(async(_url,init)=>{
      new Request(String(_url),init);
      expect(init!.redirect).toBe("manual");
      const body=String(init?.body),headers=new Headers(init?.headers),data=JSON.parse(body);
      if(data.type==="verification") return Response.json({challenge:data.challenge});
      received.push({body,headers});return new Response(null,{status:204});
    });
    const input={name:"astra.delegation.updated",arguments:{delegation_id:receipt.delegation_id},delivery:{mode:"webhook",url:"https://chatgpt.com/callback-test",secret},cursor:null};
    const subscription=await a.stub.subscribe(input,bearer,Math.floor(Date.now()/1000)+3600);
    await runInDurableObject(a.stub,async(instance: OwnerRelay)=>instance.alarm());
    expect(received).toHaveLength(1);
    const event=JSON.parse(received[0].body),headers=received[0].headers;
    expect(event.data.summary).toBe("Done");
    expect(headers.get("webhook-id")).toBe(event.eventId);
    expect(headers.get("X-MCP-Subscription-Id")).toBe(subscription.id);
    const key=await crypto.subtle.importKey("raw",new TextEncoder().encode("s".repeat(32)),{name:"HMAC",hash:"SHA-256"},false,["verify"]);
    const signature=Uint8Array.from(atob(headers.get("webhook-signature")!.slice(3)),c=>c.charCodeAt(0));
    expect(await crypto.subtle.verify("HMAC",key,signature,new TextEncoder().encode(`${event.eventId}.${headers.get("webhook-timestamp")}.${received[0].body}`))).toBe(true);
    await evictDurableObject(a.stub);
    await runInDurableObject(a.stub,async(instance: OwnerRelay)=>instance.alarm());
    expect(received).toHaveLength(1);
    await a.stub.unsubscribe({name:input.name,arguments:input.arguments,delivery:{mode:"webhook",url:input.delivery.url}});
  });
  it("refuses redirected callback verification without following it",async()=>{
    const a=await owner(),bearer=await accessToken(),receipt=await a.stub.tool("delegate_via_luna",{workspace_id:"project",request_id:"once",task:"Create answer.py"});
    const calls=vi.spyOn(globalThis,"fetch").mockImplementation(async(url,init)=>{
      new Request(String(url),init);
      expect(init!.redirect).toBe("manual");
      return new Response(null,{status:302,headers:{Location:"https://unapproved.example/callback"}});
    });
    await expectFault(a.stub,instance=>instance.subscribe({name:"astra.delegation.updated",arguments:{delegation_id:receipt.delegation_id},
      delivery:{mode:"webhook",url:"https://chatgpt.com/callback-test",secret:"whsec_"+btoa("s".repeat(32))}},
      bearer,Math.floor(Date.now()/1000)+3600),"Callback verification failed");
    expect(calls).toHaveBeenCalledTimes(1);
  });
  it("stops callbacks after pairing revocation",async()=>{
    const a=await owner(),bearer=await accessToken(),receipt=await a.stub.tool("delegate_via_luna",{workspace_id:"project",request_id:"once",task:"Create answer.py"});
    const calls=vi.spyOn(globalThis,"fetch").mockImplementation(async(_url,init)=>Response.json({challenge:JSON.parse(String(init?.body)).challenge}));
    await a.stub.subscribe({name:"astra.delegation.updated",arguments:{delegation_id:receipt.delegation_id},delivery:{mode:"webhook",url:"https://chatgpt.com/callback-test",secret:"whsec_"+btoa("s".repeat(32))}},bearer,Math.floor(Date.now()/1000)+3600);
    await a.stub.revokeWorker(a.worker_id);
    await runInDurableObject(a.stub,async(instance: OwnerRelay)=>instance.alarm());
    expect(calls).toHaveBeenCalledTimes(1);
  });
  it("refreshes idempotently, caches verification and signs a rotation with both keys",async()=>{
    const a=await owner(),bearer=await accessToken(),receipt=await a.stub.tool("delegate_via_luna",{workspace_id:"project",request_id:"once",task:"Create answer.py"});
    const signatures:string[]=[],calls=vi.spyOn(globalThis,"fetch").mockImplementation(async(_url,init)=>{
      const data=JSON.parse(String(init?.body));
      if(data.type==="verification") return Response.json({challenge:data.challenge});
      signatures.push(new Headers(init?.headers).get("webhook-signature")!);return new Response(null,{status:204});
    });
    const input={name:"astra.delegation.updated",arguments:{delegation_id:receipt.delegation_id},delivery:{mode:"webhook",url:"https://chatgpt.com/callback-test",secret:"whsec_"+btoa("s".repeat(32))}};
    const expiry=Math.floor(Date.now()/1000)+3600;
    const first=await a.stub.subscribe(input,bearer,expiry),again=await a.stub.subscribe(input,bearer,expiry);
    expect(again.id).toBe(first.id);expect(calls).toHaveBeenCalledTimes(1);
    await a.stub.subscribe({...input,delivery:{...input.delivery,secret:"whsec_"+btoa("t".repeat(32))}},bearer,expiry);
    await a.stub.workerHttp(a.worker_id,a.secret,{kind:"event",delegation_id:receipt.delegation_id,event:{sequence:1,kind:"completed",terminal:true,
      summary:"Done",task_type:"implementation",verification_required:true},result:{status:"completed"}});
    await runInDurableObject(a.stub,async(instance: OwnerRelay)=>instance.alarm());
    expect(signatures[0].split(" ")).toHaveLength(2);
    const refreshed=await a.stub.subscribe(input,bearer,expiry);
    expect(refreshed.cursor).toBe("1");
  });
  it("retries stable IDs and stops after the bounded attempt count",async()=>{
    const a=await owner(),bearer=await accessToken(),receipt=await a.stub.tool("delegate_via_luna",{workspace_id:"project",request_id:"once",task:"Create answer.py"});
    const ids:string[]=[];
    vi.spyOn(globalThis,"fetch").mockImplementation(async(_url,init)=>{
      const data=JSON.parse(String(init?.body));if(data.type==="verification") return Response.json({challenge:data.challenge});
      ids.push(new Headers(init?.headers).get("webhook-id")!);return new Response(null,{status:503});
    });
    await a.stub.subscribe({name:"astra.delegation.updated",arguments:{delegation_id:receipt.delegation_id},delivery:{mode:"webhook",url:"https://chatgpt.com/callback-test",secret:"whsec_"+btoa("s".repeat(32))}},bearer,Math.floor(Date.now()/1000)+3600);
    await a.stub.workerHttp(a.worker_id,a.secret,{kind:"event",delegation_id:receipt.delegation_id,event:{sequence:1,kind:"completed",terminal:true,
      summary:"Done",task_type:"implementation",verification_required:true},result:{status:"completed"}});
    for(let attempt=0;attempt<6;attempt++) await runInDurableObject(a.stub,async(instance: OwnerRelay,state)=>{
      state.storage.sql.exec("UPDATE subscriptions SET next_attempt=0");await instance.alarm();
    });
    expect(ids).toHaveLength(5);expect(new Set(ids).size).toBe(1);
  });
  it("stops delivery after the OAuth grant is revoked",async()=>{
    const a=await owner(),bearer=await accessToken(),receipt=await a.stub.tool("delegate_via_luna",{workspace_id:"project",request_id:"once",task:"Create answer.py"});
    const calls=vi.spyOn(globalThis,"fetch").mockImplementation(async(_url,init)=>Response.json({challenge:JSON.parse(String(init?.body)).challenge}));
    await a.stub.subscribe({name:"astra.delegation.updated",arguments:{delegation_id:receipt.delegation_id},delivery:{mode:"webhook",url:"https://chatgpt.com/callback-test",secret:"whsec_"+btoa("s".repeat(32))}},bearer,Math.floor(Date.now()/1000)+3600);
    const helpers=authorizationServer(env).getOAuthApi(env),grants=await helpers.listUserGrants(testPrefix+"owner-a");
    for(const grant of grants.items) await helpers.revokeGrant(grant.id,testPrefix+"owner-a");
    await a.stub.workerHttp(a.worker_id,a.secret,{kind:"event",delegation_id:receipt.delegation_id,event:{sequence:1,kind:"completed",terminal:true,
      summary:"Done",task_type:"implementation",verification_required:true},result:{status:"completed"}});
    await runInDurableObject(a.stub,async(instance: OwnerRelay)=>instance.alarm());
    expect(calls).toHaveBeenCalledTimes(1);
  });
});


describe("ChatGPT component contract", () => {
  it("advertises a versioned render resource and hides refresh/export tools from the model",async()=>{
    const bearer=await accessToken();
    const listed=await rpc(bearer,"tools/list",{},true);
    const tools=listed.json.result.tools as Array<Record<string,any>>;
    expect(tools.find(tool=>tool.name==="show_delegation")).toMatchObject({
      _meta:{ui:{resourceUri:"ui://astra/delegation-v1-0.17.0.html"}},outputSchema:{type:"object"}
    });
    for (const name of ["watch_delegation_ui","read_artifact_ui"])
      expect(tools.find(tool=>tool.name===name)?._meta.ui.visibility).toEqual(["app"]);
    const resource=await rpc(bearer,"resources/read",{uri:"ui://astra/delegation-v1-0.17.0.html"},true);
    expect(resource.json.result.contents[0]).toMatchObject({mimeType:"text/html;profile=mcp-app",
      _meta:{ui:{prefersBorder:true,domain:ORIGIN,csp:{connectDomains:[ORIGIN],resourceDomains:[]}},"openai/ui":{availableDisplayModes:["inline","fullscreen"]},"openai/widgetCSP":{connect_domains:[ORIGIN],redirect_domains:[]}}
    });
    expect(resource.json.result.contents[0].text).toContain("<script>");
    expect(resource.json.result.contents[0].text).not.toContain('<script src=');
    expect(resource.json.result.contents[0].text).toContain("Work finished");
    const nativeFileCompatibility=await rpc(bearer,"resources/read",{uri:"ui://astra/delegation-v1-0.16.0.html"},true);
    expect(await digest(nativeFileCompatibility.json.result.contents[0].text)).toBe("e2f8e9b2e6548be1dce9e14d04ecbe27281bdde518c7a89da69a1fb8b077f821");
    expect(nativeFileCompatibility.json.result.contents[0]).toMatchObject({mimeType:"text/html;profile=mcp-app",
      _meta:{ui:{prefersBorder:true,domain:ORIGIN,csp:{connectDomains:[ORIGIN],resourceDomains:[]}},"openai/ui":{availableDisplayModes:["inline","fullscreen"]},"openai/widgetCSP":{connect_domains:[ORIGIN],redirect_domains:[]}}
    });
    const formerlyCurrent=await rpc(bearer,"resources/read",{uri:"ui://astra/delegation-v1-0.15.1.html"},true);
    expect(await digest(formerlyCurrent.json.result.contents[0].text)).toBe("3878a363c9f4b17b5429ffb9283675d2820f41464410bd2a69cb87c3309eb5e2");
    expect(formerlyCurrent.json.result.contents[0]).toMatchObject({_meta:{"openai/ui":{availableDisplayModes:["inline"]},"openai/widgetCSP":{connect_domains:[ORIGIN],redirect_domains:[ORIGIN]}}});
    const last=await rpc(bearer,"resources/read",{uri:"ui://astra/delegation-v1-0.14.0.html"},true);
    expect(last.json.result.contents[0].text).toContain("Worker completed");
    expect(last.json.result.contents[0].text).not.toContain("Task status unavailable");
    expect(last.json.result.contents[0].text).not.toBe(resource.json.result.contents[0].text);
    const frozen=await rpc(bearer,"resources/read",{uri:"ui://astra/delegation-v1-0.12.1.html"},true);
    expect(frozen.json.result.contents[0].text).toContain("Independent review is pending until ChatGPT checks the work.");
    expect(frozen.json.result.contents[0].text).not.toBe(resource.json.result.contents[0].text);
    const previous=await rpc(bearer,"resources/read",{uri:"ui://astra/delegation-v1.html"},true);
    expect(previous.json.result.contents[0].uri).toBe("ui://astra/delegation-v1.html");
    expect(previous.json.result.contents[0].text).toBe(frozen.json.result.contents[0].text);
  });

  it("hydrates metadata without putting refresh snapshots in the parent transcript",async()=>{
    const bearer=await accessToken();
    const shown=await rpc(bearer,"tools/call",{name:"show_delegation",arguments:{}},true);
    expect(shown.json.result.structuredContent).toMatchObject({status:"setup_required"});
    expect(shown.json.result._meta.astra_ui).toMatchObject({version:1,connection:{state:"setup_required"}});
    expect(Object.keys(shown.json.result.structuredContent).sort()).toEqual(["status","summary"]);
    const refreshed=await rpc(bearer,"tools/call",{name:"watch_delegation_ui",arguments:{wait_ms:0}},true);
    expect(refreshed.json.result.content).toEqual([]);
    expect(refreshed.json.result.structuredContent).toBeUndefined();
    expect(refreshed.json.result._meta.astra_ui.version).toBe(1);
  });

  it("derives card permissions from each current grant without adding them to model context",async()=>{
    const readOnly=await accessToken("card-permissions",["workspaces:read","tasks:read"]);
    const writable=await accessToken("card-permissions");
    for (const [bearer,canWrite] of [[readOnly,false],[writable,true]] as const) {
      for (const name of ["show_delegation","watch_delegation_ui"]) {
        const response=await rpc(bearer,"tools/call",{name,arguments:name === "watch_delegation_ui" ? {wait_ms:0} : {}},true);
        const result=response.json.result;
        expect(result._meta.astra_ui.permissions).toEqual({can_write_tasks:canWrite});
        expect(JSON.stringify(result.content)).not.toContain("can_write_tasks");
        expect(JSON.stringify(result.structuredContent) ?? "").not.toContain("permissions");
      }
    }
    const denied=await rpc(readOnly,"tools/call",{name:"cancel_delegation",arguments:{delegation_id:"unstarted-task"}},true);
    expect(denied.json.result.isError).toBe(true);
    expect(denied.json.result._meta["mcp/www_authenticate"][0]).toContain("tasks:write");
  });

  it("requires connection-read scope and rejects return URL injection",async()=>{
    const bearer=await accessToken("limited",["tasks:read"]);
    const shown=await rpc(bearer,"tools/call",{name:"show_delegation",arguments:{}},true);
    expect(shown.json.result.isError).toBe(true);
    expect(shown.json.result._meta["mcp/www_authenticate"][0]).toContain("workspaces:read");
    for (const next of ["//attacker.test/", "/downloads/../account", "/renew?code="+"a".repeat(32)+"&next=//attacker.test"]){
      const reply=await SELF.fetch(ORIGIN+"/auth/login?next="+encodeURIComponent(next));
      expect(reply.status).toBe(400);
    }
    for (const path of ["/downloads/task/artifact","/renew?code="+"a".repeat(32)]) {
      const reply=await SELF.fetch(ORIGIN+path,{redirect:"manual"});
      expect(reply.status).toBe(302);
      expect(reply.headers.get("Location")).toContain("/auth/login?next=");
    }
  });

  it("renders a useful browser error while preserving JSON API errors",async()=>{
    const path=ORIGIN+"/renew?code=bad";
    const session=await seal({userId:"owner",ownerKey:await digest("owner"),csrf:"csrf",expires:Date.now()+60_000},env.SESSION_SECRET);
    const browser=await SELF.fetch(path,{headers:{Accept:"text/html",Cookie:"__Host-astra-session="+session}});
    expect(browser.status).toBe(400);
    expect(browser.headers.get("Content-Type")).toContain("text/html");
    expect(await browser.text()).toContain("Your saved task has not been restarted");
    const api=await SELF.fetch(path,{headers:{Cookie:"__Host-astra-session="+session}});
    expect(api.headers.get("Content-Type")).toContain("application/json");
  });
});


describe("retired browser file links and renewal boundaries",()=>{
  it("keeps old browser file links authenticated and owned without authorizing transfer",async()=>{
    const a=await owner(), b=await owner("different");
    const task=await a.stub.tool("delegate_via_luna",{workspace_id:"project",request_id:"download-route",task:"Return an answer"});
    const id=String(task.delegation_id), sha=await digest("42");
    await a.stub.workerHttp(a.worker_id,a.secret,{kind:"readiness",readiness:{}});
    await a.stub.workerHttp(a.worker_id,a.secret,{kind:"receipt",delegation_id:id,local_id:"local"});
    await a.stub.workerHttp(a.worker_id,a.secret,{kind:"event",delegation_id:id,event:{sequence:1,kind:"completed",terminal:true,
      summary:"Answer saved",task_type:"implementation",verification_required:true},result:{status:"completed",artifact_refs:[{
        artifact_id:"answer",path:"answer.txt",bytes:2,sha256:sha,export_available:true
      }]}});
    await runInDurableObject(a.stub,async(_instance,state)=>{
      state.storage.sql.exec("INSERT INTO artifacts(job_id,id,payload) VALUES(?,?,?)",id,"answer",JSON.stringify({id:"answer",bytes:2,sha256:sha,encoding:"base64",content:"NDI="}));
    });
    const cookie=await seal({userId:"owner",ownerKey:a.key,csrf:"download-consent",expires:Date.now()+60_000},env.SESSION_SECRET);
    const headers={Cookie:"__Host-astra-session="+cookie,Origin:ORIGIN};
    const url=ORIGIN+"/downloads/"+id+"/answer";
    const unsigned=await SELF.fetch(url,{redirect:"manual"});
    expect(unsigned.status).toBe(302);
    expect(unsigned.headers.get("Location")).toBe(ORIGIN+"/auth/login?next="+encodeURIComponent("/downloads/"+id+"/answer"));
    const preview=await SELF.fetch(url,{headers});
    expect(preview.status).toBe(200);expect(await preview.text()).toContain("Open your file in ChatGPT");
    expect((await SELF.fetch(url,{method:"POST",headers,body:new URLSearchParams({csrf:"wrong"})})).status).toBe(403);
    const continued=await SELF.fetch(url,{method:"POST",headers,body:new URLSearchParams({csrf:"download-consent"})});
    expect(continued.status).toBe(200);expect(await continued.text()).toContain("This page does not start a file transfer");
    expect(continued.headers.get("Content-Disposition")).toBeNull();
    expect(continued.headers.get("Cache-Control")).toBe("no-store");
    expect(continued.headers.get("X-Content-Type-Options")).toBe("nosniff");
    const grants=await runInDurableObject(a.stub,async(_instance,state)=>state.storage.sql.exec("SELECT id FROM download_grants").toArray());
    expect(grants).toEqual([]);
    const other=await seal({userId:"other",ownerKey:b.key,csrf:"other",expires:Date.now()+60_000},env.SESSION_SECRET);
    expect((await SELF.fetch(url,{headers:{Cookie:"__Host-astra-session="+other}})).status).toBe(404);
  });

  it("renews the same identity through explicit browser approval and recovers its claim",async()=>{
    const a=await owner();
    const Authorization=`Bearer ${a.key}.${a.worker_id}.${a.secret}`;
    const begun=await SELF.fetch(ORIGIN+"/worker/renew",{method:"POST",headers:{Authorization,"Content-Type":"application/json"},body:JSON.stringify({credential_hash:await digest("b".repeat(64))})});
    expect(begun.status).toBe(201);
    const pending=await begun.json<{renewal_id:string;verification_url:string}>();
    expect(pending.renewal_id).toMatch(/^[a-f0-9]{32}$/);
    const cookie=await seal({userId:"owner",ownerKey:a.key,csrf:"renew-consent",expires:Date.now()+60_000},env.SESSION_SECRET);
    const headers={Cookie:"__Host-astra-session="+cookie,Origin:ORIGIN};
    const preview=await SELF.fetch(pending.verification_url,{headers});expect(preview.status).toBe(200);
    expect(await preview.text()).toContain("Renew access");
    expect((await SELF.fetch(pending.verification_url,{method:"POST",headers,body:new URLSearchParams({decision:"approve",csrf:"wrong"})})).status).toBe(403);
    const approved=await SELF.fetch(pending.verification_url,{method:"POST",headers,body:new URLSearchParams({decision:"approve",csrf:"renew-consent"})});
    expect(approved.status).toBe(200);
    const claimed=await SELF.fetch(ORIGIN+"/worker/renew/"+pending.renewal_id,{method:"POST",headers:{Authorization}});
    expect(await claimed.json()).toMatchObject({status:"approved",worker_id:a.worker_id,owner_key:a.key});
  });
});
