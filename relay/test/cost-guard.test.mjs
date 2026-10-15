import {test} from "node:test";
import assert from "node:assert/strict";
import {readFile,mkdtemp,writeFile,rm,mkdir,readdir,lstat,rename,symlink,unlink} from "node:fs/promises";
import {createHash,webcrypto} from "node:crypto";
import {runInNewContext} from "node:vm";
import {tmpdir} from "node:os";
import {join,dirname,resolve,basename,isAbsolute,relative} from "node:path";
import {proveFreeSubscriptions,validateFreeConfig,checkFreeAccount,validateEntraSettings,validateCompanionSetupSettings} from "../scripts/cost-guard.mjs";
import {companionSetupSettings} from "../src/companion-setup.ts";
import {deployFree} from "../scripts/deploy-free.mjs";
import {stageWalkthroughAssets,validateWalkthroughPublication,verifyStagedWalkthroughAssets} from "../scripts/walkthrough-assets.mjs";
import {validateDirectorySettings} from "../scripts/directory-config.mjs";

function snapshot(result=[]) {return {success:true,errors:[],result,result_info:{total_count:result.length}};}
function plan(id="free",publicName="Workers Free") {return {state:"Provisioned",price:0,rate_plan:{id,public_name:publicName,scope:"account"}};}
test("accepts a complete implicit or explicit Free account",()=>{
  assert.equal(proveFreeSubscriptions(snapshot()).plan,"free");
  assert.equal(proveFreeSubscriptions(snapshot([plan()])).plan,"free");
});
test("refuses paid usage even at zero current invoice",()=>{
  for(const input of [plan("workers_paid","Workers Paid"),{...plan(),state:"Trial"},{...plan(),price:5},
    {state:"Provisioned",price:0,rate_plan:{id:"unrecognized",public_name:"Unknown",scope:"account"}}])
    assert.throws(()=>proveFreeSubscriptions(snapshot([input])));
});
test("refuses incomplete, malformed or unavailable billing proof",async()=>{
  for(const input of [{}, {success:false,result:[]}, {...snapshot(),result_info:{total_count:1}},
    {...snapshot(),result_info:{total_count:0,next_page:true}}, {...snapshot(),result_info:{total_count:0,total_pages:2}},
    {...snapshot(),result_info:{total_count:0,count:1}}, snapshot([{}])]) assert.throws(()=>proveFreeSubscriptions(input));
  await assert.rejects(checkFreeAccount("a".repeat(32),"private",async()=>new Response(null,{status:403})));
});
test("separates existing independent products while still rejecting paid or unknown Workers",()=>{
  const unrelated=[plan("r2_paid","R2 Paid"),plan("cache_reserve_paid","Cache Reserve"),plan("teams_free","Teams Free Base")];
  assert.deepEqual(proveFreeSubscriptions(snapshot(unrelated)).unrelated_existing_products,["r2_paid","cache_reserve_paid","teams_free"]);
  assert.throws(()=>proveFreeSubscriptions(snapshot([...unrelated,plan("workers_paid","Workers Paid")])));
  assert.throws(()=>proveFreeSubscriptions(snapshot([...unrelated,plan("unknown","Unknown Account Product")])));
  for(const mutation of [{public_name:"Workers Paid"},{is_contract:true},{externally_managed:true},{sets:["unknown-addon"]}]) {
    const altered={...unrelated[0],rate_plan:{...unrelated[0].rate_plan,...mutation}};
    assert.throws(()=>proveFreeSubscriptions(snapshot([altered])));
  }
});
test("does not change billing and binds verification to the explicit account",async()=>{
  await checkFreeAccount("a".repeat(32),"private",async(url,options)=>{
    assert.equal(url,"https://api.cloudflare.com/client/v4/accounts/"+"a".repeat(32)+"/subscriptions");
    assert.equal(options.method,"GET");assert.equal(options.redirect,"error");return Response.json(snapshot());
  });
});
test("allows only free relay bindings and no automatic paid additions",async()=>{
  const config=JSON.parse(await readFile(new URL("../wrangler.jsonc",import.meta.url),"utf8"));
  config.account_id="a".repeat(32);config.vars.PUBLIC_ORIGIN="https://astra-antigravity-relay.existing.workers.dev";
  config.vars.DEPLOYMENT_MODE="verified-free";config.kv_namespaces[0].id="b".repeat(32);
  assert.equal(validateFreeConfig(config),config.vars.PUBLIC_ORIGIN);
  for(const mutation of [{ai:{binding:"AI"}},{containers:[]},{services:[]},{observability:{enabled:true}},{env:{production:{}}},
    {rules:[{type:"Data",globs:["**/*"]}]}])
    assert.throws(()=>validateFreeConfig({...config,...mutation}));
  assert.throws(()=>validateFreeConfig({...config,vars:{...config.vars,DEPLOYMENT_MODE:"local"}}));
});

async function deploymentSettings(action,overrides={}) {
  const directory=await mkdtemp(join(tmpdir(),"astra-cost-test-"));
  try {
    const settings=join(directory,"settings.json");
    await writeFile(settings,JSON.stringify({account_id:"a".repeat(32),oauth_kv_id:"b".repeat(32),
      origin:"https://astra-antigravity-relay.existing.workers.dev",...overrides}));
    await action(settings);
  } finally {
    if(dirname(resolve(directory))!==resolve(tmpdir()) || !basename(directory).startsWith("astra-cost-test-")) throw new Error("Unexpected cleanup target");
    await rm(directory,{recursive:true,force:true});
  }
}

const companionSetup={bootstrap_url:"https://github.com/ramiradwan/astra-antigravity/releases/download/companion-v0.4.1/delegate-to-antigravity-0.4.1.tgz",bootstrap_sha256:"d".repeat(64)};

test("companion setup settings accept only the complete nonsecret version-pinned release pair",()=>{
  assert.equal(validateCompanionSetupSettings(undefined),undefined);
  assert.equal(validateCompanionSetupSettings(companionSetup),companionSetup);
  for(const value of [null,[],{}, {bootstrap_url:companionSetup.bootstrap_url},{bootstrap_sha256:companionSetup.bootstrap_sha256},
    {...companionSetup,bootstrap_sha256:"d".repeat(63)},{...companionSetup,bootstrap_sha256:"d".repeat(64)+"\n"},
    {...companionSetup,bootstrap_url:companionSetup.bootstrap_url.replace("companion-v0.4.1","latest")},
    {...companionSetup,bootstrap_url:companionSetup.bootstrap_url.replace("0.4.1.tgz","0.4.2.tgz")},
    {...companionSetup,bootstrap_url:companionSetup.bootstrap_url.replace("github.com","example.com")},
    {...companionSetup,bootstrap_url:companionSetup.bootstrap_url+"?download=1"},
    {...companionSetup,bootstrap_url:companionSetup.bootstrap_url+"\n"},
    {...companionSetup,command:"npx arbitrary"},{...companionSetup,client_secret:"synthetic-not-a-public-setting"}])
    assert.throws(()=>validateCompanionSetupSettings(value),/Companion setup/);
});

