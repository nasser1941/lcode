import pytest

from conftest import call, output, reply, run
from lcode import gitflow, secretscan
from lcode.secretscan import git_steps, scan_diff, scan_line
from test_gitflow import answers

PASSWORD = "Xk9pq72z"  # not a real one; lcode: allow-secret
CONFIG = f'DEFAULTS = {{"ip": "192.168.1.10", "username": "admin", "password": "{PASSWORD}"}}\n'


@pytest.mark.parametrize(
    ("text", "config", "label"),
    [
        (CONFIG, False, "a password or key"),
        (f'url = "rtsp://admin:{PASSWORD}@192.168.1.10:554/cam"', False, "a password in a URL"),
        (f'connect(password="{PASSWORD}")', False, "a password or key"),
        (f"PASSWORD={PASSWORD}", True, "a password or key"),
        (f'client_secret: "{PASSWORD}"', True, "a password or key"),
        ("XAI_API_KEY=xai-" + "Ab1" * 8, True, "an xAI API key"),  # made up, built so scanners skip it
        ('key = "ghp_' + "a" * 36 + '"', False, "a GitHub token"),
        ("-----BEGIN OPENSSH PRIVATE KEY-----", False, "a private key"),  # lcode: allow-secret
    ],
)
def test_secrets_are_found(text, config, label):
    found = scan_line(text, config)
    assert found is not None and found[0] == label
    assert PASSWORD not in found[1] and "ghp_" + "a" * 36 not in found[1]


@pytest.mark.parametrize(
    "text",
    [
        'url = f"rtsp://{user}:{password}@{ip}:554/cam"',
        '"password": "CHANGE_ME",',
        '"password": "your_password",',
        'password = os.environ["CAM_PASSWORD"]',
        'password = config["password"]',
        "password = args.password",
        'token_url = "https://example.com/oauth/token"',
        'password_file = "/run/secrets/db"',
        "max_password_length = 128",
        "postgresql://user:password@localhost/db",
        "rtsp://admin:…@192.168.1.10",
        'secret_key: "change-me-to-something-random"',
        'api_key = "test-key-0123456789"',
        f'password = "{PASSWORD}"  # lcode: allow-secret',
        f'password = "{PASSWORD}"  # gitleaks:allow',
    ],
)
def test_placeholders_and_references_are_not_secrets(text):
    assert scan_line(text) is None


def test_unquoted_values_count_only_in_configuration():
    assert scan_line(f"password = {PASSWORD}") is None  # code: a variable
    assert scan_line(f"password = {PASSWORD}", config=True) is not None
    assert scan_line("password: ${DB_PASSWORD}", config=True) is None


def test_a_diff_gives_files_and_lines():
    diff = (
        "commit:abc1234\n"
        "diff --git a/cam.py b/cam.py\n--- a/cam.py\n+++ b/cam.py\n@@ -10,0 +11,2 @@\n"
        f"+import os\n+{CONFIG}"
        "diff --git a/.env b/.env\nnew file mode 100644\n--- /dev/null\n+++ b/.env\n@@ -0,0 +1 @@\n+DEBUG=1\n"
        "diff --git a/.env.example b/.env.example\n--- /dev/null\n+++ b/.env.example\n@@ -0,0 +1 @@\n+PASSWORD=\n"
    )
    found = scan_diff(diff)
    assert [(f.path, f.line, f.commit) for f in found] == [("cam.py", 12, "abc1234"), (".env", 0, "abc1234")]
    assert found[0].describe().startswith("cam.py:12 (commit abc1234): a password or key: DEFAULTS")


def test_which_commands_commit_or_push():
    steps = git_steps('cd /src/app && git add cam.py README.md && git commit -m "fix: fit the screen"')
    assert steps.adds == [["cam.py", "README.md"]] and steps.commit == ["-m", "fix: fit the screen"]
    assert git_steps("git -C app push -u origin feature").push == ["-u", "origin", "feature"]
    assert git_steps("GIT_AUTHOR_NAME=x git commit -am wip").commit == ["-am", "wip"]
    assert git_steps("git status && git diff | grep password") is None
    assert git_steps("echo commit the git changes") is None
    nested = git_steps("bash -c 'git add . && git commit -m wip'")
    assert nested.commit == ["--all"] and nested.adds == [["."]] and nested.push is None
    assert git_steps('sh -c "git -C app push origin main"').push == []
    assert git_steps("bash -c 'git add . && git commit -m wip && git push'").push == []


