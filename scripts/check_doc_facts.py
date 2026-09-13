"""Static facts-gate: standing-docs claims vs countable repo truth (stdlib only).

Usage: python scripts/check_doc_facts.py
Exit 0 when every probe passes (SKIPPED lines are out of scope by design);
exit 1 listing failures, one per line. Every failure line carries file:line.

Probes (taxonomy local://docs_opt_taxonomy.md section 3; stale IDs T1-T7 in
local://docs_verify_top.md, M1-M6 in local://docs_verify_map.md):
  P-POST ...... Mini App route count: `add_post("/miniapp/api/` in
                src/copytradeaster/miniapp/server.py (pinned 27) vs every
                `N POST ... /miniapp/api/*` route-count claim in standing docs.
  P-PKG ....... Backend package count: dirs in src/copytradeaster/ (pinned 14)
                vs every `N пакетов` claim, plus each real package named in
                docs/ARCHITECTURE.md.
  P-LINK ...... Reuses scripts/check_doc_links.py (non-strict): every relative
                .md link resolves.
  P-TESTREF ... Every `review_stage1*.md` reference in tests/**/*.py resolves
                to a file under docs/ (bare `review_stage1.md` never existed).
  P-SINGLEHOME  No suite-counter snapshots in the DEV_PLAN header (lines 1-15,
                which must link ENGINE_TEST_STATUS instead) and none anywhere
                in docs/AGENTS.md. Dated checklist snapshots deeper in the
                skeleton are frozen history and exempt (Main decision
                2026-09-10): the single-home rule bans duplicated *current*
                counters, not historical records.
  P-SYMBOL .... OTOCO symbols (`placeStrategyOrder`/`updateStrategyOrder`)
                must be absent from src/ (pinned 0: ASTER §8 describes them but
                the code has no such calls), and ASTER_API_RESEARCH must carry
                its `> FREEZE` banner so the stale §8 is never read as current.
  P-COVER ..... Every docs/reviews/*.md basename is listed in docs/AGENTS.md,
                the unqualified "answers live in JOURNAL" sentence is gone, the
                dust/flip misattribution is gone, and the corrective-acceptance
                note survives only with its `только к ... строкам` scope.
  P-PNG ....... acceptance-s3 file count (pinned 20) vs claims; a `17 PNG`
                snapshot passes only in a file carrying its `На диске сейчас
                20` reconciliation note (append-only journals are not rewritten).
  P-ASSETS .... stage3-mini-app file count (pinned 18) plus its docs/AGENTS.md
                map row.
  P-SECTIONS .. `## N.` section count in docs/ARCHITECTURE.md (pinned 20) vs
                the `§§1–20` range claimed in root AGENTS.md.

Out of scope (printed as SKIPPED, never run): hot suite-run facts (passed /
skipped counters, formatted-file counts) need pytest/vitest executions; this
static gate never runs suites, and it deliberately does not quote numbers it
cannot verify. Their home is docs/ENGINE_TEST_STATUS_2026-08.md. Same exemption
covers append-only snapshots: only standing-docs claims with the
`/miniapp/api/*` glob are checked by P-POST, so a journal row may keep the
count of the day it was written.
"""

from __future__ import annotations

import re
from pathlib import Path

try:
    import check_doc_links  # same dir when run as `python scripts/...`
except ImportError:  # any other invocation style: load sibling by path
    import importlib.util

    _spec = importlib.util.spec_from_file_location(
        "check_doc_links", Path(__file__).resolve().parent / "check_doc_links.py"
    )
    check_doc_links = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(check_doc_links)

ROOT = Path(__file__).resolve().parent.parent

EXPECTED_POST = 27
EXPECTED_PKG = 14
EXPECTED_PNG = 20
EXPECTED_ASSETS = 18
EXPECTED_SECTIONS = 20
EXPECTED_MIGR = 65
EXPECTED_CLI = 13
EXPECTED_TABS = 4

HEADER_LINES = 15  # P-SINGLEHOME scope: DEV_PLAN header block only
SKEL = ROOT / "docs/DEV_PLAN_SKELETON.md"
ARCH = ROOT / "docs/ARCHITECTURE.md"
DOC_AGENTS = ROOT / "docs/AGENTS.md"
ROOT_AGENTS = ROOT / "AGENTS.md"
ASTER = ROOT / "docs/ASTER_API_RESEARCH_2026-08.md"
INDEX = ROOT / "docs/reports/MINIAPP_UI_ACCEPTANCE_INDEX_2026-09.md"
ENGINE = ROOT / "docs/ENGINE_TEST_STATUS_2026-08.md"

failures: list[str] = []
skipped: list[str] = []
passed: list[str] = []


