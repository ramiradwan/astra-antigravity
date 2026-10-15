import {env,SELF,runInDurableObject,evictDurableObject} from "cloudflare:test";
import {describe,it,expect,vi} from "vitest";
import type {OAuthHelpers} from "@cloudflare/workers-oauth-provider";
import {allUserGrants,revokeAllUserGrants} from "../src/auth";
import {digest,object} from "../src/contracts";
import {serveMcp} from "../src/mcp";
import {seal,sealArtifactDownload,unseal,unsealArtifactDownload} from "../src/security";
import type {OwnerRelay} from "../src/broker";
import {controlFrom,downloadGrantFrom} from "../ui/control-controller";
import {transferNativeFile} from "../ui/native-files";

declare module "cloudflare:test" {interface ProvidedEnv extends Env {}}
const ORIGIN="https://relay.example.test",SECRET="a".repeat(64);
const capabilities={artifact_stream_v1:false,max_artifact_bytes:20*1024*1024,artifact_chunk_bytes:16*1024,computer_control_v1:true};
async function fixture(persistent=false,snapshot:{installed?:boolean}={installed:true}){
  const key=await digest(crypto.randomUUID()),stub=env.OWNERS.getByName(key);
  const worker=await stub.registerWorker({label:"My computer",credential_hash:await digest(SECRET),...(persistent?{approval_mode:"until_revoked" as const}:{}),workspaces:[{id:"project",label:"My project",capabilities:["local_files","shell"],isolation:["worktree"],task_types:["implementation"]}]},key,crypto.randomUUID());
  await stub.workerHttp(worker.worker_id,SECRET,{kind:"readiness",readiness:{},transport_capabilities:capabilities});
  await runInDurableObject(stub,async(_instance,state)=>state.storage.sql.exec("UPDATE workers SET computer_snapshot=?",JSON.stringify(snapshot)).toArray());
  return {key,stub,id:worker.worker_id};
}
type Fixture=Awaited<ReturnType<typeof fixture>>;
async function prepare(a:Fixture,action:string,parameters:Record<string,unknown>={},request_id=crypto.randomUUID()){
  return a.stub.tool("prepare_operation",{request_id,action,computer_id:a.id,parameters});
}
async function confirm(a:Fixture,receipt:Record<string,unknown>){return a.stub.tool("confirm_operation",{operation_id:receipt.operation_id,confirmation_token:receipt.confirmation_token});}
async function fault(a:Fixture,action:(instance:OwnerRelay)=>Promise<unknown>){return runInDurableObject(a.stub,async instance=>{try{await action(instance);return "NO ERROR";}catch(error){return error instanceof Error?error.message:String(error);}});}
async function launch(a:Fixture,request_id=crypto.randomUUID()){return a.stub.tool("delegate_via_luna",{workspace_id:"project",request_id,task:"Create answer.py"});}
async function rpc(a:Fixture,name:string,args:Record<string,unknown>,scopes:string[]){
  const request=new Request(ORIGIN+"/mcp",{method:"POST",headers:{"Content-Type":"application/json",Accept:"application/json, text/event-stream"},body:JSON.stringify({jsonrpc:"2.0",id:1,method:"tools/call",params:{name,arguments:args}})});
  const response=await serveMcp(request,env,{props:{userId:"synthetic",ownerKey:a.key},token:"synthetic",scope:scopes});
  const body=await response.text();return JSON.parse(body.startsWith("event:")?body.split("\n").find(line=>line.startsWith("data: "))!.slice(6):body).result;
}

