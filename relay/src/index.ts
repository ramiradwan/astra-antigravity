export { OwnerRelay, WorkerPairing } from "./broker";
export { DirectoryMembership } from "./directory";

import { OAuthResourceServer } from "@cloudflare/workers-oauth-provider";
import { accountMethod, allUserGrants, authConfigured, authPage, authorizationServer, managementSession, requireManagementPost, revokeAllUserGrants, type AuthProps } from "./auth";
import { pairingSchema } from "./broker";
import { boundedBytes, boundedJson, ID, object, relayError, RelayError, token, SCOPES } from "./contracts";
import { z } from "zod";
import { html, page, publicOrigin, unsealArtifactDownload } from "./security";
import { serveMcp } from "./mcp";
import { legalPage } from "./legal";
import { directoryHook } from "./directory";
import { companionSetupSettings, setupCommandForRequest } from "./companion-setup";

function workerCredential(request: Request): { owner: string; worker: string; secret: string } {
  const match = /^Bearer ([a-f0-9]{64})\.([a-f0-9]{32})\.([a-f0-9]{64})$/.exec(request.headers.get("Authorization") ?? "");
  if (!match) throw new RelayError("invalid_worker_token", "Worker authentication required", 401);
  return { owner: match[1], worker: match[2], secret: match[3] };
}

