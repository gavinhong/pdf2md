import hashlib
import json

import pytest

from pdf2md.models import check_models, model_status


def test_manifest_checks_hash_and_blocks_traversal(tmp_path):
    keys = "layout formula table_unet table_slanet table_paddle_cls table_q_cls ocr_det ocr_rec ocr_dictionary orientation".split()
    payload = b"model fixture"
    (tmp_path / "test.onnx").write_bytes(payload)
    records = [
        {
            "id": key,
            "path": "test.onnx",
            "bytes": len(payload),
            "sha256": hashlib.sha256(payload).hexdigest(),
        }
        for key in keys
    ]
    manifest = {"files": records, "profile": "test", "total_bytes": len(payload)}
    (tmp_path / "manifest.json").write_text(json.dumps(manifest))
    assert len(check_models(tmp_path, full=True)["paths"]) == 10
    (tmp_path / "test.onnx").write_bytes(b"broken model!")
    with pytest.raises(ValueError, match="校验失败"):
        check_models(tmp_path, full=True)
    records[0]["path"] = "../test.onnx"
    (tmp_path / "manifest.json").write_text(json.dumps(manifest))
    assert not model_status(tmp_path)["ready"]
