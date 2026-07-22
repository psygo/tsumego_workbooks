#!/usr/bin/env python3
"""
Extract problems 1–2634 from the supplied 手筋大事典 PDF into separate SGF files.

This version supports both layouts used in the book:
  * Problems 1–2476: upper-right 13-column partial-board diagrams, 9 per page.
  * Problems 2477–2634: full 19×19 boards, 6 per page.

Install:
    py -m pip install pymupdf pillow numpy

Test the calibrated detector:
    py extract_sugeundaesajeon_sgf_v3.py "path\\to\\book.pdf" --self-test

Generate only the remaining full-board problems:
    py extract_sugeundaesajeon_sgf_v3.py "path\\to\\book.pdf" \
        --start 2477 --end 2634 --output full_board_problems

Generate the entire book:
    py extract_sugeundaesajeon_sgf_v3.py "path\\to\\book.pdf"
"""

from __future__ import annotations

import argparse
import csv
import re
import shutil
import sys
import zipfile
from dataclasses import dataclass
from pathlib import Path

import fitz
import numpy as np
from PIL import Image

CANONICAL_WIDTH = 992
CANONICAL_HEIGHT = 1404
MAX_PROBLEM = 2634
FIRST_PROBLEM_PDF_PAGE = 2
PARTIAL_LAST_PROBLEM = 2476
FULL_FIRST_PROBLEM = 2477
FULL_REGULAR_FIRST_PROBLEM = 2482
MIXED_LAYOUT_PAGE = 277
FULL_REGULAR_FIRST_PAGE = 278

# Partial-board layout: rightmost 13 columns of a 19×19 board.
PARTIAL_GRID_X_BASES = [109, 375, 641]
PARTIAL_GRID_Y_BASES = [127, 536, 945]
PARTIAL_GRID_X_OFFSETS = [9, 27, 46, 65, 84, 102, 121, 140, 158, 177, 196, 214, 233]
PARTIAL_GRID_Y_OFFSETS = [0, 19, 37, 56, 75, 94, 112, 131, 150, 169, 187, 206, 225, 244, 262, 281, 300, 318, 337]

# Full-board layout: two columns × three rows, 19×19 intersections.
FULL_GRID_X_BASES = [120, 533]
FULL_GRID_Y_BASES = [127, 536, 945]
FULL_GRID_OFFSETS = [0, 19, 38, 57, 75, 94, 113, 132, 150, 169, 188, 206, 225, 244, 263, 281, 300, 318, 337]

SGF_LETTERS = "abcdefghijklmnopqrs"
BLACK_DISK_MEAN_MAX = 100.0
WHITE_DISK_MEAN_MIN = 250.0
LABEL_DARK_PIXEL_MIN = 10


@dataclass(frozen=True)
class Layout:
    name: str
    pdf_page: int
    panel_column: int
    panel_row: int
    x_base: int
    y_base: int
    x_offsets: tuple[int, ...]
    y_offsets: tuple[int, ...]
    full_board: bool


@dataclass
class Detection:
    black: set[tuple[int, int]]
    white: set[tuple[int, int]]
    player: str
    warnings: list[str]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("pdf", type=Path)
    parser.add_argument("--start", type=int, default=1)
    parser.add_argument("--end", type=int, default=MAX_PROBLEM)
    parser.add_argument("--output", type=Path, default=Path("sugeundaesajeon_sgf"))
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--no-zip", action="store_true")
    parser.add_argument("--keep-renders", action="store_true")
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument(
        "--diagnostics",
        action="store_true",
        help="Write detector-overlay PNGs for the first problem on each processed page.",
    )
    return parser.parse_args()