type BrowserRecovery = { retryPath?: string };
function artifactTransferGet(request: Request): boolean {
  return request.method === "GET" && /^\/artifact-download\/[^/]{1,4096}$/.test(new URL(request.url).pathname);
}
function artifactTransferCors(request: Request, response: Response): Response {
  if (!artifactTransferGet(request)) return response;
  const headers = new Headers(response.headers);
  // The sealed, short-lived, one-use capability is the only authority here.
  // Wildcard CORS deliberately does not permit credentialed browser requests.
  headers.set("Access-Control-Allow-Origin", "*");
  headers.set("Cache-Control", "no-store");
  return new Response(response.body, { status: response.status, statusText: response.statusText, headers });
}
async function fetchRelay(request: Request, env: Env, ctx: ExecutionContext, recovery: BrowserRecovery): Promise<Response> {
  const origin = publicOrigin(env), url = new URL(request.url), server = authorizationServer(env);
  if (url.origin !== origin) throw new RelayError("wrong_origin", "Request must use the configured relay origin", 400);
  if (request.headers.has("Origin") && ![origin, "https://chatgpt.com"].includes(request.headers.get("Origin")!) && !artifactTransferGet(request)) throw new RelayError("origin_denied", "Origin is not allowed", 403);
  const length = request.headers.get("Content-Length");
  if (length && Number(length) > 32_768) throw new RelayError("payload_too_large", "Request exceeds 32 KiB", 413);
  if (request.body) request = new Request(request, { body: await boundedBytes(request) });
  if (url.pathname.startsWith("/directory/"))
    return await directoryHook(request, env) ?? new Response("Not found", { status: 404 });
  if (url.pathname === "/support") {
    if (!["GET", "HEAD"].includes(request.method))
      return new Response(null, { status: 405, headers: { Allow: "GET, HEAD" } });
    return new Response(null, { status: 302, headers: {
      Location: "https://www.linkedin.com/in/radwan-rami/",
      "Cache-Control": "no-store", "Referrer-Policy": "no-referrer",
      "X-Content-Type-Options": "nosniff"
    } });
  }
  if (["/privacy", "/terms", "/license"].includes(url.pathname))
    return request.method === "GET" ? legalPage(url.pathname) : new Response(null, { status: 405, headers: { Allow: "GET" } });
  if (url.pathname === "/.well-known/openai-apps-challenge") {
    const challenge = env.OPENAI_APPS_CHALLENGE;
    const headers = { "Content-Type": "text/plain; charset=utf-8", "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff" };
    if (challenge === undefined || challenge === "") return new Response("Not found", { status: 404, headers });
    if (typeof challenge !== "string" || !/^[A-Za-z0-9_-]{43}$/.test(challenge))
      return new Response("Domain verification is not configured correctly", { status: 503, headers });
    if (request.method !== "GET") return new Response("Method not allowed", { status: 405, headers: { ...headers, Allow: "GET" } });
    return new Response(challenge, { headers });
  }
  if (url.pathname === "/mcp" || url.pathname.startsWith("/.well-known/oauth-protected-resource")) {
    const resource = new OAuthResourceServer<Env, AuthProps>({ resourceMetadata: { resource: origin + "/mcp", authorization_servers: [origin] },
      requiredScopes: [...SCOPES],
      validateToken: actual => (audience, bearer) => server.validateToken<AuthProps>(audience, bearer, actual),
      handler: { fetch: (req, actual, context) => serveMcp(req, actual, { props: context.props, token: context.auth.token,
        expiresAt: context.auth.expiresAt, scope: context.auth.scope }) } });
    return resource.fetch(request, env, ctx);
  }
  if (url.pathname === "/worker/pair" && request.method === "POST") {
    const input = pairingSchema.parse(await boundedJson(request));
    const code = token(12);
    const receipt = await env.PAIRINGS.getByName(code).create(input);
    return Response.json({ code, claim: receipt.claim, expires: receipt.expires, verification_url: origin + "/pair?code=" + code }, { status: 201, headers: { "Cache-Control": "no-store" } });
  }
  const pairing = /^\/worker\/pair\/([a-f0-9]{24})$/.exec(url.pathname);
  if (pairing && request.method === "GET") {
    const claim = /^Bearer ([a-f0-9]{64})$/.exec(request.headers.get("Authorization") ?? "");
    if (!claim) throw new RelayError("invalid_pairing_claim", "Pairing proof required", 401);
    return Response.json(await env.PAIRINGS.getByName(pairing[1]).status(claim[1]), { headers: { "Cache-Control": "no-store" } });
  }
  if(url.pathname==="/worker/credentials/rotate"&&request.method==="POST"){
    const credential=workerCredential(request);
    return Response.json(await env.OWNERS.getByName(credential.owner).rotateWorkerCredentials(credential.worker,credential.secret,await boundedJson(request)),{headers:{"Cache-Control":"no-store"}});
  }
  const removal=/^\/worker\/removals\/([a-f0-9]{64})\/([a-f0-9]{32})\/complete$/.exec(url.pathname);
  if(removal&&request.method==="POST")return Response.json(await env.OWNERS.getByName(removal[1]).completeRemoval(removal[2],await boundedJson(request)),{headers:{"Cache-Control":"no-store"}});
  const hostDownload=/^\/artifact-download\/([^/]{1,4096})$/.exec(url.pathname);
  if(hostDownload&&request.method==="GET"){
    if (request.headers.get("Sec-Fetch-Mode")?.toLowerCase() === "navigate" || request.headers.get("Sec-Fetch-Dest")?.toLowerCase() === "document")
      throw new RelayError("browser_file_delivery_disabled", "Return to your task in ChatGPT to open this file. This page does not start a file transfer.", 403);
    const grant=await unsealArtifactDownload(decodeURIComponent(hostDownload[1]),env.SESSION_SECRET);
    if(grant.purpose!=="artifact_download"||typeof grant.owner!=="string"||!/^[a-f0-9]{64}$/.test(grant.owner)||typeof grant.expires!=="number"||grant.expires<=Date.now())throw new RelayError("download_expired","Download grant expired",410);
    const owner=env.OWNERS.getByName(grant.owner),details=await owner.downloadDetails(ID.parse(grant.grant));
    if(details.delegation_id!==grant.job||details.artifact_id!==grant.artifact||details.sha256!==grant.sha256||details.size!==grant.size)throw new RelayError("artifact_changed","Download grant no longer matches this artifact",409);
    const body=await owner.approveDownload(ID.parse(grant.grant));
    return new Response(body,{headers:{"Content-Type":details.mime_type,"Content-Length":String(details.size),"Content-Disposition":"attachment; filename=artifact; filename*=UTF-8''"+encodeURIComponent(details.file_name),"Cache-Control":"no-store","Referrer-Policy":"no-referrer","X-Content-Type-Options":"nosniff"}});
  }
  if (url.pathname === "/worker/renew" && request.method === "POST") {
    const credential=workerCredential(request);
    const input=z.object({credential_hash:z.string().regex(/^[a-f0-9]{64}$/)}).strict().parse(await boundedJson(request));
    const result=await env.OWNERS.getByName(credential.owner).beginWorkerRenewal(credential.worker,credential.secret,input.credential_hash);
    return Response.json({...result,verification_url:origin+"/renew?code="+result.renewal_id},{status:201,headers:{"Cache-Control":"no-store"}});
  }
  const renewalClaim=/^\/worker\/renew\/([a-f0-9]{32})$/.exec(url.pathname);
  if (renewalClaim && request.method === "POST") {
    const credential=workerCredential(request);
    return Response.json(await env.OWNERS.getByName(credential.owner).claimWorkerRenewal(renewalClaim[1],credential.secret,credential.worker),{headers:{"Cache-Control":"no-store"}});
  }
  if (["/worker/connect", "/worker/commands", "/worker/message"].includes(url.pathname)) {
    const credential = workerCredential(request), owner = env.OWNERS.getByName(credential.owner);
    if (url.pathname === "/worker/connect" && request.method === "GET" && request.headers.get("Upgrade")?.toLowerCase() === "websocket") return owner.fetch(request);
    if (request.method !== "POST") return new Response(null, { status: 405 });
    const result = object(await owner.workerRequest(url.pathname === "/worker/commands" ? "commands" : "message", credential.worker, credential.secret,
      url.pathname === "/worker/message" ? await boundedJson(request) : undefined));
    if (result.relay_error) return Response.json({ error: result.code, message: result.message }, { status: Number(result.status) });
    return Response.json(result);
  }
  const auth = await authPage(request, env, server.getOAuthApi(env), recovery);
  if (auth) return auth;
  const download=/^\/downloads\/([A-Za-z0-9_.-]{1,128})\/([A-Za-z0-9_.-]{1,128})$/.exec(url.pathname);
  if (download && ["GET","POST"].includes(request.method)) {
    const session=await managementSession(request,env);
    if (!session) return Response.redirect(origin+"/auth/login?next="+encodeURIComponent(url.pathname),302);
    const owner=env.OWNERS.getByName(String(session.ownerKey));
    // Retain ownership checks for old links, without creating or approving a
    // transfer. File delivery belongs to the host capability API, not this page.
    await owner.downloadDetailsByArtifact(download[1],download[2]);
    recovery.retryPath=url.pathname;
    if (request.method === "POST") {
      await requireManagementPost(request,env,await request.formData());
    }
    return page("Open your file in ChatGPT", `<section class="panel stack"><h2>Return to your task</h2>
      <p>Open the file from your task card in ChatGPT. This page does not start a file transfer.</p>
      <p>Keep the computer that created the file connected. If file delivery is unavailable in ChatGPT, the file remains on that computer.</p>
      <p>Use Computers in ChatGPT to check the connection.</p></section>${returnToConversation()}`, new Headers({ "X-Content-Type-Options": "nosniff" }), [],
      { eyebrow: "Task file", description: "Files are delivered inside ChatGPT.", accountMethod: accountMethod(session) });
  }
  if (url.pathname === "/renew" && ["GET","POST"].includes(request.method)) {
    const session=await managementSession(request,env);
    if (!session) return Response.redirect(origin+"/auth/login?next="+encodeURIComponent(url.pathname+url.search),302);
    const id=z.string().regex(/^[a-f0-9]{32}$/).parse(url.searchParams.get("code"));
    const owner=env.OWNERS.getByName(String(session.ownerKey)),details=object(await owner.renewalDetails(id));
    recovery.retryPath = "/renew?code=" + id;
    if (request.method === "POST") {
      const form=await request.formData();await requireManagementPost(request,env,form);
      if (form.get("decision") !== "approve") {
        await owner.declineWorkerRenewal(id);
        return page("Access not renewed",'<section class="status status--attention"><p>No new access was granted. Saved work stays on your computer.</p></section><div class="action-group"><a class="button" href="/account">Manage connections</a></div>'+returnToConversation(), new Headers(), [], { eyebrow: "Renewal declined", accountMethod: accountMethod(session) });
      }
      await owner.approveWorkerRenewal(id,String(session.ownerKey));
      return page("Computer reconnected",'<section class="status status--success"><p>Access is renewed for the same computer and approved workspace. The background companion completes reconnecting. Check its connection in Computers in ChatGPT. Existing task identities and files are preserved.</p></section><p>Expired tasks will not be restarted.</p>'+returnToConversation(), new Headers(), [], { eyebrow: "Access renewed", accountMethod: accountMethod(session) });
    }
    const workspaces=Array.isArray(details.workspaces) ? details.workspaces as Record<string,unknown>[] : [];
    return page("Renew computer access", `<section class="panel"><p class="eyebrow">Approved computer and folders</p><h2>${html(String(details.label ?? "Your computer"))}</h2>
      <p>Renew access for 30 days. Approve only a renewal you just requested for your own computer.</p>
      ${workspacePermissions(workspaces)}<p class="help">This keeps the same computer and approved folders; it does not restart expired work. Provider credentials stay on that computer.</p></section>
      <form method="post"><input type="hidden" name="csrf" value="${html(String(session.csrf))}"><div class="action-group"><button name="decision" value="approve">Renew access</button><button class="secondary" name="decision" value="deny">Decline</button></div></form>`,
      new Headers(), [], { eyebrow: "Review access", description: "Review the computer and approved folders before renewing access.", accountMethod: accountMethod(session) });
  }
  if (url.pathname === "/pair" || url.pathname === "/account" || url.pathname === "/connect") {
    const session = await managementSession(request, env);
    if (!session) return Response.redirect(origin + "/auth/login?next=" + encodeURIComponent(url.pathname + url.search), 302);
    if (url.pathname === "/connect") {
      const requestId = url.searchParams.get("request_id");
      const saved = requestId ? object(await env.OWNERS.getByName(String(session.ownerKey)).setupDetails(ID.parse(requestId))) : null;
      recovery.retryPath=saved ? "/connect?request_id="+encodeURIComponent(ID.parse(requestId)) : "/connect";
      if (saved && request.method === "POST") {
        const form = await request.formData();
        const verified = await requireManagementPost(request, env, form);
        const selection = String(form.get("workspace") ?? "").split("/");
        if (selection.length !== 2) throw new RelayError("workspace_policy", "Choose an approved workspace");
        const selected=object(await env.OWNERS.getByName(String(verified.ownerKey)).bindPrepared(requestId!, ID.parse(selection[0]), ID.parse(selection[1])));
        return page(selected.status==="awaiting_setup"?"Task waiting for setup":"Task queued", `<section class="status status--success"><p>${selected.status==="awaiting_setup"?"Your task is saved. Finish the provider and folder checks in ChatGPT before work can start.":"Your saved task is queued on the selected computer using its approved workspace."} You do not need to submit the task again.</p></section><p>Use Computers in ChatGPT to check the background companion and setup.</p>`+returnToConversation(), new Headers(), [], { eyebrow: "Workspace selected", accountMethod: accountMethod(session) });
      }
      if (saved && saved.status !== "awaiting_setup") return page("Task saved", `<section class="panel"><p class="badge">${html(taskStatus(String(saved.status)))}</p>
        <p>Return to the saved task in your conversation. Setup will not restart it.</p>
        ${saved.setup_url ? `<p>Your approved workspace and task are already saved. Use Computers in ChatGPT to check the connection, or start the installed background companion on that computer. It has no local setup window.</p><div class="action-group"><a class="button" href="astra-antigravity://connect?request_id=${encodeURIComponent(requestId!)}">Start background companion</a></div>` : ""}
        <p><a href="/account">Manage your connection</a></p></section>${returnToConversation()}`, new Headers(), [], { eyebrow: "Saved task", accountMethod: accountMethod(session) });
      const known = saved ? object(await env.OWNERS.getByName(String(session.ownerKey)).workspaces()).workspaces as Array<Record<string,unknown>> : [];
      const companionUrl = "astra-antigravity://connect" + (requestId ? "?request_id=" + encodeURIComponent(requestId) : "");
      const setup = companionSetupSettings(env);
      const setupCommand = "setup_command" in setup ? setupCommandForRequest(setup.setup_command, requestId ?? undefined) : undefined;
      return page("Connect your computer", `${saved ? taskPreview(String(saved.task)) + '<p class="muted">The request is saved. Your task has not started. After you approve a compatible workspace, this same task will be delivered once.</p>' : ""}
        ${known.length ? `<section class="panel"><form method="post" class="stack"><input type="hidden" name="csrf" value="${html(String(session.csrf))}">
          <fieldset><legend>Use an already approved workspace</legend>${known.map(ws => `<label class="choice"><input type="radio" name="workspace" required value="${html(String(ws.worker_id) + "/" + String(ws.id))}"><span><strong>${html(String(ws.label))}</strong><br><span class="muted">${html(String(ws.worker_label))} · ${ws.online ? "Online" : "Offline — start the background companion to receive work"}</span></span></label>`).join("")}</fieldset>
          <div class="action-group"><button>Use approved workspace</button></div></form></section>` : ""}
        <div class="grid"><section class="panel stack"><p class="eyebrow">${known.length ? "Connect another computer" : "On your Windows computer"}</p><h2>Already installed?</h2>
          <p>Start the installed background companion on the Windows computer that will run your tasks. It has no local setup window.${saved ? " This link keeps your saved task reference." : ""}</p>
          <div class="action-group"><a class="button${known.length ? " secondary" : ""}" href="${html(companionUrl)}">Start background companion</a></div>
          <details class="help"><summary>First time on this computer?</summary><p>Return to your conversation and open Computers in ChatGPT.</p>${setupCommand ? `<p>Run this setup command on your own Windows computer:</p><pre><code>${html(setupCommand)}</code></pre>` : `<p>${html("install_unavailable_message" in setup ? setup.install_unavailable_message : "Setup is unavailable.")}</p>`}<ol><li>Return the pairing code from setup to Computers in ChatGPT, then explicitly approve computer management.</li><li>In that same panel, check or sign in to your provider accounts and approve the folder where tasks may run.</li><li>Manage tasks, computer settings and results in ChatGPT. The companion runs in the background with no window, tray or local web page.</li></ol><p>Provider sign-in can open the provider's own browser page on your computer. Use the same sign-in method and account for this plugin on each device.</p></details>
          <details class="help"><summary>If the background companion does not start</summary><p>Run the verified setup entry point that installed it to check its public status. Continue code approval and account or folder setup in ChatGPT.</p><p>No local dashboard or connection form should open.</p></details></section>
        <aside class="panel"><p class="eyebrow">Continue on another device</p><h2>Using a phone or tablet?</h2>
          <p>Run the verified setup on your own Windows computer once. Enter its pairing code in Computers in ChatGPT on your phone or tablet, then finish provider and folder approval there.</p><p>${saved ? "Your task is saved, so you do not need to send it again." : "After setup, manage this computer from ChatGPT on any signed-in device."}</p>
          <p class="help">Keep that computer running while tasks execute. Credentials and full execution logs stay there. The relay carries task inputs and compact results.</p></aside></div>
        <p class="help"><a href="/account">Connected computers</a></p>`, new Headers(), [],
        { eyebrow: "Connection setup", description: "A background companion runs on your computer. ChatGPT manages pairing, provider sign-ins and folder approval using your own subscriptions.", wide: true, accountMethod: accountMethod(session) });
    }
    if (url.pathname === "/account") {
      const records=object(await env.OWNERS.getByName(String(session.ownerKey)).exportData());
      const workers=Array.isArray(records.workers) ? records.workers as Record<string,unknown>[] : [];
      const method = accountMethod(session);
      return page("Connected computers", `<section class="panel split"><div><p class="eyebrow">Your account</p><p>${method ? `Signed in using <strong>${html(method)}</strong>.` : "You are signed in."}</p>
          <p class="help">Use the same sign-in method and account on each device. Accounts from different sign-in providers remain separate. Switching methods does not move connected computers.</p></div><a class="button secondary" href="/auth/login?next=%2Faccount">Switch sign-in method</a></section>
        ${workers.length ? `<div class="grid">${workers.map(worker => computerCard(worker, String(session.csrf))).join("")}</div><div class="action-group"><a class="button" href="/connect">Connect a computer</a></div>` :
          '<section class="panel empty-state"><p class="eyebrow">Computer connection</p><h2>No computers are connected yet.</h2><p>Open Computers in ChatGPT to set up your Windows computer. The companion runs in the background using your provider accounts, with no window, tray or local web page.</p><div class="action-group"><a class="button" href="/connect">Connection help</a></div><p class="help">Already connected a computer? Check that you used the same account and sign-in method.</p></section>'}
        <section class="panel"><h2>Records and access</h2><p>Provider credentials stay on your computer. Each computer shows whether approval lasts until revoked or has an expiry date. Manage provider accounts, approved folders, tasks and results in ChatGPT. Access can be revoked here or in ChatGPT.</p>
          <details class="danger-zone"><summary>Erase relay records</summary><form method="post" action="/account/erase" class="stack"><input type="hidden" name="csrf" value="${html(String(session.csrf))}">
            <p>Erase your retained relay records and revoke computer access. Review or cancel active work first: erasure does not cancel provider tasks or remove local history. It also removes recovery IDs.</p>
            <label>Type ERASE to confirm <input name="confirm" required autocomplete="off" spellcheck="false"></label><div class="action-group"><button class="danger">Erase relay records</button></div></form></details></section>`,
        new Headers(), [], { eyebrow: "Account", description: "Review connected computers, approved folders, and access.", wide: true, accountMethod: method });
    }
    const code = url.searchParams.get("code") ?? "";
    if (!/^[a-f0-9]{24}$/.test(code)) throw new RelayError("invalid_pairing", "Invalid pairing code");
    const pending = env.PAIRINGS.getByName(code);
    const input = await pending.details();
    const task = input.setup_request_id ? object(await env.OWNERS.getByName(String(session.ownerKey)).setupDetails(input.setup_request_id)) : null;
    recovery.retryPath = "/pair?code=" + code;
    if (request.method === "POST") {
      const form = await request.formData();
      const verified = await requireManagementPost(request, env, form);
      if (form.get("decision") !== "approve") {
        await pending.decline();
        return page("Computer not connected", '<section class="status status--attention"><p>No access was granted. Return to setup to choose what to do next.</p></section><div class="action-group"><a class="button" href="/connect">Return to setup</a></div>'+returnToConversation(), new Headers(), [], { eyebrow: "Connection declined", accountMethod: accountMethod(session) });
      }
      await pending.approve(String(verified.ownerKey),form.get("approval_mode")==="until_revoked");
      return page(input.workspaces.length?"Workspace connected":"Computer management connected", `<section class="status status--success"><p>${input.workspaces.length?"Your computer can receive authorized tasks for the approved workspace. Setup continues automatically on your computer.":"Computer management is approved. No folder access has been granted yet. Open Computers in ChatGPT to check provider accounts and approve a folder."} Return to ChatGPT or Codex.</p></section>` +
        (task ? "<p>Your original task is saved. It will be delivered once when setup is ready. No new request is needed.</p>" : "") + '<p><a href="/account">Connected computers</a></p>'+returnToConversation(), new Headers(), [], { eyebrow: "Access approved", accountMethod: accountMethod(session) });
    }
    return page("Connect your computer", `<section class="panel"><p class="eyebrow">Computer requesting access</p><h2>${html(input.label)}</h2>
      <p>Connect this computer to your account. Approve only a pairing code returned by setup on your own computer.</p>
      ${workspacePermissions(input.workspaces)}<p class="help">${input.workspaces.length?"That computer can execute delegated commands and change approved files.":"This approves computer management only. Available controls depend on how the companion is installed. No folder access is approved. Approve a folder separately in ChatGPT before task work can start."} Provider credentials stay on your computer. You choose how long this computer stays approved.</p></section>
      ${task ? taskPreview(String(task.task), "Delegate your saved task") + `<p class="status status--attention">${input.workspaces.length?"Approving also queues this saved task. This task uses your provider quotas and may change files in the approved workspace.":"Your saved task waits for provider readiness and separate folder approval in ChatGPT. Management approval alone does not start work."}</p>` : ""}
      <form method="post"><input type="hidden" name="csrf" value="${html(String(session.csrf))}"><label class="choice"><input type="checkbox" name="approval_mode" value="until_revoked" checked><span>Keep this computer approved until I revoke it. Device credentials rotate automatically. Uncheck for 30-day access.</span></label><div class="action-group"><button name="decision" value="approve">${task&&input.workspaces.length ? "Connect workspace and delegate" : input.workspaces.length?"Connect computer":"Connect computer management"}</button><button class="secondary" name="decision" value="deny">Decline</button></div></form>`,
      new Headers(), [], { eyebrow: "Review computer access", description: "Check the computer and workspace permissions before you approve.", accountMethod: accountMethod(session) });
  }
  if (url.pathname === "/account/revoke" && request.method === "POST") {
    recovery.retryPath = "/account";
    const form = await request.formData(), session = await requireManagementPost(request, env, form);
    await env.OWNERS.getByName(String(session.ownerKey)).revokeWorker(String(form.get("worker_id")));
    return page("Computer access revoked", '<section class="status status--success"><p>This computer can no longer receive tasks or relay results.</p></section><p>Existing provider work may still be running; inspect it on that computer.</p><div class="action-group"><a class="button" href="/account">Manage connections</a></div>'+returnToConversation(), new Headers(), [], { eyebrow: "Access removed", accountMethod: accountMethod(session) });
  }
  if (url.pathname === "/account/export" && request.method === "GET") {
    const session=await managementSession(request,env);
    if(!session) throw new RelayError("sign_in_required","Sign in to access your relay records",401);
    const grants=await allUserGrants(server.getOAuthApi(env),String(session.userId));
    return Response.json({...object(await env.OWNERS.getByName(String(session.ownerKey)).exportData()),authorizations:grants.map(({id,clientId,scope,createdAt,expiresAt,resource})=>({id,clientId,scope,createdAt,expiresAt,resource}))},{headers:{"Cache-Control":"no-store"}});
  }
  if (url.pathname === "/account/erase" && request.method === "POST") {
    recovery.retryPath = "/account";
    const form=await request.formData(),session=await requireManagementPost(request,env,form);
    if(form.get("confirm")!=="ERASE") throw new RelayError("confirmation_required","Type ERASE to confirm removal of your relay records");
    await env.OWNERS.getByName(String(session.ownerKey)).eraseData();
    await revokeAllUserGrants(server.getOAuthApi(env),String(session.userId));
    return page("Relay records erased",'<section class="status status--success"><p>Your computer connections and retained task records were removed.</p></section><p>Local files, provider history and LinkedIn support correspondence remain under their separate controls.</p>'+
      "<p>All current application authorizations were revoked.</p>"+returnToConversation(), new Headers(), [], { eyebrow: "Your records and controls" });
  }
  if (url.pathname === "/" || url.pathname === "/health") return url.pathname === "/health"
    ? Response.json({ service: "Astra Antigravity Relay", version: "0.17.0", cost_policy: "free-only", inference: "user worker",
      deployment_mode: env.DEPLOYMENT_MODE,
      authentication_configured: authConfigured(env) },
      { headers: { "Cache-Control": "no-store" } })
    : page("Delegate to Antigravity", `<section class="panel stack"><h2>Continue in ChatGPT</h2>
      <p>Use the plugin's Computers panel to connect your computer, check provider accounts and approve folders. Tasks, files and computer controls are in ChatGPT.</p>
      <div class="action-group"><a class="button" href="https://chatgpt.com/">Open ChatGPT</a><a href="/account">Account access</a></div>
      <p class="help">Already using the plugin? Return to your existing conversation. Connect the plugin from ChatGPT when asked to sign in.</p></section>
      <div class="grid"><section class="panel"><h2>Set up once on your computer</h2>
      <p>When setup is available, run the command shown in ChatGPT on the Windows computer that will run your tasks. Paste its pairing code in ChatGPT and approve the computer there.</p>
      <p class="help">Public setup is not available in this preview yet. The companion runs in the background with no window, tray or local web page.</p></section>
      <section class="panel"><h2>Your accounts and folders</h2><p>Tasks use your own Codex and Antigravity accounts in folders you approve. Provider sign-in can open the provider's own browser page. Keep your computer awake while tasks run.</p></section></div>`, new Headers(), [], { description: "Delegate tasks from ChatGPT to a computer you connect.", wide: true });
  return server.fetch(request, env, ctx);
}

