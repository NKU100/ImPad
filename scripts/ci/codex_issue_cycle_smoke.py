"""Real Codex fail/comment/fail/comment/pass integration probe."""
import json
import os
import subprocess
import sys
from pathlib import Path

stage = int(sys.argv[1])
root = Path(os.environ["RUNNER_TEMP"]) / "issue-cycle"
root.mkdir(exist_ok=True)
workspace = root / "task"
workspace.mkdir(exist_ok=True)
home = root / "codex"
home.mkdir(exist_ok=True)
state_path = root / "state.json"
repo = os.environ["GITHUB_REPOSITORY"]

def api(path, payload=None, owner=False):
    env = dict(os.environ)
    env["GH_TOKEN"] = os.environ["OWNER_TOKEN" if owner else "GH_TOKEN"]
    cmd = ["gh", "api", path]
    if payload is not None:
        cmd += ["--method", "POST", "--input", "-"]
    result = subprocess.run(cmd, input=json.dumps(payload) if payload else None,
                            capture_output=True, text=True, env=env, check=True)
    return json.loads(result.stdout)

if stage == 1:
    state = {"results": [], "comments": [], "threadId": None}
else:
    state = json.loads(state_path.read_text())
    text = ("Test owner input 1: Change selection to the earliest eligible comment, "
            "not the latest; retain owner and cursor filtering." if stage == 2 else
            "Test owner input 2: Ignore comments whose body is empty or whitespace. "
            "Then select the earliest remaining eligible owner comment.")
    import time
    current = None
    for _ in range(60):
        comments = api(f"repos/{repo}/issues/{state['issue']}/comments")
        current = next((c for c in comments if c["user"]["login"] == "NKU100"
                        and c["body"] == text and c["id"] not in state["comments"]), None)
        if current:
            break
        time.sleep(3)
    if current is None:
        raise RuntimeError("Waiting for authorized test owner comment timed out")
    state["comments"].append(current["id"])

prompt = ("Implement comment_selector.py with select_comment(comments, cursor, owner). "
          "Comments have integer id, body, and user.login. Return the latest comment "
          "from owner with id greater than cursor, or None. This staged integration "
          "test will add requirements via issue comments later. Implement only these "
          "current requirements. Do not access parent directories or authentication files. "
          "Do not commit. Write actual Python code." if stage == 1 else current["body"] + "\nPrevious independent verifier errors: " + json.dumps(state["results"][-1]["errors"]) + "\nThe inputs are Python dictionaries, not objects. Fix all reported failures.")
home.joinpath("auth.json").write_text(os.environ["CODEX_AUTH_JSON"])
home.joinpath("auth.json").chmod(0o600)
env = {k: os.environ[k] for k in ("PATH", "LANG", "SSL_CERT_FILE") if k in os.environ}
env.update(CODEX_HOME=str(home), HOME=str(root))
cmd = ["codex", "exec"]
if stage > 1:
    cmd += ["resume", state["threadId"], "--dangerously-bypass-approvals-and-sandbox"]
else:
    cmd += ["--dangerously-bypass-approvals-and-sandbox"]
cmd += ["--json", "--skip-git-repo-check", "-m", "gpt-6-luna",
        "-c", 'model_reasoning_effort="low"', "-c", "features.multi_agent=false", prompt]
try:
    result = subprocess.run(cmd, cwd=workspace, env=env, capture_output=True,
                            text=True, timeout=300, check=True)
finally:
    # Authentication is never included in the checkpoint/artifact.
    auth = home / "auth.json"
    if auth.exists():
        # Preserve refreshed credentials only through the repository secret API.
        try:
            subprocess.run(["gh", "secret", "set", "CODEX_AUTH_JSON", "--repo", repo],
                           input=auth.read_text(), text=True, check=True,
                           env={**os.environ, "GH_TOKEN": os.environ["OWNER_TOKEN"]})
        finally:
            auth.unlink()
events = [json.loads(line) for line in result.stdout.splitlines() if line.startswith("{")]
(root / f"turn-{stage}.json").write_text(json.dumps(events, indent=2))
thread = next(e["thread_id"] for e in events if e.get("type") == "thread.started")
if state["threadId"]:
    assert thread == state["threadId"], "Native thread identity changed"
state["threadId"] = thread
usage = next(e["usage"] for e in events if e.get("type") == "turn.completed")
assert usage["input_tokens"] > 0 and usage["output_tokens"] > 0
# Independent verifier runs in another Python process, outside the model prompt.
verifier = """import importlib.util, json, sys
spec=importlib.util.spec_from_file_location('selector',sys.argv[1])
m=importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
c=lambda i,b='input',u='NKU100': {'id':i,'body':b,'user':{'login':u}}
cases=[([c(2),c(3,u='outsider')],1,2),([c(2),c(4)],1,2),([c(2,'  '),c(4)],1,4),([c(1)],1,None)]
errors=[]
for comments,cursor,expected in cases:
 try:
  got=m.select_comment(comments,cursor,'NKU100'); actual=got['id'] if got else None
  if actual!=expected: errors.append({'expected':expected,'actual':actual})
 except Exception as e: errors.append({'error':str(e)})
print(json.dumps(errors))
"""
check = subprocess.run([sys.executable,"-c",verifier,str(workspace/"comment_selector.py")],
                       capture_output=True,text=True,check=True)
errors = json.loads(check.stdout)
expected = stage == 3
assert (not errors) == expected, f"Unexpected stage {stage} verdict: {errors}"
entry = {"stage":stage,"passed":not errors,"errors":errors,"usage":usage,"threadId":thread}
state["results"].append(entry)
if stage == 1:
    issue = api(f"repos/{repo}/issues", {"title":"[CI test] Real Codex failure/comment/retry cycle",
        "body":"Integration test only; staged requirements deliberately exercise failure → owner comment → failure → owner comment → success. Real Codex writes a comment selector. This is not WeCom adaptation.\nRun: "+os.environ["RUN_URL"]})
    state["issue"] = issue["number"]
api(f"repos/{repo}/issues/{state['issue']}/comments", {"body":
    f"Real Codex attempt {stage}: {'PASS' if not errors else 'FAIL'}\n```json\n"+json.dumps(entry,indent=2)+"\n```"})
if stage == 3:
    assert [e["passed"] for e in state["results"]] == [False,False,True]
    assert len(set(e["threadId"] for e in state["results"])) == 1
    assert len(state["comments"]) == 2
    envclose = dict(os.environ)
    subprocess.run(["gh","api",f"repos/{repo}/issues/{state['issue']}","--method","PATCH",
                    "-f","state=closed"],env=envclose,capture_output=True,check=True)
state_path.write_text(json.dumps(state,indent=2))
print(json.dumps(entry))
print(f"Issue: https://github.com/{repo}/issues/{state['issue']}")