test("Free configuration accepts disabled setup or both exact release pins and rejects partial or arbitrary setup",async()=>{
  const config=await freeTestConfig();
  const pins={COMPANION_BOOTSTRAP_URL:companionSetup.bootstrap_url,COMPANION_BOOTSTRAP_SHA256:companionSetup.bootstrap_sha256};
  assert.equal(validateFreeConfig(config),config.vars.PUBLIC_ORIGIN);
  assert.equal(validateFreeConfig({...config,vars:{...config.vars,...pins}}),config.vars.PUBLIC_ORIGIN);
  for(const change of [{COMPANION_BOOTSTRAP_URL:""},{COMPANION_BOOTSTRAP_SHA256:""},
    {COMPANION_BOOTSTRAP_URL:companionSetup.bootstrap_url+"; calc"},{COMPANION_BOOTSTRAP_SHA256:null},
    {COMPANION_SETUP_COMMAND:"npx arbitrary"}])
    assert.throws(()=>validateFreeConfig({...config,vars:{...config.vars,...pins,...change}}),/Companion setup|Unexpected variable/);
});

test("guarded check and deploy forward verified release pins without changing Free-only resources",async()=>{
  for(const mode of ["--check","--deploy"]){
    await deploymentSettings(async settings=>{
      let validations=0;
      await deployFree([mode,settings],{
        environment:{CLOUDFLARE_API_TOKEN:"synthetic-deploy-secret"},
        request:async url=>url.endsWith("/subscriptions")?Response.json(snapshot()):Response.json({success:true,result:{subdomain:"existing"}}),
        run:async(_binary,args)=>{
          const config=JSON.parse(await readFile(args[args.indexOf("--config")+1],"utf8"));
          assert.equal(config.vars.COMPANION_BOOTSTRAP_URL,companionSetup.bootstrap_url);
          assert.equal(config.vars.COMPANION_BOOTSTRAP_SHA256,companionSetup.bootstrap_sha256);
          assert.deepEqual(companionSetupSettings(config.vars),{setup_command:"npx --yes "+companionSetup.bootstrap_url+" setup"});
          assert.equal(config.vars.PUBLISHER_COST_POLICY,"free-only");
          assert.equal(config.kv_namespaces.length,1);assert.equal(config.durable_objects.bindings.length,3);
          assert.equal(config.services,undefined);assert.equal(config.routes,undefined);validations++;
          return {stdout:"Synthetic validation complete"};
        },output:()=>{}
      });
      assert.equal(validations,mode==="--deploy"?2:1);
    },{companion_setup:companionSetup});
  }
});

test("invalid companion setup is rejected before reading account state, credentials or invoking Wrangler",async()=>{
  for(const value of [null,{}, {bootstrap_url:companionSetup.bootstrap_url},
    {...companionSetup,bootstrap_url:"https://example.com/installer.tgz"},
    {...companionSetup,setup_command:"arbitrary shell command"}])
    await deploymentSettings(async settings=>{
      let calls=0;
      await assert.rejects(deployFree(["--deploy",settings],{
        environment:{},request:async()=>{calls++;throw new Error("Unexpected account operation");},
        run:async()=>{calls++;throw new Error("Unexpected CLI operation");},output:()=>{calls++;}
      }),/Companion setup/);
      assert.equal(calls,0);
    },{companion_setup:value});
});

test("omitting companion_setup preserves disabled setup defaults during a guarded check",async()=>{
  await deploymentSettings(async settings=>{
    await deployFree(["--check",settings],{
      environment:{CLOUDFLARE_API_TOKEN:"synthetic-deploy-secret"},
      request:async url=>url.endsWith("/subscriptions")?Response.json(snapshot()):Response.json({success:true,result:{subdomain:"existing"}}),
      run:async(_binary,args)=>{
        const config=JSON.parse(await readFile(args[args.indexOf("--config")+1],"utf8"));
        assert.equal(config.vars.COMPANION_BOOTSTRAP_URL,"");assert.equal(config.vars.COMPANION_BOOTSTRAP_SHA256,"");
        assert.ok(!Object.hasOwn(companionSetupSettings(config.vars),"setup_command"));
        return {stdout:"Synthetic validation complete"};
      },output:()=>{}
    });
  });
});

const entraSettings={tenant_id:"11111111-2222-3333-4444-555555555555",
  authority:"https://astra-demo.ciamlogin.com/11111111-2222-3333-4444-555555555555",required_role:"Astra.User"};
const customEntraSettings={...entraSettings,custom_domain:"login.example.test",
  authority:"https://login.example.test/"+entraSettings.tenant_id};
const directorySettings={tenantId:entraSettings.tenant_id,audience:"66666666-6666-4666-8666-666666666666",
  originAttribute:"extension_11111111222233334444555555555555_registrationSource",
  attributeCollection:{listenerId:"aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",extensionId:"bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"},
  tokenIssuance:{listenerId:"cccccccc-cccc-4ccc-8ccc-cccccccccccc",extensionId:"dddddddd-dddd-4ddd-8ddd-dddddddddddd"},
  applications:{"22222222-2222-4222-8222-222222222222":{registrationSource:"astra-antigravity",groupIds:["33333333-3333-4333-8333-333333333333"]}}};

test("directory configuration pins its tenant and allows only public app, hook and group identifiers",()=>{
  assert.equal(validateDirectorySettings(undefined),undefined);
  assert.equal(validateDirectorySettings(directorySettings,entraSettings.tenant_id),directorySettings);
  const invalid=[null,{},[],{...directorySettings,tenantId:"common"},{...directorySettings,audience:"https://evil.example"},
    {...directorySettings,originAttribute:"displayName"},{...directorySettings,client_secret:"never-a-public-setting"},
    {...directorySettings,issuer:"https://evil.example"},{...directorySettings,attributeCollection:{}},
    {...directorySettings,tokenIssuance:directorySettings.attributeCollection},{...directorySettings,applications:{}},
    {...directorySettings,applications:{"22222222-2222-4222-8222-222222222222":{registrationSource:"Astra Users",groupIds:[]}}},
    {...directorySettings,applications:{"22222222-2222-4222-8222-222222222222":{registrationSource:"astra-antigravity",groupIds:["33333333-3333-4333-8333-333333333333","44444444-4444-4444-8444-444444444444"]}}}];
  for(const value of invalid) assert.throws(()=>validateDirectorySettings(value,entraSettings.tenant_id),/Directory/);
  assert.throws(()=>validateDirectorySettings(directorySettings,"99999999-9999-4999-8999-999999999999"),/Directory/);
  const duplicateGroup=structuredClone(directorySettings);
  duplicateGroup.applications["44444444-4444-4444-8444-444444444444"]={registrationSource:"conversation-analytics",groupIds:["33333333-3333-4333-8333-333333333333"]};
  assert.throws(()=>validateDirectorySettings(duplicateGroup,entraSettings.tenant_id),/Directory/);
  const duplicateSource=structuredClone(duplicateGroup);
  duplicateSource.applications["44444444-4444-4444-8444-444444444444"]={registrationSource:"astra-antigravity",groupIds:["55555555-5555-4555-8555-555555555555"]};
  assert.throws(()=>validateDirectorySettings(duplicateSource,entraSettings.tenant_id),/Directory/);
});