def fail(msg: str) -> None:
    failures.append(msg)


def rel(path: Path) -> str:
    return str(path.relative_to(ROOT))


def lines_of(path: Path) -> list[str]:
    return path.read_text(encoding="utf-8").splitlines()


def p_post() -> None:
    srv = ROOT / "src/copytradeaster/miniapp/server.py"
    actual = srv.read_text(encoding="utf-8").count('add_post("/miniapp/api/')
    if actual != EXPECTED_POST:
        fail(f"{rel(srv)}:1: [P-POST] {actual} routes != pinned {EXPECTED_POST}")
        return
    # Route-count claims use the /miniapp/api/* glob OR bare route/method
    # tallies ("26 routes", "26-method"). Per-request "N POST <concrete
    # endpoint>" traces in journals never match either pattern.
    pat = re.compile(
        r"(?<![A-Za-z0-9_])(\d+)\s+POST\s+`?/miniapp/api/\*"
        r"|(?<![A-Za-z0-9_])(\d+)\s+(?:miniapp\s+)?(?:api\s+)?routes?\b"
        r"|(?<![A-Za-z0-9_])(\d+)-methods?\b"
    )
    scan = [ROOT_AGENTS, ROOT / "miniapp/AGENTS.md", *sorted((ROOT / "docs").rglob("*.md"))]
    for path in scan:
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")
        flat = text.replace("\n", " ")  # 1:1, offsets preserved
        for mch in pat.finditer(flat):
            num = next(g for g in mch.groups() if g is not None)
            if int(num) != actual:
                lineno = text.count("\n", 0, mch.start()) + 1
                fail(f"{rel(path)}:{lineno}: [P-POST] claims {num} routes, code has {actual}")


def p_pkg() -> None:
    pkgs = sorted(
        p.name
        for p in (ROOT / "src/copytradeaster").iterdir()
        if p.is_dir() and p.name != "__pycache__"
    )
    if len(pkgs) != EXPECTED_PKG:
        fail(f"src/copytradeaster:1: [P-PKG] {len(pkgs)} packages != pinned {EXPECTED_PKG}")
        return
    atext = ARCH.read_text(encoding="utf-8")
    claim_line = next((n for n, line in enumerate(atext.splitlines(), 1) if "пакетов" in line), 1)
    for path in (ARCH, DOC_AGENTS, ROOT_AGENTS):
        for n, line in enumerate(lines_of(path), 1):
            mch = re.search(r"(\d+)\s+пакетов", line)
            if mch and int(mch.group(1)) != len(pkgs):
                fail(f"{rel(path)}:{n}: [P-PKG] claims {mch.group(1)} пакетов, fs has {len(pkgs)}")
    for pkg in pkgs:
        if pkg not in atext:
            fail(f"docs/ARCHITECTURE.md:{claim_line}: [P-PKG] package '{pkg}' not documented")


def p_singlehome() -> None:
    # Map leg: `[0-9]+ passed` only (true suite-counter snapshots, the T6 class).
    # `N/N` is intentionally NOT banned in the map: at HEAD it holds four legit
    # non-counters — `Stage 1/2/3` (stage-range label), `2026-08-22/23` (date
    # range), `9/9 PASS` (owner WebView tally), `15/15 PASS` (browser-slice
    # tally) — none duplicating ENGINE suite counters (Main decision 2026-09-10).
    for n, line in enumerate(lines_of(DOC_AGENTS), 1):
        if re.search(r"[0-9]+ passed", line):
            fail(f"docs/AGENTS.md:{n}: [P-SINGLEHOME] suite-counter snapshot outside its home")
    pat = re.compile(r"[0-9]+ passed|[0-9]+/[0-9]+")
    head = lines_of(SKEL)[:HEADER_LINES]
    for n, line in enumerate(head, 1):
        if pat.search(line):
            fail(
                f"docs/DEV_PLAN_SKELETON.md:{n}: [P-SINGLEHOME] header counter snapshot;"
                f" link {ENGINE.name} instead"
            )
    if ENGINE.name not in "\n".join(head):
        fail(
            "docs/DEV_PLAN_SKELETON.md:1: [P-SINGLEHOME] header lacks single-home link to"
            f" {ENGINE.name}"
        )


def p_link() -> None:
    for err in check_doc_links.check(False):
        fail(f"[P-LINK] {err}")


