import argparse
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

HAS_RICH = False
try:
    from rich import box
    from rich.align import Align
    from rich.console import Console, Group
    from rich.live import Live
    from rich.panel import Panel
    from rich.table import Table
    from rich.text import Text

    HAS_RICH = True
except ImportError:
    box = None
    Align = None
    Group = None
    Live = None
    Text = None

    def _strip_markup(value):
        return re.sub(r"\[/?[^\]]+\]", "", str(value))

    def _visible_len(value):
        return len(_strip_markup(value))

    class Console:
        def print(self, *values, **kwargs):
            if not values:
                print()
                return
            end = kwargs.get("end", "\n")
            sep = kwargs.get("sep", " ")
            text = sep.join(_strip_markup(value) for value in values)
            print(text, end=end)

        def rule(self, title=""):
            width = 60
            label = _strip_markup(title)
            if label:
                side = max(2, (width - len(label) - 2) // 2)
                print(f"{'─' * side} {label} {'─' * side}")
            else:
                print("─" * width)

    class Panel:
        def __init__(self, renderable, title=None, border_style=None, subtitle=None, padding=None, expand=True):
            self.renderable = renderable
            self.title = title
            self.subtitle = subtitle

        def __str__(self):
            content = _strip_markup(self.renderable)
            lines = content.splitlines() or [""]
            width = max([_visible_len(line) for line in lines] + [20])
            if self.title:
                width = max(width, _visible_len(self.title) + 4)
            if self.subtitle:
                width = max(width, _visible_len(self.subtitle) + 4)
            width = min(max(width + 2, 42), 78)

            def hline(left, right, label=None):
                if label:
                    text = f" {_strip_markup(label)} "
                    fill = max(0, width - len(text))
                    left_fill = fill // 2
                    right_fill = fill - left_fill
                    return f"{left}{'─' * left_fill}{text}{'─' * right_fill}{right}"
                return f"{left}{'─' * width}{right}"

            out = [hline("╭", "╮", self.title)]
            for line in lines:
                clean = _strip_markup(line)
                pad = max(width - len(clean) - 1, 0)
                out.append(f"│ {clean}{' ' * pad}│")
            out.append(hline("╰", "╯", self.subtitle))
            return "\n".join(out)

    class Table:
        def __init__(self, title=None, box=None, show_header=True, expand=False, padding=None, border_style=None, title_style=None):
            self.title = title
            self.columns = []
            self.rows = []
            self.show_header = show_header

        def add_column(self, header, **kwargs):
            self.columns.append(header)

        def add_row(self, *values):
            self.rows.append([_strip_markup(value) for value in values])

        def __str__(self):
            cols = self.columns or [f"C{i+1}" for i in range(max((len(r) for r in self.rows), default=0))]
            data = self.rows
            widths = []
            for index, col in enumerate(cols):
                cells = [str(col)] + [row[index] if index < len(row) else "" for row in data]
                widths.append(max(len(cell) for cell in cells))

            def fmt_row(cells):
                parts = []
                for index, width in enumerate(widths):
                    cell = cells[index] if index < len(cells) else ""
                    parts.append(f" {str(cell).ljust(width)} ")
                return "│" + "│".join(parts) + "│"

            def sep(left, mid, right):
                return left + mid.join("─" * (w + 2) for w in widths) + right

            lines = []
            if self.title:
                lines.append(f"  {_strip_markup(self.title)}")
            lines.append(sep("┌", "┬", "┐"))
            if self.show_header and cols:
                lines.append(fmt_row(cols))
                lines.append(sep("├", "┼", "┤"))
            for row in data:
                lines.append(fmt_row(row))
            lines.append(sep("└", "┴", "┘"))
            return "\n".join(lines)


console = Console(highlight=False, soft_wrap=True) if HAS_RICH else Console()

IGNORED_BATCH_FOLDERS = {
    ".git",
    ".github",
    ".idea",
    ".vscode",
    "__pycache__",
    "node_modules",
    "venv",
    ".venv",
    "env",
    ".env",
    "dist",
    "build",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
    "coverage",
    "htmlcov",
    ".next",
    ".nuxt",
    "target",
}

IGNORED_SESSION_FILES = {
    ".gitignore",
    "README.md",
}

ALWAYS_IGNORE_NAMES = {
    ".DS_Store",
    "Thumbs.db",
    ".env",
    ".env.local",
    ".env.development",
    ".env.production",
    ".env.test",
    "credentials.json",
    "secrets.json",
    "service-account.json",
    ".python-version",
}

ALWAYS_IGNORE_SUFFIXES = {
    ".pyc",
    ".pyo",
    ".pyd",
    ".log",
    ".sqlite",
    ".sqlite3",
    ".db",
}

DEFAULT_CONFIG = {
    "default_course": "",
    "default_visibility": "public",
    "naming": "{name}-ss{session:02d}-{course}",
}

SECRET_PATTERNS = [
    (re.compile(r"AKIA[0-9A-Z]{16}"), "AWS Access Key"),
    (re.compile(r"-----BEGIN (RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----"), "Private key"),
    (re.compile(r"sk-[A-Za-z0-9]{20,}"), "API key (sk-...)"),
    (re.compile(r"ghp_[A-Za-z0-9]{20,}"), "GitHub token (ghp_...)"),
    (re.compile(r"(?i)(password|passwd|secret|api[_-]?key)\s*[:=]\s*[\"'][^\"'\s]{6,}[\"']"), "Hardcoded secret/password"),
]

SECRET_SCAN_SKIP_SUFFIXES = {
    ".png", ".jpg", ".jpeg", ".gif", ".ico", ".pdf", ".zip", ".exe",
    ".sqlite", ".sqlite3", ".db", ".woff", ".woff2", ".ttf", ".mp4",
}

MAX_FILE_SIZE_WARN_BYTES = 50 * 1024 * 1024

STACK_GITIGNORES = {
    "python": "\n".join(
        [
            "__pycache__/",
            "*.py[cod]",
            "*.egg-info/",
            ".venv/",
            "venv/",
            "env/",
            ".env",
            ".env.*",
            ".pytest_cache/",
            ".mypy_cache/",
            ".ruff_cache/",
            "dist/",
            "build/",
            ".DS_Store",
            ".vscode/",
            ".idea/",
            "*.log",
            "*.sqlite3",
            "*.db",
        ]
    )
    + "\n",
    "node": "\n".join(
        [
            "node_modules/",
            "dist/",
            "build/",
            ".next/",
            ".nuxt/",
            ".env",
            ".env.*",
            "npm-debug.log*",
            "yarn-error.log*",
            ".DS_Store",
            ".vscode/",
            ".idea/",
            "coverage/",
        ]
    )
    + "\n",
    "web": "\n".join(
        [
            "node_modules/",
            ".env",
            ".env.*",
            ".DS_Store",
            ".vscode/",
            ".idea/",
            "dist/",
            "build/",
        ]
    )
    + "\n",
    "generic": "\n".join(
        [
            "node_modules/",
            ".env",
            ".env.*",
            ".venv/",
            "venv/",
            "__pycache__/",
            ".DS_Store",
            ".vscode/",
            ".idea/",
            "dist/",
            "build/",
            "*.log",
        ]
    )
    + "\n",
}


class SubmissionSkipped(Exception):
    pass


class DryRunExit(Exception):
    pass


def get_app_dir():
    folder = Path.home() / ".homework-repo-tool"
    folder.mkdir(exist_ok=True)
    return folder


def get_config_file():
    return get_app_dir() / "config.json"


def load_config():
    config_file = get_config_file()
    config = dict(DEFAULT_CONFIG)

    if config_file.exists():
        try:
            data = json.loads(config_file.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                config.update({key: data[key] for key in DEFAULT_CONFIG if key in data})
        except (json.JSONDecodeError, OSError):
            console.print("[yellow]Warning: invalid config.json, using defaults.[/yellow]")

    return config


def save_config(config):
    config_file = get_config_file()
    config_file.write_text(
        json.dumps(config, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def show_config():
    config = load_config()
    table = Table(title="Homework Repo Tool config")
    table.add_column("Key", style="bold")
    table.add_column("Value", style="green")
    table.add_column("Path", style="dim")

    for key, value in config.items():
        table.add_row(key, json.dumps(value) if not isinstance(value, str) else value, "")

    table.add_row("config_file", str(get_config_file()), "")
    console.print(table)


def set_config_value(key, value):
    if key not in DEFAULT_CONFIG:
        console.print(f"[red]Unknown config key:[/red] {key}")
        console.print(f"Available keys: {', '.join(DEFAULT_CONFIG)}")
        return

    config = load_config()
    default_value = DEFAULT_CONFIG[key]

    if isinstance(default_value, bool):
        lowered = str(value).strip().lower()
        if lowered in {"1", "true", "yes", "y", "on"}:
            parsed = True
        elif lowered in {"0", "false", "no", "n", "off"}:
            parsed = False
        else:
            console.print(f"[red]Invalid boolean value for {key}:[/red] {value}")
            return
    else:
        parsed = str(value)

    config[key] = parsed
    save_config(config)
    console.print(f"[green]Updated[/green] {key} = {parsed}")


def run(command, cwd=None, dry_run=False):
    console.print(f"[dim]> {' '.join(command)}[/dim]")
    if dry_run:
        return
    subprocess.run(command, check=True, cwd=cwd)


def has_staged_changes(folder):
    result = subprocess.run(
        ["git", "diff", "--cached", "--quiet"],
        cwd=folder,
        check=False,
    )
    return result.returncode != 0


def format_number(prefix, number):
    return f"{prefix}{int(number):02d}"


def create_repo_name(exercise, session, course):
    ex = format_number("ex", exercise)
    ss = format_number("ss", session)
    return f"{ex}-{ss}-{course.upper()}"


def slugify(value):
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip())
    slug = slug.strip("-._").lower()
    return slug or "homework"


def create_session_repo_name(file_path, session, course, naming=None):
    file_name = Path(file_path).stem
    if naming:
        try:
            return naming.format(
                name=slugify(file_name),
                file=slugify(file_name),
                session=int(session),
                course=course.upper(),
            )
        except (KeyError, ValueError):
            pass
    ss = format_number("ss", session)
    return f"{slugify(file_name)}-{ss}-{course.upper()}"


def create_repo_topics(session, course):
    return ["homework", slugify(course), format_number("ss", session)]


def detect_stack(folder):
    folder = Path(folder)
    names = {path.name.lower() for path in folder.iterdir()} if folder.exists() else set()

    if "package.json" in names:
        return "node"
    if any(name in names for name in {"requirements.txt", "pyproject.toml", "pipfile", "setup.py"}):
        return "python"
    if "index.html" in names:
        return "web"

    if folder.exists():
        for path in folder.rglob("*"):
            if not path.is_file():
                continue
            if path.suffix.lower() == ".py":
                try:
                    text = path.read_text(encoding="utf-8", errors="ignore")[:4000].lower()
                except OSError:
                    text = ""
                if "fastapi" in text or "from flask" in text or "import flask" in text:
                    return "python"
            if path.name.lower() == "package.json":
                return "node"

    py_count = 0
    html_count = 0
    if folder.exists():
        for path in folder.rglob("*"):
            if not path.is_file():
                continue
            if path.suffix.lower() == ".py":
                py_count += 1
            elif path.suffix.lower() in {".html", ".css", ".js"}:
                html_count += 1

    if py_count > 0 and py_count >= html_count:
        return "python"
    if html_count > 0:
        return "web"
    return "generic"


def is_project_folder(folder):
    folder = Path(folder)
    if not folder.is_dir():
        return False

    markers = {
        "package.json",
        "requirements.txt",
        "pyproject.toml",
        "pipfile",
        "setup.py",
        "cargo.toml",
        "go.mod",
        "composer.json",
        "pom.xml",
        "build.gradle",
        "dockerfile",
        "docker-compose.yml",
        "docker-compose.yaml",
        "manage.py",
        "main.py",
        "app.py",
        "index.html",
        "src",
        "app",
    }

    names = {path.name.lower() for path in folder.iterdir()}
    if names & markers:
        return True

    nested_files = list_uploadable_files(folder)
    return len(nested_files) >= 2


def create_readme(exercise, session, course, repo_name, folder=None, stack=None):
    folder = folder or Path.cwd()
    readme_path = folder / "README.md"

    if readme_path.exists():
        return

    stack = stack or detect_stack(folder)
    info_lines = []
    if exercise is not None:
        info_lines.append(f"- Exercise: EX{int(exercise):02d}")
    if session is not None:
        info_lines.append(f"- Session: SS{int(session):02d}")
    if course:
        info_lines.append(f"- Course: {course.upper()}")
    info_lines.append(f"- Stack: {stack}")

    info_section = "## Information\n\n" + "\n".join(info_lines) + "\n\n"
    course_label = course.upper() if course else "this course"

    if stack == "python":
        how_to_run = (
            "```bash\n"
            "python -m venv .venv\n"
            "source .venv/bin/activate  # Windows: .venv\\Scripts\\activate\n"
            "pip install -r requirements.txt  # if available\n"
            "uvicorn main:app --reload  # FastAPI example\n"
            "# or: python main.py\n"
            "```"
        )
    elif stack == "node":
        how_to_run = (
            "```bash\n"
            "npm install\n"
            "npm start\n"
            "```"
        )
    elif stack == "web":
        how_to_run = "Open `index.html` in your browser."
    else:
        how_to_run = "See project files for run instructions."

    content = (
        f"# {repo_name}\n\n"
        f"{info_section}"
        "## Description\n\n"
        f"Homework submission for {course_label}.\n\n"
        "## How to run\n\n"
        f"{how_to_run}\n"
    )

    readme_path.write_text(content, encoding="utf-8")
    console.print(f"[green]Created README.md[/green] [dim]({stack})[/dim]")


def get_history_file():
    return get_app_dir() / "history.json"


def save_history(exercise, session, course, repo_name, repo_url):
    history_file = get_history_file()

    if history_file.exists():
        history = json.loads(history_file.read_text(encoding="utf-8"))
    else:
        history = []

    exercise_label = f"EX{int(exercise):02d}" if exercise is not None else "-"
    session_label = f"SS{int(session):02d}" if session is not None else "-"
    course_label = course.upper() if course else "-"

    item = {
        "exercise": exercise_label,
        "session": session_label,
        "course": course_label,
        "repo_name": repo_name,
        "repo_url": repo_url,
        "submitted_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }

    history.append(item)
    history_file.write_text(
        json.dumps(history, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    console.print("[green]Saved to history[/green]")


def show_history(course=None, session=None, limit=None):
    history_file = get_history_file()

    if not history_file.exists():
        console.print("[yellow]No submission history yet.[/yellow]")
        return

    history = json.loads(history_file.read_text(encoding="utf-8"))
    if not history:
        console.print("[yellow]No submission history yet.[/yellow]")
        return

    filtered = history
    if course:
        course_label = course.upper()
        filtered = [item for item in filtered if item.get("course", "").upper() == course_label]
    if session is not None:
        session_label = f"SS{int(session):02d}"
        filtered = [item for item in filtered if item.get("session") == session_label]

    if limit is not None:
        filtered = filtered[-int(limit) :]

    if not filtered:
        console.print("[yellow]No matching history entries.[/yellow]")
        return

    table = Table(title="Submitted repositories")
    table.add_column("No", justify="right")
    table.add_column("Exercise")
    table.add_column("Session")
    table.add_column("Course")
    table.add_column("Repository")
    table.add_column("Link")
    table.add_column("Time")

    for index, item in enumerate(filtered, start=1):
        table.add_row(
            str(index),
            item["exercise"],
            item["session"],
            item["course"],
            item["repo_name"],
            item["repo_url"],
            item["submitted_at"],
        )

    console.print(table)


def get_last_history_repo():
    history_file = get_history_file()
    if not history_file.exists():
        return None
    try:
        history = json.loads(history_file.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    return history[-1] if history else None


def delete_repo(repo_name, yes=False):
    repo_name = str(repo_name).strip()
    if not repo_name:
        console.print("[red]Repository name is required.[/red]")
        return

    if not shutil.which("gh"):
        console.print("[red]GitHub CLI (gh) not found.[/red]")
        return

    try:
        username = get_github_username()
    except (subprocess.CalledProcessError, FileNotFoundError):
        console.print("[red]Chua dang nhap GitHub CLI. Chay: gh auth login[/red]")
        return

    full_name = f"{username}/{repo_name}"
    if not github_repo_exists(username, repo_name):
        console.print(f"[yellow]Repository not found:[/yellow] {full_name}")
        return

    if not ask_yes_no(f"Xoa vinh vien repo {full_name}? Hanh dong nay khong the hoan tac", yes=yes):
        console.print("[yellow]Skipped.[/yellow]")
        return

    result = subprocess.run(["gh", "repo", "delete", full_name, "--yes"], check=False)
    if result.returncode == 0:
        console.print(f"[green]Deleted[/green] {full_name}")
    else:
        console.print(f"[red]Failed to delete[/red] {full_name}")


def open_repo(repo_name=None):
    if not shutil.which("gh"):
        console.print("[red]GitHub CLI (gh) not found.[/red]")
        return

    if not repo_name:
        last = get_last_history_repo()
        if not last:
            console.print("[yellow]No submission history yet. Provide a repo name.[/yellow]")
            return
        repo_name = last["repo_name"]

    subprocess.run(["gh", "repo", "view", repo_name, "--web"], check=False)


def should_ignore_name(name):
    if name.startswith("."):
        if name in {".gitignore", ".env", ".env.local"}:
            return name != ".gitignore"
        if name.startswith(".env"):
            return True
        return True

    if name in ALWAYS_IGNORE_NAMES or name in IGNORED_BATCH_FOLDERS:
        return True

    lower = name.lower()
    if lower in ALWAYS_IGNORE_NAMES or lower in IGNORED_BATCH_FOLDERS:
        return True

    for suffix in ALWAYS_IGNORE_SUFFIXES:
        if lower.endswith(suffix):
            return True

    return False


def ensure_gitignore(folder, stack=None, force_write=False):
    folder = Path(folder)
    gitignore_path = folder / ".gitignore"
    stack = stack or detect_stack(folder)
    content = STACK_GITIGNORES.get(stack, STACK_GITIGNORES["generic"])

    if gitignore_path.exists() and not force_write:
        existing = gitignore_path.read_text(encoding="utf-8")
        required = [".env", "node_modules/", "__pycache__/", ".venv/"]
        missing = [item for item in required if item not in existing]
        if missing:
            with gitignore_path.open("a", encoding="utf-8") as handle:
                handle.write("\n# homework-repo-tool safety\n")
                for item in missing:
                    handle.write(f"{item}\n")
            console.print("[green]Updated .gitignore[/green] with safety rules")
        return

    gitignore_path.write_text(content, encoding="utf-8")
    console.print(f"[green]Created .gitignore[/green] [dim]({stack})[/dim]")


def get_github_username():
    return subprocess.check_output(
        ["gh", "api", "user", "--jq", ".login"],
        text=True,
    ).strip()


def ensure_github_auth():
    result = subprocess.run(
        ["gh", "auth", "status"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    if result.returncode != 0:
        raise SubmissionSkipped(
            "GitHub CLI chua dang nhap. Chay 'gh auth login' roi thu lai (hoac 'hw doctor')."
        )


def scan_for_secrets(folder):
    findings = []
    for path in list_uploadable_files(folder):
        if path.suffix.lower() in SECRET_SCAN_SKIP_SUFFIXES:
            continue
        try:
            if path.stat().st_size > 2 * 1024 * 1024:
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for pattern, label in SECRET_PATTERNS:
            if pattern.search(text):
                findings.append((path, label))
                break
    return findings


def scan_for_large_files(folder):
    large = []
    for path in list_uploadable_files(folder):
        try:
            size = path.stat().st_size
        except OSError:
            continue
        if size > MAX_FILE_SIZE_WARN_BYTES:
            large.append((path, size))
    return large


def check_before_commit(folder, yes=False):
    secrets = scan_for_secrets(folder)
    if secrets:
        console.print("[red]Canh bao: phat hien noi dung giong secret/API key:[/red]")
        for path, label in secrets:
            console.print(f"  - {path.name}: {label}")
        if not ask_yes_no("Van tiep tuc nop bai?", yes=yes):
            raise SubmissionSkipped("Da dung lai vi phat hien secret/API key trong file.")

    large_files = scan_for_large_files(folder)
    if large_files:
        console.print("[yellow]Canh bao: co file lon (>50MB), GitHub co the tu choi push:[/yellow]")
        for path, size in large_files:
            console.print(f"  - {path.name}: {size / (1024 * 1024):.1f} MB")
        if not ask_yes_no("Van tiep tuc nop bai?", yes=yes):
            raise SubmissionSkipped("Da dung lai vi co file qua lon.")


def github_repo_exists(username, repo_name):
    result = subprocess.run(
        ["gh", "repo", "view", f"{username}/{repo_name}"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return result.returncode == 0


def set_git_remote(folder, repo_url, dry_run=False):
    result = subprocess.run(
        ["git", "remote", "get-url", "origin"],
        cwd=folder,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )

    if result.returncode == 0:
        run(["git", "remote", "set-url", "origin", repo_url], cwd=folder, dry_run=dry_run)
    else:
        run(["git", "remote", "add", "origin", repo_url], cwd=folder, dry_run=dry_run)


def add_repo_topics(username, repo_name, session, course, dry_run=False):
    if session is None or not course:
        return

    topics = create_repo_topics(session, course)
    command = ["gh", "repo", "edit", f"{username}/{repo_name}"]

    for topic in topics:
        command.extend(["--add-topic", topic])

    if dry_run:
        console.print(f"[dim]> {' '.join(command)}[/dim]")
        return

    result = subprocess.run(command, check=False)
    if result.returncode == 0:
        console.print(f"[green]Added topics:[/green] {', '.join(topics)}")
    else:
        console.print("[yellow]Could not add repository topics. Continuing...[/yellow]")


def push_to_github(
    folder,
    repo_name,
    visibility,
    session,
    course,
    overwrite=False,
    yes=False,
    dry_run=False,
    force_push=False,
):
    if dry_run:
        username = "your-username"
        try:
            username = get_github_username()
        except (subprocess.CalledProcessError, FileNotFoundError):
            pass
        repo_url = f"https://github.com/{username}/{repo_name}"
        console.print(f"[cyan]DRY-RUN[/cyan] would push to {repo_url}")
        return repo_url

    username = get_github_username()
    repo_url = f"https://github.com/{username}/{repo_name}"
    git_url = f"{repo_url}.git"

    force_confirmed = overwrite or force_push
    if github_repo_exists(username, repo_name):
        if not force_confirmed:
            if not ask_yes_no(
                f"Repository {repo_name} already exists. Overwrite with force push?",
                yes=yes,
            ):
                raise SubmissionSkipped(f"Skipped existing repository: {repo_name}")
        console.print("[yellow]Repository already exists. Force pushing latest files...[/yellow]")
        use_force = True
    else:
        visibility_flag = "--public" if visibility == "public" else "--private"
        run(["gh", "repo", "create", repo_name, visibility_flag], cwd=folder, dry_run=dry_run)
        use_force = False

    run(["git", "branch", "-M", "main"], cwd=folder, dry_run=dry_run)
    set_git_remote(folder, git_url, dry_run=dry_run)

    push_command = ["git", "push", "-u", "origin", "main"]
    if use_force or force_push or overwrite:
        push_command.append("--force")

    run(push_command, cwd=folder, dry_run=dry_run)
    add_repo_topics(username, repo_name, session, course, dry_run=dry_run)
    return repo_url


def copy_tree_safe(source_folder, dest_folder):
    source_folder = Path(source_folder).resolve()
    dest_folder = Path(dest_folder)
    dest_folder.mkdir(parents=True, exist_ok=True)

    for root, dirs, filenames in os.walk(source_folder):
        root_path = Path(root)
        dirs[:] = [name for name in dirs if not should_ignore_name(name)]

        relative_root = root_path.relative_to(source_folder)
        target_root = dest_folder / relative_root
        target_root.mkdir(parents=True, exist_ok=True)

        for filename in filenames:
            if should_ignore_name(filename):
                continue
            if filename in IGNORED_SESSION_FILES:
                # keep user README/gitignore from source if present
                pass
            source_file = root_path / filename
            if source_file.is_symlink():
                continue
            shutil.copy2(source_file, target_root / filename)


def submit_folder(
    folder,
    exercise,
    session,
    course,
    visibility,
    repo_name=None,
    include_support_files=True,
    overwrite=False,
    yes=False,
    dry_run=False,
    force_push=False,
    auto_gitignore=True,
):
    folder = folder.resolve()
    repo_name = repo_name or create_repo_name(exercise, session, course)
    stack = detect_stack(folder)

    console.print(f"[bold]Repo name:[/bold] {repo_name}")
    console.print(f"[bold]Folder:[/bold] {folder}")
    console.print(f"[bold]Stack:[/bold] {stack}")

    if dry_run:
        console.print("[cyan]DRY-RUN mode: no git/github changes will be made.[/cyan]")
    else:
        ensure_github_auth()
        check_before_commit(folder, yes=yes)

    if include_support_files:
        if not dry_run:
            create_readme(exercise, session, course, repo_name, folder, stack=stack)
        else:
            console.print("[dim]> would create README.md if missing[/dim]")

    if not (folder / ".git").exists():
        run(["git", "init"], cwd=folder, dry_run=dry_run)

    if include_support_files or auto_gitignore:
        if not dry_run:
            ensure_gitignore(folder, stack=stack)
        else:
            console.print(f"[dim]> would ensure .gitignore ({stack})[/dim]")

    run(["git", "add", "."], cwd=folder, dry_run=dry_run)

    if dry_run:
        console.print("[dim]> would commit if there are staged changes[/dim]")
    elif has_staged_changes(folder):
        run(["git", "commit", "-m", f"Submit {repo_name}"], cwd=folder, dry_run=dry_run)
    else:
        console.print("[yellow]No new changes to commit. Continuing...[/yellow]")

    repo_url = push_to_github(
        folder,
        repo_name,
        visibility,
        session,
        course,
        overwrite=overwrite,
        yes=yes,
        dry_run=dry_run,
        force_push=force_push,
    )

    if not dry_run:
        save_history(exercise, session, course, repo_name, repo_url)

    console.print()
    title = "DRY-RUN. Would submit this link" if dry_run else "Done. Submit this link"
    console.print(Panel(repo_url, title=title, border_style="green"))

    return {
        "exercise": f"EX{int(exercise):02d}" if exercise is not None else "-",
        "file": None,
        "repo_name": repo_name,
        "repo_url": repo_url,
        "stack": stack,
    }


def warn_if_repo_name_invalid(repo_name):
    if re.fullmatch(r"[A-Za-z0-9._-]+", repo_name):
        return

    console.print(
        "[yellow]Warning:[/yellow] Repo name may be invalid on GitHub. "
        "Use only letters, numbers, dot (.), underscore (_), hyphen (-)."
    )


def up(repo_name, visibility, **options):
    repo_name = str(repo_name).strip()

    if not repo_name:
        console.print("[red]Repository name is required.[/red]")
        return

    warn_if_repo_name_invalid(repo_name)

    source_files = find_session_files(Path.cwd())
    if not source_files:
        console.print("[yellow]No homework files found in the current folder.[/yellow]")
        return

    console.print(
        Panel(
            f"[bold]{repo_name}[/bold]  ·  {len(source_files)} file(s)",
            title="Up",
            border_style="cyan",
        )
    )

    if options.get("dry_run"):
        console.print("[cyan]DRY-RUN:[/cyan] listing only, no push.")
        return
    if not ask_yes_no("Push ngay?", yes=options.get("yes", False)):
        console.print("[yellow]Skipped.[/yellow]")
        return

    temp_path = Path(tempfile.mkdtemp())
    folder = temp_path / repo_name
    folder.mkdir()

    try:
        for source_file in source_files:
            if should_ignore_name(source_file.name):
                continue
            shutil.copy2(source_file, folder / source_file.name)

        submit_folder(
            folder,
            exercise=None,
            session=None,
            course=None,
            visibility=visibility,
            repo_name=repo_name,
            include_support_files=False,
            auto_gitignore=True,
            **options,
        )
    except SubmissionSkipped as error:
        print(error)
    except subprocess.CalledProcessError as error:
        console.print(f"[red]Failed to submit {repo_name}: {error}[/red]")
    finally:
        shutil.rmtree(temp_path, ignore_errors=True)


def find_session_entries(folder):
    entries = []

    for path in folder.iterdir():
        if path.name.startswith("."):
            continue

        if path.is_file():
            if path.name in IGNORED_SESSION_FILES:
                continue
            if path.name.startswith("submission-links-") and path.suffix == ".md":
                continue
            if should_ignore_name(path.name):
                continue
            entries.append(path)
            continue

        if path.is_dir():
            if path.name in IGNORED_BATCH_FOLDERS or should_ignore_name(path.name):
                continue
            entries.append(path)

    return sorted(
        entries,
        key=lambda path: (get_exercise_from_file_name(path) or 999999, path.name.lower()),
    )


def list_uploadable_files(folder):
    folder = folder.resolve()
    files = []

    for root, dirs, filenames in os.walk(folder):
        dirs[:] = [name for name in dirs if not should_ignore_name(name)]
        root_path = Path(root)
        for filename in filenames:
            if should_ignore_name(filename):
                continue
            files.append(root_path / filename)

    return sorted(files, key=lambda path: str(path.relative_to(folder)).lower())


def copy_folder_to_temp_folder(source_folder, repo_name):
    temp_path = Path(tempfile.mkdtemp())
    repo_folder = temp_path / repo_name
    copy_tree_safe(source_folder, repo_folder)
    return temp_path, repo_folder


def apply_name_affixes(base_name, prefix=None, suffix=None):
    name = base_name
    if prefix:
        name = f"{slugify(prefix)}-{name}"
    if suffix:
        name = f"{name}-{slugify(suffix)}"
    return name


def build_up_session_items(folder, prefix=None, suffix=None):
    entries = find_session_entries(folder)
    items = []

    for entry in entries:
        if entry.is_file():
            repo_name = apply_name_affixes(slugify(entry.stem), prefix, suffix)
            items.append(
                {
                    "path": entry,
                    "type": "file",
                    "mode": "file",
                    "repo_name": repo_name,
                    "stack": "generic",
                }
            )
        elif entry.is_dir():
            repo_name = apply_name_affixes(slugify(entry.name), prefix, suffix)
            mode = "project" if is_project_folder(entry) else "folder"
            items.append(
                {
                    "path": entry,
                    "type": "folder",
                    "mode": mode,
                    "repo_name": repo_name,
                    "stack": detect_stack(entry),
                }
            )

    used_repo_names = set()
    for item in items:
        base_name = item["repo_name"]
        repo_name = base_name
        dedup_counter = 2
        while repo_name in used_repo_names:
            repo_name = f"{base_name}-{dedup_counter}"
            dedup_counter += 1
        item["repo_name"] = repo_name
        used_repo_names.add(repo_name)

    return items


def print_up_session_items(items):
    file_count = sum(1 for item in items if item["type"] == "file")
    folder_count = sum(1 for item in items if item["type"] == "folder")

    table = Table(
        title=f"Found {len(items)} item(s) ({file_count} file(s), {folder_count} folder(s))"
    )
    table.add_column("No", justify="right", style="cyan")
    table.add_column("Type", style="magenta")
    table.add_column("Mode", style="yellow")
    table.add_column("Item", style="bold")
    table.add_column("Stack", style="blue")
    table.add_column("Repository", style="green")

    for index, item in enumerate(items, start=1):
        entry = item["path"]
        table.add_row(
            str(index),
            item["type"],
            item.get("mode", "-"),
            entry.name,
            item.get("stack", "-"),
            item["repo_name"],
        )

    console.print(table)


def up_split_folder(folder_name, visibility, prefix=None, suffix=None, **options):
    folder_name = str(folder_name).strip()
    if not folder_name:
        console.print("[red]Folder name is required.[/red]")
        return

    if folder_name == ".":
        source_folder = Path.cwd().resolve()
    else:
        source_folder = (Path.cwd() / folder_name).resolve()

    if not source_folder.exists():
        console.print(f"[red]Folder not found:[/red] {source_folder}")
        return
    if not source_folder.is_dir():
        console.print(f"[red]Not a folder:[/red] {source_folder}")
        return

    items = build_up_session_items(source_folder, prefix=prefix, suffix=suffix)

    if not items:
        console.print("[yellow]No homework files/folders found in the selected folder.[/yellow]")
        return

    print_up_session_items(items)

    if options.get("dry_run"):
        console.print("[cyan]DRY-RUN:[/cyan] plan only.")
        return

    if not ask_yes_no("Push ngay?", yes=options.get("yes", False)):
        console.print("[yellow]Skipped.[/yellow]")
        return

    submitted = []
    failed = []

    for item in items:
        source_path = item["path"]
        repo_name = item["repo_name"]

        console.rule(f"Submitting {source_path.name}")

        try:
            if source_path.is_file():
                temp_path, folder = copy_file_to_temp_folder(source_path, repo_name)
            else:
                temp_path, folder = copy_folder_to_temp_folder(source_path, repo_name)

            try:
                result = submit_folder(
                    folder,
                    exercise=None,
                    session=None,
                    course=None,
                    visibility=visibility,
                    repo_name=repo_name,
                    include_support_files=False,
                    auto_gitignore=True,
                    **options,
                )
                result["file"] = source_path.name
                submitted.append(result)
            finally:
                shutil.rmtree(temp_path, ignore_errors=True)
        except SubmissionSkipped as error:
            failed.append({"file": source_path.name, "reason": str(error)})
            console.print(f"[yellow]{error}[/yellow]")
        except subprocess.CalledProcessError as error:
            failed.append({"file": source_path.name, "reason": str(error)})
            console.print(f"[red]Failed to submit {source_path.name}. Continuing...[/red]")

        console.print()

    print_submission_summary(submitted, failed)


def up_single_repo_folder(folder_name, visibility, repo_name=None, **options):
    folder_name = str(folder_name).strip()
    if not folder_name:
        console.print("[red]Folder name is required.[/red]")
        return

    if folder_name == ".":
        source_folder = Path.cwd().resolve()
    else:
        source_folder = (Path.cwd() / folder_name).resolve()

    if not source_folder.exists():
        console.print(f"[red]Folder not found:[/red] {source_folder}")
        return
    if not source_folder.is_dir():
        console.print(f"[red]Not a folder:[/red] {source_folder}")
        return

    repo_name = (repo_name or source_folder.name).strip()
    warn_if_repo_name_invalid(repo_name)

    source_files = list_uploadable_files(source_folder)
    if not source_files:
        console.print("[yellow]No homework files found in the selected folder.[/yellow]")
        return

    stack = detect_stack(source_folder)
    mode = "project" if is_project_folder(source_folder) else "folder"
    console.print(
        Panel(
            f"[bold]{repo_name}[/bold]  ·  {stack}  ·  {mode}  ·  {len(source_files)} file(s)",
            title="Nop folder",
            border_style="cyan",
        )
    )

    if options.get("dry_run"):
        console.print("[cyan]DRY-RUN:[/cyan] listing only.")
        return

    if not ask_yes_no("Push ngay?", yes=options.get("yes", False)):
        console.print("[yellow]Skipped.[/yellow]")
        return

    temp_path, folder = copy_folder_to_temp_folder(source_folder, repo_name)

    try:
        submit_folder(
            folder,
            exercise=None,
            session=None,
            course=None,
            visibility=visibility,
            repo_name=repo_name,
            include_support_files=False,
            auto_gitignore=True,
            **options,
        )
    except SubmissionSkipped as error:
        print(error)
    except subprocess.CalledProcessError as error:
        console.print(f"[red]Failed to submit {repo_name}: {error}[/red]")
    finally:
        shutil.rmtree(temp_path, ignore_errors=True)


def up_session(folder_name, visibility, repo_name=None, **options):
    up_single_repo_folder(folder_name, visibility, repo_name=repo_name, **options)


def up_folder(folder_name, visibility, prefix=None, suffix=None, **options):
    up_split_folder(folder_name, visibility, prefix=prefix, suffix=suffix, **options)


def up_project(folder_name, visibility, repo_name=None, **options):
    folder_name = str(folder_name).strip() or "."
    if folder_name == ".":
        source_folder = Path.cwd().resolve()
    else:
        source_folder = (Path.cwd() / folder_name).resolve()

    if not source_folder.exists():
        console.print(f"[red]Folder not found:[/red] {source_folder}")
        return
    if not source_folder.is_dir():
        console.print(f"[red]Not a folder:[/red] {source_folder}")
        return

    repo_name = (repo_name or source_folder.name).strip()
    warn_if_repo_name_invalid(repo_name)

    if not is_project_folder(source_folder):
        console.print(
            "[yellow]Warning:[/yellow] Folder does not look like a full project. "
            "Continuing as one repository anyway."
        )

    source_files = list_uploadable_files(source_folder)
    if not source_files:
        console.print("[yellow]No homework files found in the selected folder.[/yellow]")
        return

    stack = detect_stack(source_folder)
    console.print(
        Panel(
            f"[bold]{repo_name}[/bold]  ·  {stack}  ·  {len(source_files)} file(s)",
            title="Nop project",
            border_style="cyan",
        )
    )

    if options.get("dry_run"):
        console.print("[cyan]DRY-RUN:[/cyan] no push.")
        return

    if not ask_yes_no("Push ngay?", yes=options.get("yes", False)):
        console.print("[yellow]Skipped.[/yellow]")
        return

    temp_path, folder = copy_folder_to_temp_folder(source_folder, repo_name)
    try:
        submit_folder(
            folder,
            exercise=None,
            session=None,
            course=None,
            visibility=visibility,
            repo_name=repo_name,
            include_support_files=options.get("include_support_files", True),
            auto_gitignore=True,
            overwrite=options.get("overwrite", False),
            yes=options.get("yes", False),
            dry_run=False,
            force_push=options.get("force_push", False),
        )
    except SubmissionSkipped as error:
        print(error)
    except subprocess.CalledProcessError as error:
        console.print(f"[red]Failed to submit {repo_name}: {error}[/red]")
    finally:
        shutil.rmtree(temp_path, ignore_errors=True)


def plan_folder(folder_name="."):
    if folder_name == ".":
        source_folder = Path.cwd().resolve()
    else:
        source_folder = (Path.cwd() / folder_name).resolve()

    if not source_folder.exists() or not source_folder.is_dir():
        console.print(f"[red]Folder not found:[/red] {source_folder}")
        return

    items = build_up_session_items(source_folder)
    if not items:
        console.print("[yellow]No homework files/folders found.[/yellow]")
        return

    console.print(Panel(
        f"Plan for: {source_folder}\n"
        "file     = one file -> one repo\n"
        "folder   = folder without project markers -> one repo\n"
        "project  = multi-file app (FastAPI/Node/web) -> one repo",
        title="Submission plan",
        border_style="cyan",
    ))
    print_up_session_items(items)

    console.print()
    console.print("[bold]Suggested commands:[/bold]")
    console.print(f"  hw up-folder {folder_name}          # each item -> own repo")
    console.print(f"  hw up-session {folder_name}         # whole folder -> 1 repo")
    console.print(f"  hw up-project <project-folder>    # one project safely")


def copy_file_to_temp_folder(source_file, repo_name):
    temp_path = Path(tempfile.mkdtemp())
    repo_folder = temp_path / repo_name
    repo_folder.mkdir()
    shutil.copy2(source_file, repo_folder / source_file.name)
    return temp_path, repo_folder


def submit_single_file(source_file, exercise, session, course, visibility, repo_name=None, **options):
    source_file = Path(source_file)

    if not source_file.exists():
        console.print(f"[red]File not found:[/red] {source_file}")
        return None

    if not source_file.is_file():
        console.print(f"[red]Not a file:[/red] {source_file}")
        return None

    repo_name = repo_name or create_repo_name(exercise, session, course)
    temp_path, folder = copy_file_to_temp_folder(source_file, repo_name)

    try:
        result = submit_folder(
            folder,
            exercise,
            session,
            course,
            visibility,
            repo_name,
            include_support_files=False,
            auto_gitignore=True,
            **options,
        )
    finally:
        shutil.rmtree(temp_path, ignore_errors=True)

    result["file"] = source_file.name
    return result


def submit_file(file_path, session, course, visibility, **options):
    source_file = Path.cwd() / file_path
    exercise = get_exercise_from_file_name(source_file) or 1
    config = load_config()
    repo_name = create_session_repo_name(
        source_file,
        session,
        course,
        naming=config.get("naming"),
    )

    try:
        submit_single_file(
            source_file,
            exercise,
            session,
            course,
            visibility,
            repo_name,
            **options,
        )
    except SubmissionSkipped as error:
        print(error)
    except subprocess.CalledProcessError as error:
        console.print(f"[red]Failed to submit {repo_name}: {error}[/red]")


def get_exercise_from_file_name(path):
    match = re.search(r"(\d+)", path.stem)
    if not match:
        return None
    return int(match.group(1))


def find_session_files(folder):
    files = []

    for path in folder.iterdir():
        if not path.is_file():
            continue
        if path.name.startswith("."):
            continue
        if path.name in IGNORED_SESSION_FILES:
            continue
        if path.name.startswith("submission-links-") and path.suffix == ".md":
            continue
        if should_ignore_name(path.name):
            continue
        files.append(path)

    return sorted(
        files,
        key=lambda path: (get_exercise_from_file_name(path) or 999999, path.name.lower()),
    )


def build_session_items(session, course):
    files = find_session_files(Path.cwd())
    items = []
    config = load_config()

    for index, source_file in enumerate(files, start=1):
        exercise = get_exercise_from_file_name(source_file) or index
        items.append(
            {
                "file": source_file,
                "exercise": exercise,
                "repo_name": create_session_repo_name(
                    source_file,
                    session,
                    course,
                    naming=config.get("naming"),
                ),
            }
        )

    return items


def ask_yes_no(question, yes=False):
    if yes:
        console.print(f"{question} [Y/N]: [green]Y[/green] [dim](--yes)[/dim]")
        return True

    try:
        answer = input(f"{question} [Y/N]: ").strip().lower()
    except EOFError:
        return False

    return answer in {"y", "yes"}


def print_session_items(items):
    table = Table(title=f"Found {len(items)} file(s)")
    table.add_column("No", justify="right", style="cyan")
    table.add_column("File", style="bold")
    table.add_column("Repository", style="green")

    for index, item in enumerate(items, start=1):
        table.add_row(str(index), item["file"].name, item["repo_name"])

    console.print(table)


def parse_number_selection(selection, max_number):
    numbers = []

    for part in selection.split(","):
        part = part.strip()
        if not part:
            continue
        if not part.isdigit():
            raise ValueError(f"Invalid number: {part}")

        number = int(part)
        if number < 1 or number > max_number:
            raise ValueError(f"Number out of range: {number}")
        if number not in numbers:
            numbers.append(number)

    if not numbers:
        raise ValueError("No files selected")

    return numbers


def choose_session_items(items, yes=False):
    print_session_items(items)

    if ask_yes_no("Submit all files?", yes=yes):
        return items

    if yes:
        return items

    selection = input("Enter file numbers to submit, separated by comma: ").strip()

    try:
        numbers = parse_number_selection(selection, len(items))
    except ValueError as error:
        console.print(f"[red]Invalid selection:[/red] {error}")
        return []

    selected = [items[number - 1] for number in numbers]

    console.print()
    console.print("[bold]Selected files:[/bold]")
    for item in selected:
        console.print(f"- {item['file'].name} [dim]->[/dim] [green]{item['repo_name']}[/green]")
    console.print()

    return selected


def session_preview(session, course, visibility, **options):
    items = build_session_items(session, course)

    if not items:
        console.print("[yellow]No homework files found in the current folder.[/yellow]")
        return

    print_session_items(items)

    if options.get("dry_run"):
        console.print("[cyan]DRY-RUN:[/cyan] preview only.")
        return

    if ask_yes_no("Do you want to push now?", yes=options.get("yes", False)):
        console.print()
        submit_session(session, course, visibility, **options)
    else:
        console.print("[yellow]Skipped.[/yellow] You can push later with:")
        console.print(f"[bold]hw submit-session {int(session)} {course.upper()}[/bold]")


def print_submission_summary(submitted, failed):
    if submitted:
        table = Table(title="Done. Submitted repositories")
        table.add_column("No", justify="right", style="cyan")
        table.add_column("File", style="bold")
        table.add_column("Repository", style="green")
        table.add_column("Link", style="blue")

        for index, item in enumerate(submitted, start=1):
            table.add_row(str(index), item["file"], item["repo_name"], item["repo_url"])

        console.print(table)
        console.print("[bold]Copy these links:[/bold]")
        console.print()
        for item in submitted:
            console.print(item["repo_url"])

    if failed:
        console.print()
        console.print("[red]Failed files:[/red]")
        for item in failed:
            console.print(f"- {item['file']}: {item['reason']}")


def write_submission_links(session, course, submitted):
    session_label = f"SS{int(session):02d}"
    output_path = Path.cwd() / f"submission-links-ss{int(session):02d}.md"

    lines = [
        f"# Submission Links - {session_label} - {course.upper()}",
        "",
        "| Exercise | File | Repository | Link |",
        "| -------- | ---- | ---------- | ---- |",
    ]

    for item in submitted:
        lines.append(
            f"| {item['exercise']} | {item['file']} | "
            f"{item['repo_name']} | {item['repo_url']} |"
        )

    output_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    console.print()
    console.print(f"[green]Created {output_path.name}[/green]")


def submit_session(session, course, visibility, **options):
    items = build_session_items(session, course)

    if not items:
        console.print("[yellow]No homework files found in the current folder.[/yellow]")
        return

    if options.get("dry_run"):
        print_session_items(items)
        console.print("[cyan]DRY-RUN:[/cyan] no push.")
        return

    items = choose_session_items(items, yes=options.get("yes", False))

    if not items:
        console.print("[yellow]No files submitted.[/yellow]")
        return

    submitted = []
    failed = []

    for item in items:
        source_file = item["file"]
        exercise = item["exercise"]
        repo_name = item["repo_name"]

        console.rule(f"Submitting {source_file.name}")

        try:
            result = submit_single_file(
                source_file,
                exercise,
                session,
                course,
                visibility,
                repo_name,
                **options,
            )
            if result:
                submitted.append(result)
            else:
                failed.append({"file": source_file.name, "reason": "Submit skipped"})
        except SubmissionSkipped as error:
            failed.append({"file": source_file.name, "reason": str(error)})
            console.print(f"[yellow]{error}[/yellow]")
        except subprocess.CalledProcessError as error:
            failed.append({"file": source_file.name, "reason": str(error)})
            console.print(f"[red]Failed to submit {source_file.name}. Continuing...[/red]")

        console.print()

    if submitted:
        write_submission_links(session, course, submitted)

    print_submission_summary(submitted, failed)


def get_command_output(command):
    result = subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )

    if result.returncode != 0:
        return None

    output = result.stdout.strip() or result.stderr.strip()
    return output.splitlines()[0] if output else "OK"


def print_check(label, ok, detail=None):
    status = "[green]OK[/green]" if ok else "[red]MISSING[/red]"
    detail_text = detail or ""
    return label, status, detail_text


def doctor():
    console.print(Panel("Homework Repo Tool doctor", border_style="cyan"))
    table = Table()
    table.add_column("Check", style="bold")
    table.add_column("Status")
    table.add_column("Detail")

    table.add_row(*print_check("Python", True, sys.version.split()[0]))

    git_path = shutil.which("git")
    git_version = get_command_output(["git", "--version"]) if git_path else None
    table.add_row(*print_check("Git", bool(git_path), git_version or "Install Git first."))

    gh_path = shutil.which("gh")
    gh_version = get_command_output(["gh", "--version"]) if gh_path else None
    table.add_row(
        *print_check("GitHub CLI", bool(gh_path), gh_version or "Install GitHub CLI first.")
    )

    if gh_path:
        auth_result = subprocess.run(
            ["gh", "auth", "status"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        table.add_row(
            *print_check(
                "GitHub login",
                auth_result.returncode == 0,
                "Run: gh auth login" if auth_result.returncode != 0 else None,
            )
        )
    else:
        table.add_row(*print_check("GitHub login", False, "Install GitHub CLI first."))

    pipx_path = shutil.which("pipx")
    pipx_version = get_command_output(["pipx", "--version"]) if pipx_path else None
    table.add_row(*print_check("pipx", bool(pipx_path), pipx_version or "Install pipx first."))

    config = load_config()
    table.add_row(*print_check("Config", True, str(get_config_file())))
    table.add_row(
        *print_check(
            "Default course",
            True,
            config.get("default_course") or "(not set)",
        )
    )
    console.print(table)


def guide():
    guide_text = """
Huong dan Homework Repo Tool

0. Menu de dung (khuyen dung):
   hw
   hw menu

1. Kiem tra may:
   hw doctor

2. Xem / set config:
   hw config
   hw config set default_course it205
   hw config set default_visibility public

3. Xem plan truoc khi nop (file / folder / project):
   hw plan
   hw plan ss05

4. Bai 1 file (session + mon):
   hw session-preview 5 it205
   hw submit-session 5 it205
   hw submit-file bai1.py 5 it205

5. Project nhieu file (FastAPI / web / Node):
   hw up-project todo-api
   hw up-project todo-api --name my-todo-ss05

6. Folder:
   hw up-session bai3-4          # ca folder = 1 repo
   hw up-folder ss05             # moi file/folder con = 1 repo
   hw up-folder ss05 --prefix it105-ss17 --suffix tkn  # -> it105-ss17-bai1-tkn
   hw up ten-repo-tuy-chon       # folder hien tai
   (Trong menu, chuc nang "Tach folder con" se hoi mon hoc / buoi / ten ban
    roi tu dong ghep ten repo dang MONHOC-ssBUOI-tenbai-tenban)

7. An toan:
   hw up-project app --dry-run   # chi xem, khong push
   hw submit-session 5 it205 --yes
   hw up-project app --overwrite # force push khi repo da ton tai
   Tool se canh bao neu phat hien secret/API key hoac file qua lon truoc khi push.

8. Lich su:
   hw history
   hw history --course it205
   hw history --session 5 --limit 10

9. Xoa / mo repo:
   hw delete ten-repo    # xoa han repo tren GitHub (khong hoan tac duoc)
   hw open               # mo repo nop gan nhat tren trinh duyet
   hw open ten-repo       # mo repo cu the

10. Kiem tra tool con hoat dong dung khong:
   pytest                 # chay bo test cho cac ham dat ten / loc file / canh bao

Ghi chu:
- Go "hw" de mo menu chon so, khong can nho lenh
- Tool tu chan .env, venv, node_modules, __pycache__...
- Tu tao .gitignore theo stack (python/node/web)
- README theo stack (FastAPI/Node/HTML)
- Mac dinh KHONG force push; can --overwrite khi muon ghi de
- up-project: 1 project = 1 repo, hop mon FastAPI / web
- up-folder: dung --prefix/--suffix de moi lan nop khong bi trung ten repo
    """.strip()
    console.print(Panel(guide_text, title="Huong dan", border_style="cyan"))


def extract_common_options(args):
    return {
        "overwrite": bool(getattr(args, "overwrite", False)),
        "yes": bool(getattr(args, "yes", False)),
        "dry_run": bool(getattr(args, "dry_run", False)),
        "force_push": bool(getattr(args, "overwrite", False)),
    }


def add_common_flags(parser):
    parser.add_argument(
        "--visibility",
        choices=["public", "private"],
        default=None,
        help="Repo visibility (default: config or public)",
    )
    parser.add_argument(
        "--yes",
        "-y",
        action="store_true",
        help="Skip confirmation prompts",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview actions without pushing",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Force push if repository already exists",
    )


def resolve_visibility(args):
    if getattr(args, "visibility", None):
        return args.visibility
    config = load_config()
    return config.get("default_visibility") or "public"


def resolve_course(course):
    if course:
        return course
    config = load_config()
    return config.get("default_course") or ""


def prompt_text(label, default=None, required=True):
    if default:
        display = f"{label} [{default}]: "
    else:
        display = f"{label}: "

    try:
        value = input(display).strip()
    except EOFError:
        value = ""

    if not value and default is not None:
        value = str(default)

    if required and not value:
        console.print("[red]Gia tri bat buoc.[/red]")
        return None

    return value


def prompt_choice(label, choices, default=None):
    choices_text = "/".join(choices)
    if default:
        display = f"{label} ({choices_text}) [{default}]: "
    else:
        display = f"{label} ({choices_text}): "

    try:
        value = input(display).strip().lower()
    except EOFError:
        value = ""

    if not value and default is not None:
        return default

    if value not in choices:
        console.print(f"[red]Chon khong hop le. Dung: {choices_text}[/red]")
        return None

    return value


def prompt_yes_no_menu(label, default=True):
    default_label = "Y" if default else "N"
    try:
        value = input(f"{label} [Y/N] [{default_label}]: ").strip().lower()
    except EOFError:
        value = ""

    if not value:
        return default
    return value in {"y", "yes"}


def collect_menu_options():
    visibility = prompt_choice("Visibility", ["public", "private"], default="public")
    if visibility is None:
        return None

    overwrite = prompt_yes_no_menu("Cho phep force push neu repo da ton tai?", default=False)

    return {
        "visibility": visibility,
        "options": {
            "overwrite": overwrite,
            "yes": False,
            "dry_run": False,
            "force_push": overwrite,
        },
    }


def prompt_session_course():
    config = load_config()
    session = prompt_text("Session so (vd: 5)", required=True)
    if session is None:
        return None, None

    course = prompt_text(
        "Course (vd: it205)",
        default=config.get("default_course") or None,
        required=True,
    )
    if course is None:
        return None, None

    return session, course


def spin_coin(initials="TK", cycles=2, fps=10):
    """Hieu ung dong xu tron xoay, chu cai nguoi tao tool hien o giua."""
    if not HAS_RICH or Live is None:
        console.print(f"[bold yellow]{('(( ' + initials + ' ))')}[/bold yellow]")
        return

    # Vien dong xu la mot hinh oval CO DINH (khong bi bien thanh hinh chu nhat/vuong).
    # Hieu ung "xoay" duoc tao bang cach doi chu o giua (TK -> T -> | -> K -> TK)
    # va doi mau sac de tao cam giac anh sang phan chieu tren mat dong xu.
    top = "   .------.   "
    upper = "  /        \\  "
    lower = "  \\        /  "
    bottom = "   '------'   "
    inner_width = 8
    coin_width = len(top)

    frames_per_cycle = 16
    total_frames = frames_per_cycle * max(1, cycles)
    colors = ["gold1", "yellow1", "khaki1", "gold3"]
    first_letter = initials[:1] or "?"
    second_letter = initials[1:2] or first_letter

    try:
        with Live(console=console, refresh_per_second=fps, transient=True) as live:
            for frame in range(total_frames):
                phase = frame % frames_per_cycle
                cos_a = math.cos((2 * math.pi * phase) / frames_per_cycle)

                if abs(cos_a) > 0.66:
                    label = initials
                elif abs(cos_a) > 0.2:
                    label = first_letter if cos_a >= 0 else second_letter
                else:
                    label = "|"

                color = colors[frame % len(colors)]
                middle = f"|{label.center(inner_width)}|".center(coin_width)
                art = "\n".join([top, upper, middle, lower, bottom])
                live.update(Align.center(Text(art, style=f"bold {color}")))
                time.sleep(1 / fps)
    except KeyboardInterrupt:
        pass

    console.print("[dim]Tool duoc tao boi TK (Khang Nguyen)[/dim]", justify="center")


def clear_screen():
    command = "cls" if os.name == "nt" else "clear"
    try:
        os.system(command)
    except OSError:
        console.print("\n" * 3)


def suggest_repo_name(path=None):
    target = Path(path).resolve() if path else Path.cwd().resolve()
    if target.is_file():
        return slugify(target.stem)
    return slugify(target.name)


def _menu_sections():
    return [
        (
            "NOP BAI",
            [
                (
                    "1",
                    "Plan folder",
                    "Xem truoc folder hien tai (file / folder / project)",
                ),
                (
                    "2",
                    "Nop session",
                    "Moi file o goc -> 1 repo (ten tu dong: tenfile-ss..-MON)",
                ),
                (
                    "3",
                    "Nop 1 file",
                    "Chi nop 1 file (ten tu dong theo session + mon)",
                ),
                (
                    "4",
                    "Nop project",
                    "Folder hien tai = 1 project repo (Enter = ten folder)",
                ),
                (
                    "5",
                    "Nop ca folder",
                    "Folder hien tai = 1 repo (Enter = ten folder)",
                ),
                (
                    "6",
                    "Tach folder con",
                    "Moi file/folder con = 1 repo (nhap mon hoc/buoi/ten de tu dat ten)",
                ),
                (
                    "7",
                    "Up ten tuy chon",
                    "Folder hien tai; Enter = ten folder, hoac tu dat ten",
                ),
            ],
        ),
        (
            "HE THONG",
            [
                ("8", "Lich su", "Xem cac link repo da nop"),
                ("9", "Config", "default_course, visibility, naming"),
                ("10", "Doctor", "Kiem tra Git / gh / da login chua"),
                ("11", "Huong dan", "Cach dung bang tieng Viet"),
                ("12", "Mo repo", "Mo repo nop gan nhat (hoac ten tuy chon) tren trinh duyet"),
                ("13", "Xoa repo", "Xoa han mot repo GitHub da nop nham"),
                ("14", "Dong xu TK", "Xem hieu ung dong xu xoay - ten nguoi tao tool"),
                ("0", "Thoat", "Thoat menu"),
            ],
        ),
    ]


def print_menu():
    config = load_config()
    cwd = Path.cwd()
    course = config.get("default_course") or "(chua set)"
    visibility = config.get("default_visibility") or "public"
    short_cwd = str(cwd)
    if len(short_cwd) > 56:
        short_cwd = "..." + short_cwd[-53:]

    if HAS_RICH:
        title = Text("HOMEWORK REPO TOOL", style="bold white")
        info = Text()
        info.append("folder  ", style="dim")
        info.append(short_cwd, style="bold cyan")
        info.append("\n")
        info.append("course  ", style="dim")
        info.append(str(course), style="bold green")
        info.append("    visibility  ", style="dim")
        info.append(str(visibility), style="bold yellow")

        console.print()
        console.print(
            Panel(
                Align.center(Group(title, Text(""), info)),
                border_style="bright_cyan",
                box=box.DOUBLE,
                padding=(1, 2),
            )
        )

        for section_title, rows in _menu_sections():
            table = Table(
                box=box.ROUNDED,
                show_header=True,
                expand=True,
                border_style="cyan",
                title=f"[bold cyan]{section_title}[/bold cyan]",
                title_style="bold cyan",
                padding=(0, 1),
            )
            table.add_column("#", justify="center", style="bold bright_cyan", width=4)
            table.add_column("Chuc nang", style="bold white", min_width=18)
            table.add_column("Mo ta", style="dim")

            for number, name, desc in rows:
                if number == "0":
                    table.add_row(
                        f"[bold red]{number}[/bold red]",
                        f"[red]{name}[/red]",
                        f"[dim]{desc}[/dim]",
                    )
                else:
                    table.add_row(number, name, desc)

            console.print(table)

        console.print(
            Panel(
                "[dim]Nhap so roi Enter[/dim]  •  "
                "[cyan]vd: 4[/cyan] = nop project  •  "
                "[red]0[/red] = thoat",
                border_style="dim",
                box=box.SIMPLE,
            )
        )
        return

    # Fallback ASCII (khi khong co rich)
    console.print()
    console.print(
        Panel(
            f"HOMEWORK REPO TOOL\n"
            f"Folder: {short_cwd}\n"
            f"Course: {course}  |  Visibility: {visibility}",
            title="hw",
        )
    )
    for section_title, rows in _menu_sections():
        table = Table(title=section_title)
        table.add_column("#")
        table.add_column("Chuc nang")
        table.add_column("Mo ta")
        for number, name, desc in rows:
            table.add_row(number, name, desc)
        console.print(table)
    console.print("Nhap so roi Enter  |  0 = thoat")


def run_interactive_menu():
    clear_screen()
    spin_coin()
    while True:
        clear_screen()
        print_menu()
        choice = prompt_text("Chon so", required=True)
        if choice is None:
            continue

        choice = choice.strip()
        console.print()

        if choice in {"0", "q", "quit", "exit"}:
            console.print("[yellow]Bye.[/yellow]")
            return

        if choice == "1":
            plan_folder(".")

        elif choice == "2":
            session, course = prompt_session_course()
            if not session or not course:
                continue
            settings = collect_menu_options()
            if not settings:
                continue
            mode = prompt_choice(
                "Che do",
                ["preview", "submit"],
                default="preview",
            )
            if mode is None:
                continue
            if mode == "preview":
                session_preview(
                    session,
                    course,
                    settings["visibility"],
                    **settings["options"],
                )
            else:
                submit_session(
                    session,
                    course,
                    settings["visibility"],
                    **settings["options"],
                )

        elif choice == "3":
            file_name = prompt_text("Ten file (vd: bai1.py)", required=True)
            if not file_name:
                continue
            session, course = prompt_session_course()
            if not session or not course:
                continue
            settings = collect_menu_options()
            if not settings:
                continue
            suggested = create_session_repo_name(
                file_name,
                session,
                course,
                naming=load_config().get("naming"),
            )
            console.print(f"[dim]Ten repo tu dong:[/dim] [cyan]{suggested}[/cyan]")
            submit_file(
                file_name,
                session,
                course,
                settings["visibility"],
                **settings["options"],
            )

        elif choice == "4":
            suggested = suggest_repo_name()
            repo_name = prompt_text(
                "Ten repo (Enter = ten folder)",
                default=suggested,
                required=False,
            )
            settings = collect_menu_options()
            if not settings:
                continue
            no_readme = prompt_yes_no_menu("Khong tao README?", default=False)
            project_options = dict(settings["options"])
            project_options["include_support_files"] = not no_readme
            up_project(
                ".",
                settings["visibility"],
                repo_name=repo_name or suggested,
                **project_options,
            )

        elif choice == "5":
            suggested = suggest_repo_name()
            repo_name = prompt_text(
                "Ten repo (Enter = ten folder)",
                default=suggested,
                required=False,
            )
            settings = collect_menu_options()
            if not settings:
                continue
            up_session(
                ".",
                settings["visibility"],
                repo_name=repo_name or suggested,
                **settings["options"],
            )

        elif choice == "6":
            console.print(
                "[dim]Nhap thong tin de tu dong dat ten repo dang: "
                "MONHOC-ssBUOI-tenbai-tenban (vd: it105-ss17-bai1-tkn).[/dim]"
            )
            course = prompt_text("Ten mon hoc (vd: IT105, Enter = bo qua)", default="", required=False)
            session = prompt_text("So buoi / session (vd: 17, Enter = bo qua)", default="", required=False)
            student_name = prompt_text("Ten ban (vd: tkn, Enter = bo qua)", default="", required=False)

            prefix_parts = []
            if course:
                prefix_parts.append(course)
            if session:
                prefix_parts.append(f"ss{session}")
            prefix = "-".join(prefix_parts) or None
            suffix = student_name or None

            settings = collect_menu_options()
            if not settings:
                continue
            up_folder(
                ".",
                settings["visibility"],
                prefix=prefix,
                suffix=suffix,
                **settings["options"],
            )

        elif choice == "7":
            suggested = suggest_repo_name()
            repo_name = prompt_text(
                "Ten repo (Enter = ten folder hien tai)",
                default=suggested,
                required=True,
            )
            if not repo_name:
                continue
            settings = collect_menu_options()
            if not settings:
                continue
            up(repo_name, settings["visibility"], **settings["options"])

        elif choice == "8":
            config = load_config()
            course = prompt_text(
                "Loc course (Enter = tat ca)",
                default=config.get("default_course") or "",
                required=False,
            )
            session = prompt_text("Loc session (Enter = tat ca)", default="", required=False)
            limit = prompt_text("Limit (Enter = tat ca)", default="", required=False)
            show_history(
                course=course or None,
                session=session or None,
                limit=int(limit) if limit and limit.isdigit() else None,
            )

        elif choice == "9":
            show_config()
            if prompt_yes_no_menu("Muon set config?", default=False):
                key = prompt_text(
                    "Key (default_course/default_visibility/naming/...)",
                    required=True,
                )
                if not key:
                    continue
                value = prompt_text("Value", required=True)
                if value is not None:
                    set_config_value(key, value)

        elif choice == "10":
            doctor()

        elif choice == "11":
            guide()

        elif choice == "12":
            repo_name = prompt_text(
                "Ten repo (Enter = repo nop gan nhat)",
                default="",
                required=False,
            )
            open_repo(repo_name or None)

        elif choice == "13":
            repo_name = prompt_text("Ten repo can xoa", required=True)
            if not repo_name:
                continue
            delete_repo(repo_name)

        elif choice == "14":
            spin_coin()

        else:
            console.print("[red]Lua chon khong hop le.[/red]")

        console.print()
        if not prompt_yes_no_menu("Quay lai menu?", default=True):
            console.print("[yellow]Bye.[/yellow]")
            return


def main():
    parser = argparse.ArgumentParser(
        description="Tool nop bai len GitHub nhanh cho hoc sinh/sinh vien"
    )

    subparsers = parser.add_subparsers(dest="command")

    submit_file_parser = subparsers.add_parser(
        "submit-file",
        help="Nop mot file cu the thanh mot repo",
    )
    submit_file_parser.add_argument("file")
    submit_file_parser.add_argument("session")
    submit_file_parser.add_argument("course")
    add_common_flags(submit_file_parser)

    submit_session_parser = subparsers.add_parser(
        "submit-session",
        help="Quet folder hien tai va nop cac file bai tap",
    )
    submit_session_parser.add_argument("session")
    submit_session_parser.add_argument("course", nargs="?", default=None)
    add_common_flags(submit_session_parser)

    session_preview_parser = subparsers.add_parser(
        "session-preview",
        help="Xem truoc danh sach repo va hoi co push luon khong",
    )
    session_preview_parser.add_argument("session")
    session_preview_parser.add_argument("course", nargs="?", default=None)
    add_common_flags(session_preview_parser)

    up_parser = subparsers.add_parser(
        "up",
        help="Nop folder hien tai len GitHub voi ten repo tuy chon",
    )
    up_parser.add_argument(
        "repo_name",
        help="Ten repo dung y chang ten ban nhap (khong tu dong doi).",
    )
    add_common_flags(up_parser)

    up_session_parser = subparsers.add_parser(
        "up-session",
        help="Up nguyen mot folder thanh 1 repo",
    )
    up_session_parser.add_argument(
        "folder",
        nargs="?",
        default=".",
        help="Folder can up (mac dinh: folder hien tai).",
    )
    up_session_parser.add_argument(
        "--name",
        dest="repo_name",
        default=None,
        help="Ten repo (mac dinh: ten folder).",
    )
    add_common_flags(up_session_parser)

    up_folder_parser = subparsers.add_parser(
        "up-folder",
        help="Quet folder va up moi file/folder con thanh repo rieng",
    )
    up_folder_parser.add_argument(
        "folder",
        nargs="?",
        default=".",
        help="Folder can quet (mac dinh: folder hien tai).",
    )
    up_folder_parser.add_argument(
        "--prefix",
        default=None,
        help="Them tien to vao ten moi repo con (vd: ss05 -> ss05-bai1).",
    )
    up_folder_parser.add_argument(
        "--suffix",
        default=None,
        help="Them hau to vao ten moi repo con (vd: it205 -> bai1-it205).",
    )
    add_common_flags(up_folder_parser)

    up_project_parser = subparsers.add_parser(
        "up-project",
        help="Nop 1 project (FastAPI/web/Node) thanh 1 repo an toan",
    )
    up_project_parser.add_argument(
        "folder",
        nargs="?",
        default=".",
        help="Folder project (mac dinh: folder hien tai).",
    )
    up_project_parser.add_argument(
        "--name",
        dest="repo_name",
        default=None,
        help="Ten repo tuy chon (mac dinh = ten folder).",
    )
    up_project_parser.add_argument(
        "--no-readme",
        action="store_true",
        help="Khong tao README.md",
    )
    add_common_flags(up_project_parser)

    plan_parser = subparsers.add_parser(
        "plan",
        help="Xem plan nop: file / folder / project",
    )
    plan_parser.add_argument(
        "folder",
        nargs="?",
        default=".",
        help="Folder can xem plan (mac dinh: folder hien tai).",
    )

    history_parser = subparsers.add_parser("history", help="Xem lai cac link da nop")
    history_parser.add_argument("--course", default=None, help="Loc theo mon")
    history_parser.add_argument("--session", default=None, help="Loc theo session")
    history_parser.add_argument("--limit", type=int, default=None, help="Chi lay N ban ghi moi nhat")

    config_parser = subparsers.add_parser("config", help="Xem / chinh config")
    config_sub = config_parser.add_subparsers(dest="config_command")
    config_set = config_sub.add_parser("set", help="Set mot gia tri config")
    config_set.add_argument("key")
    config_set.add_argument("value")

    subparsers.add_parser("doctor", help="Kiem tra Git, GitHub CLI, login, pipx")
    subparsers.add_parser("guide", help="Huong dan su dung bang tieng Viet")
    subparsers.add_parser("menu", help="Mo menu tuong tac")

    delete_parser = subparsers.add_parser(
        "delete",
        help="Xoa mot repo GitHub da nop nham",
    )
    delete_parser.add_argument("repo_name", help="Ten repo can xoa (vd: bai1-ss05-it205)")
    delete_parser.add_argument("--yes", "-y", action="store_true", help="Khong hoi xac nhan")

    open_parser = subparsers.add_parser(
        "open",
        help="Mo repo tren trinh duyet (mac dinh: repo nop gan nhat)",
    )
    open_parser.add_argument("repo_name", nargs="?", default=None, help="Ten repo can mo")

    args = parser.parse_args()
    options = extract_common_options(args)

    if args.command is None or args.command == "menu":
        run_interactive_menu()
        return

    if args.command == "submit-file":
        course = resolve_course(args.course)
        if not course:
            console.print("[red]Course is required (or set default_course in config).[/red]")
            return
        submit_file(args.file, args.session, course, resolve_visibility(args), **options)
    elif args.command == "submit-session":
        course = resolve_course(args.course)
        if not course:
            console.print("[red]Course is required (or set default_course in config).[/red]")
            return
        submit_session(args.session, course, resolve_visibility(args), **options)
    elif args.command == "session-preview":
        course = resolve_course(args.course)
        if not course:
            console.print("[red]Course is required (or set default_course in config).[/red]")
            return
        session_preview(args.session, course, resolve_visibility(args), **options)
    elif args.command == "up":
        up(args.repo_name, resolve_visibility(args), **options)
    elif args.command == "up-session":
        up_session(
            args.folder,
            resolve_visibility(args),
            repo_name=getattr(args, "repo_name", None),
            **options,
        )
    elif args.command == "up-folder":
        up_folder(
            args.folder,
            resolve_visibility(args),
            prefix=args.prefix,
            suffix=args.suffix,
            **options,
        )
    elif args.command == "up-project":
        project_options = dict(options)
        project_options["include_support_files"] = not args.no_readme
        up_project(
            args.folder,
            resolve_visibility(args),
            repo_name=args.repo_name,
            **project_options,
        )
    elif args.command == "plan":
        plan_folder(args.folder)
    elif args.command == "history":
        show_history(course=args.course, session=args.session, limit=args.limit)
    elif args.command == "config":
        if args.config_command == "set":
            set_config_value(args.key, args.value)
        else:
            show_config()
    elif args.command == "doctor":
        doctor()
    elif args.command == "guide":
        guide()
    elif args.command == "delete":
        delete_repo(args.repo_name, yes=args.yes)
    elif args.command == "open":
        open_repo(args.repo_name)
    else:
        run_interactive_menu()


if __name__ == "__main__":
    main()
