"""`.env` 加载测试。

为什么值得测：凭据不能进仓库（`.idea/` 是跟着仓库走的），而 IDEA 启动、命令行
uvicorn、cron 三条起法各有各的环境——进程自己读这一份 `.env` 是让三者拿到同一份
配置的办法。它读错了的表现是「告警悄悄不推了」，那正是最不该悄悄失效的功能。
"""

from __future__ import annotations

import os

import pytest

from sourcepilot.settings import load_dotenv


@pytest.fixture
def isolated_env(monkeypatch):
    """把 os.environ 换成一个干净字典，测完自动还原。"""
    fake: dict[str, str] = {}
    monkeypatch.setattr(os, "environ", fake)
    return fake


def write(tmp_path, text: str):
    path = tmp_path / ".env"
    path.write_text(text, encoding="utf-8")
    return path


class TestParsing:
    def test_basic_key_value(self, tmp_path, isolated_env):
        load_dotenv(write(tmp_path, "TELEGRAM_BOT_TOKEN=123:abc\nTELEGRAM_CHAT_ID=456\n"))
        assert isolated_env["TELEGRAM_BOT_TOKEN"] == "123:abc"
        assert isolated_env["TELEGRAM_CHAT_ID"] == "456"

    def test_comments_and_blank_lines_ignored(self, tmp_path, isolated_env):
        assert load_dotenv(write(tmp_path, "# 注释\n\n  \nA=1\n#B=2\n")) == {"A": "1"}

    def test_export_prefix_tolerated(self, tmp_path, isolated_env):
        """从 shell 脚本里直接抄过来的行常常带 export。"""
        load_dotenv(write(tmp_path, "export A=1\n"))
        assert isolated_env["A"] == "1"

    def test_paired_quotes_stripped(self, tmp_path, isolated_env):
        load_dotenv(write(tmp_path, "A=\"1\"\nB='2'\nC=\"3'\n"))
        assert isolated_env["A"] == "1"
        assert isolated_env["B"] == "2"
        assert isolated_env["C"] == "\"3'"  # 不成对就原样保留，别乱改人家的值

    def test_value_may_contain_equals(self, tmp_path, isolated_env):
        """只切第一个等号——base64 之类的值里带 = 很常见。"""
        load_dotenv(write(tmp_path, "A=a=b=c\n"))
        assert isolated_env["A"] == "a=b=c"

    def test_line_without_equals_skipped(self, tmp_path, isolated_env):
        assert load_dotenv(write(tmp_path, "这行是废话\nA=1\n")) == {"A": "1"}

    def test_empty_value_is_kept(self, tmp_path, isolated_env):
        """模板里 TELEGRAM_BOT_TOKEN= 就是空的，读成空串而不是跳过——
        空串与「没有这个键」在 configured() 那里是同一个结果，但别在这里制造差异。"""
        assert load_dotenv(write(tmp_path, "A=\n")) == {"A": ""}
        assert isolated_env["A"] == ""


class TestPrecedence:
    def test_real_env_wins(self, tmp_path, isolated_env):
        """部署时用系统环境覆盖文件里的值是常规做法；反过来会让人怎么 export 都不生效。"""
        isolated_env["A"] = "来自环境"
        injected = load_dotenv(write(tmp_path, "A=来自文件\nB=新的\n"))
        assert isolated_env["A"] == "来自环境"
        assert injected == {"B": "新的"}  # 返回值只报实际注入的


class TestFailureSafety:
    def test_missing_file_is_fine(self, tmp_path, isolated_env):
        assert load_dotenv(tmp_path / "不存在") == {}

    def test_unreadable_file_does_not_raise(self, tmp_path, isolated_env, monkeypatch):
        """配置文件坏了不该让整个服务起不来——settings 是所有模块的 import 依赖。"""
        path = write(tmp_path, "A=1\n")

        def boom(*args, **kwargs):
            raise OSError("权限不足")

        monkeypatch.setattr("pathlib.Path.read_text", boom)
        assert load_dotenv(path) == {}

    def test_directory_instead_of_file(self, tmp_path, isolated_env):
        (tmp_path / "envdir").mkdir()
        assert load_dotenv(tmp_path / "envdir") == {}
