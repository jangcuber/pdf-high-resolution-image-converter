#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
PDF Rasterizer v3
- 입력 PDF의 페이지를 지정한 DPI로 PNG 렌더링 후 새 PDF 생성
- 앞부분 N페이지는 원본 그대로 유지 가능
- 나머지 페이지는 이미지화
"""

from __future__ import annotations

import os
import queue
import tempfile
import threading
import traceback
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

try:
    import fitz  # PyMuPDF
except ImportError:
    raise SystemExit(
        "PyMuPDF가 설치되어 있지 않습니다.\n"
        "터미널에서 다음 명령을 실행하세요:\n\n"
        "pip install PyMuPDF"
    )


APP_TITLE = "PDF 고해상도 이미지 변환기"
DPI_PRESETS = ("150", "300", "600")


def rasterize_pdf(
    input_pdf: str | os.PathLike,
    output_pdf: str | os.PathLike,
    dpi: int = 300,
    keep_original_pages_count: int = 0,
    keep_images: bool = False,
    image_output_dir: str | os.PathLike | None = None,
    progress_callback=None,
    status_callback=None,
    cancel_event: threading.Event | None = None,
) -> None:
    """
    PDF를 새 PDF로 변환합니다.

    keep_original_pages_count:
        앞에서부터 원본 그대로 유지할 페이지 수
        예) 0 = 전체 이미지화
            1 = 1페이지만 원본 유지
            2 = 1~2페이지 원본 유지
    """
    input_pdf = Path(input_pdf)
    output_pdf = Path(output_pdf)

    if not input_pdf.exists():
        raise FileNotFoundError(f"입력 PDF를 찾을 수 없습니다: {input_pdf}")
    if input_pdf.suffix.lower() != ".pdf":
        raise ValueError("입력 파일은 PDF여야 합니다.")
    if dpi < 72 or dpi > 1200:
        raise ValueError("DPI는 72~1200 사이로 설정해 주세요.")
    if keep_original_pages_count < 0:
        raise ValueError("원본 유지 페이지 수는 0 이상이어야 합니다.")

    output_pdf.parent.mkdir(parents=True, exist_ok=True)

    if keep_images:
        if image_output_dir is None:
            image_output_dir = output_pdf.with_name(output_pdf.stem + "_pages")
        image_dir = Path(image_output_dir)
        image_dir.mkdir(parents=True, exist_ok=True)
        temp_ctx = _FixedDirectoryContext(image_dir)
    else:
        temp_ctx = tempfile.TemporaryDirectory(prefix="pdf_raster_")

    src_doc = None
    out_doc = None

    try:
        with temp_ctx as tmpdir:
            tmpdir = Path(tmpdir)
            src_doc = fitz.open(str(input_pdf))

            if src_doc.needs_pass:
                raise ValueError(
                    "암호화된 PDF입니다. 먼저 PDF의 암호를 해제한 뒤 다시 시도해 주세요."
                )

            page_count = src_doc.page_count
            if page_count == 0:
                raise ValueError("페이지가 없는 PDF입니다.")

            out_doc = fitz.open()

            try:
                out_doc.set_metadata(dict(src_doc.metadata or {}))
            except Exception:
                pass

            keep_count = min(keep_original_pages_count, page_count)

            # 앞부분 keep_count 페이지 원본 유지
            if keep_count > 0:
                if cancel_event and cancel_event.is_set():
                    raise _CancelledError()

                if status_callback:
                    if keep_count == 1:
                        status_callback("1페이지를 원본 그대로 복사하는 중...")
                    else:
                        status_callback(f"1~{keep_count}페이지를 원본 그대로 복사하는 중...")

                out_doc.insert_pdf(src_doc, from_page=0, to_page=keep_count - 1)

                if progress_callback:
                    progress_callback(keep_count, page_count)

            # 나머지 페이지 이미지화
            for i in range(keep_count, page_count):
                if cancel_event and cancel_event.is_set():
                    raise _CancelledError()

                page = src_doc.load_page(i)

                if status_callback:
                    status_callback(
                        f"{i + 1}/{page_count} 페이지를 {dpi} DPI로 렌더링 중..."
                    )

                pix = page.get_pixmap(dpi=dpi, alpha=False)
                image_path = tmpdir / f"page_{i + 1:05d}.png"
                pix.save(str(image_path))

                page_rect = page.rect
                new_page = out_doc.new_page(
                    width=page_rect.width,
                    height=page_rect.height,
                )
                new_page.insert_image(
                    new_page.rect,
                    filename=str(image_path),
                    keep_proportion=False,
                )

                if progress_callback:
                    progress_callback(i + 1, page_count)

            if cancel_event and cancel_event.is_set():
                raise _CancelledError()

            if status_callback:
                status_callback("새 PDF를 저장하는 중...")

            out_doc.save(
                str(output_pdf),
                garbage=4,
                deflate=True,
                clean=True,
            )

    finally:
        if out_doc is not None:
            out_doc.close()
        if src_doc is not None:
            src_doc.close()


class _FixedDirectoryContext:
    """기존 폴더를 context manager처럼 사용하되 종료 시 삭제하지 않습니다."""

    def __init__(self, path: Path):
        self.path = path

    def __enter__(self):
        return str(self.path)

    def __exit__(self, exc_type, exc, tb):
        return False


class _CancelledError(Exception):
    pass


class PdfRasterizerApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP_TITLE)
        self.geometry("740x540")
        self.minsize(690, 500)

        self.msg_queue = queue.Queue()
        self.worker = None
        self.cancel_event = threading.Event()

        self.input_var = tk.StringVar()
        self.output_var = tk.StringVar()
        self.dpi_var = tk.StringVar(value="300")
        self.keep_original_count_var = tk.StringVar(value="0")
        self.keep_images_var = tk.BooleanVar(value=False)
        self.status_var = tk.StringVar(value="PDF 파일을 선택해 주세요.")
        self.progress_var = tk.DoubleVar(value=0)

        self._build_ui()
        self.after(100, self._poll_queue)

    def _build_ui(self):
        root = ttk.Frame(self, padding=18)
        root.pack(fill="both", expand=True)

        title = ttk.Label(
            root,
            text="PDF 고해상도 이미지 변환기",
            font=("TkDefaultFont", 16, "bold"),
        )
        title.pack(anchor="w")

        desc = ttk.Label(
            root,
            text=(
                "PDF의 페이지를 고해상도 이미지로 렌더링한 뒤 새 PDF로 만듭니다.\n"
                "필요하면 앞부분 몇 페이지는 원본 상태로 유지할 수 있습니다."
            ),
            justify="left",
        )
        desc.pack(anchor="w", pady=(6, 16))

        file_frame = ttk.LabelFrame(root, text="파일", padding=12)
        file_frame.pack(fill="x")

        ttk.Label(file_frame, text="입력 PDF").grid(
            row=0, column=0, sticky="w", padx=(0, 8), pady=5
        )
        ttk.Entry(file_frame, textvariable=self.input_var).grid(
            row=0, column=1, sticky="ew", pady=5
        )
        ttk.Button(file_frame, text="찾기...", command=self._choose_input).grid(
            row=0, column=2, padx=(8, 0), pady=5
        )

        ttk.Label(file_frame, text="출력 PDF").grid(
            row=1, column=0, sticky="w", padx=(0, 8), pady=5
        )
        ttk.Entry(file_frame, textvariable=self.output_var).grid(
            row=1, column=1, sticky="ew", pady=5
        )
        ttk.Button(file_frame, text="찾기...", command=self._choose_output).grid(
            row=1, column=2, padx=(8, 0), pady=5
        )

        file_frame.columnconfigure(1, weight=1)

        option_frame = ttk.LabelFrame(root, text="설정", padding=12)
        option_frame.pack(fill="x", pady=(12, 0))

        ttk.Label(option_frame, text="렌더링 DPI").grid(row=0, column=0, sticky="w")
        ttk.Combobox(
            option_frame,
            textvariable=self.dpi_var,
            values=DPI_PRESETS,
            width=10,
            state="normal",
        ).grid(row=0, column=1, sticky="w", padx=(8, 20))

        ttk.Label(
            option_frame,
            text="300 DPI 권장 / 600 DPI는 파일 크기와 메모리 사용량이 크게 늘 수 있습니다.",
        ).grid(row=0, column=2, sticky="w")

        ttk.Label(option_frame, text="앞에서 원본 유지할 페이지 수").grid(
            row=1, column=0, sticky="w", pady=(12, 0)
        )
        ttk.Spinbox(
            option_frame,
            from_=0,
            to=999,
            increment=1,
            width=10,
            textvariable=self.keep_original_count_var,
        ).grid(row=1, column=1, sticky="w", padx=(8, 20), pady=(12, 0))

        ttk.Label(
            option_frame,
            text="예: 0 = 전체 이미지화 / 1 = 1페이지 원본 유지 / 2 = 1~2페이지 원본 유지",
        ).grid(row=1, column=2, sticky="w", pady=(12, 0))

        ttk.Checkbutton(
            option_frame,
            text="페이지별 PNG 이미지도 출력 PDF 옆 폴더에 보관",
            variable=self.keep_images_var,
        ).grid(row=2, column=0, columnspan=3, sticky="w", pady=(12, 0))

        progress_frame = ttk.Frame(root)
        progress_frame.pack(fill="x", pady=(16, 0))

        ttk.Label(progress_frame, textvariable=self.status_var).pack(anchor="w")

        self.progress = ttk.Progressbar(
            progress_frame,
            variable=self.progress_var,
            maximum=100,
            mode="determinate",
        )
        self.progress.pack(fill="x", pady=(7, 0))

        btn_frame = ttk.Frame(root)
        btn_frame.pack(fill="x", pady=(18, 0))

        self.start_btn = ttk.Button(
            btn_frame,
            text="변환 시작",
            command=self._start,
        )
        self.start_btn.pack(side="right")

        self.cancel_btn = ttk.Button(
            btn_frame,
            text="취소",
            command=self._cancel,
            state="disabled",
        )
        self.cancel_btn.pack(side="right", padx=(0, 8))

        ttk.Button(btn_frame, text="종료", command=self.destroy).pack(side="left")

    def _choose_input(self):
        path = filedialog.askopenfilename(
            title="입력 PDF 선택",
            filetypes=[("PDF files", "*.pdf"), ("All files", "*.*")],
        )
        if not path:
            return

        self.input_var.set(path)

        p = Path(path)
        try:
            dpi = int(self.dpi_var.get())
        except ValueError:
            dpi = 300

        default_output = p.with_name(f"{p.stem}_rasterized_{dpi}dpi.pdf")
        self.output_var.set(str(default_output))

    def _choose_output(self):
        initial = self.output_var.get().strip()
        initial_dir = str(Path(initial).parent) if initial else None
        initial_file = Path(initial).name if initial else "rasterized.pdf"

        path = filedialog.asksaveasfilename(
            title="출력 PDF 저장",
            defaultextension=".pdf",
            filetypes=[("PDF files", "*.pdf")],
            initialdir=initial_dir,
            initialfile=initial_file,
        )
        if path:
            self.output_var.set(path)

    def _start(self):
        input_path = self.input_var.get().strip()
        output_path = self.output_var.get().strip()

        if not input_path:
            messagebox.showwarning(APP_TITLE, "입력 PDF를 선택해 주세요.")
            return

        if not output_path:
            messagebox.showwarning(APP_TITLE, "출력 PDF 위치를 선택해 주세요.")
            return

        try:
            dpi = int(self.dpi_var.get().strip())
            if not (72 <= dpi <= 1200):
                raise ValueError
        except ValueError:
            messagebox.showwarning(
                APP_TITLE,
                "DPI는 72~1200 사이의 정수로 입력해 주세요.",
            )
            return

        try:
            keep_original_count = int(self.keep_original_count_var.get().strip())
            if keep_original_count < 0:
                raise ValueError
        except ValueError:
            messagebox.showwarning(
                APP_TITLE,
                "원본 유지 페이지 수는 0 이상의 정수로 입력해 주세요.",
            )
            return

        in_path = Path(input_path).resolve()
        out_path = Path(output_path).resolve()

        if in_path == out_path:
            messagebox.showwarning(
                APP_TITLE,
                "입력 파일과 출력 파일을 다르게 지정해 주세요.",
            )
            return

        if out_path.exists():
            if not messagebox.askyesno(
                APP_TITLE,
                "출력 파일이 이미 존재합니다. 덮어쓸까요?",
            ):
                return

            try:
                out_path.unlink()
            except Exception as e:
                messagebox.showerror(
                    APP_TITLE,
                    f"기존 출력 파일을 삭제할 수 없습니다.\n\n{e}",
                )
                return

        self.cancel_event.clear()
        self.progress_var.set(0)
        self.start_btn.config(state="disabled")
        self.cancel_btn.config(state="normal")

        keep_images = self.keep_images_var.get()
        image_dir = (
            out_path.with_name(out_path.stem + "_pages")
            if keep_images
            else None
        )

        self.worker = threading.Thread(
            target=self._worker_run,
            args=(
                in_path,
                out_path,
                dpi,
                keep_original_count,
                keep_images,
                image_dir,
            ),
            daemon=True,
        )
        self.worker.start()

    def _worker_run(
        self,
        in_path,
        out_path,
        dpi,
        keep_original_count,
        keep_images,
        image_dir,
    ):
        def on_progress(done, total):
            pct = done / total * 100
            self.msg_queue.put(("progress", pct))

        def on_status(text):
            self.msg_queue.put(("status", text))

        try:
            rasterize_pdf(
                in_path,
                out_path,
                dpi=dpi,
                keep_original_pages_count=keep_original_count,
                keep_images=keep_images,
                image_output_dir=image_dir,
                progress_callback=on_progress,
                status_callback=on_status,
                cancel_event=self.cancel_event,
            )

            self.msg_queue.put(
                (
                    "done",
                    str(out_path),
                    str(image_dir) if image_dir else None,
                    keep_original_count,
                )
            )

        except _CancelledError:
            try:
                if Path(out_path).exists():
                    Path(out_path).unlink()
            except Exception:
                pass
            self.msg_queue.put(("cancelled",))

        except Exception as e:
            self.msg_queue.put(("error", str(e), traceback.format_exc()))

    def _cancel(self):
        self.cancel_event.set()
        self.status_var.set("취소 요청 중...")

    def _poll_queue(self):
        try:
            while True:
                msg = self.msg_queue.get_nowait()
                kind = msg[0]

                if kind == "progress":
                    self.progress_var.set(msg[1])

                elif kind == "status":
                    self.status_var.set(msg[1])

                elif kind == "done":
                    self.progress_var.set(100)
                    self.status_var.set("완료되었습니다.")
                    self.start_btn.config(state="normal")
                    self.cancel_btn.config(state="disabled")

                    out_path = msg[1]
                    image_dir = msg[2]
                    keep_original_count = msg[3]

                    if keep_original_count <= 0:
                        mode_text = "\n\n모든 페이지가 이미지화되었습니다."
                    elif keep_original_count == 1:
                        mode_text = "\n\n1페이지는 원본 상태로 유지되었습니다."
                    else:
                        mode_text = f"\n\n1~{keep_original_count}페이지는 원본 상태로 유지되었습니다."

                    image_text = (
                        f"\n\n페이지 이미지 폴더:\n{image_dir}"
                        if image_dir
                        else ""
                    )

                    messagebox.showinfo(
                        APP_TITLE,
                        f"변환이 완료되었습니다.\n\n출력 PDF:\n{out_path}"
                        f"{mode_text}{image_text}",
                    )

                elif kind == "cancelled":
                    self.status_var.set("변환이 취소되었습니다.")
                    self.start_btn.config(state="normal")
                    self.cancel_btn.config(state="disabled")

                elif kind == "error":
                    self.status_var.set("오류가 발생했습니다.")
                    self.start_btn.config(state="normal")
                    self.cancel_btn.config(state="disabled")
                    messagebox.showerror(
                        APP_TITLE,
                        f"변환 중 오류가 발생했습니다.\n\n{msg[1]}",
                    )

        except queue.Empty:
            pass

        self.after(100, self._poll_queue)


if __name__ == "__main__":
    app = PdfRasterizerApp()
    app.mainloop()