test("directory hooks deploy through the existing Free guard without exposing writer credentials",async()=>{
  await deploymentSettings(async settings=>{
    let validations=0;
    await deployFree(["--check",settings],{
      environment:{CLOUDFLARE_API_TOKEN:"synthetic-deploy-secret"},
      request:async url=>url.endsWith("/subscriptions") ? Response.json(snapshot()) : Response.json({success:true,result:{subdomain:"existing"}}),
      run:async(_binary,args)=>{
        const config=JSON.parse(await readFile(args[args.indexOf("--config")+1],"utf8"));
        assert.deepEqual(JSON.parse(config.vars.DIRECTORY_HOOK_CONFIG),directorySettings);
        assert.equal(config.vars.DIRECTORY_GRAPH_CLIENT_ID,undefined); assert.equal(config.vars.DIRECTORY_GRAPH_CLIENT_SECRET,undefined);
        assert.equal(config.durable_objects.bindings[2].class_name,"DirectoryMembership");
        assert.equal(config.vars.ENTRA_TENANT_ID,entraSettings.tenant_id);
        assert.equal(config.vars.PUBLISHER_COST_POLICY,"free-only"); validations++;
        return {stdout:"Validated"};
      },output:()=>{}
    });
    assert.equal(validations,1);
  },{entra:entraSettings,directory_hooks:directorySettings});
});

test("invalid directory settings fail before any account read, secret use or deployment",async()=>{
  for(const overrides of [{directory_hooks:directorySettings},
    {entra:entraSettings,directory_hooks:{...directorySettings,client_secret:"synthetic"}},
    {entra:entraSettings,directory_hooks:{...directorySettings,tenantId:"99999999-9999-4999-8999-999999999999"}}]) {
    await deploymentSettings(async settings=>{
      let calls=0;
      await assert.rejects(deployFree(["--deploy",settings],{
        environment:{CLOUDFLARE_API_TOKEN:"synthetic"},request:async()=>{calls++;throw Error();},
        run:async()=>{calls++;throw Error();},output:()=>{}
      }),/Directory/);
      assert.equal(calls,0);
    },overrides);
  }
});