describe("confirmed computer controls",()=>{
  it("shows an honest disabled first-use release without changing saved task authority",async()=>{
    const a=await fixture(),saved=await a.stub.tool("prepare_delegation",{request_id:"setup-release-gate",task:"Create a report",task_type:"document"});
    const snapshot=await a.stub.controlSnapshot();
    expect(snapshot.settings).toMatchObject({setup_url:ORIGIN+"/connect",install_unavailable_message:expect.stringContaining("A verified public setup release is not available")});
    expect(snapshot.settings.setup_command).toBeUndefined();expect(snapshot.settings.installer_url).toBeUndefined();
    expect(controlFrom({_meta:{astra_control:snapshot}})).toBeDefined();
    expect(await a.stub.tool("inspect_delegation",{delegation_id:saved.delegation_id})).toMatchObject({status:"awaiting_setup",terminal:false});
  });
  it.each([undefined,false])("keeps installed maintenance unavailable for an unknown or portable computer (installed=%s)",async installed=>{
    const a=await fixture(false,installed===undefined?{}:{installed});
    await a.stub.workerHttp(a.id,SECRET,{kind:"readiness",readiness:{},transport_capabilities:{...capabilities,setup_control_v1:true}});
    const actions=(await a.stub.controlSnapshot()).computers[0].allowed_actions;
    expect(actions).toEqual(expect.arrayContaining(["pause","stop_tasks","check_accounts","install_provider","sign_in","configure_workspace"]));
    for(const [action,parameters] of [["set_startup",{enabled:true}],["set_automatic_updates",{enabled:true}],["uninstall",{erase_history:false}]] as const){
      expect(actions).not.toContain(action);
      const result=await rpc(a,"prepare_operation",{request_id:crypto.randomUUID(),computer_id:a.id,action,parameters},["computers:manage"]);
      expect(result.isError).toBe(true);expect(result.content[0].text).toContain("does not support that action");
    }
    const paused=await prepare(a,"pause");await confirm(a,paused);
    expect((await a.stub.controlSnapshot()).computers[0].allowed_actions).toEqual(expect.arrayContaining(["resume","stop_tasks","check_accounts"]));
  });
  it("rechecks installed authority before confirming a prepared maintenance operation",async()=>{
    const a=await fixture(),operations=await Promise.all([prepare(a,"set_startup",{enabled:true}),prepare(a,"set_automatic_updates",{enabled:true}),prepare(a,"uninstall",{erase_history:false})]);
    expect((await a.stub.controlSnapshot()).computers[0].allowed_actions).toEqual(expect.arrayContaining(["set_startup","set_automatic_updates","uninstall"]));
    await a.stub.workerHttp(a.id,SECRET,{kind:"computer_status",message_id:"now-portable",revision:0,snapshot:{installed:false}});
    for(const operation of operations){
      const result=await rpc(a,"confirm_operation",{operation_id:operation.operation_id,confirmation_token:operation.confirmation_token},["computers:manage"]);
      expect(result.isError).toBe(true);expect(result.content[0].text).toContain("authority or capabilities changed");
    }
    expect((await a.stub.controlSnapshot()).receipts.every(receipt=>receipt.state==="prepared")).toBe(true);
    const dispatched=await runInDurableObject(a.stub,async(_instance,state)=>state.storage.sql.exec("SELECT id FROM computer_operations WHERE state='pending'").toArray());
    expect(dispatched).toEqual([]);
  });
  it("prepares without changing authority, binds confirmation and reuses a durable request",async()=>{
    const a=await fixture(),receipt=await prepare(a,"pause",{},"same-request");
    expect(receipt).toMatchObject({state:"prepared",computer_id:a.id,action:"pause"});
    expect(receipt.confirmation_token).toMatch(/^[a-f0-9]{64}$/);
    expect((await a.stub.controlSnapshot()).computers[0].paused).toBe(false);
    expect(await prepare(a,"pause",{},"same-request")).toEqual(receipt);
    expect(await fault(a,instance=>instance.tool("confirm_operation",{operation_id:receipt.operation_id,confirmation_token:"b".repeat(64)}))).toContain("do not match");
    expect(await fault(a,instance=>instance.tool("prepare_operation",{request_id:"same-request",action:"rename",computer_id:a.id,parameters:{label:"Other"}}))).toContain("another change");
    const first=await confirm(a,receipt);expect(first.state).toBe("pending");expect(await confirm(a,receipt)).toEqual(first);
    const exported=JSON.stringify(await a.stub.exportData());
    expect(exported).not.toContain(String(receipt.confirmation_token));expect(exported).not.toContain("credential_hash");
  });
  it("keeps queued starts fenced across eviction while allowing cancellation and controls",async()=>{
    const a=await fixture(),paused=await prepare(a,"pause");await confirm(a,paused);
    const job=await launch(a);
    await evictDurableObject(a.stub);
    const pulled=await a.stub.pullCommands(a.id,SECRET);
    expect(pulled.commands).toEqual([]);expect(pulled.controls).toMatchObject([{action:"pause",revision:1}]);
    const stopped=await a.stub.tool("cancel_delegation",{delegation_id:job.delegation_id});expect(stopped).toMatchObject({terminal:true,status:"cancelled"});
    const resumed=await prepare(a,"resume");await confirm(a,resumed);
    const next=await launch(a),commands=object(await a.stub.pullCommands(a.id,SECRET)).commands as Record<string,unknown>[];
    expect(commands).toHaveLength(1);expect(commands[0]).toMatchObject({delegation_id:next.delegation_id,allow_start:true,admission_revision:2});
    await a.stub.workerHttp(a.id,SECRET,{kind:"computer_control_result",message_id:"old-pause-ack",operation_id:paused.operation_id,state:"succeeded",summary:"Paused",revision:1,snapshot:{paused:true}});
    expect((await a.stub.controlSnapshot()).computers[0].paused).toBe(false);
  });
  it("returns only an unstarted owned lease and preserves the original launch on resume",async()=>{
    const a=await fixture(),job=await launch(a),packet=(object(await a.stub.pullCommands(a.id,SECRET)).commands as Record<string,unknown>[])[0];
    const deferred={kind:"command_deferred",message_id:"deferred-once",delegation_id:job.delegation_id,command_id:packet.command_id,reason:"computer_paused",revision:1};
    await a.stub.workerHttp(a.id,SECRET,deferred);await a.stub.workerHttp(a.id,SECRET,deferred);
    expect((await a.stub.tool("inspect_delegation",{delegation_id:job.delegation_id})).status).toBe("queued");
    await confirm(a,await prepare(a,"resume"));
    const replay=(object(await a.stub.pullCommands(a.id,SECRET)).commands as Record<string,unknown>[])[0];
    expect(replay).toMatchObject({delegation_id:job.delegation_id,command_id:packet.command_id,allow_start:true});
    await a.stub.workerHttp(a.id,SECRET,{kind:"receipt",delegation_id:job.delegation_id,local_id:"original-provider-task"});
    expect(await fault(a,instance=>instance.workerHttp(a.id,SECRET,{...deferred,message_id:"cannot-return-started"}))).toContain("cannot be returned");
  });
  it("keeps uninstall pending until its narrow, replay-safe final removal proof",async()=>{
    const a=await fixture(),prepared=await prepare(a,"uninstall",{erase_history:false});await confirm(a,prepared);
    expect((await a.stub.controlSnapshot()).computers[0]).toMatchObject({paused:true,state:"paused",allowed_actions:["revoke"]});
    const control=(object(await a.stub.pullCommands(a.id,SECRET)).controls as Record<string,unknown>[])[0],completion=object(control.completion);
    expect(completion.token).toMatch(/^[a-f0-9]{64}$/);
    await a.stub.workerHttp(a.id,SECRET,{kind:"computer_control_result",message_id:"removal-pending",operation_id:prepared.operation_id,state:"pending",summary:"Waiting for tasks to stop",revision:control.revision,snapshot:{paused:true,uninstall_status:"pending"}});
    expect((await a.stub.inspectOperation(String(prepared.operation_id))).state).toBe("pending");
    expect(JSON.stringify(await a.stub.controlSnapshot())).not.toContain(String(completion.token));
    expect(JSON.stringify(await a.stub.exportData())).not.toContain(String(completion.token));
    const path=ORIGIN+"/worker/removals/"+a.key+"/"+prepared.operation_id+"/complete";
    const denied=await SELF.fetch(path,{method:"POST",body:JSON.stringify({token:"b".repeat(64),state:"succeeded",summary:"Removed"})});expect(denied.status).toBe(403);
    const body=JSON.stringify({token:completion.token,state:"succeeded",summary:"Companion removed; task files preserved"});
    const done=await SELF.fetch(path,{method:"POST",body});expect(done.status).toBe(200);
    expect((await a.stub.controlSnapshot()).computers[0].state).toBe("revoked");
    expect((await SELF.fetch(path,{method:"POST",body})).status).toBe(200);
    expect((await a.stub.inspectOperation(String(prepared.operation_id))).state).toBe("completed");
  });
  it("drains running work during uninstall without sending a provider cancellation",async()=>{
    const a=await fixture(),job=await launch(a);
    await a.stub.workerHttp(a.id,SECRET,{kind:"receipt",delegation_id:job.delegation_id,local_id:"already-running-provider-task"});
    const prepared=await prepare(a,"uninstall",{erase_history:false});expect(prepared.summary).toContain("let work already running and its delivery finish");await confirm(a,prepared);
    expect((await a.stub.tool("inspect_delegation",{delegation_id:job.delegation_id}))).toMatchObject({status:"running",terminal:false});
    const pulled=object(await a.stub.pullCommands(a.id,SECRET));expect(pulled.commands).toEqual([]);expect(pulled.controls).toMatchObject([{action:"uninstall"}]);
    const cancellations=await runInDurableObject(a.stub,async(_instance,state)=>state.storage.sql.exec("SELECT id FROM commands WHERE operation='cancel'").toArray());expect(cancellations).toEqual([]);
    expect((await a.stub.inspectOperation(String(prepared.operation_id))).state).toBe("pending");
  });
  it("expires only issued removal confirmation to unconfirmed and schedules retention with no active workers",async()=>{
    const a=await fixture(true),prepared=await prepare(a,"uninstall",{erase_history:false});await confirm(a,prepared);
    const control=(object(await a.stub.pullCommands(a.id,SECRET)).controls as Record<string,unknown>[])[0];
    expect((await a.stub.inspectOperation(String(prepared.operation_id))).summary).toContain("--retry-removal");
    const capabilityExpiry=Number(object(control.completion).expires_at);
    const alarm=await runInDurableObject(a.stub,async(instance,state)=>{
      state.storage.sql.exec("UPDATE workers SET access_expires=?",Date.now()+14*86_400_000);
      await state.storage.deleteAlarm();await instance.alarm();return state.storage.getAlarm();
    });expect(alarm).toBe(capabilityExpiry);
    await a.stub.workerHttp(a.id,SECRET,{kind:"computer_status",message_id:"retain-dedup",revision:Number(control.revision),snapshot:{paused:true}});
    await runInDurableObject(a.stub,async(instance,state)=>{
      state.storage.sql.exec("UPDATE computer_operations SET completion_expires=? WHERE id=?",Date.now()-1,String(prepared.operation_id));
      await state.storage.deleteAlarm();await instance.alarm();
    });
    const receipt=await a.stub.inspectOperation(String(prepared.operation_id));expect(receipt).toMatchObject({state:"unconfirmed"});expect(receipt.summary).toContain("may have been removed locally");
    expect((await a.stub.controlSnapshot()).computers[0]).toMatchObject({paused:true,uninstall_status:"unconfirmed"});
    const worker=await runInDurableObject(a.stub,async(_instance,state)=>state.storage.sql.exec("SELECT revoked FROM workers").toArray());expect(worker).toEqual([{revoked:0}]);
    const late=await a.stub.workerHttp(a.id,SECRET,{kind:"computer_control_result",message_id:"late-pending-removal",operation_id:prepared.operation_id,state:"pending",summary:"Old pending receipt",revision:control.revision});expect(late).toMatchObject({disposition:"operation_expired"});
    await a.stub.revokeWorker(a.id);
    const deadlines=await runInDurableObject(a.stub,async(instance,state)=>{
      const now=Date.now();state.storage.sql.exec("UPDATE computer_operations SET updated=?",now-30*86_400_000+7_200_000);state.storage.sql.exec("UPDATE computer_messages SET created=?",now-86_400_000+3_600_000);
      await state.storage.deleteAlarm();await instance.alarm();const first=await state.storage.getAlarm();
      state.storage.sql.exec("UPDATE computer_messages SET created=?",now-86_400_000-1);await state.storage.deleteAlarm();await instance.alarm();const second=await state.storage.getAlarm();
      state.storage.sql.exec("UPDATE computer_operations SET updated=?",now-31*86_400_000);await state.storage.deleteAlarm();await instance.alarm();
      return {now,first,second,remaining:state.storage.sql.exec("SELECT id FROM computer_operations").toArray(),final:await state.storage.getAlarm()};
    });
    expect(deadlines.first).toBe(deadlines.now+3_600_000);expect(deadlines.second).toBe(deadlines.now+7_200_000);expect(deadlines.remaining).toEqual([]);expect(deadlines.final).toBeNull();
  });
  it("does not infer machine completion from a request or normal status snapshot",async()=>{
    const a=await fixture(),prepared=await prepare(a,"set_startup",{enabled:true});await confirm(a,prepared);
    await a.stub.workerHttp(a.id,SECRET,{kind:"computer_status",message_id:"normal-status",revision:0,snapshot:{startup_enabled:true,active_tasks:0,pending_delivery:false}});
    expect((await a.stub.inspectOperation(String(prepared.operation_id))).state).toBe("pending");
    const result={kind:"computer_control_result",message_id:"startup-receipt",operation_id:prepared.operation_id,state:"succeeded",summary:"Startup enabled",revision:0,snapshot:{startup_enabled:true}};
    await a.stub.workerHttp(a.id,SECRET,result);await a.stub.workerHttp(a.id,SECRET,result);
    expect((await a.stub.inspectOperation(String(prepared.operation_id))).state).toBe("completed");
    expect(await fault(a,instance=>instance.workerHttp(a.id,SECRET,{...result,summary:"Different result"}))).toContain("ID was reused");
  });
  it("does not let an offline startup change supersede a prior stop admission fence",async()=>{
    const a=await fixture();await confirm(a,await prepare(a,"stop_tasks"));await confirm(a,await prepare(a,"set_startup",{enabled:false}));
    await evictDurableObject(a.stub);
    const pulled=object(await a.stub.pullCommands(a.id,SECRET)),controls=pulled.controls as Record<string,unknown>[];
    expect(controls.map(control=>({action:control.action,revision:control.revision}))).toEqual([{action:"stop_tasks",revision:1},{action:"set_startup",revision:1}]);
    expect((await a.stub.controlSnapshot()).computers[0].paused).toBe(true);
  });
  it("rechecks a pause that arrives while a command pull is preparing control packets",async()=>{
    const a=await fixture();await launch(a);const pause=await prepare(a,"pause");
    const pulled=await runInDurableObject(a.stub,async instance=>{
      const internal=instance as unknown as {controlPackets(id:string):Promise<Record<string,unknown>[]>},original=internal.controlPackets.bind(instance);
      let entered!:()=>void,release!:()=>void;
      const started=new Promise<void>(resolve=>{entered=resolve;}),gate=new Promise<void>(resolve=>{release=resolve;});
      internal.controlPackets=async id=>{entered();await gate;return original(id);};
      try{const pulling=instance.pullCommands(a.id,SECRET);await started;await instance.tool("confirm_operation",{operation_id:pause.operation_id,confirmation_token:pause.confirmation_token});release();return await pulling;}
      finally{release();internal.controlPackets=original;}
    });
    expect(pulled.commands).toEqual([]);expect(pulled.controls).toMatchObject([{action:"pause"}]);
  });
  it("acknowledges an expired terminal operation without applying its stale snapshot or claiming success",async()=>{
    const a=await fixture(),before=await a.stub.controlSnapshot();
    const ack=await a.stub.workerHttp(a.id,SECRET,{kind:"computer_control_result",message_id:"lost-terminal-ack",operation_id:"old-expired-operation",state:"succeeded",summary:"Old result",revision:99,snapshot:{paused:true,startup_enabled:true}});
    expect(ack).toMatchObject({kind:"ack",message_id:"lost-terminal-ack",disposition:"operation_expired"});
    const after=await a.stub.controlSnapshot();expect(after.computers).toEqual(before.computers);expect(after.receipts).toEqual([]);
  });
  it("requires management and separate account deletion scopes, keeping records out of model content",async()=>{
    const a=await fixture();await launch(a);
    const readonly=await rpc(a,"show_computers",{},["workspaces:read"]);
    expect(readonly._meta.astra_control.permissions).toMatchObject({can_manage:false,can_delete_account:false});
    expect(readonly._meta.astra_control.tasks).toEqual([]);expect(readonly._meta.astra_control.computers[0].allowed_actions).toEqual([]);
    expect(readonly.content[0].text).not.toContain("Create answer.py");
    const denied=await rpc(a,"prepare_operation",{request_id:"no-scope",action:"pause",computer_id:a.id},["tasks:write"]);
    expect(denied.isError).toBe(true);expect(denied._meta["mcp/www_authenticate"][0]).toContain("computers:manage");
    const deleteDenied=await rpc(a,"prepare_operation",{request_id:"no-delete",action:"delete_account"},["computers:manage"]);
    expect(deleteDenied.isError).toBe(true);expect(deleteDenied._meta["mcp/www_authenticate"][0]).toContain("account:delete");
    const prepared=await rpc(a,"prepare_operation",{request_id:"private-confirmation",action:"rename",computer_id:a.id,parameters:{label:"Work computer"}},["computers:manage"]);
    expect(prepared.content).toEqual([]);expect(prepared._meta.astra_operation.confirmation_token).toMatch(/^[a-f0-9]{64}$/);
  });
  it("opens the read-only computers card with a retained task cancelled before computer approval",async()=>{
    const a=await fixture(),saved=await a.stub.tool("prepare_delegation",{request_id:"cancelled-before-approval",task:"Create answer.py",task_type:"implementation"});
    expect(saved.status).toBe("awaiting_setup");
    const cancelled=await a.stub.tool("cancel_delegation",{delegation_id:saved.delegation_id});
    expect(cancelled).toMatchObject({status:"cancelled",terminal:true});
    const result=await rpc(a,"show_computers",{},["workspaces:read","tasks:read"]);
    expect(result.isError).not.toBe(true);
    expect(result.structuredContent.status).toBe("ready");
    expect(result._meta.astra_control.tasks).toMatchObject([{delegation_id:saved.delegation_id,computer_id:"",status:"cancelled",terminal:true}]);
    const card=controlFrom(result);
    expect(card).toBeDefined();
    expect(card?.computers[0].id).toBe(a.id);
    expect(card?.permissions).toMatchObject({can_manage:false,can_delete_account:false,can_write_tasks:false});
    expect(card?.computers[0].allowed_actions).toEqual([]);
    expect(card?.tasks[0]).toMatchObject({delegation_id:saved.delegation_id,computer_id:"",status:"cancelled",terminal:true});
  });
});

