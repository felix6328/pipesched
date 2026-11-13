from pathlib import Path

from pipesched.envload import load_siliconflow_api_key


def test_loads_only_the_siliconflow_line(tmp_path: Path):
    p = tmp_path / ".env.local"
    p.write_text(
        "DATABASE_URL=postgres://should-not-be-loaded\n"
        "JWT_SECRET=superjwt\n"
        "AES_MASTER_KEY=aeskey\n"
        "DEEPSEEK_API_KEY=sk-deepseek-should-not-load\n"
        "DEEPSEEK_BASE_URL=https://aiproxy.xin/cosphere/v1\n"
        "SILICONFLOW_API_KEY=sk-sf-test-key-xyz\n"
        "REDIS_URL=redis://should-not-be-loaded\n",
        encoding="utf-8",
    )
    val = load_siliconflow_api_key(p)
    assert val == "sk-sf-test-key-xyz"


def test_export_prefix_and_quotes(tmp_path: Path):
    p = tmp_path / ".env.local"
    p.write_text('export SILICONFLOW_API_KEY="abc-quoted"\n', encoding="utf-8")
    assert load_siliconflow_api_key(p) == "abc-quoted"


def test_missing_or_empty(tmp_path: Path):
    p = tmp_path / ".env.local"
    p.write_text("JWT_SECRET=x\nSILICONFLOW_API_KEY=\n", encoding="utf-8")
    assert load_siliconflow_api_key(p) == ""
    q = tmp_path / "absent.env"
    assert load_siliconflow_api_key(q) == ""
