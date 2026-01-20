# -*- coding: utf-8 -*-
"""
PDFをテキストに変換（LLMによるブロック並び順決定付き）

使い方:
    # 全ページ処理
    python -m src.pdf_to_text_llm data/input/富士フィルム_統合報告書.pdf
    
    # ページ範囲指定（7〜10ページ）
    python -m src.pdf_to_text_llm data/input/富士フィルム_統合報告書.pdf --start 7 --end 10
    
    # バッチサイズ指定
    python -m src.pdf_to_text_llm data/input/富士フィルム_統合報告書.pdf --batch-size 3
"""

import asyncio
import argparse
import base64
import re
import os
from pathlib import Path
from typing import Optional

import fitz
from langchain_core.messages import SystemMessage, HumanMessage
from pydantic import BaseModel, Field

from src.utils import build_llm


# =============================================================================
# 出力スキーマ（型安全）
# =============================================================================

class BlockInfo(BaseModel):
    """ブロック情報"""
    id: int = Field(description="ブロック番号")
    type: str = Field(description="ブロックタイプ: H=ヘッダー, S=見出し, T=本文")


class PageBlocksOrder(BaseModel):
    """ページのブロック並び順"""
    blocks: list[BlockInfo] = Field(description="並び替えたブロックのリスト")
    has_visual_elements: bool = Field(
        default=False,
        description="テキストだけでは表現できない図表・グラフ・画像等が含まれる場合はTrue"
    )
    has_agenda: bool = Field(
        default=False,
        description="目次と思われる箇所があればTrue"
    )

# =============================================================================
# LLM設定
# =============================================================================

def get_llm(model: Optional[str] = None):
    """LLMインスタンスを取得"""
    model_name = (
        model
        or os.getenv("PDF_LLM_MODEL")
        or os.getenv("AZURE_OPENAI_DEPLOYMENT_NAME_COMPLEX")
        or os.getenv("GEMINI_MODEL")
        or os.getenv("OPENAI_MODEL")
        or os.getenv("MODEL")
    )
    return build_llm(model=model_name) if model_name else build_llm()


# =============================================================================
# ブロック情報抽出
# =============================================================================

def extract_page_blocks(doc: fitz.Document, page_number: int) -> dict:
    """
    1ページのブロック情報を抽出
    
    Args:
        doc: PyMuPDFのドキュメントオブジェクト
        page_number: ページ番号（1始まり）
    
    Returns:
        {
            "page": ページ番号,
            "width": ページ幅,
            "height": ページ高さ,
            "blocks": [ブロック情報のリスト],
            "prompt_text": LLM用のプロンプトテキスト
        }
    """
    fitz_page = doc[page_number - 1]
    text_dict = fitz_page.get_text("dict")
    
    blocks = []
    prompt_lines = []
    prompt_lines.append(f"=== ページ {page_number} のブロック情報 ===")
    prompt_lines.append(f"ページサイズ: 幅={fitz_page.rect.width:.1f}, 高さ={fitz_page.rect.height:.1f}")
    
    block_id = 0
    for block in text_dict["blocks"]:
        if block["type"] != 0:  # テキストブロックのみ
            continue
        
        block_id += 1
        bbox = block["bbox"]
        
        # ブロック内の全テキストと書式情報を取得
        block_text = ""
        font_sizes = set()
        font_names = set()
        is_bold = False
        
        for line in block.get("lines", []):
            line_text = ""
            for span in line.get("spans", []):
                line_text += span.get("text", "")
                font_sizes.add(round(span.get("size", 0), 1))
                font_names.add(span.get("font", ""))
                flags = span.get("flags", 0)
                if flags & 16:  # bit 4 = bold
                    is_bold = True
            block_text += line_text
        
        # ブロック情報を保存
        block_info = {
            "id": block_id,
            "x0": bbox[0],
            "y0": bbox[1],
            "x1": bbox[2],
            "y1": bbox[3],
            "sizes": list(font_sizes),
            "fonts": list(font_names),
            "bold": is_bold,
            "text": block_text,
        }
        blocks.append(block_info)
        
        # プロンプト用テキストを生成
        size_str = "/".join(str(s) for s in sorted(font_sizes))
        style_str = "太字" if is_bold else "標準"
        prompt_lines.append("-" * 80)
        prompt_lines.append(f"ブロック {block_id:2d}:")
        prompt_lines.append(f"  座標: x0={bbox[0]:6.1f}, y0={bbox[1]:6.1f}, x1={bbox[2]:6.1f}, y1={bbox[3]:6.1f}")
        prompt_lines.append(f"  書式: サイズ={size_str}pt, スタイル={style_str}")
        prompt_lines.append(f"  テキスト: {block_text}")
    
    prompt_lines.append("-" * 80)
    prompt_lines.insert(2, f"テキストブロック数: {block_id}")
    
    # ページ画像をBase64エンコード（マルチモーダル用）
    pix = fitz_page.get_pixmap(dpi=100)  # DPIを下げてトークン削減
    img_bytes = pix.tobytes("png")
    img_base64 = base64.b64encode(img_bytes).decode()
    
    return {
        "page": page_number,
        "width": fitz_page.rect.width,
        "height": fitz_page.rect.height,
        "blocks": blocks,
        "prompt_text": "\n".join(prompt_lines),
        "image_base64": img_base64,
    }


