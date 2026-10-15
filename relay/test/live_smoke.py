"""Loopback transport smoke test; opt in explicitly to inference on your own CLIs.

First run: wrangler dev --local --config test/live.wrangler.jsonc --ip 127.0.0.1
Default uses a fake gateway. --real-inference uses your own signed-in CLI quotas.
It never provisions infrastructure or invokes a billed inference API.
"""
import argparse
import asyncio
import hashlib
import importlib.util
import json
from pathlib import Path
import secrets
import subprocess
import tempfile

import httpx

SPEC=importlib.util.spec_from_file_location("relay_worker",Path(__file__).parents[1]/"worker.py")
module=importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)


class FixtureGateway:
    async def launch(self,*args): return {"delegation_id":"fixture-local"}
    async def events(self,*args): return {"events":[{"sequence":1,"kind":"completed","terminal":True,"summary":"Transport fixture complete",
        "task_type":"implementation","verification_required":True}],"closed":True}
    def acknowledge(self,*args): pass
    def result(self,*args): return {"status":"completed","task_type":"implementation","implementation":{"summary":"Fixture complete"}}
    async def readiness(self,*args): return {"ready":True,"ready_for":{"light":True}}
    async def close(self): pass


async def rpc(client,origin,bearer,name,args):
    response=await client.post(origin+"/mcp",headers={"Authorization":"Bearer "+bearer,"Accept":"application/json, text/event-stream"},
        json={"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":name,"arguments":args}})
    response.raise_for_status()
    if response.text.startswith("event:"):
        data=json.loads(next(line[6:] for line in response.text.splitlines() if line.startswith("data: ")))
    else: data=response.json()
    if data.get("error") or data.get("result",{}).get("isError"):
        raise RuntimeError("MCP smoke-test operation failed: "+name)
    return json.loads(data["result"]["content"][0]["text"])


