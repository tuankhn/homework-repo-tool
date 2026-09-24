"""
Unit test cho cac ham "thuan" (khong goi git/gh, khong can mang) trong
homework_repo_tool.cli. Chay bang: pytest

Muc tieu: dam bao logic dat ten repo, loc file, va canh bao bao mat
khong bi hong khi co nguoi sua code sau nay.
"""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from homework_repo_tool import cli


# ---------------------------------------------------------------------------
# slugify: chuan hoa ten file/folder thanh ten repo hop le
# ---------------------------------------------------------------------------

def test_slugify_basic():
    assert cli.slugify("Bai Tap 1") == "bai-tap-1"


def test_slugify_giu_dau_gach_va_cham():
    assert cli.slugify("bai_1.v2") == "bai_1.v2"


def test_slugify_rong_tra_ve_mac_dinh():
    assert cli.slugify("   ") == "homework"


def test_slugify_ky_tu_dac_biet():
    assert cli.slugify("Bài Tập #1 (nộp lại)") == "b-i-t-p-1-n-p-l-i"


# ---------------------------------------------------------------------------
# apply_name_affixes + build_up_session_items: ghep ten repo dang
# MONHOC-ssBUOI-tenbai-tenban (chuc nang "Tach folder con" / up-folder)
# ---------------------------------------------------------------------------

def test_apply_name_affixes_ca_tien_to_va_hau_to():
    assert cli.apply_name_affixes("bai1", prefix="it105-ss17", suffix="tkn") == (
        "it105-ss17-bai1-tkn"
    )


def test_apply_name_affixes_khong_co_affix():
    assert cli.apply_name_affixes("bai1") == "bai1"


def test_apply_name_affixes_chi_prefix():
    assert cli.apply_name_affixes("bai1", prefix="ss05") == "ss05-bai1"


def test_apply_name_affixes_chi_suffix():
    assert cli.apply_name_affixes("bai1", suffix="it205") == "bai1-it205"


def test_build_up_session_items_dat_ten_va_khong_trung(tmp_path):
    (tmp_path / "bai1").mkdir()
    (tmp_path / "bai1" / "a.py").write_text("print(1)")
    (tmp_path / "bai2").mkdir()
    (tmp_path / "bai2" / "b.py").write_text("print(2)")

    items = cli.build_up_session_items(tmp_path, prefix="it105-ss17", suffix="tkn")
    repo_names = sorted(item["repo_name"] for item in items)

    assert repo_names == ["it105-ss17-bai1-tkn", "it105-ss17-bai2-tkn"]


def test_build_up_session_items_trung_ten_thi_tu_them_so(tmp_path):
    # "Bai 1" va "bai-1" cung slugify ra "bai-1" -> phai tu dong danh so lai
    (tmp_path / "Bai 1").mkdir()
    (tmp_path / "Bai 1" / "a.py").write_text("x")
    (tmp_path / "bai-1").mkdir()
    (tmp_path / "bai-1" / "b.py").write_text("y")

    items = cli.build_up_session_items(tmp_path)
    repo_names = sorted(item["repo_name"] for item in items)

    assert repo_names == ["bai-1", "bai-1-2"]


# ---------------------------------------------------------------------------
# create_session_repo_name / create_repo_name: ten repo cho submit-file, up
# ---------------------------------------------------------------------------

def test_create_session_repo_name_mac_dinh():
    assert cli.create_session_repo_name("bai1.py", 5, "it205") == "bai1-ss05-IT205"


def test_create_repo_name():
    assert cli.create_repo_name(1, 5, "it205") == "ex01-ss05-IT205"


# ---------------------------------------------------------------------------
# should_ignore_name: khong nop nham file rac / bi mat
# ---------------------------------------------------------------------------

def test_should_ignore_cac_file_an_va_venv():
    assert cli.should_ignore_name(".env") is True
    assert cli.should_ignore_name(".git") is True
    assert cli.should_ignore_name("__pycache__") is True
    assert cli.should_ignore_name("node_modules") is True


def test_should_ignore_gitignore_thi_khong_bi_bo():
    assert cli.should_ignore_name(".gitignore") is False


def test_should_ignore_file_binh_thuong_thi_khong_bi_bo():
    assert cli.should_ignore_name("main.py") is False


# ---------------------------------------------------------------------------
# scan_for_secrets / scan_for_large_files: canh bao truoc khi push
# ---------------------------------------------------------------------------

def test_scan_for_secrets_phat_hien_aws_key(tmp_path):
    (tmp_path / "config.py").write_text("KEY = 'AKIAABCDEFGHIJKLMNOP'")
    findings = cli.scan_for_secrets(tmp_path)
    assert len(findings) == 1
    assert "AWS" in findings[0][1]


def test_scan_for_secrets_file_sach_thi_khong_canh_bao(tmp_path):
    (tmp_path / "main.py").write_text("print('hello world')")
    assert cli.scan_for_secrets(tmp_path) == []


def test_scan_for_large_files_phat_hien_file_qua_50mb(tmp_path):
    big_file = tmp_path / "data.bin"
    big_file.write_bytes(b"0")
    # gia lap file lon bang cach vuot MAX_FILE_SIZE_WARN_BYTES qua os.truncate
    with open(big_file, "r+b") as handle:
        handle.truncate(cli.MAX_FILE_SIZE_WARN_BYTES + 1)

    large = cli.scan_for_large_files(tmp_path)
    assert len(large) == 1
    assert large[0][0].name == "data.bin"


def test_scan_for_large_files_file_nho_thi_khong_canh_bao(tmp_path):
    (tmp_path / "small.txt").write_text("noi dung nho")
    assert cli.scan_for_large_files(tmp_path) == []


# ---------------------------------------------------------------------------
# detect_stack: phat hien stack de tao .gitignore/README phu hop
# ---------------------------------------------------------------------------

def test_detect_stack_python(tmp_path):
    (tmp_path / "requirements.txt").write_text("fastapi")
    assert cli.detect_stack(tmp_path) == "python"


def test_detect_stack_node(tmp_path):
    (tmp_path / "package.json").write_text("{}")
    assert cli.detect_stack(tmp_path) == "node"


def test_detect_stack_web(tmp_path):
    (tmp_path / "index.html").write_text("<html></html>")
    assert cli.detect_stack(tmp_path) == "web"


def test_detect_stack_generic_khi_khong_ro(tmp_path):
    (tmp_path / "notes.txt").write_text("ghi chu")
    assert cli.detect_stack(tmp_path) == "generic"
