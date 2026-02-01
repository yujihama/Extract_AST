# %%
from pathlib import Path
from openpyxl.drawing.image import Image as XLImage
from openpyxl.styles import Alignment, Font
import base64
from typing import Any, Iterable, Optional
from src.utils import build_llm
import openpyxl
from PIL import Image, ImageDraw, ImageFont
from pydantic import BaseModel, Field
import dotenv
import json

dotenv.load_dotenv()

# %%
def render_pdf_to_png_pages(
    pdf_path: Path,
    *,
    output_dir: Path,
    dpi: int = 150,
) -> list[Path]:
    """
    PDFをページごとにPNGへレンダリングして保存し、出力PNGパスのリストを返す。

    NOTE:
    - openpyxl はPDFを直接貼れないため、画像化して貼り付ける。
    - 依存: PyMuPDF（pymupdf）
    """
    import fitz  # PyMuPDF

    pdf_path = Path(pdf_path)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    doc = fitz.open(str(pdf_path))
    out_paths: list[Path] = []
    try:
        for i in range(doc.page_count):
            page = doc.load_page(i)
            pix = page.get_pixmap(dpi=int(dpi))
            out_path = output_dir / f"{pdf_path.stem}__p{i+1:03}.png"
            pix.save(str(out_path))
            out_paths.append(out_path)
    finally:
        doc.close()

    return out_paths


def _pixel_y_to_excel_row(
    pixel_y: int,
    image_start_row: int,
    image_height_px: int,
    rows_for_image: int,
) -> int:
    """
    画像内のピクセルY座標からExcelの行番号を計算する。

    Args:
        pixel_y: 画像内のY座標（ピクセル、上端=0）
        image_start_row: 画像が配置されている開始行（1-indexed）
        image_height_px: 画像の高さ（ピクセル）
        rows_for_image: 画像が占めるExcel行数

    Returns:
        対応するExcel行番号（1-indexed）
    """
    if image_height_px <= 0:
        return image_start_row
    # 比率でマッピング
    ratio = pixel_y / image_height_px
    row_offset = int(ratio * rows_for_image)
    return image_start_row + row_offset