def layout_for_problem(problem: int) -> Layout:
    if not 1 <= problem <= MAX_PROBLEM:
        raise ValueError(f"Problem must be between 1 and {MAX_PROBLEM}.")

    if problem <= PARTIAL_LAST_PROBLEM:
        pdf_page = FIRST_PROBLEM_PDF_PAGE + (problem - 1) // 9
        slot = (problem - 1) % 9
        panel_column = slot // 3
        panel_row = slot % 3
        return Layout(
            name="partial-13x19",
            pdf_page=pdf_page,
            panel_column=panel_column,
            panel_row=panel_row,
            x_base=PARTIAL_GRID_X_BASES[panel_column],
            y_base=PARTIAL_GRID_Y_BASES[panel_row],
            x_offsets=tuple(PARTIAL_GRID_X_OFFSETS),
            y_offsets=tuple(PARTIAL_GRID_Y_OFFSETS),
            full_board=False,
        )

    # PDF page 277 is mixed: problem 2476 remains the old partial-board diagram,
    # while 2477–2481 occupy the remaining five cells of the new 2×3 layout.
    if problem <= 2481:
        slot = problem - 2476  # 1..5; slot 0 is occupied by problem 2476.
        panel_column = slot // 3
        panel_row = slot % 3
        return Layout(
            name="full-19x19-mixed-page",
            pdf_page=MIXED_LAYOUT_PAGE,
            panel_column=panel_column,
            panel_row=panel_row,
            x_base=FULL_GRID_X_BASES[panel_column],
            y_base=FULL_GRID_Y_BASES[panel_row],
            x_offsets=tuple(FULL_GRID_OFFSETS),
            y_offsets=tuple(FULL_GRID_OFFSETS),
            full_board=True,
        )

    offset = problem - FULL_REGULAR_FIRST_PROBLEM
    pdf_page = FULL_REGULAR_FIRST_PAGE + offset // 6
    slot = offset % 6
    panel_column = slot // 3
    panel_row = slot % 3
    return Layout(
        name="full-19x19",
        pdf_page=pdf_page,
        panel_column=panel_column,
        panel_row=panel_row,
        x_base=FULL_GRID_X_BASES[panel_column],
        y_base=FULL_GRID_Y_BASES[panel_row],
        x_offsets=tuple(FULL_GRID_OFFSETS),
        y_offsets=tuple(FULL_GRID_OFFSETS),
        full_board=True,
    )


def render_canonical(document: fitz.Document, human_page: int) -> Image.Image:
    page = document.load_page(human_page - 1)
    pix = page.get_pixmap(
        matrix=fitz.Matrix(120 / 72, 120 / 72),
        alpha=False,
        colorspace=fitz.csGRAY,
    )
    image = Image.frombytes("L", (pix.width, pix.height), pix.samples)
    if image.size != (CANONICAL_WIDTH, CANONICAL_HEIGHT):
        image = image.resize((CANONICAL_WIDTH, CANONICAL_HEIGHT), Image.Resampling.LANCZOS)
    return image


def disk_mean(gray: np.ndarray, x: int, y: int) -> float:
    radius = 7
    patch = gray[y-radius:y+radius+1, x-radius:x+radius+1]
    yy, xx = np.ogrid[-radius:radius+1, -radius:radius+1]
    mask = np.sqrt(xx * xx + yy * yy) <= radius
    return float(patch[mask].mean())


def detect_player(gray: np.ndarray, layout: Layout) -> str:
    # Both layouts place 白先 in the same relative title area.
    region = gray[
        layout.y_base - 35:layout.y_base - 5,
        layout.x_base + 35:layout.x_base + 110,
    ]
    dark_pixels = int((region < 128).sum())
    return "W" if dark_pixels > LABEL_DARK_PIXEL_MIN else "B"


def detect_problem(image: Image.Image, layout: Layout) -> Detection:
    gray = np.asarray(image, dtype=np.uint8)
    black: set[tuple[int, int]] = set()
    white: set[tuple[int, int]] = set()
    borderline: list[tuple[int, int, float]] = []

    for board_row, y_offset in enumerate(layout.y_offsets, start=1):
        for local_column, x_offset in enumerate(layout.x_offsets, start=1):
            value = disk_mean(gray, layout.x_base + x_offset, layout.y_base + y_offset)
            if value < BLACK_DISK_MEAN_MAX:
                black.add((local_column, board_row))
            elif value > WHITE_DISK_MEAN_MIN:
                white.add((local_column, board_row))
            elif value < 210 or value > 245:
                borderline.append((local_column, board_row, value))

    warnings: list[str] = []
    total = len(black) + len(white)
    if total == 0:
        warnings.append("No stones detected")
    if total > 150:
        warnings.append(f"Unusually many stones detected: {total}")
    if borderline:
        warnings.append(f"{len(borderline)} borderline intersections")

    return Detection(
        black=black,
        white=white,
        player=detect_player(gray, layout),
        warnings=warnings,
    )