# =============================================================================
# LLM並び順決定
# =============================================================================

SYSTEM_PROMPT = """与えられたPDFのブロック情報を意味がつながる順に並び替えてください。
座標(x0,y0,x1,y1)とテキスト内容から判断してください。

ブロックタイプ:
- H: ヘッダー（ページ上部のナビゲーション等）
- S: 見出し（セクションタイトル）
- F: フッター（ページ下部のナビゲーション等）
- T: 本文

追加判定:
- テキストだけでは表現できない図表・グラフ・画像・チャート等が含まれる場合、has_visual_elements=True
- 目次と思われる箇所があれば、has_agenda=True
"""


SYSTEM_PROMPT_WITH_IMAGE = """PDFページの画像とブロック情報を見て、意味がつながる順に並び替えてください。
画像のレイアウトを参考に、座標とテキスト内容から判断してください。

ブロックタイプ:
- H: ヘッダー（ページ上部のナビゲーション等）
- S: 見出し（セクションタイトル）
- F: フッター（ページ下部のナビゲーション等）
- T: 本文

視覚要素の判定:
- テキストだけでは表現できない図表・グラフ・画像・チャート等が含まれる場合、has_visual_elements=True
- 目次と思われる箇所があれば、has_agenda=True
"""


async def determine_block_order(page_data: dict, llm, use_image: bool = False) -> dict:
    """
    LLMでブロックの並び順とタイプを決定（型安全）
    
    Args:
        page_data: extract_page_blocksの戻り値
        llm: LangChainのLLMインスタンス
        use_image: Trueの場合、ページ画像も一緒に送信
    
    Returns:
        {
            "page": ページ番号,
            "ordered_blocks": [
                {"id": 70, "type": "H", "text": "..."},
                {"id": 57, "type": "S", "text": "..."},
                ...
            ]
        }
    """
    if not page_data["blocks"]:
        return {"page": page_data["page"], "ordered_blocks": []}
    
    # structured outputを使用
    structured_llm = llm.with_structured_output(PageBlocksOrder)
    
    # メッセージ作成
    if use_image and "image_base64" in page_data:
        # マルチモーダル: 画像 + テキスト
        messages = [
            SystemMessage(content=SYSTEM_PROMPT_WITH_IMAGE),
            HumanMessage(content=[
                {"type": "text", "text": page_data["prompt_text"]},
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{page_data['image_base64']}"}},
            ]),
        ]
    else:
        # テキストのみ
        messages = [
            SystemMessage(content=SYSTEM_PROMPT),
            HumanMessage(content=page_data["prompt_text"]),
        ]
    
    # LLM呼び出し
    result: PageBlocksOrder = await structured_llm.ainvoke(messages)
    
    # 結果をマッピング
    ordered_blocks = []
    block_dict = {b["id"]: b for b in page_data["blocks"]}
    
    for block_info in result.blocks:
        if block_info.id in block_dict:
            src_block = block_dict[block_info.id]
            ordered_blocks.append({
                "id": block_info.id,
                "type": block_info.type,
                "text": src_block["text"],
                "bold": src_block["bold"],
                "sizes": src_block["sizes"],
            })
    
    return {
        "page": page_data["page"],
        "ordered_blocks": ordered_blocks,
        "has_visual_elements": result.has_visual_elements,
        "has_agenda": result.has_agenda,
    }


