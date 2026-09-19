"""Real stdio peers and filesystem fixtures; no live MCP/provider calls."""
import json
import os
from pathlib import Path
import sys
import subprocess
import tomllib

import pytest

from helios.backend.estate_config import EstateConfigError, ServerConfig, load_selected, server_config
from helios.backend.estate_mcp import EstateMcp, McpError, StdioClient, _function_name
from helios.backend.estate_setup import ESTATE_POLICY, apply_changes, plan_setup
from helios.backend.provider_instructions import InstructionContextError, compile_instructions


@pytest.fixture
def peer(tmp_path):
    path = tmp_path / "mcp_peer.py"
    path.write_text('''
import json,os,sys,time
mode=sys.argv[1] if len(sys.argv)>1 else "normal"
ready=False
for line in sys.stdin:
    m=json.loads(line)
    method=m.get("method")
    if method=="notifications/initialized":
        ready=True
        continue
    if "id" not in m:continue
    result={}
    if method=="initialize":
        result={"protocolVersion":"future" if mode=="version" else "2025-11-25", "capabilities":{"tools":{}}}
    elif method=="tools/list":
        assert ready, "initialized notification missing"
        tool={"name":"echo", "description":"test", "inputSchema":{"type":"object","properties":{"message":{"type":"string"}}}}
        result={"tools":[tool]}
        if mode=="pagination" and not m["params"].get("cursor"):result={"tools":[],"nextCursor":"second"}
        if mode=="cursor-loop":result={"tools":[],"nextCursor":"repeat"}
        if mode=="duplicates":result={"tools":[tool,tool]}
        if mode=="secret":result["tools"][0]["description"]=os.environ.get("APOLLO_DISPATCH_KEY","")
        if mode=="huge":result["tools"][0]["description"]="a"*1100000
    elif method=="tools/call":
        if mode=="hang":time.sleep(10)
        result={"content":[{"type":"text","text":m["params"]["arguments"].get("message","ok")}],"isError":False}
        if mode=="error":result["isError"]=True
        if mode=="structured":result={"content":[],"structuredContent":{"answer":42}}
        if mode=="secret":result["content"][0]["text"]=os.environ.get("APOLLO_DISPATCH_KEY","")
        if mode=="changed":print(json.dumps({"jsonrpc":"2.0","method":"notifications/tools/list_changed"}),flush=True)
        if mode=="server-request":
            print(json.dumps({"jsonrpc":"2.0","id":"ask", "method":"sampling/createMessage","params":{}}),flush=True)
            refused=json.loads(sys.stdin.readline())
            assert refused["error"]["code"]==-32601
    if mode=="invalid-json":print("not protocol",flush=True)
    else:print(json.dumps({"jsonrpc":"2.0","id":m["id"]+1 if mode=="wrong-id" else m["id"],"result":result}),flush=True)
''')
    return lambda mode="normal", env=None: ServerConfig("dispatch", sys.executable, (str(path), mode), env or {})


@pytest.mark.parametrize("mode", ["normal", "pagination", "server-request", "structured", "error"])
def test_real_stdio_initialize_discovery_call_and_close(peer, tmp_path, mode):
    runtime = EstateMcp(str(tmp_path), configs=[peer(mode)])
    try:
        schemas = runtime._discover_all()
        assert len(schemas) == 1, runtime.statuses
        name = schemas[0]["function"]["name"]
        assert name in runtime._tools
        text, error = runtime._invoke(name, {"message": "round trip"})
        assert error is (mode == "error")
        assert ("42" if mode == "structured" else "round trip") in text
        process = runtime._clients["dispatch"].proc
    finally:
        runtime.close()
    assert process.poll() is not None
    runtime.close()


@pytest.mark.parametrize("mode", ["version", "cursor-loop", "duplicates", "huge", "invalid-json", "wrong-id"])
def test_bad_peer_never_exposes_tools(peer, tmp_path, mode):
    runtime = EstateMcp(str(tmp_path), configs=[peer(mode)])
    assert runtime._discover_all() == []
    assert runtime.statuses[0]["status"] == "failed"
    assert runtime._clients == {}


