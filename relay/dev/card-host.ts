import { cardFixtures } from "./fixtures";
import { controlFixtures } from "./control-fixtures";

export function cardHost(scenario: string, theme: string, control = false): string {
  const fixture = control ? controlFixtures[scenario] : cardFixtures[scenario] ?? cardFixtures.running;
  const controls = JSON.stringify(controlFixtures.computers).replace(/</g, "\\u003c");
  const initial = JSON.stringify(fixture).replace(/</g, "\\u003c");
  return `<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Astra task card · Design gallery</title>
    <style>html{color-scheme:light dark}body{margin:0;padding:24px;font:16px/1.5 system-ui,sans-serif;background:light-dark(#f7f7f7,#171717);color:light-dark(#171717,#f5f5f5)}main{max-width:680px;margin:auto}h1{font-size:20px}iframe{width:100%;height:480px;display:block;border:1px solid light-dark(#dedede,#484848);border-radius:16px;background:light-dark(#fff,#212121)}nav{display:flex;flex-wrap:wrap;gap:12px;margin-block:20px}button,a{font:inherit;min-height:44px}button{padding:8px 14px}a{display:inline-flex;align-items:center;color:inherit}#gallery-status{font-size:14px}html[data-theme="light"]{color-scheme:light}html[data-theme="dark"]{color-scheme:dark}@media(max-width:390px){body{padding:12px}}</style></head>
    <body><main><h1>ChatGPT task card</h1><p>Synthetic fixture: ${scenario}. All actions stay in this local preview.</p>
    <iframe id="card" title="Delegated task preview" src="/gallery/card/frame"></iframe>
    <nav aria-label="Preview controls"><button id="advance" type="button">Complete sample task</button><button id="theme" type="button">Toggle theme</button><a href="/__gallery">All UI states</a></nav><p id="gallery-status" role="status">Connecting preview…</p></main>
    <script>
      const frame=document.getElementById('card');
      let state=${initial}, controls=${controls}, control=${control}, theme=${JSON.stringify(theme === "dark" ? "dark" : "light")}, connected=false;
      let pending=[], nextRevision=2;
      window.galleryCalls=[];
      const nativeDownloads=new URLSearchParams(location.search).get('nativeDownloads')!=='false';
      window.galleryDownloadPending=false; window.galleryDownloadDenied=false; window.galleryConfirmFailure=false; window.galleryDelayPrepare=false;
      const variables=()=>theme==='dark'?{'--color-background-primary':'#212121','--color-background-secondary':'#2c2c2c','--color-background-inverse':'#f5f5f5','--color-text-primary':'#f5f5f5','--color-text-secondary':'#b7b7b7','--color-text-inverse':'#171717','--color-border-primary':'#484848','--color-ring-primary':'#8ab4ff','--font-text-md-size':'16px','--font-text-sm-size':'14px'}:{'--color-background-primary':'#ffffff','--color-background-secondary':'#f5f5f5','--color-background-inverse':'#171717','--color-text-primary':'#171717','--color-text-secondary':'#616161','--color-text-inverse':'#ffffff','--color-border-primary':'#dedede','--color-ring-primary':'#2563eb','--font-text-md-size':'16px','--font-text-sm-size':'14px'};
      const post=value=>frame.contentWindow.postMessage({jsonrpc:'2.0',...value},location.origin);
      const result=()=>({content:[],_meta:control?{astra_control:state}:{astra_ui:state}});
      const reply=(id,value)=>post({id,result:value});
      window.galleryFreshControl=()=>post({method:'ui/notifications/tool-result',params:{content:[],_meta:{astra_control:controls}}});
      const flush=()=>{for(const entry of pending){clearTimeout(entry.timer);reply(entry.id,result())}pending=[]};
      function publish(){state={...state,revision:'gallery-'+nextRevision++};post({method:'ui/notifications/tool-result',params:result()});flush()}
      document.documentElement.dataset.theme=theme;
      window.addEventListener('message',event=>{
        if(event.source!==frame.contentWindow||event.origin!==location.origin)return;
        const data=event.data;if(!data||data.jsonrpc!=='2.0')return;
        if(data.method==='ui/initialize'){
          reply(data.id,{protocolVersion:data.params.protocolVersion,hostInfo:{name:'Astra local design gallery',version:'1.0.0'},hostCapabilities:{openLinks:{},...(nativeDownloads?{downloadFile:{}}:{}),serverTools:{},message:{text:{}}},hostContext:{theme,locale:'en-US',timeZone:'UTC',displayMode:'inline',styles:{variables:variables()}}});
        }else if(data.method==='ui/notifications/initialized'){
          connected=true;post({method:'ui/notifications/tool-result',params:result()});document.getElementById('gallery-status').textContent='Preview connected';
        }else if(data.method==='ui/notifications/size-changed'){
          if(Number.isFinite(data.params?.height))frame.style.height=Math.max(80,Math.ceil(data.params.height))+'px';
        }else if(data.id!==undefined){
          window.galleryCalls.push({method:data.method,params:data.params});
          if(data.method==='tools/call'&&data.params.name==='watch_delegation_ui'){
            if(data.params.arguments?.wait_ms){const entry={id:data.id,timer:0};entry.timer=setTimeout(()=>{pending=pending.filter(item=>item!==entry);reply(entry.id,result())},20000);pending.push(entry)}else reply(data.id,result());
          }else if(data.method==='tools/call'&&data.params.name==='watch_computers_ui'){
            reply(data.id,{content:[],_meta:{astra_control:control?state:controls}});
          }else if(data.method==='tools/call'&&data.params.name==='show_delegation'){
            reply(data.id,{content:[],_meta:{astra_ui:${JSON.stringify(cardFixtures.running).replace(/</g, "\\u003c")}}});
          }else if(data.method==='tools/call'&&data.params.name==='approve_pairing_ui'){
            reply(data.id,{content:[],_meta:{astra_operation:{operation_id:'approved-computer',computer_id:'desktop',action:'approve_pairing',state:'completed',summary:'Computer approved for setup and management until revoked. No workspace or task was approved.',updated_at:new Date().toISOString()}}});
          }else if(data.method==='tools/call'&&data.params.name==='prepare_operation'){
            const args=data.params.arguments;window.galleryOperation={operation_id:args.request_id,action:args.action,computer_id:args.computer_id,state:'prepared',summary:args.action==='delete_account'?'Delete relay account records and revoke computer access. Local files and provider accounts remain.':args.action==='configure_workspace'?'Approve folder '+args.parameters.path+' as workspace '+args.parameters.alias+' with '+args.parameters.isolation+' isolation. This allows delegated commands and file changes in that folder.':args.action==='rename'?'Rename this computer to '+args.parameters.label+'.':'Review '+args.action.replaceAll('_',' ')+' for this computer. Offline changes stay pending until the computer confirms.',confirmation_token:'a'.repeat(64),expires_at:new Date(Date.now()+300000).toISOString(),updated_at:new Date().toISOString()};const operation=window.galleryOperation;const deliver=()=>reply(data.id,{content:[],_meta:{astra_operation:operation}});if(window.galleryDelayPrepare)window.galleryDeliverPrepare=deliver;else deliver();
          }else if(data.method==='tools/call'&&data.params.name==='confirm_operation'){
            if(window.galleryConfirmFailure){post({id:data.id,error:{code:-32603,message:'Synthetic lost response'}});return}
            const receipt={...window.galleryOperation,confirmation_token:undefined,state:window.galleryOperation.computer_id==='laptop'?'pending':'completed',summary:window.galleryOperation.computer_id==='laptop'?'Change queued. Waiting for Research laptop to reconnect and confirm.':'The reviewed change was confirmed.'};window.galleryOperation=receipt;reply(data.id,{content:[],_meta:{astra_operation:receipt}});
          }else if(data.method==='tools/call'&&data.params.name==='inspect_operation'){
            reply(data.id,{content:[],_meta:{astra_operation:window.galleryOperation}});
          }else if(data.method==='tools/call'&&data.params.name==='export_account_ui'){
            reply(data.id,{content:[],_meta:{astra_export:{name:'account-records.json',mime_type:'application/json',encoding:'base64',content:btoa('{"synthetic":true}')}}});
          }else if(data.method==='tools/call'&&data.params.name==='prepare_artifact_download_ui'){
            if(window.galleryDownloadPending){window.galleryDownloadPending=false;reply(data.id,{content:[],_meta:{status:'transfer_requested'}})}
            else{const artifact=(state.artifacts||[]).find(item=>item.artifact_id===data.params.arguments.artifact_id);reply(data.id,{content:[],_meta:{astra_download:{url:'https://relay.example.test/artifact-download/synthetic.'+data.params.arguments.artifact_id,name:artifact?.name||'answer.txt',mime_type:artifact?.mime_type||'application/octet-stream',bytes:artifact?.bytes||2,sha256:artifact?.sha256||'73475cb40a568e8da8a045ced110137e159f890ac4da883b6b17dc651b3a8049',expires_at:new Date(Date.now()+60000).toISOString()}}})}
          }else if(data.method==='tools/call'&&data.params.name==='read_artifact_ui'){
            reply(data.id,{content:[],_meta:{astra_artifact:{id:'answer',bytes:2,sha256:'73475cb40a568e8da8a045ced110137e159f890ac4da883b6b17dc651b3a8049',encoding:'base64',content:'NDI='}}});
          }else if(data.method==='tools/call'&&data.params.name==='cancel_delegation'){
            state={...state,status:'cancelling'};reply(data.id,{content:[]});publish();
          }else if(data.method==='ui/download-file')reply(data.id,window.galleryDownloadDenied?{isError:true}:{});
          else if(['ui/message','ui/open-link','ui/update-model-context'].includes(data.method))reply(data.id,{});
          else post({id:data.id,error:{code:-32601,message:'Unsupported preview method'}});
        }
      });
      document.getElementById('advance').onclick=()=>{state={...state,status:'completed',terminal:true,decision:undefined,outcome:'The sample task finished. Review the saved result with ChatGPT.',worker_checks:['Synthetic fixture check passed.']};if(connected)publish()};
      document.getElementById('theme').onclick=()=>{theme=theme==='light'?'dark':'light';document.documentElement.dataset.theme=theme;if(connected)post({method:'ui/notifications/host-context-changed',params:{theme,styles:{variables:variables()}}})};
    </script></body></html>`;
}