test("accepts only complete tenant-pinned CIAM settings and validates an optional app role",()=>{
  assert.equal(validateEntraSettings(undefined),undefined);
  assert.equal(validateEntraSettings(entraSettings),entraSettings);
  const unrestricted={tenant_id:entraSettings.tenant_id,authority:entraSettings.authority};
  assert.equal(validateEntraSettings(unrestricted),unrestricted);
  assert.equal(validateEntraSettings({...entraSettings,required_role:"Astra.Reviewer"}).required_role,"Astra.Reviewer");
  for(const value of [null,[],{}, {...entraSettings,client_secret:"synthetic-secret"}, {...entraSettings,client_id:"synthetic-id"},
    {...entraSettings,tenant_id:entraSettings.tenant_id+"\n"}, {...entraSettings,tenant_id:"common"},
    {...entraSettings,required_role:""}, {...entraSettings,required_role:"Astra.User\n"}, {...entraSettings,required_role:"Astra User"},
    {...entraSettings,authority:"http://astra-demo.ciamlogin.com/"+entraSettings.tenant_id},
    {...entraSettings,authority:"https://login.microsoftonline.com/"+entraSettings.tenant_id},
    {...entraSettings,authority:"https://astra-demo.ciamlogin.com/another-tenant"},
    {...entraSettings,authority:entraSettings.authority+"/"}, {...entraSettings,authority:entraSettings.authority+"\n"},
    {...entraSettings,authority:entraSettings.authority+"/v2.0"}, {...entraSettings,authority:entraSettings.authority+"?next=x"},
    {...entraSettings,authority:entraSettings.authority+"#fragment"},
    {...entraSettings,authority:entraSettings.authority.replace("astra-demo", "user@astra-demo")},
    {...entraSettings,authority:entraSettings.authority.replace(".ciamlogin.com", ".ciamlogin.com.evil.test")}])
    assert.throws(()=>validateEntraSettings(value),/Entra/);
});
test("accepts custom Entra authority only with an explicit exact DNS hostname pin",()=>{
  assert.equal(validateEntraSettings(customEntraSettings),customEntraSettings);
  assert.equal(validateEntraSettings({...entraSettings,custom_domain:customEntraSettings.custom_domain}).authority,entraSettings.authority);
  for(const domain of ["login.example.com","login2.example.test","sign-in.example.test","example.fi",
    "a".repeat(63)+".example.test",["a".repeat(63),"b".repeat(63),"c".repeat(63),"d".repeat(61)].join(".")]) {
    const settings={...entraSettings,custom_domain:domain,authority:`https://${domain}/${entraSettings.tenant_id}`};
    assert.equal(validateEntraSettings(settings),settings);
  }
  const {custom_domain:omitted,...unpinned}=customEntraSettings;
  for(const settings of [unpinned,{...customEntraSettings,custom_domain:"other.example.test"},
    {...customEntraSettings,authority:"https://login.example.test.evil.test/"+entraSettings.tenant_id},
    {...customEntraSettings,authority:"https://user@login.example.test/"+entraSettings.tenant_id},
    {...customEntraSettings,authority:"https://login.example.test:443/"+entraSettings.tenant_id},
    {...customEntraSettings,authority:"https://LOGIN.example.test/"+entraSettings.tenant_id},
    {...customEntraSettings,authority:"http://login.example.test/"+entraSettings.tenant_id},
    {...customEntraSettings,authority:"https://login.example.test/99999999-9999-4999-8999-999999999999"},
    {...customEntraSettings,authority:undefined}])
    assert.throws(()=>validateEntraSettings(settings),/Entra/);
  for(const suffix of ["/","/v2.0","?next=x","#fragment","\n"])
    assert.throws(()=>validateEntraSettings({...customEntraSettings,authority:customEntraSettings.authority+suffix}),/Entra/);
});
test("rejects malformed, normalized, local and IP custom-domain pins even with a CIAM authority",()=>{
  for(const custom_domain of [undefined,null,false,42,[],{},"","localhost","login","login.localhost","login.local",
    "login.internal","login.invalid","127.0.0.1","127.1","2130706433","0x7f000001","[::1]","::1",
    "https://login.example.test","login.example.test/","login.example.test:443","user@login.example.test",
    "login.example.test?query=x","login.example.test#fragment","login.example.test."," login.example.test",
    "login.example.test ","login.example.test\n","login\n.example.test","login\t.example.test","LOGIN.example.test",
    "*.example.test","login..example.test",".example.test","-login.example.test","login-.example.test",
    "login_example.test","login\\.example.test","login%2eexample.test","lögin.example.test","login.example.1",
    "login.example.x","a".repeat(64)+".example.test",["a".repeat(63),"b".repeat(63),"c".repeat(63),"d".repeat(62)].join(".")])
    assert.throws(()=>validateEntraSettings({...entraSettings,custom_domain}),/Entra/,String(custom_domain));
});
test("Free config accepts public Entra fields together and refuses client credentials",async()=>{
  const config=JSON.parse(await readFile(new URL("../wrangler.jsonc",import.meta.url),"utf8"));
  config.account_id="a".repeat(32);config.vars.PUBLIC_ORIGIN="https://astra-antigravity-relay.existing.workers.dev";
  config.vars.DEPLOYMENT_MODE="verified-free";config.kv_namespaces[0].id="b".repeat(32);
  const values={ENTRA_TENANT_ID:entraSettings.tenant_id,ENTRA_AUTHORITY:entraSettings.authority,ENTRA_REQUIRED_ROLE:entraSettings.required_role};
  assert.equal(validateFreeConfig({...config,vars:{...config.vars,...values}}),config.vars.PUBLIC_ORIGIN);
  for(const name of ["ENTRA_TENANT_ID","ENTRA_AUTHORITY"]) {
    const partial={...values};delete partial[name];
    assert.throws(()=>validateFreeConfig({...config,vars:{...config.vars,...partial}}),/Entra/);
  }
  const unrestricted={...values};delete unrestricted.ENTRA_REQUIRED_ROLE;
  assert.equal(validateFreeConfig({...config,vars:{...config.vars,...unrestricted}}),config.vars.PUBLIC_ORIGIN);
  for(const name of ["ENTRA_CLIENT_ID","ENTRA_CLIENT_SECRET"])
    assert.throws(()=>validateFreeConfig({...config,vars:{...config.vars,...values,[name]:"synthetic"}}),/secret/);
});
test("Free config validates the public custom-domain pin together with the tenant and authority",async()=>{
  const config=await freeTestConfig();
  const values={ENTRA_TENANT_ID:customEntraSettings.tenant_id,ENTRA_AUTHORITY:customEntraSettings.authority,
    ENTRA_CUSTOM_DOMAIN:customEntraSettings.custom_domain};
  assert.equal(validateFreeConfig({...config,vars:{...config.vars,...values}}),config.vars.PUBLIC_ORIGIN);
  for(const name of ["ENTRA_TENANT_ID","ENTRA_AUTHORITY","ENTRA_CUSTOM_DOMAIN"]) {
    const partial={...values};delete partial[name];
    assert.throws(()=>validateFreeConfig({...config,vars:{...config.vars,...partial}}),/Entra/);
  }
  for(const ENTRA_CUSTOM_DOMAIN of ["evil.example.test","login.example.test.",undefined])
    assert.throws(()=>validateFreeConfig({...config,vars:{...config.vars,...values,ENTRA_CUSTOM_DOMAIN}}),/Entra/);
  assert.throws(()=>validateFreeConfig({...config,vars:{...config.vars,ENTRA_CUSTOM_DOMAIN:values.ENTRA_CUSTOM_DOMAIN}}),/Entra/);
});
test("maps Entra settings while preserving domain challenge, identity storage and secret separation",async()=>{
  const challenge="a".repeat(43);
  await deploymentSettings(async settings=>{
    let deployments=0;
    await deployFree(["--deploy",settings],{
      environment:{CLOUDFLARE_API_TOKEN:"synthetic-deploy-secret"},
      request:async url=>url.endsWith("/subscriptions") ? Response.json(snapshot()) : Response.json({success:true,result:{subdomain:"existing"}}),
      run:async(_binary,args)=>{
        const config=JSON.parse(await readFile(args[args.indexOf("--config")+1],"utf8"));
        assert.equal(config.vars.ENTRA_TENANT_ID,entraSettings.tenant_id);
        assert.equal(config.vars.ENTRA_AUTHORITY,entraSettings.authority);
        assert.ok(!Object.hasOwn(config.vars,"ENTRA_CUSTOM_DOMAIN"));
        assert.equal(config.vars.ENTRA_REQUIRED_ROLE,"Astra.User");
        assert.equal(config.vars.OPENAI_APPS_CHALLENGE,challenge);
        assert.equal(config.vars.ENTRA_CLIENT_ID,undefined);assert.equal(config.vars.ENTRA_CLIENT_SECRET,undefined);
        assert.equal(config.durable_objects.bindings.length,3);assert.equal(config.kv_namespaces.length,1);
        deployments++;return {stdout:"Deployed"};
      },output:()=>{}
    });
    assert.equal(deployments,2);
  },{entra:entraSettings,openai_apps_challenge:challenge});
});
test("check and deploy forward a pinned custom domain through the existing guarded configuration",async()=>{
  for(const mode of ["--check","--deploy"]) {
    await deploymentSettings(async settings=>{
      let validations=0;
      await deployFree([mode,settings],{
        environment:{CLOUDFLARE_API_TOKEN:"synthetic-deploy-secret"},
        request:async url=>url.endsWith("/subscriptions") ? Response.json(snapshot()) : Response.json({success:true,result:{subdomain:"existing"}}),
        run:async(_binary,args)=>{
          const config=JSON.parse(await readFile(args[args.indexOf("--config")+1],"utf8"));
          assert.equal(config.vars.ENTRA_CUSTOM_DOMAIN,customEntraSettings.custom_domain);
          assert.equal(config.vars.ENTRA_AUTHORITY,customEntraSettings.authority);
          assert.equal(config.vars.ENTRA_TENANT_ID,customEntraSettings.tenant_id);
          assert.deepEqual(JSON.parse(config.vars.DIRECTORY_HOOK_CONFIG),directorySettings);
          assert.equal(config.vars.PUBLIC_ORIGIN,"https://astra-antigravity-relay.existing.workers.dev");
          assert.equal(config.vars.PUBLISHER_COST_POLICY,"free-only");
          assert.equal(config.durable_objects.bindings.length,3);assert.equal(config.kv_namespaces.length,1);
          assert.equal(config.services,undefined);assert.equal(config.routes,undefined);
          validations++;return {stdout:"Validated"};
        },output:()=>{}
      });
      assert.equal(validations,mode==="--deploy" ? 2 : 1);
    },{entra:customEntraSettings,directory_hooks:directorySettings});
  }
});
test("rejects invalid private Entra settings before account reads or CLI operations",async()=>{
  for(const value of [null,{...entraSettings,required_role:""},{...entraSettings,client_secret:"synthetic-secret"},
    {...entraSettings,authority:"https://evil.example/"+entraSettings.tenant_id},
    {...entraSettings,authority:customEntraSettings.authority},
    {...customEntraSettings,custom_domain:"evil.example.test"},
    {...customEntraSettings,custom_domain:"login.example.test."}])
    await deploymentSettings(async settings=>{
      let calls=0;
      await assert.rejects(deployFree(["--deploy",settings],{
        environment:{CLOUDFLARE_API_TOKEN:"synthetic-deploy-secret"},
        run:async()=>{calls++;throw new Error("Unexpected CLI operation");},
        request:async()=>{calls++;throw new Error("Unexpected account operation");},output:()=>{}
      }),/Entra/);
      assert.equal(calls,0);
    },{entra:value});
});
test("keeps role gating unset for self-service Entra deployment settings",async()=>{
  const {required_role:omitted,...unrestricted}=entraSettings;
  await deploymentSettings(async settings=>{
    let validations=0;
    await deployFree(["--check",settings],{
      environment:{CLOUDFLARE_API_TOKEN:"synthetic-deploy-secret"},
      request:async url=>url.endsWith("/subscriptions") ? Response.json(snapshot()) : Response.json({success:true,result:{subdomain:"existing"}}),
      run:async(_binary,args)=>{
        const config=JSON.parse(await readFile(args[args.indexOf("--config")+1],"utf8"));
        assert.equal(config.vars.ENTRA_TENANT_ID,entraSettings.tenant_id);
        assert.equal(config.vars.ENTRA_AUTHORITY,entraSettings.authority);
        assert.ok(!Object.hasOwn(config.vars,"ENTRA_REQUIRED_ROLE"));validations++;
        return {stdout:"Validated"};
      },output:()=>{}
    });
    assert.equal(validations,1);
  },{entra:unrestricted});
});
test("uses the separate billing token only for reads and never forwards it to Wrangler",async()=>{
  await deploymentSettings(async settings=>{
    let billingReads=0,deployments=0,authReads=0;
    const logs=[];
    await deployFree(["--deploy",settings],{
      environment:{CLOUDFLARE_BILLING_API_TOKEN:"synthetic-billing-secret"},
      request:async(url,options)=>{
        if(url.endsWith("/subscriptions")) {
          assert.equal(options.headers.Authorization,"Bearer synthetic-billing-secret");
          assert.equal(options.method,"GET");billingReads++;
          return Response.json(snapshot());
        }
        assert.ok(url.endsWith("/workers/subdomain"));
        assert.equal(options.headers.Authorization,"Bearer synthetic-deploy-secret");
        return Response.json({success:true,result:{subdomain:"existing"}});
      },
      run:async(binary,args,options)=>{
        assert.equal(options.env.CLOUDFLARE_BILLING_API_TOKEN,undefined);
        assert.ok(!JSON.stringify(args).includes("synthetic-billing-secret"));
        if(args[1]==="auth") {authReads++;return {stdout:JSON.stringify({type:"oauth",token:"synthetic-deploy-secret"})};}
        assert.equal(args[1],"deploy");
        assert.equal(options.env.CLOUDFLARE_API_TOKEN,"synthetic-deploy-secret");
        deployments++;
        return {stdout:"Deployed to the selected Free relay\n"};
      },
      output:value=>logs.push(value)
    });
    assert.equal(authReads,1);assert.equal(billingReads,2);assert.equal(deployments,2);
    assert.ok(!logs.join("").includes("synthetic-billing-secret"));
  });
});
test("accepts only a disabled or single bounded domain-verification token",async()=>{
  const config=JSON.parse(await readFile(new URL("../wrangler.jsonc",import.meta.url),"utf8"));
  config.account_id="a".repeat(32);config.vars.PUBLIC_ORIGIN="https://astra-antigravity-relay.existing.workers.dev";
  config.vars.DEPLOYMENT_MODE="verified-free";config.kv_namespaces[0].id="b".repeat(32);
  for(const value of [undefined,"","a".repeat(42)+"_"])
    assert.equal(validateFreeConfig({...config,vars:{...config.vars,OPENAI_APPS_CHALLENGE:value}}),config.vars.PUBLIC_ORIGIN);
  for(const value of [null,false,43,[],{},"a".repeat(42),"a".repeat(44),"a".repeat(42)+"+","a".repeat(43)+"\n"])
    assert.throws(()=>validateFreeConfig({...config,vars:{...config.vars,OPENAI_APPS_CHALLENGE:value}}),/domain challenge/);
});
test("maps the private challenge setting without a new binding or reflected deployment output",async()=>{
  const challenge="a".repeat(42)+"_",logs=[];
  await deploymentSettings(async settings=>{
    let deployments=0;
    await deployFree(["--deploy",settings],{
      environment:{CLOUDFLARE_API_TOKEN:"synthetic-deploy-secret"},
      request:async url=>url.endsWith("/subscriptions") ? Response.json(snapshot()) : Response.json({success:true,result:{subdomain:"existing"}}),
      run:async(_binary,args)=>{
        assert.equal(args[1],"deploy");
        const config=JSON.parse(await readFile(args[args.indexOf("--config")+1],"utf8"));
        assert.equal(config.vars.OPENAI_APPS_CHALLENGE,challenge);
        assert.equal(config.durable_objects.bindings.length,3);
        assert.equal(config.kv_namespaces.length,1);
        deployments++;
        return {stdout:`Domain verification configured: ${challenge}\n`};
      },output:value=>logs.push(value)
    });
    assert.equal(deployments,2);
  },{openai_apps_challenge:challenge});
  assert.ok(!logs.join("").includes(challenge));
  assert.ok(logs.join("").includes("<configured-domain-challenge>"));
});
test("rejects invalid private challenge settings before any account or CLI operation",async()=>{
  for(const value of [null,false," "+"a".repeat(43),"a".repeat(43)+"\n"]){
    await deploymentSettings(async settings=>{
      let calls=0;
      await assert.rejects(deployFree(["--deploy",settings],{
        environment:{CLOUDFLARE_API_TOKEN:"synthetic-deploy-secret"},
        run:async()=>{calls++;throw new Error("Unexpected CLI operation");},
        request:async()=>{calls++;throw new Error("Unexpected account operation");},output:()=>{}
      }),/domain challenge/);
      assert.equal(calls,0);
    },{openai_apps_challenge:value});
  }
});
test("a paid plan found through a separate billing token prevents even the dry run",async()=>{
  await deploymentSettings(async settings=>{
    let cliCalls=0;
    await assert.rejects(deployFree(["--deploy",settings],{
      environment:{CLOUDFLARE_API_TOKEN:"synthetic-deploy-secret",CLOUDFLARE_BILLING_API_TOKEN:"synthetic-billing-secret"},
      run:async()=>{cliCalls++;throw new Error("Wrangler must not run");},
      request:async(url,options)=>{
        assert.ok(url.endsWith("/subscriptions"));
        assert.equal(options.headers.Authorization,"Bearer synthetic-billing-secret");
        return Response.json(snapshot([plan("workers_paid","Workers Paid")]));
      },output:()=>{}
    }),/paid.*deployment refused/);
    assert.equal(cliCalls,0);
  });
});