# =============================================================================
# テキスト結合（マークダウン変換）
# =============================================================================

def format_page_text(page_data: dict) -> str:
    """
    ブロックをマークダウン形式のテキストに変換
    
    タイプ変換ルール:
    - H → <!-- PAGE_HEADER --> ... <!-- /PAGE_HEADER -->
    - S → ## 見出しテキスト
    - F → <!-- PAGE_FOOTER --> ... <!-- /PAGE_FOOTER -->
    - T → そのままテキスト
    """
    lines = []
    lines.append(f"<!-- PAGE: {page_data['page']} -->")
    lines.append("")
    
    header_texts = []
    footer_texts = []
    content_lines = []
    
    for block in page_data["ordered_blocks"]:
        text = block["text"].strip()
        if not text:
            continue
        
        block_type = block.get("type", "T")
        
        if block_type == "H":
            header_texts.append(text)
        elif block_type == "S":
            content_lines.append(f"\n## {text}\n")
        elif block_type == "F":
            footer_texts.append(text)
        else:  # T or その他
            content_lines.append(text)
    
    # ヘッダーを先頭に出力
    if header_texts:
        lines.append("<!-- PAGE_HEADER -->")
        lines.append(" | ".join(header_texts))
        lines.append("<!-- /PAGE_HEADER -->")
        lines.append("")
    
    # 視覚要素がある場合はヘッダーの下に出力
    if page_data.get("has_visual_elements"):
        lines.append("<!-- VISUAL_CONTENT -->")
        lines.append("")
    
    # 目次がある場合はヘッダーの下に出力
    if page_data.get("has_agenda"):
        lines.append("<!-- AGENDA -->")
        lines.append("")
    
    # フッターを最後に出力
    if footer_texts:
        lines.append("<!-- PAGE_FOOTER -->")
        lines.append(" | ".join(footer_texts))
        lines.append("<!-- /PAGE_FOOTER -->")
        lines.append("")
    
    # コンテンツを出力
    lines.extend(content_lines)
    lines.append("")
    
    return "\n".join(lines)


# =============================================================================
# バッチ並列処理
# =============================================================================

async def process_page(doc: fitz.Document, page_number: int, llm, use_image: bool = False) -> str:
    """1ページを処理"""
    # ブロック抽出
    page_data = extract_page_blocks(doc, page_number)
    
    # LLM並び順決定
    ordered_data = await determine_block_order(page_data, llm, use_image=use_image)
    
    # テキスト変換
    page_text = format_page_text(ordered_data)
    
    return page_text


async def process_pages_batch(
    doc: fitz.Document,
    page_numbers: list[int],
    llm,
    use_image: bool = False
) -> list[tuple[int, str]]:
    """
    複数ページを並列処理
    
    Returns:
        [(page_number, page_text), ...]
    """
    tasks = [process_page(doc, pn, llm, use_image=use_image) for pn in page_numbers]
    results = await asyncio.gather(*tasks)
    return list(zip(page_numbers, results))


