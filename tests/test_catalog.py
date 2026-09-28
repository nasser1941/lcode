import pytest

from lcode import catalog
from lcode.hardware import Hardware


def test_catalog_is_valid():
    specs = catalog.load()
    keys = [s.key for s in specs]
    assert len(keys) == len(set(keys)), "duplicate keys"
    assert specs[0].key == "qwen3.6-35b"
    for s in specs:
        assert s.size_gb > 0 and s.max_context >= 32768 and s.kv_kib_per_token > 0
        assert ":" in s.tag or s.tag.replace("-", "").replace(".", "").isalnum()


def test_find_by_key_tag_and_local_name():
    spec = catalog.find("qwen3.6-35b")
    assert spec is catalog.find("qwen3.6:35b-a3b-coding") is catalog.find("lcode-qwen3.6-35b")
    assert catalog.find("laguna-xs-2.1:latest").key == "laguna-xs-2.1"
    assert catalog.find("something-else:1b") is None


def test_memory_estimate_matches_measurement():
    # Measured with Ollama on an RTX 4080 Laptop at 256K: ~26.4 GiB in total. Estimates stay slightly above.
    assert 26.4 < catalog.find("qwen3.6-35b").memory_gib(262144) < 28.5
    # qwen3.5:9b measured 9.8 GB (9.1 GiB) at 128K and 16 GB (14.9 GiB) at 256K.
    assert 9.1 < catalog.find("qwen3.5-9b").memory_gib(131072) < 11.5


MACHINES = {
    "rtx4080-laptop-12gb": (Hardware("linux", "i9", 31, "RTX 4080 Laptop", 12), "qwen3.6-35b", 262144),
    "mac-m4-16gb": (Hardware("macos", "Apple M4", 16, "Apple M4 GPU", unified=True), "qwen3.5-9b", 65536),
    "mac-m4-24gb": (Hardware("macos", "Apple M4", 24, "Apple M4 GPU", unified=True), "qwen3.5-9b", 262144),
    "mac-m4-max-36gb": (Hardware("macos", "Apple M4 Max", 36, "GPU", unified=True), "qwen3.6-35b", 65536),
    "mac-m4-pro-48gb": (Hardware("macos", "Apple M4 Pro", 48, "GPU", unified=True), "qwen3.6-35b", 262144),
    "linux-8gb-gpu": (Hardware("linux", "x", 16, "RTX 4060", 8), "qwen3.5-4b", 65536),
}


@pytest.mark.parametrize("machine", MACHINES)
def test_recommendations(machine):
    hw, key, ctx = MACHINES[machine]
    spec, context = catalog.recommend(hw)
    assert (spec.key, context) == (key, ctx)


def test_fit_speed_notes():
    gpu = Hardware("linux", "x", 64, "RTX 4090", 24)
    assert catalog.find("qwen3.6-35b").fit(gpu) == (262144, "good (experts in RAM)")
    assert catalog.find("qwen3.5-9b").fit(gpu)[1] == "fast"
    assert catalog.find("qwen3.5-9b").fit(Hardware("linux", "x", 32))[1] == "CPU only (slow)"
    assert catalog.find("nemotron-3.5-lightning").fit(Hardware("macos", "M4", 8, "GPU", unified=True)) == (
        None,
        "too large",
    )


def test_budgets():
    assert Hardware("macos", "M4 Max", 128, unified=True).budget_gib == 96
    assert Hardware("linux", "x", 32, "GPU", 12).budget_gib == 36
    assert Hardware("linux", "x", 32, "GPU", 12).fast_gib == 12