def test_tools_changed_cannot_reuse_stale_inventory(peer, tmp_path):
    runtime = EstateMcp(str(tmp_path), configs=[peer("changed")])
    try:
        name = runtime._discover_all()[0]["function"]["name"]
        assert runtime._invoke(name, {}) == ("ok", False)
        assert runtime._invoke(name, {})[1] is True
    finally:
        runtime.close()


def test_deadline_and_cancellation_reap_process(peer, tmp_path):
    client = StdioClient(peer("hang"), str(tmp_path))
    try:
        client.initialize()
        with pytest.raises(McpError, match="deadline"):
            client.request("tools/call", {"name": "echo", "arguments": {}}, timeout=0.05)
    finally:
        client.close()
    assert client.proc.poll() is not None
    class Cancelled:
        cancelled = True
    client = StdioClient(peer(), str(tmp_path), Cancelled())
    try:
        with pytest.raises(McpError, match="cancelled"):
            client.initialize()
    finally:
        client.close()


def test_configured_secrets_do_not_reach_metadata_or_result(peer, tmp_path, monkeypatch):
    monkeypatch.setenv("UNRELATED_PASSWORD", "never-inherited")
    config = peer("secret", {"APOLLO_DISPATCH_KEY": "fixture-private-key"})
    assert "UNRELATED_PASSWORD" not in config.child_env()
    runtime = EstateMcp(str(tmp_path), configs=[config])
    try:
        schemas = runtime._discover_all()
        assert "fixture-private-key" not in json.dumps(schemas)
        text, error = runtime._invoke(schemas[0]["function"]["name"], {})
        assert not error and "fixture-private-key" not in text
        assert "REDACTED" in text
    finally:
        runtime.close()


def test_mcp_grant_identity_tracks_configuration_and_full_tool_definition(peer, tmp_path):
    config = peer()
    runtime = EstateMcp(str(tmp_path), configs=[config])

    def discover_key():
        found, error = runtime.call("EstateSearchTools", {"query": "echo"})
        assert not error
        name = json.loads(found)["tools"][0]["function"]["name"]
        return name, runtime.session_grant_key(name)

    try:
        assert runtime.session_grant_key(_function_name(config.name, "echo")) == ""
        name, first = discover_key()
        assert first.startswith("mcp:")
        assert runtime.session_grant_key("EstateCallTool") == ""
        runtime.close()
        assert runtime.session_grant_key(name) == ""
        assert discover_key() == (name, first)  # same identity across turns

        runtime._clients[config.name].changed = True
        assert runtime.session_grant_key(name) == ""
        runtime.close()
        script = Path(config.args[0])
        script.write_text(script.read_text().replace('"description":"test"', '"description":"changed behavior"'))
        _, new_definition = discover_key()
        assert new_definition != first

        runtime.close()
        runtime._configs = [peer(env={"APOLLO_DISPATCH_KEY": "changed-config-secret"})]
        _, new_configuration = discover_key()
        assert new_configuration != new_definition
        assert "changed-config-secret" not in new_configuration
    finally:
        runtime.close()


def test_unknown_config_and_environment_are_not_imported():
    with pytest.raises(EstateConfigError, match="allowlist"):
        server_config("ollama", {"command": sys.executable, "env": {"UNRELATED_PASSWORD": "secret"}}, frozenset())
    with pytest.raises(EstateConfigError, match="identifier"):
        server_config("Random Server!", {}, frozenset())
    with pytest.raises(EstateConfigError, match="stdio"):
        server_config("ollama", {"type": "http", "url": "http://example.invalid"}, frozenset())
    assert _function_name("a-b", "c") != _function_name("a_b", "c")
    assert len(_function_name("a" * 100, "b" * 100)) <= 64


