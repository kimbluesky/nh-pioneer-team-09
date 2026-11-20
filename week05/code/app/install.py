"""Apply the Week 5 screen to a checkout of the referenced NH desktop app."""

import argparse
import hashlib
from pathlib import Path
from tempfile import NamedTemporaryFile


HERE = Path(__file__).resolve().parent
BASE = "e4ee2bf27bc1f3e4d9093e446ea651920cd57728"
CHANGES = {
    "frontend/src/App.tsx": [
        ("useState<Page>('home')",
         "useState<Page>(() => new URLSearchParams(window.location.search).get('view') === 'hynix-paper' ? 'strategy' : 'home')")],
    "frontend/src/data/strategyStore.ts": [
        ("STRATEGY_TABS = ['전략 목록'", "STRATEGY_TABS = ['하이닉스 모의거래', '전략 목록'")],
    "frontend/src/screens/strategy/StrategyScreen.tsx": [
        ("import { LeaveGuard }", "import { HynixPaperDesk } from './HynixPaperDesk'\nimport { LeaveGuard }"),
        ("    void loadStrategies()\n  }, [])", "    void loadStrategies()\n"
         "    if (new URLSearchParams(window.location.search).get('view') === 'hynix-paper') setTab('하이닉스 모의거래')\n  }, [])"),
        ("{tab === '전략 목록' ? (", "{tab === '하이닉스 모의거래' ? (\n          <HynixPaperDesk />\n"
         "        ) : tab === '전략 목록' ? (")],
    "backend/main.py": [
        ("from backend.strategy.strategy_routes import runs_router as strategy_runs_router",
         "from backend.strategy.strategy_routes import runs_router as strategy_runs_router\n"
         "from backend.strategy.hynix_desk import router as hynix_paper_router"),
        ("app.include_router(strategy_runs_router)", "app.include_router(strategy_runs_router)\n"
         "app.include_router(hynix_paper_router)")],
    "desktop/main.py": [
        ('        f"http://127.0.0.1:{port}",', '        f"http://127.0.0.1:{port}" + '
         '("/?view=hynix-paper" if "--hynix-paper" in sys.argv[1:] else ""),')],
}
ORIGINAL = {
    "frontend/src/App.tsx": "0480c6adf5b029b5db9872cea19d38daeab7cd5c0d38cc12907e978edd10488a",
    "frontend/src/data/strategyStore.ts": "6e331bf2dd47fb8df7f927ffe8bb726ce5773d2887ef740997da741365691eb7",
    "frontend/src/screens/strategy/StrategyScreen.tsx": "c2d8a2ce178daaa277263cf4c73570fc03d21247ce33f4a8299d5adeec0f6678",
    "backend/main.py": "5fb917610c2a464d4c040a1d94b2f4a83b132dd169a83f6d672234fea361475d",
    "desktop/main.py": "ee4fe8b46e37d7a23cbf8cb30f376db3aff3f7b0537f78777ec605f90f548d8a",
}


def integrated(path, relative):
    original = path.read_text(encoding="utf-8")
    edits = CHANGES[relative]
    restored = original
    if all(new in original for _, new in edits):
        for old, new in reversed(edits):
            restored = restored.replace(new, old, 1)
    if hashlib.sha256(restored.encode()).hexdigest() != ORIGINAL[relative]:
        raise ValueError(f"Expected an original or already integrated file from {BASE}: {path}")
    changed = restored
    for old, new in edits:
        if changed.count(old) != 1:
            raise ValueError(f"Missing or ambiguous integration point in {path}")
        changed = changed.replace(old, new)
    return changed


def apply(target):
    pending = {}
    for relative in CHANGES:
        path = target / relative
        updated = integrated(path, relative)
        if path.read_text(encoding="utf-8") != updated:
            line_end = "\r\n" if b"\r\n" in path.read_bytes() else "\n"
            pending[path] = updated.replace("\n", line_end).encode()
    files = {target / src.relative_to(HERE): src
             for folder in ("backend", "frontend") for src in (HERE / folder).rglob("*")
             if src.suffix in (".py", ".tsx", ".css", ".ts")}
    evidence = HERE.parents[1] / "results"
    for name in ("study.json", "strategy-runs.json", "trades.jsonl", "models/entry.npz", "models/exit.npz"):
        files[target / "week05/results" / name] = evidence / name
    for dest, src in files.items():
        data = src.read_bytes()
        if dest.exists():
            existing = dest.read_bytes()
            same = existing == data
            if src.suffix in (".py", ".ts", ".tsx", ".css"):
                same = existing.replace(b"\r\n", b"\n") == data.replace(b"\r\n", b"\n")
            if not same:
                raise FileExistsError(f"Existing file differs: {dest}")
        else:
            pending[dest] = data
    for dest, data in pending.items():
        dest.parent.mkdir(exist_ok=True, parents=True)
        with NamedTemporaryFile(dir=dest.parent, delete=False) as temp:
            temp.write(data)
        Path(temp.name).replace(dest)
    print(f"NH Week 5 screen ready in {target} ({len(pending)} files updated)")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("app", type=Path, help="Root of the official NH app checkout")
    apply(parser.parse_args().app.resolve())