const sha256=data=>createHash("sha256").update(data).digest("hex");
async function walkthroughFixture(action) {
  const directory=await mkdtemp(join(tmpdir(),"astra-media-test-"));
  try {
    const video=Buffer.from("000000106674797069736f6d00000000","hex");
    const captions=Buffer.from("WEBVTT\n\n00:00.000 --> 00:01.000\nSynthetic sample.\n");
    const videoPath=join(directory,"reviewed.mp4"),captionsPath=join(directory,"reviewed.vtt"),staging=join(directory,"staging");
    await writeFile(videoPath,video);await writeFile(captionsPath,captions);await mkdir(staging);
    await action({directory,staging,video,captions,settings:{publication_approved:false,
      mp4:{path:videoPath,sha256:sha256(video),bytes:video.length},
      captions:{path:captionsPath,sha256:sha256(captions),bytes:captions.length}}});
  } finally {
    if(dirname(resolve(directory))!==resolve(tmpdir()) || !basename(directory).startsWith("astra-media-test-")) throw new Error("Unexpected media test cleanup target");
    await rm(directory,{recursive:true,force:true});
  }
}
async function freeTestConfig() {
  const config=JSON.parse(await readFile(new URL("../wrangler.jsonc",import.meta.url),"utf8"));
  config.account_id="a".repeat(32);config.vars.PUBLIC_ORIGIN="https://astra-antigravity-relay.existing.workers.dev";
  config.vars.DEPLOYMENT_MODE="verified-free";config.kv_namespaces[0].id="b".repeat(32);
  return config;
}

