import json
import urllib.error
import urllib.parse
import urllib.request

import pytest

from pdf2md.desktop import DesktopBridge, LocalServer
from pdf2md.domain import Task


def test_local_api_requires_session_and_rejects_foreign_origin(service):
    server = LocalServer(DesktopBridge(service))
    server.start()
    try:

        def request(token="", origin=None):
            headers = {"X-PDF2MD-Token": token, "Content-Type": "application/json"}
            if origin:
                headers["Origin"] = origin
            return urllib.request.urlopen(
                urllib.request.Request(
                    server.url + "/api/snapshot", data=b'{"args":[]}', headers=headers
                )
            )

        for token, origin in [("", None), (server.token, "https://example.com")]:
            with pytest.raises(urllib.error.HTTPError) as error:
                request(token, origin)
            assert error.value.code == 403
        with request(server.token, server.url) as response:
            assert "tasks" in json.load(response)
        with pytest.raises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(server.url + "/%2e%2e/pyproject.toml")
        assert error.value.code == 404
    finally:
        server.stop()


def api_request(server, name, args):
    request = urllib.request.Request(
        server.url + "/api/" + name,
        data=json.dumps({"args": args}, ensure_ascii=False).encode("utf-8"),
        headers={
            "X-PDF2MD-Token": server.token,
            "Content-Type": "application/json",
            "Origin": server.url,
        },
    )
    with urllib.request.urlopen(request) as response:
        return json.load(response)


def test_review_http_saves_more_than_one_mib_utf8(service, pdf_path, tmp_path):
    folder = tmp_path / "http-result"
    folder.mkdir()
    md = folder / "http.md"
    md.write_bytes("原文😀\n".encode("utf-8"))
    task = Task(
        id="http-review",
        source=str(pdf_path),
        name=pdf_path.name,
        pages=3,
        status="succeeded",
        output_dir=str(folder),
        markdown_file=str(md),
    )
    service.tasks.append(task)
    server = LocalServer(DesktopBridge(service))
    server.start()
    try:
        doc = api_request(server, "review_document", [task.id])
        assert doc["ok"]
        suffix = "汉字😀" * 150_000
        text = doc["markdown"] + suffix
        assert len(text.encode("utf-8")) > 1024 * 1024
        end = len(doc["markdown"].encode("utf-16-le")) // 2
        reply = api_request(
            server,
            "save_review",
            [
                task.id,
                {
                    "markdown": text,
                    "expected_sha256": doc["sha256"],
                    "expected_revision": doc["review_revision"],
                    "changes": [{"from": end, "to": end, "insert": suffix}],
                    "review_updates": {},
                },
            ],
        )
        assert reply["ok"], reply
        assert reply["markdown"] == text
        assert md.read_bytes() == text.encode("utf-8")
    finally:
        server.stop()


def test_review_page_resource_requires_token_and_stays_inside_task(service, pdf_path, tmp_path):
    folder = tmp_path / "page-result"
    folder.mkdir()
    md = folder / "page.md"
    md.write_bytes(b"document\n")
    task = Task(
        id="page-review",
        source=str(pdf_path),
        name=pdf_path.name,
        pages=3,
        status="succeeded",
        output_dir=str(folder),
        markdown_file=str(md),
    )
    service.tasks.append(task)
    outside = tmp_path / "outside-secret.txt"
    outside.write_text("private document", encoding="utf-8")
    server = LocalServer(DesktopBridge(service))
    server.start()
    try:
        reply = api_request(server, "review_page", [task.id, 1, 1])
        assert reply["ok"], reply
        with urllib.request.urlopen(server.url + reply["url"]) as response:
            assert response.headers["Content-Type"] == "image/png"
            assert response.read().startswith(b"\x89PNG\r\n\x1a\n")
        for url, expected in (
            (reply["url"].replace(server.token, "wrong-token"), 403),
            (f"/resource/{server.token}/unregistered/document.md", 404),
            (f"/resource/{server.token}/{task.id}/%2e%2e/outside-secret.txt", 404),
            (
                f"/resource/{server.token}/{task.id}/"
                + urllib.parse.quote(str(outside.resolve()), safe=""),
                404,
            ),
        ):
            with pytest.raises(urllib.error.HTTPError) as error:
                urllib.request.urlopen(server.url + url)
            assert error.value.code == expected
        assert not api_request(server, "review_page", [task.id, 0, 1])["ok"]
        assert not api_request(server, "review_page", [task.id, 4, 1])["ok"]
    finally:
        server.stop()
