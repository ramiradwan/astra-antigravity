import {readFile,mkdtemp,writeFile,rm} from "node:fs/promises";
import {tmpdir} from "node:os";
import {join,resolve,dirname,basename} from "node:path";
import {fileURLToPath} from "node:url";
import {execFile} from "node:child_process";
import {promisify} from "node:util";
import {checkFreeAccount,validateFreeConfig,validateEntraSettings,validateCompanionSetupSettings} from "./cost-guard.mjs";
import {validateDirectorySettings} from "./directory-config.mjs";
import {stageWalkthroughAssets,validateWalkthroughPublication,verifyStagedWalkthroughAssets} from "./walkthrough-assets.mjs";

const exec = promisify(execFile), root=resolve(dirname(fileURLToPath(import.meta.url)),"..");
const cli=join(root,"node_modules/wrangler/bin/wrangler.js");

export async function deployFree(args, {environment=process.env,run=exec,request=fetch,output=text=>process.stdout.write(text)}={}) {
  if (args.length !== 2 || !["--check","--deploy"].includes(args[0])) throw new Error("Usage: npm run deploy:free -- --check|--deploy PRIVATE_SETTINGS.json");
  const settings=JSON.parse(await readFile(resolve(args[1]),"utf8"));
  if(!settings || typeof settings!=="object" || Array.isArray(settings)) throw new Error("Deployment settings must be a JSON object");
  if(Object.keys(settings).some(key=>!["account_id","origin","oauth_kv_id","openai_apps_challenge","public_walkthrough","entra","directory_hooks","companion_setup"].includes(key))) throw new Error("Unexpected private deployment setting");
  const entra=validateEntraSettings(settings.entra);
  const companionSetup=validateCompanionSetupSettings(settings.companion_setup);
  const directoryHooks=validateDirectorySettings(settings.directory_hooks,entra?.tenant_id);
  if(settings.public_walkthrough!==undefined && !/^[A-Za-z0-9_-]{43}$/.test(settings.openai_apps_challenge ?? ""))
    throw new Error("Walkthrough deployment must explicitly retain the current private domain challenge");
  validateWalkthroughPublication(settings.public_walkthrough,args[0]==="--deploy");
  const config=JSON.parse(await readFile(join(root,"wrangler.jsonc"),"utf8"));
  config.account_id=settings.account_id;
  config.vars.PUBLIC_ORIGIN=settings.origin;
  config.vars.DEPLOYMENT_MODE="verified-free";
  if(companionSetup) Object.assign(config.vars,{COMPANION_BOOTSTRAP_URL:companionSetup.bootstrap_url,COMPANION_BOOTSTRAP_SHA256:companionSetup.bootstrap_sha256});
  if(Object.hasOwn(settings,"openai_apps_challenge")) config.vars.OPENAI_APPS_CHALLENGE=settings.openai_apps_challenge;
  if(entra) {
    Object.assign(config.vars,{ENTRA_TENANT_ID:entra.tenant_id,ENTRA_AUTHORITY:entra.authority});
    if(entra.custom_domain!==undefined) config.vars.ENTRA_CUSTOM_DOMAIN=entra.custom_domain;
    if(entra.required_role!==undefined) config.vars.ENTRA_REQUIRED_ROLE=entra.required_role;
  }
  if(directoryHooks) config.vars.DIRECTORY_HOOK_CONFIG=JSON.stringify(directoryHooks);
  config.kv_namespaces[0].id=settings.oauth_kv_id;
  const origin=validateFreeConfig(config);
  // Capture the credential in process memory. Never emit Wrangler's token JSON,
  // errors, auth headers or API response bodies to a terminal/log/model context.
  // A Billing Read-only token can be supplied separately from Wrangler's
  // deployment credential. It is used only by the read-only account guard and
  // must never be inherited by Wrangler or become a Worker binding.
  const {CLOUDFLARE_BILLING_API_TOKEN:billingOverride,...cliEnvironment}=environment;
  let token=cliEnvironment.CLOUDFLARE_API_TOKEN;
  if(!token) {
    try {
      const auth=await run(process.execPath,[cli,"auth","token","--json"],{
        cwd:root,env:{...cliEnvironment,WRANGLER_SEND_METRICS:"false"},maxBuffer:65_536});
      const parsed=JSON.parse(auth.stdout);
      if(!["oauth","api_token"].includes(parsed.type) || typeof parsed.token !== "string") throw new Error();
      token=parsed.token;
    } catch { throw new Error("Sign in with wrangler login; no deployment was attempted"); }
  }
  const billingToken=billingOverride || token;
  const evidence=await checkFreeAccount(config.account_id,billingToken,request);
  // Bind a workers.dev origin to this account's existing subdomain.
  const reply=await request(`https://api.cloudflare.com/client/v4/accounts/${config.account_id}/workers/subdomain`,{
    headers:{Authorization:"Bearer "+token},redirect:"error",signal:AbortSignal.timeout(15_000)});
  if(!reply.ok) throw new Error("Existing workers.dev subdomain could not be verified");
  const subdomain=await reply.json();
  if(subdomain.success!==true || !/^[a-z0-9-]+$/.test(subdomain.result?.subdomain ?? "")
    || new URL(origin).hostname!==`${config.name}.${subdomain.result.subdomain}.workers.dev`) throw new Error("Origin is not this account's existing relay subdomain");
  const directory=await mkdtemp(join(tmpdir(),"astra-free-deploy-"));
  try {
    const publication=await stageWalkthroughAssets(settings.public_walkthrough,directory,args[0]==="--deploy");
    if(publication) config.assets={directory:publication.directory,run_worker_first:false,not_found_handling:"none",html_handling:"none"};
    validateFreeConfig(config);
    await verifyStagedWalkthroughAssets(publication);
    // Absolute source paths keep an external, disposable config anchored to the
    // audited relay. No generated settings or account identity enter Git.
    config.main=join(root,"src/index.ts");
    const configPath=join(directory,"wrangler.json");
    await writeFile(configPath,JSON.stringify(config),{mode:0o600});
    const deploymentEnvironment={...cliEnvironment,CLOUDFLARE_API_TOKEN:token,CLOUDFLARE_ACCOUNT_ID:config.account_id,WRANGLER_SEND_METRICS:"false"};
    await run(process.execPath,[cli,"deploy","--config",configPath,"--dry-run"],{cwd:root,env:deploymentEnvironment,maxBuffer:1_048_576});
    if(args[0]==="--deploy") {
      // A plan change while bundling invalidates the preflight.
      await checkFreeAccount(config.account_id,billingToken,request);
      await verifyStagedWalkthroughAssets(publication);
      const deployed=await run(process.execPath,[cli,"deploy","--config",configPath],{cwd:root,env:deploymentEnvironment,maxBuffer:1_048_576});
      // Wrangler may show public variable values. Redact the portal challenge;
      // don't enable payload logs or automatic CI builds.
      const challenge=config.vars.OPENAI_APPS_CHALLENGE;
      output(challenge ? deployed.stdout.split(challenge).join("<configured-domain-challenge>") : deployed.stdout);
      if(publication) output(`Reviewed walkthrough uploaded: ${origin}${publication.publicPath}\n`);
    } else output(`Free-plan check passed (${evidence.evidence}). Ready for ${origin}; nothing was deployed.\n`);
  } finally {
    if(dirname(resolve(directory))!==resolve(tmpdir()) || !basename(directory).startsWith("astra-free-deploy-")) throw new Error("Temporary cleanup target failed containment validation");
    await rm(directory,{recursive:true,force:true});
  }
}
if(process.argv[1] && resolve(process.argv[1])===fileURLToPath(import.meta.url)) {
  deployFree(process.argv.slice(2)).catch(error=>{process.stderr.write((error.message?.startsWith("Command failed") ? "Wrangler validation/deployment failed; inspect locally without exposing credentials." : error.message)+"\n");process.exitCode=1;});
}
