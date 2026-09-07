"""Handbook QA; runtime files remain entirely within mini_agent/.

From the repository root:
conda run -n nanobot python study/examples/verify_example.py
"""
import html
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

study = Path(__file__).resolve().parents[1]
source_dir = study / "examples" / "mini_agent"
page = study / "chapters" / "10-build-your-agent" / "index.html"
document = page.read_text(encoding="utf-8")
blocks = re.findall(r'<code data-source-file="([^"]+)">(.*?)</code>', document, re.S)
assert len(blocks) == 7, "all seven runtime/test files must be embedded"
for name, code in blocks:
    assert html.unescape(code) == (source_dir / name).read_text(encoding="utf-8"), name
for link in re.findall(r'(?:href|src)="([^"]+)"', document):
    if link.startswith("#"):
        assert f'id="{link[1:]}"' in document, link
        continue
    target = (page.parent / link).resolve()
    assert target.is_relative_to(study), f"link leaves study: {link}"
    assert target.exists(), f"broken link: {link}"
print("PASS: seven embedded sources match; all links stay within study and resolve")

expected = """input: add 2 3
provider: step 1
request: call-1 add {"a": 2, "b": 3}
tool: add -> ok
provider: step 2
final: Result: 5
saved: 4 messages
answer: Result: 5
"""
with tempfile.TemporaryDirectory(prefix="independent-agent-") as temporary:
    copied = Path(temporary) / "mini_agent"
    shutil.copytree(source_dir, copied, ignore=shutil.ignore_patterns("__pycache__"))
    result = subprocess.run([sys.executable, "app.py"], cwd=copied, text=True,
                            capture_output=True, check=True)
    assert result.stdout == expected, result.stdout
    subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", ".", "-v"],
                   cwd=copied, check=True)
    for key, question, count, answer in [
        ("alice", "add 2 3", 4, 5), ("alice", "add 10 20", 8, 30),
        ("bob", "add 8 9", 4, 17),
    ]:
        result = subprocess.run(
            [sys.executable, "app.py", question, "--session", key, "--state-dir", "state"],
            cwd=copied, text=True, capture_output=True, check=True,
        )
        assert f"saved: {count} messages" in result.stdout, result.stdout
        assert f"answer: Result: {answer}" in result.stdout, result.stdout
print("PASS: isolated copy, exact demo output, 12 tests, three-process persistence/isolation")