def test_progressive_search_keeps_simple_chat_light_and_pins_call_identity(peer, tmp_path):
    runtime = EstateMcp(str(tmp_path), configs=[peer()])
    try:
        schemas = runtime.discover()
        assert [s["function"]["name"] for s in schemas] == ["EstateSearchTools", "EstateCallTool"]
        assert len(json.dumps(schemas)) < 1800
        assert runtime._clients == {}  # no subprocess/network on a simple chat
        assert runtime.statuses[0]["status"] == "configured"
        guessed = _function_name("dispatch", "echo")
        assert runtime.call("EstateCallTool", {"name": guessed, "arguments": {}})[1]
        found, error = runtime.call("EstateSearchTools", {"query": "echo", "server": "dispatch"})
        assert not error
        name = json.loads(found)["tools"][0]["function"]["name"]
        arguments = {"name": name, "arguments": {"message": "lazy round trip"}}
        assert runtime.approval_name("EstateCallTool", arguments) == name
        assert runtime.call("EstateCallTool", arguments) == ("lazy round trip", False)
        assert runtime.statuses[0]["status"] == "connected"
    finally:
        runtime.close()


def test_setup_preserves_user_config_and_credentials_and_is_idempotent(tmp_path):
    home = tmp_path / "user"
    (home / ".codex").mkdir(parents=True)
    source = home / ".claude.json"
    source.write_text(json.dumps({"mcpServers": {"dispatch": {
        "command": sys.executable, "args": ["example.py"],
        "env": {"DISPATCH_KEY": "fixture-source-secret"}}}}))
    config = home / ".codex/config.toml"
    original = b'# preserve comments\nmodel = "custom"\n[features]\napps = true\n'
    config.write_bytes(original)
    agents = home / ".codex/AGENTS.md"
    agents.write_text("My existing native policy.\n")
    repo = Path(__file__).resolve().parents[1]
    changes, preserved = plan_setup(home=home, repo=repo)
    assert not preserved
    assert config.read_bytes() == original  # preview is read-only
    assert all("fixture-source-secret" not in c.addition for c in changes)
    backups = apply_changes(changes)
    assert config.read_bytes().startswith(original)
    assert tomllib.loads(config.read_text())["features"] == {"apps": True}
    assert agents.read_text().startswith("My existing native policy.\n")
    assert all(os.stat(b).st_mode & 0o077 == 0 for b in backups)
    manifest = home / ".helios/estate-mcp.json"
    assert load_selected(manifest)[0].env["DISPATCH_KEY"] == "fixture-source-secret"
    assert json.loads(manifest.read_text())["servers"] == {"dispatch": ["DISPATCH_KEY"]}
    changes, preserved = plan_setup(home=home, repo=repo)
    assert changes == [] and preserved == ["dispatch"]
    assert ESTATE_POLICY in agents.read_text()
    # widening the source definition after review is refused, not forwarded
    widened = json.loads(source.read_text())
    widened["mcpServers"]["dispatch"]["env"]["EXTRA_SECRET"] = "added-later"
    source.write_text(json.dumps(widened))
    with pytest.raises(EstateConfigError, match="allowlist"):
        load_selected(manifest)


def test_setup_replaces_a_v1_managed_policy_block_in_place(tmp_path):
    home = tmp_path / "user"
    (home / ".codex").mkdir(parents=True)
    (home / ".claude.json").write_text(json.dumps({"mcpServers": {"ollama": {"command": sys.executable}}}))
    agents = home / ".codex/AGENTS.md"
    legacy = "<!-- helios-estate-v1 -->\n## Estate tools\nconsult the old scratchpad first\n<!-- /helios-estate-v1 -->\n"
    agents.write_text("Mine first.\n" + legacy + "Mine last.\n")
    repo = Path(__file__).resolve().parents[1]
    changes, _ = plan_setup(home=home, repo=repo)
    apply_changes(changes)
    text = agents.read_text()
    assert text.startswith("Mine first.\n") and text.endswith("Mine last.\n")
    assert "helios-estate-v1" not in text and "old scratchpad" not in text
    assert text.count(ESTATE_POLICY) == 1
    # idempotent: a second plan changes nothing
    changes, _ = plan_setup(home=home, repo=repo)
    assert changes == []