test("stages only pinned media and fixed headers in an absolute directory",async()=>{
  await walkthroughFixture(async fixture=>{
    assert.equal(await stageWalkthroughAssets(undefined,fixture.staging,false),null);
    await verifyStagedWalkthroughAssets(null);
    const publication=await stageWalkthroughAssets(fixture.settings,fixture.staging,false);
    assert.ok(isAbsolute(publication.directory));
    assert.equal(publication.directory,join(fixture.staging,"public-walkthrough"));
    const stem="astra-antigravity-0.12.1-"+fixture.settings.mp4.sha256;
    assert.equal(publication.publicPath,"/review/"+stem+".html");
    assert.equal(publication.mediaPath,"/review/"+stem+".mp4");
    assert.deepEqual((await readdir(publication.directory)).sort(),["_headers","review"]);
    assert.deepEqual((await readdir(join(publication.directory,"review"))).sort(),[stem+".html",stem+".mp4",stem+".vtt"]);
    assert.deepEqual(await readFile(join(publication.directory,"review",stem+".mp4")),fixture.video);
    assert.deepEqual(await readFile(join(publication.directory,"review",stem+".vtt")),fixture.captions);
    for(const item of publication.manifest) {
      const bytes=await readFile(join(publication.directory,item.relative));
      assert.equal(bytes.length,item.bytes);assert.equal(sha256(bytes),item.sha256);
    }
    assert.match(await readFile(join(publication.directory,"_headers"),"utf8"),/^\/review\/\*\n  X-Content-Type-Options: nosniff\n  Referrer-Policy: no-referrer\n  X-Robots-Tag: noindex, nofollow, noarchive\n/);
    await verifyStagedWalkthroughAssets(publication);
  });
});

test("requires an explicit publication decision and bounded absolute media pins",async()=>{
  await walkthroughFixture(async ({settings})=>{
    assert.doesNotThrow(()=>validateWalkthroughPublication(settings,false));
    assert.doesNotThrow(()=>validateWalkthroughPublication({...settings,publication_approved:true},true));
    for(const value of [undefined,null,0,"true"])
      assert.throws(()=>validateWalkthroughPublication({...settings,publication_approved:value},false),/approval/);
    assert.throws(()=>validateWalkthroughPublication(settings,true),/approval/);
    for(const change of [{path:"relative.mp4"},{path:""},{path:settings.mp4.path+".html"},{sha256:"a".repeat(63)},
      {sha256:"A".repeat(64)},{bytes:0},{bytes:1.5},{bytes:25*1024*1024+1},{extra:"unapproved"}])
      assert.throws(()=>validateWalkthroughPublication({...settings,mp4:{...settings.mp4,...change}},false));
    assert.throws(()=>validateWalkthroughPublication({...settings,captions:{...settings.captions,bytes:128*1024+1}},false));
    assert.throws(()=>validateWalkthroughPublication({...settings,redirects:"_redirects"},false),/Unexpected/);
  });
});

test("labels a new recording version while retaining exact media pins and legacy default URLs",async()=>{
  await walkthroughFixture(async fixture=>{
    const publication=await stageWalkthroughAssets({...fixture.settings,version:"0.16.0"},fixture.staging,false);
    const stem="astra-antigravity-0.16.0-"+fixture.settings.mp4.sha256;
    assert.equal(publication.publicPath,"/review/"+stem+".html");
    assert.equal(publication.mediaPath,"/review/"+stem+".mp4");
    const html=await readFile(join(publication.directory,"review",stem+".html"),"utf8");
    assert.match(html,/<p>Recording version 0\.16\.0\. The setup and verification scope are stated in the video\.<\/p>/);
    assert.match(html,/<video id="player" controls playsinline/);
    assert.ok(html.includes('./'+stem+'.vtt'));
    assert.deepEqual(await readFile(join(publication.directory,"review",stem+".mp4")),fixture.video);
    assert.deepEqual(await readFile(join(publication.directory,"review",stem+".vtt")),fixture.captions);
    await verifyStagedWalkthroughAssets(publication);
  });
});

test("rejects unsafe, ambiguous or unbounded recording versions before staging files",async()=>{
  await walkthroughFixture(async fixture=>{
    for(const version of [null,0,false,"","latest","v0.16.0","00.16.0","0.016.0","0.16.00","1000.1.0", "0.16.0-beta",
      "0.16.0/extra","../0.16.0","0.16.0?other=1","0.16.0\n",'<script>alert(1)</script>']) {
      await assert.rejects(stageWalkthroughAssets({...fixture.settings,version},fixture.staging,false),/Walkthrough version/);
      assert.deepEqual(await readdir(fixture.staging),[]);
    }
    assert.doesNotThrow(()=>validateWalkthroughPublication({...fixture.settings,version:"0.16.0"},false));
    assert.doesNotThrow(()=>validateWalkthroughPublication({...fixture.settings,version:"1.2.3"},false));
  });
});

test("rejects changed source bytes, size, file type and staging path",async()=>{
  await walkthroughFixture(async fixture=>{
    const {settings,staging}=fixture;
    await assert.rejects(stageWalkthroughAssets({...settings,mp4:{...settings.mp4,sha256:"0".repeat(64)}},staging,false),/reviewed bytes/);
    await assert.rejects(stageWalkthroughAssets({...settings,mp4:{...settings.mp4,bytes:settings.mp4.bytes+1}},staging,false),/bounded regular file/);
    const folder=join(fixture.directory,"folder.mp4");await mkdir(folder);
    await assert.rejects(stageWalkthroughAssets({...settings,mp4:{...settings.mp4,path:folder}},staging,false),/bounded regular file/);
    await assert.rejects(stageWalkthroughAssets(settings,"relative-staging",false),/absolute temporary directory/);
    const invalid=Buffer.alloc(fixture.video.length);await writeFile(settings.mp4.path,invalid);
    await assert.rejects(stageWalkthroughAssets({...settings,mp4:{...settings.mp4,sha256:sha256(invalid)}},staging,false),/MP4 container/);
  });
});

test("validates a WebVTT signature boundary and UTF-8 before staging",async()=>{
  await walkthroughFixture(async fixture=>{
    for(const text of ["WEBVTTsuffix\n", "not captions\n",Buffer.from([0xff,0xfe])]) {
      const data=Buffer.from(text);await writeFile(fixture.settings.captions.path,data);
      await assert.rejects(stageWalkthroughAssets({...fixture.settings,captions:{...fixture.settings.captions,
        bytes:data.length,sha256:sha256(data)}},fixture.staging,false));
    }
  });
  await walkthroughFixture(async fixture=>{
    const settings={...fixture.settings};delete settings.captions;
    const publication=await stageWalkthroughAssets(settings,fixture.staging,false);
    assert.equal(publication.manifest.length,3);
    assert.equal((await readdir(join(publication.directory,"review"))).length,2);
    const html=await readFile(join(publication.directory,"review",basename(publication.publicPath)),"utf8");
    assert.doesNotMatch(html,/<track|Download captions|\.vtt/);
    await verifyStagedWalkthroughAssets(publication);
  });
});

