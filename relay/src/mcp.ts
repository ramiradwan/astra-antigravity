import { createMcpHandler, fromJsonSchema, McpServer, ProtocolError, type JsonSchemaType } from "@modelcontextprotocol/server";
import { z } from "zod";
import { descriptions, inputs, MAX_BODY_BYTES, object, relayError, RelayError, scopeFor, type ToolName, type ObjectValue } from "./contracts";
import { allUserGrants, authorizationServer, revokeAllUserGrants, type AuthProps } from "./auth";
import { publicOrigin } from "./security";
import delegationHtml from "../ui/dist/delegation.html";
import previousCurrentDelegationHtml from "../ui/compat/delegation-0.16.0.html";
import latestPreviousDelegationHtml from "../ui/compat/delegation-0.15.1.html";
import currentPreviousDelegationHtml from "../ui/compat/delegation-0.15.0.html";
import earlierDelegationHtml from "../ui/compat/delegation-0.14.1.html";
import lastDelegationHtml from "../ui/compat/delegation-0.14.0.html";
import previousDelegationHtml from "../ui/compat/delegation-0.12.1.html";

// Hosts cache component HTML by URI. Give each shipped bundle its own address,
// while retaining the previous address for already connected clients.
export const UI_RESOURCE = "ui://astra/delegation-v1-0.17.0.html";
const UI_RESOURCES = [
  { uri: UI_RESOURCE, text: delegationHtml },
  { uri: "ui://astra/delegation-v1-0.16.0.html", text: previousCurrentDelegationHtml },
  { uri:"ui://astra/delegation-v1-0.15.1.html",text:latestPreviousDelegationHtml },
  { uri:"ui://astra/delegation-v1-0.15.0.html",text:currentPreviousDelegationHtml },
  { uri:"ui://astra/delegation-v1-0.14.1.html",text:earlierDelegationHtml },
  { uri: "ui://astra/delegation-v1-0.14.0.html", text: lastDelegationHtml },
  { uri: "ui://astra/delegation-v1-0.12.1.html", text: previousDelegationHtml },
  { uri: "ui://astra/delegation-v1.html", text: previousDelegationHtml }
];
// Preserve the metadata shipped with cached native-file cards as well as HTML.
// Earlier compatibility cards retain their original inline/browser policy.
const IN_CHAT_RESOURCES = new Set([UI_RESOURCE, "ui://astra/delegation-v1-0.16.0.html"]);
const uiSummarySchema = z.object({delegation_id:z.string().optional(),status:z.string(),summary:z.string().max(600)}).strict();

export const EVENT_NAME = "astra.delegation.updated";
export const EVENT_DEFINITION = {
  name: EVENT_NAME,
  description: "A compact decision or terminal outcome for an owned delegated task. Logs and model reasoning are excluded.",
  delivery: ["webhook"],
  inputSchema: z.toJSONSchema(z.object({ delegation_id: z.string() }).strict()),
  payloadSchema: z.toJSONSchema(z.object({ delegation_id: z.string(), event_id: z.string(), sequence: z.number().int(),
    workspace_id: z.string(), kind: z.string(), terminal: z.boolean(), summary: z.string().max(1_600), task_type: z.string(),
    model: z.string().nullable().optional(), verification_required: z.boolean(), checks_count: z.number().int().optional(),
    artifacts_count: z.number().int().optional() }).strict())
};

