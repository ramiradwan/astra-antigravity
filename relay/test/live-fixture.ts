// Loopback-only test entry point. Never used by the deployment guard or package.
export {OwnerRelay,WorkerPairing} from "../src/broker";
import relay from "../src/index";
import {authorizationServer} from "../src/auth";
import {pairingSchema} from "../src/broker";
import {boundedJson,digest,SCOPES,token} from "../src/contracts";

export default {
  async fetch(request:Request,env:Env,ctx:ExecutionContext):Promise<Response> {
    const url=new URL(request.url);
    if(url.pathname!=="/test/bootstrap") return relay.fetch(request,env,ctx);
    if(env.DEPLOYMENT_MODE!=="local" || url.protocol!=="http:" || !["127.0.0.1","localhost"].includes(url.hostname) || request.method!=="POST") return new Response(null,{status:404});
    const input=pairingSchema.parse(await boundedJson(request)),userId="synthetic-"+token(16),ownerKey=await digest(userId);
    const worker=await env.OWNERS.getByName(ownerKey).registerWorker(input,ownerKey,"test-"+token(16));
    const helpers=authorizationServer(env).getOAuthApi(env);
    const client=await helpers.createClient({clientName:"Local relay transport smoke test",redirectUris:[url.origin+"/test/callback"],
      tokenEndpointAuthMethod:"none",grantTypes:["authorization_code"],responseTypes:["code"]});
    const verifier=token(),hash=await crypto.subtle.digest("SHA-256",new TextEncoder().encode(verifier));
    const challenge=btoa(String.fromCharCode(...new Uint8Array(hash))).replace(/\+/g,"-").replace(/\//g,"_").replace(/=+$/,"");
    const grant=await helpers.completeAuthorization({request:{responseType:"code",clientId:client.clientId,redirectUri:client.redirectUris[0],
      scope:[...SCOPES],state:"test",resource:url.origin+"/mcp",issuer:url.origin,codeChallenge:challenge,codeChallengeMethod:"S256"},
      userId,scope:[...SCOPES],metadata:{},props:{userId,ownerKey}});
    return Response.json({owner_key:ownerKey,worker_id:worker.worker_id,client_id:client.clientId,
      code:new URL(grant.redirectTo).searchParams.get("code"),code_verifier:verifier,redirect_uri:client.redirectUris[0]},{headers:{"Cache-Control":"no-store"}});
  }
} satisfies ExportedHandler<Env>;