describe("computer approval and credential rotation",()=>{
  it("leaves legacy expiry unchanged until explicit same-computer ongoing approval",async()=>{
    const a=await fixture(),before=await a.stub.workerGrant(a.id);
    expect(before.approval_mode).toBe("legacy_30_day");
    const hash=await digest("b".repeat(64)),refreshHash=await digest("c".repeat(64));
    expect(await fault(a,instance=>instance.rotateWorkerCredentials(a.id,SECRET,{rotation_id:"not-approved",credential_hash:hash,refresh_hash:refreshHash}))).toContain("ongoing approval");
    const pending=await prepare(a,"keep_approved");expect((await a.stub.workerGrant(a.id)).approval_mode).toBe("legacy_30_day");
    await confirm(a,pending);expect((await a.stub.workerGrant(a.id)).approval_mode).toBe("until_revoked");
    expect((await a.stub.controlSnapshot()).computers[0]).not.toHaveProperty("expires_at");
    expect((await a.stub.workspaces()).workspaces).toHaveLength(1);
  });
  it("refreshes an approved computer after months offline and limits old proof to one exact retry",async()=>{
    const a=await fixture(true),access="b".repeat(64),refresh="c".repeat(64),next={rotation_id:"rotation-one",credential_hash:await digest(access),refresh_hash:await digest(refresh)};
    await runInDurableObject(a.stub,async(_instance,state)=>state.storage.sql.exec("UPDATE workers SET access_expires=? WHERE id=?",Date.now()-120*86_400_000,a.id).toArray());
    expect(await fault(a,instance=>instance.workerHttp(a.id,SECRET,{kind:"readiness",readiness:{}}))).toContain("expired");
    const rotated=await a.stub.rotateWorkerCredentials(a.id,SECRET,next);
    expect(rotated).toMatchObject({worker_id:a.id,approval_mode:"until_revoked",rotation_id:"rotation-one"});
    expect(Number(rotated.expires_at)).toBeGreaterThan(Date.now());expect(Number(rotated.expires_at)).toBeLessThanOrEqual(Date.now()+24*3_600_000);
    expect(await a.stub.rotateWorkerCredentials(a.id,SECRET,next)).toEqual(rotated);
    expect(await fault(a,instance=>instance.workerHttp(a.id,SECRET,{kind:"readiness",readiness:{}}))).toContain("authentication failed");
    expect(await fault(a,instance=>instance.workerHttp(a.id,refresh,{kind:"readiness",readiness:{}}))).toContain("authentication failed");
    await a.stub.workerHttp(a.id,access,{kind:"readiness",readiness:{},transport_capabilities:capabilities});
    const nextAccess=await digest("d".repeat(64)),nextRefresh=await digest("e".repeat(64));
    expect(await fault(a,instance=>instance.rotateWorkerCredentials(a.id,access,{rotation_id:"access-is-not-refresh",credential_hash:nextAccess,refresh_hash:nextRefresh}))).toContain("Refresh proof");
    await a.stub.rotateWorkerCredentials(a.id,refresh,{rotation_id:"rotation-two",credential_hash:nextAccess,refresh_hash:nextRefresh});
    expect(await fault(a,instance=>instance.rotateWorkerCredentials(a.id,SECRET,next))).toContain("superseded");
    await a.stub.revokeWorker(a.id);
    expect(await fault(a,instance=>instance.rotateWorkerCredentials(a.id,"e".repeat(64),{rotation_id:"after-revoke",credential_hash:nextAccess,refresh_hash:nextRefresh}))).toContain("revoked");
  });
  it("recovers a committed lost rotation after its retry journal expires using only its candidate refresh proof",async()=>{
    const a=await fixture(true),access=await digest("b".repeat(64)),refresh="c".repeat(64),proposal={rotation_id:"lost-rotation",credential_hash:access,refresh_hash:await digest(refresh)};
    await a.stub.rotateWorkerCredentials(a.id,SECRET,proposal);
    await runInDurableObject(a.stub,async(_instance,state)=>state.storage.sql.exec("UPDATE credential_rotations SET expires=? WHERE id=?",Date.now()-1,"lost-rotation").toArray());
    expect(await fault(a,instance=>instance.rotateWorkerCredentials(a.id,SECRET,proposal))).toContain("superseded");
    const recovered=await a.stub.rotateWorkerCredentials(a.id,refresh,{rotation_id:"recover-with-candidate",credential_hash:await digest("d".repeat(64)),refresh_hash:await digest("e".repeat(64))});
    expect(recovered.worker_id).toBe(a.id);
  });
});