def p_testref() -> None:
    pat = re.compile(r"([\w./-]*review_stage1[\w.-]*\.md)")
    for path in sorted((ROOT / "tests").rglob("*.py")):
        for n, line in enumerate(lines_of(path), 1):
            for mch in pat.finditer(line):
                tok = mch.group(1)
                candidates = (
                    ROOT / tok,
                    ROOT / "docs" / tok,
                    ROOT / "docs/reviews" / Path(tok).name,
                )
                if not any(c.is_file() for c in candidates):
                    fail(
                        f"{rel(path)}:{n}: [P-TESTREF] '{tok}' resolves under neither"
                        " docs/ nor docs/reviews/"
                    )


def p_symbol() -> None:
    for path in sorted((ROOT / "src").rglob("*.py")):
        for n, line in enumerate(lines_of(path), 1):
            if "placeStrategyOrder" in line or "updateStrategyOrder" in line:
                fail(
                    f"{rel(path)}:{n}: [P-SYMBOL] OTOCO symbol present while"
                    f" {ASTER.name} §8 is frozen as absent"
                )
    if not any(line.startswith("> FREEZE") for line in lines_of(ASTER)):
        fail(
            f"docs/{ASTER.name}:1: [P-SYMBOL] stale §8 names OTOCO symbols absent from"
            " src/ but file carries no `> FREEZE` banner"
        )


def p_cover() -> None:
    atext = DOC_AGENTS.read_text(encoding="utf-8")
    alines = atext.splitlines()
    hdr = next((n for n, line in enumerate(alines, 1) if line.startswith("## docs/reviews/")), 1)
    for name in sorted(p.name for p in (ROOT / "docs/reviews").glob("*.md")):
        if name not in atext:
            fail(f"docs/AGENTS.md:{hdr}: [P-COVER] review file not in map: {name}")
    norm = re.sub(r"\s+", " ", atext).replace("`", "")
    if "Ответы на ревью живут в docs/reports/FINAL_REVIEW_GATE_JOURNAL_2026-08.md." in norm:
        n = next(i for i, line in enumerate(alines, 1) if "Ответы на ревью живут" in line)
        fail(
            f"docs/AGENTS.md:{n}: [P-COVER] overbroad answers-home claim"
            " (M1: JOURNAL covers final gate only)"
        )
    if "dust zero-target — миграция #12" in norm:
        n = next(i for i, line in enumerate(alines, 1) if "миграция #12" in line)
        fail(
            f"docs/AGENTS.md:{n}: [P-COVER] dust/flip misattributed to review_stage1_final.md (M2)"
        )
    core = "corrective acceptance после нового независимого аудита описан в"
    for mch in re.finditer(core, norm, flags=re.IGNORECASE):
        if "только к" not in norm[max(0, mch.start() - 120) : mch.start()]:
            n = next((i for i, text in enumerate(alines, 1) if "независимого аудита" in text), 1)
            fail(
                f"docs/AGENTS.md:{n}: [P-COVER] corrective-acceptance note not scoped"
                " to its gate (M3)"
            )


def p_png() -> None:
    shot_dir = ROOT / "docs/reports/acceptance-s3"
    actual = sum(1 for p in shot_dir.iterdir() if p.is_file())
    if actual != EXPECTED_PNG:
        fail(f"docs/reports/acceptance-s3:1: [P-PNG] {actual} files != pinned {EXPECTED_PNG}")
    stale = re.compile(r"17\s+(PNGs|PNG|скриншотов)")
    for path in sorted((ROOT / "docs").rglob("*.md")):
        text = path.read_text(encoding="utf-8")
        if "На диске сейчас 20" in text:
            continue  # acknowledged snapshot with trailing reconciliation note
        for n, line in enumerate(text.splitlines(), 1):
            if stale.search(line):
                fail(f"{rel(path)}:{n}: [P-PNG] stale 17-count without reconciliation note")
    for path in (INDEX, DOC_AGENTS):
        if not re.search(r"20\s+(PNGs|PNG|скриншотов)", path.read_text(encoding="utf-8")):
            fail(f"{rel(path)}:1: [P-PNG] no current 20-count claim for acceptance-s3")


def p_assets() -> None:
    actual = sum(
        1 for p in (ROOT / "docs/reports/assets/stage3-mini-app").rglob("*") if p.is_file()
    )
    if actual != EXPECTED_ASSETS:
        fail(
            "docs/reports/assets/stage3-mini-app:1: [P-ASSETS]"
            f" {actual} files != pinned {EXPECTED_ASSETS}"
        )
    if "assets/stage3-mini-app" not in DOC_AGENTS.read_text(encoding="utf-8"):
        fail("docs/AGENTS.md:1: [P-ASSETS] map has no assets/stage3-mini-app/ row")