test("revalidates the exact staged file list, sizes and digests",async()=>{
  await walkthroughFixture(async fixture=>{
    const publication=await stageWalkthroughAssets(fixture.settings,fixture.staging,false);
    const extra=join(publication.directory,"_redirects");await writeFile(extra,"/mcp /review/example.mp4 200\n");
    await assert.rejects(verifyStagedWalkthroughAssets(publication),/unexpected files/);await unlink(extra);
    const media=join(publication.directory,publication.manifest[0].relative);
    const changed=Buffer.from(fixture.video);changed[changed.length-1]^=1;await writeFile(media,changed);
    await assert.rejects(verifyStagedWalkthroughAssets(publication),/changed after review/);
    await writeFile(media,fixture.video.subarray(1));
    await assert.rejects(verifyStagedWalkthroughAssets(publication),/bounded regular file/);
    await unlink(media);
    await assert.rejects(verifyStagedWalkthroughAssets(publication),/unexpected files/);
  });
});

test("rejects a linked staging root or descendant",async()=>{
  await walkthroughFixture(async fixture=>{
    const publication=await stageWalkthroughAssets(fixture.settings,fixture.staging,false);
    const original=publication.directory+"-original";
    assert.ok(!relative(fixture.directory,original).startsWith(".."));
    await rename(publication.directory,original);
    await symlink(original,publication.directory,process.platform==="win32" ? "junction" : "dir");
    await assert.rejects(verifyStagedWalkthroughAssets(publication),/root must be a regular directory/);
    await unlink(publication.directory);await rename(original,publication.directory);
    const review=join(publication.directory,"review"),moved=join(fixture.directory,"review-original");
    assert.ok(!relative(fixture.directory,moved).startsWith(".."));
    await rename(review,moved);await symlink(moved,review,process.platform==="win32" ? "junction" : "dir");
    await assert.rejects(verifyStagedWalkthroughAssets(publication),/contains a link/);
    await unlink(review);
  });
});

test("allows only temporary asset-first routing without fallback or new bindings",async()=>{
  const config=await freeTestConfig();
  const assets={directory:join(tmpdir(),"astra-free-deploy-test","public-walkthrough"),
    run_worker_first:false,not_found_handling:"none",html_handling:"none"};
  assert.equal(validateFreeConfig({...config,assets}),config.vars.PUBLIC_ORIGIN);
  for(const change of [{directory:"relative/public-walkthrough"},{directory:join(tmpdir(),"publisher-private","public-walkthrough")},
    {directory:join(tmpdir(),"astra-free-deploy-test","all-evidence")},{run_worker_first:true},{run_worker_first:["/*"]},
    {not_found_handling:"single-page-application"},{not_found_handling:"404-page"},{html_handling:"auto-trailing-slash"},
    {binding:"ASSETS"},{base_path:"/"}])
    assert.throws(()=>validateFreeConfig({...config,assets:{...assets,...change}}),/walkthrough/);
  for(const value of [null,[],{}]) assert.throws(()=>validateFreeConfig({...config,assets:value}),/walkthrough/);
});

test("media check and deploy retain the supplied challenge, origin and owned bindings",async()=>{
  await walkthroughFixture(async fixture=>{
    const challenge="c".repeat(43),logs=[];
    for(const mode of ["--check","--deploy"]) {
      let calls=0,stagedPath;
      await deploymentSettings(async settings=>{
        await deployFree([mode,settings],{
          environment:{CLOUDFLARE_API_TOKEN:"synthetic-deploy-secret"},
          request:async url=>url.endsWith("/subscriptions") ? Response.json(snapshot()) : Response.json({success:true,result:{subdomain:"existing"}}),
          run:async(_binary,args)=>{
            const config=JSON.parse(await readFile(args[args.indexOf("--config")+1],"utf8"));
            assert.equal(args[1],"deploy");assert.equal(args.includes("--dry-run"),calls===0);
            assert.equal(config.vars.OPENAI_APPS_CHALLENGE,challenge);
            assert.equal(config.vars.PUBLIC_ORIGIN,"https://astra-antigravity-relay.existing.workers.dev");
            assert.equal(config.vars.CALLBACK_HOSTS,"chatgpt.com,api.openai.com");
            assert.deepEqual(config.durable_objects.bindings,[{name:"OWNERS",class_name:"OwnerRelay"},{name:"PAIRINGS",class_name:"WorkerPairing"},{name:"DIRECTORY_MEMBERSHIP",class_name:"DirectoryMembership"}]);
            assert.deepEqual(config.kv_namespaces,[{binding:"OAUTH_KV",id:"b".repeat(32)}]);
            assert.equal(config.observability.enabled,false);
            assert.ok(isAbsolute(config.main));assert.ok(isAbsolute(config.assets.directory));stagedPath=config.assets.directory;
            assert.deepEqual(config.assets,{directory:stagedPath,run_worker_first:false,not_found_handling:"none",html_handling:"none"});
            assert.deepEqual((await readdir(stagedPath)).sort(),["_headers","review"]);
            calls++;return {stdout:`Synthetic successful deployment ${challenge}\n`};
          },output:value=>logs.push(value)
        });
      },{openai_apps_challenge:challenge,public_walkthrough:{...fixture.settings,publication_approved:mode==="--deploy"}});
      assert.equal(calls,mode==="--deploy" ? 2 : 1);
      await assert.rejects(lstat(stagedPath),{code:"ENOENT"});
    }
    assert.ok(!logs.join("").includes(challenge));
    assert.ok(logs.join("").includes("/review/astra-antigravity-0.12.1-"+fixture.settings.mp4.sha256+".html"));
  });
});

test("refuses unapproved publication or omitted challenge before any account or CLI operation",async()=>{
  await walkthroughFixture(async fixture=>{
    for(const change of [{openai_apps_challenge:undefined},{openai_apps_challenge:""},
      {public_walkthrough:{...fixture.settings,publication_approved:false}},
      {public_walkthrough:{...fixture.settings,publication_approved:true,mp4:{...fixture.settings.mp4,bytes:25*1024*1024+1}}}]) {
      await deploymentSettings(async settings=>{
        let calls=0;
        await assert.rejects(deployFree(["--deploy",settings],{
          environment:{CLOUDFLARE_API_TOKEN:"synthetic-deploy-secret"},
          request:async()=>{calls++;throw new Error("Unexpected account operation");},
          run:async()=>{calls++;throw new Error("Unexpected CLI operation");},output:()=>{}
        }));
        assert.equal(calls,0);
      },{openai_apps_challenge:"c".repeat(43),public_walkthrough:{...fixture.settings,publication_approved:true},...change});
    }
  });
});

test("staged tampering after dry run prevents the actual deploy",async()=>{
  await walkthroughFixture(async fixture=>{
    let calls=0,stagedPath;
    await deploymentSettings(async settings=>{
      await assert.rejects(deployFree(["--deploy",settings],{
        environment:{CLOUDFLARE_API_TOKEN:"synthetic-deploy-secret"},
        request:async url=>url.endsWith("/subscriptions") ? Response.json(snapshot()) : Response.json({success:true,result:{subdomain:"existing"}}),
        run:async(_binary,args)=>{
          assert.ok(args.includes("--dry-run"));calls++;
          const config=JSON.parse(await readFile(args[args.indexOf("--config")+1],"utf8"));stagedPath=config.assets.directory;
          await writeFile(join(stagedPath,"private-extra.txt"),"Never upload this unexpected file.");
          return {stdout:"Synthetic dry run complete\n"};
        },output:()=>{}
      }),/unexpected files/);
    },{openai_apps_challenge:"c".repeat(43),public_walkthrough:{...fixture.settings,publication_approved:true}});
    assert.equal(calls,1);await assert.rejects(lstat(stagedPath),{code:"ENOENT"});
  });
});