// Authentication is completed by OAuthResourceServer before this function.
export async function serveMcp(request: Request, env: Env, auth: { props: AuthProps; token: string; expiresAt?: number; scope: string[] }): Promise<Response> {
  const owner = env.OWNERS.getByName(auth.props.ownerKey);
  const handler = createMcpHandler(() => {
    const capabilities = { tools: {}, events: {}, resources: {} };
    const server = new McpServer({ name: "Astra Antigravity Relay", version: "0.17.0" }, {
      capabilities,
      instructions: "Delegate only the authorized task to a registered workspace. Preserve launch IDs. Use a deterministic host receiver or an authorized completion subscription; continue independent work. Read the bounded handoff once when needed and independently verify results. Provider credentials and raw transcripts stay on the worker. Unsupported delivery hosts must disclose their limitation." });
    for (const [index, { uri, text }] of UI_RESOURCES.entries()) server.registerResource(index === 0 ? "delegation-card" : `delegation-card-previous-${index}`, uri, {mimeType:"text/html;profile=mcp-app",description:"Connection, task status, decisions and results."}, async () => ({
      contents:[{uri,mimeType:"text/html;profile=mcp-app",text,_meta:{
        ui:{prefersBorder:true,domain:publicOrigin(env),csp:{connectDomains:[publicOrigin(env)],resourceDomains:[]}},
        "openai/ui":{availableDisplayModes:IN_CHAT_RESOURCES.has(uri)?["inline","fullscreen"]:["inline"]},
        "openai/widgetCSP":{connect_domains:[publicOrigin(env)],redirect_domains:IN_CHAT_RESOURCES.has(uri)?[]:[publicOrigin(env)]},
        "openai/widgetDescription":"Connection, task status, decisions and results for the user's computer. The card refreshes itself; narrate only decisions and meaningful outcomes."
      }}]
    }));
    const catalog: Record<string,unknown>[] = [];
    for (const [name, schema] of Object.entries(inputs)) {
      const tool = name as ToolName;
      const publicName = name === "delegate_via_luna" ? "delegate_task" : name === "continue_via_luna" ? "continue_task" : name;
      const needed = scopeFor(tool);
      const destructive = ["delegate_via_luna", "prepare_delegation", "continue_via_luna", "cancel_delegation", "cleanup_task_worktree","confirm_operation"].includes(name);
      // Schemas are generated here, not accepted from a caller. A JSON roundtrip
      // bridges the libraries' differing TS-only $vocabulary declarations.
      const wireSchema: JsonSchemaType = JSON.parse(JSON.stringify(z.toJSONSchema(schema, { io: "input" })));
      const schemes = [{ type: "oauth2", scopes: [needed] }];
      const config = { description: descriptions[tool], inputSchema: fromJsonSchema(wireSchema),
        annotations: { readOnlyHint: !["delegate_via_luna", "prepare_delegation", "continue_via_luna", "cancel_delegation", "cleanup_task_worktree", "ack_delegation_events", "read_artifact","prepare_operation","confirm_operation","prepare_artifact_download_ui","approve_pairing_ui"].includes(name),
          destructiveHint: destructive, openWorldHint: ["delegate_via_luna", "prepare_delegation", "continue_via_luna","confirm_operation"].includes(name), idempotentHint: ["delegate_via_luna", "prepare_delegation", "continue_via_luna","prepare_operation","confirm_operation"].includes(name) },
        ...(["show_delegation","show_computers"].includes(name) ? {outputSchema:uiSummarySchema} : {}),
        _meta: { securitySchemes: schemes,
          ...(name === "show_delegation" ? {ui:{resourceUri:UI_RESOURCE},"openai/toolInvocation/invoking":"Opening task cardâ€¦","openai/toolInvocation/invoked":"Task card ready"} : {}),
          ...(name === "show_computers" ? {ui:{resourceUri:UI_RESOURCE},"openai/ui":{entrypoints:[{type:"global"},{type:"thread"}]},"openai/toolInvocation/invoking":"Opening computersâ€¦","openai/toolInvocation/invoked":"Computers ready"} : {}),
          ...(["watch_delegation_ui","read_artifact_ui","watch_computers_ui","prepare_operation","confirm_operation","inspect_operation","export_account_ui","prepare_artifact_download_ui","approve_pairing_ui"].includes(name) ? {ui:{visibility:["app"]}} : {}) } };
      catalog.push({name:publicName,description:config.description,inputSchema:wireSchema,annotations:config.annotations,securitySchemes:schemes,_meta:config._meta,
        ...(["show_delegation","show_computers"].includes(name) ? {outputSchema:z.toJSONSchema(uiSummarySchema)} : {})});
      // Cached clients may still call the old name. Both names use the same
      // internal operation and request fingerprint; only the neutral name is listed.
      for (const callName of new Set([publicName,name])) server.registerTool(callName, config, async (raw: unknown) => {
        const extra = name==="export_account_ui"||["show_delegation","watch_delegation_ui"].includes(name) && !object(raw).delegation_id ? "workspaces:read" : null;
        let missing = !auth.scope.includes(needed) ? needed : extra && !auth.scope.includes(extra) ? extra : null;
        if(!missing&&name==="prepare_operation"&&object(raw).action==="delete_account"&&!auth.scope.includes("account:delete"))missing="account:delete";
        if(!missing&&name==="confirm_operation"&&!auth.scope.includes("account:delete")){
          const operation=object(await owner.toolRequest("inspect_operation",{operation_id:object(raw).operation_id}));
          if(operation.action==="delete_account")missing="account:delete";
        }
        if (missing) return { isError: true, content: [{ type: "text", text: "The tool requires additional authorization." }],
          _meta: { "mcp/www_authenticate": [`Bearer resource_metadata="${publicOrigin(env)}/.well-known/oauth-protected-resource/mcp", scope="${missing}", error="insufficient_scope", error_description="This tool needs additional authorization"`] } };
        try {
          let result:ObjectValue;
          if(name==="approve_pairing_ui"){
            const input=inputs.approve_pairing_ui.parse(raw),pending=env.PAIRINGS.getByName(input.code),details=await pending.details();
            if(!details.setup_only||details.workspaces.length)throw new RelayError("pairing_permissions","This pairing includes workspace access. Review its full permissions on the pairing page instead of approving management-only setup.",409);
            const paired=await pending.approve(auth.props.ownerKey,true,input.request_id);
            result={operation_id:"pair_"+input.code,computer_id:paired.worker_id,action:"approve_pairing",state:"completed",summary:"Computer management approved until revoked. Task work starts only after a folder is separately approved and provider accounts are ready.",updated_at:new Date().toISOString()};
          } else result = object(await owner.toolRequest(tool, raw));
          if(!result.relay_error&&name==="confirm_operation"&&result.account_delete_confirmed===true){
            await revokeAllUserGrants(authorizationServer(env).getOAuthApi(env),auth.props.userId);
            await owner.eraseData();
            result={operation_id:result.operation_id,action:"delete_account",state:"completed",summary:"Relay records and all current application authorizations were removed. Local files, companion software and provider history remain.",updated_at:new Date().toISOString()};
          }
          if(!result.relay_error&&["show_computers","watch_computers_ui"].includes(name)){
            const summary=uiSummarySchema.parse({status:"ready",summary:`${Array.isArray(result.computers)?result.computers.length:0} connected computer records. Manage computers and settings in this card.`});
            return {content:name==="show_computers"?[{type:"text",text:summary.summary}]:[],...(name==="show_computers"?{structuredContent:summary}:{}),_meta:{astra_control:{...result,tasks:auth.scope.includes("tasks:read")?result.tasks:[],computers:Array.isArray(result.computers)?result.computers.map(raw=>({...object(raw),allowed_actions:auth.scope.includes("computers:manage")?object(raw).allowed_actions:[]})):[],permissions:{can_manage:auth.scope.includes("computers:manage"),can_delete_account:auth.scope.includes("computers:manage")&&auth.scope.includes("account:delete"),can_write_tasks:auth.scope.includes("tasks:write")}}}};
          }
          if(!result.relay_error&&["prepare_operation","confirm_operation","inspect_operation","approve_pairing_ui"].includes(name))return {content:[],_meta:{astra_operation:result}};
          if(!result.relay_error&&name==="prepare_artifact_download_ui")return {content:[],_meta:{astra_download:result}};
          if(!result.relay_error&&name==="export_account_ui"){
            const grants=await allUserGrants(authorizationServer(env).getOAuthApi(env),auth.props.userId);
            const bytes=new TextEncoder().encode(JSON.stringify({...result,authorizations:grants.map(({id,clientId,scope,createdAt,expiresAt,resource})=>({id,clientId,scope,createdAt,expiresAt,resource}))},null,2));
            let binary="";for(let at=0;at<bytes.length;at+=16_384)binary+=String.fromCharCode(...bytes.subarray(at,at+16_384));
            return {content:[],_meta:{astra_export:{name:"antigravity-relay-records.json",mime_type:"application/json",encoding:"base64",content:btoa(binary)}}};
          }
          if (!result.relay_error && ["show_delegation","watch_delegation_ui"].includes(name)) {
            const summary=uiSummarySchema.parse({...(result.delegation_id ? {delegation_id:result.delegation_id} : {}),status:result.status,summary:String(result.summary).slice(0,600)});
            return {content:name === "show_delegation" ? [{type:"text",text:summary.summary}] : [],
              ...(name === "show_delegation" ? {structuredContent:summary} : {}),
              _meta:{astra_ui:{...result,permissions:{can_write_tasks:auth.scope.includes("tasks:write")}}}};
          }
          if (!result.relay_error && name === "read_artifact_ui") return {content:[],_meta:{astra_artifact:result.artifact ?? null,status:result.status ?? "ready"}};
          return { ...(result.relay_error ? {isError:true} : {}), content: [{ type: "text", text: JSON.stringify(result) }] };
        } catch (error) {
          const failure = relayError(error);
          return { isError: true, content: [{ type: "text", text: JSON.stringify({ error: failure?.code ?? "request_rejected",
            message: failure?.message ?? "The request could not be completed; inspect the owned receipt." }) }] };
        }
      });
    }
    // The SDK's high-level tool config doesn't retain OpenAI's root extension.
    // Publish it explicitly alongside the backwards-compatible _meta mirror.
    server.server.setRequestHandler("tools/list",{params:z.object({cursor:z.string().optional()}).strict(),result:z.object({tools:z.array(z.record(z.string(),z.unknown()))})},
      async()=>({tools:catalog}));
    server.server.setRequestHandler("events/list", { params: z.object({}).strict(), result: z.object({ events: z.array(z.record(z.string(), z.unknown())) }) },
      async () => ({ events: auth.scope.includes("tasks:read") ? [EVENT_DEFINITION] : [] }));
    server.server.setRequestHandler("events/subscribe", { params: z.record(z.string(), z.unknown()), result: z.record(z.string(), z.unknown()) },
      async params => {
        if (!auth.scope.includes("tasks:read") || !auth.expiresAt) throw new RelayError("insufficient_scope", "Task read authorization required", 403);
        const result = object(await owner.eventRequest("subscribe", params, auth.token, auth.expiresAt));
        if (result.relay_error) throw new ProtocolError(result.code === "callback_verification" ? -32015 : -32602, String(result.message), {reason: result.code});
        return result;
      });
    server.server.setRequestHandler("events/unsubscribe", { params: z.record(z.string(), z.unknown()), result: z.record(z.string(), z.unknown()) },
      async params => {
        if (!auth.scope.includes("tasks:read")) throw new RelayError("insufficient_scope", "Task read authorization required", 403);
        const result = object(await owner.eventRequest("unsubscribe",params));
        if(result.relay_error) throw new ProtocolError(-32602,String(result.message),{reason:result.code});
        return result;
      });
    return server;
  }, { responseMode: "json", maxRequestBodySize: MAX_BODY_BYTES, keepAliveMs: 0 });
  try { return await handler.fetch(request); }
  finally { await handler.close(); }
}
