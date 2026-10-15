import {open,lstat,mkdir,writeFile,readdir} from "node:fs/promises";
import {isAbsolute,join,resolve} from "node:path";
import {createHash} from "node:crypto";

const digest=data=>createHash("sha256").update(data).digest("hex");
const cspHash=value=>"'sha256-"+createHash("sha256").update(value).digest("base64")+"'";
const htmlEscape=value=>String(value).replace(/[&<>"']/g,character=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"})[character]);

// This function is serialized into the generated static page, with public pins only.
function walkthroughPlayer(pin) {
  const player=document.getElementById("player"),status=document.getElementById("status"),
    progress=document.getElementById("progress"),retry=document.getElementById("retry"),main=document.querySelector("main");
  let loading=false,objectURL=null,controller=null;
  async function load() {
    if(loading) return;
    loading=true;retry.hidden=true;retry.disabled=true;player.hidden=true;progress.hidden=false;progress.value=0;
    main.setAttribute("aria-busy","true");
    if(objectURL) { player.removeAttribute("src");URL.revokeObjectURL(objectURL);objectURL=null; }
    status.textContent="Loading the complete video before playback (0%).";
    controller=new AbortController();
    const timeout=setTimeout(()=>controller.abort(),120000);
    let reader;
    try {
      if(!crypto.subtle) throw new Error("Verification unavailable");
      const response=await fetch(pin.url,{credentials:"omit",mode:"same-origin",redirect:"error",signal:controller.signal});
      if(!response.ok || !response.body) throw new Error("Video unavailable");
      const bytes=new Uint8Array(pin.bytes);
      reader=response.body.getReader();
      let offset=0,lastPercent=0;
      while(true) {
        const {done,value}=await reader.read();
        if(done) break;
        if(value.byteLength>bytes.length-offset) throw new Error("Video exceeds reviewed size");
        bytes.set(value,offset);offset+=value.byteLength;
        const percent=Math.floor(offset/bytes.length*100);progress.value=percent;
        if(percent>=lastPercent+5) { status.textContent="Loading the complete video before playback ("+percent+"%).";lastPercent=percent; }
      }
      if(offset!==bytes.length) throw new Error("Incomplete video");
      status.textContent="Preparing the video for playback.";
      const actual=Array.from(new Uint8Array(await crypto.subtle.digest("SHA-256",bytes)),byte=>byte.toString(16).padStart(2,"0")).join("");
      if(actual!==pin.sha256 || controller.signal.aborted) throw new Error("Video verification failed");
      objectURL=URL.createObjectURL(new Blob([bytes],{type:"video/mp4"}));
      player.src=objectURL;player.hidden=false;player.load();progress.hidden=true;
      status.textContent="Ready. Press Play to watch. You can seek anywhere in the video.";
    } catch {
      controller.abort();progress.hidden=true;retry.hidden=false;
      status.textContent="The video could not be loaded and verified. Retry, or use Download MP4 below.";
    } finally {
      clearTimeout(timeout);
      if(reader) { try { await reader.cancel(); } catch {} reader.releaseLock(); }
      loading=false;retry.disabled=false;main.setAttribute("aria-busy","false");
    }
  }
  retry.addEventListener("click",load);
  player.addEventListener("error",()=>{
    if(!loading && objectURL) {
      retry.hidden=false;
      status.textContent="This browser could not play the video. Retry, or use Download MP4 below.";
    }
  });
  addEventListener("pagehide",event=>{
    if(event.persisted) return;
    if(controller) controller.abort();
    if(objectURL) URL.revokeObjectURL(objectURL);
  });
  return load();
}
function playerPage(stem,source,hasCaptions,version) {
  const mp4="./"+stem+".mp4",vtt="./"+stem+".vtt";
  const script="("+walkthroughPlayer.toString().replace(/\r\n?/g,"\n")+")("+JSON.stringify({url:mp4,bytes:source.bytes,sha256:source.sha256})+");";
  const style="html{color-scheme:light dark;font:100%/1.5 system-ui,sans-serif}body{margin:0}main{max-width:72rem;margin:auto;padding:clamp(1rem,4vw,2rem)}h1{font-size:clamp(1.4rem,4vw,2rem)}video{display:block;width:100%;max-height:80vh;background:#000}progress{width:100%}button{font:inherit;min-height:44px;padding:.5rem 1rem}a{display:inline-block;padding:.5rem .25rem}a:focus-visible,button:focus-visible{outline:3px solid Highlight;outline-offset:3px}[hidden]{display:none!important}";
  const track=hasCaptions ? '<track kind="captions" srclang="en" label="English" src="'+htmlEscape(vtt)+'">' : "";
  const captionsLink=hasCaptions ? ' <a href="'+htmlEscape(vtt)+'" download>Download captions</a>' : "";
  const html='<!doctype html>\n<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Delegate to Antigravity walkthrough</title><style>'+style+'</style></head><body><main aria-busy="true"><h1>Delegate to Antigravity walkthrough</h1><p>Recording version '+htmlEscape(version)+'. The setup and verification scope are stated in the video.</p><p>The full '+(source.bytes/1000000).toFixed(1)+' MB video loads before playback so you can seek freely.'+(hasCaptions?' English captions are available.':'')+'</p><p id="status" role="status">Loading the complete video before playback (0%).</p><progress id="progress" max="100" value="0" aria-label="Video download progress"></progress><button id="retry" type="button" hidden>Retry loading video</button><video id="player" controls playsinline preload="auto" hidden aria-label="Delegate to Antigravity product walkthrough">'+track+'</video><p><a href="'+htmlEscape(mp4)+'" download>Download MP4</a>'+captionsLink+'</p><noscript><p>Use Download MP4 to watch the video in your preferred player.</p></noscript></main><script>'+script+'</script></body></html>\n';
  const csp="default-src 'none'; script-src "+cspHash(script)+"; style-src "+cspHash(style)+"; connect-src 'self'; media-src 'self' blob:; base-uri 'none'; object-src 'none'; form-action 'none'; frame-ancestors 'none'";
  return {html:Buffer.from(html),csp};
}
function keys(value,allowed) {
  if(!value || typeof value!=="object" || Array.isArray(value)
    || Object.keys(value).some(key=>!allowed.includes(key))) throw new Error("Unexpected walkthrough publication field");
}
function sourceInfo(source,extension,maximum) {
  keys(source,["path","sha256","bytes"]);
  if(typeof source.path!=="string" || !isAbsolute(source.path) || !source.path.toLowerCase().endsWith(extension)
    || !/^[a-f0-9]{64}$/.test(source.sha256 ?? "") || !Number.isSafeInteger(source.bytes)
    || source.bytes<=0 || source.bytes>maximum) throw new Error("Walkthrough requires an absolute source, exact hash and bounded size");
}
async function boundedFile(path,bytes) {
  const stat=await lstat(path);
  if(!stat.isFile() || stat.isSymbolicLink() || stat.size!==bytes) throw new Error("Walkthrough file changed or is not a bounded regular file");
  const file=await open(path,"r");
  try {
    const current=await file.stat();
    if(!current.isFile() || current.size!==bytes) throw new Error("Walkthrough file changed before reading");
    // The extra byte detects growth without an unbounded readFile allocation.
    const buffer=Buffer.alloc(bytes+1);
    let offset=0;
    while(offset<buffer.length) {
      const {bytesRead}=await file.read(buffer,offset,buffer.length-offset,offset);
      if(!bytesRead) break;
      offset+=bytesRead;
    }
    if(offset!==bytes) throw new Error("Walkthrough file size changed while reading");
    return buffer.subarray(0,bytes);
  } finally { await file.close(); }
}
async function pinned(source,extension,maximum) {
  sourceInfo(source,extension,maximum);
  const data=await boundedFile(source.path,source.bytes);
  if(digest(data)!==source.sha256) throw new Error("Walkthrough source does not match its reviewed bytes");
  return data;
}
export function validateWalkthroughPublication(settings,deploying) {
  if(settings===undefined) return;
  keys(settings,["publication_approved","version","mp4","captions"]);
  if(settings.version!==undefined && (typeof settings.version!=="string"
    || !/^(?:0|[1-9]\d{0,2})\.(?:0|[1-9]\d{0,2})\.(?:0|[1-9]\d{0,2})$/.test(settings.version)))
    throw new Error("Walkthrough version must be a bounded numeric semantic version");
  if(typeof settings.publication_approved!=="boolean" || (deploying && settings.publication_approved!==true))
    throw new Error("Public walkthrough upload requires approval of the exact reviewed files");
  sourceInfo(settings.mp4,".mp4",25*1024*1024);
  if(settings.captions!==undefined) sourceInfo(settings.captions,".vtt",128*1024);
}
export async function stageWalkthroughAssets(settings,temporaryDirectory,deploying) {
  validateWalkthroughPublication(settings,deploying);
  if(settings===undefined) return null;
  if(!isAbsolute(temporaryDirectory)) throw new Error("Walkthrough staging requires an absolute temporary directory");
  const parent=await lstat(temporaryDirectory);
  if(!parent.isDirectory() || parent.isSymbolicLink()) throw new Error("Walkthrough staging requires a regular temporary directory");
  const video=await pinned(settings.mp4,".mp4",25*1024*1024);
  if(video.length<12 || video.subarray(4,8).toString("ascii")!=="ftyp") throw new Error("Expected an MP4 container");
  const captions=settings.captions===undefined ? null : await pinned(settings.captions,".vtt",128*1024);
  if(captions && !/^WEBVTT(?:[ \t]|\r?\n|$)/.test(new TextDecoder("utf-8",{fatal:true}).decode(captions).replace(/^\uFEFF/,"")))
    throw new Error("Expected UTF-8 WebVTT captions");
  const directory=join(temporaryDirectory,"public-walkthrough"), folder=join(directory,"review");
  await mkdir(directory,{mode:0o700});
  await mkdir(folder,{mode:0o700});
  const version=settings.version ?? "0.12.1",stem="astra-antigravity-"+version+"-"+settings.mp4.sha256;
  const player=playerPage(stem,settings.mp4,Boolean(captions),version);
  const files=[["review/"+stem+".mp4",video]];
  if(captions) files.push(["review/"+stem+".vtt",captions]);
  files.push(["review/"+stem+".html",player.html]);
  files.push(["_headers",Buffer.from("/review/*\n  X-Content-Type-Options: nosniff\n  Referrer-Policy: no-referrer\n  X-Robots-Tag: noindex, nofollow, noarchive\n/review/"+stem+".html\n  Content-Security-Policy: "+player.csp+"\n")]);
  const manifest=[];
  for(const [relative,data] of files) {
    await writeFile(join(directory,relative),data,{mode:0o600,flag:"wx"});
    manifest.push({relative,bytes:data.length,sha256:digest(data)});
  }
  return {directory:resolve(directory),publicPath:"/review/"+stem+".html",mediaPath:"/review/"+stem+".mp4",manifest};
}
export async function verifyStagedWalkthroughAssets(publication) {
  if(!publication) return;
  const root=await lstat(publication.directory);
  if(!root.isDirectory() || root.isSymbolicLink()) throw new Error("Public walkthrough staging root must be a regular directory");
  const found=[];
  async function walk(directory,prefix="") {
    for(const item of await readdir(directory,{withFileTypes:true})) {
      if(item.isSymbolicLink()) throw new Error("Public walkthrough staging contains a link");
      const relative=prefix+item.name;
      if(item.isDirectory()) {
        if(relative!=="review") throw new Error("Unexpected public walkthrough directory");
        await walk(join(directory,item.name),relative+"/");
      } else if(item.isFile()) found.push(relative);
      else throw new Error("Unexpected public walkthrough file type");
    }
  }
  await walk(publication.directory);
  if(JSON.stringify(found.sort())!==JSON.stringify(publication.manifest.map(item=>item.relative).sort()))
    throw new Error("Public walkthrough staging contains unexpected files");
  for(const item of publication.manifest) {
    const data=await boundedFile(join(publication.directory,item.relative),item.bytes);
    if(data.length!==item.bytes || digest(data)!==item.sha256) throw new Error("Public walkthrough staging changed after review");
  }
}