export default {
  async fetch(request: Request, env: Env, ctx: ExecutionContext): Promise<Response> {
    const recovery: BrowserRecovery = {};
    try { return artifactTransferCors(request, await fetchRelay(request, env, ctx, recovery)); }
    catch (error) {
      const failure = relayError(error);
      if (request.headers.get("Accept")?.includes("text/html")) {
        const status=failure?.status ?? 400;
        const expired = ["invalid_pairing", "pairing_expired", "renewal_expired"].includes(failure?.code ?? "");
        const response=page(status === 401 ? "Sign in to continue" : "This step needs attention",
          `<section class="status status--attention"><p>${html(failure?.message ?? "This step could not finish. Your saved task has not been restarted.")}</p></section>
          ${expired ? '<p>Open Computers in ChatGPT to start a fresh connection or renewal. Return to the saved task in your conversation; do not submit it again.</p>' : ""}
          ${recovery.retryPath ? `<div class="action-group"><a class="button" href="${html(recovery.retryPath)}">Try this step again</a></div><p class="help">This opens the saved page again. It does not repeat the previous action.</p>` : '<div class="action-group"><a class="button" href="/account">Manage your connection</a></div>'}
          ${recovery.retryPath ? '<p><a href="/account">Manage your connection</a></p>' : ""}<p class="help">Existing work stays on your computer.</p>${returnToConversation()}`, new Headers(), [], { eyebrow: "Recovery" });
        return artifactTransferCors(request, new Response(response.body,{status,headers:response.headers}));
      }
      return artifactTransferCors(request, Response.json({ error: failure?.code ?? "request_failed",
        message: failure?.message ?? "The request could not be completed. No task was restarted." },
      { status: failure?.status ?? 400, headers: { "Cache-Control": "no-store" } }));
    }
  }
} satisfies ExportedHandler<Env>;