def add_capture_sheet_with_images(
    excel_path: str,
    images_dir: str = "data/input/sample_image",
    row_height_pt: float = 15.0,
    margin_rows: int = 2,
    output_excel_path: Optional[str] = None,
    annotate_phrases: Optional[dict[str, list[str]]] = None,
    annotate_llm: Any = None,
    annotate_output_dir: str = "data/output/annotated_capture",
    output_excel_sheet_name: str = "キャプチャ",
    annotate_max_iters: int = 3,
    pdf_dpi: int = 150,
    pdf_render_dir: Optional[str] = None,
    annotation_column: str = "A",
    image_column: str = "B",
) -> Path:
    """
    excel_path のExcelに sheet_name を作成し、images_dir 配下の画像をファイル名順に上から貼り付ける。
    既に同名シートがある場合は削除して作り直す。

    NOTE:
    - 画像貼り付けには Pillow が必要です（未インストールの場合は `pip install pillow`）。
    """
    excel_path_p = Path(excel_path)
    images_dir_p = Path(images_dir)

    if not excel_path_p.exists():
        raise FileNotFoundError(f"Excelファイルが見つかりません: {excel_path_p}")
    if not images_dir_p.exists():
        raise FileNotFoundError(f"画像フォルダが見つかりません: {images_dir_p}")

    # 対応拡張子:
    # - 画像: png/jpg/jpeg/bmp/gif/webp/tif/tiff
    # - PDF: pdf（ページごとにPNGへ変換して貼り付け）
    image_exts = {".png", ".jpg", ".jpeg", ".bmp", ".gif", ".webp", ".tif", ".tiff"}
    pdf_paths = sorted(
        [p for p in images_dir_p.iterdir() if p.is_file() and p.suffix.lower() == ".pdf"],
        key=lambda p: p.name,
    )

    # 画像とPDFページを「ファイル名順→PDFはページ順」で並べる
    items: list[tuple[tuple[str, int], Path]] = []
    for p in images_dir_p.iterdir():
        if not p.is_file():
            continue
        if p.suffix.lower() in image_exts:
            items.append(((p.name, 0), p))

    if pdf_paths:
        render_base = (
            Path(pdf_render_dir)
            if pdf_render_dir
            else Path("data/output/pdf_pages")
        )
        for pdf in pdf_paths:
            rendered = render_pdf_to_png_pages(
                pdf,
                output_dir=render_base,
                dpi=pdf_dpi,
            )
            for page_idx, png_path in enumerate(rendered, start=1):
                items.append(((pdf.name, page_idx), png_path))

    image_paths = [p for _, p in sorted(items, key=lambda t: t[0])]
    if not image_paths:
        raise FileNotFoundError(f"画像/PDFが見つかりません: {images_dir_p}")

    wb = openpyxl.load_workbook(excel_path_p)
    if output_excel_sheet_name in wb.sheetnames:
        wb.remove(wb[output_excel_sheet_name])

    ws = wb.create_sheet(title=output_excel_sheet_name)
    ws.sheet_view.showGridLines = False
    # アノテーション列（A列）の幅を設定
    ws.column_dimensions[annotation_column].width = 40
    # 画像列（B列）の幅を設定
    ws.column_dimensions[image_column].width = 120

    # 画像を上から順に配置
    # かぶり防止のため、シート側のデフォルト行高を固定し、その前提で「画像高さ→必要行数」を見積もる。
    ws.sheet_format.defaultRowHeight = row_height_pt
    row = 1
    for image_path in image_paths:
        if not Path(image_path).exists():
            # フォルダ内のファイルが途中で削除された等のケースはスキップ
            continue
        start_row = row
        img_path_for_excel = image_path
        annotation_result: Optional[AnnotationResult] = None

        if annotate_phrases and annotate_llm is not None:
            annotation_result = annotate_image_with_llm_red_boxes(
                llm=annotate_llm,
                image_path=image_path,
                target_phrases=annotate_phrases.get(image_path.name, []),
                output_dir=Path(annotate_output_dir),
                max_iters=annotate_max_iters,
                embed_text_in_image=False,  # 画像にはテキストを埋め込まない
            )
            img_path_for_excel = annotation_result.image_path

        img = XLImage(str(img_path_for_excel))

        # 元画像のサイズを取得（アノテーション位置計算用）
        original_width = annotation_result.image_width if annotation_result else 0
        original_height = annotation_result.image_height if annotation_result else 0
        if original_height == 0:
            with Image.open(image_path) as pil_im:
                original_width, original_height = pil_im.size

        # 横幅が大きすぎる場合だけ縮小（貼り付け後の見栄え用）
        max_width_px = 900
        scale = 1.0
        if getattr(img, "width", None) and img.width > max_width_px:
            scale = max_width_px / float(img.width)
            img.width = int(img.width * scale)
            img.height = int(img.height * scale)

        # 画像をB列（image_column）に配置
        ws.add_image(img, f"{image_column}{row}")

        # 次の画像開始行を計算（px→pt を 96dpi 前提で換算）
        height_px = int(getattr(img, "height", 0) or 0)
        height_pt = (height_px * 72.0) / 96.0 if height_px > 0 else 300.0
        rows_needed = max(1, int((height_pt / row_height_pt) + margin_rows))

        # 行の高さを明示し、テンプレ側の行高差異によるズレを抑える
        for r in range(start_row, start_row + rows_needed):
            ws.row_dimensions[r].height = row_height_pt

        # アノテーションをA列（annotation_column）に配置
        if annotation_result and annotation_result.annotations:
            for ann in annotation_result.annotations:
                # ピクセルY座標からExcel行を計算（元画像の座標系を使用）
                pixel_y = ann.box.y1
                target_row = _pixel_y_to_excel_row(
                    pixel_y=pixel_y,
                    image_start_row=start_row,
                    image_height_px=original_height,
                    rows_for_image=rows_needed - margin_rows,  # マージン分を除く
                )
                # アノテーションテキストを作成
                annotation_text = ann.phrase
                if ann.reason:
                    annotation_text += f"\n({ann.reason})"
                # セルにテキストを設定
                cell = ws[f"{annotation_column}{target_row}"]
                cell.value = annotation_text
                cell.alignment = Alignment(
                    wrap_text=True,
                    vertical="top",
                )
                # 赤色のフォントを設定
                cell.font = Font(
                    color="FF0000",
                    bold=True,
                )

        row += rows_needed

    save_path = Path(output_excel_path) if output_excel_path else excel_path_p
    try:
        save_path.parent.mkdir(parents=True, exist_ok=True)
        wb.save(save_path)
    except PermissionError as e:
        # in-place保存がロック等で失敗する場合に備え、別名保存にフォールバック
        fallback = save_path
        if save_path == excel_path_p:
            fallback = excel_path_p.with_name(f"{excel_path_p.stem}__capture.xlsx")
        try:
            fallback.parent.mkdir(parents=True, exist_ok=True)
            wb.save(fallback)
            return fallback
        except Exception:
            raise PermissionError(
                f"Excelファイルの保存に失敗しました。Excelで '{excel_path_p.name}' を開いている場合は閉じてから再実行してください: {excel_path_p}"
            ) from e

    return save_path


def _image_suffix_to_mime_type(p: Path) -> str:
    ext = p.suffix.lower().lstrip(".")
    if ext in {"jpg", "jpeg"}:
        return "image/jpeg"
    if ext == "png":
        return "image/png"
    if ext == "webp":
        return "image/webp"
    if ext in {"tif", "tiff"}:
        return "image/tiff"
    if ext == "gif":
        return "image/gif"
    if ext == "bmp":
        return "image/bmp"
    # fallback
    return "image/png"


