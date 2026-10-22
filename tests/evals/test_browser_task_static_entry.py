import pytest
from aiohttp import ClientSession

from evals.browser_task_gateway import BrowserTaskGateway, ScriptedBrowserProvider


@pytest.mark.parametrize("case_id", ["V073", "V079"])
async def test_explicit_frontend_uses_same_origin_and_is_not_overwritten(tmp_path, case_id):
    frontend = tmp_path / "frontend"
    frontend.mkdir()
    (frontend / "index.html").write_text("<html><body>STATIC-ENTRY</body></html>", encoding="utf-8")
    gateway = await BrowserTaskGateway.create(
        profile="B-scripted", runtime=tmp_path / "runtime", workspace=tmp_path / "workspace",
        provider=ScriptedBrowserProvider(case_id), control={"case_id": case_id},
        frontend_dir=frontend,
    )
    try:
        assert gateway.channel._static_dist_path == frontend.resolve()
        async with ClientSession() as http:
            async with http.get(f"http://127.0.0.1:{gateway.port}/") as response:
                assert response.status == 200
                assert "STATIC-ENTRY" in await response.text()
            if case_id == "V079":
                origin = f"http://127.0.0.1:{gateway.port}"
                async with http.get(origin + "/webui/bootstrap", headers={
                    "Origin": origin, "X-Nanobot-Auth": "SYNTHETIC-V079-OWNER-LOGIN"
                }) as response:
                    assert response.status == 200
                async with http.get(origin + "/webui/bootstrap", headers={
                    "Origin": "http://different.example.test", "X-Nanobot-Auth": "SYNTHETIC-V079-OWNER-LOGIN"
                }) as response:
                    assert response.status == 403
    finally:
        await gateway.close()