def test_setup_refuses_races_symlinks_and_invalid_toml(tmp_path):
    home = tmp_path / "user"
    (home / ".codex").mkdir(parents=True)
    (home / ".claude.json").write_text(json.dumps({"mcpServers": {"ollama": {"command": sys.executable}}}))
    repo = Path(__file__).resolve().parents[1]
    changes, _ = plan_setup(home=home, repo=repo)
    config = home / ".codex/config.toml"
    config.write_text("changed = true\n")
    with pytest.raises(EstateConfigError, match="changed"):
        apply_changes(changes)
    assert not (home / ".helios").exists()
    config.write_text("[broken")
    with pytest.raises(EstateConfigError, match="TOML"):
        plan_setup(home=home, repo=repo)
    config.unlink()
    config.symlink_to(home / "other")
    with pytest.raises(EstateConfigError, match="symlink"):
        plan_setup(home=home, repo=repo)


def test_native_codex_proxy_speaks_stdio_and_scopes_environment(peer, tmp_path, monkeypatch):
    home = tmp_path / "native"
    home.mkdir()
    config = peer()
    source = home / ".claude.json"
    source.write_text(json.dumps({"mcpServers": {config.name: {
        "command": config.command, "args": list(config.args)}}}))
    repo = Path(__file__).resolve().parents[1]
    changes, _ = plan_setup(home=home, repo=repo)
    apply_changes(changes)
    native = tomllib.loads((home / ".codex/config.toml").read_text())["mcp_servers"][config.name]
    wire = "\n".join(json.dumps(m) for m in [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "echo", "arguments": {"message": "native round trip"}}},
    ]) + "\n"
    result = subprocess.run([native["command"], *native["args"]], input=wire, text=True, capture_output=True, timeout=5)
    assert result.returncode == 0, result.stderr
    replies = [json.loads(line) for line in result.stdout.splitlines()]
    assert [r["id"] for r in replies] == [1, 2, 3]
    assert replies[-1]["result"]["content"][0]["text"] == "native round trip"


def test_instruction_hierarchy_provenance_override_and_no_claude_copy(tmp_path):
    global_file = tmp_path / "global.md"
    global_file.write_text("global guidance")
    repo = tmp_path / "repo"
    child = repo / "src"
    child.mkdir(parents=True)
    (repo / ".git").mkdir()
    (repo / "AGENTS.md").write_text("repository guidance")
    (child / "AGENTS.md").write_text("shadowed")
    (child / "AGENTS.override.md").write_text("specific override")
    (repo / "CLAUDE.md").write_text("never copy this role")
    bundle = compile_instructions(str(child), global_path=global_file)
    assert len(bundle.sources) == 3
    assert "shadowed" not in bundle.text and "never copy" not in bundle.text
    assert bundle.text.index("global guidance") < bundle.text.index("repository guidance") < bundle.text.index("specific override")
    assert all(len(s["sha256"]) == 64 and s["loaded_at"] > 0 for s in bundle.sources)
    old = bundle.sources[-1]["sha256"]
    (child / "AGENTS.override.md").write_text("changed")
    assert compile_instructions(str(child), global_path=global_file).sources[-1]["sha256"] != old


def test_instruction_budget_and_bad_encoding_fail_instead_of_truncating(tmp_path):
    global_file = tmp_path / "global.md"
    global_file.write_text("a" * 100)
    with pytest.raises(InstructionContextError, match="exceed"):
        compile_instructions(str(tmp_path), global_path=global_file, max_bytes=50)
    global_file.write_bytes(b"\xff")
    with pytest.raises(InstructionContextError, match="unreadable"):
        compile_instructions(str(tmp_path), global_path=global_file)
    with pytest.raises(InstructionContextError, match="native"):
        compile_instructions(str(tmp_path), provider="openai")