async def smoke(args):
    origin=module.relay_origin(args.origin,local=True)
    if not origin.startswith("http://127.0.0.1:"):
        raise ValueError("The smoke fixture is restricted to loopback HTTP")
    directory=tempfile.TemporaryDirectory(prefix="astra-relay-smoke-") if not args.output else None
    root=Path(args.output or directory.name).resolve()
    if args.output and root.exists():
        raise ValueError("Use a fresh private directory; never reuse an inference fixture")
    root.mkdir(parents=True,exist_ok=True,mode=0o700)
    workspace=root/"workspace"
    workspace.mkdir(exist_ok=True)
    for command in (["git","init","-q"],["git","config","user.name","Astra synthetic fixture"],
                    ["git","config","user.email","fixture@invalid.local"]):
        subprocess.run(command,cwd=workspace,check=True,capture_output=True)
    (workspace/"README.txt").write_text("Synthetic relay fixture; preserve this source.\n",encoding="utf-8")
    for command in (["git","add","README.txt"],["git","commit","-qm","Synthetic fixture baseline"]):
        subprocess.run(command,cwd=workspace,check=True,capture_output=True)
    baseline=subprocess.check_output(["git","rev-parse","HEAD"],cwd=workspace,text=True).strip()
    state=module.private_directory(root/"private")
    secret=secrets.token_hex(32)
    async with httpx.AsyncClient(timeout=35,follow_redirects=False,trust_env=False) as client:
        registered=await client.post(origin+"/test/bootstrap",json={"label":"Loopback transport fixture","credential_hash":hashlib.sha256(secret.encode()).hexdigest(),
            "workspaces":[{"id":"fixture","label":"Synthetic fixture","capabilities":["local_files","shell"],"isolation":["worktree"],"task_types":["implementation"]}]})
        registered.raise_for_status();data=registered.json()
        access=await client.post(origin+"/oauth/token",data={"grant_type":"authorization_code","client_id":data["client_id"],"code":data["code"],
            "code_verifier":data["code_verifier"],"redirect_uri":data["redirect_uri"],"resource":origin+"/mcp"})
        access.raise_for_status();bearer=access.json()["access_token"]
        credential=data["owner_key"]+"."+data["worker_id"]+"."+secret
        config={"relay":origin,"workspaces":{"fixture":{"path":str(workspace),"label":"Synthetic fixture","isolation":["worktree"],
            "task_types":["implementation"],"capabilities":["local_files","shell"]}}}
        worker=module.Worker(config,state,gateway=None if args.real_inference else FixtureGateway())
        daemon=asyncio.create_task(module.run_http(worker,credential,idle_timeout=0))
        try:
            # Startup probes may take seconds. Wait in deterministic test code
            # for a connected, cached-ready worker before authorizing inference.
            async with asyncio.timeout(60):
                while True:
                    readiness=await rpc(client,origin,bearer,"check_integration",{"workspace_id":"fixture","task_type":"implementation","isolation":"worktree"})
                    if readiness.get("online") and (readiness.get("readiness") or {}).get("ready"):
                        break
                    if daemon.done(): await daemon
                    await asyncio.sleep(1)
            request={"workspace_id":"fixture","request_id":"smoke."+secrets.token_hex(16),"task_type":"implementation","isolation":"worktree","reasoning":"light",
                "model":"sonnet-5.5","task":"In the isolated task worktree, create answer.py with a function answer() returning the integer 42. Run a Python check that imports and asserts answer() == 42. Preserve README.txt and other source files. Return a concise structured completion report. Do not push or publish."}
            if args.real_inference:
                choice=(readiness["readiness"].get("models") or {}).get("light") or {}
                if not choice.get("available"):
                    raise RuntimeError("The user's worker has no available light model; no inference was started")
                request["model"]=choice["profile"]
            receipt=await rpc(client,origin,bearer,"delegate_task",request)
            again=await rpc(client,origin,bearer,"delegate_task",request)
            assert again["delegation_id"]==receipt["delegation_id"]
            cursor=0
            async with asyncio.timeout(600):
                while True:
                    # This is a deterministic transport receiver, not an agent
                    # polling progress or repeatedly reading results.
                    batch=await rpc(client,origin,bearer,"receive_delegation_events",{"delegation_id":receipt["delegation_id"],"after_sequence":cursor})
                    for event in batch.get("events") or []:
                        cursor=event["sequence"]
                        await rpc(client,origin,bearer,"ack_delegation_events",{"delegation_id":receipt["delegation_id"],"sequence":cursor})
                    if batch.get("delivery_suspended"): raise RuntimeError("Worker ownership needs reconciliation; inference was not restarted")
                    if batch.get("closed"): break
                    await asyncio.sleep(0.1)
            saved=await rpc(client,origin,bearer,"read_delegation_result",{"delegation_id":receipt["delegation_id"]})
            report=saved["handoff"]
            assert report["status"]=="completed", "Provider did not complete the synthetic task"
            preserved=subprocess.check_output(["git","rev-parse","HEAD"],cwd=workspace,text=True).strip()==baseline
            preserved=preserved and not subprocess.check_output(["git","status","--porcelain"],cwd=workspace,text=True).strip()
            evidence={"transport":"HTTPS adapter over loopback HTTP fixture","real_cli_inference":bool(args.real_inference),"status":report["status"],
                "same_receipt_on_retry":True,"events_count":cursor,"source_preserved":preserved,"model":report.get("model"),"handoff_characters":len(module.canonical(report))}
            if args.real_inference:
                native=worker.gateway.result(worker.journal.job(receipt["delegation_id"])["local_id"])
                effective=Path(native["implementation"]["worktree"]["path"])
                subprocess.run(["python","-c","from answer import answer; assert answer() == 42"],cwd=effective,check=True,capture_output=True)
                evidence["independent_answer_check"]=True
            assert preserved
            print(json.dumps(evidence,indent=2),flush=True)
            (root/"evidence.json").write_text(json.dumps(evidence,indent=2)+"\n",encoding="utf-8")
        finally:
            daemon.cancel()
            await asyncio.gather(daemon,return_exceptions=True)
            await worker.close()
    if directory:
        if root.parent != Path(tempfile.gettempdir()).resolve() or not root.name.startswith("astra-relay-smoke-"):
            raise ValueError("Temporary fixture cleanup target failed containment validation")
        directory.cleanup()


if __name__=="__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--origin",default="http://127.0.0.1:8787")
    parser.add_argument("--output",type=Path,help="A fresh private directory for fixture state and evidence")
    parser.add_argument("--real-inference",action="store_true")
    asyncio.run(smoke(parser.parse_args()))