describe("management-only headless enrollment",()=>{
  it("approves management first and starts the same saved task only after folder approval and accounts are ready",async()=>{
    const key=await digest(crypto.randomUUID()),stub=env.OWNERS.getByName(key),request_id="saved-headless";
    const saved=await stub.tool("prepare_delegation",{request_id,task:"Create answer.py",task_type:"implementation"});
    const paired=await SELF.fetch(ORIGIN+"/worker/pair",{method:"POST",body:JSON.stringify({label:"New computer",credential_hash:await digest(SECRET),setup_only:true,workspaces:[]})});
    const pair=await paired.json<{code:string;claim:string}>(),a={key,stub,id:""};
    const approval=await rpc(a,"approve_pairing_ui",{code:pair.code,request_id,approval_mode:"until_revoked"},["computers:manage"]);
    expect(approval.content).toEqual([]);expect(approval._meta.astra_operation.state).toBe("completed");
    const claim=await SELF.fetch(ORIGIN+"/worker/pair/"+pair.code,{headers:{Authorization:"Bearer "+pair.claim}}),identity=await claim.json<{worker_id:string;approval_mode:string;access_expires_at:number}>();a.id=identity.worker_id;
    expect(identity.approval_mode).toBe("until_revoked");expect(identity.access_expires_at).toBeGreaterThan(Date.now());
    expect((await stub.workspaces()).workspaces).toEqual([]);expect((await stub.tool("inspect_delegation",{request_id})).status).toBe("awaiting_setup");
    await stub.workerHttp(a.id,SECRET,{kind:"readiness",readiness:{},transport_capabilities:{...capabilities,setup_control_v1:true}});
    const folder=await prepare(a,"configure_workspace",{path:"C:/Synthetic/approved",alias:"project",isolation:"worktree"});await confirm(a,folder);
    expect((await stub.workspaces()).workspaces).toEqual([]);
    const control=(object(await stub.pullCommands(a.id,SECRET)).controls as Record<string,unknown>[])[0];
    await stub.workerHttp(a.id,SECRET,{kind:"computer_control_result",message_id:"folder-approved",operation_id:folder.operation_id,state:"succeeded",summary:"Workspace project approved",revision:control.revision,workspace_approved:{id:"project",label:"My project",capabilities:["local_files","shell"],isolation:["worktree"],task_types:["implementation"]},snapshot:{accounts:{codex:{installed:true,status:"sign_in_required"},agy:{installed:true,status:"ready"}}}});
    expect((await stub.workspaces()).workspaces).toHaveLength(1);expect((await stub.tool("inspect_delegation",{request_id})).status).toBe("awaiting_setup");
    await stub.workerHttp(a.id,SECRET,{kind:"computer_status",message_id:"accounts-ready",revision:control.revision,snapshot:{accounts:{codex:{installed:true,status:"ready"},agy:{installed:true,status:"ready"}}}});
    const resumed=await stub.tool("inspect_delegation",{request_id});expect(resumed).toMatchObject({delegation_id:saved.delegation_id,status:"queued"});
    const commands=(object(await stub.pullCommands(a.id,SECRET)).commands as Record<string,unknown>[]);expect(commands).toHaveLength(1);expect(commands[0]).toMatchObject({delegation_id:saved.delegation_id,allow_start:true});
    expect(JSON.stringify(await stub.exportData())).not.toContain("C:/Synthetic/approved");
  });
  it("does not use a management-only approval to silently approve legacy workspace permissions",async()=>{
    const a=await fixture(),response=await SELF.fetch(ORIGIN+"/worker/pair",{method:"POST",body:JSON.stringify({label:"Legacy workspace request",credential_hash:await digest(SECRET),workspaces:[{id:"other",label:"Other",capabilities:["shell"],isolation:["shared"],task_types:["implementation"]}]})});
    const pair=await response.json<{code:string}>(),result=await rpc(a,"approve_pairing_ui",{code:pair.code,approval_mode:"until_revoked"},["computers:manage"]);
    expect(result.isError).toBe(true);expect(result.content[0].text).toContain("full permissions");
    expect((await a.stub.controlSnapshot()).computers).toHaveLength(1);
  });
});