def _encode_image_as_data_url(image_path: Path) -> str:
    mime = _image_suffix_to_mime_type(image_path)
    b64 = base64.b64encode(image_path.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{b64}"


def _coerce_int(v: Any, default: int = 0) -> int:
    try:
        if isinstance(v, bool):
            return default
        if isinstance(v, (int, float)):
            return int(round(float(v)))
        s = str(v).strip()
        if s == "":
            return default
        return int(round(float(s)))
    except Exception:
        return default


def _clamp_box(box: dict[str, Any], width: int, height: int) -> Optional[dict[str, int]]:
    """
    box: {"x1":..,"y1":..,"x2":..,"y2":..} を想定
    """
    x1 = _coerce_int(box.get("x1"), 0)
    y1 = _coerce_int(box.get("y1"), 0)
    x2 = _coerce_int(box.get("x2"), 0)
    y2 = _coerce_int(box.get("y2"), 0)

    x1 = max(0, min(width - 1, x1))
    x2 = max(0, min(width - 1, x2))
    y1 = max(0, min(height - 1, y1))
    y2 = max(0, min(height - 1, y2))

    # 正規化
    if x2 < x1:
        x1, x2 = x2, x1
    if y2 < y1:
        y1, y2 = y2, y1

    # 最小サイズ確保
    if x2 - x1 < 2 or y2 - y1 < 2:
        return None
    return {"x1": x1, "y1": y1, "x2": x2, "y2": y2}


def _draw_red_boxes_on_image(
    image_path: Path,
    boxes: Iterable[dict[str, int]],
    *,
    stroke_width: int = 6,
    padding_px: int = 6,
    annotations: Optional[list[str]] = None,
) -> Image.Image:
    """
    画像に赤いバウンディングボックスと注釈を描画する。
    
    Args:
        image_path: 画像パス
        boxes: バウンディングボックスのリスト [{"x1": int, "y1": int, "x2": int, "y2": int}, ...]
        stroke_width: 枠線の太さ
        padding_px: パディング（px）
        annotations: 各BBに対応する注釈テキストのリスト（Noneの場合は注釈なし）
    """
    im = Image.open(image_path).convert("RGB")
    draw = ImageDraw.Draw(im)
    
    # フォントの準備（日本語対応）
    font_size = 24  # 1.5倍のサイズ（16 * 1.5 = 24）
    font = None
    # Windows環境での日本語フォントを優先的に試す
    japanese_fonts = [
        ("C:/Windows/Fonts/msgothic.ttc", 0),  # MSゴシック（TTC、インデックス0）
        ("C:/Windows/Fonts/meiryo.ttc", 0),    # メイリオ（TTC、インデックス0）
        ("C:/Windows/Fonts/msmincho.ttc", 0),  # MS明朝（TTC、インデックス0）
        ("C:/Windows/Fonts/msgothic.ttf", None),  # MSゴシック（TTF版）
        ("C:/Windows/Fonts/meiryo.ttf", None),    # メイリオ（TTF版）
    ]
    for font_path, font_index in japanese_fonts:
        try:
            if font_index is not None:
                # TTCファイルの場合はインデックスを指定
                font = ImageFont.truetype(font_path, font_size, index=font_index)
            else:
                font = ImageFont.truetype(font_path, font_size)
            break
        except Exception:
            continue
    
    # 日本語フォントが見つからない場合は英語フォントを試す
    if font is None:
        try:
            font = ImageFont.truetype("arial.ttf", font_size)
        except Exception:
            try:
                font = ImageFont.truetype("DejaVuSans.ttf", font_size)
            except Exception:
                font = ImageFont.load_default()
    
    boxes_list = list(boxes)
    annotations_list = annotations if annotations else [None] * len(boxes_list)
    
    for idx, b in enumerate(boxes_list):
        x1 = max(0, b["x1"] - padding_px)
        y1 = max(0, b["y1"] - padding_px)
        x2 = min(im.width - 1, b["x2"] + padding_px)
        y2 = min(im.height - 1, b["y2"] + padding_px)
        # Pillowのrectangleはoutlineのwidth指定が使える
        draw.rectangle([x1, y1, x2, y2], outline=(255, 0, 0), width=stroke_width)
        
        # 注釈テキストを描画
        if idx < len(annotations_list) and annotations_list[idx]:
            annotation_text = annotations_list[idx]
            # 注釈の位置：BBの左上の少し上（上にスペースがない場合はBBの内側）
            text_x = x1
            text_y = max(0, y1 - 35)  # BBの上35px（フォントサイズが大きくなったので調整）
            
            # 改行で分割
            lines = annotation_text.split('\n')
            line_height = 27  # 行の高さ（px、フォントサイズ24に合わせて1.5倍）
            
            # 各行の幅と高さを計算
            max_text_width = 0
            total_text_height = len(lines) * line_height
            
            for line in lines:
                try:
                    bbox = draw.textbbox((text_x, text_y), line, font=font)
                    line_width = bbox[2] - bbox[0]
                except AttributeError:
                    # textbboxが使えない場合はtextsizeを使用（古いPillow）
                    line_width, _ = draw.textsize(line, font=font)
                max_text_width = max(max_text_width, line_width)
            
            # 背景を描画（半透明の白背景）
            bg_padding = 4
            bg_x1 = text_x - bg_padding
            bg_y1 = text_y - bg_padding
            bg_x2 = text_x + max_text_width + bg_padding
            bg_y2 = text_y + total_text_height + bg_padding
            
            # 背景が画像外に出ないように調整
            bg_x1 = max(0, bg_x1)
            bg_y1 = max(0, bg_y1)
            bg_x2 = min(im.width - 1, bg_x2)
            bg_y2 = min(im.height - 1, bg_y2)
            
            # 半透明の背景を描画（RGBAモードで描画してから合成）
            overlay = Image.new("RGBA", im.size, (0, 0, 0, 0))
            overlay_draw = ImageDraw.Draw(overlay)
            overlay_draw.rectangle(
                [bg_x1, bg_y1, bg_x2, bg_y2],
                fill=(255, 255, 255, 220),  # 半透明の白
                outline=(255, 0, 0, 255),  # 赤い枠線
                width=2,
            )
            im = Image.alpha_composite(im.convert("RGBA"), overlay).convert("RGB")
            draw = ImageDraw.Draw(im)
            
            # テキストを各行ごとに描画（赤色）
            current_y = text_y
            for line in lines:
                if line.strip():  # 空行はスキップ
                    draw.text((text_x, current_y), line, fill=(255, 0, 0), font=font)
                current_y += line_height
    
    return im


def _save_image_as_png(im: Image.Image, output_path: Path) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    im.save(output_path, format="PNG")
    return output_path


def _add_pixel_ruler_overlay(
    im: Image.Image,
    *,
    band_px: int = 36,
    tick_step_px: int = 50,
    major_step_px: int = 200,
    grid_step_px: int = 50,
    grid_alpha: int = 40,
    major_grid_alpha: int = 130,
    font_size: int = 16,
    boxes: Optional[list[dict[str, int]]] = None,
) -> Image.Image:
    """
    画像に「ピクセル目盛り（定規）」を重ねる。

    - 画像サイズは変えない（座標系を維持）
    - 上下左右に半透明の帯を敷き、tickとラベルを描く
    - 薄いグリッドも重ねる（座標の“物差し”）
    - boxesが指定された場合、それらを完全に含む200px単位のグリッド線を赤くする
    """
    import math
    
    base = im.convert("RGBA")
    overlay = Image.new("RGBA", base.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)

    w, h = base.size
    band_px = max(16, int(band_px))
    tick_step_px = max(10, int(tick_step_px))
    major_step_px = max(tick_step_px, int(major_step_px))
    grid_step_px = max(10, int(grid_step_px))
    grid_alpha = max(0, min(255, int(grid_alpha)))
    major_grid_alpha = max(0, min(255, int(major_grid_alpha)))
    font_size = max(10, int(font_size))

    # フォント（Windows想定で Arial を優先し、なければ既定フォント）
    try:
        font = ImageFont.truetype("arial.ttf", font_size)
    except Exception:
        try:
            font = ImageFont.truetype("DejaVuSans.ttf", font_size)
        except Exception:
            font = ImageFont.load_default()
    
    # 大きなフォント（ボックス関連のメモリ用）
    try:
        large_font = ImageFont.truetype("arial.ttf", int(font_size * 1.5))
    except Exception:
        try:
            large_font = ImageFont.truetype("DejaVuSans.ttf", int(font_size * 1.5))
        except Exception:
            large_font = font

    # バウンディングボックスを完全に含む200px単位のグリッド線を特定
    highlighted_x_lines: set[int] = set()
    highlighted_y_lines: set[int] = set()
    if boxes:
        for box in boxes:
            x1 = box.get("x1", 0)
            y1 = box.get("y1", 0)
            x2 = box.get("x2", 0)
            y2 = box.get("y2", 0)
            # ボックスを完全に含む最小の200px単位のグリッド範囲を計算
            x_min = math.floor(x1 / major_step_px) * major_step_px
            x_max = math.ceil(x2 / major_step_px) * major_step_px
            y_min = math.floor(y1 / major_step_px) * major_step_px
            y_max = math.ceil(y2 / major_step_px) * major_step_px
            # 範囲内の200px単位のグリッド線を追加
            for x in range(x_min, x_max + 1, major_step_px):
                if 0 <= x < w:
                    highlighted_x_lines.add(x)
            for y in range(y_min, y_max + 1, major_step_px):
                if 0 <= y < h:
                    highlighted_y_lines.add(y)

    # 薄いグリッド（内容を隠さないよう薄く）
    for x in range(0, w, grid_step_px):
        a = major_grid_alpha if (x % major_step_px) == 0 else grid_alpha
        draw.line([(x, 0), (x, h - 1)], fill=(0, 0, 0, a), width=1)
    for y in range(0, h, grid_step_px):
        a = major_grid_alpha if (y % major_step_px) == 0 else grid_alpha
        draw.line([(0, y), (w - 1, y)], fill=(0, 0, 0, a), width=1)
    
    # バウンディングボックスを完全に含む200px単位のグリッド線を赤く描画
    for x in highlighted_x_lines:
        draw.line([(x, 0), (x, h - 1)], fill=(255, 0, 0, 200), width=2)
    for y in highlighted_y_lines:
        draw.line([(0, y), (w - 1, y)], fill=(255, 0, 0, 200), width=2)

    # 半透明の帯（上・下・左・右）
    band_fill = (255, 255, 255, 170)
    draw.rectangle([0, 0, w - 1, band_px], fill=band_fill)  # top
    draw.rectangle([0, h - band_px, w - 1, h - 1], fill=band_fill)  # bottom
    draw.rectangle([0, 0, band_px, h - 1], fill=band_fill)  # left
    draw.rectangle([w - band_px, 0, w - 1, h - 1], fill=band_fill)  # right

    # 目盛り（上/下：x、左/右：y）
    for x in range(0, w, tick_step_px):
        is_major = (x % major_step_px) == 0
        is_highlighted = x in highlighted_x_lines
        tick_len = band_px - 6 if is_major else (band_px // 2)
        tick_color = (255, 0, 0, 255) if is_highlighted else (0, 0, 0, 255)
        tick_width = 3 if is_highlighted else (2 if is_major else 1)
        # top
        y0 = band_px - tick_len
        draw.line([(x, y0), (x, band_px)], fill=tick_color, width=tick_width)
        # bottom
        y1 = h - band_px
        draw.line([(x, y1), (x, y1 + tick_len)], fill=tick_color, width=tick_width)
        if is_major:
            text_font = large_font if is_highlighted else font
            text_color = (255, 0, 0, 255) if is_highlighted else (0, 0, 0, 255)
            draw.text((x + 2, 2), str(x), fill=text_color, font=text_font)
            draw.text((x + 2, h - band_px + 2), str(x), fill=text_color, font=text_font)

    for y in range(0, h, tick_step_px):
        is_major = (y % major_step_px) == 0
        is_highlighted = y in highlighted_y_lines
        tick_len = band_px - 6 if is_major else (band_px // 2)
        tick_color = (255, 0, 0, 255) if is_highlighted else (0, 0, 0, 255)
        tick_width = 3 if is_highlighted else (2 if is_major else 1)
        # left
        x0 = band_px - tick_len
        draw.line([(x0, y), (band_px, y)], fill=tick_color, width=tick_width)
        # right
        x1 = w - band_px
        draw.line([(x1, y), (x1 + tick_len, y)], fill=tick_color, width=tick_width)
        if is_major:
            text_font = large_font if is_highlighted else font
            text_color = (255, 0, 0, 255) if is_highlighted else (0, 0, 0, 255)
            draw.text((2, y + 2), str(y), fill=text_color, font=text_font)
            draw.text((w - band_px + 2, y + 2), str(y), fill=text_color, font=text_font)

    # 原点ラベル
    draw.text((2, 2), "0,0", fill=(200, 0, 0, 255), font=font)
    draw.text((w - band_px + 2, h - band_px + 2), f"{w-1},{h-1}", fill=(200, 0, 0, 255), font=font)

    return Image.alpha_composite(base, overlay).convert("RGB")


def _save_ruler_image(
    *,
    image_path: Path,
    output_path: Path,
    band_px: int = 36,
    tick_step_px: int = 50,
    major_step_px: int = 200,
    grid_step_px: int = 50,
    grid_alpha: int = 40,
    major_grid_alpha: int = 130,
    font_size: int = 16,
    boxes: Optional[list[dict[str, int]]] = None,
) -> Path:
    im = Image.open(image_path).convert("RGB")
    ruled = _add_pixel_ruler_overlay(
        im,
        band_px=band_px,
        tick_step_px=tick_step_px,
        major_step_px=major_step_px,
        grid_step_px=grid_step_px,
        grid_alpha=grid_alpha,
        major_grid_alpha=major_grid_alpha,
        font_size=font_size,
        boxes=boxes,
    )
    return _save_image_as_png(ruled, output_path)


class BoundingBox(BaseModel):
    """画像のピクセル座標（左上原点）。"""

    x1: int = Field(..., description="left (px)")
    y1: int = Field(..., description="top (px)")
    x2: int = Field(..., description="right (px)")
    y2: int = Field(..., description="bottom (px)")


class AnnotationInfo(BaseModel):
    """アノテーション情報（Excelテキスト挿入用）。"""
    phrase: str = Field(..., description="検出した文言")
    reason: str = Field("", description="根拠")
    box: BoundingBox = Field(..., description="バウンディングボックス")


class AnnotationResult(BaseModel):
    """annotate_image_with_llm_red_boxes の戻り値。"""
    image_path: Path = Field(..., description="処理後の画像パス")
    annotations: list[AnnotationInfo] = Field(default_factory=list, description="アノテーション情報のリスト")
    image_width: int = Field(0, description="画像の幅（px）")
    image_height: int = Field(0, description="画像の高さ（px）")

    class Config:
        arbitrary_types_allowed = True


class PhraseMatch(BaseModel):
    phrase: str = Field(..., description="検出した文言（target_phrasesのうち最も近いもの）")
    box: BoundingBox
    reason: str = Field("", description="根拠（任意）")


class FindPhraseBoundingBoxesOutput(BaseModel):
    contains: bool
    matches: list[PhraseMatch] = Field(default_factory=list)


class VerifyBoxesOutput(BaseModel):
    ok: bool
    corrected_boxes: list[BoundingBox] = Field(default_factory=list)


def _invoke_structured_output(llm: Any, schema: Any, messages: list[Any]) -> Any:
    """
    LangChainの structured output を使ってschemaに沿った出力を得る。
    実装差異（schemaを位置引数/kwで渡す等）を吸収する。
    """
    # 1) Pydanticモデルを直接渡す（OpenAI互換）
    try:
        structured_llm = llm.with_structured_output(schema, method="json_schema")
        return structured_llm.invoke(messages)
    except TypeError:
        pass

    # 2) schema= で渡す
    try:
        structured_llm = llm.with_structured_output(schema=schema, method="json_schema")
        return structured_llm.invoke(messages)
    except TypeError:
        pass

    # 3) JSON schema(dict)で渡す（戻り値はdict想定）
    try:
        json_schema = schema.model_json_schema() if hasattr(schema, "model_json_schema") else schema
        structured_llm = llm.with_structured_output(schema=json_schema, method="json_schema")
        return structured_llm.invoke(messages)
    except Exception as e:
        raise RuntimeError("structured output の初期化に失敗しました") from e


def llm_find_phrase_bounding_boxes(
    *,
    llm: Any,
    image_path: Path,
    target_phrases: list[str],
) -> dict[str, Any]:
    """
    画像内に target_phrases のいずれかが含まれるかをLLM(Vision)で判定し、
    含まれる場合は bbox（ピクセル座標）を返す。

    戻り値（期待）:
    {
      "contains": true/false,
      "matches": [{"phrase": "...", "box": {"x1":..,"y1":..,"x2":..,"y2":..}, "reason": "..."}]
    }
    """
    from langchain_core.messages import HumanMessage

    with Image.open(image_path) as im:
        width, height = im.size

    phrases_str = "\n".join([f"- {p}" for p in target_phrases])
    data_url = _encode_image_as_data_url(image_path)
    print(data_url)

    prompt = f"""
あなたは画像を見て特定の情報が含まれるバウンディングボックスを特定するタスクを行っています。
# 作業ステップ
Step1: 以下の情報が画像内に含まれているか確認してください。
- 対象情報:
{phrases_str}

Step2: Step1で特定した情報が含まれるバウンディングボックス（各座標50px単位で指定）を全て回答してください。

# 対象の画像についての情報
- 画像サイズ: width={width}, height={height}
- 画像には「ピクセル定規」と「薄いグリッド」が重ねられています（上下左右=目盛り、200pxごとに数値ラベル、グリッドは50px間隔）。
- 定規は座標系の把握のためのもので、対象文言そのものではありません。

# 出力形式
JSON schema:
{{
  "contains": boolean,
  "matches": [
    {{
      "phrase": string,
      "box": {{"x1": number, "y1": number, "x2": number, "y2": number}},
      "reason": string
    }}
  ]
}}

ルール:
- 座標は画像のピクセル座標（左上原点）で50px単位で返してください。
- 対象の情報がいずれも含まれていない場合は contains=false かつ matches=[]
- 対象の情報がいずれかが含まれている場合は contains=true かつ matchesに該当箇所をすべて入れる
- phraseは対象の情報のうち最も近いものを入れる
""".strip()

    print(prompt)

    msg = HumanMessage(
        content=[
            {"type": "text", "text": prompt},
            # OpenAI互換の形式（data URL）
            {"type": "image_url", "image_url": {"url": data_url}},
        ]
    )

    out = _invoke_structured_output(llm, FindPhraseBoundingBoxesOutput, [msg])
    if isinstance(out, BaseModel):
        print(out.model_dump())
        return out.model_dump()
    if isinstance(out, dict):
        print(out.model_dump())
        return out
    raise ValueError(f"structured output の戻り値が想定外です: type={type(out).__name__}, content={out!r}")


def llm_verify_boxes_on_annotated_image(
    *,
    llm: Any,
    original_image_path: Path,
    current_boxes: list[dict[str, int]],
    boxed_image_path: Path,
    target_phrases: list[str],
) -> dict[str, Any]:
    """
    original と boxed を比較し、赤枠が適切かをLLM(Vision)で検証する。
    OKなら {"ok": true}、NGなら修正版のboxesを返す。
    """
    from langchain_core.messages import HumanMessage

    with Image.open(original_image_path) as im:
        width, height = im.size

    phrases_str = "\n".join([f"- {p}" for p in target_phrases])
    original_url = _encode_image_as_data_url(original_image_path)
    boxed_url = _encode_image_as_data_url(boxed_image_path)

    prompt = f"""
あなたは画像を見て特定の情報が含まれるバウンディングボックスが正しく赤枠で囲われているかを評価するタスクを行っています。
# 作業ステップ
Step1: 以下の情報が画像内に含まれているか確認してください。
- 対象情報:
{phrases_str}

Step2: Step1で特定した情報が赤枠に全て囲われているかを評価してください。
※全ての情報が赤枠に入っていれば、余白が含まれていても厳密に指摘する必要はありません。

Step3: Step2で評価した結果、対象の一部分しか赤枠で囲えていない箇所は、目盛りを参考に全ての情報が入るように調整してください。
※50px単位で座標を調整してください。

# 対象の画像についての情報
- 画像サイズ: width={width}, height={height}
- 画像には「ピクセル定規」と「薄いグリッド」が重ねられています（上下左右=目盛り、200pxごとに数値ラベル、グリッドは50px間隔）。
- 現在描画されている赤枠のバウンディングボックス:
{json.dumps(current_boxes, indent=2)}

# 出力形式
{{
  "ok": boolean,
  "corrected_boxes": [
    {{"x1": number, "y1": number, "x2": number, "y2": number}}
  ]
}}

ルール:
- 十分に正しければ ok=true, corrected_boxes=[]
- 不十分なら ok=false とし、正しい赤枠の座標（元画像のピクセル座標）を corrected_boxes に列挙
- 修正不要な赤枠についても corrected_boxes に入れてください。
""".strip()

    msg = HumanMessage(
        content=[
            {"type": "text", "text": prompt},
            {"type": "image_url", "image_url": {"url": original_url}},
            {"type": "image_url", "image_url": {"url": boxed_url}},
        ]
    )

    out = _invoke_structured_output(llm, VerifyBoxesOutput, [msg])
    if isinstance(out, BaseModel):
        print(out.model_dump())
        return out.model_dump()
    if isinstance(out, dict):
        print(out)
        return out
    raise ValueError(f"structured output の戻り値が想定外です: type={type(out).__name__}, content={out!r}")


def annotate_image_with_llm_red_boxes(
    *,
    llm: Any,
    image_path: Path,
    target_phrases: list[str],
    output_dir: Path,
    max_iters: int = 3,
    stroke_width: int = 6,
    padding_px: int = 6,
    embed_text_in_image: bool = False,
) -> AnnotationResult:
    """
    フロー:
    1) LLMで対象文言の有無を判定し、あればbboxを取得
    2) bboxに赤枠を描画してPNGとして保存
    3) LLMで赤枠の妥当性を再確認し、NGなら修正bboxで描き直し
    """
    image_path = Path(image_path)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    with Image.open(image_path) as im:
        width, height = im.size

    # LLM入力用に「定規付き」画像を用意（座標系は変えない）
    ruler_original_path = output_dir / f"{image_path.stem}__ruler.png"
    _save_ruler_image(image_path=image_path, output_path=ruler_original_path)

    # 1) 検出（定規付き画像を入力にする）
    det = llm_find_phrase_bounding_boxes(llm=llm, image_path=ruler_original_path, target_phrases=target_phrases)
    if not det.get("contains"):
        return AnnotationResult(
            image_path=image_path,
            annotations=[],
            image_width=width,
            image_height=height,
        )

    boxes_raw = []
    annotations_raw = []  # 各BBに対応する注釈テキスト
    for m in det.get("matches", []):
        if isinstance(m, dict) and isinstance(m.get("box"), dict):
            b = _clamp_box(m["box"], width, height)
            if b is not None:
                boxes_raw.append(b)
                # 注釈テキストを作成（phraseとreasonを組み合わせ）
                phrase = m.get("phrase", "")
                reason = m.get("reason", "")
                if phrase and reason:
                    annotation = f"{phrase}\n({reason})"
                elif phrase:
                    annotation = phrase
                elif reason:
                    annotation = reason
                else:
                    annotation = ""
                annotations_raw.append(annotation)
    if not boxes_raw:
        return AnnotationResult(
            image_path=image_path,
            annotations=[],
            image_width=width,
            image_height=height,
        )

    # 描画→検証→修正ループ
    #
    # 目的:
    # - `ok=False` が続くときにどの段階で何が起きているか追えるよう、
    #   各イテレーションの「断面」画像をすべて保存する。
    #
    # 保存先:
    # - <stem>__boxed_iter01.png, <stem>__boxed_iter02.png, ...
    # - <stem>__boxed_final.png（最後の採用結果）
    current_boxes = boxes_raw
    current_annotations = annotations_raw  # 注釈情報も保持
    iters = max(1, int(max_iters))

    last_iter_path: Optional[Path] = None
    for i in range(iters):
        iter_path = output_dir / f"{image_path.stem}__boxed_iter{i+1:02}.png"
        # 検証ループ中は注釈なしで描画（ノイズを避けるため）
        boxed_im = _draw_red_boxes_on_image(
            image_path,
            current_boxes,
            stroke_width=stroke_width,
            padding_px=padding_px,
            annotations=None,  # 注釈なし
        )
        _save_image_as_png(boxed_im, iter_path)
        last_iter_path = iter_path

        # 検証用：定規を重ねた断面画像も保存（2回目以降はバウンディングボックス情報を渡す）
        iter_ruler_path = output_dir / f"{image_path.stem}__boxed_iter{i+1:02}__ruler.png"
        _save_ruler_image(image_path=iter_path, output_path=iter_ruler_path, boxes=current_boxes if i > 0 else None)

        ver = llm_verify_boxes_on_annotated_image(
            llm=llm,
            current_boxes=current_boxes,
            original_image_path=ruler_original_path,
            boxed_image_path=iter_ruler_path,
            target_phrases=target_phrases,
        )
        if bool(ver.get("ok")):
            # OKになった断面を最終として採用
            final_path = output_dir / f"{image_path.stem}__boxed_final.png"
            if final_path != iter_path:
                final_im = Image.open(iter_path).convert("RGB")
                _save_image_as_png(final_im, final_path)

            # 定規付きfinalも保存（バウンディングボックス情報を渡す）
            final_ruler_path = output_dir / f"{image_path.stem}__boxed_final__ruler.png"
            _save_ruler_image(image_path=final_path, output_path=final_ruler_path, boxes=current_boxes)

            # アノテーション情報を構築
            result_annotations = []
            for j, box in enumerate(current_boxes):
                annotation_text = current_annotations[j] if j < len(current_annotations) else ""
                # annotation_textは "phrase\n(reason)" の形式なのでパース
                parts = annotation_text.split('\n', 1)
                phrase = parts[0] if parts else ""
                reason = parts[1].strip("()") if len(parts) > 1 else ""
                result_annotations.append(AnnotationInfo(
                    phrase=phrase,
                    reason=reason,
                    box=BoundingBox(x1=box["x1"], y1=box["y1"], x2=box["x2"], y2=box["y2"]),
                ))

            # embed_text_in_image=Trueの場合は注釈付き画像を生成
            if embed_text_in_image:
                final_annotated_path = output_dir / f"{image_path.stem}__boxed_final_annotated.png"
                final_annotations_text = []
                for j, box in enumerate(current_boxes):
                    if j < len(current_annotations):
                        final_annotations_text.append(current_annotations[j])
                    else:
                        final_annotations_text.append("")
                final_annotated_im = _draw_red_boxes_on_image(
                    image_path,
                    current_boxes,
                    stroke_width=stroke_width,
                    padding_px=padding_px,
                    annotations=final_annotations_text,
                )
                _save_image_as_png(final_annotated_im, final_annotated_path)
                return AnnotationResult(
                    image_path=final_annotated_path,
                    annotations=result_annotations,
                    image_width=width,
                    image_height=height,
                )

            return AnnotationResult(
                image_path=final_path,
                annotations=result_annotations,
                image_width=width,
                image_height=height,
            )

        corrected: list[dict[str, int]] = []
        for b in (ver.get("corrected_boxes") or [])[:10]:
            if isinstance(b, dict):
                cb = _clamp_box(b, width, height)
                if cb is not None:
                    corrected.append(cb)
        if not corrected:
            # 修正が取れないならこの断面を最終として採用
            final_path = output_dir / f"{image_path.stem}__boxed_final.png"
            if last_iter_path and final_path != last_iter_path:
                final_im = Image.open(last_iter_path).convert("RGB")
                _save_image_as_png(final_im, final_path)
            final_ruler_path = output_dir / f"{image_path.stem}__boxed_final__ruler.png"
            if final_path.exists():
                _save_ruler_image(image_path=final_path, output_path=final_ruler_path, boxes=current_boxes)

            # アノテーション情報を構築
            result_annotations = []
            for j, box in enumerate(current_boxes):
                annotation_text = current_annotations[j] if j < len(current_annotations) else ""
                parts = annotation_text.split('\n', 1)
                phrase = parts[0] if parts else ""
                reason = parts[1].strip("()") if len(parts) > 1 else ""
                result_annotations.append(AnnotationInfo(
                    phrase=phrase,
                    reason=reason,
                    box=BoundingBox(x1=box["x1"], y1=box["y1"], x2=box["x2"], y2=box["y2"]),
                ))

            # embed_text_in_image=Trueの場合は注釈付き画像を生成
            if embed_text_in_image:
                final_annotated_path = output_dir / f"{image_path.stem}__boxed_final_annotated.png"
                final_annotations_text = []
                for j, box in enumerate(current_boxes):
                    if j < len(current_annotations):
                        final_annotations_text.append(current_annotations[j])
                    else:
                        final_annotations_text.append("")
                final_annotated_im = _draw_red_boxes_on_image(
                    image_path,
                    current_boxes,
                    stroke_width=stroke_width,
                    padding_px=padding_px,
                    annotations=final_annotations_text,
                )
                _save_image_as_png(final_annotated_im, final_annotated_path)
                return AnnotationResult(
                    image_path=final_annotated_path,
                    annotations=result_annotations,
                    image_width=width,
                    image_height=height,
                )

            return AnnotationResult(
                image_path=final_path,
                annotations=result_annotations,
                image_width=width,
                image_height=height,
            )

        # corrected_boxes が「一部だけ修正」だった場合に、未修正のBBは維持する。
        # - correctedがcurrentより少ない: 近いBBだけ差し替え、残りはそのまま
        # - correctedがcurrent以上: 近いBBを差し替え、余りは追加（最大10）
        def _center(b: dict[str, int]) -> tuple[float, float]:
            return ((b["x1"] + b["x2"]) / 2.0, (b["y1"] + b["y2"]) / 2.0)

        def _dist2(a: dict[str, int], b: dict[str, int]) -> float:
            ax, ay = _center(a)
            bx, by = _center(b)
            dx = ax - bx
            dy = ay - by
            return dx * dx + dy * dy

        new_boxes = list(current_boxes)
        new_annotations = list(current_annotations)  # 注釈も同様に更新
        used_idx: set[int] = set()
        for cb in corrected:
            # もっとも近い既存BBを1つだけ置換（既に使ったBBは避ける）
            best_j = None
            best_d = None
            for j, ob in enumerate(new_boxes):
                if j in used_idx:
                    continue
                d = _dist2(cb, ob)
                if best_d is None or d < best_d:
                    best_d = d
                    best_j = j
            if best_j is not None:
                new_boxes[best_j] = cb
                # 注釈は維持（修正後のboxでも元の注釈を使用）
                used_idx.add(best_j)
            else:
                # 置換先がないなら追加（注釈は空文字列）
                new_boxes.append(cb)
                new_annotations.append("")

        current_boxes = new_boxes[:10]
        current_annotations = new_annotations[:10]  # 注釈も同様に制限

    # ここまで来たら「max_iters回の検証でokにならなかった」。
    # 最後の修正案を反映した画像を final として保存（再検証はしない）。
    final_path = output_dir / f"{image_path.stem}__boxed_final.png"
    boxed_im = _draw_red_boxes_on_image(
        image_path,
        current_boxes,
        stroke_width=stroke_width,
        padding_px=padding_px,
        annotations=None,  # 赤枠のみ
    )
    _save_image_as_png(boxed_im, final_path)
    final_ruler_path = output_dir / f"{image_path.stem}__boxed_final__ruler.png"
    _save_ruler_image(image_path=final_path, output_path=final_ruler_path, boxes=current_boxes)

    # アノテーション情報を構築
    result_annotations = []
    for j, box in enumerate(current_boxes):
        annotation_text = current_annotations[j] if j < len(current_annotations) else ""
        parts = annotation_text.split('\n', 1)
        phrase = parts[0] if parts else ""
        reason = parts[1].strip("()") if len(parts) > 1 else ""
        result_annotations.append(AnnotationInfo(
            phrase=phrase,
            reason=reason,
            box=BoundingBox(x1=box["x1"], y1=box["y1"], x2=box["x2"], y2=box["y2"]),
        ))

    # embed_text_in_image=Trueの場合は注釈付き画像を生成
    if embed_text_in_image:
        final_annotated_path = output_dir / f"{image_path.stem}__boxed_final_annotated.png"
        final_annotations_text = []
        for j, box in enumerate(current_boxes):
            if j < len(current_annotations):
                final_annotations_text.append(current_annotations[j])
            else:
                final_annotations_text.append("")
        final_annotated_im = _draw_red_boxes_on_image(
            image_path,
            current_boxes,
            stroke_width=stroke_width,
            padding_px=padding_px,
            annotations=final_annotations_text,
        )
        _save_image_as_png(final_annotated_im, final_annotated_path)
        return AnnotationResult(
            image_path=final_annotated_path,
            annotations=result_annotations,
            image_width=width,
            image_height=height,
        )

    return AnnotationResult(
        image_path=final_path,
        annotations=result_annotations,
        image_width=width,
        image_height=height,
    )

# %%
def capture_insert_sheet(
    excel_path: str,
    images_dir: str = "data/input/sample_image",
    phrases_to_box: list[str] = [],
    row_height_pt: float = 15.0,
    margin_rows: int = 2,
    output_excel_path: Optional[str] = None,
    output_excel_sheet_name: str = "キャプチャ",
) -> Path:
    llm_complex = build_llm(model="gpt-5.2")
    output_path = add_capture_sheet_with_images(
        excel_path,
        annotate_phrases=phrases_to_box,
        annotate_llm=llm_complex,  # gpt-5.2 を想定（Vision対応）
        annotate_output_dir="data/output/annotated_capture",
        annotate_max_iters=3,
        output_excel_path=output_excel_path,
        output_excel_sheet_name=output_excel_sheet_name,
    )
    print(f"キャプチャ付きExcelを出力しました: {output_path}")


if __name__ == "__main__":
    file_path = "data/input/複数シート型1_セクション別シート.xlsx"
    # 例: 画像内にこれらの文言が含まれていたら赤枠を付ける
    phrases_to_box = {
        "仕訳定義書__p001.png": [
            "<借⽅勘定科⽬についての特別ルール>",
        ],
        "invoice_pattern5_slight_texture.png": [
            "<合計金額を表す箇所>",
            "<押印が含まれる箇所>",
        ],
    }

    output_path = capture_insert_sheet(
        file_path,
        phrases_to_box=phrases_to_box,
        output_excel_path="data/output/複数シート型1_セクション別シート__capture.xlsx",
    )
    print(f"Completed!")

# %%