def board_coordinate_to_sgf(column: int, row: int, full_board: bool) -> str:
    full_column = column if full_board else column + 6
    return SGF_LETTERS[full_column - 1] + SGF_LETTERS[row - 1]


def make_sgf(problem: int, layout: Layout, detection: Detection) -> str:
    black = "".join(
        f"[{board_coordinate_to_sgf(column, row, layout.full_board)}]"
        for column, row in sorted(detection.black, key=lambda p: (p[1], p[0]))
    )
    white = "".join(
        f"[{board_coordinate_to_sgf(column, row, layout.full_board)}]"
        for column, row in sorted(detection.white, key=lambda p: (p[1], p[0]))
    )
    view = "aa:ss" if layout.full_board else "ga:ss"
    return (
        "(;FF[4]GM[1]CA[UTF-8]AP[SugeundaesajeonExtractor:3.0]\n"
        "SZ[19]\n"
        f"GN[手筋大事典 - Problem {problem}]\n"
        f"PL[{detection.player}]\n"
        f"AB{black}\n"
        f"AW{white}\n"
        f"VW[{view}]\n"
        f"C[Transcribed from problem {problem}, PDF page {layout.pdf_page}. "
        f"Layout: {layout.name}.])\n"
    )


EXPECTED_FIRST_TEN = {
    1: ({(8,4),(10,4),(10,5),(11,2),(11,3)}, {(12,3),(10,3),(12,2),(11,5),(11,4)}, "B"),
    2: ({(4,3),(5,4),(8,5),(9,4),(9,5),(10,2),(10,3)}, {(10,6),(10,4),(11,2),(10,5),(9,3),(8,4),(11,3)}, "B"),
    3: ({(4,5),(5,4),(6,4),(7,2),(7,4),(8,2),(8,5),(9,3),(9,4),(10,4),(11,4)}, {(10,2),(6,3),(9,2),(10,3),(11,3),(7,3),(8,3),(8,4),(5,2),(5,5)}, "B"),
    4: ({(4,3),(5,6),(9,6),(9,7),(9,8),(10,4),(10,5),(10,6),(11,3),(11,5)}, {(11,6),(12,7),(12,4),(10,8),(12,5),(10,3),(10,7),(11,4)}, "B"),
    5: ({(4,4),(9,2),(9,3),(10,4),(10,5),(10,6),(11,3),(11,6)}, {(12,6),(11,7),(12,4),(8,2),(11,4),(10,2),(11,5),(10,3),(9,1)}, "B"),
    6: ({(9,12),(10,4),(10,5),(10,11),(11,3),(11,6),(11,8),(11,10),(11,13),(12,6),(12,8),(13,7)}, {(11,5),(10,7),(9,9),(10,8),(8,11),(11,7),(10,6),(12,7),(9,11)}, "W"),
    7: ({(7,7),(7,8),(7,9),(8,4),(8,6),(8,10),(8,17),(9,2),(9,8),(9,9),(10,3),(10,4),(10,5),(10,8),(10,10),(10,12),(10,16),(11,10),(11,11),(12,3),(12,11),(12,12)}, {(11,7),(11,4),(11,9),(9,7),(6,3),(12,5),(7,5),(12,13),(12,9),(8,8),(8,3),(8,2),(9,11),(11,13),(10,9),(10,11),(9,10),(11,12),(5,16)}, "B"),
    8: ({(8,3),(9,3),(9,4),(10,4),(11,3)}, {(7,4),(11,5),(10,3),(10,5),(8,4),(11,4),(9,5),(6,3)}, "W"),
    9: ({(3,3),(6,4),(7,3),(7,5),(8,5),(9,4),(10,3),(10,4),(11,4),(11,6)}, {(8,3),(9,8),(7,4),(6,3),(9,3),(9,5),(8,4),(9,6)}, "B"),
    10: ({(5,4),(5,5),(5,6),(7,5),(8,4),(9,4),(10,3),(10,5),(11,5),(11,7),(11,9),(12,4),(12,5)}, {(9,6),(9,8),(9,3),(12,3),(10,4),(11,3),(9,5),(11,4),(4,3),(4,4),(4,5)}, "B"),
}