describe("account authorization pagination",()=>{
  it("enumerates every page before revoking grants and fails closed on a repeated cursor",async()=>{
    const order:string[]=[],pages=[{items:[{id:"one"}],cursor:"next"},{items:[{id:"two"}]}];
    const helpers={listUserGrants:vi.fn(async()=>{order.push("list");return pages.shift()!;}),revokeGrant:vi.fn(async(id:string)=>{order.push(id);})} as unknown as OAuthHelpers;
    expect(await revokeAllUserGrants(helpers,"owner")).toBe(2);expect(order).toEqual(["list","list","one","two"]);
    const looping={listUserGrants:vi.fn(async()=>({items:[],cursor:"same"}))} as unknown as OAuthHelpers;
    await expect(allUserGrants(looping,"owner")).rejects.toThrow("did not advance");
  });
});

describe("host-fetch artifact grants",()=>{
  it("uses an artifact-only canonical URL-safe codec and preserves existing session and legacy grant formats",async()=>{
    const payload={purpose:"artifact_download",owner:"a".repeat(64),grant:"owned-grant",expires:Date.now()+120_000};
    const current=await sealArtifactDownload(payload,env.SESSION_SECRET);
    expect(current).toMatch(/^[A-Za-z0-9_-]{16}\.[A-Za-z0-9_-]{22,}$/);
    expect(encodeURIComponent(current)).toBe(current);
    expect(await unsealArtifactDownload(current,env.SESSION_SECRET)).toEqual(payload);
    const legacy=await seal(payload,env.SESSION_SECRET);
    expect(await unseal(legacy,env.SESSION_SECRET)).toEqual(payload);
    expect(await unsealArtifactDownload(legacy,env.SESSION_SECRET)).toEqual(payload);
    for(const changed of [current+".extra",current+"%2F",current.slice(0,-1),"/"+current])
      await expect(unsealArtifactDownload(changed,env.SESSION_SECRET)).rejects.toThrow("could not be verified");
  });
  it("uses a single-use cookie-free grant pinned to owned bytes and never returns file bytes to model content",async()=>{
    const a=await fixture(),job=await launch(a),content="answer = 42\n",sha256=await digest(content),id=String(job.delegation_id);
    await a.stub.workerHttp(a.id,SECRET,{kind:"receipt",delegation_id:id,local_id:"owned-local-task"});
    await a.stub.workerHttp(a.id,SECRET,{kind:"event",delegation_id:id,event:{sequence:1,kind:"completed",terminal:true,summary:"Finished",task_type:"implementation",verification_required:true},result:{status:"completed",artifact_refs:[{artifact_id:"answer",file_name:"answer.py",mime_type:"text/x-python",bytes:content.length,sha256,export_available:true}]}});
    await runInDurableObject(a.stub,async(_instance,state)=>state.storage.sql.exec("INSERT INTO artifacts(job_id,id,payload) VALUES(?,?,?)",id,"answer",JSON.stringify({id:"answer",encoding:"base64",content:btoa(content),bytes:content.length,sha256})).toArray());
    const snapshot=await a.stub.uiSnapshot(id);
    expect(snapshot.artifacts).toMatchObject([{artifact_id:"answer",download_origin:ORIGIN}]);
    expect(snapshot.artifacts?.[0]).not.toHaveProperty("download_url");
    const result=await rpc(a,"prepare_artifact_download_ui",{delegation_id:id,artifact_id:"answer"},["tasks:read"]);
    expect(result.content).toEqual([]);expect(result._meta.astra_download).toMatchObject({name:"answer.py",bytes:content.length,sha256});
    expect(JSON.stringify(result)).not.toContain(btoa(content));
    // Exercise the actual sealed server capability through both component
    // validators; a URL-safe fake token misses Base64's encoded /+= characters.
    const grant=downloadGrantFrom(result,snapshot.artifacts![0]);
    expect(grant).toBeDefined();
    expect(new URL(grant!.url).pathname).toMatch(/^\/artifact-download\/[A-Za-z0-9_-]{16}\.[A-Za-z0-9_-]+$/);
    const downloadFile=vi.fn(async()=>({}));
    expect(await transferNativeFile({contents:[{type:"resource_link",uri:grant!.url,name:grant!.name,mimeType:grant!.mime_type,size:grant!.bytes}]},
      {expectedOrigin:grant!.origin,expectedBytes:grant!.bytes,expectedSha256:grant!.sha256,downloadFile})).toEqual({kind:"host-download"});
    expect(downloadFile).toHaveBeenCalledTimes(1);
    const response=await SELF.fetch(result._meta.astra_download.url);expect(response.status).toBe(200);expect(await response.text()).toBe(content);
    expect(response.headers.get("Content-Length")).toBe(String(content.length));
    expect((await SELF.fetch(result._meta.astra_download.url)).status).toBe(410);
    const legacyGrant=await a.stub.tool("prepare_artifact_download_ui",{delegation_id:id,artifact_id:"answer"});
    const legacyPayload=await unsealArtifactDownload(new URL(String(legacyGrant.url)).pathname.split("/").at(-1)!,env.SESSION_SECRET);
    const legacyUrl=ORIGIN+"/artifact-download/"+encodeURIComponent(await seal(legacyPayload,env.SESSION_SECRET));
    const legacyResponse=await SELF.fetch(legacyUrl);expect(legacyResponse.status).toBe(200);expect(await legacyResponse.text()).toBe(content);
    expect((await SELF.fetch(String(legacyGrant.url))).status).toBe(410);
    const second=await a.stub.tool("prepare_artifact_download_ui",{delegation_id:id,artifact_id:"answer"});
    await a.stub.revokeWorker(a.id);expect((await SELF.fetch(String(second.url))).status).not.toBe(200);
  });
});
