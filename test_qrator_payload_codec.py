import json
from pathlib import Path

import pytest

from qrator_payload_codec import decode_field, encode_field


ROOT = Path(__file__).resolve().parent


VECTORS = json.loads((ROOT / "test_fixtures" / "codec-vectors.json").read_text(encoding="utf-8"))["vectors"]


@pytest.mark.parametrize("vector", VECTORS, ids=lambda vector: vector["name"])
def test_f_matches_synthetic_known_answer(vector) -> None:
    assert encode_field(vector["raw_f"], "f") == vector["f"]
    assert decode_field(vector["f"], "f") == vector["raw_f"]


@pytest.mark.parametrize("vector", VECTORS, ids=lambda vector: vector["name"])
def test_s_matches_synthetic_known_answer(vector) -> None:
    assert encode_field(vector["raw_s"], "s") == vector["s"]
    assert decode_field(vector["s"], "s") == vector["raw_s"]
