from __future__ import annotations

import os
import secrets
from pathlib import Path


def main() -> None:
    path = Path(
        os.environ.get(
            "TRADINGLAB_KEYS_FILE",
            Path.home() / ".config" / "tradinglab" / "tradinglab.keys",
        )
    )
    if not path.is_file():
        raise SystemExit(f"secret file not found: {path}")
    if path.stat().st_mode & 0o077:
        raise SystemExit(f"secret file permissions must be 600: {path}")

    variable_name = "_".join(("TRADINGLAB", "API", "TOKEN"))
    assignment_prefix = variable_name + "="
    lines = path.read_text(encoding="utf-8").splitlines()
    for line in lines:
        stripped = line.strip()
        if stripped.startswith(assignment_prefix):
            value = stripped.split("=", 1)[1].strip().strip("'\"")
            if value:
                print(f"{variable_name} already configured in {path}")
                return

    generated_value = secrets.token_urlsafe(32)
    temporary = path.with_name(f"{path.name}.tmp.{os.getpid()}")
    content = path.read_text(encoding="utf-8")
    if content and not content.endswith("\n"):
        content += "\n"
    content += f"{variable_name}='{generated_value}'\n"
    temporary.write_text(content, encoding="utf-8")
    temporary.chmod(0o600)
    temporary.replace(path)
    print(f"{variable_name} configured in {path}; value not displayed")


if __name__ == "__main__":
    main()