function taskPreview(task: string, title = "Saved task"): string {
  const compact = Array.from(task.replace(/\s+/g, " ").trim()), summary = compact.slice(0, 180).join("") + (compact.length > 180 ? "…" : "");
  return `<section class="panel task-preview"><p class="eyebrow">Task preview</p><h2>${html(title)}</h2><p>${html(summary)}</p><details><summary>Read the full task</summary><pre>${html(task)}</pre></details></section>`;
}
function taskStatus(value: string): string {
  const labels: Record<string, string> = { awaiting_setup: "Waiting for setup", queued: "Queued on your computer", leased: "Received by your computer", running: "In progress", completed: "Completed", failed: "Needs review", cancelled: "Cancelled", cancelling: "Cancellation requested", unknown: "Inspect saved work on your computer" };
  return labels[value] ?? "Saved — check the task in chat";
}
function workspacePermissions(workspaces: readonly Record<string, unknown>[]): string {
  const types: Record<string, string> = { implementation: "Code changes", analysis: "Analysis", research: "Supplied-source research", document: "Documents" };
  return `<ul class="permission-list">${workspaces.map(workspace => {
    const isolation = Array.isArray(workspace.isolation) ? workspace.isolation.map(value => value === "worktree" ? "Isolated Git worktree" : value === "shared" ? "Original folder (shared access)" : "").filter(Boolean) : [];
    const capabilities = Array.isArray(workspace.capabilities) ? workspace.capabilities.map(value => value === "shell" ? "Run commands" : value === "local_files" ? "Read and change approved files" : "").filter(Boolean) : [];
    const taskTypes = Array.isArray(workspace.task_types) ? workspace.task_types.map(value => types[String(value)]).filter(Boolean) : [];
    return `<li><strong>${html(String(workspace.label ?? workspace.id))}</strong>${isolation.length ? `<p>Working mode: ${html(isolation.join(" · "))}</p>` : ""}
      ${capabilities.length ? `<p>${html(capabilities.join(" · "))}</p>` : ""}${taskTypes.length ? `<p class="help">Approved task types: ${html(taskTypes.join(" · "))}</p>` : ""}
      <details class="help"><summary>Workspace alias</summary><code>${html(String(workspace.id))}</code></details></li>`;
  }).join("")}</ul>`;
}
function computerCard(worker: Record<string, unknown>, csrf: string): string {
  const expires = Number(worker.expires), revoked = Boolean(worker.revoked), expired = !Number.isFinite(expires) || expires <= Date.now();
  const persistent=worker.approval_mode==="until_revoked";
  const date = !persistent&&Number.isFinite(expires) ? new Intl.DateTimeFormat("en", { dateStyle: "medium", timeZone: "UTC" }).format(expires) : "unknown";
  let workspaces: Record<string, unknown>[] = [];
  try { const parsed = typeof worker.workspaces === "string" ? JSON.parse(worker.workspaces) : worker.workspaces; if (Array.isArray(parsed)) workspaces = parsed.filter(value => value && typeof value === "object" && !Array.isArray(value)); } catch { /* Retain account controls if old display metadata is unreadable. */ }
  return `<section class="panel computer-card"><p class="badge">${revoked ? "Access revoked" : expired ? "Access expired" : "Access approved"}</p><h2>${html(String(worker.label))}</h2>
    <p>${revoked ? "This computer cannot receive new tasks or relay results." : expired ? "Access expired. Open Computers and settings in ChatGPT to review this connection." : persistent?"Access approved until you revoke it. Short-lived computer credentials rotate automatically.":`Access approved until <time datetime="${html(new Date(expires).toISOString())}">${html(date)}</time> (UTC).`}</p>
    ${workspaces.length ? `<details><summary>Approved workspaces (${workspaces.length})</summary>${workspacePermissions(workspaces)}</details>` : ""}
    ${!revoked ? `<details class="help"><summary>Remove this computer’s access</summary><p>Existing provider work may still be running. Revoking access does not undo local changes.</p><form method="post" action="/account/revoke"><input type="hidden" name="csrf" value="${html(csrf)}"><input type="hidden" name="worker_id" value="${html(String(worker.id))}"><div class="action-group"><button class="danger">Revoke access</button></div></form></details>` : ""}</section>`;
}
function returnToConversation(): string {
  return '<aside class="next-step help"><p>You can close this tab and switch back to your original ChatGPT or Codex conversation.</p></aside>';
}