# Verified structural facts from the transition page. These do not replace visual
# review, but they catch an incorrect page/slot mapping or an empty-board failure.
EXPECTED_FULL_LAYOUT = {
    2477: (277, 0, 1, "W"),
    2478: (277, 0, 2, "W"),
    2479: (277, 1, 0, "W"),
    2480: (277, 1, 1, "W"),
    2481: (277, 1, 2, "B"),
    2482: (278, 0, 0, "W"),
    2634: (303, 0, 2, "W"),
}


def run_self_test(document: fitz.Document) -> None:
    page_cache: dict[int, Image.Image] = {}
    failures: list[str] = []

    for problem, (expected_black, expected_white, expected_player) in EXPECTED_FIRST_TEN.items():
        layout = layout_for_problem(problem)
        if layout.pdf_page not in page_cache:
            page_cache[layout.pdf_page] = render_canonical(document, layout.pdf_page)
        detected = detect_problem(page_cache[layout.pdf_page], layout)
        if detected.black != expected_black:
            failures.append(f"Problem {problem}: black mismatch; missing={sorted(expected_black-detected.black)}, extra={sorted(detected.black-expected_black)}")
        if detected.white != expected_white:
            failures.append(f"Problem {problem}: white mismatch; missing={sorted(expected_white-detected.white)}, extra={sorted(detected.white-expected_white)}")
        if detected.player != expected_player:
            failures.append(f"Problem {problem}: expected player {expected_player}, got {detected.player}")

    for problem, (page, column, row, player) in EXPECTED_FULL_LAYOUT.items():
        layout = layout_for_problem(problem)
        if (layout.pdf_page, layout.panel_column, layout.panel_row) != (page, column, row):
            failures.append(f"Problem {problem}: bad full-board mapping {layout}")
            continue
        if layout.pdf_page not in page_cache:
            page_cache[layout.pdf_page] = render_canonical(document, layout.pdf_page)
        detected = detect_problem(page_cache[layout.pdf_page], layout)
        if len(detected.black) + len(detected.white) < 5:
            failures.append(f"Problem {problem}: implausibly few stones detected")
        if detected.player != player:
            failures.append(f"Problem {problem}: expected player {player}, got {detected.player}")

    if failures:
        raise RuntimeError("SELF-TEST FAILED:\n" + "\n".join(failures))
    print("SELF-TEST PASSED: verified partial-board references and full-board transition mapping.")


def write_diagnostic(image: Image.Image, layout: Layout, detection: Detection, path: Path) -> None:
    rgb = image.convert("RGB")
    from PIL import ImageDraw
    draw = ImageDraw.Draw(rgb)
    radius = 6
    for column, row in detection.black:
        x = layout.x_base + layout.x_offsets[column - 1]
        y = layout.y_base + layout.y_offsets[row - 1]
        draw.ellipse((x-radius, y-radius, x+radius, y+radius), outline=(255, 0, 0), width=2)
    for column, row in detection.white:
        x = layout.x_base + layout.x_offsets[column - 1]
        y = layout.y_base + layout.y_offsets[row - 1]
        draw.ellipse((x-radius, y-radius, x+radius, y+radius), outline=(0, 128, 255), width=2)
    path.parent.mkdir(parents=True, exist_ok=True)
    rgb.save(path)