def p_sections() -> None:
    secs = [line for line in lines_of(ARCH) if re.match(r"## [0-9]+\.", line)]
    if len(secs) != EXPECTED_SECTIONS:
        fail(
            f"docs/ARCHITECTURE.md:1: [P-SECTIONS] {len(secs)} numbered sections"
            f" != pinned {EXPECTED_SECTIONS}"
        )
        return
    for n, line in enumerate(lines_of(ROOT_AGENTS), 1):
        mch = re.search(r"§§1\s*[–-]\s*(\d+)", line)
        if mch and "ARCHITECTURE" in line and int(mch.group(1)) != len(secs):
            fail(f"AGENTS.md:{n}: [P-SECTIONS] claims §§1–{mch.group(1)}, file has {len(secs)}")


def p_migr() -> None:
    import ast

    db = ROOT / "src/copytradeaster/storage/database.py"
    tree = ast.parse(db.read_text(encoding="utf-8"))
    actual = next(
        len(node.value.elts)
        for node in ast.walk(tree)
        if isinstance(node, (ast.Assign, ast.AnnAssign))
        and any(
            getattr(target, "id", "") == "MIGRATIONS"
            for target in ([node.target] if isinstance(node, ast.AnnAssign) else node.targets)
        )
    )
    if actual != EXPECTED_MIGR:
        fail(f"{rel(db)}:1: [P-MIGR] {actual} migrations != pinned {EXPECTED_MIGR}")
        return
    # Standing claims only (root map + ARCH); ENGINE frozen snapshots exempt.
    for path in (ROOT_AGENTS, ARCH):
        for n, line in enumerate(lines_of(path), 1):
            for mch in re.finditer(r"0\.\.(\d+)|(\d+)\s+migrations?", line):
                num = next(g for g in mch.groups() if g is not None)
                if int(num) != actual and not (mch.group(1) and int(num) == actual - 1):
                    fail(
                        f"{rel(path)}:{n}: [P-MIGR] claims {mch.group(0)}, code has"
                        f" {actual} (0..{actual - 1})"
                    )


def p_cli() -> None:
    main = ROOT / "src/copytradeaster/cli/main.py"
    actual = len(
        re.findall(
            r"^    .{0,40}?(?<!user_)sub\.add_parser\(",
            main.read_text(encoding="utf-8"),
            re.M,
        )
    )
    if actual != EXPECTED_CLI:
        fail(f"{rel(main)}:1: [P-CLI] {actual} subcommands != pinned {EXPECTED_CLI}")
        return
    for n, line in enumerate(lines_of(ROOT_AGENTS), 1):
        mch = re.search(r"(\d+)\s+subcommands?", line)
        if mch and int(mch.group(1)) != actual:
            fail(f"AGENTS.md:{n}: [P-CLI] claims {mch.group(1)} subcommands, code has {actual}")


def p_tabs() -> None:
    nav = ROOT / "miniapp/src/navigation.ts"
    actual = len(
        set(
            re.findall(
                r"['\"](home|positions|activity|settings)['\"]",
                nav.read_text(encoding="utf-8"),
            )
        )
    )
    if actual != EXPECTED_TABS:
        fail(f"{rel(nav)}:1: [P-TABS] {actual} root tabs != pinned {EXPECTED_TABS}")
        return
    for path in (ROOT_AGENTS, ROOT / "miniapp/AGENTS.md"):
        for n, line in enumerate(lines_of(path), 1):
            mch = re.search(r"(\d+)-tab\b", line)
            if mch and int(mch.group(1)) != actual:
                fail(f"{rel(path)}:{n}: [P-TABS] claims {mch.group(1)}-tab, code has {actual}")


PROBES = (
    ("P-POST", p_post),
    ("P-PKG", p_pkg),
    ("P-LINK", p_link),
    ("P-TESTREF", p_testref),
    ("P-SINGLEHOME", p_singlehome),
    ("P-SYMBOL", p_symbol),
    ("P-COVER", p_cover),
    ("P-PNG", p_png),
    ("P-ASSETS", p_assets),
    ("P-SECTIONS", p_sections),
    ("P-MIGR", p_migr),
    ("P-CLI", p_cli),
    ("P-TABS", p_tabs),
)


def main() -> int:
    for name, fn in PROBES:
        before = len(failures)
        fn()
        if len(failures) == before:
            passed.append(name)
            print(f"PASS {name}")
    skipped.append(
        "SKIPPED suite-counters: hot suite-run facts need pytest/vitest executions, which "
        f"this static gate never runs and never quotes; home is {ENGINE.name}."
    )
    for line in skipped:
        print(line)
    for line in failures:
        print(f"FAIL {line}")
    print(f"check_doc_facts: {len(passed)} passed, {len(skipped)} skipped, {len(failures)} failed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