async def convert_pdf_with_llm(
    pdf_path: str,
    output_path: Optional[str] = None,
    start_page: int = 1,
    end_page: Optional[int] = None,
    batch_size: int = 5,
    use_image: bool = False,
    model: Optional[str] = None,
    verbose: bool = True,
) -> dict:
    """
    PDF全体をテキストに変換（メイン関数）
    
    Args:
        pdf_path: PDFファイルパス
        output_path: 出力ファイルパス（省略時は自動生成）
        start_page: 開始ページ（1始まり）
        end_page: 終了ページ（省略時は最終ページ）
        batch_size: バッチサイズ（同時処理ページ数）
        use_image: Trueの場合、ページ画像もLLMに送信（マルチモーダル）
        verbose: 進捗表示
    
    Returns:
        {"output_path": str, "total_pages": int, "processed_pages": int}
    """
    # パスを解決
    pdf_path = Path(pdf_path)
    if not pdf_path.is_absolute():
        # スクリプトのディレクトリからの相対パス、またはカレントディレクトリから解決
        script_dir = Path(__file__).parent.parent.resolve()
        if (script_dir / pdf_path).exists():
            pdf_path = script_dir / pdf_path
        else:
            pdf_path = Path.cwd() / pdf_path
    
    if not pdf_path.exists():
        raise FileNotFoundError(f"PDFファイルが見つかりません: {pdf_path}")
    
    doc = fitz.open(str(pdf_path))
    total_pages = len(doc)
    
    # ページ範囲を決定
    if end_page is None:
        end_page = total_pages
    start_page = max(1, start_page)
    end_page = min(total_pages, end_page)
    
    page_numbers = list(range(start_page, end_page + 1))
    
    if verbose:
        print(f"PDF: {pdf_path}")
        print(f"総ページ数: {total_pages}")
        print(f"処理範囲: {start_page}〜{end_page} ({len(page_numbers)}ページ)")
        print(f"バッチサイズ: {batch_size}")
        print(f"画像使用: {'あり' if use_image else 'なし'}")
        print()
    
    # LLM取得
    llm = get_llm(model=model)
    
    # バッチ処理
    all_results = []
    for i in range(0, len(page_numbers), batch_size):
        batch = page_numbers[i:i + batch_size]
        if verbose:
            print(f"処理中: ページ {batch[0]}〜{batch[-1]}...")
        
        batch_results = await process_pages_batch(doc, batch, llm, use_image=use_image)
        all_results.extend(batch_results)
    
    doc.close()
    
    # ページ順にソートして結合
    all_results.sort(key=lambda x: x[0])
    full_text = "\n".join(text for _, text in all_results)
    
    # 出力パスを決定
    if output_path is None:
        base_name = Path(pdf_path).stem
        if start_page != 1 or end_page != total_pages:
            output_path = f"{base_name}_p{start_page}-{end_page}.txt"
        else:
            output_path = f"{base_name}.txt"
    
    # ファイル出力
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(full_text)
    
    if verbose:
        print()
        print(f"出力完了: {output_path}")
    
    return {
        "output_path": output_path,
        "total_pages": total_pages,
        "processed_pages": len(page_numbers),
    }


# =============================================================================
# コマンドライン実行
# =============================================================================

def main():
    parser = argparse.ArgumentParser(
        description="PDFをテキストに変換（LLMによるブロック並び順決定付き）"
    )
    parser.add_argument("pdf_path", help="PDFファイルパス")
    parser.add_argument("-s", "--start", type=int, default=1, help="開始ページ（デフォルト: 1）")
    parser.add_argument("-e", "--end", type=int, default=None, help="終了ページ（デフォルト: 最終ページ）")
    parser.add_argument("-b", "--batch-size", type=int, default=5, help="バッチサイズ（デフォルト: 5）")
    parser.add_argument("-o", "--output", type=str, default=None, help="出力ファイルパス")
    parser.add_argument("--use-image", action="store_true", help="ページ画像もLLMに送信（マルチモーダル）")
    
    args = parser.parse_args()
    
    asyncio.run(convert_pdf_with_llm(
        pdf_path=args.pdf_path,
        output_path=args.output,
        start_page=args.start,
        end_page=args.end,
        batch_size=args.batch_size,
        use_image=args.use_image,
        verbose=True,
    ))


if __name__ == "__main__":
    main()