def validate_sgf(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    if not text.startswith("(;FF[4]GM[1]") or not text.rstrip().endswith(")"):
        raise ValueError(f"Invalid SGF structure: {path}")
    if "SZ[19]" not in text or "PL[" not in text:
        raise ValueError(f"Missing required SGF property: {path}")
    for coordinate in re.findall(r"\[([a-s]{2})\]", text):
        if len(coordinate) != 2:
            raise ValueError(f"Invalid coordinate in {path}: {coordinate}")


def main() -> int:
    args = parse_args()
    if not args.pdf.is_file():
        raise FileNotFoundError(f"PDF not found: {args.pdf}")
    if not (1 <= args.start <= args.end <= MAX_PROBLEM):
        raise ValueError(f"Use 1 <= start <= end <= {MAX_PROBLEM}.")

    document = fitz.open(args.pdf)
    last_layout = layout_for_problem(args.end)
    if last_layout.pdf_page > document.page_count:
        raise RuntimeError(f"Problem {args.end} maps to PDF page {last_layout.pdf_page}, but the PDF has only {document.page_count} pages.")

    run_self_test(document)
    if args.self_test:
        document.close()
        return 0

    if args.output.exists() and any(args.output.iterdir()):
        if not args.overwrite:
            raise FileExistsError(f"Output directory is not empty: {args.output}\nUse --overwrite or choose another directory.")
        shutil.rmtree(args.output)
    args.output.mkdir(parents=True, exist_ok=True)

    manifest: list[dict[str, object]] = []
    warnings: list[dict[str, object]] = []
    page_cache: dict[int, Image.Image] = {}
    diagnostic_pages: set[int] = set()
    total = args.end - args.start + 1

    for index, problem in enumerate(range(args.start, args.end + 1), start=1):
        layout = layout_for_problem(problem)
        if layout.pdf_page not in page_cache:
            page_cache.clear()
            page_cache[layout.pdf_page] = render_canonical(document, layout.pdf_page)
        image = page_cache[layout.pdf_page]
        detection = detect_problem(image, layout)

        filename = f"problem-{problem:04d}.sgf"
        path = args.output / filename
        path.write_text(make_sgf(problem, layout, detection), encoding="utf-8")
        validate_sgf(path)

        manifest.append({
            "problem": problem,
            "pdf_page": layout.pdf_page,
            "layout": layout.name,
            "player_to_move": "White" if detection.player == "W" else "Black",
            "black_stones": len(detection.black),
            "white_stones": len(detection.white),
            "filename": filename,
        })
        for warning in detection.warnings:
            warnings.append({"problem": problem, "pdf_page": layout.pdf_page, "warning": warning})

        if args.diagnostics and layout.pdf_page not in diagnostic_pages:
            write_diagnostic(image, layout, detection, args.output / "diagnostics" / f"problem-{problem:04d}.png")
            diagnostic_pages.add(layout.pdf_page)

        if index == 1 or index % 100 == 0 or index == total:
            print(f"[{index}/{total}] Problem {problem}", flush=True)

    document.close()

    manifest_path = args.output / "manifest.csv"
    with manifest_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=manifest[0].keys())
        writer.writeheader(); writer.writerows(manifest)

    warnings_path = args.output / "extraction_warnings.csv"
    with warnings_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=["problem", "pdf_page", "warning"])
        writer.writeheader(); writer.writerows(warnings)

    sgf_files = sorted(args.output.glob("problem-*.sgf"))
    if len(sgf_files) != total:
        raise RuntimeError(f"Expected {total} SGFs, created {len(sgf_files)}.")

    if not args.no_zip:
        zip_path = args.output.parent / f"sugeundaesajeon_problems_{args.start:04d}-{args.end:04d}.zip"
        with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for path in sgf_files:
                archive.write(path, arcname=path.name)
            archive.write(manifest_path, arcname=manifest_path.name)
            archive.write(warnings_path, arcname=warnings_path.name)
            diagnostic_dir = args.output / "diagnostics"
            if diagnostic_dir.exists():
                for path in diagnostic_dir.glob("*.png"):
                    archive.write(path, arcname=f"diagnostics/{path.name}")
        print(f"ZIP: {zip_path.resolve()}")

    print(f"Output: {args.output.resolve()}")
    print(f"Warnings: {len(warnings)}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\nCancelled.", file=sys.stderr)
        raise SystemExit(130)
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(1)