def commit_count(repo):
    return int(run(repo, "rev-list", "--count", "HEAD"))


def test_what_a_commit_would_add_is_scanned(git_repo):
    (git_repo / "cam.py").write_text("import os\n" + CONFIG)
    action, found = secretscan.check(git_repo, "git add cam.py && git commit -m cam")
    assert action == "commit" and [(f.path, f.line) for f in found] == [("cam.py", 2)]
    assert secretscan.check(git_repo, "git add README.md && git commit -m docs")[1] == []
    assert secretscan.check(git_repo, "git add . && git commit -m all")[1] != []
    run(git_repo, "add", "cam.py")
    assert secretscan.check(git_repo, "git commit -m staged")[1] != []
    run(git_repo, "commit", "-qm", "cam")
    (git_repo / "cam.py").write_text("import os\n" + CONFIG.replace(PASSWORD, "Zq81mn44"))
    assert secretscan.check(git_repo, "git commit -m nothing-staged")[1] == []
    assert secretscan.check(git_repo, "git commit -am everything")[1] != []


def test_what_a_push_would_send_is_scanned(git_repo, github):
    (git_repo / "cam.py").write_text(CONFIG)
    run(git_repo, "add", "cam.py")
    run(git_repo, "commit", "-qm", "cam")
    action, found = secretscan.check(git_repo, "git push origin main")
    sha = run(git_repo, "rev-parse", "--short", "HEAD").strip()
    assert action == "push" and found[0].commit == sha and found[0].path == "cam.py"
    run(git_repo, "push", "-q", "origin", "main")
    assert secretscan.check(git_repo, "git push")[1] == []  # already there


def test_the_model_cannot_commit_a_secret_without_asking(make_agent, git_repo):
    (git_repo / "cam.py").write_text(CONFIG)
    commit = call("bash", command="git add cam.py && git commit -qm 'add camera defaults'")
    agent = make_agent([reply(tool_calls=[commit]), reply("ok")], mode="yolo")
    agent.run_turn("commit it")
    result = next(m["content"] for m in agent.messages if m["role"] == "tool")
    assert result.startswith("Error: lcode's secret check stopped this command. This commit would add")
    assert "cam.py:1: a password or key" in result and PASSWORD not in result
    assert "environment variable" in result and commit_count(git_repo) == 1
    assert "cam.py:1" in output(agent)


def test_in_ask_mode_the_user_decides(make_agent, git_repo, monkeypatch):
    (git_repo / "cam.py").write_text(CONFIG)
    asked = []
    commit = call("bash", command="git add cam.py && git commit -qm 'add camera defaults'")
    agent = make_agent([reply(tool_calls=[commit]), reply("ok")], mode="ask")
    agent.perms.approve = lambda request: asked.append(request) or True
    agent.run_turn("commit it")
    assert [r["kind"] for r in asked] == ["secret"] and commit_count(git_repo) == 2
    assert asked[0]["title"] == "This commit looks like it holds a secret"


def test_the_check_can_be_turned_off(make_agent, git_repo):
    (git_repo / "cam.py").write_text(CONFIG)
    commit = call("bash", command="git add cam.py && git commit -qm 'add camera defaults'")
    agent = make_agent([reply(tool_calls=[commit]), reply("ok")], mode="yolo", secret_check=False)
    agent.run_turn("commit it")
    assert commit_count(git_repo) == 2


def test_pr_asks_before_pushing_a_secret(make_agent, git_repo, github, monkeypatch):
    remote, _ = github
    (git_repo / "cam.py").write_text(CONFIG)
    run(git_repo, "switch", "-q", "-c", "cams")
    run(git_repo, "add", "cam.py")
    run(git_repo, "commit", "-qm", "Add cameras")
    agent = make_agent()
    answers(monkeypatch, "n")
    assert gitflow.push(agent, git_repo, "origin", "cams") is False
    assert "This push would add what looks like a secret" in output(agent) and "cams" not in run(remote, "branch")


def test_the_action_does_not_commit_a_secret(git_repo):
    (git_repo / "cam.py").write_text(CONFIG)
    found = secretscan.in_files(git_repo, ["."], staged=False)
    text = secretscan.report("commit", found, excerpts=False)
    assert "cam.py:1: a password or key" in text and "DEFAULTS" not in text
