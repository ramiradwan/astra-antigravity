"""Build the HTTPS Agent Plugins package for an actual deployed relay URL.

No legacy/stdio compatibility manifest or publisher review metadata is emitted.
Endpoint eligibility and public release approval are separate from packaging.
"""
import argparse
import hashlib
import ipaddress
import json
from pathlib import Path
from urllib.parse import urlsplit
import zipfile
import urllib.request

from build_release import read_package, ReleaseError, ROOT

PLUGIN = ROOT / "plugins/astra-antigravity-remote"
DEFAULT_SCOPES = ("workspaces:read", "tasks:read", "tasks:write", "computers:manage", "account:delete")


def mcp_configuration(url):
    # OpenAI's documented auth extension requests initial, user-selectable
    # scopes. Do not force them on every request with baseScopes or embed any
    # client credentials; registration and endpoints use OAuth discovery.
    return {"$schema": "https://agent-plugins.org/schemas/1.0.0/mcp.schema.json",
            "mcpServers": {"astra_relay": {"type": "streamable-http", "url": url,
                "extensions": {"com.openai": {"auth": {
                    "type": "oauth", "defaultScopes": list(DEFAULT_SCOPES)}}}}}}


def configure_mcp(data, url):
    try:
        config = json.loads(data)
        original_url = config["mcpServers"]["astra_relay"]["url"]
    except (ValueError, KeyError, TypeError):
        raise ReleaseError("Remote MCP configuration is invalid") from None
    if not isinstance(original_url, str) or config != mcp_configuration(original_url):
        raise ReleaseError("Remote MCP configuration must use the documented OAuth defaults without credentials or overrides")
    endpoint(original_url)
    config["mcpServers"]["astra_relay"]["url"] = url
    return (json.dumps(config, indent=2) + "\n").encode()


def endpoint(value):
    parsed = urlsplit(value)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
        or parsed.path != "/mcp" or parsed.query or parsed.fragment or parsed.port not in (None,443)
        or parsed.hostname == "localhost" or parsed.hostname.endswith((".localhost", ".example", ".invalid", ".test"))
        or "YOUR" in value or "${" in value):
        raise ReleaseError("An actual public HTTPS /mcp endpoint is required")
    try:
        address=ipaddress.ip_address(parsed.hostname)
    except ValueError:
        address=None
    if address is not None and not address.is_global:
        raise ReleaseError("A public endpoint is required")
    return value


def verify_endpoint(url, expected_version):
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            raise ReleaseError("Endpoint verification refuses redirects")
    origin = url.rsplit("/mcp",1)[0]
    opener=urllib.request.build_opener(NoRedirect)
    data=[]
    try:
        for path in ("/health", "/.well-known/oauth-protected-resource/mcp"):
            request=urllib.request.Request(origin+path,headers={"Accept":"application/json",
                "User-Agent":"Astra-Antigravity-Release/"+expected_version})
            with opener.open(request,timeout=10) as response:
                body=response.read(32_769)
                if len(body)>32_768:
                    raise ReleaseError("Endpoint metadata exceeded its limit")
                data.append(json.loads(body))
    except (OSError, ValueError):
        raise ReleaseError("The live relay and OAuth discovery must be verified before packaging") from None
    if (any(not isinstance(item,dict) for item in data)
        or data[0].get("version")!=expected_version or data[0].get("cost_policy")!="free-only"
        or data[0].get("deployment_mode")!="verified-free" or data[0].get("authentication_configured") is not True
        or data[1].get("resource")!=url or data[1].get("authorization_servers")!=[origin]):
        raise ReleaseError("Endpoint must be the deployed free-only relay with sign-in configured")


def build(output, url):
    url = endpoint(url)
    entries = json.loads((ROOT / "remote-release-files.json").read_text(encoding="utf-8"))
    files = read_package(PLUGIN, entries)
    manifest = json.loads(files["plugin.json"])
    if manifest.get("$schema") != "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json" or manifest["extensions"]["com.openai"].get("requires_local_executor") is not False:
        raise ReleaseError("Remote Agent Plugins manifest required")
    if manifest.get("license") != "MIT" or b"MIT License" not in files["LICENSE"]:
        raise ReleaseError("Remote license mismatch")
    files["mcp.json"] = configure_mcp(files["mcp.json"], url)
    verify_endpoint(url, manifest["version"])
    output = Path(output)
    output.mkdir(parents=True,exist_ok=True)
    archive = output / ("astra-antigravity-remote-" + manifest["version"] + ".zip")
    with zipfile.ZipFile(archive,"w",compression=zipfile.ZIP_STORED) as stream:
        for name,data in sorted(files.items()):
            info = zipfile.ZipInfo(name,date_time=(2020,1,1,0,0,0))
            info.create_system=3
            info.external_attr=0o100644 << 16
            stream.writestr(info,data)
    return {"archive":str(archive),"sha256":hashlib.sha256(archive.read_bytes()).hexdigest(),
        "file_count":len(files),"endpoint":url,"endpoint_verification":"live health and OAuth resource discovery passed"}


if __name__ == "__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--endpoint",required=True)
    parser.add_argument("--output",type=Path,default=ROOT / "dist")
    args=parser.parse_args()
    print(json.dumps(build(args.output,args.endpoint),indent=2))