async function stagedPlayer(fixture) {
  const publication=await stageWalkthroughAssets(fixture.settings,fixture.staging,false);
  const html=await readFile(join(publication.directory,"review",basename(publication.publicPath)),"utf8");
  return {publication,html,script:html.match(/<script>([\s\S]*?)<\/script>/)[1],
    style:html.match(/<style>([\s\S]*?)<\/style>/)[1],headers:await readFile(join(publication.directory,"_headers"),"utf8")};
}
function playerHarness(script,respond) {
  const nodes=Object.fromEntries(["player","status","progress","retry","main"].map(id=>[id,{hidden:true,handlers:{},
    addEventListener(name,handler){this.handlers[name]=handler;},setAttribute(name,value){this[name]=value;},
    removeAttribute(name){delete this[name];},load(){this.loads=(this.loads ?? 0)+1;}}]));
  const requests=[],blobs=[],revoked=[],events={};
  const ready=runInNewContext(script,{document:{getElementById:id=>nodes[id],querySelector:()=>nodes.main},
    crypto:webcrypto,AbortController,Blob,Uint8Array,setTimeout,clearTimeout,
    URL:{createObjectURL(blob){blobs.push(blob);return "blob:reviewed-local-video";},revokeObjectURL:value=>revoked.push(value)},
    addEventListener:(name,handler)=>{events[name]=handler;},
    fetch:async(url,options)=>{requests.push({url,options});return respond(requests.length);}});
  return {nodes,requests,blobs,revoked,events,ready};
}
function videoResponse(...chunks) {
  return new Response(new ReadableStream({start(controller){for(const chunk of chunks) controller.enqueue(chunk);controller.close();}}));
}

test("generates a self-contained accessible player with exact CSP script and style hashes",async()=>{
  await walkthroughFixture(async fixture=>{
    const {publication,html,script,style,headers}=await stagedPlayer(fixture);
    assert.equal(publication.manifest.length,4);
    assert.ok(!html.includes(fixture.directory));assert.doesNotMatch(html,/https?:\/\/|file:\/\/|<script\s+src=|on(?:load|click)=/);
    assert.ok(script.includes(JSON.stringify({url:"./"+basename(publication.mediaPath),bytes:fixture.video.length,sha256:sha256(fixture.video)})));
    assert.doesNotMatch(script,/\r|Accept-Ranges|Content-Range|Range:/);
    assert.match(html,/<html lang="en">/);assert.match(html,/name="viewport"/);
    assert.match(html,/<video[^>]+controls playsinline[^>]+hidden/);assert.match(html,/<progress[^>]+aria-label=/);
    assert.match(html,/<p id="status" role="status">/);assert.match(html,/full .* MB video loads before playback/);
    assert.match(html,/<track kind="captions" srclang="en" label="English" src="\.\/[^"/]+\.vtt">/);
    assert.doesNotMatch(html,/<track[^>]*\bdefault\b/);
    assert.match(html,/download>Download MP4<\/a>/);assert.match(html,/download>Download captions<\/a>/);
    const hash=value=>createHash("sha256").update(value).digest("base64");
    assert.ok(headers.includes(publication.publicPath+"\n  Content-Security-Policy: default-src 'none'; script-src 'sha256-"+hash(script)+"'; style-src 'sha256-"+hash(style)+"'"));
    assert.match(headers,/connect-src 'self'; media-src 'self' blob:/);
    assert.match(headers,/base-uri 'none'; object-src 'none'; form-action 'none'; frame-ancestors 'none'/);
    assert.doesNotMatch(headers,/unsafe-inline|unsafe-eval|Accept-Ranges|Content-Length/);
    await verifyStagedWalkthroughAssets(publication);
  });
});

test("player loads same-origin bytes without credentials and verifies before creating its Blob",async()=>{
  await walkthroughFixture(async fixture=>{
    const {script,publication}=await stagedPlayer(fixture);
    const harness=playerHarness(script,()=>videoResponse(fixture.video.subarray(0,8),fixture.video.subarray(8)));
    assert.equal(harness.blobs.length,0);await harness.ready;
    assert.equal(harness.requests.length,1);
    assert.equal(harness.requests[0].url,"./"+basename(publication.mediaPath));
    const options=harness.requests[0].options;
    assert.equal(options.credentials,"omit");assert.equal(options.mode,"same-origin");assert.equal(options.redirect,"error");
    assert.equal(harness.blobs.length,1);assert.equal(harness.blobs[0].type,"video/mp4");
    assert.deepEqual(Buffer.from(await harness.blobs[0].arrayBuffer()),fixture.video);
    assert.equal(harness.nodes.player.src,"blob:reviewed-local-video");assert.equal(harness.nodes.player.hidden,false);
    assert.equal(harness.nodes.player.loads,1);assert.equal(harness.nodes.retry.hidden,true);
    assert.equal(harness.nodes.progress.value,100);assert.match(harness.nodes.status.textContent,/Ready\. Press Play/);
    harness.events.pagehide({persisted:true});assert.equal(harness.revoked.length,0);
    harness.events.pagehide({persisted:false});assert.deepEqual(harness.revoked,["blob:reviewed-local-video"]);
  });
});

test("player rejects oversized, incomplete and wrong-hash video without automatic retries",async()=>{
  await walkthroughFixture(async fixture=>{
    const {script}=await stagedPlayer(fixture),changed=Buffer.from(fixture.video);changed[changed.length-1]^=1;
    for(const bytes of [Buffer.concat([fixture.video,Buffer.from([0])]),fixture.video.subarray(1),changed]) {
      const harness=playerHarness(script,()=>videoResponse(bytes));await harness.ready;
      assert.equal(harness.requests.length,1);assert.equal(harness.blobs.length,0);
      assert.equal(harness.nodes.player.hidden,true);assert.equal(harness.nodes.retry.hidden,false);
      assert.equal(harness.nodes.retry.disabled,false);assert.match(harness.nodes.status.textContent,/Retry, or use Download MP4/);
    }
  });
});

test("player retry is manual and blocks concurrent duplicate downloads",async()=>{
  await walkthroughFixture(async fixture=>{
    const {script}=await stagedPlayer(fixture);
    const failed=playerHarness(script,attempt=>{if(attempt===1) throw new Error("Synthetic network failure");return videoResponse(fixture.video);});
    await failed.ready;assert.equal(failed.requests.length,1);assert.equal(failed.blobs.length,0);
    await failed.nodes.retry.handlers.click();assert.equal(failed.requests.length,2);assert.equal(failed.blobs.length,1);
    let release;
    const blocked=playerHarness(script,()=>new Promise(resolve=>{release=resolve;}));
    await blocked.nodes.retry.handlers.click();assert.equal(blocked.requests.length,1);
    release(videoResponse(fixture.video));await blocked.ready;assert.equal(blocked.blobs.length,1);
  });
});
